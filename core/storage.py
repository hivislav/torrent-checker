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
    added_at TEXT NOT NULL,
    -- Когда файл удалили с диска. Запись НЕ стирается: по ней агент помнит,
    -- что эта серия уже была, и не скачивает её повторно.
    deleted_at TEXT,
    -- Когда загрузка реально дошла до 100%. Пока NULL, раздача ещё качается
    -- и в списке «Скачано на диск» не показывается (она — в «В работе»).
    done_at TEXT,
    -- Какие серии скачаны из этой раздачи: JSON-список
    -- [{"season": 4, "episode": 7}, …]. Одна раздача — одна запись в списке
    -- «Скачано на диск», даже если из неё взяли весь сезон.
    episodes TEXT NOT NULL DEFAULT '[]'
);
-- Уникальный индекс «одна запись на раздачу» создаётся в _migrate(), а не здесь:
-- в старых базах на одну раздачу могло быть несколько строк (по серии), и
-- индекс не создался бы, пока они не слиты.
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
        if "deleted_at" not in columns:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN deleted_at TEXT")
        if "episodes" not in columns:
            self._conn.execute(
                "ALTER TABLE downloads ADD COLUMN episodes TEXT NOT NULL DEFAULT '[]'"
            )
        if "done_at" not in columns:
            self._conn.execute("ALTER TABLE downloads ADD COLUMN done_at TEXT")
        # Одна раздача — одна запись. Старые базы держали по строке на серию:
        # сначала снимаем старый составной индекс (он запрещает слияние), потом
        # сливаем строки, и только затем ставим индекс «одна строка на раздачу».
        self._conn.execute("DROP INDEX IF EXISTS idx_downloads_episode")
        self._merge_download_rows()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_torrent "
            "ON downloads(torrent_id)"
        )

    def _merge_download_rows(self) -> None:
        """Схлопывает записи «по серии» в одну запись на раздачу."""
        import json as _json

        rows = self._conn.execute(
            "SELECT * FROM downloads ORDER BY id"
        ).fetchall()
        by_torrent: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            by_torrent.setdefault(str(item["torrent_id"]), []).append(item)
        for torrent_id, items in by_torrent.items():
            if len(items) == 1 and (items[0].get("episodes") or "[]") not in ("", "[]"):
                continue
            pairs: list[dict[str, Any]] = []
            seen: set[tuple[int | None, int]] = set()
            for item in items:
                stored = item.get("episodes") or "[]"
                try:
                    loaded = _json.loads(stored)
                except (TypeError, ValueError):
                    loaded = []
                if not isinstance(loaded, list):
                    loaded = []
                if loaded:
                    candidates = [
                        (entry.get("season"), entry.get("episode"))
                        for entry in loaded if isinstance(entry, dict)
                    ]
                else:
                    candidates = [(item.get("season"), item.get("episode"))]
                for season, episode in candidates:
                    if episode is None:
                        continue
                    key = (season if season is not None else None, int(episode))
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append({"season": key[0], "episode": key[1]})
            pairs.sort(key=lambda entry: (entry["season"] or 0, entry["episode"]))
            seasons = {entry["season"] for entry in pairs if entry["season"] is not None}
            keep = items[0]
            alive = any(item.get("deleted_at") is None for item in items)
            merged_season = next(iter(seasons)) if len(seasons) == 1 else None
            last_episode = max((entry["episode"] for entry in pairs), default=None)
            self._conn.execute(
                "UPDATE downloads SET episodes = ?, season = ?, episode = ?, "
                "deleted_at = ? WHERE id = ?",
                (
                    _json.dumps(pairs, ensure_ascii=False),
                    merged_season,
                    last_episode,
                    None if alive else (items[0].get("deleted_at") or now_iso()),
                    keep["id"],
                ),
            )
            for item in items[1:]:
                self._conn.execute("DELETE FROM downloads WHERE id = ?", (item["id"],))

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
            # «failed» тоже берём: периодическая задача после сбоя трекера
            # должна продолжить работу, а не остаться в завершённых навсегда.
            cursor = conn.execute(
                "SELECT * FROM tasks WHERE kind='periodic' "
                "AND state IN ('monitoring', 'failed') "
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
                              transmission_hash: str = "", torrent_name: str = "",
                              episodes: list[dict[str, Any]] | None = None) -> None:
        """Запоминает раздачу одной строкой, а серии — списком внутри неё.

        «Одна раздача — одна запись»: в списке «Скачано на диск» не должно быть
        пяти строк только потому, что из пачки взяли пять серий. Для прогресса
        (какую серию ждать дальше) серии лежат в `episodes` — по ним и считается
        `episodes_by_season`.
        """
        import json as _json

        pairs: list[dict[str, Any]] = []
        for item in episodes or []:
            try:
                episode = int(item.get("episode"))  # type: ignore[union-attr]
            except (TypeError, ValueError, AttributeError):
                continue
            season = item.get("season") if isinstance(item, dict) else None
            try:
                season = int(season) if season not in (None, "") else None
            except (TypeError, ValueError):
                season = None
            pairs.append({"season": season, "episode": episode})
        if not pairs and getattr(torrent, "episodes", None):
            season = getattr(torrent, "season", None)
            pairs = [
                {"season": season, "episode": int(item)}
                for item in torrent.episodes if item is not None
            ]

        def run(conn: sqlite3.Connection) -> None:
            existing = conn.execute(
                "SELECT episodes FROM downloads WHERE torrent_id = ?", (str(torrent.id),)
            ).fetchone()
            merged: list[dict[str, Any]] = []
            seen: set[tuple[int | None, int]] = set()
            if existing is not None:
                try:
                    loaded = _json.loads(existing["episodes"] or "[]")
                except (TypeError, ValueError):
                    loaded = []
                for entry in loaded if isinstance(loaded, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    key = (entry.get("season"), int(entry.get("episode") or 0))
                    if key[1] and key not in seen:
                        seen.add(key)
                        merged.append({"season": key[0], "episode": key[1]})
            for pair in pairs:
                key = (pair["season"], pair["episode"])
                if key not in seen:
                    seen.add(key)
                    merged.append(pair)
            merged.sort(key=lambda entry: (entry["season"] or 0, entry["episode"]))
            seasons = {entry["season"] for entry in merged if entry["season"] is not None}
            latest = max((entry["episode"] for entry in merged), default=None)
            conn.execute(
                "INSERT INTO downloads (task_id, torrent_id, title, name, series_key, "
                "quality, voice, season, episode, size, upload_date, uploaded_ts, "
                "torrent_path, transmission_hash, added_at, episodes, done_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(torrent_id) "
                "DO UPDATE SET deleted_at = NULL, added_at = excluded.added_at, "
                "torrent_path = excluded.torrent_path, "
                "transmission_hash = excluded.transmission_hash, "
                "episodes = excluded.episodes, season = excluded.season, "
                "episode = excluded.episode, "
                "done_at = COALESCE(downloads.done_at, excluded.done_at)",
                (
                    task.id,
                    torrent.id,
                    torrent.title,
                    torrent_name or getattr(torrent, "title", ""),
                    getattr(torrent, "series_key", "") or "",
                    torrent.quality,
                    torrent.voice,
                    next(iter(seasons)) if len(seasons) == 1 else None,
                    latest,
                    torrent.size,
                    torrent.upload_date,
                    torrent.uploaded_ts,
                    torrent_path,
                    transmission_hash,
                    now_iso(),
                    _json.dumps(merged, ensure_ascii=False),
                    # Пока файл не докачан, записи в «Скачано» нет: её поставит
                    # опрос прогресса, когда Transmission скажет «готово».
                    None,
                ),
            )

        await self._call(run)

    async def get_download(self, torrent_id: str) -> dict[str, Any] | None:
        """Запись о скачанном, которое ещё лежит на диске."""
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM downloads WHERE torrent_id = ? AND deleted_at IS NULL LIMIT 1",
                (str(torrent_id),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

        return await self._call(run)

    async def get_download_by_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        """Запись по хешу Transmission (когда id раздачи неизвестен)."""

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM downloads WHERE transmission_hash = ? AND deleted_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (str(torrent_hash),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

        return await self._call(run)

    async def mark_downloads_done(self, hashes: list[str]) -> None:
        """Помечает записи завершёнными: только после этого они видны в «Скачано»."""
        clean = [str(item) for item in hashes if item]
        if not clean:
            return

        def run(conn: sqlite3.Connection) -> None:
            placeholders = ",".join("?" for _ in clean)
            conn.execute(
                f"UPDATE downloads SET done_at = COALESCE(done_at, ?) "
                f"WHERE transmission_hash IN ({placeholders})",
                (now_iso(), *clean),
            )

        await self._call(run)

    async def drop_download(self, torrent_id: str) -> None:
        """Удаляет запись целиком.

        Так делаем только с отменённым скачиванием: файлов нет, «уже скачано»
        не наступило, и память о нём мешала бы взять раздачу снова.
        """

        def run(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM downloads WHERE torrent_id = ?", (str(torrent_id),))

        await self._call(run)

    async def forget_download(self, torrent_id: str) -> None:
        """Помечает скачанное удалённым, но НЕ стирает запись.

        Так агент помнит, что эта серия уже была: иначе периодическая задача
        «качай новые, начиная с третьей» после удаления третьей скачала бы её
        заново.
        """

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE downloads SET deleted_at = ? WHERE torrent_id = ? AND deleted_at IS NULL",
                (now_iso(), str(torrent_id)),
            )

        await self._call(run)

    async def is_downloaded(self, torrent_id: str) -> bool:
        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT 1 FROM downloads WHERE torrent_id = ? AND deleted_at IS NULL LIMIT 1",
                (torrent_id,),
            )
            return cursor.fetchone() is not None

        return bool(await self._call(run))

    async def downloaded_ids(self, series_key: str | None = None) -> set[str]:
        def run(conn: sqlite3.Connection):
            if series_key:
                cursor = conn.execute(
                    "SELECT torrent_id FROM downloads WHERE series_key = ? AND deleted_at IS NULL",
                    (series_key,),
                )
            else:
                cursor = conn.execute("SELECT torrent_id FROM downloads WHERE deleted_at IS NULL")
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
        """Докуда дошли по сезонам: {сезон: максимальная серия}.

        Удалённые записи тоже считаются: если пользователь посмотрел серию и
        удалил файл, прогресс не должен откатиться назад.
        """

        def run(conn: sqlite3.Connection):
            import json as _json

            cursor = conn.execute(
                "SELECT season, episode, episodes FROM downloads WHERE series_key = ?",
                (series_key,),
            )
            found: dict[int, int] = {}
            for row in cursor.fetchall():
                pairs: list[tuple[int | None, int]] = []
                try:
                    loaded = _json.loads(row["episodes"] or "[]")
                except (TypeError, ValueError):
                    loaded = []
                for entry in loaded if isinstance(loaded, list) else []:
                    if isinstance(entry, dict) and entry.get("episode") is not None:
                        pairs.append((entry.get("season"), int(entry["episode"])))
                if not pairs and row["season"] is not None and row["episode"] is not None:
                    pairs.append((row["season"], int(row["episode"])))
                for season, episode in pairs:
                    if season is None:
                        continue
                    found[int(season)] = max(found.get(int(season), 0), episode)
            return list(found.items())

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
                "SELECT * FROM downloads WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

        return await self._call(run)

    # ------------------------------------------------------------------
    # история чата
    # ------------------------------------------------------------------
    async def add_message(self, session_id: str, message: Message) -> int:
        def run(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
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
            return int(cursor.lastrowid or 0)

        return await self._call(run)

    async def last_row_id(self) -> int:
        """Последний id сообщения вообще — граница «что было до этого хода»."""

        def run(conn: sqlite3.Connection) -> int:
            return int(conn.execute("SELECT IFNULL(MAX(id), 0) FROM messages").fetchone()[0])

        return int(await self._call(run))

    async def resolve_actions(self, task_id: str, *, before_id: int | None = None) -> int:
        """Помечает последний вопрос задачи как отвеченный.

        Кнопки в сообщении нажимаются один раз: как только действие выполнено,
        этот вопрос закрыт, и после перезагрузки страницы кнопки снова не
        активны.

        `before_id` — граница хода: сообщения, созданные в этом же ходу, не
        трогаем. Иначе уведомление «нашлось новое» вместе с кнопкой «Скачать»,
        появившееся во время «Проверить сейчас», тут же помечалось отвеченным —
        и кнопка была мёртвой с рождения.
        """
        rows = await self.history_for_task(task_id, limit=200)
        for row in reversed(rows):
            actions = row.get("actions") or []
            if not actions:
                continue
            if before_id is not None and int(row["id"]) >= before_id:
                continue
            payload = row.get("payload") or {}
            if payload.get("resolved"):
                return 0
            payload["resolved"] = True

            def run(conn: sqlite3.Connection, message_id: int = int(row["id"]), body=payload) -> None:
                conn.execute(
                    "UPDATE messages SET payload = ? WHERE id = ?",
                    (json.dumps(body, ensure_ascii=False), message_id),
                )

            await self._call(run)
            return 1
        return 0

    async def set_message_task(self, message_id: int, task_id: str) -> None:
        """Привязывает сообщение к задаче: у каждой задачи своя переписка."""
        if not message_id or not task_id:
            return

        def run(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE messages SET task_id = ? WHERE id = ? AND (task_id IS NULL OR task_id = '')",
                (task_id, int(message_id)),
            )

        await self._call(run)

    async def history_for_task(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Переписка конкретной задачи — своя у каждой, как отдельный чат."""

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM messages WHERE task_id = ? ORDER BY id ASC LIMIT ?",
                (task_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

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
    async def history_since(
        self, session_id: str, after_id: int, limit: int = 60
    ) -> list[dict[str, Any]]:
        """Сообщения сессии позже указанной метки (для сброшенного окна)."""

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND id > ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, int(after_id), limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            rows.reverse()
            return rows

        return await self._call(run)

    async def last_message_id(self, session_id: str) -> int:
        """id последнего сообщения сессии — метка «всё, что было раньше, не в окне»."""

        def run(conn: sqlite3.Connection):
            cursor = conn.execute(
                "SELECT IFNULL(MAX(id), 0) FROM messages WHERE session_id = ?",
                (session_id,),
            )
            return int(cursor.fetchone()[0])

        return await self._call(run)

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
