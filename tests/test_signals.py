#!/usr/bin/env python3
"""Проверки того, что видно о раздаче и как в неё попасть.

Запуск:  .venv/bin/python tests/test_signals.py

Что проверяется:
  1. в карточке подтверждения есть сиды, пиры, вес, возраст, автор и
     комментарии — по ним решают, брать раздачу или нет;
  2. мёртвая раздача (0 сидов) предупреждает о себе до скачивания;
  3. у кнопок качества и озвучки видно, что даёт каждый вариант;
  4. кандидат умеет «Подробнее» (жанр, режиссёр, описание) и ссылку на
     страницу раздачи — это отдельные кнопки, в чат они не пишут;
  5. ссылка `details.php?id=…` заводит задачу по раздаче, минуя поиск;
  6. у задач видна последняя проверка («12:40 — нового нет»);
  7. если по названию есть только экранки — так и говорится.
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
from core.models import Task, Torrent, Turn  # noqa: E402
from core.parsing import parse_torrent  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def make_agent() -> Agent:
    tmp = Path(tempfile.mkdtemp(prefix="signals-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3", torrents_dir=tmp / "t"
    )
    return Agent(settings)


def make_task(torrent: Torrent, *, params: dict | None = None) -> Task:
    task = Task(
        id="task-1",
        kind="oneshot",
        state="awaiting_confirm",
        session_id="s",
        user_query="Дюна",
        params=params or {"title": "Дюна"},
    )
    task.candidate = torrent.to_dict()
    task.options = {"pool": [torrent.to_dict()]}
    return task


MOVIE = Torrent(
    id="1234567",
    title="Дюна: Часть вторая / Dune: Part Two / 2024 / Дубляж, LostFilm / WEB-DL (1080p)",
    size="8.47 ГБ",
    seeds=12,
    peers=3,
    comments=42,
    upload_date="15.10.2026 в 14:42",
    uploader="NewComers",
    qualities=["1080p", "WEB-DL"],
    voices=["Дубляж", "LostFilm"],
    series_title="Дюна: Часть вторая",
    year="2024",
    series_key="duneparttwo",
    uploaded_ts=parse_torrent({
        "id": "1",
        "title": "Дюна / Dune / 2024 / ДБ / WEB-DL (1080p)",
        "uploadDate": "15.10.2026 в 14:42",
    }).uploaded_ts,
)

PLAN = {
    "kind": "single",
    "size": 8 << 30,
    "total_size": 8 << 30,
    "names": ["Dune.Part.Two.2024.1080p.mkv"],
}


def test_card() -> None:
    print("\n== карточка подтверждения: сигналы раздачи ==")
    agent = make_agent()
    card = agent._confirm_card_text(make_task(MOVIE), MOVIE, PLAN)  # noqa: SLF001
    print("  " + card.replace("\n", " | ")[:300])
    check("сиды в карточке", "Сиды: 12" in card, card[:200])
    check("пиры в карточке", "пиры: 3" in card, card[:200])
    check("комментарии в карточке", "обсуждали 42" in card, card[:200])
    check("вес на трекере", "8,47 ГБ" in card, card[:200])
    check("возраст раздачи", "залит" in card, card[:200])
    check("автор раздачи", "залил NewComers" in card, card[:200])
    check("мёртвой раздача не названа", "Сидов нет" not in card, card[:200])

    dead = dataclasses.replace(MOVIE, seeds=0, peers=0)
    dead_card = agent._confirm_card_text(make_task(dead), dead, PLAN)  # noqa: SLF001
    check("предупреждение про отсутствие сидов", "Сидов нет" in dead_card, dead_card[:200])
    check("совет взять другую", "другую раздачу" in dead_card, dead_card[:200])

    weak = dataclasses.replace(MOVIE, seeds=1)
    weak_card = agent._confirm_card_text(make_task(weak), weak, PLAN)  # noqa: SLF001
    check("мало сидов — тоже предупреждение", "Мало сидов" in weak_card, weak_card[:200])

    screener = dataclasses.replace(MOVIE, screener=True)
    screener_card = agent._confirm_card_text(make_task(screener), screener, PLAN)  # noqa: SLF001
    check("экранка подписана в карточке", "Экранка" in screener_card, screener_card[:200])
    agent.store.close()


def test_options() -> None:
    print("\n== сводка по вариантам качества и озвучки ==")
    agent = make_agent()
    found = [
        dataclasses.replace(MOVIE, id="1", seeds=12),
        dataclasses.replace(MOVIE, id="2", seeds=4, size="3.10 ГБ",
                            qualities=["720p"], uploader="другой"),
        dataclasses.replace(MOVIE, id="3", seeds=0, peers=0, qualities=["720p"],
                            size="1.20 ГБ"),
    ]
    lines = agent._option_summaries(found, "quality", ["1080p", "720p"], {})  # noqa: SLF001
    for line in lines:
        print("  " + line)
    check("две строки — по варианту на каждое качество", len(lines) == 2, lines)
    check("в строке качества есть вес", "8,47 ГБ" in lines[0], lines[0])
    check("в строке качества есть сиды", "12 сидов" in lines[0], lines[0])
    check("у 720p лучшая — с сидами", "4 сида" in lines[1], lines[1])
    check("мёртвый вариант не выбран лучшим", "без сидов" not in lines[1], lines[1])

    empty = agent._option_summaries([], "quality", ["2160p"], {})  # noqa: SLF001
    check("пустой вариант честно подписан", "вариантов нет" in empty[0], empty)

    task = make_task(MOVIE)
    turn = Turn()
    asyncio.run(
        agent._ask_dimension(task, turn, "quality", ["1080p", "720p"], len(found), found=found)  # noqa: SLF001
    )
    text = turn.messages[0].text
    print("  " + text.replace("\n", " | ")[:320])
    check("в вопросе о качестве видно, что даёт вариант", "лучшая:" in text, text[:300])
    check("кнопки качества на месте",
          {a.payload.get("value") for a in turn.messages[0].actions} >= {"1080p", "720p"})
    agent.store.close()


def test_actions() -> None:
    print("\n== «Подробнее» и ссылка на трекер ==")
    agent = make_agent()
    task = make_task(MOVIE)
    actions = agent._torrent_actions(task)  # noqa: SLF001
    kinds = {action.kind for action in actions}
    check("есть обе кнопки", kinds == {"details", "link"}, kinds)
    link = next(action for action in actions if action.kind == "link")
    check("ссылка ведёт на страницу раздачи",
          link.url == "https://kinozal.me/details.php?id=1234567", link.url)
    details = next(action for action in actions if action.kind == "details")
    check("«Подробнее» знает номер раздачи",
          details.payload.get("torrent_id") == "1234567", details.payload)

    start = agent._start_actions(task, PLAN)  # noqa: SLF001
    labels = [action.label for action in start]
    check("на карточке подтверждения есть «Подробнее»", "Подробнее" in labels, labels)
    check("на карточке подтверждения есть ссылка", "Открыть на трекере" in labels, labels)
    check("подтверждение осталось главным",
          start[0].kind == "start_download" and start[0].style == "primary", start[0])
    agent.store.close()


def test_link_flow() -> None:
    print("\n== задача по ссылке ==")
    agent = make_agent()

    details = {
        "id": "1234567",
        "title": "Дюна: Часть вторая / Dune: Part Two / 2024 / Дубляж / WEB-DL (1080p)",
        "originalTitle": "Dune: Part Two",
        "year": "2024",
        "genre": "фантастика, боевик",
        "director": "Дени Вильнёв",
        "cast": "Тимоти Шаламе, Зендея",
        "description": "Пол Атрейдес мстит заговорщикам.",
        "quality": "WEB-DL (1080p)",
        "video": "1920x1080, x264",
        "audio": "Русский: AC3, 5.1",
        "size": "8.47 ГБ",
        "seeds": 12,
        "peers": 3,
        "uploadDate": "15.10.2026 в 14:42",
        "updateDate": "",
    }
    files = {"files": [{"name": "Dune.Part.Two.2024.1080p.mkv", "size": 8 << 30}]}

    async def fake_details(torrent_id: str) -> dict:
        assert torrent_id == "1234567", torrent_id
        return details

    async def fake_files(torrent_id: str) -> dict:
        return files

    agent.mcp.details = fake_details  # type: ignore[assignment]
    agent.mcp.file_list = fake_files  # type: ignore[assignment]

    turn = asyncio.run(
        agent.handle_message("s", "держи https://kinozal.me/details.php?id=1234567")
    )
    text = "\n".join(message.text for message in turn.messages)
    print("  " + text.replace("\n", " | ")[:320])
    check("задача создана по ссылке", turn.task is not None and bool(turn.task.id))
    check("название взято со страницы", "Дюна" in (turn.task.params.get("title") or ""),
          turn.task.params)
    check("карточка показана сразу", "Ставлю в Transmission?" in text, text[-200:])
    check("в карточке есть сиды", "Сиды: 12" in text, text[-300:])
    check("есть кнопка «Подробнее»",
          any(action.kind == "details" for message in turn.messages for action in message.actions))

    # Подробности для кнопки: жанр, режиссёр, описание и ссылка.
    payload = asyncio.run(agent.torrent_details("1234567"))
    check("жанр в подробностях", payload.get("genre") == "фантастика, боевик", payload)
    check("режиссёр в подробностях", payload.get("director") == "Дени Вильнёв", payload)
    check("ссылка в подробностях",
          payload.get("url") == "https://kinozal.me/details.php?id=1234567", payload.get("url"))
    agent.store.close()


def test_checks_journal() -> None:
    print("\n== последняя проверка в карточке задачи ==")
    agent = make_agent()

    async def run() -> list[dict]:
        task = await agent._task_from_params(  # noqa: SLF001
            "s", "Дюна", {"title": "Дюна"}, kind="periodic", interval=30
        )
        task.state = "monitoring"
        task.checks_done = 2
        await agent.store.save_task(task)
        await agent.store.add_check(task.id, 5, 0, "")
        await agent.store.add_check(task.id, 6, 2, "Дюна: Часть вторая")
        return await agent.list_tasks()

    tasks = asyncio.run(run())
    data = next(item for item in tasks if item["id"])
    last = data.get("last_check") or {}
    print(f"  последняя проверка: {last}")
    check("последняя проверка попала в список задач", bool(last), data.keys())
    check("видно, сколько нашли", last.get("found") == 6, last)
    check("видно, сколько новых", last.get("new") == 2, last)
    check("видно название новинки", "Дюна" in (last.get("message") or ""), last)
    agent.store.close()


def test_screeners_message() -> None:
    print("\n== если есть только экранки ==")
    agent = make_agent()
    screener = dataclasses.replace(MOVIE, id="7", screener=True, seeds=50)
    task = make_task(screener)
    turn = Turn()
    asyncio.run(
        agent._offer_screeners(task, [screener], turn)  # noqa: SLF001
    )
    message = turn.messages[0]
    print("  " + message.text.replace("\n", " | ")[:220])
    check("сказано, что нашлись только экранки", "только экранки" in message.text, message.text)
    check("есть кнопка «Показать экранку»",
          any(action.label == "Показать экранку" for action in message.actions))
    allow = next(action for action in message.actions if action.label == "Показать экранку")
    check("кнопка включает фильтр экранок",
          allow.payload.get("dimension") == "screener", allow.payload)
    check("состояние — ждём выбора", task.state == "awaiting_choice", task.state)
    agent.store.close()


def test_episode_picker_signals() -> None:
    print("\n== выбор серий: сигналы раздачи ==")
    agent = make_agent()
    pack = parse_torrent(
        {
            "id": "555",
            "title": "Тед Лассо (4 сезон: 1-3 серии из 10) / Ted Lasso / 2026 / "
                     "ПМ (NewComers) / WEB-DL (1080p)",
            "size": "33.2 ГБ",
            "seeds": 18,
            "peers": 4,
            "uploadDate": "15.10.2026 в 14:42",
        }
    )
    task = make_task(pack, params={"title": "Тед Лассо"})
    groups = [
        {"season": 4, "episode": number, "indexes": [index], "size": 2 << 30,
         "names": [f"Ted.Lasso.S04E0{number}.mkv"]}
        for index, number in enumerate((1, 2, 3))
    ]
    turn = Turn()
    asyncio.run(
        agent._show_episode_picker(task, pack, groups, None, turn)  # noqa: SLF001
    )
    message = turn.messages[0]
    print("  " + message.text.replace("\n", " | ")[:260])
    check("в списке серий виден вес и сиды", "33,2 ГБ" in message.text and "18 сидов" in message.text,
          message.text[:200])
    check("кнопка «Подробнее» и в списке серий",
          any(action.kind == "details" for action in message.actions))
    agent.store.close()


def test_details_everywhere() -> None:
    print("\n== «Подробнее» на каждом шаге выбора, а не только в карточке ==")
    agent = make_agent()
    pool = [
        dataclasses.replace(MOVIE, id="11", seeds=3, qualities=["720p"], size="3.10 ГБ"),
        dataclasses.replace(MOVIE, id="12", seeds=40, qualities=["1080p"], size="8.47 ГБ"),
    ]

    def details_actions(message) -> list:
        return [action for action in message.actions if action.kind == "details"]

    # Шаг «какое качество»: подробности о лучшей раздаче под текущие условия.
    task = make_task(pool[1], params={"title": "Дюна"})
    turn = Turn()
    asyncio.run(
        agent._ask_dimension(task, turn, "quality", ["1080p", "720p"], len(pool), found=pool)  # noqa: SLF001
    )
    found_actions = details_actions(turn.messages[0])
    check("на шаге выбора качества есть «Подробнее»", len(found_actions) == 1, turn.messages[0].actions)
    check("подробности открывают лучшую раздачу",
          found_actions and found_actions[0].payload.get("torrent_id") == "12",
          found_actions[0].payload if found_actions else None)
    check("рядом есть ссылка на трекер",
          any(action.kind == "link" for action in turn.messages[0].actions))

    # Шаг «под эти условия ничего не подошло» — тоже.
    relaxed = make_task(pool[1], params={"title": "Дюна", "quality": "2160p"})
    turn = Turn()
    asyncio.run(agent._offer_relaxed(relaxed, pool, turn))  # noqa: SLF001
    check("в «ничего не подошло» есть «Подробнее»", len(details_actions(turn.messages[0])) == 1,
          turn.messages[0].actions)

    # Шаг «какой именно фильм» — подробности о самом вероятном варианте.
    title_task = make_task(pool[1], params={"title": "Дюна"})
    turn = Turn()
    suggestions = [
        {"title": "Дюна", "torrent_id": "12", "count": 4, "years": "2021"},
        {"title": "Планета Дюна", "torrent_id": "13", "count": 2, "years": "2021"},
    ]
    asyncio.run(agent._ask_title(title_task, suggestions, 6, turn, weak=False))  # noqa: SLF001
    title_actions = details_actions(turn.messages[0])
    check("на шаге выбора названия есть «Подробнее»", len(title_actions) == 1,
          turn.messages[0].actions)
    check("подробности открывают первый вариант",
          title_actions and title_actions[0].payload.get("torrent_id") == "12",
          title_actions[0].payload if title_actions else None)

    # Экранки: подробности о найденной экранке.
    screener = dataclasses.replace(MOVIE, id="99", screener=True, seeds=30)
    turn = Turn()
    asyncio.run(agent._offer_screeners(make_task(screener), [screener], turn))  # noqa: SLF001
    check("у экранки тоже есть «Подробнее»", len(details_actions(turn.messages[0])) == 1,
          turn.messages[0].actions)

    # Без выбранной раздачи кнопки не появляются (нечего открывать).
    empty_task = Task(id="t", kind="oneshot", state="searching", session_id="s", user_query="")
    empty_task.candidate = {}
    check("без раздачи кнопок нет", agent._torrent_actions(empty_task) == [])  # noqa: SLF001
    agent.store.close()


def main() -> int:
    test_card()
    test_options()
    test_actions()
    test_details_everywhere()
    test_link_flow()
    test_checks_journal()
    test_screeners_message()
    test_episode_picker_signals()
    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: раздача показывает свои сигналы, ссылки и подробности работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
