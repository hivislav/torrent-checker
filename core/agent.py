"""Агент: мозг без интерфейса.

Всё, что умеет система, живёт здесь и вызывается методами:

    await agent.handle_message(session_id, text, mode)   # разговор
    await agent.handle_action(session_id, kind, payload) # нажатие кнопки
    await agent.list_tasks()                             # панель задач
    await agent.cancel_task(task_id)
    await agent.history(session_id)

Ни одна строка ниже не знает про FastAPI, HTML или браузер: ответы приходят
объектами `Turn` с кнопками `Action`. Поэтому тот же агент можно поднять как
Telegram-бота (веб — просто ещё один адаптер), и это требование задачи —
«агент независим от UI» — выполняется буквально.

Как устроен разбор запроса:

  текст пользователя
      │  (LLM: разбор параметров — название, качество, озвучка, сезон, период)
      ▼
  MCP kinozal-mcp (поиск по трекеру через шлюз Cloudflare)
      ▼
  чистые правила (core/matching.py): фильтр, «что нового», выбор вариантов
      ▼
  состояния задачи + кнопки (подтверждение, отмена, выбор качества/озвучки)

LLM привлекается к разбору запроса и к текстам, а выбор раздачи делают правила.
Так «4к» не превращается в что попало, а повторный запрос даёт тот же результат.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from core.debuglog import journal
from core.llm import LLM, LLMError
from core.torrent_meta import file_list, human_bytes
from core.matching import (
    QUALITY_LABELS,
    collect_options,
    describe,
    episodes_by_season,
    filter_candidates,
    find_new,
    is_pack,
    rank,
)
from core.mcp_client import KinozalMCP, MCPError
from core.models import Action, Message, Task, Torrent, Turn, new_id, now_iso
from core.notify import DesktopNotifier, EventBus
from core.push import WebPush
from core.episodes import (
    episode_groups,
    is_episode_pack,
    parse_files,
    pick_episode,
    pick_new_episodes,
)
from core.parsing import normalize, parse_torrent
from core.storage import Store
from core.storage_target import StorageUnavailable as TargetUnavailable
from core.storage_target import ensure_target as ensure_dir
from core.transmission import (
    NotEnoughSpace,
    StorageUnavailable,
    Transmission,
    TransmissionError,
)

log = logging.getLogger("agent")

# ----------------------------------------------------------------------
# Промпты
# ----------------------------------------------------------------------
PARSE_SYSTEM = """Ты — разборщик запросов торрент-агента. По сообщению пользователя
верни ТОЛЬКО JSON-объект, без пояснений и без markdown.

Поля JSON:
{
  "title": строка — название фильма/сериала, как его назвал пользователь, или null,
  "original_title": строка — оригинальное (английское) название, если пользователь его написал, иначе null,
  "quality": строка или null — только если качество названо явно:
             "2160p" (это же «4к», «4K», «UHD»), "1080p" («fullhd», «1080», «FHD»),
             "720p" («720», «HD»), "480p", "Blu-Ray", "WEB-DL", "BDRip", "HDRip", "HDTV", "DVDRip",
  "voice": строка или null — озвучка, как её назвал пользователь: "LostFilm", "Кубик в Кубе",
           "HDrezka", "ДБ", "ПМ", "ЛМ", "СТ", "Дубляж" и т.п.,
  "season": целое число или null — номер сезона,
  "episode_from": целое число или null — с какой серии начинать,
  "year": целое число (год) или null,
  "interval_minutes": целое число или null — период проверки в минутах (например «раз в час» = 60),
  "auto_download": true|false|null — true если просят сразу качать («качай», «скачивай», «сразу качай»),
                   false если просят только сообщать («сообщай», «только сообщай», «спроси разрешение»),
                   null если не сказано,
  "intent": "find" | "monitor" | "question"
}

Дополнительно поле:
  "refine": true|false — true, если сообщение является уточнением к предыдущему
            запросу в этом же диалоге («нет, я про 4 сезон», «а если 4К?»,
            «второй вариант», «не то, нужен другой фильм»), а не новой задачей.

Правила:
- Тебе дана история переписки. Текущее сообщение может быть уточнением:
  тогда заполняй только то, что уточняется, а остальные поля оставляй null —
  агент сам объединит их с прошлым запросом.
- «Не то», «другой», «этот», «второй», «первый» — это уточнение (refine = true).
- НЕ выдумывай параметры, которых в сообщении нет. Сомневаешься — null.
- Год, сезон и серию не путай: «4 сезон» — это season, «5 серия» — это episode_from.
- «Следи», «проверяй», «мониторь», «отслеживай» — это intent = "monitor".
- Вопрос, приветствие, просьба объяснить — intent = "question".
- Если пользователь просит найти и скачать — intent = "find"."""

CHAT_SYSTEM = """Ты — помощник локального торрент-агента «Кинозал». Он ищет раздачи на
kinozal.me, скачивает торрент-файлы и ставит их в Transmission, а также умеет
следить за новыми сериями.

Отвечай по-русски, коротко и по делу (2–5 предложений). Не выдумывай названия
раздач и не обещай того, чего агент не умеет. Если пользователь просит что-то
найти, скачать или отслеживать — подскажи нажать кнопку «Разово» или
«Периодически» под полем ввода и написать запрос."""


PARAM_TITLES = {
    "title": "название",
    "original_title": "оригинальное название",
    "quality": "качество",
    "voice": "озвучка",
    "season": "сезон",
    "episode_from": "серия",
    "year": "год",
}


def _plural(count: int, one: str, few: str, many: str) -> str:
    """«1 файл», «2 файла», «5 файлов» — без кривых скобок в интерфейсе."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} {one}"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} {few}"
    return f"{count} {many}"


class Agent:
    """Ядро системы. Один на процесс, потокобезопасен на уровне корутин."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = Store(self.settings.db_path)
        self.llm = LLM(self.settings)
        self.mcp = KinozalMCP(self.settings)
        self.transmission = Transmission(self.settings)
        self.events = EventBus()
        self.desktop = DesktopNotifier(enabled=True)
        self.push = WebPush(store=self.store, data_dir=self.settings.data_dir)
        self._started = False
        self._check_locks: dict[str, asyncio.Lock] = {}
        self._running_checks: set[str] = set()
        self.scheduler_task: asyncio.Task | None = None
        self.scheduler_tick_seconds = 20

    # ------------------------------------------------------------------
    # жизненный цикл
    # ------------------------------------------------------------------
    async def start(self, *, with_scheduler: bool = True) -> None:
        if self._started:
            return
        self.settings.torrents_dir.mkdir(parents=True, exist_ok=True)
        self._started = True
        try:
            await self.mcp.start()
        except MCPError as exc:
            log.error("MCP недоступен: %s", exc)
        await self._load_overrides()
        await self.push.prepare()
        if with_scheduler:
            self.scheduler_task = asyncio.create_task(self._scheduler_loop())
            log.info("шедулер периодических задач запущен")

    async def stop(self) -> None:
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.scheduler_task = None
        await self.mcp.stop()
        await self.llm.close()
        self.store.close()
        self._started = False

    async def _load_overrides(self) -> None:
        """Пароли, заданные через интерфейс, важнее .env."""
        kinozal_password = await self.store.get_setting("kinozal_password")
        if kinozal_password:
            object.__setattr__(self.settings, "kinozal_password", kinozal_password)
        email = await self.store.get_setting("kinozal_username")
        if email:
            object.__setattr__(self.settings, "kinozal_username", email)
        tr_password = await self.store.get_setting("transmission_password")
        tr_user = await self.store.get_setting("transmission_username")
        if tr_user or tr_password:
            self.transmission.configure(
                self.settings.transmission_host,
                self.settings.transmission_port,
                tr_user or self.settings.transmission_username,
                tr_password or self.settings.transmission_password,
            )

    # ------------------------------------------------------------------
    # Публичный API (используется веб-слоем и будущим ботом)
    # ------------------------------------------------------------------
    async def handle_message(self, session_id: str, text: str, mode: str = "chat") -> Turn:
        text = (text or "").strip()
        turn = Turn()
        if not text:
            turn.add("Пустой запрос. Напишите, что искать — например «Дюна 1080p».")
            return turn

        await self._remember(session_id, Message(role="user", text=text, kind="text"))

        # Контекст: последние сообщения переписки и незакрытая задача. Без этого
        # уточнение «нет, я про 4 сезон» начинало бы новый поиск с нуля.
        window = await self._context_window(session_id, limit=self.settings.context_messages + 1)
        if window and window[-1].get("text") == text:
            window = window[:-1]  # само текущее сообщение в истории не нужно
        window = window[-self.settings.context_messages :]
        active = await self._active_task(session_id)

        try:
            parsed = await self._parse_query(text, window, active)
        except LLMError as exc:
            parsed = {}
            log.warning("разбор запроса не удался: %s", exc)

        intent = str(parsed.get("intent") or "")
        effective_mode = mode if mode in ("oneshot", "periodic") else (
            "periodic" if intent == "monitor" else "oneshot"
        )
        # Уточнение ищем только среди задач того же типа, что запрошен кнопкой.
        active = await self._active_task(session_id, kind=effective_mode)

        # Уточнение к текущей задаче — правим её, а не заводим новую.
        if active is not None and self._is_refinement(text, parsed, active):
            journal.add(
                "agent",
                "уточнение к текущей задаче",
                {"текст": text, "было": active.params, "разобрано": parsed},
            )
            await self._refine_task(active, parsed, text, turn)
            turn.task = await self.store.get_task(active.id)
            await self._remember_many(session_id, turn.messages)
            return turn
        effective_mode = mode
        if mode not in ("oneshot", "periodic"):
            effective_mode = "periodic" if intent == "monitor" else "oneshot"
            if intent == "question" and not parsed.get("title"):
                reply = await self._chat_answer(text, window)
                turn.add(reply)
                await self._remember_many(session_id, turn.messages)
                return turn

        journal.add(
            "agent",
            f"запрос пользователя ({'периодически' if effective_mode == 'periodic' else 'разово'})",
            {"текст": text, "разобрано": parsed},
        )
        if effective_mode == "periodic":
            await self._start_periodic(session_id, text, parsed, turn)
        else:
            await self._start_oneshot(session_id, text, parsed, turn)

        await self._remember_many(session_id, turn.messages)
        return turn

    # ------------------------------------------------------------------
    # Контекст переписки и уточнения
    # ------------------------------------------------------------------
    RESUME_STATES = ("awaiting_choice", "awaiting_confirm", "not_found", "awaiting_setup")

    async def _active_task(self, session_id: str, kind: str | None = None) -> Task | None:
        """Незакрытая задача этой сессии — к ней относятся уточнения.

        `kind` фильтрует по типу: уточнение к разовой задаче не должно
        переиспользовать периодическую (и наоборот) — это разные кнопки и
        разные состояния.
        """
        tasks = await self.store.list_tasks(limit=20)
        for task in tasks:
            if task.session_id != session_id or task.state not in self.RESUME_STATES:
                continue
            if kind and task.kind != kind:
                continue
            return task
        return None

    def _is_refinement(self, text: str, parsed: dict[str, Any], active: Task) -> bool:
        """Это уточнение к активной задаче, а не новая задача?"""
        new_title = str(parsed.get("title") or "").strip()
        old_title = str((active.params or {}).get("title") or "").strip()
        # Другое явное название — всегда новая задача. Флаг модели здесь не
        # главный: она склонна считать уточнением любой следующий запрос.
        if new_title and old_title and normalize(new_title) != normalize(old_title):
            return False
        if parsed.get("refine") is True:
            return True
        if not new_title:
            # «4К», «7 серия», «нет, другой» — без названия это уточнение.
            return True
        return normalize(new_title) == normalize(old_title)

    async def _refine_task(
        self, task: Task, parsed: dict[str, Any], text: str, turn: Turn
    ) -> None:
        """Применяет уточнение к задаче и продолжает с того же места."""
        before = dict(task.params or {})
        merged = dict(before)
        changed: dict[str, Any] = {}
        for key in ("title", "original_title", "quality", "voice", "season", "episode_from", "year"):
            value = parsed.get(key)
            if value in (None, "", "null"):
                continue
            if merged.get(key) != value:
                merged[key] = value
                changed[key] = value

        if not changed and parsed.get("title"):
            # Пользователь повторил тот же запрос — просто продолжаем с того же
            # места (переспрашивать «что поправить» тут неуместно).
            await self._selection_step(task, turn)
            return

        if not changed:
            # Уточнили, но ничего нового не поняли — показываем, что можно менять.
            pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
            options = collect_options(pool) if pool else {"qualities": [], "voices": [], "total": 0}
            text_out = (
                "Уточните, пожалуйста, что поправить: название, качество, озвучку "
                "или серию. Можно написать словами («нужен 4K», «только LostFilm»)."
            )
            if options.get("qualities") or options.get("voices"):
                text_out += "\n\nДоступно по этой раздаче:"
                if options.get("qualities"):
                    text_out += "\n• качество: " + ", ".join(
                        self._quality_label(item) for item in options["qualities"][:6]
                    )
                if options.get("voices"):
                    text_out += "\n• озвучка: " + ", ".join(options["voices"][:6])
            message = turn.add(text_out, kind="tickets", task_id=task.id,
                               payload={"task": task.to_dict()})
            message.actions = self._option_actions(task, pool) if pool else []
            return

        title_changed = "title" in changed or "original_title" in changed
        task.params = merged
        task.user_query = f"{task.user_query} | {text}"[:400]
        task.state = "searching"
        await self.store.save_task(task)
        journal.add(
            "agent",
            "уточнение применено",
            {"было": before, "стало": merged, "изменено": changed},
        )

        pool = [] if title_changed else [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        if pool:
            # Название не менялось — ищем заново не надо, перебираем то, что уже есть.
            summary = ", ".join(
                f"{PARAM_TITLES.get(key, key)}: {value}" for key, value in changed.items()
            )
            turn.add(f"Уточнил: {summary}. Смотрю по уже найденным раздачам.", kind="text", task_id=task.id)
            task.options = {**(task.options or {}), "pool": [t.to_dict() for t in pool]}
            await self._selection_step(task, turn)
            return

        # Название изменилось — нужен новый поиск.
        try:
            torrents = await self._search(task.params)
        except MCPError as exc:
            task.state = "failed"
            task.last_error = str(exc)
            await self.store.save_task(task)
            turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
            return
        task.options = {"pool": [t.to_dict() for t in torrents[:80]], "queries": [task.params.get("title")]}
        await self.store.save_task(task)
        summary = ", ".join(
            f"{PARAM_TITLES.get(key, key)}: {value}" for key, value in changed.items()
        )
        turn.add(f"Уточнил: {summary}. Ищу заново.", kind="text", task_id=task.id)
        await self._selection_step(task, turn)

    async def handle_action(self, session_id: str, kind: str, payload: dict[str, Any]) -> Turn:
        turn = Turn()
        task_id = str(payload.get("task_id") or "")
        task = await self.store.get_task(task_id) if task_id else None
        turn.task = task

        if kind == "cancel" and task is None:
            # Отмена подтверждения (например, удаления) — задачи тут может не быть.
            turn.add("Отменено, ничего не трогаю.")
        elif kind == "cancel" and task:
            await self.cancel_task(task.id)
            turn.add(
                f"Задача «{task.title}» отменена.",
                kind="task",
                task_id=task.id,
                payload={"task": task.to_dict()},
            )
        elif kind == "choose" and task:
            dimension = str(payload.get("dimension") or "")
            value = str(payload.get("value") or "")
            if dimension and value:
                task.params[dimension] = value
                task.state = "searching"
                await self.store.save_task(task)
            await self._selection_step(task, turn)
        elif kind == "confirm_download" and task:
            torrent = Torrent.from_dict(task.candidate or {})
            await self._download_with_report(task, torrent, turn)
        elif kind == "pick_torrent" and task:
            torrent_id = str(payload.get("torrent_id") or "")
            pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
            torrent = next((t for t in pool if t.id == torrent_id), None)
            if torrent is None:
                turn.add("Не нашёл эту раздачу — начните заново.", kind="error")
            else:
                task.candidate = torrent.to_dict()
                await self._download_with_report(task, torrent, turn)
        elif kind == "make_periodic":
            source = task
            if source is None:
                turn.add("Задача не найдена.", kind="error")
            else:
                interval = int(payload.get("interval_minutes") or self.settings.default_interval_minutes)
                periodic = await self._task_from_params(
                    session_id,
                    source.user_query,
                    dict(source.params),
                    kind="periodic",
                    interval=interval,
                    auto_download=bool(payload.get("auto_download", False)),
                )
                await self._monitor_after_creation(periodic, turn, downloaded_now=False)
        elif kind in ("periodic_auto", "periodic_notify") and task:
            task.auto_download = kind == "periodic_auto"
            task.state = "monitoring"
            task.interval_minutes = task.interval_minutes or self.settings.default_interval_minutes
            self._schedule_next(task)
            await self.store.save_task(task)
            turn.add(
                self._monitoring_text(task),
                kind="task",
                task_id=task.id,
                payload={"task": task.to_dict()},
            )
            turn.messages[-1].actions = self._task_actions(task)
            if task.auto_download and task.candidate:
                torrent = Torrent.from_dict(task.candidate)
                await self._auto_download(task, torrent, turn)
        elif kind == "download_new" and task:
            torrent_id = str(payload.get("torrent_id") or "")
            pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
            torrent = next((t for t in pool if t.id == torrent_id), None)
            if torrent is None:
                turn.add("Эта раздача больше не в списке — дождитесь следующей проверки.", kind="error")
            else:
                # Нажатие «Скачать» в уведомлении — это и есть подтверждение:
                # ставим в очередь сразу, выбирая только новые серии.
                await self._auto_download(task, torrent, turn)
        elif kind == "start_download" and task:
            await self._start_download(task, turn,
                                       periodic_context=task.kind == "periodic")
        elif kind == "choose_episode" and task:
            episode = payload.get("episode")
            season = payload.get("season")
            task.params["episode_from"] = int(episode) if episode is not None else None
            if season is not None:
                task.params["season"] = int(season)
            entries = self._entries_from_task(task)
            plan = self._plan_files(task, entries)
            if plan.get("kind") != "choose":
                task.options = {**(task.options or {}), "plan": plan}
                task.state = "awaiting_confirm"
                await self.store.save_task(task)
                message = turn.add(
                    self._confirm_card_text(task, Torrent.from_dict(task.candidate or {}), plan),
                    kind="task",
                    task_id=task.id,
                    payload={"task": task.to_dict(), "plan": plan},
                )
                message.actions = self._start_actions(task, plan)
            else:
                await self.store.save_task(task)
                message = turn.add(
                    self._episode_choice_text(task, plan),
                    kind="tickets",
                    task_id=task.id,
                    payload={"task": task.to_dict(), "episodes": plan["episodes"]},
                )
                message.actions = self._episode_actions(task, plan)
        elif kind == "download_all" and task:
            entries = self._entries_from_task(task)
            plan = self._plan_files(task, entries)
            plan = {
                **plan,
                "kind": "all",
                "wanted": None,
                "size": plan.get("total_size"),
                "episode": None,
                "names": [],
                "selected_episodes": sorted(
                    {
                        int(group.get("episode"))
                        for group in (plan.get("episodes") or [])
                        if isinstance(group, dict) and group.get("episode") is not None
                    }
                ),
            }
            task.options = {**(task.options or {}), "plan": plan}
            await self.store.save_task(task)
            message = turn.add(
                self._confirm_card_text(task, Torrent.from_dict(task.candidate or {}), plan),
                kind="task",
                task_id=task.id,
                payload={"task": task.to_dict(), "plan": plan},
            )
            message.actions = self._start_actions(task, plan)
        elif kind == "delete_download":
            torrent_id = str(payload.get("torrent_id") or "")
            turn = await self.request_delete(torrent_id)
        elif kind == "confirm_delete":
            torrent_id = str(payload.get("torrent_id") or "")
            turn = await self.perform_delete(torrent_id)
        elif kind == "retry_transmission" and task:
            await self._retry_transmission(task, turn)
        elif kind == "check_now" and task:
            await self.check_task(task, manual=True, turn=turn)
        elif kind == "forget_downloads":
            await self.store.set_setting("__noop", "")
            turn.add("Готово.")
        else:
            turn.add("Не понял действие. Попробуйте ещё раз.", kind="error")

        await self._remember_many(session_id, turn.messages)
        return turn

    # ------------------------------------------------------------------
    # Задачи: чтение и отмена
    # ------------------------------------------------------------------
    async def list_tasks(self, include_finished: bool = True, limit: int = 40) -> list[dict[str, Any]]:
        tasks = await self.store.list_tasks(limit=limit)
        if not include_finished:
            tasks = [task for task in tasks if task.is_active]
        return [task.to_dict() for task in tasks]

    async def get_task(self, task_id: str) -> Task | None:
        return await self.store.get_task(task_id)

    async def cancel_task(self, task_id: str) -> Task | None:
        task = await self.store.get_task(task_id)
        if task is None:
            return None
        task.state = "cancelled"
        task.next_check_at = None
        await self.store.save_task(task)
        await self.events.publish(
            {"type": "task_cancelled", "task_id": task.id, "title": task.title}
        )
        return task

    async def history(self, session_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return await self.store.history(session_id, limit)

    async def downloads(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self.store.list_downloads(limit)

    async def status(self) -> dict[str, Any]:
        import httpx

        gate: dict[str, Any] = {"ok": False, "error": "неизвестно"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(
                    f"http://{self.settings.gate_host}:{self.settings.gate_port}/status"
                )
                gate = {"ok": response.status_code == 200, **response.json()}
        except Exception as exc:  # noqa: BLE001
            gate = {"ok": False, "error": str(exc)[:200]}

        transmission = await self.transmission.test_connection()
        from core.storage_target import dir_size, target_info
        from core.torrent_meta import human_bytes as human_size

        storage_info = self.transmission.storage_info()
        # Сколько из занятого — реально наши файлы. Разница — чужие данные или
        # корзина диска, и это единственный способ объяснить «диск пуст, но
        # место занято».
        own_downloads, done_downloads = dir_size(self.settings.transmission_download_dir)
        own_torrents, done_torrents = dir_size(self.settings.torrents_dir)
        storage_info.own_bytes = own_downloads + own_torrents
        storage_info.own_complete = done_downloads and done_torrents
        storage = storage_info.to_dict()
        storage["downloads_size_human"] = human_size(own_downloads)
        storage["torrents_size_human"] = human_size(own_torrents)
        torrents = target_info(self.settings.torrents_dir).to_dict()
        return {
            "model": {
                "name": self.settings.llm_model,
                "configured": bool(self.settings.llm_api_key),
                "last_error": self.llm.last_error,
            },
            "mcp": {"ready": self.mcp.ready},
            "gate": gate,
            "transmission": transmission,
            "storage": storage,
            "torrents": torrents,
            "tasks": {
                "active": len(await self.store.list_tasks(limit=500, states=["monitoring"])),
            },
            "now": now_iso(),
        }

    # ------------------------------------------------------------------
    # Разбор запроса и поиск
    # ------------------------------------------------------------------
    async def _context_window(self, session_id: str, limit: int | None = None) -> list[dict[str, str]]:
        """Последние сообщения переписки — скользящее окно для модели.

        Нужно, чтобы уточнения вида «нет, я про 4 сезон» относились к той же
        задаче, а не начинали разговор с чистого листа. Окно ограничено
        настройкой CONTEXT_MESSAGES (по умолчанию 20 сообщений) и обрезает
        длинные ответы агента, чтобы не жечь токены на простыни карточек.
        """
        limit = limit or self.settings.context_messages
        rows = await self.store.history(session_id, limit=limit)
        window: list[dict[str, str]] = []
        for row in rows:
            role = "assistant" if row.get("role") == "agent" else row.get("role")
            if role not in ("user", "assistant"):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            if len(text) > 400:
                text = text[:400] + "…"
            window.append({"role": role, "text": text})
        return window

    def _context_prompt(self, window: list[dict[str, str]], active: Task | None) -> str:
        """Собирает историю и состояние задачи в один текстовый блок для модели."""
        parts: list[str] = []
        if active is not None:
            params = ", ".join(
                f"{key}={value}"
                for key, value in (active.params or {}).items()
                if key in ("title", "quality", "voice", "season", "episode_from", "year")
                and value not in (None, "", [])
            )
            parts.append(
                f"Текущая задача: «{active.title}», состояние {active.state}, "
                f"параметры: {params or 'не заданы'}."
            )
        if window:
            parts.append("История переписки (последние сообщения):")
            for item in window:
                who = "пользователь" if item["role"] == "user" else "агент"
                parts.append(f"{who}: {item['text']}")
        parts.append("Текущее сообщение пользователя — ниже.")
        return "\n".join(parts)

    async def _parse_query(
        self, text: str, window: list[dict[str, str]] | None = None, active: Task | None = None
    ) -> dict[str, Any]:
        context = self._context_prompt(window or [], active)
        data = await self.llm.ask_json(PARSE_SYSTEM, f"{context}\n\nСообщение: {text}")
        if not isinstance(data, dict):
            data = {}
        cleaned: dict[str, Any] = {}
        for key in ("title", "original_title", "quality", "voice"):
            value = data.get(key)
            cleaned[key] = str(value).strip() if value not in (None, "", "null") else None
        for key in ("season", "episode_from", "year", "interval_minutes"):
            value = data.get(key)
            try:
                cleaned[key] = int(value) if value not in (None, "", "null") else None
            except (TypeError, ValueError):
                cleaned[key] = None
        auto = data.get("auto_download")
        cleaned["auto_download"] = auto if isinstance(auto, bool) else None
        cleaned["intent"] = data.get("intent") if isinstance(data.get("intent"), str) else None
        refine = data.get("refine")
        cleaned["refine"] = refine if isinstance(refine, bool) else None
        return cleaned

    async def _chat_answer(self, text: str, window: list[dict[str, str]] | None = None) -> str:
        prompt = text
        if window:
            history = "\n".join(
                ("пользователь" if item["role"] == "user" else "агент") + ": " + item["text"]
                for item in window[-10:]
            )
            prompt = f"Предыдущие сообщения:\n{history}\n\nТекущий вопрос: {text}"
        try:
            return await self.llm.ask_text(CHAT_SYSTEM, prompt, temperature=0.4, max_tokens=600)
        except LLMError as exc:
            return f"Модель сейчас недоступна: {exc}"

    async def _search(self, params: dict[str, Any]) -> list[Torrent]:
        """Поиск по трекеру: несколько запросов, потому что названия бывают разные."""
        queries: list[str] = []
        title = str(params.get("title") or "").strip()
        original = str(params.get("original_title") or "").strip()
        year = params.get("year")
        if title:
            queries.append(title)
            if year:
                queries.append(f"{title} {year}")
        if original and original.lower() != title.lower():
            queries.append(original)
        if not queries:
            return []

        seen: dict[str, Torrent] = {}
        errors: list[str] = []
        for query in dict.fromkeys(queries):
            try:
                raw_items = await self.mcp.search_pages(query, pages=2)
            except MCPError as exc:
                errors.append(f"{query}: {exc}")
                continue
            for raw in raw_items:
                torrent = parse_torrent(raw)
                if torrent.id and torrent.id not in seen:
                    seen[torrent.id] = torrent
        if not seen and errors:
            raise MCPError("; ".join(errors[:2]))
        return list(seen.values())

    # ------------------------------------------------------------------
    # Разовая задача
    # ------------------------------------------------------------------
    async def _start_oneshot(
        self, session_id: str, text: str, parsed: dict[str, Any], turn: Turn
    ) -> None:
        if not parsed.get("title"):
            turn.add(
                "Не понял, что искать. Напишите название — например "
                "«Тед Лассо 1080p LostFilm».",
            )
            return

        task = await self._task_from_params(session_id, text, parsed, kind="oneshot")
        turn.task = task
        turn.add(
            f"Ищу «{task.title}»"
            + (f" ({task.params.get('quality')})" if task.params.get("quality") else "")
            + (f", озвучка {task.params.get('voice')}" if task.params.get("voice") else "")
            + "…",
            kind="text",
            task_id=task.id,
        )

        try:
            torrents = await self._search(task.params)
        except MCPError as exc:
            task.state = "failed"
            task.last_error = str(exc)
            await self.store.save_task(task)
            turn.add(
                f"Не смог обратиться к трекеру: {exc}",
                kind="error",
                task_id=task.id,
                payload={"task": task.to_dict()},
            )
            return

        task.options = {
            "pool": [t.to_dict() for t in torrents[:80]],
            "queries": [task.params.get("title")],
        }
        await self.store.save_task(task)
        await self._selection_step(task, turn)

    async def _selection_step(self, task: Task, turn: Turn) -> None:
        """Решает: предложить выбор, подтвердить раздачу или честно сказать «нет»."""
        pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        params = task.params

        journal.add(
            "agent",
            f"поиск «{task.title}»",
            {
                "раздач на трекере": len(pool),
                "по названию": len(filter_candidates(pool, {"title": params.get("title") or ""})),
                "параметры": params,
            },
        )
        by_title = filter_candidates(pool, {"title": params.get("title") or ""})
        if not by_title:
            task.state = "not_found"
            await self.store.save_task(task)
            text = await self._not_found_text(task)
            message = turn.add(text, kind="error", task_id=task.id, payload={"task": task.to_dict()})
            message.actions = [
                Action(
                    id="make_periodic",
                    label="Сделать периодической",
                    kind="make_periodic",
                    payload={
                        "task_id": task.id,
                        "interval_minutes": self.settings.default_interval_minutes,
                        "auto_download": False,
                    },
                    style="primary",
                )
            ]
            return

        matched = filter_candidates(by_title, params)
        downloaded = await self.store.downloaded_ids()

        if not matched:
            # Нашлось, но не подходит под качество/озвучку — показываем, что есть.
            options = collect_options(by_title)
            if options["total"] and (params.get("quality") or params.get("voice")):
                text = self._no_match_text(task, by_title, options)
                message = turn.add(
                    text, kind="tickets", task_id=task.id, payload={"task": task.to_dict()}
                )
                message.actions = self._option_actions(task, by_title)
                task.state = "awaiting_choice"
                await self.store.save_task(task)
                return
            task.state = "not_found"
            await self.store.save_task(task)
            text = await self._not_found_text(task)
            message = turn.add(text, kind="error", task_id=task.id, payload={"task": task.to_dict()})
            message.actions = [
                Action(
                    id="make_periodic",
                    label="Сделать периодической",
                    kind="make_periodic",
                    payload={"task_id": task.id, "interval_minutes": 30, "auto_download": False},
                    style="primary",
                )
            ]
            return

        fresh = [t for t in matched if t.id not in downloaded] or matched

        # Не хватает параметров — предлагаем то, что реально есть на трекере.
        missing: list[str] = []
        if not params.get("quality"):
            missing.append("quality")
        if not params.get("voice"):
            missing.append("voice")
        if missing:
            options = collect_options(by_title)
            for dimension in missing:
                values = options["qualities"] if dimension == "quality" else options["voices"]
                if len(values) > 1:
                    text = self._choice_text(task, dimension, values, len(fresh))
                    message = turn.add(
                        text,
                        kind="tickets",
                        task_id=task.id,
                        payload={"task": task.to_dict(), "options": options},
                    )
                    message.actions = [
                        Action(
                            id=f"choose-{dimension}-{index}",
                            label=self._quality_label(value) if dimension == "quality" else value,
                            kind="choose",
                            payload={"task_id": task.id, "dimension": dimension, "value": value},
                        )
                        for index, value in enumerate(values[:8])
                    ]
                    message.actions.append(
                        Action(
                            id="cancel-choose",
                            label="Отмена",
                            kind="cancel",
                            payload={"task_id": task.id},
                            style="danger",
                        )
                    )
                    task.state = "awaiting_choice"
                    await self.store.save_task(task)
                    return
                if len(values) == 1:
                    # Один вариант — спрашивать не о чем, фиксируем автоматически.
                    task.params[dimension] = values[0]
                    await self.store.save_task(task)
                    fresh = filter_candidates(by_title, task.params) or fresh

        best_list = rank(fresh, task.params)
        best = best_list[0]
        journal.add(
            "agent",
            "нашёл кандидата, жду подтверждения",
            {"раздача": best.title, "размер": best.size, "сидов": best.seeds},
        )
        task.candidate = best.to_dict()
        task.options = {
            **(task.options or {}),
            "alternatives": [t.to_dict() for t in best_list[1:4]],
        }
        task.state = "awaiting_confirm"
        await self.store.save_task(task)

        if not params.get("quality") and best.quality:
            task.params["quality"] = best.quality
        if not params.get("voice") and best.voices:
            task.params["voice"] = best.voices[0]
        await self.store.save_task(task)

        message = turn.add(
            self._confirm_text(task, best, best_list[1:4]),
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict(), "torrent": best.to_dict()},
        )
        message.actions = [
            Action(
                id="confirm-download",
                label="Скачать…",
                kind="confirm_download",
                payload={"task_id": task.id, "torrent_id": best.id},
                style="primary",
            ),
            Action(
                id="cancel-confirm",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            ),
        ]
        for index, alternative in enumerate(best_list[1:4], start=2):
            message.actions.append(
                Action(
                    id=f"pick-{alternative.id}",
                    label=f"Взять вариант {index}",
                    kind="pick_torrent",
                    payload={"task_id": task.id, "torrent_id": alternative.id},
                )
            )

    @staticmethod
    def _quality_label(value: str) -> str:
        return QUALITY_LABELS.get(value, value)

    def _option_actions(self, task: Task, by_title: list[Torrent]) -> list[Action]:
        """Кнопки «что есть на самом деле», когда заданные параметры не совпали."""
        options = collect_options(by_title)
        actions: list[Action] = []
        for value in options["qualities"][:5]:
            actions.append(
                Action(
                    id=f"choose-quality-{value}",
                    label=self._quality_label(value),
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "quality", "value": value},
                )
            )
        for value in options["voices"][:5]:
            actions.append(
                Action(
                    id=f"choose-voice-{value}",
                    label=value,
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "voice", "value": value},
                )
            )
        actions.append(
            Action(
                id="cancel-options",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )
        return actions

    # ------------------------------------------------------------------
    # Периодическая задача
    # ------------------------------------------------------------------
    async def _start_periodic(
        self, session_id: str, text: str, parsed: dict[str, Any], turn: Turn
    ) -> None:
        if not parsed.get("title"):
            turn.add("Не понял, за чем следить. Напишите название фильма или сериала.")
            return

        interval = parsed.get("interval_minutes") or self.settings.default_interval_minutes
        interval = max(self.settings.min_interval_minutes, int(interval))
        task = await self._task_from_params(
            session_id,
            text,
            parsed,
            kind="periodic",
            interval=interval,
            auto_download=bool(parsed.get("auto_download")),
        )
        turn.task = task

        try:
            torrents = await self._search(task.params)
        except MCPError as exc:
            task.state = "failed"
            task.last_error = str(exc)
            await self.store.save_task(task)
            turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
            return

        task.options = {"pool": [t.to_dict() for t in torrents[:80]]}
        by_title = filter_candidates(torrents, {"title": task.params.get("title") or ""})
        matched = filter_candidates(by_title, task.params)
        best = rank(matched or by_title, task.params)
        # Что уже есть на трекере сейчас — это «норма», а не новость. Иначе
        # первая же проверка пришлёт уведомление про все 30 раздач сразу.
        self._record_baseline(task, matched or by_title)
        task.candidate = best[0].to_dict() if best else None
        task.state = "awaiting_confirm"
        await self.store.save_task(task)

        head = (
            f"Задача «{task.title}» — проверять каждые "
            f"{self._human_interval(task.interval_minutes)}."
        )
        if best:
            head += f"\n\nСейчас на трекере есть:\n• {describe(best[0])}"
            if len(best) > 1:
                head += f"\n• и ещё {len(best) - 1} раздач(и) по этому запросу"
            if not task.params.get("quality"):
                options = collect_options(by_title)
                if options["qualities"]:
                    head += "\n\nКачество в этих раздачах: " + ", ".join(
                        self._quality_label(q) for q in options["qualities"][:4]
                    )
            if task.auto_download:
                head += (
                    "\n\nВы просили скачивать новые серии сразу — подтвердите, "
                    "и я начну следить и качать."
                )
            else:
                head += "\n\nЧто делаем с новыми сериями?"
        else:
            head += (
                "\n\nСейчас по этим параметрам на трекере ничего нет — задача будет "
                "молча проверять и сообщит, когда появится."
            )

        message = turn.add(
            head,
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict(), "torrent": (best[0].to_dict() if best else None)},
        )
        message.actions = [
            Action(
                id="periodic-auto",
                label="Качать сразу и сообщать",
                kind="periodic_auto",
                payload={"task_id": task.id},
                style="primary",
            ),
            Action(
                id="periodic-notify",
                label="Только сообщать",
                kind="periodic_notify",
                payload={"task_id": task.id},
            ),
            Action(
                id="cancel-periodic",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            ),
        ]

    @staticmethod
    def _record_baseline(task: Task, torrents: list[Torrent]) -> None:
        """Запоминает, что уже есть на трекере в момент создания задачи."""
        ids = [t.id for t in torrents if t.id]
        stamps = [t.uploaded_ts for t in torrents if t.uploaded_ts]
        seen = list(task.params.get("seen_ids") or [])
        for torrent_id in ids:
            if torrent_id not in seen:
                seen.append(torrent_id)
        task.params["seen_ids"] = seen[-400:]
        by_season = episodes_by_season(torrents)
        if by_season:
            # Ключи JSON всегда строки — приводим к ним явно.
            task.params["baseline_episodes"] = {str(k): v for k, v in by_season.items()}
        if stamps:
            task.params["baseline_ts"] = max(stamps)

    async def _monitor_after_creation(self, task: Task, turn: Turn, downloaded_now: bool) -> None:
        task.state = "monitoring"
        self._schedule_next(task)
        await self.store.save_task(task)
        await self.events.publish(
            {
                "type": "task_created",
                "task_id": task.id,
                "title": task.title,
                "interval_minutes": task.interval_minutes,
            }
        )
        message = turn.add(
            self._monitoring_text(task), kind="task", task_id=task.id, payload={"task": task.to_dict()}
        )
        message.actions = self._task_actions(task)

    def _monitoring_text(self, task: Task) -> str:
        mode = "и сразу качать" if task.auto_download else "и сообщать о находках"
        return (
            f"Взял в работу: «{task.title}».\n"
            f"Буду проверять каждые {self._human_interval(task.interval_minutes)} {mode}.\n"
            f"{self._filter_text(task)}"
        )

    def _task_actions(self, task: Task) -> list[Action]:
        actions: list[Action] = []
        if task.state == "monitoring":
            actions.append(
                Action(
                    id="check-now",
                    label="Проверить сейчас",
                    kind="check_now",
                    payload={"task_id": task.id},
                )
            )
        actions.append(
            Action(
                id="cancel-task",
                label="Отменить задачу",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )
        return actions

    # ------------------------------------------------------------------
    # Скачивание
    # ------------------------------------------------------------------
    def _entries_from_task(self, task: Task) -> list[Any]:
        """Файлы раздачи, сохранённые при первом шаге (без повторного разбора)."""
        raw = (task.options or {}).get("files") or []
        return parse_files(raw)

    async def _fetch_torrent(self, torrent: Torrent, *, refresh: bool = False) -> tuple[Path | None, str]:
        """Сохраняет .torrent на внешний диск. Возвращает (путь, ошибка).

        `refresh=True` — перекачать даже если файл уже есть. Это обязательно для
        периодических задач: на трекере одна и та же раздача обновляется новыми
        выпусками, и старый .torrent уже не содержит нужную серию.
        """
        try:
            ensure_dir(self.settings.torrents_dir)
        except TargetUnavailable as exc:
            journal.add(
                "agent",
                "не могу сохранить .torrent: диск недоступен",
                {"ошибка": str(exc), "папка": str(self.settings.torrents_dir)},
                level="error",
            )
            return None, f"папка для torrent-файлов недоступна: {exc}"

        path = self.settings.torrents_dir / f"{torrent.id}.torrent"
        if not refresh and path.exists() and path.stat().st_size > 0:
            return path, ""  # уже скачан — второй раз не тянем

        result = await self.mcp.download(torrent.id, path)
        if not result.get("success"):
            return None, str(result.get("message") or "не удалось скачать torrent-файл")
        return path, ""

    async def _download_with_report(
        self, task: Task, torrent: Torrent, turn: Turn, periodic_context: bool = False
    ) -> None:
        """Шаг 1: скачать .torrent, разобрать файлы и показать полную карточку.

        Здесь выясняется, что именно попадёт на диск: одна серия или вся пачка,
        сколько это весит и хватает ли места. Если серия не названа, а в раздаче
        их несколько — предлагаем выбрать кликабельными кнопками.
        """
        task.state = "downloading"
        await self.store.save_task(task)
        journal.add(
            "agent",
            f"готовлю скачивание: {torrent.title}",
            {"id": torrent.id, "качество": torrent.quality, "озвучка": torrent.voice},
        )

        path, error = await self._fetch_torrent(torrent)
        if path is None:
            task.state = "failed"
            task.last_error = error
            await self.store.save_task(task)
            await self._report_storage_problem(
                task, torrent, {"error": error, "reason": "disk", "path": None}, turn
            )
            return

        entries = parse_files(file_list(path))
        plan = self._plan_files(task, entries)
        task.options = {
            **(task.options or {}),
            "files": [
                {"index": entry.index, "name": entry.name, "size": entry.size,
                 "season": entry.season, "episode": entry.episode}
                for entry in entries[:400]
            ],
            "plan": plan,
            "torrent_path": str(path),
        }

        if plan["kind"] == "choose":
            task.state = "awaiting_choice"
            await self.store.save_task(task)
            message = turn.add(
                self._episode_choice_text(task, plan),
                kind="tickets",
                task_id=task.id,
                payload={"task": task.to_dict(), "episodes": plan["episodes"]},
            )
            message.actions = self._episode_actions(task, plan)
            return

        task.state = "awaiting_confirm"
        await self.store.save_task(task)
        message = turn.add(
            self._confirm_card_text(task, torrent, plan),
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict(), "plan": plan},
        )
        message.actions = self._start_actions(task, plan)

    # ------------------------------------------------------------------
    # Что именно качать
    # ------------------------------------------------------------------
    def _plan_files(self, task: Task, entries: list[Any]) -> dict[str, Any]:
        """Решение «вся раздача / конкретная серия / спросить какую»."""
        params = task.params or {}
        season = params.get("season")
        try:
            season = int(season) if season not in (None, "") else None
        except (TypeError, ValueError):
            season = None
        episode = params.get("episode_from") or params.get("episode")
        try:
            episode = int(episode) if episode not in (None, "") else None
        except (TypeError, ValueError):
            episode = None

        total_size = sum(entry.size for entry in entries)
        videos = [entry for entry in entries if entry.is_video and not entry.is_junk]
        groups = episode_groups(entries)
        pack = len({(group["season"], group["episode"]) for group in groups}) > 1

        base = {
            "total_size": total_size,
            "total_files": len(entries),
            "season": season,
            "episode": episode,
            "episodes": groups,
            "pack": pack,
        }

        if episode is not None:
            picked = pick_episode(entries, season=season, episode=episode)
            if picked:
                return {
                    **base,
                    "kind": "episode",
                    "wanted": picked["indexes"],
                    "size": picked["size"],
                    "names": picked["names"],
                    "episode": picked["episode"],
                    "season": picked["season"] if picked["season"] is not None else season,
                    # Что именно скачаем — по этим сериям пишется память агента.
                    "selected_episodes": [picked["episode"]],
                }
            if pack:
                return {**base, "kind": "choose", "reason": "missing_episode"}

        if pack:
            return {**base, "kind": "choose", "reason": "no_episode"}

        # Одиночный файл или фильм — качаем целиком.
        wanted = None
        if videos and len(videos) < len(entries):
            # Выкидываем .nfo/.jpg/sample, оставляя видео и субтитры рядом с ним.
            wanted = [entry.index for entry in entries if not entry.is_junk]
        return {
            **base,
            "kind": "single",
            "wanted": wanted,
            "size": sum(entry.size for entry in entries if entry.index in (wanted or [entry.index for entry in entries])),
            "names": [entry.label for entry in entries if entry.index in (wanted or [entry.index for entry in entries])][:10],
        }

    def _episode_choice_text(self, task: Task, plan: dict[str, Any]) -> str:
        episodes = plan.get("episodes") or []
        head = (
            f"Серии {plan.get('episode')} в этой раздаче нет. Какая нужна?"
            if plan.get("reason") == "missing_episode"
            else f"В раздаче {len(episodes)} серий — какую качать?"
        )
        seasons = {group.get("season") for group in episodes if group.get("season")}
        lines = [head]
        if len(seasons) == 1:
            lines.append(f"Сезон {seasons.pop()}.")
        lines.append(f"Вес всей раздачи: {human_bytes(plan.get('total_size'))}.")
        shown = []
        for group in episodes[:24]:
            label = f"серия {group['episode']}"
            if len(seasons if isinstance(seasons, set) else []) > 1 and group.get("season"):
                label = f"{group['season']}×{group['episode']}"
            shown.append(f"{label} — {human_bytes(group['size'])}")
        lines.append("")
        lines.append("; ".join(shown))
        if len(episodes) > 24:
            lines.append(f"… и ещё {len(episodes) - 24} (номер можно указать в запросе)")
        lines.append("")
        lines.append("Выберите серию кнопкой ниже или нажмите «Скачать весь сезон».")
        return "\n".join(lines)

    def _episode_actions(self, task: Task, plan: dict[str, Any]) -> list[Action]:
        actions: list[Action] = []
        for group in (plan.get("episodes") or [])[:24]:
            episode = group.get("episode")
            season = group.get("season")
            label = f"Серия {episode}"
            if season and plan.get("season") and season != plan.get("season"):
                label = f"{season}×{episode}"
            actions.append(
                Action(
                    id=f"episode-{season}-{episode}",
                    label=label,
                    kind="choose_episode",
                    payload={"task_id": task.id, "episode": episode, "season": season},
                )
            )
        actions.append(
            Action(
                id="download-whole-pack",
                label="Скачать весь сезон",
                kind="download_all",
                payload={"task_id": task.id},
                style="primary" if not actions else "normal",
            )
        )
        actions.append(
            Action(
                id="cancel-pack",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )
        return actions

    def _confirm_card_text(self, task: Task, torrent: Torrent, plan: dict[str, Any]) -> str:
        """Полная информация перед скачиванием: что, сколько и куда."""
        storage = self.transmission.storage_info()
        info = storage.to_dict()
        needed = int(plan.get("size") or plan.get("total_size") or 0)
        reserve = self.transmission.min_free_bytes()
        fits = storage.ok and storage.free >= needed + reserve

        lines = [f"Раздача: {torrent.title}", ""]
        if torrent.qualities:
            lines.append("• Качество: " + ", ".join(torrent.qualities[:3]))
        if torrent.voices:
            lines.append("• Озвучка: " + ", ".join(torrent.voices[:3]))
        if plan.get("season") or plan.get("episode"):
            season = plan.get("season")
            episode = plan.get("episode")
            season_text = f"Сезон {season}" if season else "Сезон —"
            episode_text = f"серия {episode}" if episode else "серия —"
            lines.append(f"• {season_text}, {episode_text}")
        if torrent.size:
            lines.append(f"• На трекере: {torrent.size}")

        names = plan.get("names") or []
        if plan.get("kind") == "single" and len(names) == 1:
            lines.append(f"• Что качаем: {names[0]}")
        elif names:
            lines.append(f"• Что качаем: {_plural(len(names), 'файл', 'файла', 'файлов')}")
            for name in names[:5]:
                lines.append(f"    – {name}")
            if len(names) > 5:
                lines.append(f"    … и ещё {len(names) - 5}")
        else:
            lines.append("• Что качаем: вся раздача")

        lines.append("")
        lines.append(f"• Вес выбранного: {human_bytes(needed)}")
        if plan.get("total_size") and needed != plan.get("total_size"):
            lines.append(f"• Вес всей раздачи: {human_bytes(plan.get('total_size'))}")
        if storage.ok:
            lines.append(
                f"• Диск «{info['volume']}»: свободно {info['free_human']} из "
                f"{info['total_human']} (резерв {human_bytes(reserve)})"
            )
            lines.append(
                "• Места хватает" if fits
                else f"• ⚠ Места не хватает: нужно {human_bytes(needed + reserve)}"
            )
        else:
            lines.append(f"• ⚠ Внешний диск недоступен: {storage.error}")
        lines.append(f"• Куда: {self.settings.transmission_download_dir}")

        lines.append("")
        lines.append("Ставлю в Transmission?" if fits else "Всё равно поставить в очередь?")
        return "\n".join(lines)

    def _start_actions(self, task: Task, plan: dict[str, Any]) -> list[Action]:
        actions = [
            Action(
                id="start-download",
                label="Поставить в Transmission",
                kind="start_download",
                payload={"task_id": task.id},
                style="primary",
            )
        ]
        if plan.get("kind") == "episode" and plan.get("pack"):
            actions.append(
                Action(
                    id="download-whole-pack",
                    label="Скачать весь сезон",
                    kind="download_all",
                    payload={"task_id": task.id},
                )
            )
        actions.append(
            Action(
                id="cancel-download",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )
        return actions

    async def _report_storage_problem(
        self, task: Task, torrent: Torrent, result: dict[str, Any], turn: Turn
    ) -> None:
        """Сообщает, что скачать некуда: нет диска или мало места.

        .torrent уже лежит на диске агента, поэтому как только место появится,
        раздача ставится в очередь одной кнопкой — заново искать не нужно.
        """
        storage = self.transmission.storage_info()
        head = (
            "Не могу поставить раздачу в очередь — внешний диск не готов."
            if result.get("reason") == "disk"
            else "Не хватает места на внешнем диске."
        )
        text = f"{head}\n\n• {torrent.title}\n{result['error']}"
        if storage.ok:
            text += (
                f"\n\nДиск «{storage.to_dict()['volume']}»: свободно "
                f"{storage.to_dict()['free_human']} из {storage.to_dict()['total_human']}."
            )
        if result.get("path"):
            text += (
                f"\n\nФайл раздачи уже сохранён ({result.get('path')}), "
                "освободите место и нажмите «Поставить в очередь»."
            )
        else:
            text += (
                "\n\nTorrent-файл сохранить не удалось — подключите диск и "
                "повторите попытку, искать раздачу заново не нужно."
            )

        message = turn.add(text, kind="error", task_id=task.id, payload={"task": task.to_dict()})
        if result.get("path"):
            primary = Action(
                id="retry-transmission",
                label="Поставить в очередь",
                kind="retry_transmission",
                payload={"task_id": task.id, "torrent_id": torrent.id},
                style="primary",
            )
        else:
            primary = Action(
                id="retry-download",
                label="Повторить скачивание",
                kind="confirm_download",
                payload={"task_id": task.id, "torrent_id": torrent.id},
                style="primary",
            )
        message.actions = [
            primary,
            Action(
                id="cancel-after-space",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            ),
        ]
        await self.events.publish(
            {
                "type": "notification",
                "level": "error",
                "task_id": task.id,
                "title": "Нет места на внешнем диске",
                "text": text,
                "message": message.to_dict(),
            }
        )
        await self.desktop.send("Нет места на внешнем диске", f"{torrent.title}"[:120])
        await self.push.send("Нет места на внешнем диске", text[:200])

    # ------------------------------------------------------------------
    # Финальная постановка в очередь
    # ------------------------------------------------------------------
    async def _start_download(self, task: Task, turn: Turn, plan: dict[str, Any] | None = None,
                              periodic_context: bool = False) -> None:
        torrent = Torrent.from_dict(task.candidate or {})
        plan = plan or (task.options or {}).get("plan") or {}
        path_str = (task.options or {}).get("torrent_path") or ""
        path = Path(path_str) if path_str else self.settings.torrents_dir / f"{torrent.id}.torrent"
        if not path.exists():
            turn.add(
                "Torrent-файл пропал — начните задачу заново.",
                kind="error",
                task_id=task.id,
            )
            return

        result = await self._finalize_download(task, torrent, path, plan)
        if not result["ok"]:
            if result.get("retryable"):
                await self._report_storage_problem(task, torrent, result, turn)
            else:
                turn.add(
                    f"Не удалось поставить «{torrent.title}» в очередь: {result['error']}",
                    kind="error",
                    task_id=task.id,
                    payload={"task": task.to_dict()},
                )
            return

        if periodic_context:
            task.state = "monitoring"
            self._schedule_next(task)
        else:
            task.state = "done"
        await self.store.save_task(task)

        text = self._downloaded_text(torrent, result, plan)
        message = turn.add(text, kind="task", task_id=task.id, payload={"task": task.to_dict()})
        if task.state == "monitoring":
            message.actions = self._task_actions(task)
        await self.events.publish(
            {
                "type": "download_done",
                "task_id": task.id,
                "title": torrent.title,
                "torrent_id": torrent.id,
            }
        )
        await self.desktop.send("Торрент скачан", torrent.title[:120])

    async def _auto_download(self, task: Task, torrent: Torrent, turn: Turn) -> None:
        """Скачивание без диалога (периодическая задача в режиме «качать сразу»)."""
        result = await self._download(task, torrent)
        if result.get("ok") and result.get("skipped"):
            turn.add(
                f"«{torrent.title}» — {result['reason']}. Продолжаю следить.",
                kind="text",
                task_id=task.id,
            )
            return
        if not result["ok"]:
            if result.get("retryable"):
                await self._report_storage_problem(task, torrent, result, turn)
            else:
                turn.add(
                    f"Не удалось скачать «{torrent.title}»: {result['error']}",
                    kind="error",
                    task_id=task.id,
                )
            return
        task.state = "monitoring" if task.kind == "periodic" else "done"
        self._schedule_next(task)
        await self.store.save_task(task)
        message = turn.add(
            self._downloaded_text(torrent, result, result.get("plan") or {}),
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict()},
        )
        message.actions = self._task_actions(task)
        await self.events.publish(
            {"type": "download_done", "task_id": task.id, "title": torrent.title,
             "torrent_id": torrent.id}
        )
        await self.desktop.send("Торрент скачан", torrent.title[:120])

    def _downloaded_text(self, torrent: Torrent, result: dict[str, Any], plan: dict[str, Any]) -> str:
        lines = [f"Готово: «{torrent.title}»"]
        if plan.get("episode"):
            season = f"сезон {plan['season']}, " if plan.get("season") else ""
            lines.append(f"Качаю {season}серию {plan['episode']} — {human_bytes(plan.get('size'))}")
        elif plan.get("size"):
            lines.append(f"Качаю: {human_bytes(plan.get('size'))}")
        if result.get("transmission"):
            lines.append(f"Transmission: {result['transmission']}")
        elif result.get("transmission_error"):
            lines.append(f"Transmission: не принял торрент — {result['transmission_error']}")
        lines.append(f"Torrent-файл: {result['path']}")
        return "\n".join(lines)

    async def _finalize_download(
        self, task: Task, torrent: Torrent, path: Path, plan: dict[str, Any]
    ) -> dict[str, Any]:
        """Ставит раздачу в очередь (с выбором файлов) и запоминает факт."""
        wanted = plan.get("wanted")
        need = plan.get("size") or plan.get("total_size")
        added: dict[str, Any] = {}
        transmission_note = ""
        transmission_error = ""
        transmission_hash = ""
        try:
            added = await self.transmission.add(
                path,
                need_bytes=int(need) if need else None,
                files_wanted=list(wanted) if wanted is not None else None,
            )
            transmission_hash = str(added.get("hash") or "")
            selected = len(added.get("files_selected") or [])
            where = added.get("download_dir") or self.transmission.download_dir
            transmission_note = f"в очереди ({added.get('name') or torrent.title}) → {where}"
            if wanted is not None and selected:
                transmission_note += f", файлов выбрано: {selected}"
        except (NotEnoughSpace, StorageUnavailable) as exc:
            task.last_error = str(exc)
            task.state = "failed"
            await self.store.save_task(task)
            return {
                "ok": False,
                "error": str(exc),
                "retryable": True,
                "reason": "space" if isinstance(exc, NotEnoughSpace) else "disk",
                "path": str(path),
            }
        except TransmissionError as exc:
            transmission_error = str(exc)
            log.warning("Transmission не принял торрент: %s", exc)

        # Запоминаем каждую скачанную серию отдельной строкой. Иначе пачка
        # «1–10» осталась бы в памяти как одна запись, и следующая проверка
        # решила бы, что серии 8–10 уже есть. У Torrent серия — вычисляемое
        # свойство, поэтому подменяем список серий, а не поле episode.
        raw_selected = plan.get("selected_episodes") or (
            [plan["episode"]] if plan.get("episode") is not None else []
        )
        episodes: list[int] = []
        for item in raw_selected:
            value = item.get("episode") if isinstance(item, dict) else item
            if value is not None:
                episodes.append(int(value))
        if episodes:
            for episode in sorted(set(episodes)):
                try:
                    recorded = replace(
                        torrent,
                        episodes=[episode],
                        seasons=(
                            [int(plan["season"])]
                            if plan.get("season") is not None
                            else torrent.seasons
                        ),
                        episodes_total=torrent.episodes_total,
                    )
                except Exception:  # noqa: BLE001
                    recorded = torrent
                await self.store.record_download(
                    task, recorded, str(path), transmission_hash,
                    torrent_name=added.get("name") or "",
                )
        else:
            await self.store.record_download(
                task, torrent, str(path), transmission_hash, torrent_name=added.get("name") or ""
            )
        task.last_result = f"скачано: {torrent.title}"
        task.last_error = ""
        seen = list(task.params.get("seen_ids") or [])
        if torrent.id not in seen:
            seen.append(torrent.id)
        task.params["seen_ids"] = seen[-200:]
        return {
            "ok": True,
            "path": str(path),
            "transmission": transmission_note,
            "transmission_error": transmission_error,
            "plan": plan,
        }

    async def _download(self, task: Task, torrent: Torrent) -> dict[str, Any]:
        """Автоматический путь (периодические задачи): без диалога.

        Качаем ровно те серии, ради которых задача заведена: начиная с
        `episode_from` и не ниже той, что идёт за уже скачанной. Если раздача —
        пачка на 14 выпусков, где сейчас 1–2, а нужна третья, то при появлении
        «1–3» скачается только третий файл.
        """
        path, error = await self._fetch_torrent(torrent, refresh=True)
        if path is None:
            task.last_error = error
            task.state = "failed"
            await self.store.save_task(task)
            return {"ok": False, "error": error, "retryable": True, "reason": "disk", "path": None}

        entries = parse_files(file_list(path))
        plan = self._plan_files(task, entries)

        params = task.params or {}
        try:
            season = int(params["season"]) if params.get("season") is not None else None
        except (TypeError, ValueError):
            season = None
        next_episode = int(params.get("next_episode") or 0)
        if not next_episode:
            next_episode = self._next_episode(params, {})

        picked = None
        if next_episode:
            picked = pick_new_episodes(entries, after_episode=next_episode - 1, season=season)
        if picked:
            journal.add(
                "agent",
                f"автоскачивание: серии {picked.get('episodes')}",
                {"next_episode": next_episode, "размер": picked.get("size")},
            )
            plan = {
                **plan,
                "kind": "episode",
                "wanted": picked["indexes"],
                "size": picked["size"],
                "names": picked["names"],
                "episode": picked["episode"],
                "season": season,
                "selected_episodes": [
                    int(item) for item in (picked.get("episodes") or []) if item is not None
                ],
            }
        elif plan["kind"] == "choose" and plan.get("pack") and next_episode:
            # В раздаче есть серии, но нужной пока нет (например, просили с
            # третьей, а на трекере 1–2). Ничего не качаем: ждём появления.
            return {
                "ok": True,
                "skipped": True,
                "reason": f"серий с {next_episode} в раздаче пока нет",
                "path": str(path),
                "plan": plan,
            }
        elif plan["kind"] == "choose":
            # Серий в раздаче не разобрать вовсе — берём всю пачку, как раньше.
            plan = {**plan, "kind": "all", "wanted": None, "size": plan.get("total_size")}

        task.options = {**(task.options or {}), "plan": plan, "torrent_path": str(path)}
        return await self._finalize_download(task, torrent, path, plan)

    async def _last_episode_for(self, task: Task) -> int:
        """Максимальная скачанная серия по сериалу задачи."""
        record = await self.store.list_downloads(limit=200)
        title = str((task.params or {}).get("title") or "")
        best = 0
        for item in record:
            if title and title.lower() not in str(item.get("title") or "").lower():
                continue
            if item.get("episode"):
                best = max(best, int(item["episode"]))
        return best

    async def _retry_transmission(self, task: Task, turn: Turn) -> None:
        """Повторная попытка постановки уже скачанного .torrent в очередь."""
        torrent = Torrent.from_dict(task.candidate or {})
        plan = (task.options or {}).get("plan") or {}
        path_str = (task.options or {}).get("torrent_path") or ""
        path = Path(path_str) if path_str else self.settings.torrents_dir / f"{torrent.id}.torrent"
        if not path.exists():
            turn.add(
                "Файл раздачи не найден — начните задачу заново.",
                kind="error",
                task_id=task.id,
            )
            return
        wanted = plan.get("wanted")
        try:
            added = await self.transmission.add(
                path,
                need_bytes=int(plan.get("size") or plan.get("total_size") or 0) or None,
                files_wanted=list(wanted) if wanted is not None else None,
            )
        except (NotEnoughSpace, StorageUnavailable) as exc:
            task.state = "failed"
            task.last_error = str(exc)
            await self.store.save_task(task)
            await self._report_storage_problem(
                task,
                torrent,
                {"error": str(exc), "reason": "space", "path": str(path)},
                turn,
            )
            return
        except TransmissionError as exc:
            turn.add(f"Transmission не принял раздачу: {exc}", kind="error", task_id=task.id)
            return

        await self.store.record_download(
            task, torrent, str(path), str(added.get("hash") or "")
        )
        task.state = "done" if task.kind == "oneshot" else "monitoring"
        task.last_error = ""
        task.last_result = f"скачано: {torrent.title}"
        if task.kind == "periodic":
            self._schedule_next(task)
        await self.store.save_task(task)
        message = turn.add(
            f"Поставил в очередь: «{torrent.title}» → {added.get('download_dir')}",
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict()},
        )
        if task.kind == "periodic":
            message.actions = self._task_actions(task)

    # ------------------------------------------------------------------
    # Удаление раздачи вместе с файлами
    # ------------------------------------------------------------------
    async def request_delete(self, torrent_id: str) -> Turn:
        """Спрашивает подтверждение: удаление файлов необратимо."""
        turn = Turn()
        record = await self.store.get_download(torrent_id)
        if record is None:
            turn.add(
                "Не нашёл эту раздачу в списке скачанного — обновите картотеку.",
                kind="error",
            )
            return turn

        size_note = f" ({record.get('size')})" if record.get("size") else ""
        lines = [
            f"Удалить раздачу «{record.get('title')}»{size_note}?",
        ]
        if record.get("name"):
            lines.append(f"Будет удалено: {record.get('name')} в каталоге загрузок.")
        if record.get("torrent_path"):
            lines.append(f"Torrent-файл: {record.get('torrent_path')}")
        lines.append(
            "Файлы стираются насовсем, минуя корзину — восстановить их не получится."
        )
        message = turn.add("\n".join(lines), kind="error", payload={"download": record})
        message.actions = [
            Action(
                id="delete-forever",
                label="Удалить навсегда",
                kind="confirm_delete",
                payload={"torrent_id": torrent_id},
                style="danger",
            ),
            Action(
                id="cancel-delete",
                label="Отмена",
                kind="cancel",
                payload={"torrent_id": torrent_id, "task_id": record.get("task_id") or ""},
            ),
        ]
        return turn

    async def perform_delete(self, torrent_id: str) -> Turn:
        """Убирает раздачу из Transmission, стирает файлы и .torrent."""
        from core.files import delete_paths

        turn = Turn()
        record = await self.store.get_download(torrent_id)
        if record is None:
            turn.add("Запись уже удалена — обновите картотеку.", kind="error")
            return turn

        torrent_hash = str(record.get("transmission_hash") or "")
        name = str(record.get("name") or "")
        download_dir = Path(self.settings.transmission_download_dir)
        file_list: list[str] = []

        # 1. Раздача ещё в очереди? Забираем список файлов и убираем её оттуда
        #    без delete_local_data: иначе macOS отправит данные в корзину.
        if torrent_hash:
            try:
                found = await self.transmission.find_by_hash(torrent_hash)
            except TransmissionError as exc:
                found = None
                journal.add(
                    "transmission", "не смог прочитать раздачу перед удалением",
                    {"ошибка": str(exc)}, level="warn",
                )
            if found:
                if found.get("download_dir"):
                    download_dir = Path(found["download_dir"])
                file_list = [str(item) for item in found.get("files") or []]
                if not name:
                    name = str(found.get("name") or "")
                try:
                    await self.transmission.remove(torrent_hash, delete_data=False)
                except TransmissionError as exc:
                    turn.add(f"Не удалось убрать раздачу из Transmission: {exc}", kind="error")
                    return turn

        # 2. Что именно удалять на диске.
        roots = [download_dir, download_dir / ".incomplete", self.settings.torrents_dir]
        targets: list[Path] = []
        if name:
            targets.append(download_dir / name)
            targets.append(download_dir / ".incomplete" / name)
        for item in file_list:
            targets.append(download_dir / item)
        torrent_path = str(record.get("torrent_path") or "")
        if torrent_path:
            targets.append(Path(torrent_path))
        else:
            targets.append(self.settings.torrents_dir / f"{torrent_id}.torrent")

        result = await asyncio.to_thread(delete_paths, targets, roots)

        # Повторная проверка: если Transmission успел дописать файл уже после
        # удаления, папка появляется снова. Дочищаем (до трёх подходов).
        for _ in range(3):
            leftovers = [path for path in targets if path.exists()]
            if not leftovers:
                break
            await asyncio.sleep(1.0)
            extra = await asyncio.to_thread(delete_paths, leftovers, roots)
            result.deleted.extend(extra.deleted)
            result.freed_bytes += extra.freed_bytes
            result.errors.extend(extra.errors)

        # 3. Забываем раздачу: файлов больше нет.
        await self.store.forget_download(torrent_id)
        journal.add(
            "agent",
            f"удалена раздача: {record.get('title')}",
            {
                "файлов удалено": len(result.deleted),
                "освобождено": result.summary(),
                "вне разрешённых папок": result.refused[:5] or None,
                "ошибки": result.errors or None,
            },
            level="error" if result.errors else "info",
        )

        text = f"Удалил «{record.get('title')}» из очереди и с диска. {result.summary()}"
        if result.missing and not result.deleted:
            text += "\nФайлов на диске уже не было — убрал только запись и torrent-файл."
        if result.errors:
            text += "\nПроблемы:\n• " + "\n• ".join(result.errors[:5])
        last = turn.add(text, kind="error" if result.errors else "task")
        await self.events.publish(
            {
                "type": "download_deleted",
                "torrent_id": torrent_id,
                "title": str(record.get("title") or ""),
                "message": last.to_dict(),
            }
        )
        await self.desktop.send("Раздача удалена", str(record.get("title") or "")[:120])
        return turn

    # ------------------------------------------------------------------
    # Периодическая проверка (вызывается шедулером)
    # ------------------------------------------------------------------
    def _schedule_next(self, task: Task) -> None:
        interval = task.interval_minutes or self.settings.default_interval_minutes
        task.next_check_at = (
            datetime.now(timezone.utc) + timedelta(minutes=interval)
        ).isoformat(timespec="seconds")

    async def _scheduler_loop(self) -> None:
        """Независимые проверки: у каждой задачи своя корутина, лимита нет."""
        while True:
            try:
                due = await self.store.due_periodic()
                for task in due:
                    if task.id in self._running_checks:
                        continue
                    asyncio.create_task(self._run_check_isolated(task))
                await asyncio.sleep(self.scheduler_tick_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - шедулер не должен умирать
                log.exception("ошибка в шедулере")
                await asyncio.sleep(5)

    @staticmethod
    def _next_episode(params: dict[str, Any], season_episodes: dict[int, int]) -> int:
        """Следующая серия, которую надо взять: max(«начиная с N», скачанные+1)."""
        try:
            floor = int(params.get("episode_from") or 0)
        except (TypeError, ValueError):
            floor = 0
        try:
            season = int(params["season"]) if params.get("season") is not None else None
        except (TypeError, ValueError):
            season = None
        if season is not None:
            known = int(season_episodes.get(season, 0))
        else:
            known = max(season_episodes.values() or [0])
        return max(floor, known + 1 if known else 0)

    async def _run_check_isolated(self, task: Task) -> None:
        """Одна задача — одна корутина: падение одной не влияет на остальные."""
        self._running_checks.add(task.id)
        try:
            await self.check_task(task)
        except Exception:  # noqa: BLE001
            log.exception("проверка задачи %s упала", task.id)
        finally:
            self._running_checks.discard(task.id)

    async def check_task(self, task: Task, *, manual: bool = False, turn: Turn | None = None) -> None:
        lock = self._check_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            turn = turn or Turn()
            params = task.params
            task.last_check_at = now_iso()
            task.checks_done += 1
            try:
                torrents = await self._search(params)
            except MCPError as exc:
                task.last_error = str(exc)
                self._schedule_next(task)
                await self.store.save_task(task)
                await self.store.add_check(task.id, 0, 0, f"ошибка: {exc}")
                log.warning("проверка %s: %s", task.id, exc)
                return

            by_title = filter_candidates(torrents, {"title": params.get("title") or ""})
            matched = filter_candidates(by_title, params)
            seen = set(params.get("seen_ids") or [])
            seen |= await self.store.downloaded_ids()

            pool = matched or by_title
            series = str(pool[0].series_key) if pool else ""
            season_episodes: dict[int, int] = {}
            max_ts = 0.0
            if series:
                season_episodes = await self.store.episodes_by_season(series)
                max_ts = await self.store.last_upload_ts(series)
            # То, что уже было на трекере при создании задачи, новым не считаем.
            for season_key, episode in (params.get("baseline_episodes") or {}).items():
                try:
                    season_number = int(season_key)
                    season_episodes[season_number] = max(
                        season_episodes.get(season_number, 0), int(episode)
                    )
                except (TypeError, ValueError):
                    continue
            max_ts = max(max_ts, float(params.get("baseline_ts") or 0.0))

            # Какую серию ищем следующей: «начиная с N» из запроса, но не ниже
            # той, что идёт за уже скачанной. Так задача «качай новые серии,
            # начиная с третьей» после скачивания третьей ищет четвёртую, а не
            # третью повторно.
            next_episode = self._next_episode(params, season_episodes)
            task.params["next_episode"] = next_episode

            fresh = find_new(
                matched or by_title,
                params,
                known_ids=seen,
                season_episodes=season_episodes,
                max_uploaded_ts=max_ts,
                next_episode=next_episode,
            )
            task.found_total = len(matched)
            task.next_check_at = None
            self._schedule_next(task)
            await self.store.add_check(
                task.id, len(matched), len(fresh), ", ".join(t.title for t in fresh[:3])
            )
            journal.add(
                "agent",
                f"проверка задачи «{task.title}»",
                {
                    "подходящих раздач": len(matched),
                    "новых": len(fresh),
                    "новинки": [item.title for item in fresh[:3]] or None,
                    "автоскачивание": task.auto_download,
                },
            )
            if not fresh:
                await self.store.save_task(task)
                if manual:
                    turn.add(
                        f"Проверил «{task.title}»: нового нет "
                        f"(всего подходящих раздач: {len(matched)}).",
                        kind="text",
                        task_id=task.id,
                    )
                return

            task.params["seen_ids"] = list(seen | {t.id for t in fresh})[-200:]
            task.options = {**(task.options or {}), "pool": [t.to_dict() for t in fresh[:20]]}
            await self.store.save_task(task)

            if task.auto_download:
                for torrent in fresh[:3]:
                    await self._download_quiet(task, torrent)
            else:
                await self._notify_new(task, fresh)

            if manual:
                turn.add(
                    f"Проверил «{task.title}»: нового {len(fresh)}.",
                    kind="text",
                    task_id=task.id,
                )

    async def _download_quiet(self, task: Task, torrent: Torrent) -> None:
        """Скачивание по периодической задаче: без диалога, с уведомлением."""
        try:
            result = await self._download(task, torrent)
        except Exception as exc:  # noqa: BLE001
            log.exception("тихое скачивание не удалось")
            await self._notify_error(task, torrent, str(exc))
            return
        if not result["ok"]:
            await self._notify_error(task, torrent, result["error"])
            return
        text = (
            f"Новая серия по задаче «{task.title}»: скачал и поставил в очередь.\n"
            f"• {torrent.title}\n{describe(torrent)}"
        )
        if result.get("transmission_error"):
            text += f"\nTransmission: {result['transmission_error']}"
        message = Message(
            role="agent", text=text, kind="notice", task_id=task.id,
            payload={"task": task.to_dict(), "torrent": torrent.to_dict()},
        )
        message.actions = self._task_actions(task)
        await self._remember_many(task.session_id, [message])
        await self.events.publish(
            {
                "type": "notification",
                "level": "success",
                "task_id": task.id,
                "title": "Скачано по задаче",
                "text": text,
                "message": message.to_dict(),
            }
        )
        await self.desktop.send("Скачано по задаче", torrent.title[:120])
        await self.push.send(
            "Скачано по задаче",
            f"{task.title}: {torrent.title}"[:200],
        )

    async def _notify_new(self, task: Task, fresh: list[Torrent]) -> None:
        lines = [f"По задаче «{task.title}» появилось новое ({len(fresh)}):"]
        for torrent in fresh[:5]:
            lines.append(f"• {torrent.title}\n   {describe(torrent)}")
        text = "\n".join(lines)
        message = Message(
            role="agent",
            text=text,
            kind="notice",
            task_id=task.id,
            payload={"task": task.to_dict(), "torrents": [t.to_dict() for t in fresh[:5]]},
        )
        for index, torrent in enumerate(fresh[:3], start=1):
            message.actions.append(
                Action(
                    id=f"download-new-{torrent.id}",
                    label=f"Скачать {index}",
                    kind="download_new",
                    payload={"task_id": task.id, "torrent_id": torrent.id},
                    style="primary" if index == 1 else "normal",
                )
            )
        message.actions.extend(self._task_actions(task))
        await self._remember_many(task.session_id, [message])
        await self.events.publish(
            {
                "type": "notification",
                "level": "info",
                "task_id": task.id,
                "title": "Новое по задаче",
                "text": text,
                "message": message.to_dict(),
            }
        )
        await self.desktop.send("Новое по задаче", f"{task.title}: {fresh[0].title}"[:200])
        await self.push.send(
            "Новое по задаче",
            f"{task.title}: {fresh[0].title}"[:200],
        )

    async def _notify_error(self, task: Task, torrent: Torrent, error: str) -> None:
        text = f"Задача «{task.title}»: не смог скачать «{torrent.title}» — {error}"
        message = Message(role="agent", text=text, kind="error", task_id=task.id)
        await self._remember_many(task.session_id, [message])
        await self.events.publish(
            {"type": "notification", "level": "error", "task_id": task.id, "title": "Ошибка", "text": text}
        )
        await self.push.send("Ошибка в задаче", text[:200])

    # ------------------------------------------------------------------
    # Вспомогательные тексты
    # ------------------------------------------------------------------
    def _filter_text(self, task: Task) -> str:
        bits = []
        if task.params.get("quality"):
            bits.append(f"качество: {self._quality_label(str(task.params['quality']))}")
        if task.params.get("voice"):
            bits.append(f"озвучка: {task.params['voice']}")
        if task.params.get("season"):
            bits.append(f"сезон: {task.params['season']}")
        return ("Фильтр — " + ", ".join(bits)) if bits else "Фильтр по качеству и озвучке не задан."

    def _confirm_text(self, task: Task, best: Torrent, alternatives: list[Torrent]) -> str:
        text = (
            f"Нашёл подходящую раздачу по запросу «{task.title}»:\n\n"
            f"• {best.title}\n   {describe(best)}\n\n"
            "Берём в работу?"
        )
        # Сразу видно, куда лягут файлы и хватает ли там места.
        storage = self.transmission.storage_info()
        info = storage.to_dict()
        if storage.ok:
            text += (
                f"\n\nКачать буду на «{info['volume']}» ({info['path']}), "
                f"свободно {info['free_human']} из {info['total_human']}."
            )
        else:
            text += f"\n\n⚠ Внешний диск недоступен: {storage.error}"
        if alternatives:
            text += "\n\nДругие варианты:"
            for index, item in enumerate(alternatives, start=2):
                text += f"\n{index}. {item.title}\n   {describe(item)}"
        return text

    def _choice_text(self, task: Task, dimension: str, values: list[str], count: int) -> str:
        shown = ", ".join(self._quality_label(v) if dimension == "quality" else v for v in values[:8])
        question = (
            "Какое качество берём?" if dimension == "quality" else "Какую озвучку берём?"
        )
        return (
            f"По запросу «{task.title}» нашлось {count} раздач. "
            f"{question}\n\nНа трекере есть: {shown}."
        )

    def _no_match_text(self, task: Task, by_title: list[Torrent], options: dict[str, Any]) -> str:
        asked = []
        if task.params.get("quality"):
            asked.append(f"качество {self._quality_label(str(task.params['quality']))}")
        if task.params.get("voice"):
            asked.append(f"озвучка {task.params['voice']}")
        qualities = ", ".join(self._quality_label(q) for q in options["qualities"][:6]) or "—"
        voices = ", ".join(options["voices"][:6]) or "—"
        return (
            f"По запросу «{task.title}» "
            + ("с параметрами (" + ", ".join(asked) + ") " if asked else "")
            + f"ничего не подошло.\n\n"
            f"Но на трекере есть {options['total']} раздач этого названия:\n"
            f"• качество: {qualities}\n• озвучка: {voices}\n\n"
            "Выберите, что взять, или уточните запрос."
        )

    async def _not_found_text(self, task: Task) -> str:
        base = (
            f"По запросу «{task.title}» на kinozal.me ничего не нашлось.\n\n"
            "Могу сделать задачу периодической: тогда я буду проверять трекер и "
            "сообщу, как только раздача появится."
        )
        try:
            hint = await self.llm.ask_text(
                "Ты помощник торрент-агента. Ответь ОДНИМ коротким предложением "
                "по-русски: что пользователю уточнить в запросе, чтобы поиск на "
                "трекере удался (год, оригинальное название, сезон). Без вступлений.",
                f"Запрос: «{task.title}», параметры: {json.dumps(task.params, ensure_ascii=False)}",
                max_tokens=120,
            )
            hint = (hint or "").strip()
            if hint and len(hint) < 300:
                return base + "\n\n" + hint
        except LLMError:
            pass
        return base

    @staticmethod
    def _human_interval(minutes: int | None) -> str:
        if not minutes:
            return "не задано"
        if minutes % 60 == 0 and minutes >= 60:
            hours = minutes // 60
            word = "час" if hours == 1 else ("часа" if hours in (2, 3, 4) else "часов")
            return f"{hours} {word}"
        word = "минуту" if minutes % 10 == 1 and minutes % 100 != 11 else "минут"
        if minutes % 10 in (2, 3, 4) and minutes % 100 not in (12, 13, 14):
            word = "минуты"
        return f"{minutes} {word}"

    # ------------------------------------------------------------------
    # создание задачи и работа с историей
    # ------------------------------------------------------------------
    async def _task_from_params(
        self,
        session_id: str,
        text: str,
        params: dict[str, Any],
        *,
        kind: str,
        interval: int | None = None,
        auto_download: bool = False,
    ) -> Task:
        clean = {
            key: value
            for key, value in params.items()
            if key in ("title", "original_title", "quality", "voice", "season", "episode_from", "year")
            and value not in (None, "", "null")
        }
        clean.setdefault("seen_ids", [])
        task = Task(
            id=new_id(),
            kind=kind,
            state="searching",
            session_id=session_id,
            user_query=text,
            params=clean,
            interval_minutes=interval,
            auto_download=auto_download,
        )
        if kind == "periodic":
            self._schedule_next(task)
        await self.store.save_task(task)
        return task

    async def _remember(self, session_id: str, message: Message) -> None:
        await self.store.add_message(session_id, message)

    async def _remember_many(self, session_id: str, messages: list[Message]) -> None:
        for message in messages:
            await self.store.add_message(session_id, message)
