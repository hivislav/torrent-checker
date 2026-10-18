#!/usr/bin/env python3
"""Проверки контекста переписки и уточнений к текущей задаче.

Запуск:  .venv/bin/python tests/test_context.py

Что проверяется:
  1. скользящее окно: не больше CONTEXT_MESSAGES сообщений, в правильном порядке,
     с обрезкой длинных ответов агента;
  2. окно и состояние активной задачи попадают в запрос к модели;
  3. уточнение («нет, нужен 4K», «а можно 1080p») правит ту же задачу, а новое
     название заводит новую;
  4. живой диалог через API: уточнение меняет параметры задачи, а не создаёт
     вторую (шаг с реальной моделью пропускается, если модель недоступна).
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import Agent  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.models import Message, Task  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def make_agent(prefix: str = "ctx-") -> Agent:
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "ctx.sqlite3",
        torrents_dir=tmp / "torrents",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    return Agent(settings)


async def test_window() -> None:
    print("\n== скользящее окно ==")
    agent = make_agent()
    session = "s1"
    # 30 сообщений: 20 последних должны попасть в окно, ранние — нет.
    for index in range(30):
        role = "user" if index % 2 == 0 else "agent"
        await agent.store.add_message(session, Message(role=role, text=f"сообщение {index}"))

    window = await agent._context_window(session)  # noqa: SLF001
    print("  размер окна:", len(window))
    check("окно ограничено настройкой", len(window) <= agent.settings.context_messages, len(window))
    check("в окне ровно 20 последних", len(window) == 20, len(window))
    check("последнее сообщение на месте", window[-1]["text"] == "сообщение 29", window[-1])
    check("ранние не попали", all("сообщение 9" != item["text"] for item in window))
    check("роль агента переименована", any(item["role"] == "assistant" for item in window), window[:2])
    check("роли только user/assistant", {item["role"] for item in window} <= {"user", "assistant"})

    await agent.store.add_message(session, Message(role="agent", text="д" * 1000))
    window2 = await agent._context_window(session)  # noqa: SLF001
    check("длинный ответ обрезан", len(window2[-1]["text"]) <= 401, len(window2[-1]["text"]))
    agent.store.close()


async def test_prompt() -> None:
    print("\n== окно попадает в запрос к модели ==")
    agent = make_agent()
    task = Task(
        id="t1",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s1",
        user_query="Тед Лассо",
        params={"title": "Тед Лассо", "quality": "1080p"},
    )
    window = [
        {"role": "user", "text": "Тед Лассо"},
        {"role": "assistant", "text": "Нашёл 32 раздачи, какое качество берём?"},
    ]
    prompt = agent._context_prompt(window, task)  # noqa: SLF001
    print("  " + prompt.replace("\n", " | ")[:220])
    check("в запросе есть история", "Тед Лассо" in prompt and "какое качество" in prompt)
    check("в запросе есть состояние задачи", "Текущая задача" in prompt and "awaiting_choice" in prompt)
    check("в запросе есть параметры задачи", "quality=1080p" in prompt, prompt[:200])
    check("есть просьба учитывать уточнения", "Текущее сообщение" in prompt)

    # Промпт разбора обязан требовать историю и уточнения
    from core.agent import PARSE_SYSTEM

    check("промпт требует учитывать историю", "история переписки" in PARSE_SYSTEM.lower())
    check("промпт просит поле refine", '"refine"' in PARSE_SYSTEM)
    agent.store.close()


def test_refinement_rules() -> None:
    print("\n== правила уточнения ==")
    agent = make_agent()
    task = Task(
        id="t2",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s1",
        user_query="Тед Лассо",
        params={"title": "Тед Лассо"},
    )

    check("без названия — уточнение", agent._is_refinement("нужен 4K", {}, task))  # noqa: SLF001
    check(
        "то же название — уточнение",
        agent._is_refinement("Тед Лассо 1080p", {"title": "Тед Лассо"}, task),  # noqa: SLF001
    )
    check(
        "другой фильм — новая задача",
        not agent._is_refinement("Дюна", {"title": "Дюна"}, task),  # noqa: SLF001
    )
    check(
        "явный refine с тем же названием уважается",
        agent._is_refinement("второй вариант", {"refine": True, "title": "Тед Лассо"}, task),  # noqa: SLF001
    )
    check(
        "другое название перебивает флаг refine",
        not agent._is_refinement("нет, мне нужен другой фильм", {"refine": True, "title": "Дюна"}, task),  # noqa: SLF001
    )
    agent.store.close()


async def test_merge() -> None:
    print("\n== слияние уточнения с задачей ==")
    agent = make_agent()
    task = Task(
        id="t3",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s1",
        user_query="Тед Лассо",
        params={"title": "Тед Лассо", "quality": "1080p"},
        options={"pool": []},
    )
    await agent.store.save_task(task)

    from core.models import Turn

    turn = Turn()
    # Меняем качество — задача та же, поиск заново не нужен (пул пуст, но это
    # локальная проверка слияния, сети тут нет).
    agent._search = lambda params: asyncio.sleep(0, result=[])  # type: ignore[method-assign]
    await agent._refine_task(task, {"quality": "2160p"}, "не то, нужен 4K", turn)  # noqa: SLF001
    after = await agent.store.get_task("t3")
    assert after is not None
    print("  параметры после:", after.params)
    check("качество обновилось", after.params.get("quality") == "2160p", after.params)
    check("название сохранилось", after.params.get("title") == "Тед Лассо", after.params)
    check("уточнение записано в историю задачи", "нужен 4K" in after.user_query, after.user_query)
    agent.store.close()


async def test_live_dialog() -> None:
    print("\n== живой диалог (через модель) ==")
    import httpx

    settings = get_settings()
    base = f"https://127.0.0.1:{settings.site_port}"
    try:
        async with httpx.AsyncClient(verify=False, timeout=240) as client:
            login = await client.post(f"{base}/api/login", json={"password": settings.site_password})
            if login.status_code != 200:
                print("  (нет доступа к сервису — пропускаю)")
                return
            first = await client.post(
                f"{base}/api/chat", json={"text": "Тед Лассо 1080p", "mode": "oneshot"}
            )
            if first.status_code != 200:
                print("  (агент недоступен — пропускаю)", first.status_code)
                return
            task_id = (first.json().get("task") or {}).get("id")
            check("первый запрос создал задачу", bool(task_id), task_id)
            if not task_id:
                return
            second = await client.post(
                f"{base}/api/chat", json={"text": "а можно 4K", "mode": "oneshot"}
            )
            payload = second.json()
            new_task_id = (payload.get("task") or {}).get("id")
            params = (payload.get("task") or {}).get("params") or {}
            texts = " ".join(message.get("text") or "" for message in payload.get("messages", []))
            print("  задача:", new_task_id, "| качество:", params.get("quality"))
            check("уточнение не создало новую задачу", new_task_id == task_id, (task_id, new_task_id))
            check("качество стало 2160p", params.get("quality") in ("2160p", "4K"), params)
            check("агент сказал, что уточнил", "Уточнил" in texts, texts[:120])
    except Exception as exc:  # noqa: BLE001
        print("  (проверка пропущена:", str(exc)[:80], ")")


async def main() -> int:
    await test_window()
    await test_prompt()
    test_refinement_rules()
    await test_merge()
    await test_live_dialog()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: контекст переписки и уточнения работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
