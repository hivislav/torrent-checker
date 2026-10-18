"""Похожие названия: «не найдено — возможно, вы имели в виду…».

Пользователь часто помнит название неточно: на трекере «Экстрасенсы. Реванш»,
а в запросе «битва экстрасенсов реванш» — поиск по трекеру требует все слова и
возвращает ноль. Такое лучше не молча терять, а подсказать.

Как работает:

1. `query_variants` строит более короткие запросы из исходного — скользящие окна
   по словам («экстрасенсов реванш»), варианты без первого и без последнего
   слова, самое длинное слово. Именно эти варианты ищутся на трекере.
2. `similar_titles` сравнивает найденные названия с исходным запросом и
   возвращает самые похожие.

Сравнение — по нормализованным строкам (латиница, без пунктуации) и по словам,
причём отдельно оценивается «хвост» запроса: в названиях фильмов и сериалов
важные слова обычно стоят в конце («…Реванш»), а лишние — в начале («Битва…»).
"""
from __future__ import annotations

import difflib
import re
from typing import Iterable

from core.parsing import normalize
from core.models import Torrent

# Сколько вариантов запроса пробовать и сколько подсказок показывать.
MAX_VARIANTS = 6
MAX_SUGGESTIONS = 5
MIN_SCORE = 0.55


def query_variants(query: str, limit: int = MAX_VARIANTS) -> list[str]:
    """Более короткие варианты запроса: их и ищем, если исходный дал ноль."""
    words = [word for word in str(query).split() if word]
    if len(words) < 2:
        return []
    original = query.strip().lower()
    out: list[str] = []

    def add(parts: Iterable[str]) -> None:
        variant = " ".join(parts).strip()
        if not variant or variant.lower() == original or variant in out:
            return
        out.append(variant)

    for size in (3, 2):
        if len(words) > size:
            for start in range(len(words) - size + 1):
                add(words[start : start + size])
    add(words[1:])  # без первого слова: «битва» часто лишняя
    add(words[:-1])  # без последнего
    add([max(words, key=len)])  # самое длинное слово
    return out[:limit]


def _token_match(left: str, right: str) -> bool:
    """Слова совпадают, даже если стоят в другой форме: экстрасенсов/экстрасенсы."""
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.8


def _coverage(tokens: list[str], haystack: list[str]) -> float:
    if not tokens:
        return 0.0
    matched = sum(1 for token in tokens if any(_token_match(token, other) for other in haystack))
    return matched / len(tokens)


def similarity(query: str, title: str) -> float:
    """Насколько найденное название похоже на то, что просили (0..1)."""
    query_norm = normalize(str(query))
    title_norm = normalize(str(title))
    if not query_norm or not title_norm:
        return 0.0

    ratio = difflib.SequenceMatcher(None, query_norm, title_norm).ratio()

    query_tokens = [normalize(word) for word in str(query).split() if normalize(word)]
    title_tokens = [normalize(word) for word in str(title).split() if normalize(word)]
    coverage = _coverage(query_tokens, title_tokens)

    # Хвост запроса важнее начала: «…Реванш» — то, что определяет название.
    tail = query_tokens[-2:] if len(query_tokens) > 2 else query_tokens
    tail_coverage = _coverage(tail, title_tokens)

    # Штраф за лишние слова в названии, которых не просили.
    extra = max(0, len(title_tokens) - len(query_tokens))
    penalty = min(0.15, 0.03 * extra)

    score = 0.45 * ratio + 0.35 * tail_coverage + 0.20 * coverage - penalty
    return max(0.0, min(1.0, score))


def similar_titles(
    query: str, torrents: Iterable[Torrent], limit: int = MAX_SUGGESTIONS
) -> list[dict]:
    """Самые похожие названия из найденных раздач — по одному на название."""
    best: dict[str, dict] = {}
    for torrent in torrents:
        title = torrent.series_title or torrent.title
        score = similarity(query, title)
        if score < MIN_SCORE:
            continue
        key = normalize(title)
        if key in best and best[key]["score"] >= score:
            continue
        best[key] = {
            "title": title,
            "score": round(score, 3),
            "torrent_id": torrent.id,
            "torrent_title": torrent.title,
        }
    ranked = sorted(best.values(), key=lambda item: -item["score"])
    return ranked[:limit]


def best_guess(query: str, torrents: Iterable[Torrent]) -> dict | None:
    found = similar_titles(query, torrents, limit=1)
    return found[0] if found else None
