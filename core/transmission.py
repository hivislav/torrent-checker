"""Transmission RPC: постановка .torrent в очередь загрузки.

Клиент берётся готовый (transmission-rpc), вызовы синхронные — поэтому каждый
уходит в отдельный поток через `asyncio.to_thread`, чтобы не вешать цикл
событий на время сети.

Пароль RPC Transmission хранит в своём keychain, а не в настройках, поэтому
логин/пароль задаются в .env или прямо в интерфейсе агента (экран «Настройки»
пишет их в SQLite и перекрывает .env).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings

log = logging.getLogger("transmission")


class TransmissionError(RuntimeError):
    pass


class Transmission:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.host = self.settings.transmission_host
        self.port = self.settings.transmission_port
        self.username = self.settings.transmission_username
        self.password = self.settings.transmission_password
        self._client = None
        self._sig: tuple[str, int, str, str] | None = None

    # ------------------------------------------------------------------
    def configure(self, host: str, port: int, username: str, password: str) -> None:
        self.host, self.port = host, port
        self.username, self.password = username, password
        self._client = None
        self._sig = None

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
        return self._client

    # ------------------------------------------------------------------
    async def test_connection(self) -> dict[str, Any]:
        """Проверка связи: нужна и в UI, и перед постановкой в очередь."""

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
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            return {"ok": False, "error": self._explain(exc)}

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

    async def add(self, torrent_path: Path, *, download_dir: str | None = None) -> dict[str, Any]:
        """Ставит .torrent в очередь и возвращает имя и хеш раздачи."""

        def run() -> dict[str, Any]:
            client = self._get_client()
            data = Path(torrent_path).read_bytes()
            result = client.add_torrent(data, download_dir=download_dir)
            return {
                "ok": True,
                "name": getattr(result, "name", ""),
                "hash": getattr(result, "hashString", "") or getattr(result, "hash_string", ""),
                "id": getattr(result, "id", None),
            }

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            raise TransmissionError(self._explain(exc)) from exc

    async def list_torrents(self, limit: int = 50) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            client = self._get_client()
            torrents = client.get_torrents()
            items = []
            for torrent in torrents[:limit]:
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
                    }
                )
            return items

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise TransmissionError(self._explain(exc)) from exc
