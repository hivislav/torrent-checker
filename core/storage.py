"""Хранилище агента на SQLite: задачи, история, «что уже скачано», настройки.

Почему SQLite, а не JSON-файлы: периодические задачи идут параллельно и пишут
результаты из разных корутин. Один файл с блокировкой в Python ломался бы на
первом же пересечении, а транзакции SQLite решают это бесплатно.

Доступ к базе — синхронный, но вызывается из `asyncio.to_thread`: запросы
короткие, а блокировать событийный цикл ради них не нужно.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from core.models import Message, Task, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT 'default',
    user_query TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    interval_minutes INTEGER,
    auto_download INTEGER NOT NULL DEFAULT 0,
    candidate TEXT,
    options TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_check_at TEXT,
    next_check_at TEXT,
    last_result TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    checks_done INTEGER NOT NULL DEFAULT 0,
    found_total INTEGER NOT NULL DEFAULT 0,
    notify_last TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind);

-- Память агента: что уже скачано. По ней же считается «новая серия».
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    torrent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    series_key TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '',
    voice TEXT NOT NULL DEFAULT '',
    season INTEGER,
    episode INTEGER,
    size TEXT NOT NULL DEFAULT '',
    upload_date TEXT NOT NULL DEFAULT '',
    uploaded_ts REAL,
    torrent_path TEXT NOT NULL DEFAULT '',
    transmission_hash TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
-- Уникальность по раздаче И серии: одна и та же раздача обновляется на трекере
-- и приносит новые выпуски, поэтому записей по ней может быть несколько.
CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_episode
    ON downloads(torrent_id, IFNULL(season, 0), IFNULL(episode, 0));
CREATE INDEX IF NOT EXISTS idx_downloads_series ON downloads(series_key);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT 'default',
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    payload TEXT NOT NULL DEFAULT '{}',
    actions TEXT NOT NULL DEFAULT '[]',
    task_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checks_task ON checks(task_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Дописывает колонки, появившиеся позже: базу не пересоздаём."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(downloads)").fetchall()
        }
        if "name" not in columns:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        # Старый уникальный индекс «одна запись на раздачу» мешает запоминать
        # несколько серий из одной и той же пачки — заменяем на составной.
        self._conn.execute("DROP INDEX IF EXISTS idx_downloads_torrent")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_episode "
            "ON downloads(torrent_id, IFNULL(season, 0), IFNULL(episode, 0))"
        )

    # ------------------------------------------------------------------
    # низкий уровень
    # ------------------------------------------------------------------
    def _run(self, func):
        with self._lock:
            result = func(self._conn)
            self._conn.commit()
            return result

    async def _call(self, func):
        return await asyncio.to_thread(self._run, func)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # задачи
    # ------------------------------------------------------------------
    async def save_task(self, task: Task) -> None:
        task.updated_at = now_iso()
        row = asdict(task)
        row["params"] = json.dumps(task.params, ensure_ascii=False)
        row["candidate"] = json.dumps(task.candidate, ensure_ascii=False) if task.candidate else None
        row["options"] = json.dumps(task.options, ensure_ascii=False) if task.options else None
        row["auto_download"] = int(task.auto_download)

        columns = list(row)
        placeholders = ", ".join(f":{c}" for c in columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "id")

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                row,
            )

        await self._call(run)

    async def get_task(self, task_id: str) -> Task | None:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

        row = await self._call(run)
        return Task.from_row(row) if row else None

    async def list_tasks(
        self, limit: int = 50, states: Iterable[str] | None = None, kind: str | None = None
    ) -> list[Task]:
        query = "SELECT * FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if states:
            states = list(states)
            clauses.append(f"state IN ({', '.join('?' for _ in states)})")
            params.extend(states)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

        rows = await self._call(run)
        return [Task.from_row(row) for row in rows]

    async def active_periodic(self) -> list[Task]:
        return await self.list_tasks(limit=1000, states=["monitoring"], kind="periodic")

    async def due_periodic(self) -> list[Task]:
        """Задачи, которым пора проверяться (по next_check_at)."""
        stamp = now_iso()

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM tasks WHERE kind='periodic' AND state='monitoring' "
                "AND (next_check_at IS NULL OR next_check_at <= ?) ORDER BY next_check_at",
                (stamp,),
            )
            return [dict(row) for row in cursor.fetchall()]

        rows = await self._call(run)
        return [Task.from_row(row) for row in rows]

    async def delete_task(self, task_id: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.execute("DELETE FROM checks WHERE task_id = ?", (task_id,))

        await self._call(run)

    # ------------------------------------------------------------------
    # скачанное (память агента)
    # ------------------------------------------------------------------
    async def record_download(self, task: Task, torrent: Any, torrent_path: str,
                              transmission_hash: str = "", torrent_name: str = "") -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO downloads (task_id, torrent_id, title, name, series_key, "
                "quality, voice, season, episode, size, upload_date, uploaded_ts, "
                "torrent_path, transmission_hash, added_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.id,
                    torrent.id,
                    torrent.title,
                    torrent_name or getattr(torrent, "title", ""),
                    getattr(torrent, "series_key", "") or "",
                    torrent.quality,
                    torrent.voice,
                    torrent.season,
                    torrent.episode,
                    torrent.size,
                    torrent.upload_date,
                    torrent.uploaded_ts,
                    torrent_path,
                    transmission_hash,
                    now_iso(),
                ),
            )

        await self._call(run)

    async def get_download(self, torrent_id: str) -> dict[str, Any] | None:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM downloads WHERE torrent_id = ? LIMIT 1", (str(torrent_id),)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

        return await self._call(run)

    async def forget_download(self, torrent_id: str) -> None:
        """Убирает запись из памяти агента: файлов больше нет, помнить нечего."""

        def run(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM downloads WHERE torrent_id = ?", (str(torrent_id),))

        await self._call(run)

    async def is_downloaded(self, torrent_id: str) -> bool:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute("SELECT 1 FROM downloads WHERE torrent_id = ? LIMIT 1", (torrent_id,))
            return cursor.fetchone() is not None

        return bool(await self._call(run))

    async def downloaded_ids(self, series_key: str | None = None) -> set[str]:
        def run(conn: sqlite3.Connection):
            if series_key:
                cursor = conn.execute(
                    "SELECT torrent_id FROM downloads WHERE series_key = ?", (series_key,)
                )
            else:
                cursor = conn.execute("SELECT torrent_id FROM downloads")
            return [row["torrent_id"] for row in cursor.fetchall()]

        return set(await self._call(run))

    async def last_episode(self, series_key: str, season: int | None = None) -> int:
        """Максимальная скачанная серия (по сезону, если он известен)."""

        def run(conn: sqlite3.Connection):
            query = (
                "SELECT MAX(episode) AS ep FROM downloads "
                "WHERE series_key = ? AND episode IS NOT NULL"
            )
            params: list[Any] = [series_key]
            if season is not None:
                query += " AND season = ?"
                params.append(season)
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            return row["ep"] if row and row["ep"] is not None else 0

        return int(await self._call(run) or 0)

    async def episodes_by_season(self, series_key: str) -> dict[int, int]:
        """Что уже скачано по сезонам: {сезон: максимальная серия}."""

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT season, MAX(episode) AS ep FROM downloads "
                "WHERE series_key = ? AND season IS NOT NULL AND episode IS NOT NULL "
                "GROUP BY season",
                (series_key,),
            )
            return [(row["season"], row["ep"]) for row in cursor.fetchall()]

        rows = await self._call(run)
        return {int(season): int(episode) for season, episode in rows}

    async def last_upload_ts(self, series_key: str) -> float:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT MAX(uploaded_ts) AS ts FROM downloads WHERE series_key = ?", (series_key,)
            )
            row = cursor.fetchone()
            return row["ts"] if row and row["ts"] else 0.0

        return float(await self._call(run) or 0.0)

    async def list_downloads(self, limit: int = 100) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

        return await self._call(run)

    # ------------------------------------------------------------------
    # история чата
    # ------------------------------------------------------------------
    async def add_message(self, session_id: str, message: Message) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO messages (session_id, role, text, kind, payload, actions, task_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    message.role,
                    message.text,
                    message.kind,
                    json.dumps(message.payload, ensure_ascii=False),
                    json.dumps([asdict(a) for a in message.actions], ensure_ascii=False),
                    message.task_id,
                    message.created_at,
                ),
            )

        await self._call(run)

    async def history(self, session_id: str, limit: int = 60) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            rows.reverse()
            return rows

        rows = await self._call(run)
        for row in rows:
            try:
                row["payload"] = json.loads(row.get("payload") or "{}")
            except ValueError:
                row["payload"] = {}
            try:
                row["actions"] = json.loads(row.get("actions") or "[]")
            except ValueError:
                row["actions"] = []
        return rows

    async def clear_history(self, session_id: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

        await self._call(run)

    # ------------------------------------------------------------------
    # журнал проверок периодических задач
    # ------------------------------------------------------------------
    async def add_check(self, task_id: str, found: int, new_count: int, message: str = "") -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO checks (task_id, created_at, found, new_count, message) VALUES (?,?,?,?,?)",
                (task_id, now_iso(), found, new_count, message),
            )

        await self._call(run)

    async def list_checks(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM checks WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
            )
            return [dict(row) for row in cursor.fetchall()]

        return await self._call(run)

    # ------------------------------------------------------------------
    # настройки
    # ------------------------------------------------------------------
    async def get_setting(self, key: str, default: str = "") -> str:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default

        return str(await self._call(run))

    async def set_setting(self, key: str, value: str) -> None:
        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

        await self._call(run)
