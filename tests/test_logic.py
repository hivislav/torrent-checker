#!/usr/bin/env python3
"""Тесты правил: разбор названий, фильтрация и «что считать новой серией».

Запуск:  .venv/bin/python tests/test_logic.py
Сеть не нужна — работают чистые функции, поэтому тест быстрый и стабильный.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.matching import (  # noqa: E402
    collect_options,
    episodes_by_season,
    filter_candidates,
    find_new,
    is_pack,
    rank,
)
from core.parsing import parse_torrent  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def make(tid: str, title: str, seeds: int = 10, uploaded: str = "15.10.2026 в 14:42") -> dict:
    return {
        "id": tid,
        "title": title,
        "size": "6.47 ГБ",
        "seeds": seeds,
        "peers": 3,
        "comments": 1,
        "uploadDate": uploaded,
        "uploader": "someone",
        "category": "46",
    }


def test_parsing() -> None:
    print("\n== разбор названий ==")
    torrent = parse_torrent(
        make("1", "Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / "
                  "ПМ (NewComers), СТ / WEB-DL (1080p)")
    )
    check("название", torrent.series_title == "Тед Лассо", torrent.series_title)
    check("качество 1080p", "1080p" in torrent.qualities, torrent.qualities)
    check("озвучка ПМ", any("ПМ" in v for v in torrent.voices), torrent.voices)
    check("сезон 4", torrent.seasons == [4], torrent.seasons)
    check("серии 1..10", torrent.episodes == [1, 10], torrent.episodes)
    check("это пачка", is_pack(torrent))

    single = parse_torrent(make("2", "Король Талсы (4 сезон: 5 серия из 10) / Tulsa King / "
                                     "2026 / ПМ (HDrezka Studio) / HEVC"))
    check("одна серия — не пачка", not is_pack(single))
    check("серия 5", single.episodes == [5], single.episodes)

    movie = parse_torrent(make("3", "Дюна: Часть вторая / Dune: Part Two / 2024 / "
                                    "Дубляж, LostFilm / BDRip (1080p)"))
    check("фильм без сезонов", not movie.seasons and not movie.episodes)
    check("озвучка LostFilm", any("LostFilm" in v for v in movie.voices), movie.voices)
    check("ключ сериала по оригинальному названию",
          movie.series_key.startswith("duneparttwo"), movie.series_key)


def test_filtering() -> None:
    print("\n== фильтрация и варианты ==")
    pool = [
        parse_torrent(make("10", "Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / "
                                 "ПМ (NewComers) / WEB-DL (1080p)", seeds=50)),
        parse_torrent(make("11", "Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / "
                                 "ЛМ (LostFilm) / WEB-DL (1080p)", seeds=20)),
        parse_torrent(make("12", "Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / "
                                 "ДБ / 4K, HEVC", seeds=5)),
        parse_torrent(make("13", "Дюна / Dune / 2021 / Дубляж / BDRip", seeds=100)),
    ]
    found_1080 = filter_candidates(pool, {"title": "Тед Лассо", "quality": "1080p"})
    check("1080p отбирает два", {t.id for t in found_1080} == {"10", "11"},
          [t.id for t in found_1080])
    found_4k = filter_candidates(pool, {"title": "Тед Лассо", "quality": "4к"})
    check("4к находит 4K", [t.id for t in found_4k] == ["12"], [t.id for t in found_4k])
    found_lf = filter_candidates(pool, {"title": "Тед Лассо", "voice": "lostfilm"})
    check("lostfilm находит ЛМ (LostFilm)", [t.id for t in found_lf] == ["11"],
          [t.id for t in found_lf])
    title_only = filter_candidates(pool, {"title": "Тед Лассо"})
    check("фильтр по названию", len(title_only) == 3, len(title_only))
    options = collect_options(title_only)
    check("варианты качества — разрешения", options["qualities"] == ["2160p", "1080p"],
          options["qualities"])
    ranked = rank(found_1080, {"title": "Тед Лассо", "quality": "1080p"})
    check("ранжирование по сидам", ranked[0].id == "10", ranked[0].id)

    # Сериал vs фильм: у сериала в названии есть сезон/серия.
    series_only = filter_candidates(pool, {"title": "", "type": "series"})
    check("type=series оставляет сериалы", {t.id for t in series_only} == {"10", "11", "12"},
          [t.id for t in series_only])
    movie_only = filter_candidates(pool, {"title": "", "type": "movie"})
    check("type=movie оставляет фильмы", [t.id for t in movie_only] == ["13"],
          [t.id for t in movie_only])


def test_new_episodes() -> None:
    print("\n== что считать новой серией ==")
    baseline_pack = parse_torrent(
        make("20", "Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 / ПМ / WEB-DL (1080p)")
    )
    multiseason_pack = parse_torrent(
        make("21", "Тед Лассо (1-4 сезоны: 1-44 серии из 44) / Ted Lasso / 2020-2026 / ДБ / WEB-DLRip")
    )
    ep11 = parse_torrent(
        make("22", "Тед Лассо (4 сезон: 11 серия из 12) / Ted Lasso / 2026 / ПМ / WEB-DL (1080p)",
             uploaded="20.10.2026 в 10:00")
    )
    ep09 = parse_torrent(
        make("23", "Тед Лассо (4 сезон: 9 серия из 10) / Ted Lasso / 2026 / ПМ / WEB-DL (1080p)")
    )

    season_map = episodes_by_season([baseline_pack, multiseason_pack])
    check("многосезонная пачка не задаёт нумерацию сезона", season_map == {4: 10}, season_map)

    known = {"20", "21"}
    fresh = find_new(
        [ep11, ep09], {"title": "Тед Лассо"}, known_ids=known,
        season_episodes=season_map, max_uploaded_ts=0,
    )
    check("11-я серия новая", [t.id for t in fresh] == ["22"], [t.id for t in fresh])

    # Ту же раздачу трекер обновляет новыми сериями: id тот же, серия новая.
    fresh_known = find_new(
        [ep11], {"title": "Тед Лассо"}, known_ids={"20", "21", "22"},
        season_episodes=season_map, max_uploaded_ts=0,
    )
    check("известная раздача с новой серией — новая", [t.id for t in fresh_known] == ["22"], fresh_known)

    # А если эта серия уже скачана — второй раз не предлагаем.
    fresh_done = find_new(
        [ep11], {"title": "Тед Лассо"}, known_ids={"20", "21", "22"},
        season_episodes={4: 11}, max_uploaded_ts=0,
    )
    check("уже скачанную серию не предлагаем", fresh_done == [], fresh_done)

    movie_old = parse_torrent(make("30", "Дюна / Dune / 2021 / Дубляж / BDRip",
                                   uploaded="10.09.2026 в 12:00"))
    movie_new = parse_torrent(make("31", "Дюна / Dune: Part Two / 2024 / Дубляж / BDRip",
                                   uploaded="16.10.2026 в 12:00"))
    fresh_movies = find_new(
        [movie_old, movie_new], {"title": "Дюна"}, known_ids=set(),
        season_episodes={}, max_uploaded_ts=movie_old.uploaded_ts or 0,
    )
    check("новый фильм по дате", [t.id for t in fresh_movies] == ["31"],
          [t.id for t in fresh_movies])

    ep_new_in_known_season = parse_torrent(
        make("24", "Тед Лассо (4 сезон: 1-12 серии из 12) / Ted Lasso / 2026 / ПМ / WEB-DL (1080p)",
             uploaded="21.10.2026 в 10:00")
    )
    fresh_pack = find_new(
        [ep_new_in_known_season], {"title": "Тед Лассо"}, known_ids={"20"},
        season_episodes=season_map, max_uploaded_ts=0,
    )
    check("обновлённая пачка с новыми сериями — новая",
          [t.id for t in fresh_pack] == ["24"], [t.id for t in fresh_pack])


def main() -> int:
    test_parsing()
    test_filtering()
    test_new_episodes()
    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: все проверки правил прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
