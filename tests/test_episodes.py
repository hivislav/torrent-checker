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


def main() -> int:
    test_detect()
    test_selection()
    test_plan()
    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: выбор серии и карточка подтверждения работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
