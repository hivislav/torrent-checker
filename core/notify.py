"""События и уведомления — без привязки к конкретному каналу.

Агент сообщает о результате периодической задачи одним способом: публикует
событие в шину. Кто его покажет — решает адаптер:

  * веб: SSE-поток + центр уведомлений в браузере (web/app.py);
  * macOS: системное уведомление через osascript (здесь же, best-effort);
  * в будущем: Telegram-бот подпишется на ту же `EventBus`.

Так «отправка уведомлений» не зашита в UI и не ломается при его изменении.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

log = logging.getLogger("notify")


class EventBus:
    """Простая шина: издатель не ждёт подписчиков, медленный подписчик не тормозит."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._history: list[dict[str, Any]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @asynccontextmanager
    async def stream(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        self._history.append(event)
        if len(self._history) > 200:
            self._history = self._history[-200:]
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("подписчик не успевает читать события, событие пропущено")

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return self._history[-limit:]


class DesktopNotifier:
    """Системные уведомления macOS. Молча выключается там, где их нет."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and shutil.which("osascript") is not None

    async def send(self, title: str, message: str) -> bool:
        if not self.enabled:
            return False
        # Кавычки в тексте ломают AppleScript — экранируем.
        safe_title = title.replace('"', "'").replace("\\", "/")
        safe_message = message.replace('"', "'").replace("\\", "/")[:240]
        script = (
            f'display notification "{safe_message}" with title "{safe_title}" '
            f'sound name "Glass"'
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=5)
            return process.returncode == 0
        except Exception as exc:  # noqa: BLE001 - уведомление не должно ронять задачу
            log.debug("системное уведомление не отправлено: %s", exc)
            return False
