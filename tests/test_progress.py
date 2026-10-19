#!/usr/bin/env python3
"""Проверки отслеживания загрузки: задача не «завершается» сразу после постановки.

Запуск:  .venv/bin/python tests/test_progress.py

Transmission не умеет присылать события, поэтому агент опрашивает прогресс сам
(`Agent.refresh_progress`). Тест подменяет Transmission и проверяет переходы:
«скачивание» → обновление процента/скорости → «готово» с уведомлением.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import Agent  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.models import Task  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


class FakeTransmission:
    """Отдаёт заданный прогресс; запоминает, сколько раз его спрашивали."""

    def __init__(self, download_dir: Path) -> None:
        self.download_dir = download_dir
        self.state: dict[str, dict] = {}
        self.calls = 0

    def storage_info(self):  # noqa: D102
        return SimpleNamespace(
            ok=True, mounted=True, writable=True, free=100 * 1024**3,
            total=224 * 1024**3, error="", own_bytes=0, own_complete=True,
            to_dict=lambda: {"volume": "EAGET", "path": str(self.download_dir),
                             "free_human": "100 ГБ", "total_human": "224 ГБ", "ok": True},
        )

    def min_free_bytes(self) -> int:  # noqa: D102
        return 5 * 1024**3

    async def progress(self, hashes):  # noqa: D102
        self.calls += 1
        return {
            item: self.state[item] for item in hashes if item in self.state
        }

    async def add(self, path, *, need_bytes=None, files_wanted=None):  # noqa: D102
        return {"ok": True, "name": "Проба", "hash": "h1",
                "download_dir": str(self.download_dir), "files_selected": []}


def build_agent() -> tuple[Agent, FakeTransmission]:
    tmp = Path(tempfile.mkdtemp(prefix="progress-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "p.sqlite3",
        torrents_dir=tmp / "torrents", transmission_download_dir=tmp / "dl",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    settings.transmission_download_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    fake = FakeTransmission(settings.transmission_download_dir)
    agent.transmission = fake  # type: ignore[assignment]
    return agent, fake


async def make_task(agent: Agent) -> Task:
    task = Task(
        id="dl",
        kind="oneshot",
        state="downloading",
        session_id="s",
        user_query="проба",
        params={"title": "Проба", "transmission_hash": "h1"},
    )
    await agent.store.save_task(task)
    return task


async def main() -> int:
    print("\n== прогресс загрузки ==")
    agent, fake = build_agent()

    fake.state["h1"] = {
        "hash": "h1", "name": "Проба", "percent": 42.5, "done": False,
        "rate": 1024 * 1024, "rate_human": "1 МБ/с",
        "eta_seconds": 600, "eta_human": "10 мин",
        "size": 4 * 1024**3, "size_human": "4 ГБ",
        "downloaded_human": "1,7 ГБ", "left_human": "2,3 ГБ", "status": "downloading",
    }
    task = await make_task(agent)
    await agent.refresh_progress()
    updated = await agent.store.get_task("dl")
    assert updated is not None
    progress = (updated.params or {}).get("progress") or {}
    print("  состояние:", updated.state, "| подпись:", updated.last_result)
    check("задача остаётся в «скачивании»", updated.state == "downloading", updated.state)
    check("процент сохранён", progress.get("percent") == 42.5, progress.get("percent"))
    check("скорость в подписи", "МБ/с" in updated.last_result, updated.last_result)
    check("остаток времени в подписи", "осталось" in updated.last_result, updated.last_result)
    check("размер выбранного, а не всей раздачи", progress.get("size_human") == "4 ГБ",
          progress.get("size_human"))

    print("\n  -- загрузка завершилась --")
    fake.state["h1"] = {
        "hash": "h1", "name": "Проба", "percent": 100.0, "done": True,
        "rate": 0, "rate_human": "", "eta_seconds": None, "eta_human": "",
        "size": 4 * 1024**3, "size_human": "4 ГБ",
        "downloaded_human": "4 ГБ", "left_human": "0 Б", "status": "seeding",
    }
    queue = agent.events.subscribe()
    await agent.refresh_progress()
    finished = await agent.store.get_task("dl")
    assert finished is not None
    check("задача уходит в завершённые", finished.state == "done", finished.state)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    check("опубликовано событие о завершении",
          any(event.get("type") == "download_complete" for event in events),
          [event.get("type") for event in events])
    history = await agent.store.history("s", limit=10)
    check("в чат добавлено сообщение о готовности",
          any("Скачано" in (row.get("text") or "") for row in history),
          [row.get("text", "")[:40] for row in history])

    print("\n  -- раздачу убрали из очереди --")
    fake.state.pop("h1")
    fake2_task = Task(
        id="dl2", kind="oneshot", state="downloading", session_id="s",
        user_query="проба2", params={"title": "Проба2", "transmission_hash": "h2"},
    )
    await agent.store.save_task(fake2_task)
    await agent.refresh_progress()
    gone = await agent.store.get_task("dl2")
    assert gone is not None
    check("исчезнувшая раздача не висит в «скачивании»", gone.state == "done", gone.state)

    check("прогресс спрашивали у Transmission", fake.calls >= 2, fake.calls)

    print("\n== «готова», хотя торрент ещё качается ==")
    # Бывает, что после постановки в очередь RPC ещё не знает прогресс: раньше
    # пустой ответ считался завершением, задача уходила в «готово» и больше не
    # опрашивалась — а файл качался.
    fake.state["h3"] = {
        "hash": "h3", "name": "Проба3", "percent": 0.9, "done": False,
        "rate": 1531904, "rate_human": "1,5 МБ/с", "eta_seconds": None, "eta_human": "",
        "size": 24947720192, "size_human": "23,2 ГБ",
        "downloaded_human": "211,9 МБ", "left_human": "23 ГБ", "status": "downloading",
    }
    stale = Task(
        id="dl3", kind="oneshot", state="done", session_id="s",
        user_query="проба3", params={"title": "Проба3", "transmission_hash": "h3"},
    )
    stale.last_result = "скачано: Проба3"
    await agent.store.save_task(stale)
    await agent.refresh_progress()
    revived = await agent.store.get_task("dl3")
    assert revived is not None
    print("  состояние:", revived.state, "|", revived.last_result)
    check("задача возвращена в «скачивание»", revived.state == "downloading", revived.state)
    check("видно процент", "0.9%" in (revived.last_result or ""), revived.last_result)

    # А по-настоящему завершённую задачу второй раз не трогаем.
    fake.state["h1"] = {
        "hash": "h1", "name": "Проба", "percent": 100.0, "done": True,
        "rate": 0, "rate_human": "", "eta_seconds": None, "eta_human": "",
        "size": 4 * 1024**3, "size_human": "4 ГБ",
        "downloaded_human": "4 ГБ", "left_human": "0 Б", "status": "seeding",
    }
    await agent.refresh_progress()
    again = await agent.store.get_task("dl")
    assert again is not None
    check("завершённая задача остаётся завершённой", again.state == "done", again.state)
    agent.store.close()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: прогресс загрузки отслеживается, задача ждёт завершения")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
