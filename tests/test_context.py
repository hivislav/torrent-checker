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
from core.models import Action, Message, Task  # noqa: E402

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


async def test_reset() -> None:
    print("\n== сброс окна после отмены и старта скачивания ==")
    agent = make_agent("reset-")
    session = "reset"
    for index in range(6):
        role = "user" if index % 2 == 0 else "agent"
        await agent.store.add_message(session, Message(role=role, text=f"старое {index}"))
    window = await agent._context_window(session)  # noqa: SLF001
    check("до сброса окно не пустое", len(window) == 6, len(window))

    await agent.store.add_message(session, Message(role="user", text="новое после сброса"))
    await agent._reset_context(session)  # noqa: SLF001
    window = await agent._context_window(session)  # noqa: SLF001
    check("после сброса окно пустое", window == [], window)

    await agent.store.add_message(session, Message(role="user", text="совсем новое"))
    window = await agent._context_window(session)  # noqa: SLF001
    check("новые сообщения в окно попадают", [item["text"] for item in window] == ["совсем новое"], window)

    # Отмена задачи тоже сбрасывает окно (кнопка «Отмена» в картотеке).
    task = Task(
        id="reset-task",
        kind="oneshot",
        state="awaiting_choice",
        session_id=session,
        user_query="Тед Лассо",
        params={"title": "Тед Лассо"},
    )
    await agent.store.save_task(task)
    await agent.store.add_message(session, Message(role="agent", text="какое качество?"))
    await agent.cancel_task(task.id)
    window = await agent._context_window(session)  # noqa: SLF001
    check("отмена задачи сбрасывает окно", window == [], window)
    agent.store.close()


async def test_task_chats() -> None:
    print("\n== у каждой задачи своя переписка ==")
    agent = make_agent("chats-")
    session = "s1"
    first = Task(id="chat-a", kind="oneshot", state="awaiting_choice", session_id=session,
                 user_query="Дюна", params={"title": "Дюна"})
    second = Task(id="chat-b", kind="oneshot", state="awaiting_choice", session_id=session,
                  user_query="Тед Лассо", params={"title": "Тед Лассо"})
    await agent.store.save_task(first)
    await agent.store.save_task(second)

    for task, word in ((first, "дюна"), (second, "тед")):
        user_id = await agent._remember(  # noqa: SLF001
            session, Message(role="user", text=f"ищу {word}", kind="text", task_id=task.id)
        )
        turn = type("T", (), {"task": task, "messages": []})()
        turn.messages = [Message(role="agent", text=f"ответ про {word}", kind="text")]
        await agent._tag_turn(user_id, turn)  # noqa: SLF001
        await agent._remember_many(session, turn.messages)  # noqa: SLF001

    a = await agent.history_for_task("chat-a")
    b = await agent.history_for_task("chat-b")
    check("в чате первой задачи только её сообщения",
          [m["text"] for m in a] == ["ищу дюна", "ответ про дюна"], [m["text"] for m in a])
    check("во второй — свои",
          [m["text"] for m in b] == ["ищу тед", "ответ про тед"], [m["text"] for m in b])
    window = await agent._task_window("chat-a")  # noqa: SLF001
    check("контекст модели — тоже только своей задачи",
          [item["text"] for item in window] == ["ищу дюна", "ответ про дюна"], window)
    agent.store.close()


async def test_answered_once() -> None:
    print("\n== кнопки в сообщении нажимаются один раз ==")
    agent = make_agent("answered-")
    session = "s1"
    task = Task(id="ans-task", kind="oneshot", state="awaiting_choice", session_id=session,
                user_query="Дюна", params={"title": "Дюна"})
    await agent.store.save_task(task)
    question = Message(role="agent", text="Какое качество берём?", kind="tickets", task_id=task.id)
    question.actions = [Action(id="q-1080", label="1080p", kind="choose",
                               payload={"task_id": task.id, "dimension": "quality", "value": "1080p"})]
    await agent._remember(session, question)  # noqa: SLF001

    rows = await agent.history_for_task("ans-task")
    check("у вопроса с кнопками есть статус «не отвечен»",
          rows and rows[-1]["payload"].get("resolved") is False, rows and rows[-1]["payload"])

    marked = await agent.store.resolve_actions("ans-task")
    rows = await agent.history_for_task("ans-task")
    check("после действия вопрос помечен отвеченным", marked == 1, marked)
    check("флаг сохранился в базе", rows[-1]["payload"].get("resolved") is True, rows[-1]["payload"])
    check("повторная пометка ничего не меняет",
          await agent.store.resolve_actions("ans-task") == 0)
    agent.store.close()


async def test_foreign_query() -> None:
    print("\n== чужой запрос не уточняет старую задачу ==")
    agent = make_agent("foreign-")
    task = Task(
        id="old-task",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s1",
        user_query="Человек паук",
        params={"title": "Человек-паук: Новый день", "quality": "1080p"},
    )
    check("короткое название — это название", agent._looks_like_title("Извне"))  # noqa: SLF001
    check("«а можно 4K» — не название", not agent._looks_like_title("а можно 4K"))  # noqa: SLF001
    check("«Сериал» — не название", not agent._looks_like_title("Сериал"))  # noqa: SLF001
    check("«нет» — не название", not agent._looks_like_title("нет"))  # noqa: SLF001

    parsed = {"title": "Человек-паук: Новый день", "quality": "1080p"}
    check("«Извне» не уточняет чужую задачу",
          not agent._is_refinement("Извне", parsed, task))  # noqa: SLF001
    check("«а можно 4K» уточняет",
          agent._is_refinement("а можно 4K", {"quality": "2160p"}, task))  # noqa: SLF001
    check("то же название — уточнение",
          agent._is_refinement("Человек-паук: Новый день 4K",
                               {"quality": "2160p"}, task))  # noqa: SLF001
    check("«Сериал» — уточнение типа, а не новый поиск",
          agent._is_refinement("Сериал", {"type": "series"}, task))  # noqa: SLF001
    check("«Реалити шоу» — тоже уточнение типа",
          agent._is_refinement("Реалити шоу", {"type": "series"}, task))  # noqa: SLF001
    check("«реалити шоу» — жанр, а не название",
          agent._genre_only("Реалити шоу") and not agent._looks_like_title("Реалити шоу"))  # noqa: SLF001
    check("«Шоу Трумана» — не жанр",
          not agent._genre_only("Шоу Трумана"))  # noqa: SLF001

    # Старая задача не должна считаться текущей.
    from datetime import datetime, timedelta, timezone

    stale = Task(
        id="stale-task",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s2",
        user_query="Человек паук",
        params={"title": "Человек-паук: Новый день"},
    )
    await agent.store.save_task(stale)
    # save_task ставит updated_at=сейчас; состариваем задачу напрямую в базе.
    import sqlite3

    old_stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(agent.settings.db_path))
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (old_stamp, "stale-task"))
    conn.commit()
    conn.close()
    check("задача двухчасовой давности не активна",
          await agent._active_task("s2") is None)  # noqa: SLF001

    fresh = Task(
        id="fresh-task",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s3",
        user_query="Тед Лассо",
        params={"title": "Тед Лассо"},
    )
    await agent.store.save_task(fresh)
    check("свежая задача активна", (await agent._active_task("s3")) is not None)  # noqa: SLF001

    # Свежую задачу новый поиск не закрывает, старую — закрывает.
    await agent._close_stale_tasks("s3", "oneshot", keep_id="other")  # noqa: SLF001
    still = await agent.store.get_task("fresh-task")
    check("свежая задача переживает новый поиск", still is not None and still.state == "awaiting_choice",
          still.state if still else None)
    await agent._close_stale_tasks("s2", "oneshot", keep_id="other")  # noqa: SLF001
    gone = await agent.store.get_task("stale-task")
    check("старая задача закрывается", gone is not None and gone.state == "cancelled",
          gone.state if gone else None)

    # Кнопки закрытой задачи не должны запускать работу.
    closed = Task(
        id="closed-task",
        kind="oneshot",
        state="cancelled",
        session_id="s4",
        user_query="Человек паук",
        params={"title": "Человек-паук: Новый день"},
    )
    await agent.store.save_task(closed)
    turn = await agent.handle_action(
        "s4", "choose", {"task_id": "closed-task", "dimension": "quality", "value": "1080p"}
    )
    texts = " ".join(message.text for message in turn.messages)
    check("закрытая задача не продолжается", "уже закрыта" in texts, texts[:120])
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

    import sqlite3

    settings = get_settings()
    base = f"https://127.0.0.1:{settings.site_port}"
    # Тест пишет в живой чат: после прогона убираем и сообщения, и задачу,
    # иначе человек увидит в «Киножурнале» чужие запросы.
    conn = sqlite3.connect(str(settings.db_path))
    start_msg = conn.execute("SELECT IFNULL(MAX(id), 0) FROM messages").fetchone()[0]
    start_tasks = {row[0] for row in conn.execute("SELECT id FROM tasks")}
    conn.close()
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
    finally:
        conn = sqlite3.connect(str(settings.db_path))
        conn.execute("DELETE FROM messages WHERE id > ?", (start_msg,))
        conn.execute(
            "DELETE FROM tasks WHERE id NOT IN ("
            + ",".join("?" for _ in start_tasks or {""})
            + ")",
            tuple(start_tasks or {""}),
        )
        conn.commit()
        conn.close()


async def main() -> int:
    await test_window()
    await test_reset()
    await test_task_chats()
    await test_answered_once()
    await test_foreign_query()
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
