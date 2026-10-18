/* =====================================================================
   Кинозал — клиент. Ванильный JS, без сборки и без зависимостей:
   страница должна открываться и на телефоне в чужой сети, и с самоподписанным
   сертификатом, где половина «умных» загрузчиков капризничает.
   ===================================================================== */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    mode: "oneshot",
    busy: false,
    notifications: 0,
    es: null,
    lastTaskId: null,
  };

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      ...options,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (response.status === 401) {
      window.location.reload();
      throw new Error("нужен вход");
    }
    if (!response.ok) {
      let detail = "";
      try {
        detail = (await response.json()).detail || "";
      } catch (_) {
        detail = await response.text();
      }
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return response.json();
  }

  // ------------------------------------------------------------------
  // Рендер сообщений
  // ------------------------------------------------------------------
  const chat = $("chat");
  const template = $("message-template");

  const ROLE_LABEL = { user: "вы", agent: "агент", system: "система" };

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function clearIntro() {
    const intro = chat.querySelector(".chat__intro");
    if (intro) intro.remove();
  }

  function renderMessage(message, { scroll = true } = {}) {
    clearIntro();
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.add(`msg--${message.role}`);
    if (message.kind === "notice") node.classList.add("msg--notice");
    if (message.kind === "error") node.classList.add("msg--error");

    node.querySelector(".msg__role").textContent =
      ROLE_LABEL[message.role] || message.role;
    node.querySelector(".msg__time").textContent = formatTime(message.created_at);
    node.querySelector(".msg__text").textContent = message.text || "";

    const actionsBox = node.querySelector(".msg__actions");
    for (const action of message.actions || []) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn--tiny";
      if (action.style === "primary") button.classList.add("btn--primary");
      if (action.style === "danger") button.classList.add("btn--danger");
      button.textContent = action.label;
      button.addEventListener("click", () => {
        if (button.disabled) return;
        runAction(action, button);
      });
      actionsBox.appendChild(button);
    }

    chat.appendChild(node);
    if (scroll) chat.scrollTop = chat.scrollHeight;
    return node;
  }

  function renderPending() {
    clearIntro();
    const node = document.createElement("article");
    node.className = "msg msg--agent";
    node.id = "pending-message";
    node.innerHTML = `
      <div class="msg__filmstrip" aria-hidden="true"></div>
      <div class="msg__body">
        <div class="msg__meta"><span class="msg__role">агент</span></div>
        <div class="msg__pending">кручу плёнку <i></i><i></i><i></i></div>
      </div>`;
    chat.appendChild(node);
    chat.scrollTop = chat.scrollHeight;
  }

  function removePending() {
    const node = $("pending-message");
    if (node) node.remove();
  }

  function renderTurn(payload) {
    removePending();
    for (const message of payload.messages || []) {
      renderMessage(message);
      if (message.kind === "notice" || message.kind === "error") {
        pushNotification(message);
      }
      if (message.task_id) state.lastTaskId = message.task_id;
    }
    refreshTasks();
  }

  // ------------------------------------------------------------------
  // Отправка
  // ------------------------------------------------------------------
  async function send(text) {
    if (state.busy) return;
    const value = (text || "").trim();
    if (!value) return;

    state.busy = true;
    $("send").disabled = true;
    $("input").value = "";
    autoGrow();

    renderMessage({
      role: "user",
      text: value,
      created_at: new Date().toISOString(),
    });
    renderPending();

    try {
      const payload = await api("/api/chat", {
        method: "POST",
        body: { text: value, mode: state.mode },
      });
      renderTurn(payload);
    } catch (error) {
      removePending();
      renderMessage({ role: "agent", text: `Ошибка: ${error.message}`, kind: "error" });
    } finally {
      state.busy = false;
      $("send").disabled = false;
      $("input").focus();
    }
  }

  async function runAction(action, button) {
    if (state.busy) return;
    state.busy = true;
    button.disabled = true;
    renderPending();
    try {
      const payload = await api("/api/action", {
        method: "POST",
        body: { kind: action.kind, payload: action.payload || {} },
      });
      renderTurn(payload);
      if (action.kind === "cancel") refreshTasks();
    } catch (error) {
      removePending();
      renderMessage({ role: "agent", text: `Ошибка: ${error.message}`, kind: "error" });
    } finally {
      state.busy = false;
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------
  // Панель задач
  // ------------------------------------------------------------------
  const ACTIVE_STATES = new Set([
    "searching",
    "awaiting_choice",
    "awaiting_confirm",
    "awaiting_setup",
    "downloading",
    "monitoring",
  ]);

  const STATE_TITLES = {
    searching: "поиск",
    awaiting_choice: "выбор варианта",
    awaiting_confirm: "ждёт подтверждения",
    awaiting_setup: "уточнение",
    downloading: "скачивание",
    monitoring: "мониторинг",
    done: "готово",
    not_found: "не найдено",
    failed: "ошибка",
    cancelled: "отменена",
  };

  function taskCard(task) {
    const card = document.createElement("div");
    card.className = `task-card task-card--${task.state}`;

    const title = document.createElement("h3");
    title.className = "task-card__title";
    title.textContent = task.params?.title || task.user_query || "без названия";

    const meta = document.createElement("div");
    meta.className = "task-card__meta";
    const stateLabel = document.createElement("span");
    stateLabel.className = "task-card__state";
    stateLabel.textContent = STATE_TITLES[task.state] || task.state;
    meta.appendChild(stateLabel);

    if (task.kind === "periodic") {
      const kind = document.createElement("span");
      kind.textContent = task.interval_minutes ? `каждые ${task.interval_minutes} мин` : "периодически";
      meta.appendChild(kind);
      const mode = document.createElement("span");
      mode.textContent = task.auto_download ? "качает сам" : "только сообщает";
      meta.appendChild(mode);
    } else {
      const kind = document.createElement("span");
      kind.textContent = "разово";
      meta.appendChild(kind);
    }

    if (task.checks_done) {
      const checks = document.createElement("span");
      checks.textContent = `проверок: ${task.checks_done}`;
      meta.appendChild(checks);
    }

    card.appendChild(title);
    card.appendChild(meta);

    if (task.last_result) {
      const line = document.createElement("div");
      line.className = "task-card__meta";
      line.textContent = task.last_result.slice(0, 120);
      card.appendChild(line);
    }
    if (task.last_error && task.state !== "cancelled") {
      const line = document.createElement("div");
      line.className = "task-card__meta";
      line.style.color = "var(--red)";
      line.textContent = task.last_error.slice(0, 160);
      card.appendChild(line);
    }

    const actions = document.createElement("div");
    actions.className = "task-card__actions";

    if (task.state === "monitoring") {
      actions.appendChild(
        smallButton("Проверить", "btn", async () => {
          await runAction(
            { kind: "check_now", payload: { task_id: task.id } },
            actions.querySelector("button")
          );
        })
      );
    }
    if (ACTIVE_STATES.has(task.state)) {
      actions.appendChild(
        smallButton("Отменить", "btn btn--danger", async (event) => {
          event.target.disabled = true;
          try {
            await api(`/api/tasks/${task.id}/cancel`, { method: "POST" });
            renderMessage({
              role: "agent",
              text: `Задача «${task.params?.title || task.user_query}» отменена.`,
              created_at: new Date().toISOString(),
            });
            refreshTasks();
          } catch (error) {
            event.target.disabled = false;
            renderMessage({ role: "agent", text: `Не смог отменить: ${error.message}`, kind: "error" });
          }
        })
      );
    }
    card.appendChild(actions);
    return card;
  }

  function smallButton(label, className, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `${className} btn--tiny`;
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
  }

  async function refreshTasks() {
    try {
      const [{ tasks }, { downloads }] = await Promise.all([
        api("/api/tasks"),
        api("/api/downloads?limit=30"),
      ]);
      const active = tasks.filter((task) => ACTIVE_STATES.has(task.state));
      const past = tasks.filter((task) => !ACTIVE_STATES.has(task.state));

      const activeBox = $("active-tasks");
      activeBox.innerHTML = "";
      $("active-count").textContent = String(active.length);
      if (!active.length) {
        activeBox.innerHTML = '<div class="task-card__meta">пока пусто</div>';
      }
      active.forEach((task) => activeBox.appendChild(taskCard(task)));

      const pastBox = $("past-tasks");
      pastBox.innerHTML = "";
      past.slice(0, 30).forEach((task) => pastBox.appendChild(taskCard(task)));

      const downloadBox = $("download-list");
      downloadBox.innerHTML = "";
      if (!downloads.length) {
        downloadBox.innerHTML = '<div class="task-card__meta">ещё ничего не скачано</div>';
      }
      downloads.slice(0, 12).forEach((item) => {
        const row = document.createElement("div");
        row.className = "task-card task-card--done";
        const heading = document.createElement("h3");
        heading.className = "task-card__title";
        heading.textContent = item.title;
        heading.style.fontSize = "12px";
        const meta = document.createElement("div");
        meta.className = "task-card__meta";
        const bits = [item.quality, item.voice].filter(Boolean).join(" · ");
        meta.textContent = [bits, item.season ? `сезон ${item.season}` : "", item.episode ? `серия ${item.episode}` : ""]
          .filter(Boolean)
          .join(" · ");
        row.appendChild(heading);
        if (meta.textContent) row.appendChild(meta);
        downloadBox.appendChild(row);
      });
    } catch (error) {
      console.warn("не удалось обновить задачи", error);
    }
  }

  // ------------------------------------------------------------------
  // Статус
  // ------------------------------------------------------------------
  async function refreshStatus() {
    const box = $("status-grid");
    box.textContent = "проверяю…";
    try {
      const status = await api("/api/status");
      const rows = [];
      const mark = (ok) => (ok ? '<span class="status-ok">ок</span>' : '<span class="status-bad">нет</span>');
      rows.push(["модель", `${status.model.name} ${mark(status.model.configured)}`]);
      rows.push(["mcp", mark(status.mcp.ready)]);
      rows.push([
        "шлюз cf",
        status.gate.ok
          ? `${mark(true)}${status.gate.logged_in ? " + сессия" : ""}`
          : `<span class="status-bad">${(status.gate.error || "нет связи").slice(0, 60)}</span>`,
      ]);
      rows.push([
        "transmission",
        status.transmission.ok
          ? `${mark(true)} ${status.transmission.version || ""}`
          : `<span class="status-bad">${(status.transmission.error || "").slice(0, 80)}</span>`,
      ]);
      if (status.storage) {
        const disk = status.storage;
        rows.push([
          `диск ${disk.volume || "?"}`,
          disk.ok
            ? `${mark(true)} свободно ${disk.free_human} из ${disk.total_human}`
            : `<span class="status-bad">${(disk.error || "нет доступа").slice(0, 80)}</span>`,
        ]);
      }
      rows.push(["в работе", String(status.tasks.active)]);
      box.innerHTML = rows
        .map(([key, value]) => `<b>${key}</b><span>${value}</span>`)
        .join("");
    } catch (error) {
      box.innerHTML = `<b>статус</b><span class="status-bad">${error.message}</span>`;
    }
  }

  // ------------------------------------------------------------------
  // Уведомления
  // ------------------------------------------------------------------
  function pushNotification(message) {
    state.notifications += 1;
    $("bell-dot").hidden = false;
    const list = $("notification-list");
    const item = document.createElement("div");
    item.className = "notification-item";
    if (message.kind === "error") item.classList.add("notification-item--error");
    item.textContent = message.text || "";
    list.prepend(item);
    while (list.children.length > 40) list.lastChild.remove();

    if (window.Notification && Notification.permission === "granted") {
      try {
        new Notification("Кинозал", { body: (message.text || "").slice(0, 180), icon: "/static/icon-192.png" });
      } catch (_) {
        /* на самоподписанном сертификате браузер может отказать — это не ошибка */
      }
    }
  }

  function connectEvents() {
    if (state.es) state.es.close();
    const source = new EventSource("/api/events");
    state.es = source;
    source.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      if (data.type === "hello") return;
      if (data.type === "notification") {
        if (data.message) {
          renderMessage(data.message);
          pushNotification(data.message);
        }
        refreshTasks();
      } else if (data.type === "download_done" || data.type === "task_cancelled") {
        refreshTasks();
      }
    };
    source.onerror = () => {
      source.close();
      state.es = null;
      setTimeout(connectEvents, 5000);
    };
  }

  function urlBase64ToUint8Array(base64) {
    const padding = "=".repeat((4 - (base64.length % 4)) % 4);
    const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(normalized);
    return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)));
  }

  async function subscribePush() {
    // Web Push требует доверенного HTTPS: с самоподписанным сертификатом
    // браузер откажет в регистрации service worker'а. Это не поломка —
    // остаются центр уведомлений и системные уведомления macOS.
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) return "нет поддержки";
    try {
      const registration = await navigator.serviceWorker.register("/sw.js");
      const { key } = await api("/api/push/key");
      if (!key) return "нет ключа";
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
      await api("/api/push/subscribe", {
        method: "POST",
        body: { subscription: subscription.toJSON() },
      });
      return "push включён";
    } catch (error) {
      console.warn("web push недоступен", error);
      return "push недоступен (сертификат?)";
    }
  }

  async function enableNotifications() {
    if (!("Notification" in window)) {
      alert("Этот браузер не умеет системные уведомления — пользуйтесь колокольчиком ✦");
      return;
    }
    const permission = await Notification.requestPermission();
    let note = "";
    if (permission === "granted") {
      new Notification("Кинозал", { body: "Уведомления включены", icon: "/static/icon-192.png" });
      note = await subscribePush();
    } else {
      note = "разрешение не выдано";
    }
    renderMessage({
      role: "system",
      text: `Уведомления: ${permission}. ${note}`,
      created_at: new Date().toISOString(),
    });
  }

  // ------------------------------------------------------------------
  // Настройки доступов
  // ------------------------------------------------------------------
  function openSettings() {
    $("settings-modal").hidden = false;
    $("settings-result").hidden = true;
  }

  function closeSettings() {
    $("settings-modal").hidden = true;
  }

  async function saveSettings() {
    const result = $("settings-result");
    result.hidden = false;
    result.textContent = "сохраняю…";
    try {
      const payload = {
        kinozal_username: $("kz-user").value.trim(),
        kinozal_password: $("kz-pass").value,
        transmission_username: $("tr-user").value.trim(),
        transmission_password: $("tr-pass").value,
      };
      const data = await api("/api/settings", { method: "POST", body: payload });
      result.textContent =
        "сохранено\n\n" +
        `kinozal: сессия ${data.status.gate.logged_in ? "есть" : "нет"}\n` +
        `transmission: ${data.status.transmission.ok ? "связь есть" : (data.status.transmission.error || "нет связи")}`;
      refreshStatus();
    } catch (error) {
      result.textContent = `ошибка: ${error.message}`;
    }
  }

  async function testSettings() {
    const result = $("settings-result");
    result.hidden = false;
    result.textContent = "проверяю (вход на трекер занимает до минуты)…";
    try {
      const data = await api("/api/settings/test", { method: "POST" });
      result.textContent =
        `kinozal: ${data.kinozal.ok ? "вход выполнен" : "вход не удался"}\n` +
        `${JSON.stringify(data.kinozal.detail, null, 2).slice(0, 400)}\n\n` +
        `transmission: ${data.transmission.ok ? "связь есть" : "ошибка"} ` +
        `${data.transmission.error || data.transmission.version || ""}`;
      refreshStatus();
    } catch (error) {
      result.textContent = `ошибка: ${error.message}`;
    }
  }

  // ------------------------------------------------------------------
  // Журнал отладки
  // ------------------------------------------------------------------
  const debugState = { open: false, filter: "all", last: null, events: [] };

  const CATEGORY_TITLES = {
    all: "всё",
    llm: "модель",
    mcp: "трекер",
    gate: "шлюз",
    transmission: "transmission",
    agent: "агент",
    log: "логи",
  };

  function debugRow(item) {
    const wrap = document.createElement("article");
    wrap.className = `debug__item debug__item--${item.category} debug__item--${item.level}`;

    const row = document.createElement("div");
    row.className = "debug__row";

    const time = document.createElement("span");
    time.className = "debug__time";
    time.textContent = item.time;

    const cat = document.createElement("span");
    cat.className = "debug__cat";
    cat.textContent = item.category;

    const title = document.createElement("span");
    title.className = "debug__title";
    title.textContent = item.title;

    row.append(time, cat, title);

    if (item.duration_ms !== null && item.duration_ms !== undefined) {
      const ms = document.createElement("span");
      ms.className = "debug__ms";
      ms.textContent = item.duration_ms >= 1000
        ? `${(item.duration_ms / 1000).toFixed(1)} с`
        : `${Math.round(item.duration_ms)} мс`;
      row.appendChild(ms);
    }

    const detail = document.createElement("div");
    detail.className = "debug__detail";
    detail.hidden = true;
    const entries = Object.entries(item.detail || {});
    if (!entries.length) {
      detail.hidden = false;
      const empty = document.createElement("div");
      empty.className = "debug__pair";
      empty.textContent = "подробностей нет";
      detail.appendChild(empty);
    }
    for (const [key, value] of entries) {
      if (value === null || value === undefined || value === "" ) continue;
      const pair = document.createElement("div");
      pair.className = "debug__pair";
      const name = document.createElement("span");
      name.className = "debug__key";
      name.textContent = key;
      const val = document.createElement("span");
      val.className = "debug__value";
      val.textContent = typeof value === "object" ? JSON.stringify(value, null, 1) : String(value);
      pair.append(name, val);
      detail.appendChild(pair);
    }

    row.addEventListener("click", () => {
      detail.hidden = !detail.hidden;
    });

    wrap.append(row, detail);
    return wrap;
  }

  function renderDebugFilters(categories) {
    const box = $("debug-filters");
    const known = ["all", "llm", "mcp", "gate", "transmission", "agent", "log"];
    const extra = categories.filter((name) => !known.includes(name));
    box.innerHTML = "";
    for (const name of [...known, ...extra]) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (debugState.filter === name ? " is-active" : "");
      chip.textContent = CATEGORY_TITLES[name] || name;
      chip.addEventListener("click", () => {
        debugState.filter = name;
        loadDebug();
      });
      box.appendChild(chip);
    }
  }

  async function loadDebug() {
    try {
      const data = await api(`/api/debug?limit=200&category=${encodeURIComponent(debugState.filter)}`);
      debugState.events = data.events || [];
      renderDebugFilters(data.categories || []);
      const list = $("debug-list");
      list.innerHTML = "";
      // Новые — сверху: свежие события важнее старых.
      [...debugState.events].reverse().forEach((item) => list.appendChild(debugRow(item)));
      $("debug-empty").hidden = debugState.events.length > 0;
    } catch (error) {
      $("debug-empty").hidden = false;
      $("debug-empty").textContent = `не удалось прочитать журнал: ${error.message}`;
    }
  }

  function openDebug() {
    debugState.open = true;
    $("debug-modal").hidden = false;
    loadDebug();
    if (debugState.timer) clearInterval(debugState.timer);
    debugState.timer = setInterval(() => {
      if (debugState.open && !$("debug-modal").hidden) loadDebug();
    }, 4000);
  }

  function closeDebug() {
    debugState.open = false;
    $("debug-modal").hidden = true;
    if (debugState.timer) clearInterval(debugState.timer);
    debugState.timer = null;
  }

  async function clearDebug() {
    await api("/api/debug", { method: "DELETE" });
    await loadDebug();
  }

  async function copyDebug() {
    const text = debugState.events
      .map((item) => {
        const parts = Object.entries(item.detail || {})
          .filter(([, v]) => v !== null && v !== undefined && v !== "")
          .map(([k, v]) => `    ${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`);
        return `[${item.time}] ${item.category} · ${item.title}${
          item.duration_ms ? ` (${Math.round(item.duration_ms)} мс)` : ""
        }${parts.length ? "\n" + parts.join("\n") : ""}`;
      })
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(text || "журнал пуст");
      $("debug-copy").textContent = "Скопировано";
      setTimeout(() => ($("debug-copy").textContent = "Скопировать"), 1500);
    } catch (error) {
      renderMessage({ role: "system", text: `копирование недоступно: ${error.message}` });
    }
  }

  // ------------------------------------------------------------------
  // Управление
  // ------------------------------------------------------------------
  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".mode-btn").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.mode === mode);
    });
    const hints = {
      oneshot: "Разово: найду сейчас и скачаю после вашего «да»",
      periodic: "Периодически: буду проверять трекер и сообщать о новом",
    };
    $("composer-hint").textContent = hints[mode] + " · Enter — отправить";
    $("input").focus();
  }

  function autoGrow() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  }

  // ------------------------------------------------------------------
  // Инициализация
  // ------------------------------------------------------------------
  async function loadHistory() {
    try {
      const data = await api("/api/history");
      if (!data.messages || !data.messages.length) return;
      clearIntro();
      data.messages.forEach((row) => {
        renderMessage(
          {
            role: row.role,
            text: row.text,
            kind: row.kind,
            actions: row.actions || [],
            created_at: row.created_at,
            task_id: row.task_id,
          },
          { scroll: false }
        );
      });
      chat.scrollTop = chat.scrollHeight;
    } catch (error) {
      console.warn("история не загрузилась", error);
    }
  }

  function bind() {
    document.querySelectorAll(".mode-btn").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.mode));
    });

    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      send($("input").value);
    });

    const input = $("input");
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send(input.value);
      }
    });
    input.addEventListener("input", autoGrow);

    $("panel-open").addEventListener("click", () => {
      $("panel").classList.add("is-open");
      $("panel-scrim").classList.add("is-visible");
    });
    const closePanel = () => {
      $("panel").classList.remove("is-open");
      $("panel-scrim").classList.remove("is-visible");
    };
    $("panel-close").addEventListener("click", closePanel);
    $("panel-scrim").addEventListener("click", closePanel);

    $("toggle-archive").addEventListener("click", (event) => {
      const box = $("past-tasks");
      box.hidden = !box.hidden;
      event.target.textContent = box.hidden ? "показать" : "скрыть";
    });

    $("bell").addEventListener("click", () => {
      const tray = $("notification-tray");
      tray.hidden = !tray.hidden;
      if (!tray.hidden) {
        state.notifications = 0;
        $("bell-dot").hidden = true;
      }
    });
    $("clear-notifications").addEventListener("click", () => {
      $("notification-list").innerHTML = "";
    });

    $("refresh-status").addEventListener("click", refreshStatus);
    $("enable-notifications").addEventListener("click", enableNotifications);
    $("open-debug").addEventListener("click", openDebug);
    $("debug-close").addEventListener("click", closeDebug);
    $("debug-refresh").addEventListener("click", loadDebug);
    $("debug-clear").addEventListener("click", clearDebug);
    $("debug-copy").addEventListener("click", copyDebug);
    $("open-settings").addEventListener("click", openSettings);
    $("close-settings").addEventListener("click", closeSettings);
    $("save-settings").addEventListener("click", saveSettings);
    $("test-settings").addEventListener("click", testSettings);
    $("logout").addEventListener("click", async () => {
      await api("/api/logout", { method: "POST" });
      window.location.reload();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeSettings();
        closeDebug();
        $("notification-tray").hidden = true;
        closePanel();
      }
    });
  }

  async function init() {
    bind();
    setMode("oneshot");
    await loadHistory();
    await refreshTasks();
    await refreshStatus();
    connectEvents();
    setInterval(refreshTasks, 20000);
    setInterval(refreshStatus, 60000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
