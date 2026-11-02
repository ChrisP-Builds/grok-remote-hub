// Push and notification clicks only; no fetch handler and no offline cache (ADR-028).

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

function readPayload(event) {
  if (!event.data) return {};
  try {
    return event.data.json() || {};
  } catch (_) {
    return { body: event.data.text() };
  }
}

function sameOriginUrl(raw) {
  try {
    const url = new URL(raw || "/", self.location.origin);
    return url.origin === self.location.origin ? url.href : self.location.origin + "/";
  } catch (_) {
    return self.location.origin + "/";
  }
}

self.addEventListener("push", (event) => {
  const data = readPayload(event);
  const options = {
    body: String(data.body || ""),
    icon: "/icons/icon-192.png",
    data: { url: sameOriginUrl(data.url), sessionId: data.sessionId || null },
  };
  if (data.tag) {
    options.tag = String(data.tag);
    options.renotify = data.kind === "approval";
  }
  event.waitUntil(
    self.registration.showNotification(String(data.title || "Grok Remote Hub"), options)
  );
});

async function focusOrOpen(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const ours = windows.filter((c) => new URL(c.url).origin === self.location.origin);
  const shell = ours.find((c) => new URL(c.url).pathname === "/");
  if (shell) {
    await shell.focus();
    shell.postMessage({ type: "open-url", url });
    return shell;
  }
  if (ours.length) {
    try {
      const navigated = await ours[0].navigate(url);
      if (navigated) return navigated.focus();
    } catch (_) {}
  }
  return self.clients.openWindow(url);
}

function urlBase64ToUint8Array(value) {
  const padded = value + "=".repeat((4 - (value.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function currentServerKey(event) {
  const old = event.oldSubscription;
  if (old && old.options && old.options.applicationServerKey) {
    return old.options.applicationServerKey;
  }
  const res = await fetch("/api/push/config", { credentials: "same-origin" });
  const config = res.ok ? await res.json() : {};
  return config.enabled && config.publicKey ? urlBase64ToUint8Array(config.publicKey) : null;
}

async function renewSubscription(event) {
  let sub = event.newSubscription;
  if (!sub) {
    const applicationServerKey = await currentServerKey(event);
    if (!applicationServerKey) return;
    sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
  }
  await fetch("/api/push/subscribe", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subscription: sub.toJSON(), device: "Renewed subscription" }),
  });
}

self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(renewSubscription(event));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  event.waitUntil(focusOrOpen(sameOriginUrl(data.url)));
});
