#!/usr/bin/env python3
"""Проверки подсказок «возможно, вы имели в виду…» и устойчивости к опечаткам.

Запуск:  .venv/bin/python tests/test_suggest.py

Пример из жизни: на трекере раздача называется «Экстрасенсы. Реванш», а
пользователь ищет «битва экстрасенсов реванш» — трекер ищет по всем словам и
возвращает ноль. Тест проверяет, что агент строит укороченные варианты запроса,
находит по ним раздачи, ранжирует похожие названия и показывает их кнопками.
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
from core.models import Torrent  # noqa: E402
from core.suggest import query_variants, similar_titles, similarity  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def test_variants() -> None:
    print("\n== варианты запроса ==")
    variants = query_variants("битва экстрасенсов реванш")
    print("  ", variants)
    check("есть вариант без первого слова", "экстрасенсов реванш" in variants, variants)
    check("есть скользящее окно", "битва экстрасенсов" in variants, variants)
    check("исходный запрос не дублируется", "битва экстрасенсов реванш" not in variants)
    check("одно слово не дробится", query_variants("дюна") == [], query_variants("дюна"))


def test_similarity() -> None:
    print("\n== похожесть ==")
    query = "битва экстрасенсов реванш"
    scores = {
        title: similarity(query, title)
        for title in ["Экстрасенсы. Реванш", "Новая Битва экстрасенсов", "Дюна"]
    }
    for title, score in scores.items():
        print(f"  {score:.3f}  {title}")
    check("нужное название — первое по похожести",
          max(scores, key=scores.get) == "Экстрасенсы. Реванш", scores)
    check("посторонний фильм отсекается", scores["Дюна"] < 0.55, scores["Дюна"])

    for typo, real in (("тед ласо", "Тед Лассо"), ("караль талсы", "Король Талсы"),
                       ("матрица ревалюшн", "Матрица: Революция")):
        value = similarity(typo, real)
        print(f"  {value:.3f}  «{typo}» → «{real}»")
        check(f"опечатка «{typo}» распознаётся", value >= 0.55, value)


def test_ranking() -> None:
    print("\n== ранжирование подсказок ==")
    torrents = [
        Torrent(id="1", title="Экстрасенсы. Реванш (3 сезон: 1-5 выпуск)", series_title="Экстрасенсы. Реванш"),
        Torrent(id="2", title="Новая Битва экстрасенсов (23-24 сезоны)", series_title="Новая Битва экстрасенсов"),
        Torrent(id="3", title="Дюна", series_title="Дюна"),
    ]
    found = similar_titles("битва экстрасенсов реванш", torrents)
    print("  ", [(item["title"], item["score"]) for item in found])
    check("первая подсказка — верная", found and found[0]["title"] == "Экстрасенсы. Реванш", found[:1])
    check("постороннее не попало", all(item["title"] != "Дюна" for item in found), found)


class TypoMCP:
    """Трекер, который знает только правильное название, но ищет по всем словам."""

    KNOWN = [
        {
            "id": "555",
            "title": "Экстрасенсы. Реванш (3 сезон: 1-5 выпуск) / 2026 / РУ / WEB-DL (1080p)",
            "size": "12 ГБ",
            "seeds": 30,
            "peers": 3,
            "comments": 2,
            "uploadDate": "18.10.2026 в 10:00",
            "uploader": "x",
            "category": "11",
        }
    ]

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search_pages(self, query: str, pages: int = 2) -> list[dict]:
        self.queries.append(query)
        words = query.lower().split()
        # Как kinozal: все слова запроса должны встретиться в названии.
        matches = []
        for row in self.KNOWN:
            title = row["title"].lower()
            if all(any(word[:5] in part for part in title.split()) for word in words):
                matches.append(row)
        return matches

    async def download(self, torrent_id: str, path) -> dict:
        """Пишет настоящий .torrent с одним файлом, чтобы разбор состава работал."""
        import pathlib as _pathlib

        def bencode(value) -> bytes:
            if isinstance(value, int):
                return b"i" + str(value).encode() + b"e"
            if isinstance(value, str):
                value = value.encode()
            if isinstance(value, bytes):
                return str(len(value)).encode() + b":" + value
            if isinstance(value, list):
                return b"l" + b"".join(bencode(item) for item in value) + b"e"
            if isinstance(value, dict):
                return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"
            raise TypeError(type(value))

        payload = {
            "announce": "http://127.0.0.1:1/announce",
            "info": {
                "name": "Экстрасенсы. Реванш.S03.WEB-DL.1080p.mkv",
                "length": 12 * 1024**3,
                "piece length": 16384,
                "pieces": b"\x00" * 20,
            },
        }
        _pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        _pathlib.Path(path).write_bytes(bencode(payload))
        return {"success": True, "message": "ok", "path": str(path)}

    async def start(self) -> None:  # noqa: D102
        return None

    async def stop(self) -> None:  # noqa: D102
        return None


async def test_agent_flow() -> None:
    print("\n== агент предлагает похожие названия ==")
    tmp = Path(tempfile.mkdtemp(prefix="typo-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "t.sqlite3", torrents_dir=tmp / "tr"
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    mcp = TypoMCP()
    agent.mcp = mcp  # type: ignore[assignment]

    torrents, suggestions = await agent._search_with_suggestions(  # noqa: SLF001
        {"title": "битва экстрасенсов реванш"}
    )
    print("  запросы к трекеру:", mcp.queries)
    print("  подсказки:", [(item["title"], item["score"]) for item in suggestions])
    check("исходный запрос спросили", "битва экстрасенсов реванш" in mcp.queries, mcp.queries)
    check("пробовали укороченные варианты", any("экстрасенсов" in q for q in mcp.queries), mcp.queries)
    check("подсказка найдена", bool(suggestions), suggestions)
    check("подсказка верная", suggestions and suggestions[0]["title"] == "Экстрасенсы. Реванш",
          suggestions[:1])

    # Экран подсказок в чате
    task = await agent._task_from_params("s", "битва экстрасенсов реванш",  # noqa: SLF001
                                        {"title": "битва экстрасенсов реванш"}, kind="oneshot")
    task.options = {"pool": [t.to_dict() for t in torrents], "suggestions": suggestions}
    await agent.store.save_task(task)
    from core.models import Turn

    turn = Turn()
    await agent._selection_step(task, turn)  # noqa: SLF001
    message = turn.messages[-1]
    print("  текст:", message.text.replace("\n", " | ")[:160])
    check("сказано «возможно, вы имели в виду»", "Возможно, вы имели в виду" in message.text,
          message.text[:120])
    check("название есть в тексте", "Экстрасенсы. Реванш" in message.text, message.text[:160])
    labels = [action.label for action in message.actions]
    check("есть кнопка с названием", "Экстрасенсы. Реванш" in labels, labels)
    check("кнопки сгруппированы", any(action.group == "Похожие названия" for action in message.actions),
          [(a.group, a.label) for a in message.actions])

    # Выбор названия → поиск заново по нему → находится
    turn2 = Turn()
    await agent._apply_title(task, "Экстрасенсы. Реванш", turn2)  # noqa: SLF001
    texts = " ".join(item.text for item in turn2.messages)
    print("  после выбора:", texts.replace("\n", " | ")[:160])
    check("ищет по исправленному названию", "Экстрасенсы. Реванш" in texts, texts[:120])
    check("нашлись раздачи", task.state == "awaiting_confirm"
          or any("нашлось" in item.text for item in turn2.messages), task.state)
    agent.store.close()


async def test_hyphen_split_queries() -> None:
    """Склеенное дефисами название с опечаткой ищется по частям.

    Настоящий случай: «черепашки-мутантантах-ниндзя» — для трекера это одно
    слово, и опечатка в нём оставляет запрос вообще без ответа: укороченные
    варианты строятся по словам, а слово одно.
    """
    print("\n== опечатка в названии через дефисы ==")
    tmp = Path(tempfile.mkdtemp(prefix="hyphen-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "t.sqlite3", torrents_dir=tmp / "tr"
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    mcp = TypoMCP()
    agent.mcp = mcp  # type: ignore[assignment]
    await agent._search_with_suggestions(  # noqa: SLF001
        {"title": "черепашки-мутантантах-ниндзя"}, original="черепашки-мутантантах-ниндзя"
    )
    print("  запросы к трекеру:", mcp.queries)
    check("исходный запрос спросили",
          "черепашки-мутантантах-ниндзя" in mcp.queries, mcp.queries)
    check("искали и по частям названия",
          any(item == "черепашки" for item in mcp.queries), mcp.queries)
    check("и по последнему слову",
          any(item == "ниндзя" for item in mcp.queries), mcp.queries)
    agent.store.close()


async def main() -> int:
    test_variants()
    test_similarity()
    test_ranking()
    await test_agent_flow()
    await test_hyphen_split_queries()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: опечатки распознаются, подсказки предлагаются")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
