"""Клиент DeepSeek (модель deepseek-flash = DeepSeek-V4.1-Flash).

Два решения, которые здесь зашиты:

1. **Размышления выключены.** В запрос всегда уходит `thinking: {"type": "disabled"}`
   (проверено: без этого параметра модель возвращает `reasoning_content` и тратит
   на него токены, с ним — сразу отвечает). Агенту нужен разбор запроса и текст
   ответа, а не цепочка рассуждений: она только добавляет задержку.
2. **Строгий JSON для разбора.** Модель просят вернуть только JSON, а разбор
   здесь терпимый: снимаем ```-обёртки, ищем сбалансированный объект. Если
   совсем не вышло — возвращаем пустой словарь, и агент работает по правилам
   (см. core/agent.py), а не падает.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from core.config import Settings, get_settings
from core.debuglog import journal

log = logging.getLogger("llm")


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели, даже если тот обёрнут в ```."""
    if not text:
        return {}
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    if start == -1:
        return {}
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                chunk = cleaned[start : index + 1]
                try:
                    data = json.loads(chunk)
                    return data if isinstance(data, dict) else {}
                except ValueError:
                    return {}
    return {}


class LLM:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self.last_error: str = ""

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.llm_base_url,
                timeout=httpx.Timeout(self.settings.llm_timeout, connect=15.0),
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # базовый вызов
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        label: str = "запрос",
    ) -> LLMResult:
        if not self.settings.llm_api_key:
            raise LLMError("DEEPSEEK_API_KEY не задан — модель недоступна")

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            # Размышления выключены — см. docstring модуля.
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        journal.add(
            "llm",
            f"→ модель: {label}",
            {
                "модель": self.settings.llm_model,
                "сообщений": len(messages),
                "символов в запросе": prompt_chars,
                "json": json_mode,
                "инструменты": [tool.get("function", {}).get("name") for tool in (tools or [])] or None,
                "последнее сообщение": (messages[-1].get("content") if messages else ""),
            },
        )
        client = await self._http()
        last_error = ""
        for attempt in range(3):
            try:
                response = await client.post("/chat/completions", json=payload)
                if response.status_code >= 400:
                    body = response.text[:400]
                    last_error = f"HTTP {response.status_code}: {body}"
                    journal.add(
                        "llm",
                        f"← модель: ошибка {response.status_code}",
                        {"ответ сервера": body, "label": label},
                        duration_ms=(time.perf_counter() - started) * 1000,
                        level="error",
                    )
                    # 429/5xx имеет смысл повторить, 4xx — нет.
                    if response.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise LLMError(last_error)
                data = response.json()
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                result = LLMResult(
                    text=(message.get("content") or "").strip(),
                    tool_calls=message.get("tool_calls") or [],
                    raw=message,
                    usage=data.get("usage") or {},
                )
                self.last_error = ""
                journal.add(
                    "llm",
                    f"← модель: {label}",
                    {
                        "модель": data.get("model") or self.settings.llm_model,
                        "ответ": result.text,
                        "вызовы инструментов": [
                            {
                                "имя": (call.get("function") or {}).get("name"),
                                "аргументы": (call.get("function") or {}).get("arguments"),
                            }
                            for call in result.tool_calls
                        ]
                        or None,
                        "токены": result.usage,
                    },
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
                return result
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.5 * (attempt + 1))
        self.last_error = last_error
        raise LLMError(f"модель не ответила: {last_error}")

    # ------------------------------------------------------------------
    # удобные обёртки
    # ------------------------------------------------------------------
    async def ask_json(self, system: str, user: str, *, max_tokens: int | None = None) -> dict[str, Any]:
        result = await self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=max_tokens or 900,
            json_mode=True,
            label="разбор запроса",
        )
        data = extract_json(result.text)
        if not data:
            log.warning("модель вернула не-JSON: %s", result.text[:200])
        return data

    async def ask_text(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        result = await self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            label="текст ответа",
        )
        return result.text

    async def tool_loop(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]],
        *,
        max_steps: int = 4,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Цикл вызова инструментов: модель просит — код выполняет — модель отвечает."""
        transcript: list[dict[str, Any]] = list(messages)
        trace: list[dict[str, Any]] = []
        for _ in range(max_steps):
            result = await self.chat(transcript, tools=tools, label="шаг с инструментами")
            if not result.tool_calls:
                return result.text, trace
            transcript.append(result.raw)
            for call in result.tool_calls:
                name = (call.get("function") or {}).get("name") or ""
                raw_args = (call.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except ValueError:
                    args = {}
                handler = handlers.get(name)
                if handler is None:
                    output: Any = {"error": f"неизвестный инструмент {name}"}
                else:
                    try:
                        output = await handler(args)
                    except Exception as exc:  # noqa: BLE001 - ошибку показываем модели
                        output = {"error": f"{type(exc).__name__}: {exc}"}
                trace.append({"tool": name, "args": args, "result": output})
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or "",
                        "content": json.dumps(output, ensure_ascii=False)[:6000],
                    }
                )
        final = await self.chat(transcript, label="итоговый ответ")
        return final.text, trace
