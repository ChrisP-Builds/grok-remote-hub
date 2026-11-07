from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from hub.config import Config, load_config
from hub.harden_policy import require_hub_token
from hub.http_guard import build_allowed_hosts, normalize_host, tailscale_owner_login
from hub.server import Hub, _tailscale_ip, resolve_bind_hosts, tailscale_dns_names, tailscale_status

TAILSCALE_RETRY_SECONDS = 15.0


def _tailscale_owner_login() -> str | None:
    status = tailscale_status()
    return tailscale_owner_login(status) if status else None


def configure_tailscale(
    hub: Hub,
    config: Config,
    hosts: list[str],
    mode: str,
    ts_ip: str | None,
    dns_names: list[str],
    owner: str | None,
) -> None:
    """Bind facts, accepted Host names and Tailscale Serve login trust (ADR-026, ADR-028)."""
    log = logging.getLogger("hub")
    hub.bind_hosts = hosts
    hub.bind_host = ts_ip if ts_ip and ts_ip in hosts else hosts[0]
    hub.bind_mode = mode
    hub.tailscale_ip = ts_ip
    hub.guard.allowed_hosts = build_allowed_hosts(
        hosts,
        ts_ip,
        dns_names,
        [*config.allowed_hosts, socket.gethostname()],
    )
    log.info("Accepted Host names: %s", ", ".join(sorted(hub.guard.allowed_hosts)))
    hub.guard.serve_hosts = frozenset(normalize_host(n) for n in dns_names)
    hub.guard.tailscale_logins = frozenset(
        login.casefold() for login in [owner or "", *config.allowed_tailscale_logins] if login
    )
    hub.serve_url = f"https://{normalize_host(dns_names[0])}/" if dns_names else None
    log.info(
        "Tailscale Serve: %s; %d login(s) trusted without token (owner %s)",
        hub.serve_url or "no DNS name",
        len(hub.guard.tailscale_logins),
        "found" if owner else "not found",
    )


async def adopt_tailscale_when_up(
    hub: Hub,
    add_site: Callable[[str], Awaitable[None]],
    *,
    find_ip: Callable[[], str | None] = _tailscale_ip,
    find_dns_names: Callable[[], list[str]] = tailscale_dns_names,
    find_owner: Callable[[], str | None] = _tailscale_owner_login,
    interval: float = TAILSCALE_RETRY_SECONDS,
) -> str | None:
    """A hub that booted before Tailscale connected binds its IP and trusts Serve once it is up."""
    log = logging.getLogger("hub")
    while not (ip := await asyncio.to_thread(find_ip)):
        await asyncio.sleep(interval)
    config = hub.config
    hosts = ["127.0.0.1", ip]
    ok, err = require_hub_token(hosts, config.hub_token or "", allowed_hosts=config.allowed_hosts)
    if not ok:
        log.error("Tailscale is up but the hub stays local only: %s", err)
        return None
    try:
        await add_site(ip)
    except OSError as exc:
        log.warning("Tailscale is up but %s:%s could not be bound: %s", ip, config.bind_port, exc)
        hosts = ["127.0.0.1"]
    dns_names = await asyncio.to_thread(find_dns_names)
    owner = await asyncio.to_thread(find_owner)
    configure_tailscale(hub, config, hosts, "tailscale", ip, dns_names, owner)
    log.info("Tailscale came up after start; now serving the tailnet")
    return ip


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = log_dir / f"hub-{day}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _ignore_proactor_reset(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Demote normal client disconnects (ConnectionReset / WinError 10054) to debug."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        logging.getLogger("asyncio").debug(
            "connection reset (client closed): %s", exc
        )
        return
    msg = context.get("message", "")
    if "ConnectionResetError" in str(exc) or "10054" in str(msg) or "10054" in str(exc):
        logging.getLogger("asyncio").debug("%s", context)
        return
    loop.default_exception_handler(context)


async def _run(hub: Hub, hosts: list[str], port: int) -> None:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_ignore_proactor_reset)

    app = hub.build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    sites: list[web.TCPSite] = []
    log = logging.getLogger("hub")

    async def add_site(host: str) -> None:
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
        sites.append(site)
        log.info("Listening on http://%s:%s", host, port)

    adopter: asyncio.Task | None = None
    try:
        for host in hosts:
            await add_site(host)
        if hub.bind_mode == "local" and not hub.config.bind_host:
            adopter = asyncio.create_task(adopt_tailscale_when_up(hub, add_site), name="tailscale-adopt")
        # Block until cancelled (Ctrl+C / process kill)
        stop = asyncio.Event()
        try:
            await stop.wait()
        except asyncio.CancelledError:
            raise
    finally:
        if adopter is not None:
            adopter.cancel()
        for site in sites:
            try:
                await site.stop()
            except Exception:
                pass
        await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grok Remote Hub")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.toml")
    parser.add_argument("--host", type=str, default=None, help="Override bind host")
    parser.add_argument("--port", type=int, default=None, help="Override bind port")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.port is not None:
        config.bind_port = args.port
    if args.host is not None:
        config.bind_host = args.host

    _setup_logging(config.log_dir)
    log = logging.getLogger("hub")

    hub = Hub(config)
    try:
        hosts, mode, ts_ip = resolve_bind_hosts(config)
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    token_ok, token_err = require_hub_token(
        hosts, config.hub_token or "", allowed_hosts=config.allowed_hosts
    )
    if not token_ok:
        log.error("%s", token_err)
        return 2
    if str(config.hub_token or "").strip():
        log.info("hub_token configured (remote auth required)")
    else:
        log.warning("hub_token empty; allowed for loopback-only bind")

    dns_names = tailscale_dns_names() if ts_ip else []
    owner = _tailscale_owner_login() if ts_ip else None
    configure_tailscale(hub, config, hosts, mode, ts_ip, dns_names, owner)

    log.info(
        "Starting Grok Remote Hub on %s (%s)",
        ", ".join(f"http://{h}:{config.bind_port}" for h in hosts),
        mode,
    )
    if ts_ip:
        log.info("Local URL:     http://127.0.0.1:%s", config.bind_port)
        log.info("Tailscale URL: http://%s:%s", ts_ip, config.bind_port)
    if mode == "local" and not config.bind_host:
        log.warning(
            "Tailscale IP not found; binding local only and checking again every %.0fs",
            TAILSCALE_RETRY_SECONDS,
        )

    try:
        asyncio.run(_run(hub, hosts, config.bind_port))
    except KeyboardInterrupt:
        log.info("Shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
