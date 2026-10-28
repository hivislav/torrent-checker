/* Service worker: нужен, чтобы приложение можно было «поставить» на телефон и
   чтобы уведомления доходили при свёрнутой вкладке. Кэш — только оболочка
   (HTML/CSS/JS), данные всегда тянутся из сети: раздачи меняются каждую минуту. */
const SHELL = "kinozal-shell-v2";
const ASSETS = ["/", "/static/style.css", "/static/app.js", "/static/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(ASSETS).catch(() => undefined))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== SHELL).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // API и события — всегда из сети.
  if (url.pathname.startsWith("/api/") || url.pathname === "/sw.js") return;
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(SHELL).then((cache) => cache.put(event.request, copy)).catch(() => undefined);
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match("/")))
  );
});

self.addEventListener("push", (event) => {
  let data = { title: "Кинозал", body: "Есть новости по задаче" };
  try {
    data = event.data.json();
  } catch (_) {
    /* push без JSON — показываем заглушку */
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Кинозал", {
      body: data.body || "",
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      data: { url: "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(self.registration.scope)) {
          return client.focus();
        }
      }
      return self.clients.openWindow("/");
    })
  );
});
