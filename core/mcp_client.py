"""MCP-клиент к kinozal-mcp (stdio).

Агент общается с трекером не напрямую, а по протоколу Model Context Protocol:
запускает `node mcp/kinozal-mcp/build/index.js` дочерним процессом и зовёт его
инструменты. Это тот самый MCP из задачи (github.com/tokezooo/kinozal-mcp),
только с адресом kinozal.me и транспортом через шлюз Cloudflare.

Зачем так, а не «просто http-клиент»: инструменты трекера остаются сменными.
Захочется добавить второй трекер — это будет ещё один MCP-сервер рядом, а
ядро агента не изменится.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.config import Settings, get_settings
from core.debuglog import journal

log = logging.getLogger("mcp")


class MCPError(RuntimeError):
    pass


def _error_text(text: str) -> str:
    """Достаёт из ответа инструмента человекочитаемое сообщение.

    MCP-сервер отдаёт ошибку как JSON (`{"success": false, "message": …}`),
    и без разбора в интерфейс попадала бы вся структура вместо фразы.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return raw


class KinozalMCP:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.server_dir = self.settings.root / "mcp" / "kinozal-mcp"

    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._session is not None:
            return
        entry = self.server_dir / "build" / "index.js"
        if not entry.exists():
            raise MCPError(
                f"нет сборки MCP-сервера: {entry}. Соберите его: "
                f"cd {self.server_dir} && npm install && npm run build"
            )

        env = {
            "KINOZAL_BASE_URL": self.settings.kinozal_base_url,
            "KINOZAL_GATE_URL": f"http://{self.settings.gate_host}:{self.settings.gate_port}",
            "KINOZAL_USERNAME": self.settings.kinozal_username,
            "KINOZAL_PASSWORD": self.settings.kinozal_password,
            "NODE_ENV": "production",
        }
        params = StdioServerParameters(
            command="node",
            args=[str(entry)],
            env=env,
            cwd=str(self.server_dir),
        )
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            tools = await session.list_tools()
            log.info(
                "MCP kinozal-mcp подключён, инструменты: %s",
                ", ".join(tool.name for tool in tools.tools),
            )
        except Exception as exc:  # noqa: BLE001
            await self.stop()
            raise MCPError(f"не удалось запустить MCP-сервер: {exc}") from exc

    async def stop(self) -> None:
        self._session = None
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 - закрытие не должно падать
                pass
            self._stack = None

    @property
    def ready(self) -> bool:
        return self._session is not None

    # ------------------------------------------------------------------
    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            await self.start()
        assert self._session is not None
        started = time.perf_counter()
        journal.add("mcp", f"→ трекер: {tool}", dict(arguments))
        result = await self._session.call_tool(tool, arguments)
        text = ""
        for block in result.content:
            chunk = getattr(block, "text", None)
            if chunk:
                text += chunk
        # В Python-SDK поле называется is_error, в TypeScript-описании — isError.
        # Проверяем оба: иначе текст ошибки тихо возвращался как «результат»,
        # и поиск выглядел как «ничего не нашлось».
        failed = bool(
            getattr(result, "is_error", False) or getattr(result, "isError", False)
        )
        if failed:
            journal.add(
                "mcp",
                f"← трекер: {tool} — ошибка",
                {"ответ": text},
                duration_ms=(time.perf_counter() - started) * 1000,
                level="error",
            )
            raise MCPError(_error_text(text) or f"инструмент {tool} вернул ошибку")
        journal.add(
            "mcp",
            f"← трекер: {tool}",
            {"получено символов": len(text), "ответ": text},
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    # ------------------------------------------------------------------
    async def search(self, query: str, page: int = 0) -> list[dict[str, Any]]:
        data = await self.call("search_torrents", {"query": query, "page": page})
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [item for item in data["results"] if isinstance(item, dict)]
        return []

    async def search_pages(self, query: str, pages: int = 2) -> list[dict[str, Any]]:
        """Ищет по нескольким страницам: на трекере выдача легко переполняется."""
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(max(1, pages)):
            try:
                batch = await self.search(query, page)
            except MCPError:
                if page == 0:
                    raise
                break
            if not batch:
                break
            for item in batch:
                identifier = str(item.get("id") or "")
                if identifier and identifier in seen:
                    continue
                seen.add(identifier)
                collected.append(item)
            if len(batch) < 25:
                break
        return collected

    async def details(self, torrent_id: str) -> dict[str, Any]:
        data = await self.call("get_torrent_details", {"id": str(torrent_id)})
        return data if isinstance(data, dict) else {}

    async def download(self, torrent_id: str, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        data = await self.call(
            "download_torrent", {"id": str(torrent_id), "outputPath": str(output_path)}
        )
        if isinstance(data, dict):
            return data
        return {"success": False, "message": str(data)}
