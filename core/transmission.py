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
import os
import shutil
import signal
import subprocess
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


class TransmissionBusy(TransmissionError):
    """Демон жив, но занят диском и не отвечает: это не поломка связи.

    Transmission отдаёт JSON-RPC из того же потока, который пишет куски на
    диск. Пока идёт большая запись (4K-сезон на внешний exfat), ответа нет
    вообще — секунды, иногда минуты. Отличать это от «приложение не запущено»
    важно: в первом случае панель должна показать последние известные данные
    и пометку «занят записью», а не красную ошибку и прочерки.
    """

    busy = True

    def __init__(self, message: str, *, waited: float = 0.0) -> None:
        self.waited = waited
        super().__init__(message)


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

_DAEMON_PATTERN = "Transmission.app/Contents/MacOS/Transmission"


def daemon_pids() -> list[int]:
    """PID запущенного Transmission.app (пусто — приложение не запущено).

    Именно приложения, а не демона transmission-daemon: в этой системе
    Transmission работает как обычное приложение из /Applications.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", _DAEMON_PATTERN],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(chunk) for chunk in result.stdout.split() if chunk.strip().isdigit()]


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
        # «Демон занят диском»: пока стоит эта пауза, обычные вызовы падают
        # сразу, а не ждут по 8 с каждый (см. _run).
        self._timeouts = 0
        self._busy_until = 0.0
        self._busy_since: float | None = None
        self._last_ok: dict[str, Any] | None = None
        # Сколько ждать ответа в «прощупывающем» режиме, сколько — на повтор
        # после обрыва, и до какого предела растёт пауза между попытками.
        self.busy_probe_budget = 3.0
        self.busy_retry_budget = 2.0
        self.busy_max_pause = 20.0

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

    def _reset_client(self) -> None:
        """Забывает соединение: следующий вызов откроет новое.

        После простоя (сон диска, засыпание экрана, пауза машины) старый сокет
        уже мёртв, и первый запрос висит до таймаута. Переподключение дешевле.
        """
        self._client = None
        self._sig = None
        self._session_synced = False

    # -- «демон занят диском» ------------------------------------------
    def busy_for(self) -> float:
        """Сколько секунд ещё действует пауза (0 — демон свободен)."""
        return max(0.0, self._busy_until - time.monotonic())

    def busy_text(self) -> str:
        waited = time.monotonic() - self._busy_since if self._busy_since else 0.0
        tail = f" (уже {int(waited)} с)" if waited >= 1 else ""
        return f"Transmission занят записью на диск и не отвечает{tail}"

    def _mark_busy(self, exc: Exception) -> float:
        """Ставит паузу после обрыва и возвращает её длину."""
        self._timeouts += 1
        delay = min(self.busy_max_pause, float(2 ** (self._timeouts - 1)))
        self._busy_until = time.monotonic() + delay
        if self._busy_since is None:
            self._busy_since = time.monotonic()
        return delay

    def _mark_alive(self) -> None:
        if self._timeouts:
            log.info("Transmission снова отвечает после %s обрывов", self._timeouts)
        self._timeouts = 0
        self._busy_until = 0.0
        self._busy_since = None

    async def _run(self, fn, *, budget: float = 8.0, probe: bool = False, ignore_pause: bool = False):
        """Блокирующий вызов RPC: короткий бюджет, без шторма повторов.

        Клиент transmission-rpc синхронный, поэтому вызов уходит в поток. При
        обрыве первого ответа бывает достаточно переподключиться — поэтому один
        повтор (с коротким бюджетом). Но если демон завис на записи, повтор
        только удваивал бы ожидание (было 8 с + 8 с), и панель, обновляющаяся
        каждые 5 с, вставала вместе с ним.

        Поэтому после обрыва включается пауза: обычные вызовы сразу бросают
        `TransmissionBusy`, а `probe=True` (короткий бюджет, одна попытка)
        проверяет, ожил ли демон, и снимает паузу. `ignore_pause=True` — для
        действий по кнопке (поставить/убрать раздачу): их лучше попробовать,
        чем отказать из-за недавнего обрыва.
        """
        import transmission_rpc as trpc

        busy = self.busy_for()
        if busy > 0 and not probe and not ignore_pause:
            raise TransmissionBusy(self.busy_text(), waited=busy)

        transient = (
            asyncio.TimeoutError,
            trpc.TransmissionTimeoutError,
            trpc.TransmissionConnectError,
            ConnectionError,
            OSError,
        )
        timeout = min(budget, self.busy_probe_budget) if probe else budget
        # Повтор нужен для другого случая: мёртвый сокет после простоя — тогда
        # второй заход с новым соединением отвечает мгновенно. Поэтому он с
        # коротким бюджетом: при зависшем на записи демоне ожидание не
        # удваивается (было 8 + 8), а сразу переходит в паузу.
        retry_timeout = min(budget, self.busy_retry_budget)
        schedule = [(1, timeout)] if probe else [(1, timeout), (2, retry_timeout)]
        last: Exception | None = None
        for attempt, limit in schedule:
            try:
                result = await asyncio.wait_for(asyncio.to_thread(fn), timeout=limit)
            except transient as exc:  # noqa: PERF203 - два подхода, не цикл
                last = exc
                pause = self._mark_busy(exc)
                self._reset_client()
                # В журнал — только начало заминки. Дальше панель опрашивает
                # демон каждые несколько секунд, и каждый такой таймаут
                # превращался в новую запись: журнал тонул в одинаковых строках.
                if self._timeouts == 1:
                    journal.add(
                        "transmission",
                        "RPC не ответил — жду, пока демон освободится",
                        {
                            "ждал": f"{limit:g} с",
                            "пауза": f"{pause:.0f} с",
                            "ошибка": f"{type(exc).__name__}: {exc}",
                        },
                        level="warn",
                    )
            except Exception:
                self._reset_client()
                raise
            else:
                self._mark_alive()
                return result
        raise TransmissionBusy(
            f"Transmission не ответил за {int(timeout)} с: {type(last).__name__}",
            waited=float(self._timeouts),
        )

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
        """Проверка связи для панели — мягкая к занятому диском демону.

        Единственный вызов, которому разрешено стучаться во время паузы: когда
        демон занят записью, он идёт в «прощупывающем» режиме (короткий бюджет,
        без повтора). Если ответа нет, возвращаем последние известные данные, а
        не пустоту: панель не должна выглядеть сломанной из-за записи на диск.
        """

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
            result = await self._run(run, budget=6.0, probe=self.busy_for() > 0)
        except TransmissionBusy as exc:
            return self._unreachable(exc, busy=True)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            return self._unreachable(exc, busy=False)
        try:
            result["storage"] = self.storage_info().to_dict()
        except Exception as exc:  # noqa: BLE001 - про диск панель скажет отдельно
            log.debug("не удалось прочитать состояние диска: %s", exc)
        self._last_ok = dict(result)
        return result

    def _unreachable(self, exc: Exception, *, busy: bool) -> dict[str, Any]:
        """Ответ «связи нет»: с последними известными данными, если они были."""
        stale = dict(self._last_ok or {})
        stale.update(
            {
                "ok": False,
                "busy": busy,
                "error": self.busy_text() if busy else self._explain(exc),
                "stale": bool(stale),
                "measured_at": time.strftime("%H:%M", time.localtime()),
            }
        )
        return stale

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
        cache_mb = int(self.settings.transmission_cache_mb or 0)
        speed_mbps = float(self.settings.transmission_speed_limit_mbps or 0)
        speed_kbps = int(speed_mbps * 1024) if speed_mbps > 0 else 0

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
            # Разгрузка диска: крупный кэш = редкая, но большая запись, а
            # потолок скорости оставляет демону время отвечать на RPC. Оба
            # параметра ставим только если они разошлись с нашими.
            have_cache = int(getattr(session, "cache_size_mib", 0) or 0)
            if cache_mb and have_cache != cache_mb:
                changes["cache_size_mb"] = cache_mb
            have_limit = int(getattr(session, "speed_limit_down", 0) or 0)
            limit_on = bool(getattr(session, "speed_limit_down_enabled", False))
            if speed_kbps and (not limit_on or have_limit != speed_kbps):
                changes["speed_limit_down_enabled"] = True
                changes["speed_limit_down"] = speed_kbps
            if changes:
                client.set_session(**changes)
                log.info(
                    "Transmission: каталог %s, кэш %s МБ, потолок %s МБ/с",
                    target,
                    cache_mb or "как есть",
                    f"{speed_mbps:g}" if speed_kbps else "нет",
                )
            else:
                log.debug("Transmission: настройки сессии уже наши")

        try:
            await self._run(run, budget=15.0)
            self._session_synced = True
        except Exception as exc:  # noqa: BLE001 - не смертельно: путь передаём явно
            log.warning("не удалось обновить настройки каталога Transmission: %s", exc)

    async def add(
        self,
        torrent_path: Path,
        *,
        need_bytes: int | None = None,
        files_wanted: list[int] | None = None,
    ) -> dict[str, Any]:
        """Ставит .torrent в очередь на внешний диск.

        `files_wanted` — индексы файлов внутри раздачи (нужно, чтобы качать одну
        серию, а не весь сезон). Всё остальное помечается как ненужное, поэтому
        Transmission качает только выбранное, а не «всё, потом отменить».

        Бросает NotEnoughSpace/StorageUnavailable — проверка до постановки.
        """
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
            torrent_hash = (
                getattr(result, "hash_string", "") or getattr(result, "hashString", "")
            )
            selected: list[int] = []
            if files_wanted is not None:
                files = client.get_torrent(torrent_hash).get_files()
                wanted = {int(index) for index in files_wanted}
                unwanted = [item.id for item in files if item.id not in wanted]
                client.change_torrent(
                    torrent_hash,
                    files_wanted=sorted(index for index in wanted if index < len(files)),
                    files_unwanted=unwanted,
                )
                selected = sorted(wanted)
                journal.add(
                    "transmission",
                    f"выбраны файлы раздачи: {len(selected)} из {len(files)}",
                    {"индексы": selected[:20]},
                )
            return {
                "ok": True,
                "name": getattr(result, "name", ""),
                "hash": torrent_hash,
                "id": getattr(result, "id", None),
                "download_dir": str(self.download_dir),
                "files_selected": selected,
                "free_after_human": human_bytes(max(0, info.free - (need_bytes or 0))),
            }

        try:
            result = await self._run(run, ignore_pause=True)
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

    async def progress(self, hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Прогресс раздач по хешам: процент, скорость, остаток, статус.

        Transmission RPC не умеет присылать события — только отвечать на запросы,
        поэтому прогресс приходится опрашивать. Один вызов на все нужные раздачи.
        """
        wanted = {item for item in hashes if item}
        if not wanted:
            return {}

        def run() -> dict[str, dict[str, Any]]:
            client = self._get_client()
            result: dict[str, dict[str, Any]] = {}
            for torrent in client.get_torrents():
                torrent_hash = getattr(torrent, "hash_string", "")
                if torrent_hash not in wanted:
                    continue
                percent = float(getattr(torrent, "percent_done", 0.0) or 0.0)
                eta = getattr(torrent, "eta", None)
                try:
                    eta_seconds = int(eta) if eta is not None else None
                except (TypeError, ValueError):
                    eta_seconds = None
                rate = int(getattr(torrent, "rate_download", 0) or 0)
                # size_when_done — объём ТОЛЬКО выбранных файлов: если качаем одну
                # серию из пачки, показывать «29 ГБ» неверно.
                selected = int(getattr(torrent, "size_when_done", 0) or 0)
                total = selected or int(getattr(torrent, "total_size", 0) or 0)
                done = int(percent * total)
                result[torrent_hash] = {
                    "hash": torrent_hash,
                    "name": getattr(torrent, "name", ""),
                    "percent": round(percent * 100, 1),
                    "done": percent >= 0.999,
                    "status": str(getattr(torrent, "status", "")),
                    "rate": rate,
                    "rate_human": human_bytes(rate) + "/с" if rate else "",
                    "eta_seconds": eta_seconds if eta_seconds and eta_seconds > 0 else None,
                    "eta_human": _human_eta(eta_seconds) if eta_seconds and eta_seconds > 0 else "",
                    "size": total,
                    "size_human": human_bytes(total),
                    "downloaded_human": human_bytes(done),
                    "left_human": human_bytes(max(0, total - done)),
                }
            return result

        try:
            return await self._run(run)
        except TransmissionBusy as exc:
            # Демон занят записью — это не ошибка, просто ждём следующего опроса.
            log.debug("прогресс отложен: %s", exc)
            return {}
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось получить прогресс: %s", exc)
            return {}

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

        return await self._run(run)

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
            result = await self._run(run, ignore_pause=True)
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
            return await self._run(run)
        except Exception as exc:  # noqa: BLE001
            raise TransmissionError(self._explain(exc)) from exc

    # ------------------------------------------------------------------
    # Перезапуск демона и разгрузка диска
    # ------------------------------------------------------------------
    async def startup_tuning(self) -> None:
        """Приводит настройки сессии в порядок сразу при старте приложения.

        Раньше каталог/кэш выставлялись только при первой постановке раздачи,
        то есть уже после того, как диск начали грузить.
        """
        self._session_synced = False
        await self._sync_session_dir()

    def _kill_daemon(self) -> dict[str, Any]:
        """Синхронная часть перезапуска: гасит процесс (SIGTERM, потом SIGKILL)."""
        pids = daemon_pids()
        if not pids:
            return {"ok": True, "killed": False, "forced": False}
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 8
        while time.time() < deadline and daemon_pids():
            time.sleep(0.3)
        forced = False
        if daemon_pids():
            # Зависший в записи процесс SIGTERM не замечает: он выйдет только
            # когда вернётся системный вызов, а SIGKILL лишь пометит его на
            # выход. Поэтому ждём здесь долго, а не считаем «не убился».
            forced = True
            for pid in daemon_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.time() + 40
            while time.time() < deadline and daemon_pids():
                time.sleep(0.5)
        left = daemon_pids()
        if left:
            return {
                "ok": False,
                "killed": False,
                "forced": forced,
                "pids": left,
                "error": "Transmission не завершается: держит зависшую запись на диск",
            }
        return {"ok": True, "killed": True, "forced": forced}

    @staticmethod
    def _open_app() -> bool:
        try:
            result = subprocess.run(
                ["open", "-a", "Transmission"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    async def restart_daemon(self, *, wait_ready: float = 60.0) -> dict[str, Any]:
        """Гасит зависший Transmission и поднимает его заново.

        Зачем это здесь: демон умеет намертво застрять в записи на внешний
        диск (процесс в состоянии `U`). Тогда RPC мёртв, а сам себя демон уже
        не отпустит — SIGTERM игнорируется, SIGKILL срабатывает только после
        возврата зависшей записи. До этого лечилось исключительно терминалом.

        Возвращает честный отчёт: что сделали, сколько ждали, поднялся ли и
        с какой версией. Данные скачанного Transmission пересчитает сам.
        """
        killed = await asyncio.to_thread(self._kill_daemon)
        if not killed.get("ok"):
            return {**killed, "restarted": False}
        if not await asyncio.to_thread(self._open_app):
            return {
                "ok": False,
                "restarted": False,
                "forced": killed.get("forced", False),
                "error": "не удалось запустить Transmission.app",
            }
        self._reset_client()
        self._mark_alive()
        # После перезапуска настройки сессии надо применить заново.
        self._session_synced = False

        def ping() -> dict[str, Any]:
            session = self._get_client().get_session()
            return {"version": getattr(session, "version", "")}

        started = time.monotonic()
        deadline = started + wait_ready
        while time.monotonic() < deadline:
            try:
                info = await self._run(ping, budget=5.0, probe=True)
            except Exception:  # noqa: BLE001 - ещё поднимается
                await asyncio.sleep(2.0)
                continue
            waited = round(time.monotonic() - started, 1)
            journal.add(
                "transmission",
                "Transmission перезапущен",
                {
                    "пришлось убивать": killed.get("forced", False),
                    "ждали": f"{waited} с",
                    "версия": info.get("version", ""),
                },
            )
            await self._sync_session_dir()
            return {
                "ok": True,
                "restarted": True,
                "killed": killed.get("killed", False),
                "forced": killed.get("forced", False),
                "waited": waited,
                "version": info.get("version", ""),
            }
        return {
            "ok": False,
            "restarted": True,
            "forced": killed.get("forced", False),
            "error": f"Transmission не ответил за {int(wait_ready)} с после перезапуска",
        }
