"""Разбор названий раздач kinozal.me.

Название на трекере — это почти структурированная строка, из которой видно всё,
что нужно агенту:

    Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso / 2026 /
        ПМ (NewComers), СТ / WEB-DL (1080p)

То есть: русское название + (сезон/серии) / оригинальное название / год /
озвучки / качество. Здесь это превращается в поля `Torrent`, по которым потом
работает фильтрация (core/matching.py) — модель к этому делу не привлекается,
чтобы «4к» не превращалось в «4к-что-нибудь».
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Iterable

from core.models import Torrent

# ----------------------------------------------------------------------
# Качество
# ----------------------------------------------------------------------
# Разрешение — главный признак. Порядок важен: более высокое проверяем раньше.
RESOLUTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("2160p", re.compile(r"(?<!\d)(?:2160p?|4k|uhd|ultra\s*hd)(?!\d)", re.I)),
    ("1080p", re.compile(r"(?<!\d)(?:1080p?|full\s*hd|fhd)(?!\d)", re.I)),
    ("720p", re.compile(r"(?<!\d)720p?(?!\d)", re.I)),
    ("480p", re.compile(r"(?<!\d)480p?(?!\d)", re.I)),
]

# Источник раздачи — тоже узнаваемая характеристика качества.
SOURCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Blu-Ray", re.compile(r"blu-?\s?ray|\bbdrip\b|\bbdremux\b|\bremux\b", re.I)),
    ("WEB-DL", re.compile(r"web[- ]?dl(?:rip)?", re.I)),
    ("WEBRip", re.compile(r"web[- ]?rip", re.I)),
    ("HDRip", re.compile(r"hd[- ]?rip", re.I)),
    ("BDRip", re.compile(r"bd[- ]?rip", re.I)),
    ("DVDRip", re.compile(r"dvd[- ]?rip|\bdvd\b", re.I)),
    ("HDTV", re.compile(r"hdtv", re.I)),
    ("SATRip", re.compile(r"sat[- ]?rip", re.I)),
    ("HEVC", re.compile(r"\bhevc\b|\bh\.?265\b|\bx265\b", re.I)),
    ("AVC", re.compile(r"\bavc\b|\bh\.?264\b|\bx264\b", re.I)),
    ("HDR", re.compile(r"\bhdr(?:10)?\b|\bdolby\s*vision\b", re.I)),
]

# Как пользователь называет качество -> какие метки считаем совпадением.
QUALITY_SYNONYMS: dict[str, set[str]] = {
    "4k": {"2160p"},
    "4к": {"2160p"},
    "uhd": {"2160p"},
    "2160": {"2160p"},
    "2160p": {"2160p"},
    "ultra": {"2160p"},
    "1080": {"1080p"},
    "1080p": {"1080p"},
    "fullhd": {"1080p"},
    "full hd": {"1080p"},
    "fhd": {"1080p"},
    "720": {"720p"},
    "720p": {"720p"},
    "hd": {"720p", "1080p"},
    "480": {"480p"},
    "480p": {"480p"},
    "blu-ray": {"Blu-Ray"},
    "bluray": {"Blu-Ray"},
    "bdrip": {"BDRip", "Blu-Ray"},
    "web-dl": {"WEB-DL"},
    "webdl": {"WEB-DL"},
    "web": {"WEB-DL", "WEBRip"},
    "webrip": {"WEBRip"},
    "hdrip": {"HDRip"},
    "dvdrip": {"DVDRip"},
    "hdtv": {"HDTV"},
    "hevc": {"HEVC"},
    "x265": {"HEVC"},
    "h265": {"HEVC"},
    "x264": {"AVC"},
    "h264": {"AVC"},
    "avc": {"AVC"},
    "hdr": {"HDR"},
}

# ----------------------------------------------------------------------
# Озвучка
# ----------------------------------------------------------------------
# Стандартные сокращения kinozal.
VOICE_ABBR: dict[str, str] = {
    "пм": "ПМ",
    "лм": "ЛМ",
    "дб": "ДБ",
    "мв": "МВ",
    "ст": "СТ",
    "по": "ПО",
    "до": "ДО",
    "авп": "АВП",
}

# Известные студии, которые пользователь может назвать словами.
KNOWN_STUDIOS: tuple[str, ...] = (
    "LostFilm",
    "Кубик в Кубе",
    "HDrezka",
    "Jaskier",
    "AlexFilm",
    "NewStudio",
    "NewComers",
    "Пифагор",
    "Red Head Sound",
    "AMEDIA",
    "Gremlin Creative Studio",
    "Cinema Prestige",
    "ViruseProject",
    "AniLibria",
    "AniDUB",
    "Force Media",
    "Goblin",
    "Кравец",
    "Сербин",
    "Дублированный",
    "Мосфильм",
    "Хамстер",
    "BaibaKo",
    "Flarrow Films",
    "iTunes",
    "Netflix",
    "Первый канал",
)


def _split_segments(title: str) -> list[str]:
    """Делит название на части через ' / ', но не внутри скобок."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(title):
        char = title[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if depth == 0 and title.startswith(" / ", i):
            parts.append("".join(current))
            current = []
            i += 3
            continue
        current.append(char)
        i += 1
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def extract_qualities(title: str) -> list[str]:
    found: list[str] = []
    for label, pattern in RESOLUTION_PATTERNS:
        if pattern.search(title) and label not in found:
            found.append(label)
    for label, pattern in SOURCE_PATTERNS:
        if pattern.search(title) and label not in found:
            found.append(label)
    return found


def _split_top_level(text: str, separators: tuple[str, ...] = (",",)) -> list[str]:
    """Делит строку по разделителям, но не внутри скобок.

    Нужно для «ДБ (Пифагор, Red Head Sound)»: запятая внутри скобок — часть
    названия студий, а не разделитель озвучек.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if depth == 0:
            matched = None
            for sep in separators:
                if text.startswith(sep, i):
                    matched = sep
                    break
            if matched:
                parts.append("".join(current))
                current = []
                i += len(matched)
                continue
        current.append(char)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def extract_voices(title: str) -> list[str]:
    """Озвучки из названия: и сокращения (ПМ/ЛМ/ДБ), и названия студий."""
    voices: list[str] = []
    segments = _split_segments(title)
    # Озвучки живут в сегменте перед качеством: ... / 2026 / ПМ (NewComers), СТ / WEB-DL
    candidates: list[str] = []
    for segment in segments:
        if re.match(r"(?i)^(?:19|20)\d{2}", segment.strip()):
            continue
        if any(p.search(segment) for _, p in RESOLUTION_PATTERNS + SOURCE_PATTERNS):
            # В сегменте с качеством студий обычно нет, но встречаются (4K, HEVC)
            continue
        candidates.append(segment)

    for segment in candidates:
        # Разделители: запятая и двойной пробел — но не внутри скобок.
        for item in _split_top_level(segment, separators=(",", "  ")):
            item = re.sub(r"\s{2,}", " ", item).strip()
            if not item:
                continue
            low = item.lower()
            low_clean = re.sub(r"^\d+\s*[xх]\s*", "", low).strip()
            if VOICE_ABBR.get(low_clean):
                voices.append(item)
                continue
            if re.match(r"^(?:пм|лм|дб|мв|ст|по|до)\b", low_clean):
                voices.append(item)
                continue
            if re.search(r"дубляж|многоголос|двухголос|одноголос|закадров|субтитр|оригинал", low):
                voices.append(item)
                continue
            for studio in KNOWN_STUDIOS:
                if studio.lower() in low:
                    voices.append(item)
                    break

    # Студии, которые могли оказаться вне сегмента озвучек (бывает в скобках у названия)
    for studio in KNOWN_STUDIOS:
        if re.search(re.escape(studio), title, re.I):
            if not any(studio.lower() in v.lower() for v in voices):
                voices.append(studio)
    return _dedupe(voices)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = normalize(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


# ----------------------------------------------------------------------
# Сезоны и серии
# ----------------------------------------------------------------------
def extract_seasons_episodes(title: str) -> tuple[list[int], list[int], int | None]:
    """Возвращает (сезоны, серии, всего серий).

    Разбор идёт одной осмысленной последовательностью, а не набором независимых
    регулярок: иначе «1-4 сезоны: 1-44 серии из 44» превращается в серию № 4.
    """
    seasons: list[int] = []
    episodes: list[int] = []

    def add_seasons(first: int, last: int) -> None:
        span = last - first
        if 0 <= span <= 10:
            seasons.extend(range(first, last + 1))
        else:
            seasons.extend([first, last])

    # 1) Явная запись S01E05.
    for match in re.finditer(r"\bs(\d{1,2})\s*[ ._-]?e(\d{1,3})\b", title, re.I):
        seasons.append(int(match.group(1)))
        episodes.append(int(match.group(2)))

    # 2) «4 сезон: 1-10 серии из 10» / «1-4 сезоны: 1-44 серии из 44».
    block = re.search(
        r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*сезон[а-яё]*\s*[:\-–]\s*"
        r"(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?",
        title,
        re.I,
    )
    if block:
        first = int(block.group(1))
        last = int(block.group(2)) if block.group(2) else first
        add_seasons(first, last)
        episodes.append(int(block.group(3)))
        if block.group(4):
            episodes.append(int(block.group(4)))
    else:
        # 3) Сезон без списка серий: «4 сезон», «1-4 сезоны».
        season_only = re.search(
            r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*сезон[а-яё]*", title, re.I
        )
        if season_only:
            first = int(season_only.group(1))
            last = int(season_only.group(2)) if season_only.group(2) else first
            add_seasons(first, last)
        # 4) Отдельные серии: «серия 5», «выпуск 5», «эпизод 5-6».
        for match in re.finditer(
            r"(?:серия|серии|эпизод|выпуск)\s*(\d{1,4})(?:\s*[-–]\s*(\d{1,4}))?", title, re.I
        ):
            episodes.append(int(match.group(1)))
            if match.group(2):
                episodes.append(int(match.group(2)))

    total_match = re.search(r"из\s*(\d{1,4})", title)
    total = int(total_match.group(1)) if total_match else None

    seasons = sorted({s for s in seasons if 0 < s <= 40})
    episodes = sorted({e for e in episodes if 0 < e <= 999})
    return seasons, episodes, total


# ----------------------------------------------------------------------
# Названия
# ----------------------------------------------------------------------
def split_titles(title: str) -> tuple[str, str, str]:
    """(русское название, оригинальное название, год) из полного названия."""
    segments = _split_segments(title)
    ru = ""
    original = ""
    year = ""

    for index, segment in enumerate(segments):
        if re.fullmatch(r"(?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?", segment.strip()):
            year = segment.strip()
            if index + 1 < len(segments):
                # Оригинальное название обычно стоит прямо перед годом.
                if not original and index >= 1:
                    original = segments[index - 1]
            break

    if segments:
        ru = re.sub(r"\s*\((?:[^()]*)\)\s*", " ", segments[0]).strip(" -/")
        ru = re.sub(r"\s{2,}", " ", ru).strip()

    if not original and len(segments) > 1:
        candidate = segments[1]
        if not re.fullmatch(r"(?:19|20)\d{2}.*", candidate.strip()):
            original = re.sub(r"\s*\((?:[^()]*)\)\s*", " ", candidate).strip()

    # Год мог не найтись отдельным сегментом — ищем хоть где-то.
    if not year:
        year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", title)
        if year_match:
            year = year_match.group(1)
    return ru, original, year


# ----------------------------------------------------------------------
# Нормализация
# ----------------------------------------------------------------------
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def normalize(text: str) -> str:
    """Нормализация для сравнений: нижний регистр, без пунктуации, латиница."""
    if not text:
        return ""
    lowered = text.lower().replace("ё", "е")
    transliterated = "".join(_TRANSLIT.get(ch, ch) for ch in lowered)
    return re.sub(r"[\s\-–—_:.,/\\|()\[\]\"'«»!?]+", "", transliterated)


def normalize_quality(value: str) -> set[str]:
    """«4к», «4K», «2160p» -> {'2160p'}; «1080» -> {'1080p'}."""
    if not value:
        return set()
    low = value.lower().strip()
    labels: set[str] = set()
    for key, mapped in QUALITY_SYNONYMS.items():
        if key in low:
            labels |= mapped
    # Прямое совпадение с метками, которые мы снимаем с названий.
    for label, pattern in RESOLUTION_PATTERNS + SOURCE_PATTERNS:
        if pattern.search(low):
            labels.add(label)
    return labels


def quality_match(requested: str, qualities: list[str]) -> bool:
    if not requested:
        return True
    wanted = normalize_quality(requested)
    if not wanted:
        # Непонятную строку сравниваем как текст.
        return normalize(requested) in normalize(" ".join(qualities))
    available = {q for q in qualities}
    if wanted & available:
        return True
    # «HD» — это и 720p, и 1080p; «4k» подходит к «2160p» и наоборот.
    for label in wanted:
        if label in available:
            return True
    return False


VOICE_ALIASES: dict[str, set[str]] = {
    "lostfilm": {"lostfilm", "лм", "lm"},
    "лостфильм": {"lostfilm", "лм"},
    "лф": {"lostfilm", "лм"},
    "кубик": {"кубик", "kubik"},
    "hdrezka": {"hdrezka", "rezka", "пм"},
    "rezka": {"hdrezka", "rezka"},
    "jaskier": {"jaskier", "джейскиер"},
    "alexfilm": {"alexfilm"},
    "newstudio": {"newstudio", "ньюстудио"},
    "амедиа": {"amedia", "амедиа"},
    "дубляж": {"дубляж", "дублированный", "дб"},
    "дб": {"дб", "дубляж", "двухголос"},
    "пм": {"пм", "многоголос"},
    "лм": {"лм", "многоголос", "lostfilm"},
    "ст": {"ст", "субтитр"},
    "оригинал": {"оригинал", "original"},
    "netflix": {"netflix", "нф"},
}


def voice_match(requested: str, voices: list[str]) -> bool:
    if not requested:
        return True
    haystack = normalize(" ".join(voices))
    raw_haystack = " ".join(voices).lower()
    wanted_norm = normalize(requested)
    if not wanted_norm:
        return True
    if wanted_norm in haystack:
        return True
    # Алиасы: «lostfilm» -> «ЛМ (LostFilm)» -> нормализованные «лмlostfilm»
    key = re.sub(r"\s+", "", requested.lower().strip())
    aliases = VOICE_ALIASES.get(key, set())
    for alias in aliases:
        alias_norm = normalize(alias)
        if alias_norm and (alias_norm in haystack or alias in raw_haystack):
            return True
    # По словам: «студия lostfilm» должно совпасть с «LostFilm»
    for word in re.split(r"[\s,;]+", requested.lower()):
        word_norm = normalize(word)
        if len(word_norm) >= 3 and word_norm in haystack:
            return True
    return False


def parse_upload_date(text: str) -> float | None:
    """«15.10.2026 в 14:42» или «Сегодня в 14:42» -> timestamp."""
    if not text:
        return None
    cleaned = text.strip().replace("в ", "").replace("  ", " ").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.timestamp()
        except ValueError:
            continue
    if cleaned.lower().startswith(("сегодня", "today")):
        now = datetime.now()
        time_match = re.search(r"(\d{1,2}):(\d{2})", cleaned)
        hour = int(time_match.group(1)) if time_match else 0
        minute = int(time_match.group(2)) if time_match else 0
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    if cleaned.lower().startswith(("вчера", "yesterday")):
        now = datetime.now()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - 86400
    return None


def series_key(ru_title: str, original: str = "") -> str:
    """Ключ сериала для «что я уже качал»: нормализованное название без года."""
    base = original or ru_title
    if not base:
        return ""
    cleaned = re.sub(r"\s*\((?:[^()]*)\)\s*", " ", base)
    cleaned = re.sub(r"(?i)\b(?:сезон|серия|выпуск|эпизод)\b.*$", "", cleaned)
    return normalize(cleaned)[:80]


def parse_torrent(raw: dict) -> Torrent:
    """Превращает ответ MCP (search_torrents) в разобранную раздачу."""
    title = str(raw.get("title") or "").strip()
    ru, original, year = split_titles(title)
    seasons, episodes, total = extract_seasons_episodes(title)
    torrent = Torrent(
        id=str(raw.get("id") or ""),
        title=title,
        size=str(raw.get("size") or ""),
        seeds=int(raw.get("seeds") or 0),
        peers=int(raw.get("peers") or 0),
        comments=int(raw.get("comments") or 0),
        upload_date=str(raw.get("uploadDate") or raw.get("upload_date") or ""),
        uploader=str(raw.get("uploader") or ""),
        category=str(raw.get("category") or ""),
        series_title=ru,
        year=year,
        qualities=extract_qualities(title),
        voices=extract_voices(title),
        seasons=seasons,
        episodes=episodes,
        episodes_total=total,
        series_key=series_key(ru, original),
        uploaded_ts=parse_upload_date(str(raw.get("uploadDate") or raw.get("upload_date") or "")),
    )
    if not torrent.series_title:
        torrent.series_title = original
    return torrent
