"""Модели данных агента: раздачи, задачи, состояния, ответы.

Слой ничего не знает про веб: те же объекты сможет использовать будущий
Telegram-бот или CLI. Поэтому здесь нет ни FastAPI, ни шаблонов, ни HTML.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal

# ----------------------------------------------------------------------
# Состояния задачи
# ----------------------------------------------------------------------
# Разовые:
#   searching        — ищем по параметрам (выполнение)
#   awaiting_choice  — нашлось несколько вариантов, ждём выбор качества/озвучки
#   awaiting_confirm — нашли конкретную раздачу, ждём «качаем»
#   downloading      — тянем .torrent и ставим в Transmission
#   done             — скачано и поставлено в очередь
#   not_found        — ничего не нашлось (предлагаем сделать периодической)
#   failed           — ошибка (сеть, логин, Transmission)
#   cancelled        — отменил пользователь
# Периодические:
#   awaiting_setup   — уточняем период и что делать с находками
#   awaiting_confirm — подтверждение «берём в работу» (те же данные, что разовая)
#   monitoring       — задача живёт, шедулер её проверяет
#   cancelled        — отменена кнопкой
State = Literal[
    "searching",
    "awaiting_choice",
    "awaiting_confirm",
    "awaiting_setup",
    "downloading",
    "monitoring",
    "done",
    "not_found",
    "failed",
    "cancelled",
]

ACTIVE_STATES = {
    "searching",
    "awaiting_choice",
    "awaiting_confirm",
    "awaiting_setup",
    "downloading",
    "monitoring",
}
FINISHED_STATES = {"done", "not_found", "failed", "cancelled"}

STATE_TITLES: dict[str, str] = {
    "searching": "поиск",
    "awaiting_choice": "выбор варианта",
    "awaiting_confirm": "ждёт подтверждения",
    "awaiting_setup": "уточнение параметров",
    "downloading": "скачивание",
    "monitoring": "мониторинг",
    "done": "готово",
    "not_found": "не найдено",
    "failed": "ошибка",
    "cancelled": "отменена",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ----------------------------------------------------------------------
# Раздача
# ----------------------------------------------------------------------
@dataclass
class Torrent:
    """Одна раздача с трекера: что отдал MCP плюс то, что мы разобрали сами."""

    id: str
    title: str
    size: str = ""
    seeds: int = 0
    peers: int = 0
    comments: int = 0
    upload_date: str = ""
    uploader: str = ""
    category: str = ""

    # Разобранное из названия (см. core/parsing.py)
    series_title: str = ""
    year: str = ""
    qualities: list[str] = field(default_factory=list)
    voices: list[str] = field(default_factory=list)
    seasons: list[int] = field(default_factory=list)
    episodes: list[int] = field(default_factory=list)
    # Ключ сериала для памяти «что уже скачано» (core/parsing.series_key)
    series_key: str = ""
    episodes_total: int | None = None
    uploaded_ts: float | None = None
    # Экранка (CAM/TS/TC/SCR): снято с экрана, а не из релиза. В выдачу такие
    # раздачи по умолчанию не попадают — см. core/parsing.is_screener.
    screener: bool = False

    @property
    def quality(self) -> str:
        return self.qualities[0] if self.qualities else ""

    @property
    def voice(self) -> str:
        return ", ".join(self.voices) if self.voices else ""

    @property
    def episode(self) -> int | None:
        return self.episodes[-1] if self.episodes else None

    @property
    def season(self) -> int | None:
        return self.seasons[-1] if self.seasons else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Torrent":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ----------------------------------------------------------------------
# Задача
# ----------------------------------------------------------------------
@dataclass
class Task:
    id: str
    kind: Literal["oneshot", "periodic"]
    state: str
    session_id: str
    user_query: str

    # Разобранные параметры запроса: title / quality / voice / season / episode / year
    params: dict[str, Any] = field(default_factory=dict)

    # Периодические задачи
    interval_minutes: int | None = None
    auto_download: bool = False  # сразу качать или только сообщать

    # Текущий кандидат и предложенные варианты выбора
    candidate: dict[str, Any] | None = None
    options: dict[str, Any] | None = None

    # Служебное
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    last_check_at: str | None = None
    next_check_at: str | None = None
    last_result: str = ""
    last_error: str = ""
    checks_done: int = 0
    found_total: int = 0
    notify_last: str = ""

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def title(self) -> str:
        params = self.params or {}
        return str(params.get("title") or self.user_query or "без названия")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Task":
        import json

        def load(name: str, default: Any) -> Any:
            raw = row.get(name)
            if raw in (None, ""):
                return default
            if isinstance(raw, (dict, list)):
                return raw
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            id=row["id"],
            kind=row["kind"],
            state=row["state"],
            session_id=row.get("session_id") or "default",
            user_query=row.get("user_query") or "",
            params=load("params", {}),
            interval_minutes=row.get("interval_minutes"),
            auto_download=bool(row.get("auto_download")),
            candidate=load("candidate", None),
            options=load("options", None),
            created_at=row.get("created_at") or now_iso(),
            updated_at=row.get("updated_at") or now_iso(),
            last_check_at=row.get("last_check_at"),
            next_check_at=row.get("next_check_at"),
            last_result=row.get("last_result") or "",
            last_error=row.get("last_error") or "",
            checks_done=int(row.get("checks_done") or 0),
            found_total=int(row.get("found_total") or 0),
            notify_last=row.get("notify_last") or "",
        )


# ----------------------------------------------------------------------
# Сообщения и кнопки
# ----------------------------------------------------------------------
@dataclass
class Action:
    """Кнопка под сообщением агента."""

    id: str
    label: str
    # link — это не действие, а переход: клиент рисует ссылку (`url`), а не
    # кнопку с запросом на сервер. Так на карточке живёт «Открыть на трекере».
    kind: str  # confirm | cancel | choose | create_periodic | download | link | details
    payload: dict[str, Any] = field(default_factory=dict)
    style: str = "normal"  # normal | primary | danger
    # Куда ведёт кнопка-ссылка (kind="link"): только внешний адрес трекера.
    url: str = ""
    # Подпись группы: «Качество», «Озвучка», «Серии» — чтобы в одном сообщении
    # было видно, что к чему относится.
    group: str = ""
    # Множественный выбор: клиент запоминает нажатия и отправляет список.
    multi: bool = False
    # Выбрано ли сейчас (для кнопок множественного выбора).
    selected: bool = False


@dataclass
class Message:
    role: Literal["user", "agent", "system"]
    text: str
    kind: str = "text"  # text | task | notice | error | tickets
    payload: dict[str, Any] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    task_id: str | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["actions"] = [asdict(a) for a in self.actions]
        return data


@dataclass
class Turn:
    """Результат обработки одного сообщения пользователя."""

    messages: list[Message] = field(default_factory=list)
    task: Task | None = None

    def add(self, text: str, **kwargs: Any) -> Message:
        message = Message(role="agent", text=text, **kwargs)
        self.messages.append(message)
        return message
