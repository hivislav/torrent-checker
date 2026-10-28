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
import re
import time
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
    quality_match,
    voice_match,
    collect_options,
    describe,
    episodes_by_season,
    filter_candidates,
    find_new,
    is_pack,
    rank,
    title_match,
)
from core.mcp_client import KinozalMCP, MCPError
from core.models import (
    Action,
    Message,
    Task,
    Torrent,
    Turn,
    new_id,
    now_iso,
    task_editable,
)
from core.notify import DesktopNotifier, EventBus
from core.push import WebPush
from core.episodes import (
    episode_groups,
    is_episode_pack,
    parse_files,
    pick_episode,
    pick_new_episodes,
)
from core.parsing import extract_qualities, normalize, parse_torrent, torrent_id_from_link
from core.suggest import (
    MIN_SCORE,
    query_variants,
    similar_titles,
    similarity,
    translit,
)
from core.storage import Store
from core.storage_target import StorageUnavailable as TargetUnavailable
from core.storage_target import ensure_target as ensure_dir
from core.transmission import (
    NotEnoughSpace,
    StorageUnavailable,
    Transmission,
    TransmissionBusy,
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
  "screener": true|false|null — экранка: true если её прямо просят («экранку», «камрип»,
              «сойдёт и экранка»), false если просят «не экранку», null если не сказано.
              Экранки (CAM/TS/TC/SCR) по умолчанию не предлагаются — не заполняй без просьбы.
  "intent": "find" | "monitor" | "question"
}

Дополнительно поля:
  "refine": true|false — true, если сообщение является уточнением к предыдущему
            запросу в этом же диалоге («нет, я про 4 сезон», «а если 4К?»,
            «второй вариант», «не то, нужен другой фильм»), а не новой задачей.
  "aliases": массив строк — ДРУГИЕ общеизвестные названия того же фильма/сериала
            (русское ↔ оригинальное, официальный перевод): для «Лост» — 
            ["Остаться в живых", "Lost"]. До 3 штук, только если уверен.
            Не выдумывай: нет уверенности — пустой массив.
  "type": "series" | "movie" | null — сериал или фильм, если это названо
            («сериал», «фильм», «серия», «сезоны») или очевидно из названия.

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


# Одна и та же подсказка на шагах выбора: про кнопку «Подробнее» легко не
# догадаться, а читать про фильм нужно до выбора качества, а не после.
DETAILS_HINT = "Кнопка «Подробнее» покажет жанр, описание и актёров."


PARAM_TITLES = {
    "title": "название",
    "type": "тип",
    "aliases": "другое название",
    "original_title": "оригинальное название",
    "quality": "качество",
    "voice": "озвучка",
    "season": "сезон",
    "episode_from": "серия",
    "year": "год",
    "screener": "экранка",
}

# Параметры запроса, которые попадают в задачу и переживают уточнения.
# Один список на все места (разбор, уточнение, правка задачи) — иначе легко
# забыть ключ в одном из них и потерять уточнение.
PARAM_KEYS = (
    "title",
    "original_title",
    "quality",
    "voice",
    "season",
    "episode_from",
    "year",
    "type",
    "aliases",
    "screener",
)


def _parse_iso(value: str | None) -> datetime | None:
    """«2026-10-18T15:30:42+00:00» → datetime; мусор → None."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _comma(text: str) -> str:
    """«5.74 ГБ» → «5,74 ГБ»: размеры с трекера приходят с точкой."""
    return str(text).replace(".", ",")


def _plural(count: int, one: str, few: str, many: str) -> str:
    """«1 файл», «2 файла», «5 файлов» — без кривых скобок в интерфейсе."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} {one}"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} {few}"
    return f"{count} {many}"


def _word_plural(count: int, one: str, few: str, many: str) -> str:
    """Только слово без числа: «3 дня» → «дня» (для «3 дня назад»)."""
    return _plural(count, one, few, many).split(" ", 1)[-1]


def _shorten(text: str, limit: int) -> str:
    """Длинный список актёров режем с многоточием: в окне он читается как стена."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(",", 1)[0]
    return (cut or value[:limit]).rstrip(" ,") + " …"


def _short_clock() -> str:
    """«19:04» — когда показатель панели действительно измеряли."""
    return time.strftime("%H:%M", time.localtime())


def _age_text(stamp: float | None) -> str:
    """«сегодня», «вчера», «3 дня назад» или дата — возраст раздачи.

    Возраст понятнее точной даты: по нему сразу видно, свежий это релиз или
    раздача, которую залили полгода назад.
    """
    if not stamp:
        return ""
    moment = datetime.fromtimestamp(stamp, tz=timezone.utc)
    days = int((datetime.now(timezone.utc) - moment).total_seconds() // 86400)
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} {_word_plural(days, 'день', 'дня', 'дней')} назад"
    if days < 60:
        weeks = days // 7
        return f"{weeks} {_word_plural(weeks, 'неделю', 'недели', 'недель')} назад"
    return moment.strftime("%d.%m.%Y")


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
        # Подробности раздачи (жанр, актёры, описание) запрашиваются по кнопке,
        # а не при каждом поиске: это ещё один поход через шлюз Cloudflare.
        # Кэш на 10 минут — чтобы повторное нажатие не ходило на трекер.
        self._details_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._details_cache_ttl = 600
        self.scheduler_task: asyncio.Task | None = None
        self.progress_task: asyncio.Task | None = None
        self._tuning_task: asyncio.Task | None = None
        # Состояние последнего чтения очереди: «Transmission занят диском» —
        # это не пустой список, панель должна сказать об этом честно.
        self.downloads_meta: dict[str, Any] = {
            "stale": False,
            "busy": False,
            "error": "",
            "measured_at": "",
        }
        self.progress_tick_seconds = 5
        self.scheduler_tick_seconds = 20
        # Сколько серий можно поставить в очередь без спроса. Больше — это уже
        # «пачка» (у сериалов на трекере бывает 10-40 серий в одной раздаче),
        # и лучше спросить: 20 файлов — это под сотню гигабайт.
        self.auto_episodes_max = 3

    # ------------------------------------------------------------------
    # жизненный цикл
    # ------------------------------------------------------------------
    async def start(self, *, with_scheduler: bool = True) -> None:
        if self._started:
            return
        # Внешний диск может быть не смонтирован (выдернули, идёт проверка
        # fsck, монтирование ещё не вернулось). Это не повод падать на старте:
        # интерфейс должен подняться и честно показать «диск недоступен»,
        # а заработать — как только том вернётся.
        try:
            self.settings.torrents_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("каталог torrent-файлов недоступен на старте: %s", exc)
        self._started = True
        try:
            await self.mcp.start()
        except MCPError as exc:
            log.error("MCP недоступен: %s", exc)
        await self._load_overrides()
        await self.push.prepare()
        if with_scheduler:
            self.scheduler_task = asyncio.create_task(self._scheduler_loop())
            self.progress_task = asyncio.create_task(self._progress_loop())
            log.info("шедулер периодических задач и монитор загрузок запущены")
        # Настройки сессии Transmission (каталог, кэш, потолок скорости) —
        # в фоне: если приложение выключено, это ожидание, а интерфейс стартовать
        # не мешает.
        self._tuning_task = asyncio.create_task(self.transmission.startup_tuning())

    async def stop(self) -> None:
        if self.progress_task:
            self.progress_task.cancel()
            try:
                await self.progress_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.progress_task = None
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
    async def handle_message(
        self, session_id: str, text: str, mode: str = "chat", task_id: str | None = None
    ) -> Turn:
        """Ответ на сообщение. `task_id` — открытая задача (её переписка и есть контекст).

        У каждой задачи своя история: сообщения хранятся с её id, и окно контекста
        собирается только из них. Новый запрос без task_id начинает новую задачу.
        """
        text = (text or "").strip()
        turn = Turn()
        if not text:
            turn.add("Пустой запрос. Напишите, что искать — например «Дюна 1080p».")
            return turn

        # Граница хода: сообщения, созданные ниже, нельзя помечать «отвеченными».
        turn_mark = await self.store.last_row_id()
        user_message_id = await self._remember(
            session_id, Message(role="user", text=text, kind="text", task_id=task_id or "")
        )

        # Ссылка на раздачу — самый быстрый путь: раздача уже известна, искать
        # по названию (и спрашивать модель) не нужно вовсе.
        link_id = torrent_id_from_link(text)
        if link_id:
            await self._start_from_link(session_id, text, link_id, turn)
            await self._tag_turn(user_message_id, turn)
            await self._remember_many(session_id, turn.messages)
            return turn

        # Контекст — переписка именно этой задачи (как отдельный чат).
        current = await self.store.get_task(task_id) if task_id else None
        window = await self._task_window(current.id) if current is not None else []
        window = [item for item in window if item.get("text") != text]
        active = current or await self._active_task(session_id)

        # Задачу, которую уже нельзя править (в работе или закрытую), текст в
        # поле ввода не меняет: это новая задача — то же самое, что кнопка
        # «Новая задача». Иначе качающая задача превращалась в другую (так
        # «Брестская крепость» стала «Ты все видишь»), а закрытую можно было
        # «переоткрыть» одной фразой.
        locked = current is not None and not self._can_edit(current)

        try:
            parsed = await self._parse_query(text, window, active)
        except LLMError as exc:
            parsed = {}
            log.warning("разбор запроса не удался: %s", exc)

        intent = str(parsed.get("intent") or "")
        effective_mode = mode if mode in ("oneshot", "periodic") else (
            "periodic" if intent == "monitor" else "oneshot"
        )
        if locked:
            journal.add(
                "agent",
                "правка нередактируемой задачи: завожу новую",
                {
                    "задача": current.id,
                    "состояние": current.state,
                    "текст": text,
                    "разобрано": parsed,
                },
            )
            turn.add(self._locked_note(current, parsed, text), kind="text")
            turn.start_new = True
            has_title = bool(str(parsed.get("title") or "").strip()) or self._looks_like_title(text)
            current = None
            active = None
            if not has_title:
                # Названия в тексте нет («а можно 4K») — заводить нечего, и
                # просить его второй раз («Не понял, что искать») незачем:
                # просьба уже есть в ответе.
                await self._remember_many(session_id, turn.messages)
                return turn
        elif current is None:
            # Без открытой задачи — начинаем новую (так же работают API и бот).
            active = await self._active_task(session_id, kind=effective_mode)
        else:
            # Внутри задачи новых задач не заводим: всё, что человек пишет,
            # относится к ней. Другое название — это правка задачи, а не
            # новый поиск (для новой задачи есть кнопка «Новая задача»).
            active = current

        # Дополнение к уточнению: сначала ответили списком названий, потом человек
        # добавил слово («возвращение»). Ищем внутри уже найденного, а не с нуля.
        if (
            active is not None
            and active.state == "awaiting_choice"
            and self._is_extra_words(text)
            and await self._narrow_pool(active, text, turn)
        ):
            turn.task = await self.store.get_task(active.id)
            await self._tag_turn(user_message_id, turn)
            await self._remember_many(session_id, turn.messages)
            return turn

        # Уточнение к текущей задаче — правим её, а не заводим новую.
        if current is not None:
            refined = True
            if not self._is_refinement(text, parsed, current):
                journal.add(
                    "agent",
                    "внутри задачи: трактуем как правку задачи",
                    {"задача": current.id, "текст": text, "разобрано": parsed},
                )
        else:
            refined = active is not None and self._is_refinement(text, parsed, active)
        if active is not None and not refined:
            # Не уточнение: если модель повторила старое название, берём
            # название из текста, а не из контекста прошлой задачи. Но если
            # модель дала СВОЁ исправленное название («Ставка на любов» →
            # «Ставка на любовь»), её вариант важнее — не затираем.
            old_title = str((active.params or {}).get("title") or "")
            model_title = str(parsed.get("title") or "").strip()
            repeats_old = not model_title or (
                old_title and normalize(model_title) == normalize(old_title)
            )
            if (
                repeats_old
                and self._looks_like_title(text)
                and not self._text_about_title(text, old_title)
            ):
                parsed = {**parsed, "title": self._clean_title_text(text)}
                journal.add(
                    "agent",
                    "название взято из нового запроса",
                    {"было": old_title, "стало": parsed["title"]},
                )
        if refined:
            journal.add(
                "agent",
                "уточнение к текущей задаче",
                {"текст": text, "было": active.params, "разобрано": parsed},
            )
            await self._refine_task(active, parsed, text, turn)
            turn.task = await self.store.get_task(active.id)
            await self._tag_turn(user_message_id, turn)
            await self._remember_many(session_id, turn.messages)
            return turn
        effective_mode = mode
        if mode not in ("oneshot", "periodic"):
            effective_mode = "periodic" if intent == "monitor" else "oneshot"
            if intent == "question" and not parsed.get("title"):
                reply = await self._chat_answer(text, window)
                turn.add(reply)
                await self._tag_turn(user_message_id, turn)
                await self._remember_many(session_id, turn.messages)
                return turn

        if not parsed.get("title") and self._looks_like_title(text):
            # «Извне» без года и качества — это название, пусть модель его
            # и не выделила: ищем по тому, что человек написал.
            parsed = {**parsed, "title": self._clean_title_text(text)}
            journal.add("agent", "название взято из текста запроса", {"название": parsed["title"]})
        journal.add(
            "agent",
            f"запрос пользователя ({'периодически' if effective_mode == 'periodic' else 'разово'})",
            {"текст": text, "разобрано": parsed},
        )
        if effective_mode == "periodic":
            await self._start_periodic(session_id, text, parsed, turn)
        else:
            await self._start_oneshot(session_id, text, parsed, turn)

        await self._tag_turn(user_message_id, turn)
        if turn.task is not None and turn.messages:
            # Ответ появился — значит, вопрос, который висел, уже отвечен.
            # (toggle_episode не шлёт сообщений: там выбор ещё идёт.)
            try:
                await self.store.resolve_actions(turn.task.id, before_id=turn_mark)
            except Exception as exc:  # noqa: BLE001 - пометка не должна ронять ответ
                log.warning("не удалось пометить вопрос отвеченным: %s", exc)
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
        ttl = max(1, int(getattr(self.settings, "active_task_ttl_minutes", 30) or 30))
        deadline = datetime.now(timezone.utc) - timedelta(minutes=ttl)
        for task in tasks:
            if task.session_id != session_id or task.state not in self.RESUME_STATES:
                continue
            if kind and task.kind != kind:
                continue
            # Старая незакрытая задача не должна ловить новые запросы: у неё
            # уже своя история и свои кнопки.
            stamp = _parse_iso(task.updated_at) or _parse_iso(task.created_at)
            if stamp is not None and stamp < deadline:
                continue
            return task
        return None

    # Слова, которые не могут быть названием: это команды и уточнения.
    REFINE_STOPWORDS = {
        "да", "нет", "ок", "окей", "спасибо", "другой", "другое", "другая",
        "другие", "это", "не", "то", "отмена", "всё", "все", "ещё", "еще",
        "можно", "а", "и", "или", "ничего", "первый", "второй", "третий",
    }
    REFINE_KEYWORDS = (
        "качеств", "озвучк", "сезон", "серия", "серии", "серий", "год",
        "сериал", "фильм", "кино", "раздач", "торрент", "скач", "найди",
        "поищи", "покажи", "хочу", "нужен", "нужна", "надо", "возьми",
        "реалити", "шоу", "ток-шоу", "документал", "мультсериал",
    )
    # Слова-жанры: «реалити шоу», «сериал», «фильм» — это уточнение типа,
    # а не новое название. Срабатывают, только если ВСЁ сообщение из них.
    GENRE_WORDS = {
        "реалити", "шоу", "ток", "ток-шоу", "тв", "тв-шоу", "сериал", "сериала",
        "сериалы", "сериалов", "фильм", "фильма", "фильмы", "кино", "мультфильм",
        "мультсериал", "документальный", "документальное", "документал",
        "док", "дорама", "дорамы", "аниме",
    }

    def _genre_only(self, text: str) -> bool:
        """Сообщение состоит только из слов-жанров («реалити шоу», «сериал»)."""
        cleaned = self._clean_title_text(text).lower()
        tokens = [token for token in cleaned.split() if token]
        return bool(tokens) and all(token in self.GENRE_WORDS for token in tokens)

    def _looks_like_title(self, text: str) -> bool:
        """Текст похож на название, а не на уточнение («4K», «7 серия», «нет»)."""
        cleaned = re.sub(r"[«»\"'.,!?:;()\[\]]+", " ", str(text or "")).strip()
        if not cleaned:
            return False
        low = cleaned.lower()
        if re.search(r"\d", low):
            return False
        if any(word in low for word in self.REFINE_KEYWORDS):
            return False
        tokens = [token for token in low.split() if token]
        if not tokens or len(tokens) > 6:
            return False
        if any(token in self.REFINE_STOPWORDS for token in tokens):
            return False
        return any(len(token) > 2 for token in tokens)

    @staticmethod
    def _owns_live_download(task: Task) -> bool:
        """Задача прямо сейчас качает раздачу — её нельзя переделывать в другую.

        В памяти задачи лежит хеш раздачи и последний прогресс. Пока прогресс не
        дошёл до конца, раздача живёт в Transmission, и всё, что мы сделаем с
        задачей (переименуем, превратим в периодическую), расходится с ней.
        """
        params = task.params or {}
        if not str(params.get("transmission_hash") or ""):
            return False
        progress = params.get("progress") or {}
        return not progress.get("done")

    def _can_edit(self, task: Task) -> bool:
        """Задачу ещё можно править текстом (правило — core.models.task_editable).

        Пока идёт поиск, выбор качества/озвучки/серий или подтверждение — можно.
        Периодическую в мониторинге — тоже: так человек меняет условия проверки.
        Скачивание и закрытые задачи — только просмотр: текст заводит новую.
        """
        return task_editable(task)

    # Как назвать состояние в объяснении «правки в неё не вношу».
    STATE_WORDS = {
        "downloading": "уже качается",
        "monitoring": "уже в работе (мониторинг)",
        "done": "уже скачана",
        "cancelled": "отменена",
        "failed": "завершилась ошибкой",
        "not_found": "закрыта: ничего не нашлось",
    }

    def _locked_note(self, task: Task, parsed: dict[str, Any], text: str) -> str:
        """Объясняет, почему текст открыл новую задачу, а не правку."""
        title = str((task.params or {}).get("title") or task.user_query or "")
        if self._owns_live_download(task):
            percent = ((task.params or {}).get("progress") or {}).get("percent")
            head = f"Сейчас качается «{title}»"
            if percent is not None:
                head += f" ({percent} %)"
        else:
            head = f"Задача «{title}» {self.STATE_WORDS.get(task.state, 'уже не правится')}"
        # Название новой задачи берём из запроса, если оно там есть: «а можно 4K»
        # названием не является, и обещать «начинаю новую: „а можно 4K“» нельзя.
        new_title = str(parsed.get("title") or "").strip()
        if not new_title and self._looks_like_title(text):
            new_title = self._clean_title_text(text)
        tail = (
            f"Начинаю новую: «{new_title}»."
            if new_title
            else "Чтобы начать новую, напишите её название — например «Тед Лассо 1080p»."
        )
        return f"{head} — эту задачу не трогаю и не правлю.\n{tail}"

    def _is_refinement(self, text: str, parsed: dict[str, Any], active: Task) -> bool:
        """Это уточнение к активной задаче, а не новая задача?"""
        new_title = str(parsed.get("title") or "").strip()
        old_title = str((active.params or {}).get("title") or "").strip()
        genre_only = self._genre_only(text)
        # Другое явное название — всегда новая задача. Флаг модели здесь не
        # главный: она склонна считать уточнением любой следующий запрос.
        # Но «реалити шоу» названием не считается — это жанр к текущей задаче.
        if (
            new_title
            and old_title
            and normalize(new_title) != normalize(old_title)
            and not genre_only
        ):
            return False
        plain_title = self._looks_like_title(text)
        if not new_title:
            # Модель не распознала название, а текст на него похож — это новый
            # поиск, а не «что поправить» в старой задаче («Извне»).
            return not plain_title
        if normalize(new_title) == normalize(old_title) and plain_title:
            # Модель могла просто повторить текущую задачу (и приписать к ней
            # параметры из контекста). Если человек написал другое — это новый
            # поиск: «Извне» после «Человека-паука» ищет «Извне».
            if not self._text_about_title(text, old_title):
                return False
        parsed_params = [
            key
            for key in ("original_title", "quality", "voice", "season", "episode_from",
                        "year", "type")
            if parsed.get(key) not in (None, "", "null")
        ]
        if parsed_params:
            return True
        return normalize(new_title) == normalize(old_title)

    @staticmethod
    def _clean_title_text(text: str) -> str:
        """Убирает пунктуацию по краям: «Извне!» → «Извне»."""
        cleaned = re.sub(r"[«»\"'.,!?:;()\[\]]+", " ", str(text or ""))
        return re.sub(r"\s+", " ", cleaned).strip()

    def _is_extra_words(self, text: str) -> bool:
        """Похоже на уточняющие слова к названию («возвращение», «нет пути домой»)."""
        if self._genre_only(text):
            return False
        cleaned = self._clean_title_text(text)
        if not cleaned or re.search(r"\d", cleaned):
            return False
        tokens = [token for token in cleaned.lower().split() if token]
        if not tokens or len(tokens) > 3:
            return False
        if any(word in cleaned.lower() for word in self.REFINE_KEYWORDS):
            return False
        return any(token not in self.REFINE_STOPWORDS for token in tokens)

    async def _narrow_pool(self, task: Task, text: str, turn: Turn) -> bool:
        """Сужает найденное по новым словам, не выходя на трекер заново.

        «человек паук» дало 80 раздач с разными фильмами; человек пишет
        «возвращение» — берём только те, где это слово есть в названии.
        """
        pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        if not pool:
            return False
        tokens = self._significant_tokens(text)
        if not tokens:
            return False
        narrowed = [
            item
            for item in pool
            if all(token in normalize(item.title) for token in tokens)
        ]
        old_title = str((task.params or {}).get("title") or task.title)
        if not narrowed:
            # В уже найденном такого нет — спрашиваем трекер словами вместе:
            # «человек паук» + «возвращение» находит «Человек-паук: Возвращение домой».
            combined_query = f"{old_title} {text}".strip()
            try:
                rows = await self.mcp.search_pages(combined_query, pages=1)
            except MCPError as exc:
                journal.add(
                    "agent", "поиск уточнения не удался",
                    {"запрос": combined_query, "ошибка": str(exc)}, level="warn",
                )
                rows = []
            narrowed = [
                torrent
                for torrent in (parse_torrent(raw) for raw in rows)
                if torrent.id and all(token in normalize(torrent.title) for token in tokens)
            ]
            if narrowed:
                journal.add(
                    "agent",
                    "уточнение искали на трекере вместе с названием",
                    {"запрос": combined_query, "найдено": len(narrowed)},
                )
        if not narrowed:
            return False
        if narrowed == pool:
            # Ничего не сузилось — считаем это новым запросом, а не уточнением.
            return False
        journal.add(
            "agent",
            "сужение по словам запроса",
            {"добавлено": text, "было": len(pool), "осталось": len(narrowed),
             "название": old_title},
        )
        task.params = {**(task.params or {}), "title": f"{old_title} {text}".strip()}
        task.options = {**(task.options or {}), "pool": [item.to_dict() for item in narrowed],
                        "suggestions": []}
        task.state = "searching"
        task.user_query = f"{task.user_query} | {text}"[:400]
        await self.store.save_task(task)
        turn.add(
            f"Сузил поиск по «{task.title}»: подходит "
            f"{_plural(len(narrowed), 'раздача', 'раздачи', 'раздач')}.",
            kind="text",
            task_id=task.id,
        )
        await self._selection_step(task, turn)
        return True

    def _text_about_title(self, text: str, title: str) -> bool:
        """Текст относится к этому названию? («Тед Лассо» ⊂ «Тед Лассо»)."""
        wanted = [token for token in str(text or "").lower().split() if token]
        haystack = normalize(title)
        return bool(wanted) and all(token in haystack for token in wanted)

    async def _refine_task(
        self, task: Task, parsed: dict[str, Any], text: str, turn: Turn
    ) -> None:
        """Применяет уточнение к задаче и продолжает с того же места."""
        before = dict(task.params or {})
        merged = dict(before)
        changed: dict[str, Any] = {}
        for key in PARAM_KEYS:
            value = parsed.get(key)
            if value in (None, "", "null", []):
                continue
            if merged.get(key) != value:
                merged[key] = value
                changed[key] = value

        parsed_fields = {
            key: value
            for key, value in (parsed or {}).items()
            if key in PARAM_KEYS and value not in (None, "", "null", [])
        }
        # Интервал и режим — это не «параметры поиска», а настройки задачи:
        # «проверяй раз в сутки» и «только сообщай» должны менять именно их.
        interval_change = parsed.get("interval_minutes")
        auto_change = parsed.get("auto_download")
        has_task_settings = bool(
            interval_change not in (None, "", "null")
            or isinstance(auto_change, bool)
        )
        if task.kind == "periodic" and (changed or parsed_fields or has_task_settings):
            # У периодической задачи уточнение — это правка задачи, а не разовый
            # поиск: иначе «только 4к» уводило задачу в «не найдено» и она
            # переставала следить за трекером.
            await self._refine_periodic(task, changed, merged, text, before, turn, parsed)
            return
        if not changed and parsed_fields:
            # В сообщении были параметры, просто уже такие же — показываем
            # текущий шаг, а не просим «уточнить, что поправить».
            task.state = "searching"
            await self.store.save_task(task)
            turn.add(
                "Уже так и настроено — вот что сейчас по задаче.",
                kind="text",
                task_id=task.id,
            )
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

        shown: dict[str, Any] = {}
        for key, value in changed.items():
            if key == "type":
                shown[key] = "сериал" if value == "series" else "фильм" if value == "movie" else value
            elif key == "aliases" and isinstance(value, list):
                shown[key] = ", ".join(str(item) for item in value)
            else:
                shown[key] = value
        pool = [] if title_changed else [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        if pool:
            # Название не менялось — ищем заново не надо, перебираем то, что уже есть.
            summary = ", ".join(
                f"{PARAM_TITLES.get(key, key)}: {value}" for key, value in shown.items()
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
            f"{PARAM_TITLES.get(key, key)}: {value}" for key, value in shown.items()
        )
        turn.add(f"Уточнил: {summary}. Ищу заново.", kind="text", task_id=task.id)
        await self._selection_step(task, turn)

    async def _refine_periodic(
        self,
        task: Task,
        changed: dict[str, Any],
        merged: dict[str, Any],
        text: str,
        before: dict[str, Any],
        turn: Turn,
        parsed: dict[str, Any] | None = None,
    ) -> None:
        """Уточнение внутри периодической задачи: меняем фильтры мониторинга.

        Задача остаётся в работе и продолжает проверять трекер с новыми
        условиями: «только 4к» — это уточнение качества, а не новый разовый
        поиск. Проверку не запускаем, чтобы ответ был мгновенным (трекер с
        Cloudflare отвечает не всегда быстро) — рядом кнопка «Проверить сейчас».
        """
        was_state = task.state
        if "title" in changed or "original_title" in changed:
            # Другой сериал — старая базовая линия («в 3 сезоне уже 24 серии»)
            # к нему не относится, иначе задача будет ждать несуществующую серию.
            for key in ("baseline_episodes", "baseline_ts", "next_episode", "seen_ids"):
                task.params.pop(key, None)
            merged = {key: value for key, value in merged.items()
                      if key not in ("baseline_episodes", "baseline_ts", "next_episode", "seen_ids")}
        task.params = merged
        task.user_query = f"{task.user_query} | {text}"[:400]
        task.last_error = ""

        # Настройки самой задачи: период проверки и режим («качать сразу» /
        # «только сообщать»). Это не параметры поиска, поэтому в params они не
        # живут — но именно их чаще всего и правят словами.
        settings_changed: list[str] = []
        parsed = parsed or {}
        interval_value = parsed.get("interval_minutes")
        if interval_value not in (None, "", "null"):
            try:
                interval = max(self.settings.min_interval_minutes, int(interval_value))
            except (TypeError, ValueError):
                interval = None
            if interval and interval != task.interval_minutes:
                task.interval_minutes = interval
                task.next_check_at = None
                if task.state == "monitoring":
                    self._schedule_next(task)
                settings_changed.append(f"проверка — {self._human_interval(interval)}")
        auto_value = parsed.get("auto_download")
        if isinstance(auto_value, bool) and auto_value != task.auto_download:
            task.auto_download = auto_value
            settings_changed.append(
                "качаю сразу" if auto_value else "только сообщаю"
            )

        journal.add(
            "agent",
            "уточнение периодической задачи",
            {
                "было": before,
                "стало": merged,
                "изменено": changed,
                "настройки": settings_changed or None,
                "состояние": was_state,
            },
        )

        if not changed and not settings_changed:
            # Уже так и настроено — показываем, что сейчас по задаче, и не
            # трогаем состояние.
            task.state = "monitoring" if was_state in ("monitoring", "failed", "not_found") else was_state
            await self.store.save_task(task)
            lines = ["Уже так и настроено — вот что сейчас по задаче."]
            watch = self._watch_text(task)
            if watch:
                lines.append(f"• Ждём: {watch}")
            next_at = _parse_iso(task.next_check_at)
            if next_at is not None:
                lines.append(f"• Следующая проверка {self._clock(next_at)}")
            message = turn.add(
                "\n".join(lines), kind="task", task_id=task.id,
                payload={"task": task.to_dict()},
            )
            message.actions = self._task_actions(task)
            return

        if was_state == "awaiting_confirm":
            # Задача ещё не взята в работу — показываем карточку заново.
            task.options = {**(task.options or {}), "selected_episodes": []}
            await self.store.save_task(task)
            summary = ", ".join(
                f"{PARAM_TITLES.get(key, key)}: {value}" for key, value in changed.items()
            )
            turn.add(f"Уточнил: {summary}.", kind="text", task_id=task.id)
            try:
                torrents = await self._search(task.params)
            except MCPError as exc:
                turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
                return
            task.options = {**(task.options or {}), "pool": [t.to_dict() for t in torrents[:80]]}
            await self._present_periodic(task, torrents, turn)
            return

        # Задача уже в работе (или упала из-за трекера): остаётся в работе.
        task.state = "monitoring"
        task.params.pop("pack_notified", None)
        if not task.interval_minutes:
            task.interval_minutes = self.settings.default_interval_minutes
        if _parse_iso(task.next_check_at) is None:
            self._schedule_next(task)
        await self.store.save_task(task)

        summary = ", ".join(
            f"{PARAM_TITLES.get(key, key)}: {value}" for key, value in changed.items()
        )
        if settings_changed:
            summary = ", ".join(filter(None, [summary, *settings_changed]))
        mode = "и сразу качать" if task.auto_download else "и сообщать о находках"
        lines = [
            f"Уточнил: {summary}.",
            f"Задача «{task.title}» продолжает следить "
            f"{self._human_interval(task.interval_minutes)} {mode}.",
        ]
        watch = self._watch_text(task)
        if watch:
            lines.append(f"• Ждём: {watch}")
        next_at = _parse_iso(task.next_check_at)
        if next_at is not None:
            lines.append(f"• Следующая проверка {self._clock(next_at)}")
        lines.append("Проверить сразу — кнопкой ниже.")
        message = turn.add(
            "\n".join(lines), kind="task", task_id=task.id, payload={"task": task.to_dict()}
        )
        message.actions = self._task_actions(task)
        await self.events.publish(
            {
                "type": "task_updated",
                "task_id": task.id,
                "title": task.title,
                "changed": changed,
            }
        )

    async def handle_action(self, session_id: str, kind: str, payload: dict[str, Any]) -> Turn:
        turn = Turn()
        # Граница хода: своё же новое сообщение нельзя помечать «отвеченным».
        turn_mark = await self.store.last_row_id()
        task_id = str(payload.get("task_id") or "")
        task = await self.store.get_task(task_id) if task_id else None
        turn.task = task

        CONT_SUFFIXES = (
            "choose", "choose_episodes", "start_download", "pick_torrent", "toggle_episode",
            "periodic_pick", "periodic_back", "relax_filters",
            "download_selected", "download_all", "choose_episode", "confirm_download",
            "download_new", "periodic_auto", "periodic_notify", "make_periodic",
        )
        if task is not None and task.state == "cancelled" and (
            kind == "choose" or kind in CONT_SUFFIXES
        ):
            turn.add(
                f"Задача «{task.title}» уже закрыта — напишите запрос заново, "
                "например в поле ввода.",
                kind="error",
                task_id=task.id,
            )
            await self._remember_many(session_id, turn.messages)
            return turn

        if kind == "cancel" and task is None:
            # Отмена подтверждения (например, удаления) — задачи тут может не быть.
            await self._reset_context(session_id)
            turn.add("Отменено, ничего не трогаю.")
        elif kind == "cancel" and task:
            note = (await self.cancel_task(task.id) or task).last_result
            await self._reset_context(session_id)
            task = await self.store.get_task(task.id) or task
            text = f"Задача «{task.title}» отменена."
            if note:
                text += f"\n{note[0].upper()}{note[1:]}."
            turn.add(text, kind="task", task_id=task.id, payload={"task": task.to_dict()})
        elif kind == "choose" and task and str(payload.get("dimension") or "") == "title":
            await self._apply_title(task, str(payload.get("value") or ""), turn)
        elif kind == "noop" and task:
            turn.add(
                "Оставил запрос как есть. Если появится — найдётся и так.",
                kind="text",
                task_id=task.id,
            )
        elif kind == "choose" and task:
            dimension = str(payload.get("dimension") or "")
            value = str(payload.get("value") or "")
            if dimension and value:
                task.params[dimension] = value
            elif dimension:
                # Пустое значение — «без учёта этого фильтра».
                task.params.pop(dimension, None)
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
        elif kind == "periodic_pick" and task:
            variant = str(payload.get("variant") or "strict")
            if variant.startswith("near:"):
                wanted_id = variant.split(":", 1)[1]
                pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
                chosen = next((item for item in pool if item.id == wanted_id), None)
                if chosen is None:
                    turn.add(
                        "Не нашёл этот вариант — начните задачу заново.",
                        kind="error",
                        task_id=task.id,
                    )
                    return
                episode_from = task.params.get("episode_from")
                params: dict[str, Any] = {
                    "title": (chosen.series_title or chosen.title or "").strip(),
                    "type": "series" if (chosen.seasons or chosen.episodes) else "movie",
                    "seen_ids": [],
                }
                if str(chosen.year).isdigit():
                    params["year"] = int(chosen.year)
                if chosen.qualities:
                    params["quality"] = chosen.qualities[0]
                if chosen.voices:
                    params["voice"] = chosen.voices[0]
                if chosen.season:
                    params["season"] = chosen.season
                if episode_from not in (None, ""):
                    params["episode_from"] = episode_from
                task.params = params
                task.candidate = chosen.to_dict()
                journal.add(
                    "agent",
                    "периодическая задача: выбран похожий вариант",
                    {"раздача": chosen.id, "название": chosen.title, "условия": params},
                )
            else:
                journal.add("agent", "периодическая задача: свои условия",
                            {"название": task.title, "условия": task.params})
            await self.store.save_task(task)
            await self._periodic_summary(task, turn)
        elif kind == "periodic_back" and task:
            pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
            if not pool:
                try:
                    pool = await self._search(task.params)
                except MCPError as exc:
                    turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
                    return
            await self._present_periodic(task, pool, turn)
        elif kind == "relax_filters" and task:
            drop = [str(item) for item in (payload.get("drop") or []) if item]
            for key in drop:
                task.params.pop(key, None)
            new_title = str(payload.get("title") or "").strip()
            if new_title:
                task.params["title"] = new_title
                task.params.pop("original_title", None)
            journal.add("agent", "периодическая задача: ослаблены условия",
                        {"убран": drop, "осталось": task.params})
            task.options = {**(task.options or {}), "suggestions": []}
            await self.store.save_task(task)
            pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
            if not pool:
                try:
                    pool = await self._search(task.params)
                except MCPError as exc:
                    turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
                    return
            turn.add(
                f"Ослабил условия: следим {self._filters_text(task.params)}.",
                kind="text",
                task_id=task.id,
            )
            await self._present_periodic(task, pool, turn)
        elif kind == "make_periodic":
            # Задача не создаёт новую задачу: та же задача становится
            # периодической (kind меняется на месте). Иначе одна история
            # плодила бы двойников в картотеке.
            source = task
            if source is None:
                turn.add("Задача не найдена.", kind="error")
            else:
                interval = max(
                    self.settings.min_interval_minutes,
                    int(payload.get("interval_minutes") or self.settings.default_interval_minutes),
                )
                source.kind = "periodic"
                source.interval_minutes = interval
                source.auto_download = bool(payload.get("auto_download", False))
                source.state = "searching"
                await self.store.save_task(source)
                journal.add(
                    "agent",
                    "задача переведена в периодические",
                    {"задача": source.id, "название": source.title, "интервал": interval,
                     "автоскачивание": source.auto_download},
                )
                try:
                    torrents, suggestions = await self._search_with_suggestions(
                        source.params, original=source.user_query
                    )
                except MCPError as exc:
                    source.state = "failed"
                    source.last_error = str(exc)
                    await self.store.save_task(source)
                    turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error",
                             task_id=source.id)
                    return
                source.options = {**(source.options or {}), "suggestions": suggestions}
                await self._present_periodic(source, torrents, turn)
        elif kind in ("periodic_auto", "periodic_notify") and task:
            task.auto_download = kind == "periodic_auto"
            self._record_start_baseline(task)
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
        elif kind == "download_new" and task:
            torrent_id = str(payload.get("torrent_id") or "")
            pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
            torrent = next((t for t in pool if t.id == torrent_id), None)
            if torrent is None:
                turn.add("Эта раздача больше не в списке — дождитесь следующей проверки.", kind="error")
            else:
                # Нажатие «Скачать» в уведомлении — это и есть подтверждение.
                # Качает отдельная разовая задача: мониторинг остаётся
                # мониторингом, а прогресс виден в «В работе».
                await self._download_quiet(task, torrent)
                turn.add(
                    f"Поставил качать «{torrent.title}» отдельной задачей — она в работе.",
                    kind="text",
                    task_id=task.id,
                )
        elif kind == "start_download" and task:
            await self._start_download(task, turn,
                                       periodic_context=task.kind == "periodic")
        elif kind == "pick_season" and task:
            await self._pick_season(task, payload, turn)
        elif kind == "choose_episodes" and task:
            groups = [
                group for group in (task.options or {}).get("episode_groups") or []
                if group.get("episode") is not None
            ]
            torrent = Torrent.from_dict(task.candidate or {})
            if not groups or not torrent.id:
                turn.add(
                    "Серий в этой раздаче я не вижу — начните задачу заново.",
                    kind="error",
                    task_id=task.id,
                )
            else:
                path_str = (task.options or {}).get("torrent_path") or ""
                await self._show_episode_picker(
                    task, torrent, groups, Path(path_str) if path_str else None, turn
                )
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
        elif kind == "toggle_episode" and task:
            # Сам выбор переключает клиент; здесь только фиксируем состояние,
            # чтобы оно пережило перезагрузку страницы. Серия — это пара
            # (сезон, серия): в пачке на несколько сезонов «Серия 1» без
            # сезона означала бы четыре разных файла.
            selected = {
                (item["season"], item["episode"])
                for item in self._as_pairs((task.options or {}).get("selected_episodes"))
            }
            key = self._pair_key(payload.get("season"), payload.get("episode"))
            if key[1]:
                selected.symmetric_difference_update({key})
            ordered = sorted(selected, key=lambda item: (item[0] or 0, item[1]))
            task.options = {
                **(task.options or {}),
                "selected_episodes": [
                    {"season": season, "episode": episode} for season, episode in ordered
                ],
            }
            await self.store.save_task(task)
        elif kind == "download_selected" and task:
            raw = payload.get("episodes")
            if not raw:
                raw = (task.options or {}).get("selected_episodes") or []
            pairs = self._as_pairs(raw)
            if not pairs:
                turn.add(
                    "Отметьте серии кнопками выше — можно несколько.",
                    kind="text",
                    task_id=task.id,
                )
            else:
                plan = self._build_episodes_plan(task, pairs)
                if plan is None:
                    turn.add(
                        "Не нашёл файлов этих серий в раздаче — выберите заново.",
                        kind="error",
                        task_id=task.id,
                    )
                else:
                    task.options = {
                        **(task.options or {}),
                        "plan": plan,
                        "selected_episodes": plan["selected_episodes"],
                    }
                    await self.store.save_task(task)
                    await self._start_download(task, turn)
        elif kind == "download_all" and task:
            season_filter = payload.get("season")
            groups = [
                group for group in (task.options or {}).get("episode_groups") or []
                if group.get("episode") is not None
            ]
            season_pairs = [
                self._pair_key(group.get("season"), group.get("episode"))
                for group in groups
            ]
            whole_season = None
            if season_filter not in (None, ""):
                try:
                    wanted_season = int(season_filter)
                except (TypeError, ValueError):
                    wanted_season = None
                if wanted_season is not None:
                    season_pairs = [pair for pair in season_pairs if pair[0] == wanted_season]
                    whole_season = wanted_season
            plan = self._build_episodes_plan(
                task, [{"season": season, "episode": episode} for season, episode in season_pairs]
            )
            if plan is None:
                # Сезон не разобрали — берём всю раздачу, как раньше.
                entries = self._entries_from_task(task)
                plan = self._plan_files(task, entries)
            plan = {
                **plan,
                # `wanted` НЕ обнуляем: «весь сезон» — это файлы выбранного
                # сезона. Раньше здесь стояло wanted=None, и Transmission ставил
                # всю раздачу (в пачке 1-10 сезонов это 52 ГБ вместо 5).
                "kind": "all",
                "size": plan.get("size") or plan.get("total_size"),
                "episode": None,
                "names": plan.get("names") or [],
                "season": whole_season if whole_season is not None else plan.get("season"),
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
            turn = await self.request_delete(
                str(payload.get("torrent_id") or ""),
                str(payload.get("hash") or ""),
            )
        elif kind == "confirm_delete":
            turn = await self.perform_delete(
                str(payload.get("torrent_id") or ""),
                str(payload.get("hash") or ""),
            )
        elif kind == "retry_transmission" and task:
            await self._retry_transmission(task, turn)
        elif kind == "check_now" and task:
            await self.check_task(task, manual=True, turn=turn)
        elif kind == "forget_downloads":
            await self.store.set_setting("__noop", "")
            turn.add("Готово.")
        else:
            turn.add("Не понял действие. Попробуйте ещё раз.", kind="error")

        if turn.task is not None and turn.messages:
            # Ответ появился — значит, вопрос, который висел, уже отвечен.
            # Своё же новое сообщение не трогаем: у него кнопки ещё не нажимали.
            try:
                await self.store.resolve_actions(turn.task.id, before_id=turn_mark)
            except Exception as exc:  # noqa: BLE001 - пометка не должна ронять ответ
                log.warning("не удалось пометить вопрос отвеченным: %s", exc)
        await self._remember_many(session_id, turn.messages)
        return turn

    # ------------------------------------------------------------------
    # Задачи: чтение и отмена
    # ------------------------------------------------------------------
    async def list_tasks(self, include_finished: bool = True, limit: int = 500) -> list[dict[str, Any]]:
        tasks = await self.store.list_tasks(limit=limit)
        if not include_finished:
            tasks = [task for task in tasks if task.is_active]
        result: list[dict[str, Any]] = []
        for task in tasks:
            data = task.to_dict()
            # Что именно отслеживает задача: фильтр, какую серию ждём, не ждёт
            # ли повтора скачивание. Это ответ на вопрос «а он вообще следит
            # за тем, чем надо?».
            data["watch"] = self._watch_text(task)
            # Последняя проверка видна в карточке задачи: «проверял 12:40 —
            # 0 новых». Без этого непонятно, работает мониторинг или нет.
            if task.checks_done:
                checks = await self.store.list_checks(task.id, limit=1)
                if checks:
                    last = checks[0]
                    data["last_check"] = {
                        "at": last.get("created_at"),
                        "found": int(last.get("found") or 0),
                        "new": int(last.get("new_count") or 0),
                        "message": str(last.get("message") or ""),
                    }
            result.append(data)
        return result

    async def get_task(self, task_id: str) -> Task | None:
        return await self.store.get_task(task_id)

    async def _cancel_download(self, task: Task) -> str:
        """Отмена задачи отменяет и скачивание.

        «Отменить» должно убрать раздачу из очереди Transmission — иначе торрент
        продолжает качаться сам по себе, занимать место и висеть в списке.
        Уже докачанные файлы не трогаем: они попадают в «Скачано на диск» и
        остаются у человека.
        """
        torrent_hash = str((task.params or {}).get("transmission_hash") or "")
        torrent_id = str((task.candidate or {}).get("id") or "")
        if not torrent_hash and not torrent_id:
            return ""
        progress: dict[str, Any] = {}
        if torrent_hash:
            try:
                progress = ((await self.transmission.progress([torrent_hash])) or {}).get(torrent_hash) or {}
            except Exception:  # noqa: BLE001 - отмена не должна падать
                progress = {}
        if progress.get("done"):
            await self.store.mark_downloads_done([torrent_hash])
            return "раздача уже докачана — файлы остались в «Скачано»"

        record = await self._find_delete_target(torrent_id, torrent_hash)
        if record is None:
            return ""
        record_id = str(record.get("torrent_id") or torrent_id or "")
        error, result = await self._drop_torrent(record_id, torrent_hash, record)
        if error or result is None:
            return f"скачивание убрать не удалось: {error or 'неизвестная причина'}"
        if record_id:
            await self.store.drop_download(record_id)
        journal.add(
            "agent",
            f"отмена задачи: скачивание убрано ({record.get('title')})",
            {"файлов удалено": len(result.deleted), "освобождено": result.summary(),
             "ошибки": result.errors or None},
            level="error" if result.errors else "info",
        )
        note = "раздача убрана из очереди"
        if result.deleted:
            note += f", {result.summary()}"
        if result.errors:
            note += "; часть файлов не тронута"
        return note

    async def cancel_task(self, task_id: str) -> Task | None:
        task = await self.store.get_task(task_id)
        if task is None:
            return None
        was_state = task.state
        note = ""
        if was_state == "downloading" or (task.params or {}).get("transmission_hash"):
            note = await self._cancel_download(task)
        task.state = "cancelled"
        task.next_check_at = None
        if note:
            task.last_result = note
        await self.store.save_task(task)
        # Отмена — граница истории: следующий запрос не должен тянуть
        # в контекст переписку о брошенной задаче.
        await self._reset_context(task.session_id)
        await self.events.publish(
            {"type": "task_cancelled", "task_id": task.id, "title": task.title}
        )
        return task

    async def history(self, session_id: str, limit: int = 80) -> list[dict[str, Any]]:
        return await self.store.history(session_id, limit)

    async def history_for_task(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Переписка одной задачи — то, что видно при переключении на неё."""
        return await self.store.history_for_task(task_id, limit)

    async def downloads(self, limit: int = 50) -> list[dict[str, Any]]:
        """Что лежит на диске: наши записи ПЛЮС всё, что есть в очереди Transmission.

        Второе важно: торрент могли добавить прямо в Transmission (или запись
        потерялась), и он всё равно должен быть виден в карточке — иначе список
        расходится с реальностью.

        С коротким кэшем (3 с): картотека обновляется каждые 5 секунд, а этот
        вызов — два запроса в Transmission. Кэш не даёт им копиться, если RPC
        на секунду задумался.
        """
        cached = getattr(self, "_downloads_cache", None)
        if cached and time.monotonic() - cached[0] < 3 and cached[2] == limit:
            return [dict(item) for item in cached[1]]
        records = await self.store.list_downloads(limit)
        for row in records:
            raw = row.get("episodes") or "[]"
            try:
                loaded = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                loaded = []
            row["episodes"] = [
                item for item in (loaded or []) if isinstance(item, dict) and item.get("episode")
            ]
        try:
            queue = await self.transmission.list_torrents(limit=200)
        except TransmissionBusy as exc:
            # Панель спрашивает очередь каждые несколько секунд: во время
            # записи большого файла это нормальный ответ, а не ошибка.
            log.debug("очередь отложена: %s", exc)
            queue = []
            self.downloads_meta = {
                "stale": True,
                "busy": True,
                "error": str(exc),
                "measured_at": _short_clock(),
            }
        except TransmissionError as exc:
            log.warning("не удалось прочитать очередь Transmission: %s", exc)
            queue = []
            self.downloads_meta = {
                "stale": True,
                "busy": False,
                "error": str(exc),
                "measured_at": _short_clock(),
            }
        else:
            self.downloads_meta = {"stale": False, "busy": False, "error": "", "measured_at": _short_clock()}

        hashes = [str(row.get("transmission_hash") or "") for row in records]
        hashes += [str(item.get("hash") or "") for item in queue]
        progress = await self.transmission.progress([item for item in hashes if item]) or {}

        # «Скачано на диск» — это то, что уже докачано. Пока Transmission не
        # сказал «готово», раздача живёт только в «В работе» (там прогресс).
        finished = {
            key for key, state in progress.items() if state and state.get("done")
        }
        if finished:
            await self.store.mark_downloads_done(sorted(finished))

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in records:
            torrent_hash = str(row.get("transmission_hash") or "")
            row["source"] = "agent"
            row["hash"] = torrent_hash
            row["progress"] = progress.get(torrent_hash)
            is_done = bool(row.get("done_at")) or torrent_hash in finished
            if not is_done:
                # Ещё качается (или непонятно) — в «Скачано» рано.
                seen.add(torrent_hash)
                continue
            row["done_at"] = row.get("done_at") or now_iso()
            result.append(row)
            if torrent_hash:
                seen.add(torrent_hash)

        for item in queue:
            torrent_hash = str(item.get("hash") or "")
            if torrent_hash in seen:
                continue
            state = progress.get(torrent_hash) or {}
            if not state.get("done"):
                continue
            result.append(
                {
                    "id": None,
                    "torrent_id": "",
                    "task_id": "",
                    "title": item.get("name") or "без названия",
                    "name": item.get("name") or "",
                    "series_key": "",
                    "quality": "",
                    "voice": "",
                    "season": None,
                    "episode": None,
                    "size": state.get("size_human") or "",
                    "torrent_path": "",
                    "transmission_hash": torrent_hash,
                    "hash": torrent_hash,
                    "episodes": [],
                    "progress": state or None,
                    "source": "transmission",
                    "added_at": "",
                    "download_dir": item.get("download_dir") or "",
                }
            )
        self._downloads_cache = (time.monotonic(), result, limit)
        return result

    async def status(self) -> dict[str, Any]:
        import httpx

        cached = getattr(self, "_status_cache", None)
        if cached and time.monotonic() - cached[0] < 5:
            return cached[1]

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
        storage, torrents = await self._storage_status()
        if transmission.get("stale"):
            # Демон занят записью на диск: панель показывает последние известные
            # данные с пометкой, а не прочерки и красную ошибку.
            log.debug("Transmission занят: отдаю последние известные данные")
        payload = {
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
        self._status_cache = (time.monotonic(), payload)
        return payload

    async def restart_transmission(self) -> dict[str, Any]:
        """Перезапуск Transmission из интерфейса.

        Демон умеет намертво застрять в записи на внешний диск: процесс висит в
        состоянии `U`, RPC молчит, и сам он себя не отпустит. До этого помогал
        только терминал — теперь есть кнопка. Скачанное Transmission пересчитает
        сам (возможна перепроверка частичных файлов), поэтому об этом честно
        пишем в отчёте.
        """
        journal.add("transmission", "перезапуск из интерфейса", {})
        result = await self.transmission.restart_daemon()
        # Кэши описывают старый процесс — после перезапуска они врут.
        self._status_cache = None
        self._downloads_cache = None
        self._storage_cache = None
        self.downloads_meta = {
            "stale": False,
            "busy": False,
            "error": "",
            "measured_at": _short_clock(),
        }
        if result.get("ok"):
            journal.add(
                "transmission",
                "Transmission перезапущен из интерфейса",
                {"ждали": f"{result.get('waited', 0)} с"},
            )
        else:
            journal.add(
                "transmission",
                "перезапуск Transmission не удался",
                {"ошибка": result.get("error") or ""},
                level="error",
            )
        return result

    async def _storage_status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Состояние диска для «Служебного»: в потоке, с бюджетом и кэшем.

        `target_info` делает `statvfs` и проверку записи, а `dir_size` обходит
        каталог. На внешнем диске, который уснул после десяти минут простоя, это
        блокирующие вызовы на секунды — и, если делать их прямо здесь, встаёт
        весь цикл событий: чат, SSE, опрос прогресса. Поэтому — в потоке,
        с бюджетом и коротким кэшем, чтобы панель, обновляющаяся каждые 30 с,
        не дёргала диск зря.
        """
        cached = getattr(self, "_storage_cache", None)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1], cached[2]

        from core.storage_target import dir_size, target_info
        from core.torrent_meta import human_bytes as human_size

        def probe() -> tuple[dict[str, Any], dict[str, Any]]:
            info = self.transmission.storage_info()
            # Сколько из занятого — реально наши файлы. Разница — чужие данные
            # или корзина диска: иначе «диск пуст, а место занято».
            own_downloads, done_downloads = dir_size(self.settings.transmission_download_dir)
            own_torrents, done_torrents = dir_size(self.settings.torrents_dir)
            info.own_bytes = own_downloads + own_torrents
            info.own_complete = done_downloads and done_torrents
            storage = info.to_dict()
            storage["downloads_size_human"] = human_size(own_downloads)
            storage["torrents_size_human"] = human_size(own_torrents)
            storage["checked_at"] = _short_clock()
            # Служебные каталоги тома: их не измерить (macOS не даёт читать
            # .Spotlight-V100), но именно они попадают в «не учтено». Пусть
            # панель скажет об этом прямо, а не намекает на чужие файлы.
            root = Path(storage.get("path") or self.settings.transmission_download_dir)
            volume = root.parent
            system_dirs = [
                name for name in (".Spotlight-V100", ".fseventsd", ".Trashes")
                if (volume / name).exists()
            ]
            storage["system_dirs"] = system_dirs
            return storage, target_info(self.settings.torrents_dir).to_dict()

        try:
            result = await asyncio.wait_for(asyncio.to_thread(probe), timeout=6.0)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            journal.add(
                "transmission",
                "диск не ответил за отведённое время",
                {"ошибка": f"{type(exc).__name__}: {exc}"},
                level="warn",
            )
            result = self._stale_storage()
        self._storage_cache = (time.monotonic(), result[0], result[1])
        return result

    def _stale_storage(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Прошлые значения вместо прочерков, если диск занят записью.

        Пока Transmission пишет 4K-сезон, `statvfs` и обход каталога на внешнем
        томе могут не уложиться в бюджет. Раньше панель в этот момент писала
        «диск не ответил» и обнуляла все цифры — хотя диск на месте и работает.
        Теперь показываем последние известные данные с пометкой «устарели».
        """
        cached = getattr(self, "_storage_cache", None)
        if cached and (cached[1] or {}).get("ok"):
            storage = dict(cached[1])
            storage.update(
                {
                    "stale": True,
                    "error": "диск занят записью — данные от последней проверки",
                }
            )
            return storage, dict(cached[2] or {})
        return (
            {
                "ok": False,
                "mounted": True,
                "writable": False,
                "volume": "",
                "path": str(self.settings.transmission_download_dir),
                "free_human": "—",
                "total_human": "—",
                "used_human": "—",
                "own_human": "—",
                "hidden_human": "—",
                "error": "диск не ответил за отведённое время",
            },
            {},
        )

    # ------------------------------------------------------------------
    # Разбор запроса и поиск
    # ------------------------------------------------------------------
    async def _tag_turn(self, user_message_id: int, turn: Turn) -> None:
        """Привязывает ход переписки к задаче: у каждой задачи своя история."""
        if turn.task is None:
            return
        await self.store.set_message_task(user_message_id, turn.task.id)
        for message in turn.messages:
            if not message.task_id:
                message.task_id = turn.task.id

    async def _task_window(self, task_id: str) -> list[dict[str, str]]:
        """История одной задачи для модели — свой контекст у каждой задачи."""
        rows = await self.store.history_for_task(task_id, limit=self.settings.context_messages)
        return self._shape_window(rows, self.settings.context_messages)

    def _shape_window(self, rows: list[dict[str, Any]], limit: int) -> list[dict[str, str]]:
        window: list[dict[str, str]] = []
        for row in rows[-limit:]:
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

    async def _context_window(self, session_id: str, limit: int | None = None) -> list[dict[str, str]]:
        """Последние сообщения переписки — скользящее окно для модели.

        Нужно, чтобы уточнения вида «нет, я про 4 сезон» относились к той же
        задаче, а не начинали разговор с чистого листа. Окно ограничено
        настройкой CONTEXT_MESSAGES (по умолчанию 20 сообщений) и обрезает
        длинные ответы агента, чтобы не жечь токены на простыни карточек.
        """
        limit = limit or self.settings.context_messages
        # После отмены задачи или старта скачивания окно сбрасывается: чат
        # остаётся на экране, но модель больше не видит прошлую переписку и
        # не подмешивает её в новый поиск.
        try:
            since = int(await self.store.get_setting(f"context_from:{session_id}", "0") or 0)
        except (TypeError, ValueError):
            since = 0
        if since:
            rows = await self.store.history_since(session_id, since, limit=limit)
        else:
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

    async def _reset_context(self, session_id: str) -> None:
        """Обнуляет скользящее окно для новой задачи.

        Сообщения не удаляются (человек видит историю в чате), но модель
        получает только то, что было после этой метки.
        """
        if not session_id:
            return
        try:
            top = await self.store.last_message_id(session_id)
            await self.store.set_setting(f"context_from:{session_id}", str(top))
        except Exception as exc:  # noqa: BLE001 - сброс окна не должен ронять ответ
            log.warning("не удалось сбросить контекст: %s", exc)

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
        aliases = data.get("aliases")
        cleaned["aliases"] = [
            str(item).strip()
            for item in (aliases if isinstance(aliases, list) else [])
            if str(item).strip()
        ][:3]
        kind_hint = str(data.get("type") or "").strip().lower()
        # Если модель не указала тип, но человек написал «сериал»/«фильм» —
        # понимаем это сами: на трекере это разные категории.
        lowered = str(text or "").lower()
        if kind_hint not in ("series", "movie"):
            if re.search(r"\bсериал|\bреалити|\bшоу|\bток-шоу|\bмультсериал|\bдокументал", lowered):
                kind_hint = "series"
            elif re.search(r"\bфильм|\bкино|\bмультфильм", lowered):
                kind_hint = "movie"
            else:
                kind_hint = ""
        cleaned["type"] = kind_hint or None
        for key in ("season", "episode_from", "year", "interval_minutes"):
            value = data.get(key)
            try:
                cleaned[key] = int(value) if value not in (None, "", "null") else None
            except (TypeError, ValueError):
                cleaned[key] = None
        auto = data.get("auto_download")
        cleaned["auto_download"] = auto if isinstance(auto, bool) else None
        # Экранку просят редко и обычно словами, которые модель может не
        # отметить (это не качество из списка) — ловим самые явные формы сами.
        screener = data.get("screener")
        if isinstance(screener, bool):
            cleaned["screener"] = screener
        elif re.search(r"экранк|кам[- ]?рип|телесинк", lowered):
            cleaned["screener"] = True
        else:
            cleaned["screener"] = None
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

    # Слова запроса, которые не участвуют в сравнении названий: служебные.
    _STOP_WORDS = {
        "сезон", "сезоны", "серия", "серии", "выпуск", "выпуски", "фильм", "сериал",
        "качай", "скачать", "скачай", "проверяй", "следи", "каждый", "каждые",
        "часов", "часа", "час", "минут", "сутки", "начиная", "нужен", "нужна",
        "этот", "этом", "только", "появится", "если", "когда", "чтобы", "про",
    }

    @classmethod
    def _stop_tokens(cls) -> set[str]:
        """Служебные слова в обоих видах: как написаны и как нормализованы."""
        cached = getattr(cls, "_stop_cache", None)
        if cached is None:
            cached = set(cls._STOP_WORDS) | {normalize(word) for word in cls._STOP_WORDS}
            cached |= {"season", "sezon", "series", "episode", "release", "torrent"}
            cls._stop_cache = cached
        return cached

    @classmethod
    def _significant_tokens(cls, text: str) -> list[str]:
        tokens: list[str] = []
        stop = cls._stop_tokens()
        for word in str(text or "").split():
            token = normalize(word)
            if len(token) < 4 or word.lower().strip(".,!?()«»") in stop or token in stop:
                continue
            if re.fullmatch(r"\d+[a-z]*", token):  # 1080, 2026, 4k
                continue
            tokens.append(token)
        return tokens

    @classmethod
    def _missing_tokens(cls, original: str, found_title: str) -> list[str]:
        """Слова из запроса, которых нет в найденном названии.

        Если такие есть, значит поиск нашёл «похожее, но не то»: в запросе было
        уточнение («… реванш»), которое потерялось.
        """
        haystack = normalize(str(found_title))
        return [token for token in cls._significant_tokens(original) if token not in haystack]

    async def _search_with_suggestions(self, params: dict[str, Any], *, original: str = "", pages: int = 2) -> tuple[list[Torrent], list[dict]]:
        """Поиск, а если по названию пусто — подбор похожих вариантов.

        Трекер ищет по всем словам запроса: «битва экстрасенсов реванш» даёт
        ноль, хотя на трекере есть «Экстрасенсы. Реванш». Поэтому при пустом
        результате пробуем укороченные запросы и подсказываем похожие названия.
        """
        title = str(params.get("title") or "")
        source = str(original or title)
        torrents = await self._search(params)
        by_title = filter_candidates(torrents, {"title": title}) if title else []
        if by_title:
            # Нашлось — но то ли? Если в исходном тексте есть слово, которого
            # нет в найденном названии, или само название мало похоже на
            # найденное («Лост» → «Джимми Киммел… Алоха, Лост!»), ищем variants.
            best_found = rank(by_title, params)[0]
            missing = self._missing_tokens(source, best_found.title)
            score = similarity(source, best_found.series_title or best_found.title)
            if not missing and score >= MIN_SCORE:
                return torrents, []
            journal.add(
                "agent",
                "найденное не похоже на запрос",
                {"запрос": source, "название": best_found.title,
                 "лишние слова": missing, "похожесть": round(score, 2)},
            )

        seen = {torrent.id for torrent in torrents}
        extra: list[Torrent] = []
        alias_hits: dict[str, dict[str, Any]] = {}
        queries: list[str] = []

        def add_query(value: str) -> None:
            query = str(value or "").strip()
            if query and query.lower() not in {item.lower() for item in queries}:
                queries.append(query)

        # 1) Другие названия того же фильма/сериала — их подсказала модель:
        #    «Лост» → «Остаться в живых», «Lost». Именно они находят сериал.
        aliases = [str(item) for item in (params.get("aliases") or []) if str(item).strip()]
        for alias in aliases:
            add_query(alias)
        # Для подсказок берём только первое (самое точное) название: поиск по
        # короткому «Lost» тянет «Lost You Forever» и прочие чужие сериалы.
        primary_alias = aliases[0] if aliases else ""
        latin = translit(title or source)
        add_query(latin)
        # 2) Укороченные варианты запроса — на случай лишних слов.
        for variant in query_variants(source):
            add_query(variant)
        # 3) Дефисы и подчёркивания: «черепашки-мутантантах-ниндзя» для трекера —
        #    одно слово, и опечатка в нём оставляет запрос вообще без ответа.
        #    Ищем и по частям, а похожесть потом отфильтрует лишнее.
        for chunk in re.split(r"[-_/]+", str(source)):
            chunk = chunk.strip()
            if len(chunk) > 2 and " " not in chunk:
                add_query(chunk)

        wanted_type = str(params.get("type") or "").lower()
        for query in queries:
            try:
                rows = await self.mcp.search_pages(query, pages=1)
            except MCPError as exc:
                log.warning("поиск варианта «%s» не удался: %s", query, exc)
                continue
            # Подсказки — только по точному альтернативному названию: латинская
            # транслитерация ищет по слову и тащит чужие «Lost You Forever».
            trusted = bool(primary_alias) and query == primary_alias
            for raw in rows:
                torrent = parse_torrent(raw)
                if not torrent.id:
                    continue
                if torrent.id not in seen:
                    seen.add(torrent.id)
                    extra.append(torrent)
                if not trusted:
                    continue
                # Оставляем только то, где слова другого названия реально есть:
                # поиск по «Остаться в живых» иначе тащит случайные сериалы.
                alias_tokens = [
                    token for token in normalize(query).split() if len(token) > 2
                ]
                haystack = normalize(torrent.title)
                if alias_tokens and not all(token in haystack for token in alias_tokens):
                    continue
                is_series = bool(torrent.seasons or torrent.episodes)
                if wanted_type == "series" and not is_series:
                    continue
                if wanted_type == "movie" and is_series:
                    continue
                name = (torrent.series_title or torrent.title or "").strip()
                key = normalize(name)
                if not key:
                    continue
                hit = alias_hits.setdefault(
                    key,
                    {"title": name, "torrent_id": torrent.id, "count": 0, "score": 0.9},
                )
                hit["count"] += 1

        type_map = {
            torrent.id: bool(torrent.seasons or torrent.episodes) for torrent in torrents + extra
        }
        kinds: list[dict[str, Any]] = []
        for item in similar_titles(source or title, extra or torrents):
            if wanted_type:
                is_series = type_map.get(str(item.get("torrent_id") or ""))
                if is_series is None:
                    continue
                if (wanted_type == "series") != is_series:
                    continue
            kinds.append({**item, "alias": False})
        # Альтернативные названия важнее «похожих по буквам»: это тот же фильм.
        for key, hit in alias_hits.items():
            kinds.append({**hit, "alias": True, "torrent_title": hit["title"]})
        deduped: dict[str, dict[str, Any]] = {}
        for item in kinds:
            key = normalize(item["title"])
            if not key:
                continue
            previous = deduped.get(key)
            if previous is None or (item.get("alias") and not previous.get("alias")):
                deduped[key] = item
        suggestions = sorted(
            deduped.values(),
            key=lambda item: (
                1 if item.get("alias") else 0,
                item.get("count") or 0,
                item.get("score") or 0,
            ),
            reverse=True,
        )[:10]
        journal.add(
            "agent",
            "подбор похожих названий",
            {
                "запрос": source,
                "название от модели": title,
                "другие названия": aliases,
                "искали ещё": queries,
                "найдено раздач": len(extra),
                "подсказки": [item["title"] for item in suggestions],
            },
        )
        return torrents + extra, suggestions

    # ------------------------------------------------------------------
    # Разовая задача
    # ------------------------------------------------------------------
    async def _same_periodic_title(self, session_id: str, title: str) -> Task | None:
        """Уже следящая периодическая задача с тем же названием (без дублей)."""
        wanted = normalize(title)
        if not wanted:
            return None
        for task in await self.store.list_tasks(limit=200):
            if task.kind != "periodic" or task.state not in ("monitoring", "failed"):
                continue
            if normalize(str((task.params or {}).get("title") or "")) == wanted:
                return task
        return None

    async def _same_title_task(self, session_id: str, title: str) -> Task | None:
        """Незакрытая задача этой сессии с тем же названием (чтобы не плодить дубли)."""
        wanted = normalize(title)
        if not wanted:
            return None
        for task in await self.store.list_tasks(limit=50):
            if task.session_id != session_id or task.state not in self.RESUME_STATES:
                continue
            if task.kind != "oneshot":
                continue
            if normalize(str((task.params or {}).get("title") or "")) == wanted:
                return task
        return None

    async def _start_from_link(
        self, session_id: str, text: str, torrent_id: str, turn: Turn
    ) -> None:
        """Задача по ссылке на раздачу — без поиска и без разбора запроса.

        Человек часто присылает готовую ссылку («держи, …/details.php?id=123»).
        Открываем страницу раздачи, собираем `Torrent` из её данных и сразу
        показываем то же, что показали бы после поиска: выбор серий или
        карточку подтверждения.
        """
        turn.add(f"Открываю раздачу по ссылке (id {torrent_id})…", kind="text")
        try:
            details = await self.mcp.details(torrent_id)
        except MCPError as exc:
            turn.add(f"Не смог открыть раздачу #{torrent_id}: {exc}", kind="error")
            return

        torrent = self._torrent_from_details(torrent_id, details)
        if not torrent.title:
            turn.add(
                f"Страница раздачи #{torrent_id} не отдала название — возможно, "
                "ссылка устарела или раздачу удалили.",
                kind="error",
            )
            return

        kind = "series" if (torrent.seasons or torrent.episodes) else "movie"
        task = await self._task_from_params(
            session_id,
            text,
            {"title": (torrent.series_title or torrent.title).strip(), "type": kind},
            kind="oneshot",
        )
        task.options = {"pool": [torrent.to_dict()], "queries": [torrent_id]}
        task.state = "searching"
        await self.store.save_task(task)
        journal.add(
            "agent",
            "задача заведена по ссылке на раздачу",
            {"раздача": torrent.id, "название": torrent.title, "сидов": torrent.seeds},
        )

        turn.task = task
        turn.add(f"По ссылке: «{torrent.title}»", kind="text", task_id=task.id)
        await self._present_candidate(task, [torrent], turn)

    @staticmethod
    def _torrent_from_details(torrent_id: str, details: dict[str, Any]) -> Torrent:
        """Раздача из страницы `details.php`: название, вес, сиды и качество."""
        torrent = parse_torrent(
            {
                "id": torrent_id,
                "title": str(details.get("title") or ""),
                "size": details.get("size") or "",
                "seeds": details.get("seeds") or 0,
                "peers": details.get("peers") or 0,
                "uploadDate": details.get("uploadDate") or "",
                "uploader": details.get("uploader") or "",
                "category": details.get("category") or "",
            }
        )
        # Страница отдаёт качество отдельной строкой («WEB-DL (1080p)»), а в
        # названии старых раздач его может не быть вовсе.
        for label in extract_qualities(str(details.get("quality") or "")):
            if label not in torrent.qualities:
                torrent.qualities.append(label)
        return torrent

    async def _start_oneshot(
        self, session_id: str, text: str, parsed: dict[str, Any], turn: Turn
    ) -> None:
        if not parsed.get("title"):
            turn.add(
                "Не понял, что искать. Напишите название — например "
                "«Тед Лассо 1080p LostFilm».",
            )
            return

        same = await self._same_title_task(session_id, str(parsed.get("title") or ""))
        if same is not None:
            journal.add(
                "agent",
                "продолжаю прежнюю задачу с тем же названием",
                {"задача": same.id, "название": same.title},
            )
            task = same
            task.params = {
                **task.params,
                **{
                    key: value
                    for key, value in parsed.items()
                    if key in PARAM_KEYS and value not in (None, "", "null", [])
                },
            }
            task.state = "searching"
            task.user_query = text
            await self.store.save_task(task)
        else:
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
            torrents, suggestions = await self._search_with_suggestions(
                task.params, original=text
            )
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
            "suggestions": suggestions,
        }
        await self.store.save_task(task)
        await self._selection_step(task, turn)

    def _options_under(self, by_title: list[Torrent], params: dict[str, Any], dimension: str) -> tuple[dict, list[Torrent]]:
        """Что реально доступно, если менять ТОЛЬКО это измерение.

        Важно: варианты считаются с учётом остальных фильтров (сезон, год,
        другая характеристика). Иначе агент предлагал «720p», которого нет в
        четвёртом сезоне, и после выбора честно отвечал «ничего не подошло».
        """
        relaxed = {key: value for key, value in params.items() if key != dimension}
        pool = filter_candidates(by_title, relaxed)
        return collect_options(pool), pool

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
        # Экранки в by_title уже отфильтрованы правилом (core/matching.py).
        # Но если по названию есть ТОЛЬКО они — об этом надо сказать, иначе
        # человек решит, что раздачи нет вовсе.
        screeners = [
            item
            for item in pool
            if item.screener
            and not params.get("screener")
            and title_match(str(params.get("title") or ""), item)
        ]
        if screeners:
            journal.add(
                "agent",
                "экранки убраны из выдачи",
                {"сколько": len(screeners), "пример": screeners[0].title[:120]},
            )
        if not by_title and screeners:
            await self._offer_screeners(task, screeners, turn)
            return
        if not by_title:
            suggestions = (task.options or {}).get("suggestions") or []
            if suggestions:
                await self._offer_suggestions(task, suggestions, turn)
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
            await self._offer_relaxed(task, by_title, turn)
            return

        fresh = [t for t in matched if t.id not in downloaded] or matched

        # Название совпало не точно: сначала уточняем сериал кнопками. Иначе
        # пришлось бы спрашивать «какое качество» у чужих раздач и показывать
        # два списка, которые спорят друг с другом.
        ranked = rank(fresh, params)
        suggestions = (task.options or {}).get("suggestions") or []
        weak = False
        if ranked:
            best_found = ranked[0]
            weak = bool(self._missing_tokens(task.title, best_found.title))
            if not weak:
                score = similarity(task.title, best_found.series_title or best_found.title)
                # Похожесть ниже порога — это другой фильм («Лост» → «Джимми
                # Киммел… Алоха, Лост!»). Показываем подсказки, а не карточку.
                weak = score < MIN_SCORE
        if weak and suggestions:
            await self._ask_title(task, suggestions[:10], len(fresh), turn, weak=True)
            return

        # В выдаче разные фильмы/сериалы — сначала выбираем раздачу. Качество
        # спрашивать раньше бессмысленно: оно относится к конкретному релизу.
        groups: dict[str, list[Torrent]] = {}
        for item in fresh:
            name = (item.series_title or "").strip() or (item.title or "").strip()
            key = normalize(name)
            if key:
                groups.setdefault(key, []).append(item)
        if len(groups) > 1:
            options = [
                {
                    "title": (items[0].series_title or items[0].title).strip(),
                    "torrent_id": items[0].id,
                    "count": len(items),
                    "years": self._years_text(items),
                }
                for items in sorted(groups.values(), key=lambda chunk: -len(chunk))[:10]
            ]
            await self._ask_title(task, options, len(fresh), turn, weak=False)
            return

        # Не хватает параметров — предлагаем только то, что реально есть
        # с учётом уже заданных условий.
        missing: list[str] = []
        if not params.get("quality"):
            missing.append("quality")
        if not params.get("voice"):
            missing.append("voice")
        for dimension in missing:
            options, options_pool = self._options_under(by_title, params, dimension)
            values = options["qualities"] if dimension == "quality" else options["voices"]
            if len(values) > 1:
                await self._ask_dimension(
                    task, turn, dimension, values, len(fresh), found=fresh
                )
                return
            if len(values) == 1:
                # Один вариант — спрашивать не о чем, фиксируем автоматически.
                task.params[dimension] = values[0]
                await self.store.save_task(task)
                fresh = filter_candidates(by_title, task.params) or fresh

        best_list = rank(fresh, task.params)
        await self._present_candidate(task, best_list, turn)

    async def _ask_title(
        self,
        task: Task,
        suggestions: list[dict],
        found_count: int,
        turn: Turn,
        *,
        weak: bool = True,
    ) -> None:
        """Спрашиваем, какая раздача имеется в виду.

        Два повода: название не совпало с найденным (`weak=True`) или в выдаче
        оказались разные фильмы/сериалы. Качество и озвучку спрашивать раньше
        времени бессмысленно: они относятся к конкретному релизу.
        """
        plural = _plural(found_count, 'раздача', 'раздачи', 'раздач')
        if weak:
            head = f"По запросу «{task.title}» нашлось {plural}, но это другие сериалы."
            question = "Какой сериал вы имели в виду?"
        else:
            head = f"По запросу «{task.title}» нашлось {plural} — это разные фильмы/сериалы."
            question = "Какой именно нужен?"
        lines = [head, "", question, ""]
        for item in suggestions[:10]:
            line = f"• {item['title']}"
            bits: list[str] = []
            if item.get("count"):
                bits.append(_plural(int(item["count"]), "раздача", "раздачи", "раздач"))
            if item.get("years"):
                bits.append(str(item["years"]))
            if bits:
                line += " — " + ", ".join(bits)
            lines.append(line)
        lines.append("")
        lines.append("Кнопка выберет раздачу — и я поищу заново уже по ней.")
        lines.append(DETAILS_HINT)
        message = turn.add(
            "\n".join(lines),
            kind="tickets",
            task_id=task.id,
            payload={"task": task.to_dict(), "suggestions": suggestions},
        )
        for item in suggestions[:10]:
            message.actions.append(
                Action(
                    id=f"similar-title-{item['torrent_id']}",
                    label=f"Это «{item['title']}»"[:60],
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "title", "value": item["title"]},
                    group="Похожие названия",
                )
            )
        # Кликабельная строка на каждый вариант: иначе непонятно, чем «Дюна»
        # отличается от «Планеты Дюна», и открыть можно было только первый.
        for item in suggestions[:10]:
            torrent_id = str(item.get("torrent_id") or "")
            if not torrent_id:
                continue
            hint_bits: list[str] = []
            if item.get("count"):
                hint_bits.append(_plural(int(item["count"]), "раздача", "раздачи", "раздач"))
            if item.get("years"):
                hint_bits.append(str(item["years"]))
            message.actions.append(
                self._variant_row(
                    torrent_id,
                    str(item.get("title") or ""),
                    hint=" · ".join(hint_bits),
                    task_id=task.id,
                )
            )
        message.actions.append(
            Action(
                id="cancel-title",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )
        task.state = "awaiting_choice"
        await self.store.save_task(task)

    async def _offer_screeners(self, task: Task, screeners: list[Torrent], turn: Turn) -> None:
        """По названию есть только экранки: говорим прямо и даём выбор.

        Молча сказать «ничего не нашлось» нельзя — раздача есть, и человек
        будет искать её руками. Но и подсовывать CAM/TS без предупреждения
        нельзя: это другой продукт по качеству.
        """
        best = rank(screeners, task.params)[0]
        lines = [
            f"По запросу «{task.title}» нашлись только экранки (CAM/TS/SCR) — "
            "их я не предлагаю: картинка и звук сняты с экрана.",
            "",
            f"• {best.title}",
            f"• {self._signals_line(best)}",
            "",
            "Если это единственный вариант — возьмите кнопкой ниже. "
            "Хотите нормальную раздачу — можно следить за ней.",
        ]
        task.state = "awaiting_choice"
        await self.store.save_task(task)
        message = turn.add("\n".join(lines), kind="tickets", task_id=task.id,
                           payload={"task": task.to_dict(), "screeners": len(screeners)})
        message.actions = [
            Action(
                id="allow-screeners",
                label="Показать экранку",
                kind="choose",
                payload={"task_id": task.id, "dimension": "screener", "value": "1"},
                style="primary",
            ),
            *self._torrent_actions(task, torrent=best),
            Action(
                id="screeners-periodic",
                label="Следить за нормальной раздачей",
                kind="make_periodic",
                payload={
                    "task_id": task.id,
                    "interval_minutes": self.settings.default_interval_minutes,
                    "auto_download": False,
                },
            ),
            Action(
                id="cancel-screeners",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            ),
        ]

    async def _offer_suggestions(self, task: Task, suggestions: list[dict], turn: Turn) -> None:
        """«Не найдено — возможно, вы имели в виду…» с кнопками."""
        asked = str((task.params or {}).get("title") or task.title)
        lines = [f"По запросу «{asked}» на kinozal.me ничего не нашлось.", ""]
        lines.append("Возможно, вы имели в виду:")
        for index, item in enumerate(suggestions[:5], start=1):
            lines.append(f"{index}. {item['title']}")
        lines.append("")
        lines.append("Выберите название кнопкой — поищу заново по нему.")
        message = turn.add("\n".join(lines), kind="tickets", task_id=task.id,
                           payload={"task": task.to_dict(), "suggestions": suggestions})
        for item in suggestions[:10]:
            message.actions.append(
                Action(
                    id=f"title-{item['torrent_id']}",
                    label=item["title"][:60],
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "title", "value": item["title"]},
                    group="Похожие названия",
                    style="primary" if item is suggestions[0] else "normal",
                )
            )
        for item in suggestions[:10]:
            torrent_id = str(item.get("torrent_id") or "")
            if torrent_id:
                message.actions.append(
                    self._variant_row(torrent_id, str(item.get("title") or ""), task_id=task.id)
                )
        message.actions.append(
            Action(
                id="keep-typo",
                label="Искать как написал",
                kind="noop",
                payload={"task_id": task.id},
                group="Действие",
            )
        )
        message.actions.append(
            Action(
                id="make-periodic-typo",
                label="Следить за этим названием",
                kind="make_periodic",
                payload={
                    "task_id": task.id,
                    "interval_minutes": self.settings.default_interval_minutes,
                    "auto_download": False,
                },
                group="Действие",
            )
        )
        task.state = "awaiting_choice"
        await self.store.save_task(task)

    async def _offer_relaxed(self, task: Task, by_title: list[Torrent], turn: Turn) -> None:
        """Подходящего нет: показываем, что есть, и даём снять лишний фильтр."""
        params = task.params
        has_filters = bool(params.get("quality") or params.get("voice"))
        q_options, q_pool = self._options_under(by_title, params, "quality")
        v_options, v_pool = self._options_under(by_title, params, "voice")

        if not has_filters:
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

        # Точная причина: выбранного значения в этих раздачах может не быть вовсе.
        causes: list[str] = []
        if params.get("voice") and not any(voice_match(str(params["voice"]), t.voices) for t in by_title):
            causes.append(f"озвучки «{params['voice']}» в этих раздачах нет")
        if params.get("quality") and not any(
            quality_match(str(params["quality"]), t.qualities) for t in by_title
        ):
            causes.append(f"качества {self._quality_label(str(params['quality']))} в этих раздачах нет")

        if causes:
            lines = [f"Не подходит: {self._filters_text(params)}.", "", "• " + "; ".join(causes) + "."]
        else:
            lines = [f"Под эти условия ничего не подошло: {self._filters_text(params)}.", ""]
        if q_options["qualities"]:
            # Те же сигналы, что и в вопросе о качестве: видно, что даёт
            # каждый вариант, а не просто список слов.
            lines.append("• качество:")
            lines.extend(
                self._option_summaries(
                    q_pool, "quality", q_options["qualities"], params
                )
            )
        if v_options["voices"]:
            lines.append("• озвучка:")
            lines.extend(
                self._option_summaries(v_pool, "voice", v_options["voices"], params)
            )
        if not (q_options["qualities"] or v_options["voices"]):
            lines.append("С учётом остальных условий вариантов нет — попробуйте убрать фильтры.")
        else:
            lines.append("")
            lines.append("Выберите из доступного или снимите фильтр кнопкой ниже.")

        task.state = "awaiting_choice"
        await self.store.save_task(task)
        message = turn.add("\n".join(lines), kind="tickets", task_id=task.id,
                           payload={"task": task.to_dict()})
        message.actions = []
        for value in q_options["qualities"][:6]:
            message.actions.append(
                Action(
                    id=f"q-{value}",
                    label=self._quality_label(value),
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "quality", "value": value},
                    group="Качество",
                )
            )
        for value in v_options["voices"][:6]:
            message.actions.append(
                Action(
                    id=f"v-{value}",
                    label=value,
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "voice", "value": value},
                    group="Озвучка",
                )
            )
        if params.get("quality"):
            message.actions.append(
                Action(
                    id="drop-quality",
                    label="Без учёта качества",
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "quality", "value": ""},
                )
            )
        if params.get("voice"):
            message.actions.append(
                Action(
                    id="drop-voice",
                    label="Без учёта озвучки",
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "voice", "value": ""},
                )
            )
        # Строка на каждый вариант: качество и озвучка открывают карточку
        # лучшей раздачи этого варианта, а не одну общую «лучшую».
        message.actions.extend(
            self._option_rows(
                task, q_pool, "quality", q_options["qualities"][:6], params
            )
        )
        message.actions.extend(
            self._option_rows(task, v_pool, "voice", v_options["voices"][:6], params)
        )
        if not (q_options["qualities"] or v_options["voices"]):
            message.actions.extend(
                self._torrent_actions(task, torrent=self._best_torrent(by_title, params))
            )
        message.actions.append(
            Action(
                id="cancel-options",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )

    async def _ask_dimension(
        self,
        task: Task,
        turn: Turn,
        dimension: str,
        values: list[str],
        count: int,
        found: list[Torrent] | None = None,
    ) -> None:
        """Спрашивает качество или озвучку — отдельной группой кнопок."""
        title = "Какое качество берём?" if dimension == "quality" else "Какую озвучку берём?"
        shown = ", ".join(
            self._quality_label(v) if dimension == "quality" else v for v in values[:8]
        )
        lines = [
            f"По запросу «{task.title}» нашлось "
            f"{_plural(count, 'раздача', 'раздачи', 'раздач')}. {title}",
            "",
        ]
        found = found or []
        suggestions = (task.options or {}).get("suggestions") or []
        best_found = max(found, key=lambda item: item.seeds, default=None) if found else None
        # Сравниваем с названием, по которому реально искали (после подсказки),
        # а не с исходной опечаткой — иначе предупреждение висит и после выбора.
        weak = bool(suggestions) and best_found is not None and bool(
            self._missing_tokens(task.title, best_found.title)
        )
        if weak:
            # Один список на всё: кандидаты, которые и станут кнопками ниже.
            # Отдельный перечень «что нашлось по словам» только путал — там
            # были чужие сериалы, да ещё с дублями из-за регистра.
            lines.append("Похоже, это другие сериалы. Скорее всего, вы имели в виду (кнопки ниже):")
            lines.append("")
            for item in suggestions[:3]:
                lines.append(f"• {item['title']}")
            lines.append("")
        else:
            # Точное совпадение: показываем, какие сериалы реально есть в выдаче.
            shows: dict[str, tuple[str, int, list[Torrent]]] = {}
            for item in found:
                name = (item.series_title or "").strip() or (item.title or "").strip()
                key = normalize(name)
                if not key:
                    continue
                title_text, number, items = shows.get(key, (name, 0, []))
                shows[key] = (title_text, number + 1, items + [item])
            top = sorted(shows.values(), key=lambda pair: -pair[1])[:4]
            if len(top) > 1:
                lines.append("Что нашлось:")
                for name, number, items in top:
                    years = self._years_text(items)
                    line = f"• {name} — {_plural(number, 'раздача', 'раздачи', 'раздач')}"
                    if years:
                        line += f", {years}"
                    lines.append(line)
                lines.append("")
        # Под каждым вариантом — вес, сиды и возраст лучшей раздачи: иначе
        # «1080p» и «1080p» выглядят одинаково, хотя одна мёртвая, а другая 40 ГБ.
        summary = self._option_summaries(found, dimension, values, task.params)
        if summary:
            lines.append("Доступно с учётом остальных условий:")
            lines.extend(summary)
        else:
            lines.append(f"Доступно с учётом остальных условий: {shown}.")
        lines.append(DETAILS_HINT)
        text = "\n".join(lines)
        group = "Качество" if dimension == "quality" else "Озвучка"
        message = turn.add(text, kind="tickets", task_id=task.id, payload={"task": task.to_dict()})
        message.actions = [
            Action(
                id=f"choose-{dimension}-{index}",
                label=self._quality_label(value) if dimension == "quality" else value,
                kind="choose",
                payload={"task_id": task.id, "dimension": dimension, "value": value},
                group=group,
            )
            for index, value in enumerate(values[:8])
        ]
        # Название совпало не точно — под рукой кнопки с похожими сериалами,
        # чтобы не набирать название заново.
        if weak:
            for item in ((task.options or {}).get("suggestions") or [])[:3]:
                message.actions.append(
                    Action(
                        id=f"similar-title-{item['torrent_id']}",
                        label=f"Это «{item['title']}»"[:60],
                        kind="choose",
                        payload={
                            "task_id": task.id,
                            "dimension": "title",
                            "value": item["title"],
                        },
                        group="Похожие названия",
                    )
                )
        # Кликабельная строка на КАЖДЫЙ вариант: нажал «DVDRip» — открылась
        # карточка лучшей раздачи этого варианта. Одна кнопка «Подробнее о
        # раздаче» показывала только лучшую из всех и сбивала с толку.
        option_rows = self._option_rows(task, found, dimension, values, task.params)
        if option_rows:
            message.actions.extend(option_rows)
        else:
            message.actions.extend(
                self._torrent_actions(task, torrent=self._best_torrent(found, task.params))
            )
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

    @staticmethod
    def _filters_text(params: dict[str, Any]) -> str:
        bits = []
        if params.get("year"):
            bits.append(f"год {params['year']}")
        if params.get("season"):
            bits.append(f"сезон {params['season']}")
        if params.get("quality"):
            bits.append(f"качество {params['quality']}")
        if params.get("voice"):
            bits.append(f"озвучка {params['voice']}")
        if params.get("episode_from"):
            bits.append(f"серия {params['episode_from']}")
        if params.get("screener"):
            bits.append("с экранкой")
        return ", ".join(bits) or "без уточнений"

    async def _listing_entries(self, torrent: Torrent) -> list[dict[str, Any]] | None:
        """Состав раздачи со страницы трекера — без траты дневного лимита.

        На kinozal.me в сутки даётся ограниченное число .torrent-файлов, а состав
        файлов нужен уже для карточки (видно вес и серии). Страница раздачи
        отдаёт его отдельным запросом, поэтому лимит тратится только на
        настоящее скачивание.
        """
        try:
            listing = await self.mcp.file_list(torrent.id)
        except Exception as exc:  # noqa: BLE001 - без списка просто качаем .torrent
            journal.add(
                "agent",
                "не удалось получить список файлов со страницы раздачи",
                {"раздача": torrent.id, "ошибка": str(exc)},
                level="warn",
            )
            return None
        files = listing.get("files") or []
        if not files:
            return None
        return [
            {
                "index": index,
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
            }
            for index, item in enumerate(files)
        ]

    async def _present_candidate(
        self, task: Task, best_list: list[Torrent], turn: Turn
    ) -> None:
        """Показывает кандидата: выбор серий или сразу полную карточку.

        Состав файлов берём со страницы раздачи (лимит .torrent не тратится),
        и только если страница не отдала список — качаем .torrent. Сам .torrent
        нужен лишь в момент, когда человек нажал «Скачать».
        """
        best = best_list[0]
        journal.add(
            "agent",
            "нашёл кандидата",
            {"раздача": best.title, "размер": best.size, "сидов": best.seeds,
             "серии": best.episodes},
        )
        task.candidate = best.to_dict()
        task.options = {
            **(task.options or {}),
            "alternatives": [t.to_dict() for t in best_list[1:4]],
        }

        listing = await self._listing_entries(best)
        if listing is not None:
            path: Path | None = None
            entries = parse_files(listing)
            journal.add(
                "agent",
                "состав раздачи взят со страницы трекера",
                {"раздача": best.id, "файлов": len(entries)},
            )
        else:
            path, error, reason = await self._fetch_torrent(best)
            if path is None:
                if reason == "tracker":
                    await self._report_tracker_problem(task, best, error, turn)
                else:
                    await self._report_storage_problem(
                        task, best, {"error": error, "reason": "disk", "path": None}, turn
                    )
                return
            entries = parse_files(file_list(path))

        groups = episode_groups(entries)
        if path is not None:
            task.options = {**(task.options or {}), "torrent_path": str(path)}

        # Сериал определяем по названию раздачи: у коллекции фильмов файлы тоже
        # нумеруются («Пила 1», «Пила 2»), но это не серии.
        wanted_type = str(task.params.get("type") or "").lower()
        is_series = bool(best.seasons or best.episodes)
        if wanted_type == "movie":
            is_series = False
        elif wanted_type == "series":
            is_series = True

        # Пачка серий и серия не названа — спрашиваем, какие качать.
        if is_series and len(groups) > 1 and not task.params.get("episode_from"):
            await self._show_episode_picker(task, best, groups, path, turn)
            return

        plan = self._plan_files(task, entries, best)
        if plan.get("kind") == "choose":
            await self._show_episode_picker(task, best, groups, path, turn)
            return
        if not task.params.get("quality") and best.quality:
            task.params["quality"] = best.quality
        if not task.params.get("voice") and best.voices:
            task.params["voice"] = best.voices[0]
        task.options = {**(task.options or {}), "plan": plan}
        task.state = "awaiting_confirm"
        await self.store.save_task(task)
        text = self._confirm_card_text(task, best, plan)
        suggestions = (task.options or {}).get("suggestions") or []
        # «Совпало не точно» — когда в названии, по которому искали, были слова,
        # которых нет в найденном: значит, поиск нашёл похожее, но не то.
        weak = bool(suggestions) and bool(self._missing_tokens(task.title, best.title))
        if weak and suggestions:
            text += "\n\n⚠ Название совпало не точно. Возможно, вы имели в виду:"
            for index, item in enumerate(suggestions[:3], start=1):
                text += f"\n{index}. {item['title']}"
        # Раздача целиком (весь сезон) — частое желание: если она есть в пуле,
        # предлагаем кнопкой и подписываем, что это именно она.
        whole = self._season_pack(task, best)
        if whole is not None:
            line = f"• Весь сезон есть отдельной раздачей:\n    {whole.title}"
            if whole.size:
                line += f" — {_comma(whole.size)}"
            text += f"\n{line}"
        alternatives = best_list[1:4]
        variant_rows: list[Action] = []
        if alternatives:
            text += "\n\nДругие раздачи по этому запросу (нажмите, чтобы посмотреть):"
            for index, item in enumerate(alternatives, start=2):
                label = self._short_torrent_label(item)
                text += f"\n{index}. {item.title}" + (f" — {label}" if label else "")
                variant_rows.append(self._variant_row(item.id, item.title, hint=label))
        text += "\n\nСтавлю в Transmission?"
        message = turn.add(
            text,
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict(), "plan": plan},
        )
        message.actions = self._start_actions(task, plan)
        if whole is not None:
            message.actions.insert(
                1,
                Action(
                    id=f"pick-pack-{whole.id}",
                    label=f"Скачать весь сезон · {_comma(whole.size)}"[:60],
                    kind="pick_torrent",
                    payload={"task_id": task.id, "torrent_id": whole.id},
                ),
            )
        if weak:
            for item in suggestions[:3]:
                message.actions.append(
                    Action(
                        id=f"confirm-title-{item['torrent_id']}",
                        label=f"Это «{item['title']}»"[:60],
                        kind="choose",
                        payload={"task_id": task.id, "dimension": "title", "value": item["title"]},
                        group="Похожие названия",
                    )
                )
        # Кликабельные строки вариантов: нажал на раздачу — открылась её карточка.
        message.actions.extend(variant_rows)
        if alternatives:
            for index, alternative in enumerate(alternatives, start=2):
                label = self._short_torrent_label(alternative)
                message.actions.append(
                    Action(
                        id=f"pick-{alternative.id}",
                        label=f"{index}. {label}"[:60] if label else f"Вариант {index}",
                        kind="pick_torrent",
                        payload={"task_id": task.id, "torrent_id": alternative.id},
                    )
                )

    # ------------------------------------------------------------------
    # Серии: один вид на все места (сезон + номер)
    # ------------------------------------------------------------------
    @staticmethod
    def _pair_key(season: Any, episode: Any) -> tuple[int | None, int]:
        """(сезон, серия) — ключ, по которому сверяем выбор и файлы."""
        try:
            season_value = int(season) if season is not None and season != "" else None
        except (TypeError, ValueError):
            season_value = None
        try:
            episode_value = int(episode)
        except (TypeError, ValueError):
            episode_value = 0
        return season_value, episode_value

    @classmethod
    def _as_pairs(cls, value: Any) -> list[dict[str, Any]]:
        """Серии в одном виде: `[{'season': 2, 'episode': 1}, …]`.

        В старых задачах серии лежали просто числами. Число без сезона значит
        «эта серия в сезоне задачи» (а если сезона нет — в любом): иначе для
        пачки «1-4 сезоны» выбор «серия 1» раскрывался в четыре файла.
        """
        pairs: list[dict[str, Any]] = []
        seen: set[tuple[int | None, int]] = set()
        for item in value or []:
            if isinstance(item, dict):
                season, episode = item.get("season"), item.get("episode")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                season, episode = item[0], item[1]
            else:
                season, episode = None, item
            key = cls._pair_key(season, episode)
            if not key[1] or key in seen:
                continue
            seen.add(key)
            pairs.append({"season": key[0], "episode": key[1]})
        return pairs

    @staticmethod
    def _episodes_text(pairs: list[dict[str, Any]]) -> str:
        """«сезон 2, серия 1» / «сезон 2, серии 1, 3» / «серии 2×1, 3×4»."""
        if not pairs:
            return ""
        seasons = {item.get("season") for item in pairs}
        numbers = sorted({int(item["episode"]) for item in pairs})
        if len(seasons) == 1:
            season = next(iter(seasons))
            if len(numbers) == 1:
                body = f"серия {numbers[0]}"
            elif numbers == list(range(numbers[0], numbers[-1] + 1)) and len(numbers) > 2:
                body = f"серии {numbers[0]}–{numbers[-1]}"
            else:
                body = "серии " + ", ".join(str(number) for number in numbers)
            return f"сезон {season}, {body}" if season is not None else body
        listed = ", ".join(
            f"{item.get('season')}×{item['episode']}" if item.get("season") is not None
            else str(item["episode"])
            for item in sorted(pairs, key=lambda pair: (pair.get("season") or 0, int(pair["episode"])))
        )
        return "серии " + listed

    def _build_episodes_plan(self, task: Task, episodes: Any) -> dict | None:
        """План по выбранным сериям: файлы, вес, номера.

        Выбор сверяется по паре (сезон, серия). Раньше сверялся только номер:
        в пачке «1-4 сезоны» одно нажатие «Серия 1» ставило в очередь
        S01E01, S02E01, S03E01 и S04E01 — четыре файла вместо одного.
        """
        groups = (task.options or {}).get("episode_groups") or []
        wanted_pairs = self._as_pairs(episodes)
        if not wanted_pairs:
            return None
        task_season = task.params.get("season")
        wanted_keys: set[tuple[int | None, int]] = set()
        for pair in wanted_pairs:
            season = pair["season"]
            if season is None and task_season not in (None, ""):
                try:
                    season = int(task_season)
                except (TypeError, ValueError):
                    season = None
            wanted_keys.add((season, int(pair["episode"])))

        matched: list[dict[str, Any]] = []
        for group in groups:
            group_season, group_episode = self._pair_key(
                group.get("season"), group.get("episode")
            )
            for season, episode in wanted_keys:
                if episode != group_episode:
                    continue
                if season is not None and group_season is not None and season != group_season:
                    continue
                matched.append(group)
                break
        if not matched:
            return None

        wanted: list[int] = []
        size = 0
        names: list[str] = []
        selected: list[dict[str, Any]] = []
        for group in matched:
            wanted.extend(int(index) for index in (group.get("indexes") or []))
            size += int(group.get("size") or 0)
            names.extend(str(name) for name in (group.get("names") or []))
            selected.append(
                {
                    "season": self._pair_key(group.get("season"), group.get("episode"))[0],
                    "episode": int(group.get("episode") or 0),
                }
            )
        selected.sort(key=lambda item: (item.get("season") or 0, item["episode"]))
        total_size = sum(int(group.get("size") or 0) for group in groups)
        seasons = {item["season"] for item in selected if item["season"] is not None}
        return {
            "kind": "episode",
            "wanted": sorted(set(wanted)),
            "size": size,
            "names": names,
            "season": next(iter(seasons)) if len(seasons) == 1 else None,
            "episode": max(item["episode"] for item in selected),
            "selected_episodes": selected,
            "episodes": selected,
            "total_size": total_size,
            "pack": True,
            "total_files": len(wanted),
        }

    async def _confirm_selected_episodes(
        self, task: Task, episodes: Any, turn: Turn
    ) -> None:
        """Полная карточка по выбранным сериям (для альтернатив и проверок)."""
        plan = self._build_episodes_plan(task, episodes)
        if plan is None:
            turn.add(
                "Не нашёл файлов этих серий в раздаче — попробуйте выбрать заново.",
                kind="error",
                task_id=task.id,
            )
            return
        task.options = {
            **(task.options or {}),
            "plan": plan,
            "selected_episodes": plan["selected_episodes"],
        }
        task.state = "awaiting_confirm"
        await self.store.save_task(task)
        message = turn.add(
            self._confirm_card_text(task, Torrent.from_dict(task.candidate or {}), plan),
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict(), "plan": plan},
        )
        message.actions = self._start_actions(task, plan)

    async def _apply_title(self, task: Task, title: str, turn: Turn) -> None:
        """Пользователь выбрал похожее название — ищем заново уже по нему."""
        title = title.strip()
        if not title:
            turn.add("Пустое название — поищите ещё раз.", kind="error", task_id=task.id)
            return
        task.params["title"] = title
        task.params.pop("original_title", None)
        task.params["seen_ids"] = []
        task.state = "searching"
        await self.store.save_task(task)
        journal.add("agent", "название исправлено по подсказке", {"стало": title})

        try:
            torrents = await self._search(task.params)
        except MCPError as exc:
            task.state = "failed"
            task.last_error = str(exc)
            await self.store.save_task(task)
            turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
            return

        # Выбрали конкретную раздачу — оставляем только её: поиск по слову
        # вернёт и «Вторжение извне», и «Голоса извне», а нам нужна одна.
        chosen = normalize(title)
        exact = [item for item in torrents if normalize(item.series_title or "") == chosen]
        if exact:
            journal.add(
                "agent",
                "поиск сужен до выбранной раздачи",
                {"название": title, "раздач": len(torrents), "осталось": len(exact)},
            )
            torrents = exact
        task.options = {
            **(task.options or {}),
            "pool": [t.to_dict() for t in torrents[:80]],
            "suggestions": [],
            "queries": [title],
        }
        await self.store.save_task(task)
        turn.add(f"Ищу «{title}».", kind="text", task_id=task.id)
        if task.kind == "periodic":
            await self._present_periodic(task, torrents, turn)
        else:
            await self._selection_step(task, turn)

    async def _show_episode_picker(
        self,
        task: Task,
        torrent: Torrent,
        groups: list[dict],
        path: Path | None,
        turn: Turn,
    ) -> None:
        """Список серий кнопками: можно выбрать одну, несколько или все.

        `path` может быть None: состав раздачи пришёл со страницы трекера,
        а .torrent скачается уже после нажатия «Скачать».

        В пачке на несколько сезонов («1-4 сезоны: 1-40 серии») показываем
        только сезон задачи: иначе список — это 40 кнопок «Серия 1…10»,
        повторённых четыре раза, и понять, что выбрал, невозможно.
        """
        selected = self._as_pairs((getattr(task, "options", {}) or {}).get("selected_episodes"))
        selected_keys = {(item["season"], item["episode"]) for item in selected}
        task.options = {
            **(task.options or {}),
            "episode_groups": [
                {
                    "season": group.get("season"),
                    "episode": group.get("episode"),
                    "indexes": group.get("indexes"),
                    "size": group.get("size"),
                    "names": group.get("names"),
                }
                for group in groups
            ],
            "selected_episodes": selected,
        }
        if path is not None:
            task.options["torrent_path"] = str(path)

        pack_seasons = sorted(
            {int(group["season"]) for group in groups if group.get("season") is not None}
        )
        task_season = task.params.get("season")
        try:
            task_season = int(task_season) if task_season not in (None, "") else None
        except (TypeError, ValueError):
            task_season = None
        # Какой сезон показываем: сезон задачи, если он есть в этой пачке.
        shown_season = task_season if task_season in pack_seasons else None
        # Пачка на несколько сезонов: сначала сезон. Иначе список — это 191
        # кнопка «Серия N» без шансов найти нужную, а всё, что за пределами
        # экрана, просто не видно.
        if shown_season is None and len(pack_seasons) > 1:
            await self._ask_pack_season(task, torrent, groups, path, turn, pack_seasons)
            return
        visible = [
            group for group in groups
            if shown_season is None or self._pair_key(group.get("season"), group.get("episode"))[0] == shown_season
        ] or groups
        multi_season = len({self._pair_key(group.get("season"), group.get("episode"))[0] for group in visible}) > 1

        head = f"В раздаче {_plural(len(visible), 'серия', 'серии', 'серий')}"
        if shown_season is not None:
            head += f" (сезон {shown_season})"
            if len(pack_seasons) > 1:
                head += f", а в пачке сезоны {pack_seasons[0]}–{pack_seasons[-1]}"
        elif len(pack_seasons) > 1:
            head += f" (сезоны {pack_seasons[0]}–{pack_seasons[-1]})"
        head += " — что качаем?"
        storage = self.transmission.storage_info()
        info = storage.to_dict()
        lines = [head, ""]
        # Что за раздача и живая ли она — до выбора серий, а не после.
        lines.extend(self._torrent_warnings(torrent))
        signals = self._signals_line(torrent)
        if signals:
            lines.append(f"• Раздача: {signals}")
        season_size = sum(int(group.get("size") or 0) for group in visible)
        lines.append(
            ("• Весь сезон — " if shown_season is not None else "• Вся раздача — ")
            + human_bytes(season_size)
        )
        if storage.ok:
            lines.append(
                f"• Диск «{info['volume']}»: свободно {info['free_human']} "
                f"из {info['total_human']} (резерв {human_bytes(self.transmission.min_free_bytes())})"
            )
        else:
            lines.append(f"• ⚠ Внешний диск недоступен: {storage.error}")
        lines.append("• Отметьте серии кнопками (можно несколько) — сумма посчитается сама.")
        lines.append("• Куда: " + str(self.settings.transmission_download_dir))
        limit = 60
        hidden = len(visible) - limit
        if hidden > 0:
            lines.append(
                f"• Показаны первые {limit} из {len(visible)} серий — остальные "
                "можно взять как «Весь сезон»."
            )
        message = turn.add("\n".join(lines), kind="tickets", task_id=task.id,
                           payload={"task": task.to_dict(), "torrent": torrent.to_dict()})
        for group in visible[:limit]:
            season, episode = self._pair_key(group.get("season"), group.get("episode"))
            label = f"Серия {episode} · {human_bytes(int(group.get('size') or 0))}"
            if multi_season and season is not None:
                label = f"{season}×{episode} · {human_bytes(int(group.get('size') or 0))}"
            message.actions.append(
                Action(
                    id=f"ep-{season if season is not None else 'x'}-{episode}",
                    label=label,
                    kind="toggle_episode",
                    payload={
                        "task_id": task.id,
                        "episode": episode,
                        "season": season,
                        "size": int(group.get("size") or 0),
                    },
                    group="Серии",
                    multi=True,
                    selected=(season, episode) in selected_keys,
                )
            )
        message.actions.append(
            Action(
                id="submit-episodes",
                label="Скачать выбранные",
                kind="download_selected",
                payload={"task_id": task.id},
                style="primary",
                group="Действие",
            )
        )
        message.actions.append(
            Action(
                id="whole-pack",
                label=f"Весь сезон {shown_season}" if shown_season is not None else "Весь сезон",
                kind="download_all",
                payload={"task_id": task.id, "season": shown_season},
                group="Действие",
            )
        )
        message.actions.extend(self._torrent_actions(task))
        if len(pack_seasons) > 1:
            message.actions.append(
                Action(
                    id="other-season",
                    label="Другой сезон",
                    kind="pick_season",
                    payload={"task_id": task.id},
                    group="Действие",
                )
            )
        message.actions.append(
            Action(
                id="cancel-episodes",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
                group="Действие",
            )
        )
        task.state = "awaiting_choice"
        await self.store.save_task(task)

    async def _ask_pack_season(
        self,
        task: Task,
        torrent: Torrent,
        groups: list[dict],
        path: Path | None,
        turn: Turn,
        pack_seasons: list[int],
    ) -> None:
        """В пачке несколько сезонов — сначала сезон, потом серии.

        Пачка «1-10 сезоны: 1-193 серии» иначе превращается в 191 кнопку
        «Серия N»: найти нужную невозможно, а всё за пределами экрана и вовсе
        не видно.
        """
        counts: dict[int, int] = {}
        for group in groups:
            season = self._pair_key(group.get("season"), group.get("episode"))[0]
            if season is None:
                continue
            counts[int(season)] = counts.get(int(season), 0) + 1
        lines = [
            f"В раздаче {_plural(len(groups), 'серия', 'серии', 'серий')} "
            f"в {_plural(len(pack_seasons), 'сезоне', 'сезонах', 'сезонах')} — какой сезон?",
            "",
        ]
        lines.extend(self._torrent_warnings(torrent))
        signals = self._signals_line(torrent)
        if signals:
            lines.append(f"• Раздача: {signals}")
        lines.append("• " + ", ".join(f"{season} — {counts.get(season, 0)}" for season in pack_seasons))
        lines.append("• Затем отметьте серии этого сезона.")
        message = turn.add(
            "\n".join(lines),
            kind="tickets",
            task_id=task.id,
            payload={"task": task.to_dict(), "torrent": torrent.to_dict()},
        )
        for season in pack_seasons:
            message.actions.append(
                Action(
                    id=f"season-{season}",
                    label=f"Сезон {season} · {counts.get(season, 0)} сер.",
                    kind="pick_season",
                    payload={"task_id": task.id, "season": season},
                    group="Сезоны",
                )
            )
        message.actions.extend(self._torrent_actions(task))
        message.actions.append(
            Action(
                id="cancel-season",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
                group="Действие",
            )
        )
        task.options = {
            **(task.options or {}),
            "episode_groups": [
                {
                    "season": group.get("season"),
                    "episode": group.get("episode"),
                    "indexes": group.get("indexes"),
                    "size": group.get("size"),
                    "names": group.get("names"),
                }
                for group in groups
            ],
        }
        if path is not None:
            task.options["torrent_path"] = str(path)
        task.state = "awaiting_choice"
        await self.store.save_task(task)

    async def _pick_season(self, task: Task, payload: dict[str, Any], turn: Turn) -> None:
        """Выбран сезон пачки: показываем серии этого сезона (или список сезонов)."""
        raw = payload.get("season")
        try:
            season = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            season = None
        if season is not None:
            task.params["season"] = season
        else:
            task.params.pop("season", None)
        task.options = {**(task.options or {}), "selected_episodes": []}
        await self.store.save_task(task)
        groups = (task.options or {}).get("episode_groups") or []
        torrent = Torrent.from_dict(task.candidate or {}) if task.candidate else None
        if torrent is None or not groups:
            turn.add("Раздача уже не под рукой — найдите её заново.", kind="error", task_id=task.id)
            return
        path_str = str((task.options or {}).get("torrent_path") or "")
        await self._show_episode_picker(
            task, torrent, groups, Path(path_str) if path_str else None, turn
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
                    group="Качество",
                )
            )
        for value in options["voices"][:5]:
            actions.append(
                Action(
                    id=f"choose-voice-{value}",
                    label=value,
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "voice", "value": value},
                    group="Озвучка",
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
    async def _present_periodic(
        self, task: Task, torrents: list[Torrent], turn: Turn
    ) -> None:
        """Первый шаг: что подходит под условия и за каким вариантом следить.

        Строго: раздачи, которые не подходят под заданные условия (год,
        качество, озвучка, сезон, серия), не считаются найденными. Раньше при
        пустом совпадении агент подставлял «что-нибудь похожее по названию» — и
        задача «Прайм-тайм 2026 в 4K» начинала следить за фильмом 2024 года.

        Режим («качать сразу» / «только сообщать») здесь не спрашивается: сначала
        человек выбирает вариант — свой или похожий, — и уже в резюме выбранного
        варианта появляются эти две кнопки.
        """
        # Пул храним: выбор варианта и резюме работают без похода на трекер.
        task.options = {
            **(task.options or {}),
            "pool": [item.to_dict() for item in torrents[:80]],
            "queries": [task.params.get("title")],
        }
        title = str(task.params.get("title") or "")
        by_title = filter_candidates(torrents, {"title": title})
        # «С 4 серии» значит, что раздача с 1-3 сериями не подходит, даже если
        # название и качество совпали.
        episode_from = task.params.get("episode_from")
        try:
            min_episode = int(episode_from) if episode_from not in (None, "") else None
        except (TypeError, ValueError):
            min_episode = None
        matched = filter_candidates(by_title, task.params, min_episode=min_episode)
        best = rank(matched, task.params)
        # Что уже есть под наши условия — это «норма», а не новость.
        self._record_baseline(task, matched)
        task.candidate = best[0].to_dict() if best else None
        task.state = "awaiting_confirm"
        if not best:
            task.notify_last = now_iso()
        await self.store.save_task(task)

        conditions = self._filters_text(task.params)
        head = f"Задача «{task.title}» — проверять {self._human_interval(task.interval_minutes)}."
        if best:
            head += f"\n\nСейчас подходит ({conditions}):\n• {describe(best[0])}"
            if len(best) > 1:
                head += f"\n• и ещё {_plural(len(best) - 1, 'раздача', 'раздачи', 'раздач')}"
        else:
            head += (
                f"\n\nПо вашим условиям ({conditions}) на трекере сейчас ничего нет — "
                "буду проверять и сообщать, когда появится."
            )

        matched_ids = {item.id for item in matched}
        near = [item for item in rank(by_title, task.params) if item.id not in matched_ids][:3]
        if near:
            head += "\n\nПохожие по названию, но с другими параметрами:"
            for item in near:
                head += f"\n• {item.title[:110]}"

        message = turn.add(
            head,
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict(), "torrent": (best[0].to_dict() if best else None)},
        )

        # 1) Похожие названия от модели (вдруг опечатка в названии).
        suggestions = (task.options or {}).get("suggestions") or []
        original = str(task.user_query or "").split(" | ")[0]
        if best and suggestions and not self._missing_tokens(original, best[0].title):
            suggestions = []
        for item in suggestions[:3]:
            message.actions.append(
                Action(
                    id=f"periodic-title-{item['torrent_id']}",
                    label=f"Это «{item['title']}»"[:60],
                    kind="choose",
                    payload={"task_id": task.id, "dimension": "title", "value": item["title"]},
                    group="Похожие названия",
                )
            )

        # 2) Свой вариант — по заданным условиям. Всегда есть и главный.
        message.actions.append(
            Action(
                id="periodic-strict",
                label=f"Следить строго: {conditions}"[:70],
                kind="periodic_pick",
                payload={"task_id": task.id, "variant": "strict"},
                style="primary",
                group="Ваш вариант",
            )
        )
        if best:
            # Подробности о найденном — чтобы решить, то ли это вообще.
            message.actions.extend(self._torrent_actions(task))

        # 3) По кнопке на каждый похожий вариант + «Подробнее» к нему.
        for item in near:
            short = self._short_torrent_label(item) or item.title[:40]
            message.actions.append(
                Action(
                    id=f"periodic-near-{item.id}",
                    label=f"Следить: {short}"[:60],
                    kind="periodic_pick",
                    payload={"task_id": task.id, "variant": f"near:{item.id}"},
                    group="Похожие варианты",
                )
            )
            message.actions.append(
                Action(
                    id=f"periodic-near-details-{item.id}",
                    label="Подробнее",
                    kind="details",
                    payload={"task_id": task.id, "torrent_id": item.id},
                    group="Похожие варианты",
                )
            )
        message.actions.append(
            Action(
                id="cancel-periodic",
                label="Отмена",
                kind="cancel",
                payload={"task_id": task.id},
                style="danger",
            )
        )

    async def _periodic_summary(self, task: Task, turn: Turn) -> None:
        """Второй шаг: резюме выбранного варианта и выбор режима слежения.

        Здесь наконец спрашиваются «Качать сразу и сообщать» / «Только
        сообщать»: человек уже подтвердил, за чем следить.
        """
        task.state = "awaiting_confirm"
        next_at = _parse_iso(task.next_check_at)
        pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        episode_from = task.params.get("episode_from")
        try:
            min_episode = int(episode_from) if episode_from not in (None, "") else None
        except (TypeError, ValueError):
            min_episode = None
        matched = (
            filter_candidates(pool, task.params, min_episode=min_episode) if pool else []
        )
        conditions = self._filters_text(task.params)

        lines = [
            f"Вы выбрали: «{task.title}».",
            f"• Условия: {conditions}",
        ]
        watch = self._watch_text(task)
        if watch and watch != conditions:
            lines.append(f"• Ждём: {watch}")
        if task.interval_minutes:
            lines.append(f"• Проверка: {self._human_interval(task.interval_minutes)}")
        if matched:
            lines.append(
                f"• Сейчас подходит: {_plural(len(matched), 'раздача', 'раздачи', 'раздач')}"
            )
            lines.append(f"    {matched[0].title[:100]}")
        else:
            lines.append("• Сейчас подходит: ничего — сообщу, когда появится")
        if next_at is not None:
            lines.append(f"• Первая проверка {self._clock(next_at)}")
        lines.append("")
        lines.append("Как следить за новым?")

        message = turn.add(
            "\n".join(lines),
            kind="task",
            task_id=task.id,
            payload={"task": task.to_dict()},
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
                id="periodic-back",
                label="Другой вариант",
                kind="periodic_back",
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

    async def _start_periodic(
        self, session_id: str, text: str, parsed: dict[str, Any], turn: Turn
    ) -> None:
        if not parsed.get("title"):
            turn.add("Не понял, за чем следить. Напишите название фильма или сериала.")
            return

        interval = parsed.get("interval_minutes") or self.settings.default_interval_minutes
        interval = max(self.settings.min_interval_minutes, int(interval))
        # Вторая задача на тот же сериал — это два независимых «жду серию N»:
        # они качают одно и то же дважды и расходятся в прогрессе. Если такая
        # уже следит, правим её.
        same = await self._same_periodic_title(session_id, str(parsed.get("title") or ""))
        if same is not None:
            merged = dict(same.params or {})
            for key, value in parsed.items():
                if key in PARAM_KEYS and value not in (None, "", "null", []):
                    merged[key] = value
            same.params = merged
            same.user_query = text
            if same.interval_minutes != interval:
                same.interval_minutes = interval
            same.auto_download = bool(parsed.get("auto_download"))
            if same.state not in ("monitoring",):
                same.state = "monitoring"
            self._schedule_next(same)
            await self.store.save_task(same)
            journal.add(
                "agent",
                "второй периодической задачи не завожу — правлю существующую",
                {"задача": same.id, "название": same.title, "интервал": interval},
            )
            turn.task = same
            turn.add(
                f"За «{same.title}» уже слежу — правлю эту задачу, вторую не завожу.",
                kind="text",
                task_id=same.id,
            )
            task = same
        else:
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
            torrents, suggestions = await self._search_with_suggestions(
                task.params, original=text
            )
        except MCPError as exc:
            task.state = "failed"
            task.last_error = str(exc)
            await self.store.save_task(task)
            turn.add(f"Не смог обратиться к трекеру: {exc}", kind="error", task_id=task.id)
            return

        task.options = {
            "pool": [item.to_dict() for item in torrents[:80]],
            "suggestions": suggestions,
        }
        await self._present_periodic(task, torrents, turn)

    def _record_start_baseline(self, task: Task) -> None:
        """Фиксирует «что уже есть на трекере» по итоговым условиям.

        Условия могли измениться, если человек выбрал похожий вариант, поэтому
        базовая линия пишется в момент старта, а не при создании задачи.
        """
        pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        if pool:
            self._record_baseline(task, filter_candidates(pool, task.params))

    async def _monitor_after_creation(self, task: Task, turn: Turn, downloaded_now: bool) -> None:
        self._record_start_baseline(task)
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
            f"Буду проверять {self._human_interval(task.interval_minutes)} {mode}.\n"
            f"{self._filter_text(task)}"
        )

    def _task_actions(self, task: Task) -> list[Action]:
        """Кнопки под сообщением задачи.

        У задачи, которая уже следит (`monitoring`), отмены здесь нет: она живёт
        только в карточке «В работе» — один пульт на одно действие. Пока задача
        ждёт подтверждения или качается, кнопка остаётся в чате: карточка может
        быть вне поля зрения (например, на телефоне панель закрыта).
        """
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
            return actions
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
        """Файлы раздачи: из сохранённого состава или из уже скачанного .torrent."""
        raw = (task.options or {}).get("files") or []
        if raw:
            return parse_files(raw)
        path_str = (task.options or {}).get("torrent_path") or ""
        if path_str:
            path = Path(path_str)
            if path.exists():
                try:
                    return parse_files(file_list(path))
                except Exception:  # noqa: BLE001 - состав не критичен
                    return []
        return []

    async def _fetch_torrent(
        self, torrent: Torrent, *, refresh: bool = False
    ) -> tuple[Path | None, str, str]:
        """Сохраняет .torrent на внешний диск. Возвращает (путь, ошибка, причина).

        Причина пустая при успехе, «disk» — если недоступен внешний диск,
        «tracker» — если файл не выдал сам трекер (лимит скачиваний, слетевшая
        сессия). Раньше ошибка трекера вылетала исключением и превращалась
        в 500 без внятного текста.

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
            return None, f"папка для torrent-файлов недоступна: {exc}", "disk"

        path = self.settings.torrents_dir / f"{torrent.id}.torrent"
        if not refresh and path.exists() and path.stat().st_size > 0:
            return path, "", ""  # уже скачан — второй раз не тянем

        try:
            result = await self.mcp.download(torrent.id, path)
        except MCPError as exc:
            return None, str(exc), "tracker"
        if not result.get("success"):
            message = str(result.get("message") or "не удалось скачать torrent-файл")
            return None, message, "tracker"
        return path, "", ""

    def _mark_problem(self, task: Task, error: str) -> None:
        """Запоминает ошибку задачи.

        Периодическая задача при сбое не закрывается: она остаётся в работе
        и просто пробует снова в свой срок. «Упавшая» задача выпадала в
        завершённые, и человек терял её из виду.
        """
        task.last_error = error
        if task.kind == "periodic":
            task.state = "monitoring"
            self._schedule_next(task)
        else:
            task.state = "failed"

    async def _report_tracker_problem(
        self, task: Task, torrent: Torrent, error: str, turn: Turn
    ) -> None:
        """Трекер не отдал .torrent: объясняем причину и даём кнопку «Повторить»."""
        self._mark_problem(task, error)
        await self.store.save_task(task)
        text = (
            "Трекер не выдал torrent-файл, поэтому скачать пока нечего.\n\n"
            f"• {torrent.title}\n{error}"
        )
        lowered = error.lower()
        if "лимит" in lowered or "количество торрент-файлов" in lowered or "использовали" in lowered:
            text += (
                "\n\nЭто ограничение kinozal.me: на аккаунте кончился дневной лимит "
                "торрент-файлов. Счётчик обнуляется в «Управление голосами» на "
                "трекере (за голоса) или сам на следующий день."
            )
        if "вход" in lowered or "войдите" in lowered:
            text += "\n\nПохоже, сессия на трекере слетела — стоит заново войти на kinozal.me."
        if task.kind == "periodic":
            interval = self._human_interval(
                task.interval_minutes or self.settings.default_interval_minutes
            )
            text += (
                f"\n\nЗадача продолжает работать: следующая проверка через {interval}, "
                "и я попробую снова."
            )
        message = turn.add(text, kind="error", task_id=task.id, payload={"task": task.to_dict()})
        if task.kind == "periodic":
            message.actions = [
                Action(
                    id="check-now",
                    label="Проверить сейчас",
                    kind="check_now",
                    payload={"task_id": task.id},
                    style="primary",
                ),
                Action(
                    id="cancel-fetch",
                    label="Отмена",
                    kind="cancel",
                    payload={"task_id": task.id},
                    style="danger",
                ),
            ]
        else:
            message.actions = [
                Action(
                    id="retry-fetch",
                    label="Повторить",
                    kind="confirm_download",
                    payload={"task_id": task.id, "torrent_id": torrent.id},
                    style="primary",
                ),
                Action(
                    id="cancel-fetch",
                    label="Отмена",
                    kind="cancel",
                    payload={"task_id": task.id},
                    style="danger",
                ),
            ]

    async def _download_with_report(
        self, task: Task, torrent: Torrent, turn: Turn, periodic_context: bool = False
    ) -> None:
        """Шаг 1: разобрать файлы и показать полную карточку.

        Здесь выясняется, что именно попадёт на диск: одна серия или вся пачка,
        сколько это весит и хватает ли места. Если серия не названа, а в раздаче
        их несколько — предлагаем выбрать кликабельными кнопками.

        Состав берём со страницы трекера (без .torrent): человек может просто
        смотреть варианты, а дневной лимит тратится только на саму загрузку.
        """
        task.state = "downloading"
        await self.store.save_task(task)
        journal.add(
            "agent",
            f"готовлю скачивание: {torrent.title}",
            {"id": torrent.id, "качество": torrent.quality, "озвучка": torrent.voice},
        )

        listing = await self._listing_entries(torrent)
        path: Path | None = None
        if listing is not None:
            entries = parse_files(listing)
        else:
            path, error, reason = await self._fetch_torrent(torrent)
            if path is None:
                if reason == "tracker":
                    await self._report_tracker_problem(task, torrent, error, turn)
                    return
                task.state = "failed"
                task.last_error = error
                await self.store.save_task(task)
                await self._report_storage_problem(
                    task, torrent, {"error": error, "reason": "disk", "path": None}, turn
                )
                return
            entries = parse_files(file_list(path))
        plan = self._plan_files(task, entries, torrent)
        task.options = {
            **(task.options or {}),
            "files": [
                {"index": entry.index, "name": entry.name, "size": entry.size,
                 "season": entry.season, "episode": entry.episode}
                for entry in entries[:400]
            ],
            "plan": plan,
            **({"torrent_path": str(path)} if path is not None else {}),
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
    def _plan_files(
        self, task: Task, entries: list[Any], torrent: Torrent | None = None
    ) -> dict[str, Any]:
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
        # Сериал определяем по названию раздачи, а не по именам файлов: в
        # коллекции фильмов файлы тоже нумеруются («Пила 1…», «Пила 2…»), и
        # раньше такая коллекция превращалась в «список серий».
        wanted_type = str(params.get("type") or "").lower()
        is_series = True
        if torrent is not None:
            is_series = bool(torrent.seasons or torrent.episodes)
        if wanted_type == "movie":
            is_series = False
        if wanted_type == "series":
            is_series = True
        if not is_series:
            pack = False

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
                    "selected_episodes": [
                        {
                            "season": picked["season"] if picked["season"] is not None else season,
                            "episode": picked["episode"],
                        }
                    ],
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

    # ------------------------------------------------------------------
    # Что видно о раздаче: сигналы, предупреждения, ссылка на трекер
    # ------------------------------------------------------------------
    def _torrent_link(self, torrent_id: str) -> str:
        """Адрес страницы раздачи на трекере — туда, где комментарии."""
        base = str(getattr(self.settings, "kinozal_base_url", "") or "https://kinozal.me")
        return f"{base.rstrip('/')}/details.php?id={str(torrent_id).strip()}"

    @staticmethod
    def _signals_line(torrent: Torrent) -> str:
        """«36,8 ГБ · 12 сидов · 2 дня назад» — короткие факты о раздаче.

        Одна строка, по которой видно, стоит ли брать: вес, живые сиды и
        возраст. Без неё «1080p» и «1080p» выглядят одинаково.
        """
        bits: list[str] = []
        if torrent.size:
            bits.append(_comma(torrent.size))
        if torrent.seeds or torrent.peers:
            bits.append(_plural(torrent.seeds, "сид", "сида", "сидов"))
        else:
            bits.append("без сидов")
        age = _age_text(torrent.uploaded_ts)
        if age:
            bits.append(age)
        return " · ".join(bits)

    @staticmethod
    def _torrent_warnings(torrent: Torrent) -> list[str]:
        """Что важно знать до скачивания: мёртвая раздача, экранка."""
        warnings: list[str] = []
        if torrent.screener:
            warnings.append("⚠ Экранка (CAM/TS): снято с экрана — картинка и звук хуже релиза.")
        if not (torrent.seeds or torrent.peers):
            warnings.append("⚠ Сидов нет — качать будет нечем. Лучше взять другую раздачу.")
        elif torrent.seeds <= 2:
            warnings.append(f"⚠ Мало сидов ({torrent.seeds}) — скорость будет низкой.")
        return warnings

    def _option_variants(
        self,
        found: list[Torrent],
        dimension: str,
        values: list[str],
        params: dict[str, Any] | None = None,
    ) -> list[tuple[str, Torrent | None, int]]:
        """(подпись варианта, лучшая раздача, сколько их) по каждому значению.

        Одна функция и для текста подсказок, и для кликабельных строк: иначе
        список и кнопки под ним начинают расходиться.
        """
        params = params or {}
        out: list[tuple[str, Torrent | None, int]] = []
        for value in values[:8]:
            if dimension == "quality":
                subset = [item for item in found if quality_match(value, item.qualities)]
                label = self._quality_label(value)
            else:
                subset = [item for item in found if voice_match(value, item.voices)]
                label = value
            out.append((label, rank(subset, params)[0] if subset else None, len(subset)))
        return out

    def _option_summaries(
        self,
        found: list[Torrent],
        dimension: str,
        values: list[str],
        params: dict[str, Any] | None = None,
    ) -> list[str]:
        """Под каждым вариантом — что он реально даёт: вес, сиды, возраст.

        «1080p — 5 раздач, лучшая: 36,8 ГБ · 12 сидов · 2 дня назад». Так
        выбор качества перестаёт быть выбором вслепую.
        """
        lines: list[str] = []
        for label, best, count in self._option_variants(found, dimension, values, params):
            if best is None:
                lines.append(f"• {label} — вариантов нет")
                continue
            detail = self._signals_line(best)
            line = f"• {label} — {_plural(count, 'раздача', 'раздачи', 'раздач')}"
            if detail:
                line += f", лучшая: {detail}"
            lines.append(line)
        return lines

    def _option_rows(
        self,
        task: Task,
        found: list[Torrent],
        dimension: str,
        values: list[str],
        params: dict[str, Any] | None = None,
    ) -> list[Action]:
        """Кликабельные строки по вариантам: клик — карточка лучшей раздачи."""
        rows: list[Action] = []
        for label, best, count in self._option_variants(found, dimension, values, params):
            if best is None:
                continue
            hint = f"{_plural(count, 'раздача', 'раздачи', 'раздач')}"
            signals = self._signals_line(best)
            if signals:
                hint += f" · {signals}"
            rows.append(self._variant_row(best.id, label, hint=hint, task_id=task.id))
        return rows

    async def torrent_details(self, torrent_id: str) -> dict[str, Any]:
        """Подробности раздачи для кнопки «Подробнее»: жанр, актёры, описание.

        Запрашивается только по нажатию: обычный поиск не должен ждать ещё
        один запрос через шлюз Cloudflare. Ответ кэшируется на 10 минут.
        """
        torrent_id = str(torrent_id or "").strip()
        if not torrent_id.isdigit():
            raise MCPError(f"«{torrent_id}» не похоже на номер раздачи")

        cached = self._details_cache.get(torrent_id)
        now = time.monotonic()
        if cached and now - cached[0] < self._details_cache_ttl:
            return cached[1]

        data = await self.mcp.details(torrent_id)
        result = {
            "id": torrent_id,
            "title": str(data.get("title") or ""),
            "original_title": str(data.get("originalTitle") or ""),
            "year": str(data.get("year") or ""),
            "genre": str(data.get("genre") or ""),
            "director": str(data.get("director") or ""),
            "cast": _shorten(str(data.get("cast") or ""), 300),
            "description": str(data.get("description") or ""),
            "quality": str(data.get("quality") or ""),
            "video": str(data.get("video") or ""),
            "audio": str(data.get("audio") or ""),
            "size": str(data.get("size") or ""),
            "seeds": int(data.get("seeds") or 0),
            "peers": int(data.get("peers") or 0),
            "upload_date": str(data.get("uploadDate") or ""),
            "update_date": str(data.get("updateDate") or ""),
            "url": self._torrent_link(torrent_id),
        }
        # Кэш не растёт бесконечно: держим последние 50 раздач.
        if len(self._details_cache) > 50:
            self._details_cache.clear()
        self._details_cache[torrent_id] = (now, result)
        return result

    def _confirm_card_text(self, task: Task, torrent: Torrent, plan: dict[str, Any]) -> str:
        """Полная информация перед скачиванием: что, сколько и куда."""
        storage = self.transmission.storage_info()
        info = storage.to_dict()
        needed = int(plan.get("size") or plan.get("total_size") or 0)
        reserve = self.transmission.min_free_bytes()
        fits = storage.ok and storage.free >= needed + reserve

        lines = [f"Раздача: {torrent.title}", ""]
        # Мёртвую раздачу видно до подтверждения, а не через час ожидания.
        warnings = self._torrent_warnings(torrent)
        for warning in warnings:
            lines.append(warning)
        if warnings:
            lines.append("")
        crowd: list[str] = []
        if torrent.seeds or torrent.peers:
            crowd.append(f"Сиды: {torrent.seeds}")
            if torrent.peers:
                crowd.append(f"пиры: {torrent.peers}")
        if torrent.comments:
            crowd.append(f"обсуждали {torrent.comments}")
        if crowd:
            lines.append("• " + " · ".join(crowd))
        if torrent.qualities:
            lines.append("• Качество: " + ", ".join(torrent.qualities[:3]))
        if torrent.voices:
            lines.append("• Озвучка: " + ", ".join(torrent.voices[:3]))
        selected_episodes = self._as_pairs(plan.get("selected_episodes"))
        if plan.get("season") or plan.get("episode") or selected_episodes:
            text = self._episodes_text(selected_episodes)
            if not text:
                season = plan.get("season")
                text = f"сезон {season}, серия {plan.get('episode')}" if season \
                    else f"серия {plan.get('episode')}"
            lines.append(f"• {text[0].upper()}{text[1:]}")
        if torrent.year:
            lines.append(f"• Год: {torrent.year}")
        if torrent.size:
            # Возраст и автор — рядом с весом: по ним видно, свежий это релиз
            # и кто его залил.
            line = f"• На трекере: {_comma(torrent.size)}"
            age = _age_text(torrent.uploaded_ts)
            if age:
                line += f" · залит {age}"
            elif torrent.upload_date:
                line += f" · залит {_comma(torrent.upload_date)}"
            if torrent.uploader:
                line += f" · залил {torrent.uploader}"
            lines.append(line)

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
        return "\n".join(lines)


    def _plan_from_episodes(self, entries: list[Any], plan: dict[str, Any]) -> dict[str, Any] | None:
        """Пересобирает план серий по настоящему .torrent (когда индексы разошлись)."""
        pairs = self._as_pairs(plan.get("selected_episodes") or plan.get("episodes"))
        if not pairs:
            return None
        fallback_season = plan.get("season")
        wanted: list[int] = []
        names: list[str] = []
        size = 0
        seasons: set[int] = set()
        selected: list[dict[str, Any]] = []
        for pair in pairs:
            season = pair["season"] if pair["season"] is not None else fallback_season
            episode = pair["episode"]
            picked = pick_episode(entries, season=season, episode=episode)
            if not picked:
                continue
            wanted.extend(picked.get("indexes") or [])
            size += int(picked.get("size") or 0)
            names.extend(picked.get("names") or [])
            picked_season = picked.get("season") if picked.get("season") is not None else season
            if picked_season is not None:
                seasons.add(int(picked_season))
            selected.append({"season": picked_season, "episode": int(picked.get("episode") or episode)})
        if not wanted:
            return None
        selected.sort(key=lambda item: (item.get("season") or 0, item["episode"]))
        return {
            **plan,
            "kind": "episode",
            "wanted": sorted(set(wanted)),
            "size": size,
            "names": names,
            "season": next(iter(seasons)) if len(seasons) == 1 else None,
            "episode": max(item["episode"] for item in selected),
            "selected_episodes": selected,
            "episodes": selected,
            "total_files": len(entries),
            "total_size": sum(entry.size for entry in entries),
        }

    def _season_pack(self, task: Task, best: Torrent) -> Torrent | None:
        """Раздача «весь сезон» под те же условия поиска, если она есть в пуле.

        Нужна, когда в карточке выбрана одна серия: обычно человек хочет
        скачать сезон целиком, и это должна быть отдельная кнопка, а не
        «вариант 4» без подписи.
        """
        pool = [Torrent.from_dict(item) for item in (task.options or {}).get("pool", [])]
        params = task.params or {}
        want_quality = str(params.get("quality") or best.quality or "")
        want_season = best.season if best.season is not None else params.get("season")
        episodes = set(best.episodes or [])
        series = best.series_key or ""
        candidates: list[Torrent] = []
        for item in pool:
            if item.id == best.id:
                continue
            if len(set(item.episodes or [])) < 2:
                continue
            if want_quality and item.quality != want_quality:
                continue
            if series and item.series_key and item.series_key != series:
                continue
            if want_season is not None and item.season is not None:
                if int(item.season) != int(want_season):
                    continue
            if episodes and not episodes.issubset(set(item.episodes or [])):
                continue
            candidates.append(item)
        return max(candidates, key=lambda item: item.seeds, default=None)

    @staticmethod
    @staticmethod
    def _years_text(items: list[Torrent]) -> str:
        """Годы раздачи: «2021», «2021–2024» или пусто."""
        years: set[int] = set()
        for item in items:
            raw = str(item.year or "").strip()
            if not raw:
                continue
            for low, high in re.findall(r"((?:19|20)\d{2})(?:\s*[-–]\s*((?:19|20)\d{2}))?", raw):
                start = int(low)
                years.add(start)
                if high:
                    years.add(int(high))
        if not years:
            return ""
        low, high = min(years), max(years)
        return str(low) if low == high else f"{low}–{high}"

    @staticmethod
    def _short_torrent_label(torrent: Torrent) -> str:
        """Короткая подпись раздачи для кнопки и списка: «2026 · серия 4 · 1080p · 5,7 ГБ»."""
        bits: list[str] = []
        episodes = sorted({int(item) for item in (torrent.episodes or [])})
        season = torrent.season
        if len(episodes) > 1:
            bits.append(f"сезон {season}, серии {episodes[0]}–{episodes[-1]}" if season
                        else f"серии {episodes[0]}–{episodes[-1]}")
        elif episodes:
            bits.append(f"серия {episodes[0]}")
        elif season:
            bits.append(f"сезон {season}")
        if torrent.year:
            bits.append(str(torrent.year))
        if torrent.quality:
            bits.append(torrent.quality)
        if torrent.size:
            bits.append(_comma(torrent.size))
        return " · ".join(bits)

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
        actions.extend(self._torrent_actions(task))
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

    def _torrent_actions(
        self,
        task: Task,
        torrent: Torrent | None = None,
        torrent_id: str = "",
    ) -> list[Action]:
        """Кнопки «посмотреть раздачу»: подробности и страница на трекере.

        Работают на любом шаге: пока раздача не выбрана, передаём ту, о которой
        идёт речь (лучшую из найденных на этом шаге) — «что это за фильм»
        хочется прочитать ДО выбора качества, а не после.

        «Подробнее» открывается клиентом (`/api/torrent/<id>/details`): это не
        действие над задачей, поэтому в чат ничего не пишется.
        """
        identifier = (
            torrent_id
            or (torrent.id if torrent is not None else "")
            or str((task.candidate or {}).get("id") or "")
        ).strip()
        if not identifier:
            return []
        # Одна строка вместо двух кнопок: «Подробнее» открывает окно, а ссылка на
        # трекер живёт уже внутри него — но только если человек захочет.
        return [self._variant_row(identifier, "Подробнее о раздаче", task_id=task.id)]

    @staticmethod
    def _variant_row(
        torrent_id: str, label: str, *, hint: str = "", task_id: str = ""
    ) -> Action:
        """Кликабельная строка-вариант: открывает окно «Подробнее».

        Раньше у каждой раздачи в списке висели две кнопки — «Подробнее» и
        «Открыть на трекере». Теперь список читается как список: клик по строке
        открывает карточку раздачи, а ссылка на трекер — внутри неё.
        """
        text = label if not hint else f"{label} — {hint}"
        return Action(
            id=f"details-{torrent_id}",
            label=text[:140],
            kind="details",
            payload={"task_id": task_id, "torrent_id": torrent_id, "row": True},
            group="Варианты",
        )

    def _best_torrent(self, torrents: list[Torrent], params: dict[str, Any] | None = None) -> Torrent | None:
        """Лучшая раздача из списка — о ней и показываем подробности."""
        if not torrents:
            return None
        return rank(torrents, params or {})[0]

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
        # Человек подтвердил скачивание — прошлая переписка больше не нужна.
        await self._reset_context(task.session_id)
        path_str = (task.options or {}).get("torrent_path") or ""
        path = Path(path_str) if path_str else self.settings.torrents_dir / f"{torrent.id}.torrent"
        if not path.exists():
            # Карточка показывается без .torrent (чтобы не тратить дневной лимит),
            # поэтому файл забираем здесь — когда человек действительно качает.
            fetched, error, reason = await self._fetch_torrent(torrent)
            if fetched is None:
                if reason == "tracker":
                    await self._report_tracker_problem(task, torrent, error, turn)
                else:
                    turn.add(
                        f"Torrent-файл недоступен: {error}",
                        kind="error",
                        task_id=task.id,
                    )
                return
            path = fetched
            task.options = {**(task.options or {}), "torrent_path": str(path)}
            await self.store.save_task(task)

        # Список со страницы и настоящий .torrent могут разойтись порядком
        # файлов. Тогда план по индексам неверен — пересобираем по сериям.
        if plan.get("total_files"):
            entries = parse_files(file_list(path))
            expected = int(plan.get("total_files") or 0)
            if len(entries) != expected:
                journal.add(
                    "agent",
                    "файлы в .torrent не совпали со списком со страницы",
                    {"ожидалось": expected, "в торренте": len(entries)},
                    level="warn",
                )
                rebuilt = self._plan_from_episodes(entries, plan)
                if rebuilt is not None:
                    plan = rebuilt
                    task.options = {**(task.options or {}), "plan": plan}
                    await self.store.save_task(task)

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

        # Раздача только что встала в очередь: показываем процесс, а не «готово».
        progress = {}
        torrent_hash = result.get("transmission_hash") or ""
        if torrent_hash:
            task.params["transmission_hash"] = torrent_hash
            try:
                progress = ((await self.transmission.progress([torrent_hash])) or {}).get(torrent_hash) or {}
            except Exception:  # noqa: BLE001 - прогресс не критичен
                progress = {}
        # «Готово» ставим только когда Transmission прямо сказал, что всё
        # скачано. Раньше отсутствие данных о прогрессе (RPC ещё не ответил
        # про только что добавленную раздачу) считалось завершением — задача
        # уходила в «готово» и больше не опрашивалась, хотя файл качался.
        finished = bool(progress) and bool(progress.get("done"))
        if progress:
            task.params["progress"] = progress
            plan = {**plan, "progress": progress}
            result = {**result, "progress": progress}
        if torrent_hash and progress.get("done"):
            # Раздача уже докачана (мелкий файл или всё лежало на диске):
            # сразу попадает в «Скачано», не дожидаясь опроса прогресса.
            await self.store.mark_downloads_done([torrent_hash])
        if periodic_context:
            task.state = "monitoring"
            self._schedule_next(task)
        elif finished:
            task.state = "done"
        else:
            task.state = "downloading"
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
        if result.get("ok") and result.get("needs_choice"):
            # Пачка без выбранной серии: показываем выбор, а не качаем всё.
            groups = result.get("groups") or []
            path_str = str(result.get("path") or "")
            await self._show_episode_picker(
                task, torrent, groups, Path(path_str) if path_str else None, turn
            )
            return
        if result.get("ok") and result.get("skipped"):
            turn.add(
                f"«{torrent.title}» — {result['reason']}. Продолжаю следить.",
                kind="text",
                task_id=task.id,
            )
            return
        if not result["ok"]:
            if result.get("reason") == "tracker":
                await self._report_tracker_problem(task, torrent, str(result["error"]), turn)
            elif result.get("retryable"):
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
        progress = result.get("progress") or {}
        in_progress = bool(progress) and not progress.get("done")
        lines = [("Качается: " if in_progress else "Готово: ") + f"«{torrent.title}»"]
        picked = self._as_pairs(plan.get("selected_episodes") or plan.get("episodes"))
        if picked:
            what = self._episodes_text(picked)
            lines.append(f"Качаю {what} — {human_bytes(plan.get('size'))}")
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
            self._mark_problem(task, str(exc))
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

        # Запоминаем скачанное. Главное правило: раздача уже стоит в очереди
        # Transmission, поэтому ЛЮБАЯ ошибка записи не должна ронять задачу —
        # иначе получится «торрент в Transmission есть, а агент про него не
        # знает» (именно так однажды и потерялась запись).
        try:
            await self._remember_download(task, torrent, path, plan, transmission_hash,
                                          str(added.get("name") or ""))
        except Exception as exc:  # noqa: BLE001
            log.exception("не удалось запомнить скачанное")
            journal.add(
                "agent",
                "скачанное не записано в память",
                {
                    "раздача": torrent.title,
                    "хеш": transmission_hash,
                    "ошибка": f"{type(exc).__name__}: {exc}",
                },
                level="error",
            )
            task.last_error = f"запись в память не удалась: {exc}"

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
            "transmission_hash": transmission_hash,
            "plan": plan,
        }

    async def _remember_download(
        self,
        task: Task,
        torrent: Torrent,
        path: Path,
        plan: dict[str, Any],
        transmission_hash: str,
        torrent_name: str,
    ) -> None:
        """Пишет в память одну запись на раздачу, серии — списком внутри неё.

        «Весь сезон» — это одна раздача и одна строка в списке «Скачано на
        диск», а не пять строк по числу серий. Какие именно серии взяты, лежит
        в `episodes` записи: по ним считается прогресс («какую серию ждём
        дальше») и по ним же задача не скачивает серию повторно.
        """
        raw_selected = plan.get("selected_episodes") or (
            [plan["episode"]] if plan.get("episode") is not None else []
        )
        pairs = self._as_pairs(raw_selected)
        if not pairs and len(torrent.seasons) <= 1 and (torrent.episodes_total or torrent.seasons):
            # Вся раздача целиком (весь сезон или мини-сериал) без выбранной
            # серии: серии берём из файлов, которые реально качаем, а не из
            # названия. В названии бывает «из 20», а в раздаче — один выпуск:
            # записать тогда 20 серий значит соврать о прогрессе (из-за этого
            # задача потом «ждала 21-ю серию»).
            groups = plan.get("episodes") or []
            if len(groups) > 1:
                pairs = [
                    {"season": group.get("season"), "episode": group.get("episode")}
                    for group in groups
                    if group.get("episode") is not None
                ]
        fallback_season = plan.get("season")
        episodes: list[dict[str, Any]] = []
        for pair in pairs:
            season_value = pair["season"] if pair["season"] is not None else fallback_season
            try:
                season_value = int(season_value) if season_value is not None else None
            except (TypeError, ValueError):
                season_value = None
            episodes.append({"season": season_value, "episode": int(pair["episode"])})
        await self.store.record_download(
            task,
            torrent,
            str(path),
            transmission_hash,
            torrent_name=torrent_name,
            episodes=episodes or None,
        )

    async def _download(self, task: Task, torrent: Torrent) -> dict[str, Any]:
        """Автоматический путь (периодические задачи): без диалога.

        Качаем ровно те серии, ради которых задача заведена: начиная с
        `episode_from` и не ниже той, что идёт за уже скачанной. Если раздача —
        пачка на 14 выпусков, где сейчас 1–2, а нужна третья, то при появлении
        «1–3» скачается только третий файл.

        Порядок важен из-за суточного лимита торрент-файлов: сначала состав
        раздачи спрашиваем у страницы трекера (`get_file_list`, лимит не
        тратит) и только если нужная серия там правда есть — качаем `.torrent`.
        Иначе каждая проверка «на всякий случай» съедала бы по одному файлу
        лимита, даже когда качать нечего.
        """
        params = task.params or {}
        try:
            season = int(params["season"]) if params.get("season") is not None else None
        except (TypeError, ValueError):
            season = None
        next_episode = int(params.get("next_episode") or 0)
        if not next_episode:
            # Прогресс берём из базы (удалённые серии тоже учитываются).
            progress: dict[int, int] = {}
            if torrent.series_key:
                progress = await self.store.episodes_by_season(torrent.series_key)
            next_episode = self._next_episode(params, progress)

        # 1) Бесплатная проверка: что реально лежит в раздаче.
        preview_entries = parse_files(await self._listing_entries(torrent) or [])
        numbered = [entry for entry in preview_entries if entry.episode is not None]
        if next_episode and numbered:
            # Решаем только когда номера серий видны в именах файлов. Если их
            # нет (бывает «Ставка на любовь .mkv»), судить по странице нельзя —
            # идём обычным путём и смотрим настоящий .torrent.
            preview = pick_new_episodes(
                numbered, after_episode=next_episode - 1, season=season
            )
            if not preview:
                # Нужной серии в раздаче нет — .torrent не тратим, лимит цел.
                journal.add(
                    "agent",
                    f"в раздаче пока нет серии {next_episode}: .torrent не качаю",
                    {"раздача": torrent.title, "файлов на странице": len(preview_entries)},
                )
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": f"серий с {next_episode} в раздаче пока нет",
                    "path": None,
                    "plan": {"kind": "episode", "wanted": [], "size": 0},
                }
            journal.add(
                "agent",
                f"на странице есть серии {preview.get('episodes')}: качаю .torrent",
                {"next_episode": next_episode, "размер": preview.get("size")},
            )

        # 2) Только теперь тратим торрент-файл: он нужен, чтобы поставить
        #    раздачу в очередь с точными индексами файлов.
        path, error, reason = await self._fetch_torrent(torrent, refresh=True)
        if path is None:
            self._mark_problem(task, error)
            await self.store.save_task(task)
            return {
                "ok": False,
                "error": error,
                "retryable": reason != "tracker",
                "reason": reason or "disk",
                "path": None,
            }

        entries = parse_files(file_list(path))
        plan = self._plan_files(task, entries, torrent)

        picked = None
        if next_episode:
            picked = pick_new_episodes(entries, after_episode=next_episode - 1, season=season)
        if picked and len(picked.get("episodes") or []) > self.auto_episodes_max:
            # Новых серий сразу много (догнали пачку) — не ставим всё молча.
            picked_episodes = {
                int(item) for item in (picked.get("episodes") or []) if item is not None
            }
            all_groups = [
                {
                    "season": group.get("season"),
                    "episode": group.get("episode"),
                    "indexes": group.get("indexes"),
                    "size": group.get("size"),
                    "names": group.get("names"),
                }
                for group in (plan.get("episodes") or [])
            ]
            new_groups = [
                group for group in all_groups
                if int(group.get("episode") or 0) in picked_episodes
                and (season is None or group.get("season") in (None, season))
            ]
            task.options = {**(task.options or {}), "episode_groups": all_groups}
            task.candidate = torrent.to_dict()
            new_size = human_bytes(sum(int(group.get("size") or 0) for group in new_groups))
            journal.add(
                "agent",
                "новых серий много: спрашиваю, а не качаю всё",
                {"новых": len(new_groups), "вес": new_size,
                 "серий в раздаче": len(all_groups), "порог": self.auto_episodes_max},
            )
            return {
                "ok": True,
                "skipped": True,
                "needs_choice": True,
                "reason": (
                    f"новых серий {_plural(len(new_groups), 'серия', 'серии', 'серий')} "
                    f"на {new_size}"
                ),
                "path": str(path),
                "plan": plan,
                "groups": all_groups,
                "new_groups": new_groups,
                "torrent": torrent,
            }
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
                    {"season": season, "episode": int(item)}
                    for item in (picked.get("episodes") or [])
                    if item is not None
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
        elif plan["kind"] == "choose" and plan.get("pack") and len(plan.get("episodes") or []) > 1:
            # Раздача — пачка серий, а какую брать, нигде не сказано (задача
            # заведена без серии и без качества). Вслепую качать всю пачку
            # нельзя: это легко десятки и сотни гигабайт. Спрашиваем.
            episodes = plan.get("episodes") or []
            groups = [
                {
                    "season": group.get("season"),
                    "episode": group.get("episode"),
                    "indexes": group.get("indexes"),
                    "size": group.get("size"),
                    "names": group.get("names"),
                }
                for group in episodes
            ]
            task.options = {**(task.options or {}), "episode_groups": groups}
            task.candidate = torrent.to_dict()
            total = human_bytes(sum(int(group.get("size") or 0) for group in episodes))
            journal.add(
                "agent",
                f"пачка без выбранной серии: не качаю вслепую ({torrent.title[:60]})",
                {"серий": len(episodes), "вес": total, "файлов": len(entries)},
            )
            return {
                "ok": True,
                "skipped": True,
                "needs_choice": True,
                "reason": (
                    f"в раздаче {_plural(len(episodes), 'серия', 'серии', 'серий')} "
                    f"на {total} — скажи, какие качать"
                ),
                "path": str(path),
                "plan": plan,
                "groups": groups,
                "torrent": torrent,
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
            self._mark_problem(task, str(exc))
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
    async def _find_delete_target(
        self, torrent_id: str = "", torrent_hash: str = ""
    ) -> dict[str, Any] | None:
        """Ищет запись для удаления: по id раздачи, по хешу или прямо в очереди."""
        record = await self.store.get_download(torrent_id) if torrent_id else None
        if record is None and torrent_hash:
            record = await self.store.get_download_by_hash(torrent_hash)
        if record is not None:
            return record
        if not torrent_hash:
            return None
        # Раздачи нет в нашей памяти — она могла быть добавлена прямо в Transmission.
        try:
            found = await self.transmission.find_by_hash(torrent_hash)
        except TransmissionError:
            found = None
        if not found:
            return None
        return {
            "torrent_id": "",
            "task_id": "",
            "title": found.get("name") or "раздача из Transmission",
            "name": found.get("name") or "",
            "size": human_bytes(int(found.get("total_size") or 0)),
            "torrent_path": "",
            "transmission_hash": torrent_hash,
            "source": "transmission",
        }

    async def request_delete(self, torrent_id: str, torrent_hash: str = "") -> Turn:
        """Спрашивает подтверждение: удаление файлов необратимо."""
        turn = Turn()
        record = await self._find_delete_target(torrent_id, torrent_hash)
        if record is None:
            turn.add(
                "Не нашёл эту раздачу ни в памяти агента, ни в очереди Transmission — "
                "обновите картотеку.",
                kind="error",
            )
            return turn
        if not torrent_id:
            torrent_id = str(record.get("torrent_id") or "")

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
        if record.get("episode"):
            lines.append(
                f"Серия {record['episode']} останется в памяти агента: периодическая "
                "задача продолжит со следующей и не станет качать её заново."
            )
        message = turn.add("\n".join(lines), kind="error", payload={"download": record})
        message.actions = [
            Action(
                id="delete-forever",
                label="Удалить навсегда",
                kind="confirm_delete",
                payload={"torrent_id": torrent_id, "hash": record.get("transmission_hash") or ""},
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

    async def _drop_torrent(
        self, torrent_id: str, torrent_hash: str, record: dict[str, Any]
    ) -> tuple[str, Any]:
        """Убирает раздачу из очереди и стирает её файлы (без корзины).

        Возвращает (ошибка, результат удаления). Общий шаг для «Удалить» и для
        отмены задачи: если раздача уже поставлена в очередь, отмена задачи
        значит и отмену скачивания.
        """
        from core.files import delete_paths

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
                    return f"Не удалось убрать раздачу из Transmission: {exc}", None

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
        return "", result

    async def perform_delete(self, torrent_id: str, torrent_hash: str = "") -> Turn:
        """Убирает раздачу из Transmission, стирает файлы и .torrent."""
        turn = Turn()
        record = await self._find_delete_target(torrent_id, torrent_hash)
        if record is None:
            turn.add("Запись уже удалена — обновите картотеку.", kind="error")
            return turn
        torrent_id = str(record.get("torrent_id") or torrent_id or "")
        torrent_hash = str(record.get("transmission_hash") or "") or torrent_hash

        error, result = await self._drop_torrent(torrent_id, torrent_hash, record)
        if error or result is None:
            turn.add(error or "Не удалось удалить раздачу.", kind="error")
            return turn

        # 3. Забываем раздачу: файлов больше нет (если о ней была запись).
        if torrent_id:
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
        if record.get("episode"):
            text += (
                f"\nСерия {record['episode']} осталась в памяти: следующая проверка "
                "возьмёт серию дальше, а не эту же."
            )
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

    @staticmethod
    def _progress_text(progress: dict[str, Any]) -> str:
        """«45,2% · 1,2 МБ/с · осталось 12 мин · 1,1 ГБ из 5,7 ГБ»."""
        if not progress:
            return ""
        bits = [f"{progress.get('percent', 0)}%"]
        if progress.get("rate_human"):
            bits.append(str(progress["rate_human"]))
        if progress.get("eta_human"):
            bits.append(f"осталось {progress['eta_human']}")
        if progress.get("downloaded_human") and progress.get("size_human"):
            bits.append(f"{progress['downloaded_human']} из {progress['size_human']}")
        return " · ".join(bits)

    async def refresh_progress(self) -> None:
        """Опрашивает Transmission и обновляет задачи в состоянии «скачивание».

        Transmission не присылает события — только отвечает, поэтому прогресс
        забираем опросом. Задача остаётся в «скачивании», пока файл не дойдёт до
        100%, и только потом уходит в завершённые.

        Смотрим и задачи в «готово»: если их раздача всё ещё качается (бывает,
        когда состояние выставили по неполным данным), возвращаем в
        «скачивание» — иначе интерфейс говорит «готова», а файл льётся.
        """
        tasks = await self.store.list_tasks(limit=200, states=["downloading", "done"])
        tasks = [
            task for task in tasks
            if task.state == "downloading"
            or not ((task.params or {}).get("progress") or {}).get("done")
        ]
        if not tasks:
            return
        hashes = [
            str((task.params or {}).get("transmission_hash") or "")
            for task in tasks
            if (task.params or {}).get("transmission_hash")
        ]
        progress = await self.transmission.progress([item for item in hashes if item])
        if progress is None:
            # Спросить не удалось (демон занят записью). Состояния не трогаем:
            # иначе задача «то в работе, то в завершённых».
            return

        for task in tasks:
            torrent_hash = str((task.params or {}).get("transmission_hash") or "")
            state = progress.get(torrent_hash)
            if state is None:
                # Раздачи нет в очереди. Один пустой опрос ничего не значит —
                # сразу после перезапуска Transmission сессия ещё грузится, —
                # поэтому «готово» ставим только со второго раза подряд.
                misses = int((task.params or {}).get("missing_polls") or 0) + 1
                task.params["missing_polls"] = misses
                if misses < 2:
                    await self.store.save_task(task)
                    continue
                task.params.pop("missing_polls", None)
                task.params.pop("progress", None)
                task.state = "done"
                task.last_result = "раздача больше не в очереди"
                await self.store.save_task(task)
                continue

            task.params.pop("missing_polls", None)
            task.params["progress"] = state
            task.last_result = self._progress_text(state)
            if task.state == "done" and not state.get("done"):
                # Раздача ещё качается — задача не завершена.
                task.state = "downloading"
                await self.store.save_task(task)
                journal.add(
                    "agent",
                    "задача возвращена в «скачивание»: торрент ещё не готов",
                    {"задача": task.id, "процент": state.get("percent")},
                )
                continue
            if state.get("done"):
                task.state = "done"
                if torrent_hash:
                    await self.store.mark_downloads_done([torrent_hash])
                await self.store.save_task(task)
                text = (
                    f"Скачано: «{state.get('name') or task.title}» "
                    f"({state.get('size_human') or 'размер неизвестен'})"
                )
                message = Message(role="agent", text=text, kind="task", task_id=task.id,
                                  payload={"task": task.to_dict(), "progress": state})
                await self._remember_many(task.session_id, [message])
                await self.events.publish(
                    {
                        "type": "download_complete",
                        "task_id": task.id,
                        "title": state.get("name") or task.title,
                        "text": text,
                        "message": message.to_dict(),
                    }
                )
                await self.desktop.send("Скачивание завершено", str(state.get("name") or "")[:120])
                continue
            await self.store.save_task(task)

    async def _progress_loop(self) -> None:
        while True:
            try:
                await self.refresh_progress()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - монитор не должен падать
                log.exception("ошибка при обновлении прогресса")
            await asyncio.sleep(self.progress_tick_seconds)

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
            if task.kind == "periodic" and task.state == "failed":
                # Возвращаем задачу в работу: сбой трекера — не приговор.
                task.state = "monitoring"
                await self.store.save_task(task)
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
            # Прошлая ошибка не должна висеть вечно: если трекер снова
            # отвечает, старая причина (например, суточный лимит) уже может
            # быть неактуальной. Скачивание запишет свою ошибку заново.
            task.last_error = ""

            by_title = filter_candidates(torrents, {"title": params.get("title") or ""})
            matched = filter_candidates(by_title, params)
            seen = set(params.get("seen_ids") or [])
            seen |= await self.store.downloaded_ids()

            # Никаких «похожих вместо подходящих»: если под условия ничего не
            # подходит, новых раздач нет. Иначе задача «Прайм-тайм 2026 в 4K»
            # качала фильм 2024 года.
            pool = matched
            series = str(pool[0].series_key) if pool else ""
            if not series:
                # Ничего не нашлось или трекер молчит — ключ сериала берём из
                # самой задачи. Иначе прогресс «забывается», и задача снова
                # ждёт уже скачанную серию (а иногда качает её повторно).
                series = str(params.get("series_key") or "")
            if series and params.get("series_key") != series:
                params["series_key"] = series
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
            if not season_episodes:
                # Прогресса под рукой нет: не откатываем номер назад к «начиная
                # с N», иначе задача ждёт серию, которая уже лежит на диске.
                next_episode = max(next_episode, int(params.get("next_episode") or 0))
            task.params["next_episode"] = next_episode

            fresh = find_new(
                matched,
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
                    "ждём серию": next_episode or None,
                },
            )
            # Отложенное скачивание: в прошлый раз трекер не отдал .torrent
            # (например, кончился суточный лимит). Пробуем снова на каждой
            # проверке, даже если нового ничего не появилось — иначе человек
            # жмёт «Проверить», а агент молчит.
            retried = await self._retry_pending(task, turn, manual=manual)
            if not fresh:
                await self.store.save_task(task)
                if not matched:
                    # Совсем ничего нет (сериал ещё не вышел) — раз в сутки
                    # сообщаем, что мониторинг продолжается.
                    await self._notify_nothing(task, manual=manual)
                if manual and not retried:
                    turn.add(self._checked_text(task, len(matched), 0), kind="text",
                             task_id=task.id)
                return

            task.params["seen_ids"] = list(seen | {t.id for t in fresh})[-200:]
            task.options = {**(task.options or {}), "pool": [t.to_dict() for t in fresh[:20]]}
            await self.store.save_task(task)

            if task.auto_download:
                for torrent in fresh[:3]:
                    # Эту раздачу уже скачал повтор отложенного — второй раз
                    # не ставим, иначе в очереди окажутся две копии.
                    if retried and torrent.id == retried:
                        continue
                    await self._download_quiet(task, torrent)
            else:
                await self._notify_new(task, fresh)

            if manual:
                turn.add(self._checked_text(task, len(matched), len(fresh)), kind="text",
                         task_id=task.id)

    def _checked_text(self, task: Task, found: int, new: int) -> str:
        """Ответ на «Проверить сейчас»: что смотрели и чего ждём.

        «Нового нет» само по себе ничего не объясняет: человек не понимает,
        ждёт ли задача 3-ю серию, или сломана.
        """
        head = f"Проверил «{task.title}»: "
        head += "нового нет" if not new else f"нового {new}"
        head += f" (подходящих раздач: {found})."
        watch = self._watch_text(task)
        if watch:
            head += f"\n• {watch}"
        next_at = _parse_iso(task.next_check_at)
        if next_at is not None:
            head += f"\n• следующая проверка {self._clock(next_at)}"
        return head

    @staticmethod
    def _clock(stamp: datetime) -> str:
        """«18:21» / «завтра в 18:21» / «20.10 в 18:21» в местном времени.

        Одного времени мало: у задачи раз в сутки «следующая проверка в 12:20»
        читается как «через час», хотя это завтра.
        """
        local = stamp.astimezone()
        now = datetime.now().astimezone()
        days = (local.date() - now.date()).days
        clock = local.strftime("%H:%M")
        if days <= 0:
            return f"в {clock}"
        if days == 1:
            return f"завтра в {clock}"
        return f"{local.strftime('%d.%m')} в {clock}"

    def _watch_text(self, task: Task) -> str:
        """Одной строкой: что именно отслеживает задача.

        Видно и фильтр, и какую серию ждём, и что скачивание ждёт повтора.
        """
        bits: list[str] = []
        params = task.params or {}
        if params.get("year"):
            bits.append(f"год {params['year']}")
        if params.get("quality"):
            bits.append(self._quality_label(str(params["quality"])))
        if params.get("voice"):
            bits.append(str(params["voice"]))
        if params.get("season"):
            bits.append(f"сезон {params['season']}")
        if task.kind == "periodic":
            episodes = params.get("next_episode")
            if params.get("episode_from") and not episodes:
                episodes = params.get("episode_from")
            if episodes:
                bits.append(f"жду серию {episodes}")
        if not bits and task.kind == "periodic":
            # Ни качества, ни сезона не задано — задача ловит любую новую
            # раздачу по названию. Так и пишем, чтобы строка не была пустой.
            bits.append("любая новая раздача")
        text = ", ".join(bits)
        pending = (task.params or {}).get("pending_download") or {}
        if pending:
            note = "скачивание ждёт повтора"
            error = str(pending.get("error") or "")[:90]
            if error:
                note += f" ({error})"
            text = f"{text}; {note}" if text else note
        return text

    async def _remember_pending(self, task: Task, torrent: Torrent, error: str) -> None:
        """Запоминает раздачу, которую не удалось скачать: повторим сами.

        Иначе после сброса суточного лимита .torrent человек жмёт «Проверить»,
        а задача отвечает «нового нет» — ведь раздача уже считается найденной.
        """
        task.params["pending_download"] = {
            "torrent": torrent.to_dict(),
            "error": str(error)[:200],
            "at": now_iso(),
        }

    def _clear_pending(self, task: Task) -> None:
        task.params.pop("pending_download", None)

    async def _retry_pending(self, task: Task, turn: Turn, *, manual: bool = False) -> str:
        """Повторяет отложенное скачивание.

        Возвращает id раздачи, если повтор удался (тогда в общем цикле её
        скачивать уже не нужно), иначе пустую строку.
        """
        pending = (task.params or {}).get("pending_download") or {}
        raw = pending.get("torrent")
        if not raw:
            return ""
        # Не тянем одну и ту же неудачу вечно: если с прошлой попытки прошла
        # неделя, раздача скорее всего уже не актуальна.
        stamp = _parse_iso(str(pending.get("at") or ""))
        if stamp is not None and datetime.now(timezone.utc) - stamp > timedelta(days=7):
            self._clear_pending(task)
            await self.store.save_task(task)
            journal.add("agent", f"отложенное скачивание устарело: {task.title}", {})
            return ""
        try:
            torrent = Torrent.from_dict(raw)
        except TypeError:
            self._clear_pending(task)
            return ""
        if not torrent.id:
            self._clear_pending(task)
            return ""
        journal.add("agent", f"повторяю отложенное скачивание: {torrent.title}",
                    {"раздача": torrent.id, "прошлая ошибка": pending.get("error")})
        if task.auto_download:
            await self._download_quiet(task, torrent)
        else:
            await self._notify_new(task, [torrent])
            return torrent.id
        ok = not (task.params or {}).get("pending_download")
        if ok:
            task.last_error = ""
            await self.store.save_task(task)
            return torrent.id
        if manual:
            reason = str(pending.get("error") or "трекер молчит").rstrip(".")
            turn.add(
                f"Повторить скачивание не удалось: {reason}. "
                "Попробую на следующей проверке.",
                kind="error",
                task_id=task.id,
            )
        return ""

    async def _download_quiet(self, task: Task, torrent: Torrent) -> None:
        """Новая серия по периодической задаче: качаем отдельной задачей.

        Периодическая задача — это только мониторинг. Нашла то, что ждала, —
        заводит разовую задачу-потомка и отдаёт скачивание ей. Так прогресс
        виден в «в работе», загрузку можно отменить кнопкой, а карточка
        мониторинга остаётся карточкой мониторинга.

        Завести другую задачу — единственное, что периодической задаче можно:
        без этого скачивание жило внутри мониторинга и нигде не отображалось.
        """
        child = await self._child_download_task(task, torrent)
        journal.add(
            "agent",
            f"периодическая задача завела скачивание: {torrent.title}",
            {
                "задача": task.id,
                "новая задача": child.id,
                "серия": (child.params or {}).get("next_episode"),
            },
        )
        try:
            result = await self._download(child, torrent)
        except Exception as exc:  # noqa: BLE001
            log.exception("тихое скачивание не удалось")
            await self._drop_child(child)
            await self._remember_pending(task, torrent, str(exc))
            await self.store.save_task(task)
            await self._notify_error(task, torrent, str(exc))
            return
        if not result["ok"]:
            await self._drop_child(child)
            await self._remember_pending(task, torrent, str(result.get("error") or ""))
            task.last_error = str(result.get("error") or "")
            await self.store.save_task(task)
            await self._notify_error(task, torrent, result.get("error") or "не удалось скачать")
            return
        if result.get("transmission_error") and not str(result.get("transmission_hash") or ""):
            # Раздача не встала в очередь (Transmission молчит или отказал):
            # оставлять задачу «качается» нельзя — прогресса не будет никогда.
            await self._drop_child(child)
            await self._remember_pending(task, torrent, str(result["transmission_error"]))
            task.last_error = str(result["transmission_error"])
            await self.store.save_task(task)
            await self._notify_error(task, torrent, str(result["transmission_error"]))
            return
        # Раздача — пачка серий, а какую брать, не сказано: спрашиваем, а не
        # качаем десятки гигабайт «на всякий случай». Спрашиваем уже в задаче-
        # потомке: выбор серий — это её скачивание.
        if result.get("needs_choice"):
            self._clear_pending(task)
            task.last_error = ""
            child.state = "awaiting_choice"
            child.last_result = "жду выбора серий"
            await self.store.save_task(child)
            sent = await self._ask_which_episodes(child, torrent, result, marker_task=task)
            if not sent:
                # Об этой пачке уже спрашивали — не заводим пустую задачу.
                await self._drop_child(child)
            else:
                task.last_result = (
                    f"нашёл серию {(child.params or {}).get('next_episode') or ''}: "
                    "спрашиваю, какие серии качать"
                ).strip()
            await self.store.save_task(task)
            return
        # Раздача есть, а нужной серии в ней нет: качать нечего и врать про
        # «скачал» нельзя. Снимаем отложенное и ждём настоящей серии.
        if result.get("skipped"):
            await self._drop_child(child)
            self._clear_pending(task)
            task.last_error = ""
            await self.store.save_task(task)
            journal.add(
                "agent",
                f"по задаче «{task.title}» качать пока нечего",
                {"раздача": torrent.title, "причина": result.get("reason")},
            )
            return

        # Успех: скачивание живёт отдельной задачей, мониторинг продолжается.
        found = (child.params or {}).get("next_episode")
        self._clear_pending(task)
        task.last_error = ""
        await self._finish_child(child, torrent, result)
        # У задачи-потомка своя переписка: открыл карточку — сразу видно, что
        # именно качается и по чьей задаче.
        await self._remember_many(
            child.session_id,
            [
                Message(
                    role="agent",
                    text=(
                        f"Качаю по задаче «{task.title}»"
                        + (f": серия {found}." if found else ".")
                        + f"\n• {torrent.title}\n{describe(torrent)}"
                    ),
                    kind="task",
                    task_id=child.id,
                    payload={"task": child.to_dict(), "torrent": torrent.to_dict()},
                )
            ],
        )
        if found:
            task.last_result = f"нашёл серию {found} — качает отдельная задача"
        else:
            task.last_result = "нашёл новую раздачу — качает отдельная задача"
        await self._advance_next_episode(task, torrent)
        await self.store.save_task(task)

        lines = [f"Новая серия по задаче «{task.title}»: нашёл{'' if not found else f' серию {found}'} и поставил качать."]
        lines.append(f"• {torrent.title}")
        lines.append(describe(torrent))
        lines.append("Скачивание ведёт отдельная задача — она в работе, со своим прогрессом и кнопкой отмены.")
        if (task.params or {}).get("next_episode"):
            lines.append(f"Мониторинг продолжается: жду серию {task.params['next_episode']}.")
        if result.get("transmission_error"):
            lines.append(f"Transmission: {result['transmission_error']}")
        text = "\n".join(lines)
        message = Message(
            role="agent", text=text, kind="notice", task_id=task.id,
            payload={"task": task.to_dict(), "torrent": torrent.to_dict(),
                     "download_task": child.to_dict()},
        )
        message.actions = self._task_actions(task)
        await self._remember_many(task.session_id, [message])
        await self.events.publish(
            {
                "type": "notification",
                "level": "success",
                "task_id": task.id,
                "title": "Скачивание по задаче",
                "text": text,
                "message": message.to_dict(),
            }
        )
        await self.desktop.send("Скачивание по задаче", torrent.title[:120])
        await self.push.send(
            "Скачивание по задаче",
            f"{task.title}: {torrent.title}"[:200],
        )

    async def _child_download_task(self, task: Task, torrent: Torrent) -> Task:
        """Разовая задача-потомок: она качает и показывает прогресс.

        Название берём от сериала и серии («Звёзды в джунглях — серия 3»), чтобы
        в картотеке было видно, что именно льётся, а не «Звёзды в джунглях»
        рядом с такой же карточкой мониторинга.
        """
        params = task.params or {}
        season = params.get("season", torrent.season)
        episode = params.get("next_episode")
        title = f"{task.title} — серия {episode}" if episode else task.title
        child = Task(
            id=new_id(),
            kind="oneshot",
            state="searching",
            session_id=task.session_id,
            user_query=title,
            candidate=torrent.to_dict(),
            params={
                "title": title,
                "original_title": params.get("original_title"),
                "quality": params.get("quality") or torrent.quality,
                "voice": params.get("voice") or torrent.voice,
                "season": season,
                "episode_from": params.get("episode_from"),
                "next_episode": episode,
                "year": params.get("year"),
                "type": params.get("type"),
                "series_key": torrent.series_key or params.get("series_key"),
                "torrent_id": torrent.id,
                # Откуда пришла задача: видно в журнале и в карточке.
                "source_periodic": task.id,
                "seen_ids": [],
            },
        )
        child.params = {
            key: value for key, value in child.params.items() if value not in (None, "", [])
        }
        await self.store.save_task(child)
        return child

    async def _finish_child(self, child: Task, torrent: Torrent, result: dict[str, Any]) -> None:
        """Потомок становится обычным скачиванием: хеш, прогресс, план."""
        child.state = "downloading"
        child.candidate = torrent.to_dict()
        child.last_error = ""
        child.params["transmission_hash"] = str(result.get("transmission_hash") or "")
        child.params["torrent_id"] = torrent.id
        # Сразу спрашиваем прогресс: иначе карточка несколько секунд выглядит
        # пустой, и кажется, что скачивание не запустилось.
        child.last_result = "поставлено в очередь — жду первых данных"
        if child.params["transmission_hash"]:
            try:
                state = await self.transmission.progress([child.params["transmission_hash"]])
                first = (state or {}).get(child.params["transmission_hash"])
            except Exception:  # noqa: BLE001 - подпись не должна ронять скачивание
                first = None
            if first:
                child.params["progress"] = first
                child.last_result = self._progress_text(first)
        child.options = {
            **(child.options or {}),
            "plan": result.get("plan") or {},
            "torrent_path": result.get("path") or "",
        }
        await self.store.save_task(child)

    async def _drop_child(self, child: Task) -> None:
        """Скачивание не состоялось — пустую задачу-потомка убираем совсем."""
        try:
            await self.store.delete_task(child.id)
        except Exception:  # noqa: BLE001 - уборка не должна мешать отчёту
            log.warning("не удалось убрать задачу-потомка %s", child.id)

    async def _advance_next_episode(self, task: Task, torrent: Torrent) -> None:
        """Пересчитывает «какую серию ждём» по базе скачанного.

        Раньше номер считался только в начале проверки, поэтому после скачивания
        третьей серии карточка ещё полсуток писала «жду серию 3».
        """
        params = task.params or {}
        series = str(params.get("series_key") or torrent.series_key or "")
        if not series:
            return
        progress = await self.store.episodes_by_season(series)
        for season_key, episode in (params.get("baseline_episodes") or {}).items():
            try:
                progress[int(season_key)] = max(progress.get(int(season_key), 0), int(episode))
            except (TypeError, ValueError):
                continue
        params["next_episode"] = self._next_episode(params, progress)

    async def _ask_which_episodes(
        self,
        task: Task,
        torrent: Torrent,
        result: dict[str, Any],
        *,
        marker_task: Task | None = None,
    ) -> bool:
        """Пачка серий без выбранной серии: спрашиваем, а не качаем всё.

        «Проверяй и скачивай» без указания серии и качества не значит «залей
        всю пачку на 120 ГБ». Уведомление повторяется только когда пачка
        выросла — иначе задача писала бы об одном и том же каждый раз.

        `marker_task` — где хранить отметку «об этой пачке уже спрашивали».
        Для периодической задачи выбор идёт в задаче-потомке (она живёт один
        раз), а сравнение должно быть по карточке мониторинга: иначе один и тот
        же вопрос приходил бы каждую проверку.
        """
        groups = result.get("groups") or []
        if not groups:
            return False
        newest = max(int(group.get("episode") or 0) for group in groups)
        marker = {"id": torrent.id, "episodes": len(groups), "newest": newest}
        owner = marker_task or task
        if (owner.params or {}).get("pack_notified") == marker:
            return False
        owner.params["pack_notified"] = marker
        await self.store.save_task(owner)
        new_groups = result.get("new_groups") or groups
        total = human_bytes(sum(int(group.get("size") or 0) for group in groups))
        fresh_size = human_bytes(sum(int(group.get("size") or 0) for group in new_groups))
        text = (
            f"По задаче «{task.title}» — раздача с сериями: "
            f"{_plural(len(groups), 'серия', 'серии', 'серий')} на {total}"
        )
        if len(new_groups) < len(groups):
            text += f", из них новых {len(new_groups)} на {fresh_size}"
        text += (
            f".\n• {torrent.title}\n\n"
            "Всю пачку сразу не качаю: это может быть сотня гигабайт. "
            "Выберите серии или нажмите «Скачать всю пачку»."
        )
        message = Message(
            role="agent", text=text, kind="notice", task_id=task.id,
            payload={"task": task.to_dict(), "torrent": torrent.to_dict()},
        )
        message.actions = [
            Action(
                id="pack-all",
                label="Скачать всю пачку",
                kind="download_all",
                payload={"task_id": task.id, "season": task.params.get("season")},
                style="primary",
            ),
            *self._torrent_actions(task, torrent=torrent),
        ]
        message.actions.append(
            Action(
                id="pick-episodes",
                label="Выбрать серии",
                kind="choose_episodes",
                payload={"task_id": task.id},
            )
        )
        await self._remember_many(task.session_id, [message])
        await self.events.publish(
            {
                "type": "notification",
                "level": "info",
                "task_id": task.id,
                "title": "Нужно выбрать серии",
                "text": text,
                "message": message.to_dict(),
            }
        )
        await self.desktop.send("По задаче нужна серия", torrent.title[:120])
        return True

    async def _notify_nothing(self, task: Task, *, manual: bool = False) -> None:
        """По задаче на трекере вообще пусто — сообщаем не чаще раза в сутки.

        Иначе человек думает, что задача перестала работать: раз в сутки
        приходит тихое «пока ничего нет, продолжаю следить».
        """
        if task.kind != "periodic":
            return
        now = datetime.now(timezone.utc)
        last = _parse_iso(task.notify_last)
        if not manual and last is not None and (now - last) < timedelta(hours=24):
            return
        task.notify_last = now.isoformat(timespec="seconds")
        await self.store.save_task(task)
        interval = self._human_interval(
            task.interval_minutes or self.settings.default_interval_minutes
        )
        text = (
            f"По задаче «{task.title}» на трекере пока ничего нет.\n"
            f"Продолжаю следить {interval} — сообщу, как только появится."
        )
        message = Message(
            role="agent", text=text, kind="notice", task_id=task.id,
            payload={"task": task.to_dict()},
        )
        message.actions = self._task_actions(task)
        await self._remember_many(task.session_id, [message])
        await self.events.publish(
            {
                "type": "notification",
                "level": "info",
                "task_id": task.id,
                "title": "Ничего не найдено",
                "text": text,
                "message": message.to_dict(),
            }
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
        params = task.params or {}
        if params.get("year"):
            bits.append(f"год {params['year']}")
        if params.get("quality"):
            bits.append(f"качество: {self._quality_label(str(params['quality']))}")
        if params.get("voice"):
            bits.append(f"озвучка: {params['voice']}")
        if params.get("season"):
            bits.append(f"сезон: {params['season']}")
        if params.get("episode_from"):
            bits.append(f"с {params['episode_from']} серии")
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
        """Период словами, готовый к подстановке: «проверять раз в час».

        Возвращает оборот целиком («раз в час», «каждые 2 часа», «раз в сутки»),
        чтобы не появлялось «каждые 1 час» и «каждые 24 часов».
        """
        if not minutes:
            return "не задано"
        if minutes % 1440 == 0 and minutes >= 1440:
            days = minutes // 1440
            return "раз в сутки" if days == 1 else f"каждые {days} суток" if days % 10 in (2, 3, 4) else f"каждые {days} суток"
        if minutes % 60 == 0 and minutes >= 60:
            hours = minutes // 60
            if hours == 1:
                return "раз в час"
            if hours in (2, 3, 4):
                return f"каждые {hours} часа"
            return f"каждые {hours} часов"
        if minutes == 1:
            return "раз в минуту"
        if minutes % 10 in (2, 3, 4) and minutes % 100 not in (12, 13, 14):
            return f"каждые {minutes} минуты"
        return f"каждые {minutes} минут"

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
            if key in PARAM_KEYS and value not in (None, "", "null", [])
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

    async def _remember(self, session_id: str, message: Message) -> int:
        # У сообщений с кнопками всегда известен статус «отвечено ли»:
        # иначе после перезагрузки страницы старые кнопки снова активны.
        if message.actions:
            message.payload.setdefault("resolved", False)
        return await self.store.add_message(session_id, message)

    async def _remember_many(self, session_id: str, messages: list[Message]) -> None:
        for message in messages:
            await self._remember(session_id, message)
