#!/usr/bin/env python3
"""Проверки ошибок трекера и «что именно нашлось» в вопросе о качестве.

Запуск:  .venv/bin/python tests/test_tracker.py

Повод: человек ввёл заведомо неверное название, трекер всё равно отдал
41 похожую раздачу, а на клик по качеству прилетела ошибка «body stream
already read». Здесь проверяется, что:
  1. ошибка трекера не вылетает исключением, а превращается в причину
     «tracker» с человеческим текстом;
  2. в вопросе «какое качество» видно, ЧТО нашлось, и есть кнопки похожих
     названий, если совпало не точно;
  3. ответ MCP в виде JSON разбирается в одну понятную фразу.

Сеть, MCP и Transmission не нужны — всё подменяется заглушками.
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
from core.mcp_client import MCPError, _error_text  # noqa: E402
from core.models import Task, Torrent, Turn  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def test_error_text() -> None:
    print("\n== разбор ошибки MCP ==")
    raw = '{"success": false, "message": "трекер не выдал torrent-файл: лимит"}'
    check("из JSON берётся сообщение", _error_text(raw) == "трекер не выдал torrent-файл: лимит",
          _error_text(raw))
    check("обычный текст остаётся как есть", _error_text("что-то сломалось") == "что-то сломалось")
    check("пустой ответ не ломает", _error_text("") == "")


class FakeMCP:
    """Заглушка MCP: имитирует отказ трекера."""

    def __init__(self, message: str, *, raise_error: bool) -> None:
        self.message = message
        self.raise_error = raise_error
        self.calls = 0

    async def download(self, torrent_id: str, path: Path) -> dict:
        self.calls += 1
        if self.raise_error:
            raise MCPError(self.message)
        return {"success": False, "message": self.message}


async def test_fetch_torrent() -> None:
    print("\n== .torrent не пришёл: причина, а не падение ==")
    tmp = Path(tempfile.mkdtemp(prefix="tracker-"))
    torrents_dir = tmp / "torrent-files"
    torrents_dir.mkdir(parents=True)
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "tracker.sqlite3",
        torrents_dir=torrents_dir,
        transmission_download_dir=tmp / "torrents",
    )
    agent = Agent(settings)
    torrent = Torrent(id="42", title="Раздача", size="1 ГБ")

    agent.mcp = FakeMCP("трекер не выдал torrent-файл: лимит", raise_error=True)  # type: ignore[assignment]
    path, error, reason = await agent._fetch_torrent(torrent)
    check("исключения нет", path is None, path)
    check("причина «tracker»", reason == "tracker", reason)
    check("текст объясняет причину", "лимит" in error, error)

    agent.mcp = FakeMCP("трекер вернул страницу вместо torrent-файла", raise_error=False)  # type: ignore[assignment]
    path, error, reason = await agent._fetch_torrent(torrent)
    check("отказ без исключения тоже «tracker»", reason == "tracker" and path is None, reason)

    task = Task(
        id="tracker-task",
        kind="oneshot",
        state="searching",
        session_id="checks",
        user_query="Битва экстрасенсов реванш",
        params={"title": "Битва экстрасенсов реванш"},
    )
    await agent.store.save_task(task)
    turn = Turn()
    await agent._report_tracker_problem(
        task, torrent, "Вам недоступен торрент-файл. Вы использовали количество в сутки.", turn
    )
    message = turn.messages[-1]
    check("сказано, что трекер не выдал файл", "не выдал torrent-файл" in message.text,
          message.text[:80])
    check("про лимит объяснено", "лимит" in message.text, message.text[:200])
    labels = [action.label for action in message.actions]
    check("есть «Повторить» и «Отмена»", labels[:2] == ["Повторить", "Отмена"], labels)
    check("задача помечена неудачной", task.state == "failed", task.state)


async def test_ask_dimension() -> None:
    print("\n== вопрос о качестве показывает найденное ==")
    tmp = Path(tempfile.mkdtemp(prefix="dimension-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "dimension.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    agent = Agent(settings)
    task = Task(
        id="dim-task",
        kind="oneshot",
        state="searching",
        session_id="checks",
        user_query="Битва экстрасенсов реванш",
        params={"title": "Битва экстрасенсов реванш"},
    )
    task.options = {
        "suggestions": [
            {"title": "Экстрасенсы. Реванш", "torrent_id": "1", "score": 0.85},
            {"title": "Битва экстрасенсов", "torrent_id": "2", "score": 0.68},
        ]
    }
    found = [
        Torrent(id="10", title="Новая битва экстрасенсов (25 сезон: 15 выпуск) / HDTV (1080p)",
                series_title="Новая битва экстрасенсов", seeds=12),
        Torrent(id="11", title="Новая битва экстрасенсов (25 сезон: 14 выпуск) / HDTV (1080p)",
                series_title="Новая битва экстрасенсов", seeds=9),
        Torrent(id="12", title="Битва экстрасенсов (11 сезон) / SATRip",
                series_title="Битва экстрасенсов", seeds=4),
    ]
    turn = Turn()
    await agent._ask_dimension(task, turn, "quality", ["1080p", "720p"], 3, found=found)
    message = turn.messages[-1]
    check("видно количество раздач", "нашлось 3 раздачи" in message.text, message.text[:120])
    check("перечислено, что нашлось", "Новая битва экстрасенсов — 2 раздачи" in message.text,
          message.text[:400])
    check("сказано про неточное совпадение", "Точного совпадения по названию нет" in message.text,
          message.text[:400])
    labels = [action.label for action in message.actions]
    check("качество кнопками", labels[:2] == ["1080p", "720p"], labels)
    check("похожие названия кнопками", any(label.startswith("Это «") for label in labels), labels)

    # Точное совпадение — лишних предупреждений и кнопок быть не должно.
    exact = Task(
        id="exact-task",
        kind="oneshot",
        state="searching",
        session_id="checks",
        user_query="Тед Лассо 1080p",
        params={"title": "Тед Лассо"},
    )
    exact.options = {"suggestions": []}
    turn2 = Turn()
    await agent._ask_dimension(
        exact, turn2, "quality", ["1080p", "720p"], 1,
        found=[Torrent(id="20", title="Тед Лассо (4 сезон) / WEB-DL (1080p)",
                       series_title="Тед Лассо", seeds=30)],
    )
    text2 = turn2.messages[-1].text
    check("при точном названии без предупреждения", "Точного совпадения" not in text2, text2[:200])


async def main() -> int:
    test_error_text()
    await test_fetch_torrent()
    await test_ask_dimension()
    print("\n=== ИТОГ ===")
    if FAILS:
        print("FAIL:", ", ".join(FAILS))
        return 1
    print("OK: ошибки трекера объясняются, найденное видно в вопросе")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
