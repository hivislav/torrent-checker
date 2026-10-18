"""Фильтрация, ранжирование и «что нового» — вся логика выбора раздачи.

Здесь нет ни модели, ни сети: только чистые функции над разобранными раздачами.
Так поведение агента предсказуемо (одинаковый запрос — одинаковый выбор) и его
можно проверять тестами без трекера.
"""
from __future__ import annotations

from typing import Any, Iterable

from core.models import Torrent
from core.parsing import normalize, quality_match, voice_match

# Как показывать качество пользователю, в порядке предпочтения.
QUALITY_ORDER = ["2160p", "1080p", "720p", "480p", "Blu-Ray", "WEB-DL", "WEBRip", "BDRip", "HDRip", "HDTV", "DVDRip", "HEVC", "AVC", "HDR"]

QUALITY_LABELS = {
    "2160p": "4K (2160p)",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "Blu-Ray": "Blu-Ray",
    "WEB-DL": "WEB-DL",
    "WEBRip": "WEBRip",
    "BDRip": "BDRip",
    "HDRip": "HDRip",
    "HDTV": "HDTV",
    "DVDRip": "DVDRip",
    "HEVC": "HEVC (x265)",
    "AVC": "AVC (x264)",
    "HDR": "HDR",
}


def is_pack(torrent: Torrent) -> bool:
    """Раздача — пачка серий (сезон/несколько серий), а не одна серия."""
    return len(torrent.episodes) > 1


def episode_span(torrent: Torrent) -> tuple[int, int] | None:
    if not torrent.episodes:
        return None
    return min(torrent.episodes), max(torrent.episodes)


def title_tokens(value: str) -> list[str]:
    tokens = [normalize(word) for word in str(value).replace("/", " ").split()]
    return [token for token in tokens if len(token) >= 2]


def title_match(query: str, torrent: Torrent) -> bool:
    """Похоже ли название раздачи на то, что просили."""
    tokens = title_tokens(query)
    if not tokens:
        return True
    haystack = normalize(torrent.title)
    if not haystack:
        return False
    present = [token for token in tokens if token in haystack]
    if len(tokens) == 1:
        return bool(present)
    # Все слова или почти все (трекер иногда дописывает слова в название).
    return len(present) >= max(1, len(tokens) - 1)


def filter_candidates(
    torrents: Iterable[Torrent],
    params: dict[str, Any],
    *,
    skip_ids: set[str] | None = None,
    min_episode: int | None = None,
) -> list[Torrent]:
    """Оставляет раздачи, подходящие под параметры запроса."""
    quality = str(params.get("quality") or "").strip()
    voice = str(params.get("voice") or "").strip()
    season = params.get("season")
    skip_ids = skip_ids or set()

    result: list[Torrent] = []
    for torrent in torrents:
        if skip_ids and torrent.id in skip_ids:
            continue
        if not title_match(str(params.get("title") or ""), torrent):
            continue
        if quality and not quality_match(quality, torrent.qualities):
            continue
        if voice and not voice_match(voice, torrent.voices):
            continue
        if season:
            try:
                wanted_season = int(season)
            except (TypeError, ValueError):
                wanted_season = None
            if wanted_season and torrent.seasons and wanted_season not in torrent.seasons:
                continue
        if min_episode:
            span = episode_span(torrent)
            if span is not None and span[1] < min_episode:
                continue
        result.append(torrent)
    return result


def rank(torrents: list[Torrent], params: dict[str, Any] | None = None) -> list[Torrent]:
    """Лучшее — первым: точное качество, затем свежесть, затем сиды."""
    params = params or {}
    quality = str(params.get("quality") or "").strip()

    def score(torrent: Torrent) -> tuple:
        exact_quality = 1 if quality and quality_match(quality, torrent.qualities) else 0
        exact_voice = 1 if params.get("voice") and voice_match(str(params["voice"]), torrent.voices) else 0
        # Раздача с одной серией интереснее пачки, когда ищем «новую серию».
        single = 1 if torrent.episodes and not is_pack(torrent) else 0
        return (
            exact_quality,
            exact_voice,
            single,
            torrent.uploaded_ts or 0,
            torrent.seeds,
        )

    return sorted(torrents, key=score, reverse=True)


def collect_options(torrents: list[Torrent]) -> dict[str, Any]:
    """Что вообще есть на трекере по этому названию — для вопроса пользователю."""
    qualities: dict[str, int] = {}
    voices: dict[str, int] = {}
    for torrent in torrents:
        for quality in torrent.qualities:
            if quality in ("HDR", "AVC", "HEVC"):
                continue
            qualities[quality] = qualities.get(quality, 0) + 1
        for voice in torrent.voices:
            voices[voice] = voices.get(voice, 0) + 1

    def sort_qualities(items: dict[str, int]) -> list[str]:
        known = [q for q in QUALITY_ORDER if q in items]
        extra = sorted(k for k in items if k not in QUALITY_ORDER)
        return known + extra

    ordered_voices = [v for v, _ in sorted(voices.items(), key=lambda kv: (-kv[1], kv[0]))]
    ordered_qualities = sort_qualities(qualities)
    # Разрешение понятнее пользователю, чем источник: «1080p» вместо «WEB-DL»,
    # хотя в названии раздачи стоят оба. Источники показываем только если
    # разрешения не указаны вовсе (старые рипы).
    resolutions = [q for q in ordered_qualities if q in ("2160p", "1080p", "720p", "480p")]
    return {
        "qualities": resolutions or ordered_qualities,
        "qualities_all": ordered_qualities,
        "voices": ordered_voices[:12],
        "voices_all": ordered_voices,
        "total": len(torrents),
    }


def describe(torrent: Torrent) -> str:
    """Строка для карточки раздачи в чате."""
    parts: list[str] = []
    if torrent.seasons:
        if len(torrent.seasons) == 1:
            parts.append(f"сезон {torrent.seasons[0]}")
        else:
            parts.append(f"сезоны {torrent.seasons[0]}–{torrent.seasons[-1]}")
    if torrent.episodes:
        if is_pack(torrent):
            parts.append(f"серии {min(torrent.episodes)}–{max(torrent.episodes)}")
        else:
            parts.append(f"серия {torrent.episodes[0]}")
    if torrent.episodes_total:
        parts.append(f"из {torrent.episodes_total}")
    if torrent.qualities:
        shown = [q for q in torrent.qualities if q not in ("HDR", "AVC")]
        if shown:
            parts.append("качество: " + ", ".join(shown[:3]))
    if torrent.voices:
        parts.append("озвучка: " + ", ".join(torrent.voices[:3]))
    if torrent.size:
        parts.append(torrent.size)
    if torrent.seeds:
        parts.append(f"сидов {torrent.seeds}")
    if torrent.upload_date:
        parts.append(torrent.upload_date)
    # Пустые части (бывает, когда трекер не отдал размер или сидов) не должны
    # давать висящий разделитель в конце строки.
    return " • ".join(part for part in parts if part and part.strip())


def newest_first(torrents: Iterable[Torrent]) -> list[Torrent]:
    return sorted(torrents, key=lambda t: (t.uploaded_ts or 0, t.seeds), reverse=True)


def episodes_by_season(torrents: Iterable[Torrent]) -> dict[int, int]:
    """Максимальная серия по каждому сезону.

    Многосезонные пачки («1-4 сезоны: 1-44 серии из 44») сюда не попадают:
    в них нумерация сквозная по всем сезонам, и «44» ничего не говорит о том,
    сколько серий в четвёртом сезоне. Иначе новая 11-я серия 4-го сезона
    считалась бы уже скачанной.
    """
    result: dict[int, int] = {}
    for torrent in torrents:
        if not torrent.episodes or not torrent.seasons:
            continue
        if len(torrent.seasons) > 1:
            continue
        season = torrent.seasons[0]
        result[season] = max(result.get(season, 0), max(torrent.episodes))
    return result


def is_new_episode(
    torrent: Torrent,
    *,
    known_ids: set[str],
    season_episodes: dict[int, int],
    max_ts: float,
) -> bool:
    """Новая ли это раздача относительно того, что мы уже знаем."""
    if torrent.id in known_ids:
        return False

    if torrent.episodes and torrent.seasons and len(torrent.seasons) == 1:
        season = torrent.seasons[0]
        return max(torrent.episodes) > season_episodes.get(season, 0)

    if torrent.episodes and torrent.seasons:
        # Пачка на несколько сезонов: судим только по дате заливки.
        return (torrent.uploaded_ts or 0) > max_ts

    if torrent.episodes:
        return max(torrent.episodes) > max(season_episodes.values() or [0])

    # Не сериал (фильм, музыка, книга) — новое по дате размещения.
    return (torrent.uploaded_ts or 0) > max_ts


def find_new(
    torrents: Iterable[Torrent],
    params: dict[str, Any],
    *,
    known_ids: set[str],
    season_episodes: dict[int, int] | None = None,
    max_uploaded_ts: float = 0.0,
) -> list[Torrent]:
    """Что появилось нового с прошлой проверки.

    «Новое» — раздача, которой мы раньше не видели, и при этом она свежее
    уже известного: для сериала — номер серии выше максимального известного
    в том же сезоне, для остального — дата размещения позже последней.
    """
    season_episodes = season_episodes or {}
    fresh = [
        torrent
        for torrent in torrents
        if is_new_episode(
            torrent,
            known_ids=known_ids,
            season_episodes=season_episodes,
            max_ts=max_uploaded_ts,
        )
    ]
    return newest_first(fresh)


def looks_like_series(params: dict[str, Any], torrents: Iterable[Torrent]) -> bool:
    if params.get("season") or params.get("episode"):
        return True
    for torrent in torrents:
        if torrent.episodes or torrent.seasons:
            return True
    return False
