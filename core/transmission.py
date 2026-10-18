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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from core.torrent_meta import human_bytes, total_length

log = logging.getLogger("transmission")


class TransmissionError(RuntimeError):
    pass


class StorageUnavailable(TransmissionError):
    """Внешний диск не подключён (или каталог недоступен для записи)."""


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


@dataclass
class StorageInfo:
    """Состояние диска, куда кладём загрузки."""

    path: Path
    mounted: bool
    writable: bool
    free: int = 0
    total: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.mounted and self.writable

    def to_dict(self) -> dict[str, Any]:
        volume = self.path.parent.name if self.path.parent.name else str(self.path.parent)
        return {
            "path": str(self.path),
            "volume": volume,
            "ok": self.ok,
            "mounted": self.mounted,
            "writable": self.writable,
            "free": self.free,
            "free_human": human_bytes(self.free),
            "total": self.total,
            "total_human": human_bytes(self.total),
            "error": self.error,
        }


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
        """Точка монтирования тома: /Volumes/EAGET для /Volumes/EAGET/torrents.

        Отдельная ветка для /Volumes обязательна: без неё поиск «первой точки
        монтирования вверх по дереву» уходил до корня «/» (он тоже mount point),
        и агент считал смонтированным несуществующий диск.
        """
        import os

        parts = self.download_dir.parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            return Path("/Volumes") / parts[2]
        for candidate in (self.download_dir, *self.download_dir.parents):
            try:
                if os.path.ismount(str(candidate)):
                    return candidate
            except OSError:
                continue
        return self.download_dir.parent

    def storage_info(self) -> StorageInfo:
        """Живое состояние целевого диска. Ничего не создаёт и не падает."""
        path = self.download_dir
        volume = self._volume_root()

        # Диск должен быть именно смонтирован: проверка «каталог существует»
        # недостаточна — пустая папка /Volumes/EAGET может остаться от прошлого
        # раза, и загрузка молча легла бы на системный раздел.
        import os

        mounted = volume.exists() and os.path.ismount(str(volume))
        if not mounted:
            return StorageInfo(
                path=path,
                mounted=False,
                writable=False,
                error=f"внешний диск «{volume.name}» не подключён ({volume} отсутствует)",
            )
        try:
            usage = shutil.disk_usage(str(volume))
        except OSError as exc:
            return StorageInfo(
                path=path,
                mounted=True,
                writable=False,
                error=f"не удалось прочитать размер свободного места: {exc}",
            )

        writable = path.exists() and _can_write(path)
        if not path.exists():
            writable = _can_write(volume)
        return StorageInfo(
            path=path,
            mounted=True,
            writable=writable,
            free=usage.free,
            total=usage.total,
            error="" if writable else f"каталог {path} недоступен для записи",
        )

    def ensure_target(self) -> StorageInfo:
        """Проверяет диск и создаёт каталог загрузок. Бросает StorageUnavailable."""
        info = self.storage_info()
        if not info.mounted:
            raise StorageUnavailable(info.error or "внешний диск не подключён")
        if not info.writable:
            # Пробуем создать каталог — на подключённом диске он обычно просто
            # ещё не создан (первый запуск).
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
        """Места хватает? Бросает NotEnoughSpace, если нет."""
        info = self.ensure_target()
        if need_bytes is not None:
            required = need_bytes + self.min_free_bytes()
            if info.free < required:
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
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            raise TransmissionError(self._explain(exc)) from exc

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


def _can_write(path: Path) -> bool:
    """Проверка записи настоящей пробой.

    `os.access` на внешних томах (exFAT, сетевые диски) врёт: права есть, а
    запись падает. Поэтому пробуем создать и сразу удалить пустой файл — это и
    есть ответ на вопрос «можно ли сюда качать».
    """
    probe = Path(path) / ".agent-write-probe"
    try:
        probe.write_bytes(b"")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True
