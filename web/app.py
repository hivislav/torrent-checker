"""Веб-адаптер агента: тонкий слой FastAPI над core.agent.Agent.

Здесь только транспорт: вход по паролю, JSON-маршруты, SSE-поток событий и
раздача статики. Никакой логики поиска и скачивания тут нет — она в ядре,
поэтому тот же агент уедет в Telegram-бота без изменений.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.agent import Agent
from core.debuglog import configure as configure_journal
from core.debuglog import install_logging_bridge, journal
from core.config import get_settings
from core.models import Message

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()
WEB_DIR = Path(__file__).resolve().parent
agent: Agent = Agent(settings)

SESSION_COOKIE = "kino_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30


# ----------------------------------------------------------------------
# Подпись cookie: пароля достаточно, но подделывать cookie тоже не нужно.
# ----------------------------------------------------------------------
def _secret() -> bytes:
    key_file = settings.data_dir / "session.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        key_file.write_bytes(secrets.token_bytes(32))
    return key_file.read_bytes()


def _sign(value: str) -> str:
    digest = hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{digest}"


def _verify(token: str) -> bool:
    if "." not in token:
        return False
    value, _, digest = token.rpartition(".")
    expected = hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)


def _auth_required() -> bool:
    return bool(settings.site_password)


async def current_session(request: Request) -> str:
    if not _auth_required():
        return "default"
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or not _verify(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="нужен вход")
    return "default"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Журнал отладки общий для процессов: шлюз пишет в тот же файл, поэтому его
    # события видны в интерфейсе наравне с вызовами модели и MCP.
    configure_journal(settings.data_dir / "debug.jsonl")
    # Предупреждения и ошибки логов попадают в тот же журнал, что и вызовы
    # модели/MCP — так на одном экране видно всю картину.
    install_logging_bridge()
    await agent.start()
    log.info("агент запущен, данные в %s", settings.data_dir)
    yield
    await agent.stop()


app = FastAPI(title="Кинозал-агент", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


# ----------------------------------------------------------------------
# Вход
# ----------------------------------------------------------------------
class LoginPayload(BaseModel):
    password: str = ""


@app.post("/api/login")
async def login(payload: LoginPayload, response: Response) -> dict[str, Any]:
    if not _auth_required():
        return {"ok": True, "auth_required": False}
    if not hmac.compare_digest(payload.password.encode(), settings.site_password.encode()):
        raise HTTPException(status_code=401, detail="неверный пароль")
    token = _sign(secrets.token_hex(16))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.site_tls,
    )
    return {"ok": True, "auth_required": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ----------------------------------------------------------------------
# Чат
# ----------------------------------------------------------------------
class ChatPayload(BaseModel):
    text: str
    mode: str = "chat"  # oneshot | periodic | chat
    task_id: str | None = None  # открытая задача: её переписка и есть контекст


class ActionPayload(BaseModel):
    kind: str
    payload: dict[str, Any] = {}


def _turn_to_json(turn) -> dict[str, Any]:
    return {
        "messages": [message.to_dict() for message in turn.messages],
        "task": turn.task.to_dict() if turn.task else None,
        # Правка пришла в задачу, которую нельзя править: интерфейс открывает
        # чистый лист новой задачи, как по кнопке «Новая задача».
        "start_new": bool(turn.start_new),
    }


@app.get("/api/history")
async def history(
    task_id: str = "", session: str = Depends(current_session)
) -> dict[str, Any]:
    """Переписка задачи (или последние сообщения сессии, если задача не выбрана)."""
    if task_id:
        return {"messages": await agent.history_for_task(task_id)}
    return {"messages": await agent.history(session)}


@app.post("/api/chat")
async def chat(payload: ChatPayload, session: str = Depends(current_session)) -> dict[str, Any]:
    turn = await agent.handle_message(session, payload.text, payload.mode, payload.task_id)
    return _turn_to_json(turn)


@app.post("/api/action")
async def action(payload: ActionPayload, session: str = Depends(current_session)) -> dict[str, Any]:
    turn = await agent.handle_action(session, payload.kind, payload.payload)
    return _turn_to_json(turn)


@app.get("/api/tasks")
async def tasks(
    all: bool = True, limit: int = 500, session: str = Depends(current_session)
) -> dict[str, Any]:
    # Лимит раньше был 40, и при 50+ задачах часть картотеки просто не приезжала:
    # «в работе» и «завершено» показывали меньше, чем есть, а периодические
    # задачи «исчезали» из списка.
    return {"tasks": await agent.list_tasks(include_finished=all, limit=limit)}


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str, session: str = Depends(current_session)) -> dict[str, Any]:
    task = await agent.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="задача не найдена")
    checks = await agent.store.list_checks(task_id, limit=10)
    return {"task": task.to_dict(), "checks": checks}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, session: str = Depends(current_session)) -> dict[str, Any]:
    task = await agent.cancel_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="задача не найдена")
    await agent.events.publish({"type": "task_cancelled", "task_id": task.id, "title": task.title})
    # note — что сделали со скачиванием: «раздача убрана из очереди, удалено 2,1 ГБ».
    return {"ok": True, "task": task.to_dict(), "note": task.last_result if task.state == "cancelled" else ""}


@app.get("/api/downloads")
async def downloads(limit: int = 50, session: str = Depends(current_session)) -> dict[str, Any]:
    items = await agent.downloads(limit)
    # meta — «Transmission занят диском»: список может быть от прошлого удачного
    # чтения, и панель должна сказать об этом, а не делать вид, что всё свежее.
    return {"downloads": items, "meta": agent.downloads_meta}


@app.post("/api/transmission/restart")
async def transmission_restart(session: str = Depends(current_session)) -> dict[str, Any]:
    """Перезапуск Transmission из панели: лечит зависший в записи демон."""
    return await agent.restart_transmission()


@app.get("/api/torrent/{torrent_id}/details")
async def torrent_details(
    torrent_id: str, session: str = Depends(current_session)
) -> dict[str, Any]:
    """Подробности раздачи для кнопки «Подробнее».

    Отдельный маршрут (а не сообщение в чат): клиент открывает их по клику,
    поэтому поиск и подтверждение не ждут лишнего запроса к трекеру.
    """
    try:
        return await agent.torrent_details(torrent_id)
    except Exception as exc:  # noqa: BLE001 - любая причина = «не открылось»
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc


@app.get("/api/debug")
async def debug_events(
    limit: int = 200,
    category: str = "all",
    after: int | None = None,
    session: str = Depends(current_session),
) -> dict[str, Any]:
    """Журнал отладки: вызовы модели, ответы MCP, шлюз, Transmission."""
    return {
        "events": journal.query(limit=min(limit, 400), category=category, after=after),
        "categories": journal.categories(),
    }


@app.delete("/api/debug")
async def debug_clear(session: str = Depends(current_session)) -> dict[str, Any]:
    journal.clear()
    return {"ok": True}


@app.get("/api/status")
async def status_endpoint(session: str = Depends(current_session)) -> dict[str, Any]:
    return await agent.status()


@app.get("/api/events")
async def events(request: Request, session: str = Depends(current_session)) -> StreamingResponse:
    """SSE: сюда прилетают уведомления периодических задач."""

    async def stream():
        queue = agent.events.subscribe()
        try:
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Комментарий-«пульс», чтобы прокси не рвал соединение.
                    yield ": ping\n\n"
        finally:
            agent.events.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class SettingsPayload(BaseModel):
    kinozal_username: str | None = None
    kinozal_password: str | None = None
    transmission_username: str | None = None
    transmission_password: str | None = None


@app.post("/api/settings")
async def save_settings(
    payload: SettingsPayload, session: str = Depends(current_session)
) -> dict[str, Any]:
    if payload.kinozal_username is not None:
        await agent.store.set_setting("kinozal_username", payload.kinozal_username.strip())
        object.__setattr__(settings, "kinozal_username", payload.kinozal_username.strip())
    if payload.kinozal_password is not None:
        await agent.store.set_setting("kinozal_password", payload.kinozal_password)
        object.__setattr__(settings, "kinozal_password", payload.kinozal_password)
    if payload.transmission_username is not None:
        await agent.store.set_setting("transmission_username", payload.transmission_username.strip())
    if payload.transmission_password is not None:
        await agent.store.set_setting("transmission_password", payload.transmission_password)
    await agent._load_overrides()
    return {"ok": True, "status": await agent.status()}


@app.post("/api/settings/test")
async def test_settings(session: str = Depends(current_session)) -> dict[str, Any]:
    """Проверка пары логин/пароль на kinozal.me (через шлюз) и Transmission."""
    import httpx

    result: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"http://{settings.gate_host}:{settings.gate_port}/login", params={"force": True}
            )
            result["kinozal"] = {
                "ok": response.status_code == 200 and bool(response.json().get("ok")),
                "detail": response.json() if response.status_code == 200 else response.text[:300],
            }
    except Exception as exc:  # noqa: BLE001
        result["kinozal"] = {"ok": False, "detail": str(exc)[:300]}
    result["transmission"] = await agent.transmission.test_connection()
    return result


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


# ----------------------------------------------------------------------
# Страница и PWA-мелочи
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    if _auth_required():
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token or not _verify(token):
            return FileResponse(WEB_DIR / "templates" / "login.html")
    return FileResponse(WEB_DIR / "templates" / "index.html")


class PushPayload(BaseModel):
    subscription: dict[str, Any] = {}


@app.get("/api/push/key")
async def push_key(session: str = Depends(current_session)) -> dict[str, Any]:
    await agent.push.prepare()
    return {
        "key": agent.push.public_key,
        "enabled": bool(agent.push.public_key),
    }


@app.post("/api/push/subscribe")
async def push_subscribe(
    payload: PushPayload, session: str = Depends(current_session)
) -> dict[str, Any]:
    ok = await agent.push.subscribe(payload.subscription)
    if not ok:
        raise HTTPException(status_code=400, detail="подписка без endpoint")
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(session: str = Depends(current_session)) -> dict[str, Any]:
    sent = await agent.push.send("Кинозал", "Проверка уведомлений: всё работает")
    return {"ok": True, "sent": sent}


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(WEB_DIR / "static" / "icon-192.png", media_type="image/png")


@app.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Кинозал-агент",
            "short_name": "Кинозал",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#100c08",
            "theme_color": "#1a120a",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "static" / "sw.js", media_type="application/javascript")
