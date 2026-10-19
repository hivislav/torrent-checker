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


async def test_nothing_new_inside_task() -> None:
    print("\n== внутри задачи новых задач не появляется ==")
    agent = make_agent("inside-")
    task = Task(id="inside-task", kind="oneshot", state="awaiting_choice", session_id="s1",
                user_query="Дюна", params={"title": "Дюна"})
    await agent.store.save_task(task)

    async def fake_parse(text, window, active):
        return {"title": "Тед Лассо", "quality": "1080p", "intent": "find"}

    async def fake_search(params):
        return []

    async def fake_selection(task_obj, turn):
        turn.add("ищу дальше", kind="text", task_id=task_obj.id)

    agent._parse_query = fake_parse  # noqa: SLF001
    agent._search = fake_search  # type: ignore[assignment]
    agent._selection_step = fake_selection  # type: ignore[assignment]

    turn = await agent.handle_message("s1", "Тед Лассо 1080p", "chat", task.id)
    check("задача осталась той же", turn.task is not None and turn.task.id == "inside-task",
          turn.task.id if turn.task else None)
    stored = await agent.store.get_task("inside-task")
    check("название изменилось внутри задачи",
          (stored.params or {}).get("title") == "Тед Лассо", stored.params)
    check("качество применилось", (stored.params or {}).get("quality") == "1080p", stored.params)
    all_tasks = await agent.store.list_tasks(limit=20)
    check("новых задач не появилось", len(all_tasks) == 1, [t.id for t in all_tasks])
    agent.store.close()


async def test_locked_tasks_start_new() -> None:
    """Текст при открытой задаче «в работе» или закрытой = новая задача.

    Правило владельца: править можно только задачу, которая ещё выбирается
    (поиск, качество, озвучка, серии, подтверждение). Задача в работе или
    закрытая не редактируется вовсе — текст в поле ввода делает то же, что
    кнопка «Новая задача». Раньше из-за этого «Брестская крепость» превратилась
    в «Ты все видишь», а раздача осталась качаться без присмотра.
    """
    print("\n== в работе и закрытые задачи не редактируются ==")
    agent = make_agent("locked-")

    async def fake_parse(text, window, active):
        return {"title": "Ты все видишь", "year": 2026, "intent": "find"}

    async def fake_parse_refine(text, window, active):
        return {"quality": "2160p", "intent": "find"}

    async def fake_search(params):
        return []

    async def fake_selection(task_obj, turn):
        turn.add("ищу дальше", kind="text", task_id=task_obj.id)

    class FakeMCP:
        """Заглушка трекера: настоящий MCP поднимает Chrome и портит цикл теста."""

        ready = True

        async def search_pages(self, query, pages=1):
            return []

        async def start(self):
            return None

        async def stop(self):
            return None

    agent._search = fake_search  # type: ignore[assignment]
    agent._selection_step = fake_selection  # type: ignore[assignment]
    agent.mcp = FakeMCP()  # type: ignore[assignment]

    # 1) Качающаяся задача: любой текст заводит новую, старую не трогаем.
    live = Task(
        id="live-task",
        kind="oneshot",
        state="downloading",
        session_id="s1",
        user_query="Брестская крепость",
        params={
            "title": "Брестская крепость",
            "type": "movie",
            "transmission_hash": "b333ced2",
            "progress": {"percent": 2.3, "done": False},
        },
    )
    await agent.store.save_task(live)
    agent._parse_query = fake_parse  # noqa: SLF001
    turn = await agent.handle_message(
        "s1", "Ты все видишь документальный фильм", "chat", "live-task"
    )
    stored = await agent.store.get_task("live-task")
    params = stored.params or {}
    check("название качающей задачи не изменилось",
          params.get("title") == "Брестская крепость", params.get("title"))
    check("её скачивание не тронуто",
          params.get("transmission_hash") == "b333ced2", params.get("transmission_hash"))
    check("задача всё ещё качает", stored.state == "downloading", stored.state)
    check("новая задача заведена отдельно",
          turn.task is not None and turn.task.id != "live-task",
          turn.task.id if turn.task else None)
    text = " ".join(message.text for message in turn.messages).lower()
    check("сказано, что сейчас качается", "качается" in text, text[:120])
    check("сказано, что задачу не трогают", "не трогаю" in text, text[:120])
    check("новая задача принадлежит новому запросу",
          turn.task is not None and str((turn.task.params or {}).get("title")) == "Ты все видишь",
          (turn.task.params or {}).get("title") if turn.task else None)
    check("интерфейсу сказано открыть чистый лист", turn.start_new is True, turn.start_new)

    # 2) Даже «уточнение» вида «а можно 4K» не правит качающую задачу.
    agent._parse_query = fake_parse_refine  # noqa: SLF001
    turn = await agent.handle_message("s1", "а можно 4K", "chat", "live-task")
    stored = await agent.store.get_task("live-task")
    check("качество качающей задачи не поменялось",
          (stored.params or {}).get("quality") != "2160p", (stored.params or {}).get("quality"))
    check("на текст без названия задача не заводится",
          turn.task is None, turn.task.id if turn.task else None)
    check("но чистый лист всё равно открывается", turn.start_new is True, turn.start_new)
    text = " ".join(message.text for message in turn.messages).lower()
    check("и попрошено название новой задачи", "название" in text, text[:160])
    check("второго «не понял, что искать» нет", len(turn.messages) == 1,
          [m.text[:40] for m in turn.messages])

    # 3) Периодическая в мониторинге: к ней пускает кнопка «Редактировать»,
    #    поэтому правка идёт внутри задачи (в интерфейсе поле ввода появляется
    #    только после нажатия этой кнопки).
    watching = Task(
        id="watch-task",
        kind="periodic",
        state="monitoring",
        session_id="s1",
        user_query="Ставка на любовь",
        params={"title": "Ставка на любовь", "quality": "1080p"},
    )
    await agent.store.save_task(watching)
    agent._parse_query = fake_parse  # noqa: SLF001
    turn = await agent.handle_message("s1", "Ты все видишь", "chat", "watch-task")
    stored = await agent.store.get_task("watch-task")
    check("мониторинг правится внутри задачи",
          turn.task is not None and turn.task.id == "watch-task",
          turn.task.id if turn.task else None)
    check("название задачи обновилось",
          (stored.params or {}).get("title") == "Ты все видишь",
          (stored.params or {}).get("title"))
    check("задача осталась в мониторинге", stored.state == "monitoring", stored.state)
    check("мониторинг помечен как редактируемый",
          stored.to_dict().get("editable") is True, stored.to_dict().get("editable"))

    # 4) Закрытая задача (отменена) — то же самое.
    closed = Task(
        id="closed-task",
        kind="oneshot",
        state="cancelled",
        session_id="s1",
        user_query="Тед Лассо",
        params={"title": "Тед Лассо", "quality": "1080p"},
    )
    await agent.store.save_task(closed)
    turn = await agent.handle_message("s1", "Ты все видишь", "chat", "closed-task")
    stored = await agent.store.get_task("closed-task")
    check("отменённая задача осталась отменённой", stored.state == "cancelled", stored.state)
    check("и не переименовалась",
          (stored.params or {}).get("title") == "Тед Лассо", (stored.params or {}).get("title"))
    check("для закрытой тоже новая задача",
          turn.task is not None and turn.task.id != "closed-task",
          turn.task.id if turn.task else None)

    # 5) Выбор ещё идёт — правка остаётся внутри задачи.
    agent._parse_query = fake_parse_refine  # noqa: SLF001
    picking = Task(
        id="pick-task",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s2",
        user_query="Дюна",
        params={"title": "Дюна", "quality": "1080p"},
    )
    await agent.store.save_task(picking)
    turn = await agent.handle_message("s2", "а можно 4K", "chat", "pick-task")
    stored = await agent.store.get_task("pick-task")
    check("на этапе выбора правка работает",
          turn.task is not None and turn.task.id == "pick-task",
          turn.task.id if turn.task else None)
    check("качество применилось",
          (stored.params or {}).get("quality") == "2160p", (stored.params or {}).get("quality"))
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

    # Новый поиск не закрывает прежние задачи: человек ушёл на другую задачу —
    # старая остаётся в работе, пока он сам её не отменит (правило владельца).
    async def fake_parse_new(text, window, active):
        return {"title": "Дюна", "intent": "find"}

    async def fake_search_empty(params):
        return []

    async def fake_selection_quiet(task_obj, turn):
        turn.add("ищу дальше", kind="text", task_id=task_obj.id)

    class FakeMCP:
        ready = True

        async def search_pages(self, query, pages=1):
            return []

        async def start(self):
            return None

        async def stop(self):
            return None

    agent._parse_query = fake_parse_new  # noqa: SLF001
    agent._search = fake_search_empty  # type: ignore[assignment]
    agent._selection_step = fake_selection_quiet  # type: ignore[assignment]
    agent.mcp = FakeMCP()  # type: ignore[assignment]
    await agent.handle_message("s2", "Дюна", "chat")
    still = await agent.store.get_task("stale-task")
    check("старая задача остаётся в работе после нового поиска",
          still is not None and still.state == "awaiting_choice",
          still.state if still else None)
    fresh_still = await agent.store.get_task("fresh-task")
    check("и свежая тоже",
          fresh_still is not None and fresh_still.state == "awaiting_choice",
          fresh_still.state if fresh_still else None)

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
        # Убираем только сообщения и задачи, созданные этим прогоном:
        # чужие переписки и завершённые задачи трогать нельзя.
        placeholders = ",".join("?" for _ in start_tasks or {""})
        conn.execute(
            "DELETE FROM messages WHERE id > ? AND task_id IS NOT NULL "
            f"AND task_id <> '' AND task_id NOT IN ({placeholders})",
            (start_msg, *tuple(start_tasks or {""})),
        )
        conn.execute(
            "DELETE FROM tasks WHERE id NOT IN ("
            + placeholders
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
    await test_nothing_new_inside_task()
    await test_locked_tasks_start_new()
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
