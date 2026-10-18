"""Серии внутри раздачи: что именно качать из пачки файлов.

Сезон на трекере часто лежит одной раздачей из 10–20 файлов. Если пользователю
нужна седьмая серия, качать весь сезон бессмысленно: Transmission умеет выбирать
файлы раздачи, надо лишь понять, какой индекс соответствует нужной серии.

Здесь только разбор имён файлов — без сети и без Transmission:

    Ted.Lasso.S04E07.1080p.WEB-DL.mkv       → сезон 4, серия 7
    4х07, 4x07                               → сезон 4, серия 7
    Серия 7 / серия 07 / эпизод 7            → серия 7
    Выпуск 12                                → серия 12
    S04.E07, s04 e07, S04_EP07               → сезон 4, серия 7
    Ted Lasso - 07 - название.mkv            → серия 7 (только номер в имени)

Функции возвращают индексы файлов — те же номера, которые понимает
`Client.change_torrent(files_wanted=...)`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Видео выбираем в первую очередь: без него «скачать серию» не имеет смысла.
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg")
# Субтитры и звуковые дорожки тянем вместе с видео того же эпизода.
SIDE_EXT = (".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".mka", ".ac3", ".dts", ".aac")
# Служебный мусор, который не нужен при выборе серии.
JUNK_HINTS = ("sample", "трейлер", "trailer", "nfo", ".txt", ".url", ".jpg", ".png", ".exe")

_SEASON_EPISODE_PATTERNS = (
    re.compile(r"(?i)\bs(\d{1,2})[\s._-]*e(?:p)?[\s._-]*(\d{1,3})\b"),
    re.compile(r"(?i)(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)"),
    re.compile(r"(?i)сезон[\s._-]*(\d{1,2}).{0,12}?серия[\s._-]*(\d{1,3})"),
    # «4 сезон 7 серия» — сезон стоит ПЕРЕД словом «сезон»
    re.compile(r"(?i)(\d{1,2})\s*[-_]?\s*сезон\S*.{0,20}?(\d{1,3})\s*[-_]?\s*(?:серия|эпизод|выпуск)"),
)

_EPISODE_ONLY_PATTERNS = (
    re.compile(r"(?i)\b(?:серия|эпизод|выпуск)[\s._-]*(\d{1,3})\b"),
    re.compile(r"(?i)(?:^|[\s._-])e(?:p)?[\s._-]*(\d{1,3})(?:[\s._-]|$)"),
    re.compile(r"(?i)(?:^|[\s._-])(\d{1,3})[\s._-]+(?:из|of)[\s._-]+\d{1,3}\b"),
    # «Название - 07 - что-то»: отдельно стоящий номер между разделителями.
    re.compile(r"(?:^|[\s._-])(\d{1,2})[\s._-]{1,3}(?:[^\d]|$)"),
)


@dataclass
class FileEntry:
    index: int
    name: str
    size: int
    season: int | None = None
    episode: int | None = None

    @property
    def is_video(self) -> bool:
        return self.name.lower().endswith(VIDEO_EXT)

    @property
    def is_side(self) -> bool:
        return self.name.lower().endswith(SIDE_EXT)

    @property
    def is_junk(self) -> bool:
        low = self.name.lower()
        return any(hint in low for hint in JUNK_HINTS) or self.name.startswith("__MACOSX")

    @property
    def label(self) -> str:
        return self.name.rsplit("/", 1)[-1]


# Числа, которые встречаются в именах релизов, но серией не являются:
# разрешение, кодек, год, «часть», аудио-каналы.
_NOT_EPISODE = {
    264, 265, 480, 576, 720, 1080, 1440, 2160, 4320,
    5, 8, 10, 12, 24, 25, 30, 50, 60,
    1, 2, 3, 4, 6, 7, 9, 11, 13, 14,  # «Часть 2», «CD1» — слишком неоднозначно
}


def detect_episode(name: str) -> tuple[int | None, int | None]:
    """(сезон, серия) из имени файла. Что не нашлось — None."""
    for pattern in _SEASON_EPISODE_PATTERNS:
        match = pattern.search(name)
        if match:
            return int(match.group(1)), int(match.group(2))
    for pattern in _EPISODE_ONLY_PATTERNS[:-1]:
        match = pattern.search(name)
        if match:
            return None, int(match.group(1))
    # Последний, самый осторожный вариант: одиночный номер между разделителями.
    match = _EPISODE_ONLY_PATTERNS[-1].search(name)
    if match:
        number = int(match.group(1))
        if number not in _NOT_EPISODE and 1 <= number <= 99:
            return None, number
    return None, None


def parse_files(files: Iterable[dict]) -> list[FileEntry]:
    """Превращает файлы раздачи (из .torrent) в записи с сезонами и сериями."""
    entries: list[FileEntry] = []
    for item in files:
        name = str(item.get("name") or "")
        season, episode = detect_episode(name)
        entries.append(
            FileEntry(
                index=int(item.get("index") or 0),
                name=name,
                size=int(item.get("size") or 0),
                season=season,
                episode=episode,
            )
        )
    return entries


def episode_groups(entries: Iterable[FileEntry]) -> list[dict]:
    """Список серий в раздаче: номер, сезон, какие файлы и сколько это весит."""
    groups: dict[tuple[int | None, int | None], dict] = {}
    for entry in entries:
        if entry.episode is None or entry.is_junk or not (entry.is_video or entry.is_side):
            continue
        key = (entry.season, entry.episode)
        group = groups.setdefault(
            key,
            {
                "season": entry.season,
                "episode": entry.episode,
                "indexes": [],
                "size": 0,
                "names": [],
            },
        )
        group["indexes"].append(entry.index)
        group["size"] += entry.size
        group["names"].append(entry.label)

    result = list(groups.values())
    result.sort(key=lambda item: (item["season"] or 0, item["episode"] or 0))
    return result


def is_episode_pack(entries: Iterable[FileEntry]) -> bool:
    """Раздача содержит несколько серий? (тогда есть смысл спрашивать какую)"""
    groups = episode_groups(entries)
    return len({(group["season"], group["episode"]) for group in groups}) > 1


def pick_episode(
    entries: list[FileEntry],
    *,
    season: int | None = None,
    episode: int | None = None,
) -> dict | None:
    """Файлы одной серии: видео + субтитры/дорожки с тем же номером."""
    if episode is None:
        return None
    candidates = [
        entry
        for entry in entries
        if entry.episode == episode
        and (season is None or entry.season in (None, season))
        and not entry.is_junk
    ]
    if not candidates:
        return None
    videos = [entry for entry in candidates if entry.is_video]
    chosen = videos or candidates
    # Добираем сопутствующие файлы: субтитры и дорожки того же эпизода.
    side = [entry for entry in candidates if entry not in chosen and entry.is_side]
    all_files = sorted(chosen + side, key=lambda entry: entry.index)
    return {
        "season": season if season is not None else (chosen[0].season if chosen else None),
        "episode": episode,
        "indexes": [entry.index for entry in all_files],
        "size": sum(entry.size for entry in all_files),
        "names": [entry.label for entry in all_files],
    }


def pick_new_episodes(
    entries: list[FileEntry], *, after_episode: int, season: int | None = None
) -> dict | None:
    """Серии пачки, которые новее уже скачанной (для автоскачивания)."""
    groups = episode_groups(entries)
    fresh = [
        group
        for group in groups
        if (group["episode"] or 0) > after_episode
        and (season is None or group["season"] in (None, season))
    ]
    if not fresh:
        return None
    indexes = sorted({index for group in fresh for index in group["indexes"]})
    return {
        "season": season,
        "episode": max(int(group["episode"] or 0) for group in fresh),
        "episodes": [group["episode"] for group in fresh],
        "indexes": indexes,
        "size": sum(group["size"] for group in fresh),
        "names": [name for group in fresh for name in group["names"]],
    }


def video_indexes(entries: Iterable[FileEntry]) -> list[int]:
    """Все видеофайлы (когда качаем раздачу целиком, но без мусора)."""
    return [entry.index for entry in entries if entry.is_video and not entry.is_junk]
