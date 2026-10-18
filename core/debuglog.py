"""Журнал отладки: что агент делал, в человекочитаемом виде.

Зачем отдельный буфер, а не только логи uvicorn: по строкам про HTTP-запросы не
видно, что именно спросили у модели и что ответил MCP. Здесь события собираются
ровно по делу:

    llm           — какой запрос ушёл в модель и что она вернула (включая токены)
    mcp           — какой инструмент трекера вызван, с чем и что пришло
    gate          — запросы к kinozal.me через шлюз: адрес, статус, как прошёл
    transmission  — постановка в очередь и проверки места
    agent         — что агент понял из запроса и что решил
    log           — предупреждения и ошибки из логов модулей

**Журнал общий для процессов.** Шлюз Cloudflare работает отдельным процессом
(порт 8123), и его события иначе были бы не видны в интерфейсе. Поэтому события
дописываются в файл `data/debug.jsonl`, а чтение идёт из него: порядок — порядок
записи, размер ограничен. Файл — диагностика, а не аудит: он не нужен для работы
агента и его можно удалить в любой момент.

Секретов здесь нет: ключи и пароли не пишутся никогда, а тексты запросов — это
то, что пользователь сам написал в чат.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_TEXT = 1200  # сколько символов одного значения показываем
MAX_LINES = 3000  # сколько последних событий держим в файле


def _short(value: Any, limit: int = MAX_TEXT) -> Any:
    """Обрезает длинные строки и вложенные структуры до разумного размера."""
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} симв.)"
    if isinstance(value, dict):
        return {str(k): _short(v, limit) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        items = [_short(item, limit) for item in list(value)[:20]]
        if len(value) > 20:
            items.append(f"… ещё {len(value) - 20}")
        return items
    return value


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class DebugEvent:
    seq: int
    time: str
    category: str
    title: str
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    level: str = "info"  # info | warn | error

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "time": self.time,
            "category": self.category,
            "title": self.title,
            "detail": self.detail,
            "duration_ms": None if self.duration_ms is None else round(self.duration_ms, 1),
            "level": self.level,
        }


class DebugLog:
    """Кольцевой буфер в памяти плюс (по возможности) общий файл в data/."""

    def __init__(self, capacity: int = 400, path: Path | None = None) -> None:
        self._events: deque[DebugEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def set_path(self, path: Path) -> None:
        """Подключает общий файл (вызывается на старте процесса)."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def add(
        self,
        category: str,
        title: str,
        detail: dict[str, Any] | None = None,
        *,
        duration_ms: float | None = None,
        level: str = "info",
    ) -> None:
        with self._lock:
            self._seq += 1
            # seq — миллисекунды времени плюс счётчик процесса: уникально
            # между процессами и годится для «покажи только новее».
            seq = int(time.time() * 1000) * 10 + (self._seq % 10)
            event = DebugEvent(
                seq=seq,
                time=_now(),
                category=category,
                title=title,
                detail=_short(detail or {}),
                duration_ms=duration_ms,
                level=level,
            )
            self._events.append(event)
            if self.path is not None:
                try:
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                except OSError:
                    pass  # диагностика не должна ломать работу

    # ------------------------------------------------------------------
    def _from_file(self) -> list[dict[str, Any]]:
        assert self.path is not None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()[-MAX_LINES:]
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def query(
        self,
        limit: int = 200,
        category: str | None = None,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        if self.path is not None:
            items = self._from_file()
        else:
            with self._lock:
                items = [event.to_dict() for event in self._events]
        if category and category != "all":
            items = [item for item in items if item.get("category") == category]
        if after is not None:
            items = [item for item in items if isinstance(item.get("seq"), int) and item["seq"] > after]
        return items[-limit:]

    def categories(self) -> list[str]:
        return sorted({str(item.get("category")) for item in self.query(limit=MAX_LINES)})

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            if self.path is not None:
                try:
                    self.path.write_text("", encoding="utf-8")
                except OSError:
                    pass

    def dump(self, limit: int = 200) -> str:
        return json.dumps(self.query(limit=limit), ensure_ascii=False, indent=2)


# Один журнал на процесс: его пишут все модули ядра.
journal = DebugLog()


def configure(path: Path | str) -> DebugLog:
    """Включает общий файл журнала. Вызывается на старте каждого процесса."""
    journal.set_path(Path(path))
    return journal


def install_logging_bridge(min_level: int = 30) -> None:
    """Перекладывает предупреждения и ошибки логов в журнал отладки."""
    import logging

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno < min_level:
                return
            level = "error" if record.levelno >= 40 else "warn"
            journal.add(
                "log",
                f"{record.name}: {record.getMessage()[:300]}",
                {"logger": record.name, "уровень": record.levelname},
                level=level,
            )

    handler = _Handler(level=min_level)
    logging.getLogger().addHandler(handler)
