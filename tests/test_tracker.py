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
    check("сказано, что это другие сериалы",
          "другие сериалы" in message.text, message.text[:400])
    check("подсказки перечислены так же, как на кнопках",
          "вы имели в виду" in message.text and "Экстрасенсы. Реванш" in message.text,
          message.text[:400])
    check("нет второго списка «что нашлось»",
          "Что нашлось:" not in message.text, message.text[:400])
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
    check("при точном названии без предупреждения",
          "другие сериалы" not in text2 and "Что нашлось:" not in text2, text2[:200])


class ListingMCP:
    """Заглушка MCP: список файлов со страницы раздачи и счёт вызовов .torrent."""

    def __init__(self, files: list[dict] | None, *, fail_listing: bool = False) -> None:
        self.files = files
        self.fail_listing = fail_listing
        self.downloads = 0

    async def file_list(self, torrent_id: str) -> dict:
        if self.fail_listing:
            raise RuntimeError("страница не отдала список")
        return {"success": True, "files": self.files or []}

    async def download(self, torrent_id: str, path: Path) -> dict:
        self.downloads += 1
        return {"success": False, "message": "Вам недоступен торрент-файл"}


async def test_card_without_torrent() -> None:
    print("\n== карточка без траты .torrent ==")
    tmp = Path(tempfile.mkdtemp(prefix="listing-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "listing.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    agent = Agent(settings)
    task = Task(
        id="listing-task",
        kind="oneshot",
        state="searching",
        session_id="checks",
        user_query="Экстрасенсы реванш",
        params={"title": "Экстрасенсы реванш", "quality": "1080p"},
    )
    await agent.store.save_task(task)

    files = [
        {"name": "05.Экстрасенсы. Реванш.s03.E05.(2026).HDTV.(1080р).ts", "size": 6124008256},
    ]
    mcp = ListingMCP(files)
    agent.mcp = mcp  # type: ignore[assignment]
    torrent = Torrent(
        id="2157531",
        title="Экстрасенсы. Реванш (3 сезон: 5 выпуск из 15) / 2026 / РУ / HDTV (1080p)",
        size="5.7 ГБ",
        seeds=248,
        series_title="Экстрасенсы. Реванш",
        qualities=["1080p"],
        seasons=[3],
        episodes=[5],
    )
    turn = Turn()
    await agent._present_candidate(task, [torrent], turn)
    message = turn.messages[-1]
    check("карточка показана", "Раздача:" in message.text, message.text[:100])
    check("торрент-файл не качался", mcp.downloads == 0, mcp.downloads)
    check("файл из списка попал в карточку",
          "05.Экстрасенсы" in message.text, message.text[:300])
    check("кнопка постановки в очередь есть",
          any(action.kind == "start_download" for action in message.actions),
          [action.label for action in message.actions])

    # Страница не отдала список — тогда путь через .torrent (и его ошибка).
    task2 = Task(
        id="listing-task-2",
        kind="oneshot",
        state="searching",
        session_id="checks",
        user_query="Экстрасенсы реванш",
        params={"title": "Экстрасенсы реванш", "quality": "1080p"},
    )
    await agent.store.save_task(task2)
    mcp2 = ListingMCP(None, fail_listing=True)
    agent.mcp = mcp2  # type: ignore[assignment]
    turn2 = Turn()
    await agent._present_candidate(task2, [torrent], turn2)
    check("без списка пробует .torrent", mcp2.downloads == 1, mcp2.downloads)
    check("и объясняет отказ трекера",
          any("не выдал torrent-файл" in message.text for message in turn2.messages),
          [message.text[:60] for message in turn2.messages])


async def test_title_first() -> None:
    print("\n== неточное название: сначала сериал, потом качество ==")
    tmp = Path(tempfile.mkdtemp(prefix="title-first-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "title-first.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    agent = Agent(settings)
    task = Task(
        id="title-task", kind="oneshot", state="searching", session_id="checks",
        user_query="Битва экстрасенсов реванш",
        params={"title": "Битва экстрасенсов реванш"},
    )
    other = Torrent(
        id="900", title="Новая битва экстрасенсов (25 сезон: 15 выпуск) / HDTV (1080p)",
        size="5.7 ГБ", seeds=50, series_title="Новая битва экстрасенсов",
        qualities=["1080p"], seasons=[25], episodes=[15],
    )
    other2 = Torrent(
        id="901", title="Новая Битва экстрасенсов (25 сезон: 14 выпуск) / HDTV (720p)",
        size="3 ГБ", seeds=10, series_title="Новая Битва экстрасенсов",
        qualities=["720p"], seasons=[25], episodes=[14],
    )
    task.options = {
        "pool": [other.to_dict(), other2.to_dict()],
        "suggestions": [
            {"title": "Экстрасенсы. Реванш", "torrent_id": "2154198", "score": 0.85},
            {"title": "Битва экстрасенсов", "torrent_id": "836714", "score": 0.68},
        ],
    }
    await agent.store.save_task(task)
    turn = Turn()
    await agent._selection_step(task, turn)
    message = turn.messages[-1]
    check("спрашиваем сериал, а не качество",
          "Какой сериал вы имели в виду?" in message.text, message.text[:300])
    check("в списке те же названия, что на кнопках",
          "Экстрасенсы. Реванш" in message.text and "Битва экстрасенсов" in message.text,
          message.text[:400])
    labels = [action.label for action in message.actions]
    check("кнопок качества здесь нет", not any(label in ("1080p", "720p") for label in labels), labels)
    check("есть кнопки подсказок",
          sum(1 for label in labels if label.startswith("Это «")) == 2, labels)
    check("есть отмена", any(action.kind == "cancel" for action in message.actions), labels)


async def test_alternatives_and_pack() -> None:
    print("\n== варианты подписаны, сезон предлагается кнопкой ==")
    tmp = Path(tempfile.mkdtemp(prefix="pack-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "pack.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    agent = Agent(settings)
    agent.mcp = ListingMCP(["dummy"]) if False else agent.mcp
    mcp = ListingMCP([{"name": "05.Серия.s03.E05.ts", "size": 6124008256}])
    agent.mcp = mcp  # type: ignore[assignment]

    episode5 = Torrent(
        id="2157531", title="Экстрасенсы. Реванш (3 сезон: 5 выпуск из 15) / HDTV (1080p)",
        size="5.74 ГБ", seeds=251, series_title="Экстрасенсы. Реванш", series_key="ekstrasensyrevansh",
        qualities=["1080p", "HDTV"], seasons=[3], episodes=[5],
    )
    episode4 = Torrent(
        id="2156772", title="Экстрасенсы. Реванш (3 сезон: 4 выпуск из 15) / HDTV (1080p)",
        size="5.74 ГБ", seeds=30, series_title="Экстрасенсы. Реванш", series_key="ekstrasensyrevansh",
        qualities=["1080p", "HDTV"], seasons=[3], episodes=[4],
    )
    pack = Torrent(
        id="2154196", title="Экстрасенсы. Реванш (3 сезон: 1-5 выпуск) / WEB-DL (1080p)",
        size="16.2 ГБ", seeds=6, series_title="Экстрасенсы. Реванш", series_key="ekstrasensyrevansh",
        qualities=["1080p", "WEB-DL"], seasons=[3], episodes=[1, 5],
    )
    task = Task(
        id="pack-task", kind="oneshot", state="searching", session_id="checks",
        user_query="Экстрасенсы реванш", params={"title": "Экстрасенсы. Реванш", "quality": "1080p"},
    )
    task.options = {"pool": [episode5.to_dict(), episode4.to_dict(), pack.to_dict()]}
    await agent.store.save_task(task)

    turn = Turn()
    await agent._present_candidate(task, [episode5, episode4], turn)
    message = turn.messages[-1]
    check("подпись варианта по серии, а не «вариант 2»",
          "серия 4" in message.text and "5,74 ГБ" in message.text, message.text[-300:])
    check("весь сезон предложен в тексте",
          "Весь сезон есть отдельной раздачей" in message.text, message.text[-400:])
    labels = [action.label for action in message.actions]
    check("есть кнопка «Скачать весь сезон»",
          any("весь сезон" in label.lower() for label in labels), labels)
    pack_action = next(action for action in message.actions if "весь сезон" in action.label.lower())
    check("кнопка ведёт на раздачу-сезон", pack_action.payload.get("torrent_id") == "2154196",
          pack_action.payload)
    check("вопрос про Transmission в конце",
          message.text.strip().endswith("Ставлю в Transmission?"), message.text[-60:])


async def main() -> int:
    test_error_text()
    await test_fetch_torrent()
    await test_ask_dimension()
    await test_title_first()
    await test_card_without_torrent()
    await test_alternatives_and_pack()
    print("\n=== ИТОГ ===")
    if FAILS:
        print("FAIL:", ", ".join(FAILS))
        return 1
    print("OK: ошибки трекера объясняются, найденное видно в вопросе")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
