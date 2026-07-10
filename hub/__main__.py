from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from hub.config import load_config
from hub.server import Hub, resolve_bind_hosts


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


async def _run(hub: Hub, hosts: list[str], port: int) -> None:
    app = hub.build_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    sites: list[web.TCPSite] = []
    log = logging.getLogger("hub")
    try:
        for host in hosts:
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()
            sites.append(site)
            log.info("Listening on http://%s:%s", host, port)
        # Block until cancelled (Ctrl+C / process kill)
        stop = asyncio.Event()
        try:
            await stop.wait()
        except asyncio.CancelledError:
            raise
    finally:
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

    hub.bind_hosts = hosts
    hub.bind_host = ts_ip if ts_ip and ts_ip in hosts else hosts[0]
    hub.bind_mode = mode
    hub.tailscale_ip = ts_ip

    log.info(
        "Starting Grok Remote Hub on %s (%s)",
        ", ".join(f"http://{h}:{config.bind_port}" for h in hosts),
        mode,
    )
    if ts_ip:
        log.info("Local URL:     http://127.0.0.1:%s", config.bind_port)
        log.info("Tailscale URL: http://%s:%s", ts_ip, config.bind_port)
    if mode == "local":
        log.warning("Tailscale IP not found; binding local only")

    try:
        asyncio.run(_run(hub, hosts, config.bind_port))
    except KeyboardInterrupt:
        log.info("Shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
