#!/usr/bin/env python3
"""Проверки выбора серии и карточки подтверждения.

Запуск:  .venv/bin/python tests/test_episodes.py

Что проверяется:
  1. номера серий и сезонов разбираются из реальных имён файлов, а «1080p»,
     «Part 2» и прочие числа сериями не считаются;
  2. агент по раздаче-пачке предлагает выбор серии, по названной серии сразу
     берёт её одну, а фильм качает целиком;
  3. в карточке подтверждения есть вес выбранного и свободное место на диске.
"""
from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import Agent  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.episodes import (  # noqa: E402
    detect_episode,
    episode_groups,
    is_episode_pack,
    parse_files,
    pick_episode,
    pick_new_episodes,
)
from core.models import Task, Torrent  # noqa: E402
from core.torrent_meta import human_bytes  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def test_detect() -> None:
    print("\n== номера серий в именах файлов ==")
    cases = {
        "Ted.Lasso.S04E07.1080p.WEB-DL.mkv": (4, 7),
        "ted.lasso.s04.e07.rus.lostfilm.mkv": (4, 7),
        "Tulsa.King.4x07.HDTV.mkv": (4, 7),
        "Тед Лассо - Серия 7 из 10.mkv": (None, 7),
        "Тед Лассо 4 сезон 7 серия.mkv": (4, 7),
        "Сезон 4 Серия 7.mkv": (4, 7),
        "Король Талсы.Выпуск 7.avi": (None, 7),
        "Ted.Lasso.S04E07.1080p.WEB-DL.srt": (4, 7),
        # не серии:
        "Ted.Lasso.2024.1080p.WEB-DL.mkv": (None, None),
        "Film.Part.2.2160p.mkv": (None, None),
        "sample.mkv": (None, None),
    }
    for name, expected in cases.items():
        got = detect_episode(name)
        check(f"{name}", got == expected, f"получили {got}, ждали {expected}")


def make_entries(count: int = 10, size: int = 3_300_000_000) -> list[dict]:
    files = [
        {
            "index": index,
            "name": f"Ted.Lasso.S04E{index:02d}.1080p.WEB-DL.mkv",
            "size": size,
        }
        for index in range(1, count + 1)
    ]
    files.append({"index": count, "name": "Ted.Lasso.S04E07.1080p.WEB-DL.srt", "size": 40_000})
    return files


def test_selection() -> None:
    print("\n== выбор серии из пачки ==")
    entries = parse_files(make_entries())
    check("пачка распознана", is_episode_pack(entries))
    groups = episode_groups(entries)
    check("серий найдено: 10", len(groups) == 10, len(groups))

    picked = pick_episode(entries, season=4, episode=7)
    check("серия 7 найдена", picked is not None)
    check("взято видео + субтитры", len(picked["indexes"]) == 2, picked["indexes"])
    check("вес — только этой серии", picked["size"] == 3_300_040_000, picked["size"])
    check(
        "вес меньше всей пачки",
        picked["size"] < sum(item["size"] for item in make_entries()),
        picked["size"],
    )

    fresh = pick_new_episodes(entries, after_episode=8, season=4)
    check("новые серии после 8-й", fresh["episodes"] == [9, 10], fresh["episodes"])

    nothing = pick_new_episodes(entries, after_episode=10, season=4)
    check("новых нет, если уже 10-я", nothing is None)


def test_plan() -> None:
    print("\n== решение агента: что качать ==")
    tmp = Path(tempfile.mkdtemp(prefix="episodes-test-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "episodes.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)
    entries = parse_files(make_entries())
    movie = parse_files([{"index": 0, "name": "Дюна.2024.BDRip.1080p.mkv", "size": 8 << 30}])

    def task(**params) -> Task:
        return Task(
            id="t",
            kind="oneshot",
            state="awaiting_confirm",
            session_id="s",
            user_query="тест",
            params=params,
        )

    pack_plan = agent._plan_files(task(title="Тед Лассо"), entries)  # noqa: SLF001
    check("без серии — предлагает выбор", pack_plan["kind"] == "choose", pack_plan["kind"])
    check("в выборе 10 серий", len(pack_plan["episodes"]) == 10)

    episode_plan = agent._plan_files(task(title="Тед Лассо", season=4, episode_from=7), entries)  # noqa: SLF001
    check("названа серия — берём её", episode_plan["kind"] == "episode", episode_plan["kind"])
    check("индексы только этой серии", episode_plan["wanted"] == [7, 10], episode_plan["wanted"])
    check("вес выбранного меньше общего",
          episode_plan["size"] < episode_plan["total_size"],
          f"{episode_plan['size']} из {episode_plan['total_size']}")

    missing = agent._plan_files(task(title="Тед Лассо", season=4, episode_from=99), entries)  # noqa: SLF001
    check("нет такой серии — предлагает выбрать", missing["kind"] == "choose", missing["kind"])
    check("причина — отсутствие серии", missing.get("reason") == "missing_episode", missing.get("reason"))

    # Коллекция фильмов: файлы пронумерованы («01.Пила…», «02.Пила…»), но это
    # не серии — раздача качается целиком.
    collection_entries = parse_files(
        [
            {"index": index, "name": name, "size": 9 << 30}
            for index, name in enumerate(
                [
                    "01.Пила.Игра.на.выживание.Saw.2004.DC.BDRip-HEVC.1080p.mkv",
                    "02.Пила.2.Saw.II.2005.DC.BDRip-HEVC.1080p.mkv",
                    "03.Пила.3.Saw.III.2006.DC.BDRip-HEVC.1080p.mkv",
                ]
            )
        ]
    )
    collection = Torrent(
        id="9",
        title="Пила (Коллекция 10 фильмов) / Saw (Collection) / 2004-2023 / ДБ / BDRip (1080p)",
        year="2004-2023",
    )
    collection_plan = agent._plan_files(task(title="Пила", type="movie"), collection_entries, collection)  # noqa: SLF001
    check("коллекция фильмов — не выбор серий",
          collection_plan["kind"] == "single", collection_plan["kind"])
    check("в плане все файлы коллекции",
          len(collection_plan["names"]) == 3, collection_plan["names"])

    real_pack = Torrent(
        id="10",
        title="Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / WEB-DL (1080p)",
        seasons=[4],
        episodes=[1, 10],
    )
    series_plan = agent._plan_files(task(title="Тед Лассо"), entries, real_pack)  # noqa: SLF001
    check("настоящий сериал по-прежнему предлагает серии",
          series_plan["kind"] == "choose", series_plan["kind"])

    movie_plan = agent._plan_files(task(title="Дюна"), movie)  # noqa: SLF001
    check("фильм качаем целиком", movie_plan["kind"] == "single", movie_plan["kind"])
    check("вес фильма посчитан", movie_plan["size"] == 8 << 30, movie_plan["size"])

    print("\n== карточка подтверждения ==")
    torrent = Torrent(
        id="1",
        title="Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / ПМ (NewComers), СТ / WEB-DL (1080p)",
        qualities=["1080p", "WEB-DL"],
        voices=["ПМ (NewComers)", "СТ"],
        seasons=[4],
        episodes=[1, 10],
    )
    card = agent._confirm_card_text(task(title="Тед Лассо"), torrent, episode_plan)  # noqa: SLF001
    print("  " + card.replace("\n", " | ")[:260])
    check("в карточке есть сезон и серия", "Сезон 4, серия 7" in card, card[:200])
    check("в карточке есть вес выбранного", "Вес выбранного" in card)
    check("вес указан в ГБ", "ГБ" in card)
    check(
        "в карточке есть свободное место",
        "свободно" in card or "недоступен" in card,
        card[-200:],
    )
    check("в карточке есть путь загрузки", str(settings.transmission_download_dir) in card)
    check("в карточке видно имя файла серии", "S04E07" in card or "файл" in card)
    agent.store.close()


def test_selection_options() -> None:
    print("\n== варианты выбора не врут ==")
    import dataclasses
    import tempfile

    from core.agent import Agent
    from core.config import get_settings
    from core.models import Task, Torrent

    tmp = Path(tempfile.mkdtemp(prefix="options-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "o.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)

    def tor(tid: str, title: str, quality: str, voice: str, season: int) -> Torrent:
        return Torrent(
            id=tid,
            title=title,
            qualities=[quality],
            voices=[voice],
            seasons=[season],
            episodes=[1, 8],
            series_key="tedlasso",
        )

    pool = [
        tor("1", "Тед Лассо 4 сезон 1080p ДБ", "1080p", "ДБ", 4),
        tor("2", "Тед Лассо 4 сезон 2160p ДБ", "2160p", "ДБ", 4),
        tor("3", "Тед Лассо 2 сезон 720p ПМ", "720p", "ПМ", 2),
    ]
    options, pool_used = agent._options_under(pool, {"season": 4}, "quality")  # noqa: SLF001
    print("  качество для 4 сезона:", options["qualities"])
    check("720p не предлагается, если его нет в сезоне", "720p" not in options["qualities"],
          options["qualities"])
    check("предлагается только доступное", set(options["qualities"]) == {"2160p", "1080p"},
          options["qualities"])

    v_options, _ = agent._options_under(pool, {"season": 4, "quality": "1080p"}, "voice")  # noqa: SLF001
    check("озвучка считается с учётом качества", v_options["voices"] == ["ДБ"], v_options["voices"])

    # «без учёта качества» расширяет выбор
    v_any, _ = agent._options_under(pool, {"season": 4}, "voice")  # noqa: SLF001
    check("без качества озвучек больше", len(v_any["voices"]) >= len(v_options["voices"]),
          v_any["voices"])
    agent.store.close()


def test_multi_episode_plan() -> None:
    print("\n== несколько серий одним планом ==")
    import dataclasses
    import tempfile

    from core.agent import Agent
    from core.config import get_settings
    from core.models import Task, Torrent, Turn

    tmp = Path(tempfile.mkdtemp(prefix="multi-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "m.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)
    task = Task(
        id="m",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s",
        user_query="Тед Лассо",
        params={"title": "Тед Лассо", "season": 4, "quality": "1080p"},
        candidate=Torrent(id="9", title="Тед Лассо 4 сезон").to_dict(),
        options={
            "episode_groups": [
                {"season": 4, "episode": 1, "indexes": [0], "size": 100, "names": ["e1.mkv"]},
                {"season": 4, "episode": 3, "indexes": [2], "size": 300, "names": ["e3.mkv"]},
                {"season": 4, "episode": 5, "indexes": [4], "size": 500, "names": ["e5.mkv"]},
            ]
        },
    )
    turn = Turn()
    import asyncio

    asyncio.get_event_loop_policy()
    asyncio.run(agent._confirm_selected_episodes(task, [1, 5], turn))  # noqa: SLF001
    plan = (task.options or {}).get("plan") or {}
    print("  план:", {k: plan.get(k) for k in ("kind", "wanted", "size", "selected_episodes")})
    check("выбраны файлы 1 и 5 серии", plan.get("wanted") == [0, 4], plan.get("wanted"))
    check("вес — сумма выбранных", plan.get("size") == 600, plan.get("size"))
    check("вес всей раздачи рядом", plan.get("total_size") == 900, plan.get("total_size"))
    labels = [action.label for message in turn.messages for action in message.actions]
    check("есть кнопка постановки в очередь", "Поставить в Transmission" in labels, labels)
    agent.store.close()

def test_plan_rebuild() -> None:
    """План пересобирается по настоящему .torrent, когда файлов больше.

    Настоящий сбой: в списке со страницы было 10 серий, в .torrent оказалось
    11 файлов, агент шёл пересобирать план по сериям — и падал с
    `NameError: name 'self' is not defined`, то есть кнопка «Скачать»
    возвращала 500 вместо раздачи.
    """
    print("\n== пересборка плана по настоящему .torrent ==")
    from core.models import Task

    tmp = Path(tempfile.mkdtemp(prefix="rebuild-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "r.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)

    entries = parse_files(
        [
            {"index": index, "size": 1000, "name": name}
            for index, name in enumerate(
                [
                    "Ted.Lasso.S04E01.1080p.mkv",
                    "Ted.Lasso.S04E02.1080p.mkv",
                    "Ted.Lasso.S04E03.1080p.mkv",
                    "Ted.Lasso.S04E04.1080p.mkv",
                    "Ted.Lasso.S04E05.1080p.mkv",
                    "Ted.Lasso.S04E06.1080p.mkv",
                    "Ted.Lasso.S04E07.1080p.mkv",
                    "Ted.Lasso.S04E08.1080p.mkv",
                    "Ted.Lasso.S04E09.1080p.mkv",
                    "Ted.Lasso.S04E10.1080p.mkv",
                    "sample.mkv",
                ]
            )
        ]
    )
    plan = {
        "kind": "episode",
        "season": 4,
        "total_files": 10,
        "total_size": 10000,
        "selected_episodes": [
            {"season": 4, "episode": 1},
            {"season": 4, "episode": 5},
        ],
    }
    rebuilt = agent._plan_from_episodes(entries, plan)  # noqa: SLF001
    check("план пересобрался", isinstance(rebuilt, dict), rebuilt)
    if isinstance(rebuilt, dict):
        check("индексы настоящие", rebuilt.get("wanted") == [0, 4], rebuilt.get("wanted"))
        check("вес — только выбранные серии", rebuilt.get("size") == 2000, rebuilt.get("size"))
        check("серии сохранены",
              [item["episode"] for item in rebuilt.get("selected_episodes") or []] == [1, 5],
              rebuilt.get("selected_episodes"))

    empty = agent._plan_from_episodes(entries, {**plan, "selected_episodes": []})  # noqa: SLF001
    check("без выбранных серий пересобирать нечего", empty is None, empty)
    agent.store.close()


def test_pack_seasons_and_big_lists() -> None:
    """Многолетняя пачка: сначала сезон, потом серии; список не обрезается молча.

    Настоящий случай («Черепашки мутанты ниндзя», 193 серии в 10 сезонах):
    имена файлов вида «01..02. Название.mkv» разбирались как «серия 1, серия 1,
    …» — весь сезон схлопывался в одну серию, и вместо 193 серий показывалось 10
    кнопок, каждая из которых качала целый сезон.
    """
    print("\n== пачка на несколько сезонов: сначала сезон ==")
    import asyncio
    from core.models import Task, Torrent, Turn

    pairs = [detect_episode(name) for name in (
        "01..01. Первая.mkv",
        "01..02. Вторая.mkv",
        "02..01. Третья.mkv",
        "10..05. Последняя.mkv",
    )]
    print("  разбор NN..MM:", pairs)
    check("сезон и серия из «01..02» разбираются", pairs[0] == (1, 1) and pairs[1] == (1, 2), pairs)
    check("второй сезон не путается с первым", pairs[2] == (2, 1), pairs[2])
    check("десятый сезон разбирается", pairs[3] == (10, 5), pairs[3])
    check("«4х07» с кириллической х — тоже сезон и серия", detect_episode("4х07.mkv") == (4, 7))

    glued = parse_files(
        [
            {"index": 0, "size": 10, "name": "Сезон..01. A.mkv 01..02. B.mkv 01..03. C.mkv"},
            {"index": 1, "size": 10, "name": "01..01. A.mkv"},
            {"index": 2, "size": 10, "name": "02..01. B.mkv"},
        ]
    )
    groups = episode_groups(glued)
    print("  группы:", [f"{g['season']}×{g['episode']}" for g in groups])
    check("склеенная строка сезона не считается файлом", glued[0].is_junk, glued[0].name[:40])
    check("в группы попали только настоящие файлы", len(groups) == 2, len(groups))

    tmp = Path(tempfile.mkdtemp(prefix="seasons-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)
    entries = parse_files(
        [
            {"index": index, "size": 100, "name": name}
            for index, name in enumerate(
                ["01..01. A.mkv", "01..02. B.mkv", "02..01. C.mkv", "02..02. D.mkv", "02..03. E.mkv"]
            )
        ]
    )
    groups = episode_groups(entries)
    task = Task(
        id="pack",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s",
        user_query="Черепашки",
        params={"title": "Черепашки"},
        candidate=Torrent(id="777", title="Черепашки (1-2 сезоны: 1-5 серии из 5)").to_dict(),
    )

    async def scenario() -> None:
        await agent.store.save_task(task)
        turn = Turn()
        await agent._show_episode_picker(  # noqa: SLF001
            task, Torrent.from_dict(task.candidate), groups, None, turn
        )
        first = turn.messages[-1]
        labels = [action.label for action in first.actions]
        print("  шаг «сезон»:", labels)
        check("сначала спрашивается сезон",
              "Сезон 1" in " ".join(labels) and "Сезон 2" in " ".join(labels), labels)
        check("серии на этом шаге не предлагаются",
              not any(action.kind == "toggle_episode" for action in first.actions), labels)

        await agent._pick_season(task, {"season": 2}, turn)  # noqa: SLF001
        second = turn.messages[-1]
        labels = [action.label for action in second.actions]
        print("  шаг «серии сезона 2»:", labels[:6])
        episodes = [action for action in second.actions if action.kind == "toggle_episode"]
        check("показаны серии выбранного сезона", len(episodes) == 3, labels)
        check("у каждой кнопки сезон в payload",
              all(action.payload.get("season") == 2 for action in episodes),
              [action.payload.get("season") for action in episodes])
        check("есть кнопка «Другой сезон»", "Другой сезон" in labels, labels)

    asyncio.run(scenario())
    agent.store.close()


def test_whole_season_limits_files() -> None:
    """«Скачать весь сезон» ставит файлы этого сезона, а не всю раздачу.

    Настоящий случай: в пачке «1-10 сезоны» нажали «Весь сезон 1» — агент
    обещал сезон, а Transmission ставил все файлы раздачи (52 ГБ вместо 5).
    """
    print("\n== «весь сезон» — только файлы сезона ==")
    import asyncio

    from core.models import Task, Torrent

    tmp = Path(tempfile.mkdtemp(prefix="season-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)
    groups = [
        {"season": 1, "episode": 1, "indexes": [0], "size": 100, "names": ["s1e1.mkv"]},
        {"season": 1, "episode": 2, "indexes": [1], "size": 100, "names": ["s1e2.mkv"]},
        {"season": 1, "episode": 3, "indexes": [2], "size": 100, "names": ["s1e3.mkv"]},
        {"season": 2, "episode": 1, "indexes": [3], "size": 200, "names": ["s2e1.mkv"]},
    ]
    task = Task(
        id="season",
        kind="oneshot",
        state="awaiting_choice",
        session_id="s",
        user_query="Черепашки",
        params={"title": "Черепашки", "season": 1},
        candidate=Torrent(id="900", title="Черепашки (1-2 сезоны: 1-4 серии из 4)").to_dict(),
        options={"episode_groups": groups},
    )

    async def scenario() -> None:
        await agent.store.save_task(task)
        turn = await agent.handle_action(
            "s", "download_all", {"task_id": "season", "season": 1}
        )
        stored = await agent.store.get_task("season")
        plan = (stored.options or {}).get("plan") or {}
        print("  план:", {k: plan.get(k) for k in ("kind", "wanted", "size", "total_size")})
        check("в плане только файлы первого сезона", plan.get("wanted") == [0, 1, 2],
              plan.get("wanted"))
        check("вес — только сезон, а не вся пачка", plan.get("size") == 300, plan.get("size"))
        check("вес всей пачки всё равно виден", plan.get("total_size") == 500,
              plan.get("total_size"))
        labels = [action.label for action in (turn.messages[-1].actions if turn.messages else [])]
        check("на карточке есть постановка в очередь", "Поставить в Transmission" in labels, labels)

    asyncio.run(scenario())
    agent.store.close()


def test_multi_season_pack() -> None:
    """Пачка «1-4 сезоны: 1-40 серии» — серии различаются по сезону.

    Настоящий случай с трекера: в раздаче четыре сезона по 10 серий, задача
    про второй сезон. Раньше кнопки назывались просто «Серия 1…10» четыре
    раза подряд, а выбор одной серии ставил в очередь по файлу из КАЖДОГО
    сезона (четыре файла, 23 ГБ вместо 5).
    """
    print("\n== пачка на несколько сезонов ==")
    import asyncio
    import dataclasses
    import tempfile

    from core.agent import Agent
    from core.config import get_settings
    from core.models import Action, Task, Turn

    tmp = Path(tempfile.mkdtemp(prefix="seasons-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3", torrents_dir=tmp / "t"
    )
    agent = Agent(settings)
    groups = []
    index = 0
    for season in (1, 2, 3, 4):
        for episode in range(1, 11):
            groups.append(
                {
                    "season": season,
                    "episode": episode,
                    "indexes": [index],
                    "size": 5 << 30,
                    "names": [f"From.S0{season}E{episode:02d}.mkv"],
                }
            )
            index += 1
    torrent = Torrent(
        id="2146438",
        title="Извне (1-4 сезоны: 1-40 серии из 40) / From / 2022-2026 / ПМ / 4K / WEB-DL (2160p)",
        qualities=["2160p"],
        voices=["ПМ"],
        seasons=[1, 2, 3, 4],
        episodes=[1, 40],
        series_key="from",
        size="233.73 ГБ",
    )
    task = Task(
        id="seasons-task", kind="oneshot", state="awaiting_choice", session_id="s",
        user_query="Извне второй сезон", params={"title": "Извне", "season": 2},
    )
    task.candidate = torrent.to_dict()
    task.options = {"pool": [torrent.to_dict()]}
    turn = Turn()
    asyncio.run(agent._show_episode_picker(task, torrent, groups, None, turn))  # noqa: SLF001
    message = turn.messages[-1]
    labels = [action.label for action in message.actions if action.kind == "toggle_episode"]
    print("  заголовок:", message.text.splitlines()[0])
    print("  кнопки:", labels[:3], "… всего", len(labels))
    check("в заголовке сезон задачи", "сезон 2" in message.text.splitlines()[0],
          message.text.splitlines()[0])
    check("показаны только серии второго сезона", len(labels) == 10, len(labels))
    check("кнопки подписаны сезоном или без повторов",
          len({label for label in labels}) == len(labels), labels)
    check("id кнопок уникальны",
          len({action.id for action in message.actions}) == len(message.actions))

    # Отмечаем одну серию — план должен взять ровно один файл второго сезона.
    episode_action = next(
        action for action in message.actions
        if action.kind == "toggle_episode" and action.payload.get("episode") == 1
    )
    plan_turn = asyncio.run(
        agent.handle_action(
            "s", "toggle_episode",
            {"task_id": task.id, "season": episode_action.payload["season"],
             "episode": episode_action.payload["episode"]},
        )
    )
    stored = asyncio.run(agent.store.get_task(task.id))
    assert stored is not None
    print("  сохранённый выбор:", stored.options.get("selected_episodes"))
    check("выбор запомнен с сезоном",
          stored.options.get("selected_episodes") == [{"season": 2, "episode": 1}],
          stored.options.get("selected_episodes"))

    plan = agent._build_episodes_plan(stored, [{"season": 2, "episode": 1}])  # noqa: SLF001
    print("  план:", {"wanted": plan["wanted"], "size": plan["size"], "names": plan["names"]})
    check("в плане один файл", plan["wanted"] == [10], plan["wanted"])
    check("вес — одна серия, а не четыре", plan["size"] == 5 << 30, plan["size"])
    check("имя файла из второго сезона", plan["names"] == ["From.S02E01.mkv"], plan["names"])
    check("сезон в плане — 2", plan["season"] == 2, plan["season"])

    # Старый формат (просто число) тоже должен пониматься — но в сезоне задачи.
    legacy = agent._build_episodes_plan(stored, [1])  # noqa: SLF001
    check("старый выбор «серия 1» берёт сезон задачи",
          legacy["wanted"] == [10], legacy["wanted"])

    # «Весь сезон 2» — десять файлов второго сезона, а не вся пачка.
    whole_turn = asyncio.run(
        agent.handle_action("s", "download_all", {"task_id": task.id, "season": 2})
    )
    whole_plan = (asyncio.run(agent.store.get_task(task.id)).options or {}).get("plan") or {}
    print("  весь сезон 2:", {"файлов": len(whole_plan.get("selected_episodes") or [])})
    check("весь сезон — 10 серий", len(whole_plan.get("selected_episodes") or []) == 10,
          whole_plan.get("selected_episodes"))
    check("и это только второй сезон",
          {item.get("season") for item in whole_plan.get("selected_episodes")} == {2},
          whole_plan.get("selected_episodes"))
    agent.store.close()


def main() -> int:
    test_detect()
    test_selection()
    test_plan()
    test_selection_options()
    test_multi_episode_plan()
    test_plan_rebuild()
    test_pack_seasons_and_big_lists()
    test_whole_season_limits_files()
    test_multi_season_pack()
    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: выбор серии и карточка подтверждения работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
