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
    // Открытая задача = открытый чат. У каждой задачи своя переписка и контекст.
    taskId: null,
    introHTML: null,
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
      // Тело читаем ОДИН раз: response.json() забирает поток, и повторный
      // response.text() падал с «body stream already read» вместо текста ошибки.
      let detail = "";
      const raw = await response.text();
      try {
        detail = JSON.parse(raw).detail || "";
      } catch (_) {
        detail = raw;
      }
      throw new Error(String(detail || `HTTP ${response.status}`).slice(0, 400));
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

  // «18.10, 12:40» — для журнала проверок: там важен и день, а не только время.
  function formatStamp(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return `${date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })}, ` +
      date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  // «12:40» сегодня, «завтра в 12:40», «20.10 в 12:40» — иначе у задачи раз в
  // сутки «следующая 11:30» читается как «через час».
  function formatWhen(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const time = date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    const today = new Date();
    const dayOf = (d) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const tomorrow = new Date(today.getTime() + 24 * 3600 * 1000);
    if (dayOf(date) === dayOf(today)) return time;
    if (dayOf(date) === dayOf(tomorrow)) return `завтра в ${time}`;
    return `${date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })} в ${time}`;
  }

  function clearIntro() {
    const intro = chat.querySelector(".chat__intro");
    if (intro) intro.remove();
  }

  function rememberIntro() {
    const intro = chat.querySelector(".chat__intro");
    if (intro && !state.introHTML) state.introHTML = intro.outerHTML;
  }

  function showIntro() {
    if (!state.introHTML) return;
    if (chat.querySelector(".chat__intro")) return;
    chat.insertAdjacentHTML("afterbegin", state.introHTML);
  }

  function clearChat() {
    chat.querySelectorAll(".msg").forEach((node) => node.remove());
    showIntro();
  }

  async function openConversation(taskId) {
    if (state.busy) return;
    state.taskId = taskId || null;
    clearChat();
    const known = (state.tasks || []).find((task) => task.id === taskId);
    showCurrentTask(known ? known.params?.title || known.user_query || "" : "");
    if (taskId) {
      try {
        const data = await api(`/api/history?task_id=${encodeURIComponent(taskId)}`);
        const rows = data.messages || [];
        let lastActionIndex = -1;
        rows.forEach((row, index) => {
          if ((row.actions || []).length) lastActionIndex = index;
        });
        rows.forEach((row, index) => {
          renderMessage(
            {
              role: row.role,
              text: row.text,
              kind: row.kind,
              actions: row.actions || [],
              payload: row.payload || {},
              created_at: row.created_at,
              task_id: row.task_id,
            },
            { scroll: false, answered: answeredAlready(row, index, lastActionIndex) }
          );
        });
      } catch (error) {
        console.warn("история задачи не загрузилась", error);
      }
    }
    chat.scrollTop = chat.scrollHeight;
    refreshTasks();
  }

  function showCurrentTask(title) {
    const node = $("current-task");
    if (!node) return;
    const text = title ? `чат: «${title}»` : "";
    node.textContent = text;
    node.hidden = !text;
    const subtitle = $("marquee-subtitle");
    if (subtitle) subtitle.hidden = Boolean(text);
  }

  function startNewTask() {
    state.taskId = null;
    clearChat();
    showCurrentTask("");
    $("input").focus();
    refreshTasks();
  }

  function answeredAlready(message, index, lastActionIndex) {
    const resolved = message.payload && message.payload.resolved;
    if (resolved === true) return true;
    // Старые сообщения (до появления флага) считаем отвеченными, если после
    // них было ещё одно сообщение с кнопками.
    if (resolved === false) return false;
    if (typeof index === "number" && typeof lastActionIndex === "number") {
      return index < lastActionIndex;
    }
    return false;
  }

  function lockMessage(node) {
    node.classList.add("msg--answered");
    node.querySelectorAll(".msg__actions button").forEach((button) => {
      button.disabled = true;
    });
  }

  function lockAllMessages() {
    chat.querySelectorAll(".msg:not(#pending-message)").forEach((node) => {
      if (node.querySelector(".msg__actions button")) lockMessage(node);
    });
  }

  function renderMessage(message, { scroll = true, answered = false } = {}) {
    clearIntro();
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.add(`msg--${message.role}`);
    if (message.kind === "notice") node.classList.add("msg--notice");
    if (message.kind === "error") node.classList.add("msg--error");

    node.querySelector(".msg__role").textContent =
      ROLE_LABEL[message.role] || message.role;
    node.querySelector(".msg__time").textContent = formatTime(message.created_at);
    node.querySelector(".msg__text").textContent = message.text || "";

    renderActions(node.querySelector(".msg__actions"), message.actions || []);
    if (answered) lockMessage(node);

    chat.appendChild(node);
    if (scroll) chat.scrollTop = chat.scrollHeight;
    return node;
  }

  // Кнопки сообщения: сгруппированы по смыслу («Качество», «Озвучка», «Серии»).
  // Кнопки с multi включаются/выключаются без похода на сервер, а выбранное
  // уходит одним действием «Скачать выбранные (N)».
  function renderActions(box, actions) {
    if (!box || !actions.length) return;
    box.innerHTML = "";
    // id -> {season, episode}: серия без сезона в пачке на несколько сезонов
    // означает сразу несколько файлов, поэтому запоминаем пару.
    const picked = new Map();
    const sizes = new Map(); // id -> размер в байтах

    const groups = new Map();
    for (const action of actions) {
      const key = action.group || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(action);
    }

    let submitButton = null;
    const humanSize = (bytes) => {
      if (!bytes) return "";
      const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
      let value = bytes;
      let index = 0;
      while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index += 1;
      }
      const text = index === 0 ? String(value) : value.toFixed(1).replace(".", ",");
      return `${text} ${units[index]}`;
    };
    const refreshSubmit = () => {
      if (!submitButton) return;
      let total = 0;
      for (const id of picked.keys()) total += sizes.get(id) || 0;
      const sizeText = total ? ` · ${humanSize(total)}` : "";
      submitButton.textContent = `Скачать выбранные (${picked.size}${sizeText})`;
      submitButton.disabled = picked.size === 0;
    };

    for (const [groupName, items] of groups) {
      const wrap = document.createElement("div");
      wrap.className = "msg__action-group";
      if (groupName) {
        const caption = document.createElement("div");
        caption.className = "msg__action-caption";
        caption.textContent = groupName;
        wrap.appendChild(caption);
      }
      const row = document.createElement("div");
      row.className = "msg__action-row";

      for (const action of items) {
        // Кнопка-ссылка: уходит на трекер в новой вкладке, на сервер не ходит.
        // Рисуем именно <a>: на телефоне это открывается в браузере, а не в PWA.
        if (action.kind === "link" && action.url) {
          const link = document.createElement("a");
          link.className = "btn btn--tiny btn--link";
          link.href = action.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = action.label;
          row.appendChild(link);
          continue;
        }

        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--tiny";
        if (action.style === "primary") button.classList.add("btn--primary");
        if (action.style === "danger") button.classList.add("btn--danger");
        button.textContent = action.label;

        if (action.multi) {
          button.classList.add("btn--multi");
          const value = {
            season: action.payload?.season ?? null,
            episode: action.payload?.episode,
          };
          if (action.selected) {
            button.classList.add("is-selected");
            picked.set(action.id, value);
            sizes.set(action.id, action.payload.size || 0);
          }
          button.addEventListener("click", () => {
            const on = !button.classList.contains("is-selected");
            button.classList.toggle("is-selected", on);
            if (on) {
              picked.set(action.id, value);
              sizes.set(action.id, action.payload.size || 0);
            } else {
              picked.delete(action.id);
              sizes.delete(action.id);
            }
            refreshSubmit();
          });
          row.appendChild(button);
          continue;
        }

        if (action.kind === "download_selected") {
          submitButton = button;
          button.addEventListener("click", () => {
            if (button.disabled) return;
            const episodes = [...picked.values()].filter(
              (item) => item && item.episode !== undefined && item.episode !== null
            );
            runAction({ ...action, payload: { ...action.payload, episodes } }, button);
          });
          row.appendChild(button);
          continue;
        }

        // «Подробнее» — чтение, а не действие: открываем окно и НЕ отправляем
        // ход в агента. Иначе прошлое сообщение с кнопками стало бы неактивным,
        // и подтвердить скачивание было бы нечем.
        if (action.kind === "details") {
          button.addEventListener("click", () => {
            showTorrentDetails(action.payload?.torrent_id, button);
          });
          row.appendChild(button);
          continue;
        }

        button.addEventListener("click", () => {
          if (button.disabled) return;
          runAction(action, button);
        });
        row.appendChild(button);
      }

      wrap.appendChild(row);
      box.appendChild(wrap);
    }

    refreshSubmit();
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
    // Новый ответ = прошлый вопрос отвечен: его кнопки больше не нажимаются.
    lockAllMessages();
    for (const message of payload.messages || []) {
      renderMessage(message);
      if (message.kind === "notice" || message.kind === "error") {
        pushNotification(message);
      }
      if (message.task_id) state.lastTaskId = message.task_id;
    }
    refreshTasks();
    // Освободилось/занялось место — строка про диск не должна врать.
    refreshStatus();
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
        body: { text: value, mode: state.mode, task_id: state.taskId || null },
      });
      const created = payload.task && payload.task.id;
      if (created && created !== state.taskId) {
        state.taskId = created;
        clearChat();
        showCurrentTask(payload.task.params?.title || payload.task.user_query || "");
      }
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
    const owner = button.closest(".msg");
    if (owner) lockMessage(owner);
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
      // Кнопку не возвращаем: сообщение уже отвечено, второй раз не нажать.
    }
  }

  // ------------------------------------------------------------------
  // Сворачиваемые секции картотеки
  // ------------------------------------------------------------------
  // Состояние «свёрнуто/развёрнуто» хранится в localStorage: панель не должна
  // каждый раз показывать одно и то же в неудобном виде.
  const SECTIONS = {
    active: { list: "active-tasks", hint: true, defaultHidden: false },
    finished: { list: "past-tasks", hint: true, defaultHidden: true },
    downloads: { list: "download-list", hint: true, defaultHidden: false },
  };

  const sectionKey = (name) => `kino.section.${name}`;

  function sectionHidden(name) {
    const config = SECTIONS[name];
    if (!config) return false;
    try {
      const stored = localStorage.getItem(sectionKey(name));
      if (stored !== null) return stored === "hidden";
    } catch (_) {
      /* приватный режим — просто не запоминаем */
    }
    return config.defaultHidden;
  }

  function applySection(name, hidden) {
    const config = SECTIONS[name];
    if (!config) return;
    const box = $(config.list);
    const button = document.querySelector(`[data-toggle="${name}"]`);
    const section = document.querySelector(`[data-section="${name}"]`);
    if (box) box.hidden = hidden;
    if (button) button.textContent = hidden ? "показать" : "скрыть";
    if (section) section.classList.toggle("is-collapsed", hidden);
  }

  function toggleSection(name) {
    const hidden = !sectionHidden(name);
    try {
      localStorage.setItem(sectionKey(name), hidden ? "hidden" : "shown");
    } catch (_) {
      /* запомнить не получилось — не беда */
    }
    applySection(name, hidden);
  }

  function restoreSections() {
    for (const name of Object.keys(SECTIONS)) applySection(name, sectionHidden(name));
  }

  // ------------------------------------------------------------------
  // Панель задач
  // ------------------------------------------------------------------
  // В работе — всё, что не закрыто: отменённые и скачанные уходят в архив.
  const FINISHED_STATES = new Set(["cancelled", "done", "not_found"]);

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
    if (task.id === state.taskId) card.classList.add("is-current");
    card.title = "Открыть чат этой задачи";
    card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      openConversation(task.id);
      closePanel();
    });

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

    const progress = task.params?.progress;
    if (progress && task.state === "downloading") {
      const bar = document.createElement("div");
      bar.className = "progress";
      const fill = document.createElement("div");
      fill.className = "progress__bar";
      fill.style.width = `${Math.max(1, Math.min(100, progress.percent || 0))}%`;
      bar.appendChild(fill);
      const label = document.createElement("div");
      label.className = "progress__label";
      label.textContent = progressText(progress);
      card.appendChild(bar);
      card.appendChild(label);
    }

    if (task.last_result) {
      const line = document.createElement("div");
      line.className = "task-card__meta";
      line.textContent = task.last_result.slice(0, 120);
      card.appendChild(line);
    }
    // Что именно отслеживаем: «1080p, сезон 3, жду серию 2» — иначе непонятно,
    // за тем ли следит задача.
    if (task.watch) {
      const line = document.createElement("div");
      line.className = "task-card__meta task-card__watch";
      line.textContent = task.watch;
      card.appendChild(line);
    }
    if (task.last_error && task.state !== "cancelled") {
      const line = document.createElement("div");
      line.className = "task-card__meta";
      line.style.color = "var(--red)";
      line.textContent = task.last_error.slice(0, 160);
      card.appendChild(line);
    }

    // Когда проверяли и что нашли: «12:40 — 0 новых · следующая 13:40».
    // Без этого непонятно, живой мониторинг или он молча встал.
    if (task.last_check || task.next_check_at) {
      const line = document.createElement("div");
      line.className = "task-card__meta task-card__checks";
      const bits = [];
      if (task.last_check) {
        const when = formatWhen(task.last_check.at);
        bits.push(`${when} — ${task.last_check.new === 0 ? "нового нет" : `новых: ${task.last_check.new}`}`);
      }
      if (task.next_check_at) {
        const next = formatWhen(task.next_check_at);
        if (next) bits.push(`следующая ${next}`);
      }
      line.textContent = "проверка: " + bits.join(" · ");
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
    if (task.kind === "periodic" && task.checks_done) {
      // Журнал проверок: когда проверяли и что нашли — без выхода из картотеки.
      actions.appendChild(
        smallButton("Журнал", "btn", () => showChecks(task.id, task.params?.title || task.user_query))
      );
    }
    if (!FINISHED_STATES.has(task.state)) {
      actions.appendChild(
        smallButton("Отменить", "btn btn--danger", async (event) => {
          event.target.disabled = true;
          try {
            await api(`/api/tasks/${task.id}/cancel`, { method: "POST" });
            refreshStatus();
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

  function progressText(progress) {
    if (!progress) return "";
    const bits = [`${progress.percent ?? 0}%`];
    if (progress.rate_human) bits.push(progress.rate_human);
    if (progress.eta_human) bits.push(`осталось ${progress.eta_human}`);
    if (progress.downloaded_human && progress.size_human) {
      bits.push(`${progress.downloaded_human} из ${progress.size_human}`);
    }
    return bits.join(" · ");
  }

  function progressBar(progress) {
    const wrap = document.createElement("div");
    wrap.className = "progress";
    const fill = document.createElement("div");
    fill.className = "progress__bar";
    fill.style.width = `${Math.max(1, Math.min(100, progress.percent || 0))}%`;
    wrap.appendChild(fill);
    return wrap;
  }

  function placeholder(text) {
    const box = document.createElement("div");
    box.className = "task-card__meta";
    box.style.padding = "2px 0";
    box.textContent = text;
    return box;
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
      state.tasks = tasks;
      if (state.taskId) {
        const current = tasks.find((task) => task.id === state.taskId);
        if (current) showCurrentTask(current.params?.title || current.user_query || "");
      }
      const active = tasks.filter((task) => !FINISHED_STATES.has(task.state));
      const past = tasks.filter((task) => FINISHED_STATES.has(task.state));

      const activeBox = $("active-tasks");
      activeBox.innerHTML = "";
      $("active-count").textContent = String(active.length);
      $("finished-count").textContent = String(past.length);
      $("download-count").textContent = String(downloads.length);
      // Заглушка нужна, чтобы список не схлопывался: иначе у «показать/скрыть»
      // нет видимого эффекта, и непонятно, пусто тут или спрятано.
      if (!active.length) {
        activeBox.appendChild(placeholder("активных задач нет"));
      }
      active.forEach((task) => activeBox.appendChild(taskCard(task)));

      const pastBox = $("past-tasks");
      pastBox.innerHTML = "";
      if (!past.length) {
        pastBox.appendChild(placeholder("завершённых задач пока нет"));
      }
      past.slice(0, 30).forEach((task) => pastBox.appendChild(taskCard(task)));

      const downloadBox = $("download-list");
      downloadBox.innerHTML = "";
      if (!downloads.length) {
        downloadBox.appendChild(placeholder("ещё ничего не скачано"));
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
        const origin = item.source === "transmission" ? "добавлено в Transmission" : "";
        if (origin) {
          const note = document.createElement("div");
          note.className = "task-card__meta";
          note.style.opacity = ".7";
          note.textContent = origin;
          meta.textContent = [meta.textContent, origin].filter(Boolean).join(" · ");
        }
        if (meta.textContent) row.appendChild(meta);
        if (item.progress) {
          row.appendChild(progressBar(item.progress));
          const label = document.createElement("div");
          label.className = "progress__label";
          label.textContent = item.progress.done
            ? `скачано · ${item.progress.size_human || ""}`
            : progressText(item.progress);
          row.appendChild(label);
        }

        const actions = document.createElement("div");
        actions.className = "task-card__actions";
        // «Подробнее» по уже скачанному: жанр, актёры, описание — чтобы решить,
        // что смотреть и что удалять. Берём со страницы раздачи, а не из карточки
        // загрузки: там только название.
        if (item.torrent_id) {
          actions.appendChild(
            smallButton("Подробнее", "btn", (event) => {
              showTorrentDetails(item.torrent_id, event.target);
            })
          );
        }
        actions.appendChild(
          smallButton("Удалить", "btn btn--danger", (event) => {
            deleteDownload(item, event.target);
          })
        );
        row.appendChild(actions);
        downloadBox.appendChild(row);
      });
    } catch (error) {
      console.warn("не удалось обновить задачи", error);
    }
  }

  // ------------------------------------------------------------------
  // Статус
  // ------------------------------------------------------------------
  let statusBusy = false;
  async function refreshStatus() {
    if (statusBusy) return;   // не плодим параллельные запросы
    statusBusy = true;
    const box = $("status-grid");
    if (!box.children.length) box.textContent = "проверяю…";
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
      if (status.torrents) {
        const files = status.torrents;
        rows.push([
          "torrent-файлы",
          files.ok
            ? `${mark(true)} ${files.path}`
            : `<span class="status-bad">${(files.error || "нет доступа").slice(0, 80)}</span>`,
        ]);
      }
      if (status.storage) {
        const disk = status.storage;
        rows.push([
          "диск",
          disk.ok
            ? `<b>${disk.volume || "?"}</b> · свободно ${disk.free_human} из ${disk.total_human}`
            : `<span class="status-bad">${(disk.error || "нет доступа").slice(0, 80)}</span>`,
        ]);
        if (disk.ok) {
          // «Диск пуст, а место занято» — самая частая причина вопросов.
          // Показываем занятое и отдельно наши файлы, чтобы разница была видна.
          rows.push([
            "занято",
            `${disk.used_human} · наши файлы ${disk.own_human}` +
              (disk.own_complete ? "" : " (≈)"),
          ]);
          if ((disk.hidden_bytes || 0) > 1024 ** 3) {
            rows.push([
              "не наши файлы",
              `<span class="status-warn" title="Занято ${disk.used_human}, из них наши загрузки и torrent-файлы — ${disk.own_human}. Остальное — другие файлы на диске или его корзина (.Trashes): место вернётся после «Очистить корзину» в Finder.">${disk.hidden_human}</span> — чужие данные или корзина диска`,
            ]);
          }
        }
      }
      rows.push(["в работе", String(status.tasks.active)]);
      box.innerHTML = rows
        .map(([key, value]) => `<b>${key}</b><span>${value}</span>`)
        .join("");
    } catch (error) {
      box.innerHTML = `<b>статус</b><span class="status-bad">${error.message}</span>`;
    } finally {
      statusBusy = false;
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
      } else if (data.type === "download_complete") {
        refreshTasks();
        refreshStatus();
      } else if (data.type === "download_progress") {
        refreshTasks();
      } else if (
        data.type === "download_done" ||
        data.type === "download_deleted" ||
        data.type === "task_cancelled"
      ) {
        refreshTasks();
        refreshStatus();
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
  // Окно «Подробности раздачи» / «Журнал проверок»
  // ------------------------------------------------------------------
  // Одно окно на два случая чтения. Кнопки действий тут не живут намеренно:
  // скачивание и отмена остаются в чате, чтобы не было двух пультов.
  function openInfo(title, content) {
    $("info-title").textContent = title;
    fillInfo(content);
    $("info-modal").hidden = false;
  }

  function closeInfo() {
    $("info-modal").hidden = true;
  }

  // ------------------------------------------------------------------
  // Подтверждение опасного действия — окном, а не сообщением в чате.
  // Кнопку «Удалить» нажимают в картотеке, а сообщение появлялось в чате:
  // на телефоне чат под панелью, и второй шаг («Удалить навсегда») легко
  // не заметить — со стороны это выглядит как «удаление не работает».
  // ------------------------------------------------------------------
  function confirmDialog({ title, text, confirmLabel, danger = true }) {
    return new Promise((resolve) => {
      $("confirm-title").textContent = title;
      $("confirm-body").textContent = text;
      const yes = $("confirm-yes");
      const no = $("confirm-no");
      yes.textContent = confirmLabel;
      yes.classList.toggle("btn--danger", Boolean(danger));
      yes.classList.toggle("btn--primary", !danger);
      const finish = (value) => {
        $("confirm-modal").hidden = true;
        yes.removeEventListener("click", onYes);
        no.removeEventListener("click", onNo);
        document.removeEventListener("keydown", onKey);
        resolve(value);
      };
      const onYes = () => finish(true);
      const onNo = () => finish(false);
      const onKey = (event) => {
        if (event.key === "Escape") finish(false);
      };
      yes.addEventListener("click", onYes);
      no.addEventListener("click", onNo);
      document.addEventListener("keydown", onKey);
      $("confirm-modal").hidden = false;
      yes.focus();
    });
  }

  // Удаление раздачи: сначала спрашиваем сервер (он проверяет, что удалять),
  // потом подтверждаем окном и удаляем. Текст берём серверный — там точный
  // вес, папка и оговорка про память агента.
  async function deleteDownload(item, button) {
    if (state.busy) return;
    state.busy = true;
    const label = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "проверяю…";
    }
    try {
      const prepared = await api("/api/action", {
        method: "POST",
        body: {
          kind: "delete_download",
          payload: { torrent_id: item.torrent_id || "", hash: item.hash || "" },
        },
      });
      const message = (prepared.messages || [])[0] || {};
      const details = (message.payload || {}).download || {};
      if (!Object.keys(details).length) {
        // Сервер объяснит причину (запись уже удалена / не нашёл раздачу).
        renderTurn(prepared);
        return;
      }
      const confirmed = await confirmDialog({
        title: "Удалить раздачу?",
        text: message.text || "Удалить раздачу вместе с файлами?",
        confirmLabel: "Удалить навсегда",
      });
      if (!confirmed) return;
      const action = (message.actions || []).find((item) => item.kind === "confirm_delete");
      const result = await api("/api/action", {
        method: "POST",
        body: { kind: "confirm_delete", payload: action ? action.payload : {} },
      });
      renderTurn(result);
    } catch (error) {
      renderMessage({
        role: "agent",
        text: `Не смог удалить: ${error.message}`,
        kind: "error",
      });
    } finally {
      state.busy = false;
      if (button) {
        button.disabled = false;
        button.textContent = label;
      }
    }
  }

  function infoRow(label, value) {
    if (!value) return null;
    const row = document.createElement("div");
    row.className = "info__row";
    const name = document.createElement("span");
    name.className = "info__label";
    name.textContent = label;
    const text = document.createElement("span");
    text.className = "info__value";
    text.textContent = value;
    row.append(name, text);
    return row;
  }

  function infoLink(url, label) {
    const link = document.createElement("a");
    link.className = "btn btn--tiny btn--link";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = label;
    return link;
  }

  function detailsBox(data, torrentId) {
    const box = document.createElement("div");
    const head = document.createElement("p");
    head.className = "info__title";
    head.textContent = data.title || `раздача #${torrentId}`;
    box.appendChild(head);
    if (data.original_title && data.original_title !== data.title) {
      box.appendChild(infoRow("Оригинал", data.original_title));
    }
    const rows = [
      ["Год", data.year],
      ["Жанр", data.genre],
      ["Режиссёр", data.director],
      ["В ролях", data.cast],
      ["Качество", data.quality],
      ["Видео", data.video],
      ["Аудио", data.audio],
      ["Вес", data.size],
      ["Сиды", `${data.seeds ?? 0} · пиры ${data.peers ?? 0}`],
      ["Залит", data.upload_date],
      ["Обновлён", data.update_date],
    ];
    for (const [name, value] of rows) {
      const row = infoRow(name, value);
      if (row) box.appendChild(row);
    }
    if (data.description) {
      const text = document.createElement("p");
      text.className = "info__text";
      text.textContent = data.description;
      box.appendChild(text);
    }
    if (data.url) {
      const wrap = document.createElement("div");
      wrap.className = "info__actions";
      wrap.appendChild(infoLink(data.url, "Открыть на трекере · комментарии"));
      box.appendChild(wrap);
    }
    return box;
  }

  function fillInfo(content) {
    const body = $("info-body");
    body.innerHTML = "";
    if (typeof content === "string") {
      const text = document.createElement("p");
      text.className = "info__text";
      text.textContent = content;
      body.appendChild(text);
    } else if (content) {
      body.appendChild(content);
    }
  }

  // Окно открывается СРАЗУ, до запроса к трекеру: страница раздачи идёт через
  // шлюз Cloudflare и на холодном старте может отвечать секундами. Молчаливая
  // кнопка «открываю…» без окна выглядит как «ничего не произошло».
  async function showTorrentDetails(torrentId, button) {
    if (!torrentId) return;
    const label = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "открываю…";
    }
    openInfo("Подробности раздачи", "Читаю страницу раздачи на трекере…");
    try {
      const data = await api(`/api/torrent/${encodeURIComponent(torrentId)}/details`);
      fillInfo(detailsBox(data, torrentId));
    } catch (error) {
      fillInfo(`Не получилось открыть раздачу: ${error.message}`);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = label;
      }
    }
  }

  async function showChecks(taskId, title) {
    try {
      const data = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      const box = document.createElement("div");
      const head = document.createElement("p");
      head.className = "info__title";
      head.textContent = data.task?.params?.title || title || "задача";
      box.appendChild(head);
      const checks = data.checks || [];
      if (!checks.length) {
        const empty = document.createElement("p");
        empty.className = "info__text";
        empty.textContent = "проверок ещё не было";
        box.appendChild(empty);
      }
      for (const check of checks) {
        const row = document.createElement("div");
        row.className = "info__row";
        const name = document.createElement("span");
        name.className = "info__label";
        name.textContent = formatStamp(check.created_at);
        const value = document.createElement("span");
        value.className = "info__value";
        const bits = [
          `подходящих: ${check.found ?? 0}`,
          `новых: ${check.new_count ?? 0}`,
        ];
        if (check.message) bits.push(check.message);
        value.textContent = bits.join(" · ");
        row.append(name, value);
        box.appendChild(row);
      }
      openInfo("Журнал проверок", box);
    } catch (error) {
      openInfo("Журнал проверок", `Не получилось прочитать журнал: ${error.message}`);
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

    $("panel-open").addEventListener("click", panelOpen);
    $("panel-close").addEventListener("click", closePanel);
    $("panel-scrim").addEventListener("click", closePanel);

    for (const button of document.querySelectorAll("[data-toggle]")) {
      button.addEventListener("click", () => toggleSection(button.dataset.toggle));
    }
    restoreSections();

    $("new-task").addEventListener("click", startNewTask);
    $("panel-new-task").addEventListener("click", () => {
      startNewTask();
      closePanel();
    });
    setupPullToRefresh();
    setupHeaderReload();

    $("bell").addEventListener("click", () => {
      if ($("notification-tray").hidden) trayOpen();
      else closeTray();
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
    $("info-close").addEventListener("click", closeInfo);
    $("logout").addEventListener("click", async () => {
      await api("/api/logout", { method: "POST" });
      window.location.reload();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeSettings();
        closeDebug();
        closeInfo();
        closeOverlays();
      }
    });
  }

  // ------------------------------------------------------------------
  // Боковая панель, киножурнал и кнопка «назад» на телефоне.
  // При открытии панели кладём запись в историю: «назад» закрывает панель,
  // а не выкидывает со страницы.
  // ------------------------------------------------------------------
  let overlayPushed = false;

  function pushOverlay() {
    if (overlayPushed) return;
    try {
      history.pushState({ overlay: true }, "");
      overlayPushed = true;
    } catch (_) {
      overlayPushed = false;
    }
  }

  function popOverlay() {
    if (!overlayPushed) return;
    overlayPushed = false;
    history.back();
  }

  function panelOpen() {
    $("panel").classList.add("is-open");
    $("panel-scrim").classList.add("is-visible");
    pushOverlay();
  }

  function panelCloseQuiet() {
    $("panel").classList.remove("is-open");
    $("panel-scrim").classList.remove("is-visible");
  }

  function closePanel() {
    panelCloseQuiet();
    popOverlay();
  }

  function trayOpen() {
    const tray = $("notification-tray");
    tray.hidden = false;
    state.notifications = 0;
    $("bell-dot").hidden = true;
    pushOverlay();
  }

  function trayCloseQuiet() {
    $("notification-tray").hidden = true;
  }

  function closeTray() {
    trayCloseQuiet();
    popOverlay();
  }

  function closeOverlays() {
    panelCloseQuiet();
    trayCloseQuiet();
    popOverlay();
  }

  window.addEventListener("popstate", () => {
    overlayPushed = false;
    panelCloseQuiet();
    trayCloseQuiet();
  });

  // ------------------------------------------------------------------
  // Свайп вниз = обновить (телефон). Тянем только когда чат прокручен вверх,
  // чтобы не мешать обычной листовке.
  async function refreshEverything() {
    const hint = $("pull-hint");
    if (hint) {
      hint.textContent = "обновляю…";
      hint.classList.add("is-visible", "is-busy");
    }
    try {
      await refreshTasks();
      if (state.taskId) await openConversation(state.taskId);
      await refreshStatus();
    } finally {
      if (hint) {
        hint.classList.remove("is-busy");
        hint.classList.remove("is-visible");
      }
    }
  }

  // Потянуть за шапку («Кинозал» / название чата) → перезагрузка страницы.
  function setupHeaderReload() {
    if (!("ontouchstart" in window)) return;
    const header = document.querySelector(".marquee");
    const hint = $("pull-hint");
    if (!header || !hint) return;
    const threshold = 80;
    let startY = 0;
    let dragging = false;
    let armed = false;

    header.addEventListener(
      "touchstart",
      (event) => {
        if (state.busy || event.touches.length !== 1) return;
        if (event.target.closest("button")) return;
        startY = event.touches[0].clientY;
        dragging = true;
        armed = false;
      },
      { passive: true }
    );

    header.addEventListener(
      "touchmove",
      (event) => {
        if (!dragging) return;
        const delta = event.touches[0].clientY - startY;
        if (delta <= 0) {
          dragging = false;
          hint.classList.remove("is-visible", "is-busy");
          return;
        }
        if (event.cancelable) event.preventDefault();
        armed = delta > threshold;
        hint.textContent = armed ? "отпустите, чтобы перезагрузить" : "потяните, чтобы перезагрузить";
        hint.classList.add("is-visible", "is-busy");
      },
      { passive: false }
    );

    const finish = () => {
      if (!dragging) return;
      dragging = false;
      if (armed) {
        armed = false;
        hint.textContent = "перезагружаю…";
        hint.classList.add("is-visible", "is-busy");
        window.location.reload();
        return;
      }
      hint.classList.remove("is-visible", "is-busy");
    };

    header.addEventListener("touchend", finish, { passive: true });
    header.addEventListener("touchcancel", finish, { passive: true });
  }

  function setupPullToRefresh() {
    if (!("ontouchstart" in window)) return;
    const threshold = 70;
    let startY = 0;
    let pulling = false;
    let armed = false;

    const hint = $("pull-hint");

    chat.addEventListener(
      "touchstart",
      (event) => {
        if (state.busy || event.touches.length !== 1) return;
        if (chat.scrollTop > 0) return;
        startY = event.touches[0].clientY;
        pulling = true;
        armed = false;
      },
      { passive: true }
    );

    chat.addEventListener(
      "touchmove",
      (event) => {
        if (!pulling) return;
        const delta = event.touches[0].clientY - startY;
        if (delta <= 0) {
          pulling = false;
          if (hint) hint.classList.remove("is-visible");
          return;
        }
        if (chat.scrollTop > 0) return;
        if (event.cancelable) event.preventDefault();
        armed = delta > threshold;
        if (hint) {
          hint.textContent = armed ? "отпустите, чтобы обновить" : "потяните, чтобы обновить";
          hint.classList.add("is-visible");
        }
      },
      { passive: false }
    );

    const finish = () => {
      if (!pulling) return;
      pulling = false;
      if (armed) {
        armed = false;
        refreshEverything();
      } else if (hint) {
        hint.classList.remove("is-visible");
      }
    };

    chat.addEventListener("touchend", finish, { passive: true });
    chat.addEventListener("touchcancel", finish, { passive: true });
  }

  async function openLatestConversation() {
    try {
      const data = await api("/api/tasks");
      const tasks = (data.tasks || []).slice().sort((a, b) =>
        String(b.updated_at || "").localeCompare(String(a.updated_at || ""))
      );
      if (tasks.length) {
        await openConversation(tasks[0].id);
        return;
      }
    } catch (error) {
      console.warn("не удалось открыть последнюю задачу", error);
    }
    startNewTask();
  }

  async function init() {
    rememberIntro();
    bind();
    setMode("oneshot");
    await openLatestConversation();
    await refreshStatus();
    connectEvents();
    setInterval(refreshTasks, 5000);
    setInterval(refreshStatus, 30000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
