"""Transmission RPC: постановка .torrent в очередь загрузки на внешний диск.

Два правила, которые здесь зашиты и которые нельзя нарушать:

1. **Загрузки всегда идут на внешний SSD** (`TRANSMISSION_DOWNLOAD_DIR`,
   по умолчанию `/Volumes/EAGET/torrents`). Путь передаётся в каждом
   `add_torrent` явно, а не берётся из настроек Transmission, — иначе любая
   правка настроек в самом клиенте тихо уводила бы файлы на внутренний диск.
   Если диск не смонтирован, агент **отказывается** ставить раздачу: лучше
   внятная ошибка, чем 40 ГБ на системном разделе.
2. **Перед постановкой проверяется свободное место.** Нужен точный размер
   раздачи (из .torrent, см. core/torrent_meta.py) плюс резерв
   (`TRANSMISSION_MIN_FREE_GB`). Не хватает — раздача не ставится, а агент
   сообщает, сколько нужно и сколько есть.

Клиент transmission-rpc синхронный, поэтому все вызовы уходят в поток через
`asyncio.to_thread`.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from core.debuglog import journal
from core.storage_target import (  # общие правила для внешнего тома
    StorageUnavailable,
    TargetInfo,
    can_write as _can_write,
    volume_root,
)
from core.torrent_meta import human_bytes, total_length

log = logging.getLogger("transmission")


class TransmissionError(RuntimeError):
    pass


# StorageUnavailable приходит из core.storage_target: одно правило для всех папок.


class NotEnoughSpace(TransmissionError):
    """На внешнем диске не хватает места под раздачу."""

    def __init__(self, need: int | None, free: int, target: Path, reserve: int) -> None:
        self.need = need
        self.free = free
        self.target = target
        self.reserve = reserve
        super().__init__(self.describe())

    def describe(self) -> str:
        have = human_bytes(self.free)
        if self.need is None:
            return (
                f"на диске «{self.target.parent.name or self.target}» свободно {have}, "
                f"а размер раздачи определить не удалось — не рискую ставить в очередь"
            )
        need = human_bytes(self.need)
        short = human_bytes(max(0, self.need + self.reserve - self.free))
        return (
            f"на диске «{self.target.parent.name or self.target}» не хватает места: "
            f"нужно {need} + резерв {human_bytes(self.reserve)}, свободно {have} "
            f"(не хватает {short})"
        )


StorageInfo = TargetInfo  # прежнее имя, чтобы не ломать вызывающий код


class Transmission:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.host = self.settings.transmission_host
        self.port = self.settings.transmission_port
        self.username = self.settings.transmission_username
        self.password = self.settings.transmission_password
        self.download_dir = Path(self.settings.transmission_download_dir)
        self._client = None
        self._sig: tuple[str, int, str, str] | None = None
        self._session_synced = False

    # ------------------------------------------------------------------
    def configure(self, host: str, port: int, username: str, password: str) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self._client = None
        self._sig = None
        self._session_synced = False

    def _signature(self) -> tuple[str, int, str, str]:
        return (self.host, self.port, self.username, self.password)

    def _get_client(self):
        signature = self._signature()
        if self._client is not None and self._sig == signature:
            return self._client
        from transmission_rpc import Client

        kwargs: dict[str, Any] = {"host": self.host, "port": self.port, "timeout": 20}
        if self.username:
            kwargs["username"] = self.username
        if self.password:
            kwargs["password"] = self.password
        self._client = Client(**kwargs)
        self._sig = signature
        self._session_synced = False
        return self._client

    @staticmethod
    def _explain(exc: Exception) -> str:
        text = str(exc)
        name = type(exc).__name__
        if "401" in text or "Unauthorized" in text or "unauthorized" in name.lower():
            return (
                "Transmission требует логин/пароль RPC (401 Unauthorized). "
                "Укажите их в настройках агента."
            )
        if "Connection refused" in text or "ConnectError" in name or "NewConnectionError" in name:
            return "Transmission RPC недоступен: соединение отклонено. Приложение запущено?"
        return f"{name}: {text}"

    # ------------------------------------------------------------------
    # Внешний диск
    # ------------------------------------------------------------------
    def min_free_bytes(self) -> int:
        return int(self.settings.transmission_min_free_gb * 1024**3)

    def _volume_root(self) -> Path:
        """Точка монтирования тома для каталога загрузок."""
        return volume_root(self.download_dir)

    def storage_info(self) -> StorageInfo:
        """Живое состояние целевого диска (общие правила — core.storage_target)."""
        from core.storage_target import target_info

        return target_info(self.download_dir)

    def ensure_target(self) -> StorageInfo:
        """Проверяет диск и создаёт каталог загрузок.

        Решение «есть ли диск» принимается по `self.storage_info()`: этот метод
        остаётся единственной точкой правды, через которую можно подменить
        состояние диска в тестах.
        """
        info = self.storage_info()
        if not info.mounted:
            raise StorageUnavailable(info.error or "внешний диск не подключён")
        if not info.writable:
            try:
                self.download_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageUnavailable(
                    f"не удалось создать {self.download_dir}: {exc}"
                ) from exc
            info = self.storage_info()
            if not info.writable:
                raise StorageUnavailable(info.error or "каталог недоступен для записи")
        return info

    def check_space(self, need_bytes: int | None) -> StorageInfo:
        """Места хватает? Бросает NotEnoughSpace, если нет (и пишет это в журнал)."""
        try:
            info = self.ensure_target()
        except StorageUnavailable as exc:
            journal.add(
                "transmission",
                "внешний диск недоступен",
                {"ошибка": str(exc), "каталог": str(self.download_dir)},
                level="error",
            )
            raise
        if need_bytes is not None:
            required = need_bytes + self.min_free_bytes()
            if info.free < required:
                journal.add(
                    "transmission",
                    "не хватает места",
                    {
                        "нужно": human_bytes(need_bytes),
                        "свободно": human_bytes(info.free),
                        "резерв": human_bytes(self.min_free_bytes()),
                        "диск": str(self.download_dir),
                    },
                    level="error",
                )
                raise NotEnoughSpace(need_bytes, info.free, self.download_dir, self.min_free_bytes())
        return info

    # ------------------------------------------------------------------
    async def test_connection(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            client = self._get_client()
            session = client.get_session()
            return {
                "ok": True,
                "version": getattr(session, "version", ""),
                "download_dir": str(getattr(session, "download_dir", "")),
                "rpc_version": getattr(session, "rpc_version", ""),
            }

        try:
            result = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            return {"ok": False, "error": self._explain(exc)}
        result["storage"] = self.storage_info().to_dict()
        return result

    async def _sync_session_dir(self) -> None:
        """Ставит каталог загрузок в самом Transmission — для «ручных» добавлений.

        Агент всё равно передаёт путь в каждом add_torrent, но если владелец
        добавит торрент мышкой в приложении, он тоже должен попасть на внешний
        диск. Делается один раз за процесс.
        """
        if self._session_synced:
            return
        target = str(self.download_dir)
        incomplete = str(self.download_dir / ".incomplete")

        def run() -> None:
            client = self._get_client()
            session = client.get_session()
            changes: dict[str, Any] = {}
            if str(getattr(session, "download_dir", "")) != target:
                changes["download_dir"] = target
            if str(getattr(session, "incomplete_dir", "")) != incomplete:
                changes["incomplete_dir"] = incomplete
            if not getattr(session, "incomplete_dir_enabled", False):
                changes["incomplete_dir_enabled"] = True
            if changes:
                client.set_session(**changes)
                log.info("Transmission: каталог загрузок переведён на %s", target)
            else:
                log.debug("Transmission: каталог загрузок уже %s", target)

        try:
            await asyncio.to_thread(run)
            self._session_synced = True
        except Exception as exc:  # noqa: BLE001 - не смертельно: путь передаём явно
            log.warning("не удалось обновить настройки каталога Transmission: %s", exc)

    async def add(self, torrent_path: Path, *, need_bytes: int | None = None) -> dict[str, Any]:
        """Ставит .torrent в очередь на внешний диск. Бросает NotEnoughSpace/StorageUnavailable."""
        torrent_path = Path(torrent_path)
        if need_bytes is None:
            try:
                need_bytes = total_length(torrent_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("размер раздачи не определён: %s", exc)

        info = self.check_space(need_bytes)  # бросит исключение, если места мало
        await self._sync_session_dir()

        def run() -> dict[str, Any]:
            client = self._get_client()
            data = torrent_path.read_bytes()
            # download_dir передаём явно — это и есть гарантия «всегда на EAGET».
            result = client.add_torrent(data, download_dir=str(self.download_dir))
            return {
                "ok": True,
                "name": getattr(result, "name", ""),
                "hash": getattr(result, "hash_string", "") or getattr(result, "hashString", ""),
                "id": getattr(result, "id", None),
                "download_dir": str(self.download_dir),
                "free_after_human": human_bytes(max(0, info.free - (need_bytes or 0))),
            }

        try:
            result = await asyncio.to_thread(run)
            journal.add(
                "transmission",
                f"в очередь: {result.get('name') or torrent_path.name}",
                {
                    "каталог": result.get("download_dir"),
                    "размер": human_bytes(need_bytes),
                    "свободно было": human_bytes(info.free),
                    "хеш": result.get("hash"),
                },
            )
            return result
        except Exception as exc:  # noqa: BLE001
            self._client = None
            journal.add(
                "transmission",
                "Transmission не принял торрент",
                {"ошибка": self._explain(exc)},
                level="error",
            )
            raise TransmissionError(self._explain(exc)) from exc

    async def find_by_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        """Раздача в очереди по хешу: имя, файлы, каталог, размер."""
        if not torrent_hash:
            return None

        def run() -> dict[str, Any] | None:
            client = self._get_client()
            for torrent in client.get_torrents():
                if getattr(torrent, "hash_string", "") != torrent_hash:
                    continue
                # Имена файлов лежат в сырых полях RPC: у объекта Torrent
                # списка файлов нет (там только FileStat без имён).
                raw_files = (getattr(torrent, "fields", {}) or {}).get("files") or []
                files = [
                    str(item.get("name"))
                    for item in raw_files
                    if isinstance(item, dict) and item.get("name")
                ]
                return {
                    "hash": torrent_hash,
                    "name": getattr(torrent, "name", ""),
                    "download_dir": str(getattr(torrent, "download_dir", "")),
                    "percent_done": float(getattr(torrent, "percent_done", 0.0)),
                    "total_size": int(getattr(torrent, "total_size", 0) or 0),
                    "files": files,
                }
            return None

        return await asyncio.to_thread(run)

    async def remove(self, torrent_hash: str, *, delete_data: bool = False) -> dict[str, Any]:
        """Убирает раздачу из очереди.

        `delete_data` намеренно выключен по умолчанию: на macOS Transmission
        «удаляет» данные перемещением в корзину, а нам нужно освобождать место
        сразу. Файлы стирает агент — см. core/files.py.
        """
        if not torrent_hash:
            return {"ok": False, "error": "не передан хеш раздачи"}

        def run() -> dict[str, Any]:
            client = self._get_client()
            try:
                client.stop_torrent(torrent_hash)
            except Exception:  # noqa: BLE001 - уже остановлена
                pass
            client.remove_torrent(torrent_hash, delete_data=delete_data)
            # Transmission убирает раздачу не мгновенно: пока он это делает,
            # файлы могут ещё дописываться. Ждём, пока раздача исчезнет из
            # очереди, иначе удаление файлов прошло бы «под» активной записью и
            # каталог успел бы появиться снова.
            deadline = time.time() + 15
            while time.time() < deadline:
                if not any(
                    getattr(item, "hash_string", "") == torrent_hash
                    for item in client.get_torrents()
                ):
                    break
                time.sleep(0.3)
            return {"ok": True, "hash": torrent_hash, "delete_data": delete_data}

        try:
            result = await asyncio.to_thread(run)
            journal.add(
                "transmission",
                "раздача убрана из очереди",
                {"хеш": torrent_hash, "данные удалил Transmission": delete_data},
            )
            return result
        except Exception as exc:  # noqa: BLE001
            message = self._explain(exc)
            # «нет такой раздачи» — не ошибка: файлы всё равно надо убрать
            if "not found" in str(exc).lower() or "Invalid argument" in str(exc):
                return {"ok": True, "hash": torrent_hash, "already_gone": True}
            journal.add("transmission", "не удалось убрать раздачу", {"ошибка": message}, level="error")
            raise TransmissionError(message) from exc

    async def list_torrents(self, limit: int = 50) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            client = self._get_client()
            items = []
            for torrent in client.get_torrents()[:limit]:
                items.append(
                    {
                        "id": torrent.id,
                        "name": torrent.name,
                        "status": str(torrent.status),
                        "progress": round(float(getattr(torrent, "percent_done", 0.0)) * 100, 1),
                        "rate_download": getattr(torrent, "rate_download", 0),
                        "rate_upload": getattr(torrent, "rate_upload", 0),
                        "size": getattr(torrent, "total_size", 0),
                        "hash": getattr(torrent, "hash_string", ""),
                        "download_dir": str(getattr(torrent, "download_dir", "")),
                    }
                )
            return items

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise TransmissionError(self._explain(exc)) from exc
