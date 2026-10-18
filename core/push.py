"""Web Push: уведомления, которые доходят при закрытой вкладке.

Зачем отдельный канал, если есть SSE: SSE работает только пока страница открыта.
Периодическая задача срабатывает ночью — разбудить её результат должен push.

Как это устроено:
  * при первом запуске генерируется пара ключей VAPID (data/vapid.json) —
    публичный отдаётся браузеру, приватным подписываются отправки;
  * подписки браузеров складываются в SQLite (settings.push_subscriptions);
  * отправка идёт через pywebpush; протухшие подписки (404/410) удаляются сами.

Если pywebpush не установлен, канал просто молчит: остаются центр уведомлений
в интерфейсе и системные уведомления macOS.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("push")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


@dataclass
class WebPush:
    store: Any
    data_dir: Path
    contact: str = "mailto:agent@localhost"
    _private_key: str = ""
    _public_key: str = ""
    _checked: bool = False
    subscriptions: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    async def _ensure_keys(self) -> bool:
        if self._public_key and self._private_key:
            return True
        stored_public = await self.store.get_setting("vapid_public")
        stored_private = await self.store.get_setting("vapid_private")
        if stored_public and stored_private:
            self._public_key, self._private_key = stored_public, stored_private
            return True
        if not self._checked:
            self._checked = True
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import ec
            except Exception as exc:  # noqa: BLE001
                log.info("web push недоступен (нет cryptography): %s", exc)
                return False
            private_key = ec.generate_private_key(ec.SECP256R1())
            public_bytes = private_key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
            private_bytes = private_key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self._public_key = _b64url(public_bytes)
            self._private_key = _b64url(private_bytes)
            await self.store.set_setting("vapid_public", self._public_key)
            await self.store.set_setting("vapid_private", self._private_key)
            log.info("сгенерированы ключи VAPID для web push")
        return bool(self._public_key and self._private_key)

    @property
    def public_key(self) -> str:
        return self._public_key

    async def prepare(self) -> None:
        """Готовит ключи заранее (вызывается на старте приложения)."""
        try:
            await self._ensure_keys()
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось подготовить web push: %s", exc)

    # ------------------------------------------------------------------
    async def _load_subscriptions(self) -> list[dict[str, Any]]:
        raw = await self.store.get_setting("push_subscriptions", "[]")
        try:
            data = json.loads(raw or "[]")
            return [item for item in data if isinstance(item, dict)]
        except ValueError:
            return []

    async def _save_subscriptions(self, items: list[dict[str, Any]]) -> None:
        await self.store.set_setting("push_subscriptions", json.dumps(items, ensure_ascii=False))

    async def subscribe(self, subscription: dict[str, Any]) -> bool:
        endpoint = str(subscription.get("endpoint") or "")
        if not endpoint:
            return False
        items = await self._load_subscriptions()
        items = [item for item in items if item.get("endpoint") != endpoint]
        items.append(subscription)
        await self._save_subscriptions(items[-50:])
        log.info("подписка на web push сохранена (всего %s)", len(items))
        return True

    async def send(self, title: str, body: str, url: str = "/") -> int:
        """Отправляет push всем подпискам. Возвращает число успешных доставок."""
        if not await self._ensure_keys():
            return 0
        items = await self._load_subscriptions()
        if not items:
            return 0
        try:
            from pywebpush import WebPushException, webpush
        except Exception:  # noqa: BLE001
            return 0

        payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
        alive: list[dict[str, Any]] = []
        sent = 0

        def deliver(subscription: dict[str, Any]) -> bool:
            try:
                webpush(
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=self._private_key,
                    vapid_claims={"sub": self.contact},
                    timeout=15,
                )
                return True
            except WebPushException as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code in (404, 410):
                    return False  # подписка отозвана
                log.warning("push не доставлен: %s", exc)
                return True  # сетевую ошибку не считаем поводом удалять подписку
            except Exception as exc:  # noqa: BLE001
                log.warning("push не доставлен: %s", exc)
                return True

        for item in items:
            ok = await asyncio.to_thread(deliver, item)
            if ok:
                alive.append(item)
                sent += 1
        if len(alive) != len(items):
            await self._save_subscriptions(alive)
        return sent
