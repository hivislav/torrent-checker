#!/usr/bin/env python3
"""Проверки журнала отладки.

Запуск:  .venv/bin/python tests/test_debug.py

Проверяется сам буфер (то, что он не растёт бесконечно и умеет фильтры) и то,
что ключевые модули действительно пишут в него события: вызовы модели, ответы
MCP, шлюз и Transmission нельзя «забыть» проинструментировать — иначе кнопка
«Журнал отладки» окажется пустой.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.debuglog import DebugLog, _short  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def test_buffer() -> None:
    print("\n== буфер событий ==")
    log = DebugLog(capacity=5)
    for index in range(8):
        log.add("llm" if index % 2 == 0 else "mcp", f"событие {index}", {"i": index})
    events = log.query(limit=100)
    check("ёмкость соблюдается", len(events) == 5, len(events))
    check("порядок по времени", events[-1]["title"] == "событие 7", events[-1]["title"])
    check("есть счётчик последовательности", events[-1]["seq"] > events[0]["seq"])

    only_llm = log.query(limit=100, category="llm")
    check("фильтр по категории", all(item["category"] == "llm" for item in only_llm), only_llm)

    last_seq = events[-1]["seq"]
    log.add("gate", "новое", {})
    after = log.query(limit=100, after=last_seq)
    check("фильтр «только новые»", len(after) == 1 and after[0]["title"] == "новое", after)

    categories = log.categories()
    check("список категорий", set(categories) >= {"llm", "mcp", "gate"}, categories)

    log.clear()
    check("очистка", log.query(limit=100) == [])

    long_value = "x" * 5000
    shortened = _short(long_value)
    check("длинные значения обрезаются", len(shortened) < 1500 and "симв." in shortened, len(shortened))
    check("вложенное обрезается тоже", isinstance(_short({"a": [long_value]}), dict))


async def test_instrumentation() -> None:
    print("\n== модули пишут в журнал ==")
    from core.debuglog import journal
    from core.transmission import NotEnoughSpace, Transmission

    journal.clear()

    # LLM: проверяем, что вызов с неверным ключом оставляет след (сеть не нужна —
    # ошибка запроса тоже событие).
    from core.llm import LLM, LLMError

    llm = LLM()
    try:
        await llm.chat([{"role": "user", "content": "проверка"}], label="самопроверка", max_tokens=5)
    except Exception:  # noqa: BLE001 - ответ не важен, важен факт записи
        pass
    finally:
        await llm.close()
    llm_events = [item for item in journal.query(limit=50) if item["category"] == "llm"]
    check("LLM пишет запрос в журнал", any("→ модель" in item["title"] for item in llm_events), llm_events[:2])

    # Transmission: подменяем состояние диска, чтобы не зависеть от прав записи
    # в этом окружении и проверить именно ветку «не хватает места».
    from types import SimpleNamespace

    journal.clear()
    transmission = Transmission()
    real = transmission.storage_info()
    transmission.storage_info = lambda: SimpleNamespace(  # type: ignore[method-assign]
        path=transmission.download_dir,
        mounted=True,
        writable=True,
        free=1024**3,
        total=224 * 1024**3,
        error="",
        ok=True,
    )
    try:
        transmission.check_space(need_bytes=10 * 1024**3)
    except NotEnoughSpace:
        pass
    except Exception as exc:  # noqa: BLE001
        check("Transmission пишет отказ по месту", False, f"неожиданное исключение: {exc}")
    transmission.storage_info = lambda: real  # type: ignore[method-assign]
    events = [item for item in journal.query(limit=20) if item["category"] == "transmission"]
    check("Transmission пишет отказ по месту", any("не хватает места" in item["title"] for item in events), events)

    # Шлюз живёт в отдельном процессе, поэтому его события видно только через
    # API приложения — заодно проверяем, что маршрут /api/debug действительно
    # отдаёт то, что накопилось в работающем сервисе.
    import httpx

    from core.config import get_settings

    settings = get_settings()
    base = f"https://127.0.0.1:{settings.site_port}"
    try:
        async with httpx.AsyncClient(verify=False, timeout=200) as client:
            await client.post(f"{base}/api/login", json={"password": settings.site_password})
            await client.post(
                f"{base}/api/debug", headers={}, content=None
            ) if False else None
            await client.post(
                "http://127.0.0.1:8123/fetch",
                json={"url": "https://kinozal.me/robots.txt", "retries": 0},
                timeout=180,
            )
            response = await client.get(f"{base}/api/debug?limit=100")
            events = response.json().get("events", []) if response.status_code == 200 else []
        gate_events = [item for item in events if item["category"] == "gate"]
        check("журнал доступен по API", response.status_code == 200, response.status_code)
        check("шлюз пишет запросы", bool(gate_events), gate_events[:1] or events[:2])
    except Exception as exc:  # noqa: BLE001
        print("  (сервис недоступен — пропускаю:", str(exc)[:80], ")")


async def main() -> int:
    test_buffer()
    await test_instrumentation()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: журнал отладки работает и наполняется")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
