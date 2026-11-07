# ADR-028: Serve over Tailscale HTTPS, trust the Tailscale login, and send web push

- **Status:** Accepted (amended 2026-11-07)
- **Date:** 2026-11-01
- **Deciders:** project owner, Grok Remote Hub session
- **Tags:** auth, tailscale, https, pwa, push, mobile

## Context

Phone approvals (ADR-027) only help if the phone gets a nudge when the page is
closed. Web push needs a secure context and, on iPhone, a Home Screen web app.
Today the hub is `http://100.x.y.z:8787/?token=...`; Home Screen apps have
storage separate from Safari, so the token does not carry over. A spike
(2026-11-01) behind `tailscale serve` showed: requests reach the backend from
`127.0.0.1`, `Host` keeps the `*.ts.net` name, Serve adds `Tailscale-User-Login`
for the calling user and **replaces** a client-forged copy of that header.

## Decision

1. **HTTPS address.** `tailscale serve --bg 8787` (tailnet only, not Funnel)
   gives `https://<machine>.<tailnet>.ts.net/`. The direct `100.x:8787` bind and
   the token stay as a fallback.
2. **Trust the Tailscale login** only when all hold: peer is loopback, `Host`
   is this machine's Serve name, and `Tailscale-User-Login` is the machine
   owner's login (from `tailscale status`) or in `hub.allowed_tailscale_logins`.
   Such requests need no token. Anything else still needs the token.
3. **Home Screen app.** A web app manifest, icons and a minimal service worker
   (push and notification clicks only, no offline cache) at `/sw.js`.
4. **Web push** with VAPID (`pywebpush`), keys in `data/vapid.json`,
   subscriptions in `data/push-subscriptions.json`, dead endpoints pruned.
   Pushed when no open, visible page shows that session: approval needed,
   agent question, and a turn that ran 60 s or more finishing or failing.
   Pages report visibility over the WebSocket (`presence`).
5. **Lock-screen privacy.** Notifications name the project and category,
   never the command, path or prompt text.
6. **Phone setup.** The desktop UI shows the HTTPS address as a QR code
   (rendered server-side with `segno`) with the three setup steps.

## Alternatives considered

- **Paste the token once in the Home Screen app:** works, but friction on
  every new device and the token lives in another store.
- **HttpOnly cookie:** still needs a first login inside the Home Screen app.
- **Tailscale Funnel / public relay:** exposes the hub to the internet.

## Consequences

### Positive

- One-tap app on the phone, no token, notifications for approvals.

### Negative

- Any device signed in to Tailscale as the owner opens the hub without a token
  (similar to a token saved in each browser today).
- A local process on the PC can forge the headers on `127.0.0.1`; local
  processes are already trusted (they can read `config.toml`).
- Push metadata (timing, size, endpoint) passes Apple's or Google's push
  service; content is end-to-end encrypted. Two new dependencies.
- The machine and tailnet names appear in public Certificate Transparency logs.

## Validation

- Guard tests: trusted only for loopback + Serve host + allowed login.
- Push tests: subscription store, pruning on 404/410, trigger suppression
  while a visible page shows the session, notification text has no command.
- Live: phone opens the Serve address without a token, Add to Home Screen,
  receives a push for an approval and opens the card from it.

## Related

- ADR-026 Host/Origin guard; ADR-027 phone approvals

## Notes (2026-11-01)

Review hardening: a page with no input for 2 minutes counts as not visible,
and the hub re-checks pending approvals and questions every few seconds so a
nudge still goes out after the desktop is left focused. Subscriptions are
re-posted on every load, endpoints are limited to the known push services
with redirects off, and token-created subscriptions are dropped when the
token changes. The VAPID `sub` claim carries `https://<machine>.<tailnet>.ts.net`,
so the push services also see the machine name (already public via CT logs).

## Amendment (2026-11-07): adopt Tailscale when it connects after the hub

After a reboot the hub can start before Tailscale connects. It then found no
Tailscale IP or Serve name, bound loopback only, and answered every request
through the Serve address with 403 until someone restarted it.

Now a hub in auto bind mode that starts local-only checks again every 15 s.
Once Tailscale is up it binds the Tailscale IP and applies the same Host
allowlist and Serve login trust as a normal start, with no restart. The
token gate still applies: with an empty `hub_token` it stays local. All
`tailscale` CLI calls run without a console window, since the hub runs
under pythonw. Validation: `tests/test_tailscale_late.py`.
