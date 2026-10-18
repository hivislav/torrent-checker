"""HTTP-обёртка над CloudflareGate.

Отдельный процесс (127.0.0.1:8123), потому что шлюзом пользуются двое:
  * MCP-сервер kinozal-mcp (Node) — он не умеет держать Chrome, но умеет парсить;
  * сам агент — для проверки статуса и входа.

Слушает только петлю: наружу этот порт отдавать нельзя, он ходит по сайту под
нашей учёткой.

Запуск: .venv/bin/python -m uvicorn services.gate_service:app --port 8123
"""
from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from core.config import get_settings
from services.cf_gate import ChallengeBlocked, CloudflareGate, LoginFailed

log = logging.getLogger("gate-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

gate = CloudflareGate()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await gate.start()
        log.info("шлюз Cloudflare поднят")
    except Exception as exc:  # noqa: BLE001 - сервис должен жить и уметь пожаловаться
        log.error("не удалось поднять Chrome: %s", exc)
    yield
    await gate.stop()


app = FastAPI(title="Cloudflare gate", lifespan=lifespan)


class FetchRequest(BaseModel):
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    data: dict[str, str] | None = None
    # true — вернуть base64 (для .torrent и прочих бинарников)
    binary: bool = False
    retries: int = 2


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, **gate.status()}


@app.get("/status")
async def status() -> dict[str, Any]:
    return gate.status()


@app.post("/clearance")
async def clearance() -> dict[str, Any]:
    try:
        await gate.refresh_clearance()
    except ChallengeBlocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "cookies": sorted(gate._cookies)}


@app.post("/login")
async def login(force: bool = False) -> dict[str, Any]:
    try:
        cookies = await gate.login(force=force)
    except LoginFailed as exc:
        # 401 — «логин/пароль не приняты» (это не про Cloudflare, повторять бессмысленно)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ChallengeBlocked as exc:
        raise HTTPException(status_code=503, detail=f"Cloudflare: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": "pass" in cookies or "uid" in cookies, "cookies": sorted(cookies)}


@app.post("/fetch")
async def fetch(request: FetchRequest) -> dict[str, Any]:
    try:
        result = await gate.fetch(
            request.url,
            method=request.method,
            data=request.data,
            headers=request.headers or None,
            retries=request.retries,
        )
    except ChallengeBlocked as exc:
        raise HTTPException(status_code=503, detail=f"Cloudflare: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    payload: dict[str, Any] = {
        "status": result.status,
        "url": result.url,
        "via": result.via,
        "challenged": result.challenged,
        "headers": result.headers,
    }
    if request.binary:
        payload["body_b64"] = base64.b64encode(result.body).decode("ascii")
        payload["bytes"] = len(result.body)
    else:
        # kinozal отдаёт windows-1251 — пусть решает вызывающая сторона,
        # поэтому текст декодируем как cp1251 и отдаём utf-8-строкой.
        payload["text"] = result.text_cp1251()
        payload["bytes"] = len(result.body)
    return payload


@app.post("/api/fetch")
async def fetch_alias(request: FetchRequest) -> dict[str, Any]:
    """Псевдоним: MCP зовёт /api/fetch, чтобы путь не совпадал с именем функции."""
    return await fetch(request)
