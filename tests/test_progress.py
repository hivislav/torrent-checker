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

    async def list_torrents(self, limit: int = 200):  # noqa: D102
        return [{"hash": key, "name": state.get("name", ""),
                 "status": state.get("status", ""), "download_dir": str(self.download_dir)}
                for key, state in self.state.items()]


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


async def test_cancel_stops_download() -> None:
    """«Отменить» задачу на скачивание значит и убрать раздачу из очереди."""
    print("\n== отмена задачи убирает раздачу из очереди ==")
    agent, fake = build_agent()
    task = await make_task(agent)
    from core.models import Torrent

    folder = Path(agent.settings.transmission_download_dir) / "Gangster"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Gangster.S02E01.mkv").write_bytes(b"\x00" * (3 * 1024 * 1024))

    await agent.store.record_download(
        task, Torrent(id="777", title="Гангстерленд (2 сезон: 1-5 серии из 10)", size="5 ГБ",
                      seasons=[2], episodes=[1, 5], episodes_total=10),
        str(agent.settings.torrents_dir / "777.torrent"), "h1", torrent_name="Gangster",
        episodes=[{"season": 2, "episode": 1}],
    )
    fake.removed = []
    fake.state["h1"] = {
        "hash": "h1", "name": "Gangster", "percent": 13.0, "done": False,
        "rate": 0, "rate_human": "", "eta_seconds": None, "eta_human": "",
        "size": 5 * 1024**3, "size_human": "5 ГБ",
        "downloaded_human": "0,6 ГБ", "left_human": "4,4 ГБ", "status": "downloading",
    }
    # фейку нужен find_by_hash, чтобы отмена знала папку и файлы
    async def find_by_hash(torrent_hash):  # noqa: D102
        return {"hash": torrent_hash, "name": "Gangster",
                "download_dir": str(agent.settings.transmission_download_dir), "files": []}

    fake.find_by_hash = find_by_hash  # type: ignore[assignment]
    fake.remove = _remover(fake)  # type: ignore[assignment]

    cancelled = await agent.cancel_task("dl")
    assert cancelled is not None
    print("  задача:", cancelled.state, "| примечание:", cancelled.last_result)
    print("  убрано из очереди:", fake.removed)
    check("задача отменена", cancelled.state == "cancelled", cancelled.state)
    check("раздача убрана из Transmission", fake.removed == ["h1"], fake.removed)
    check("недокачанное удалено с диска", not folder.exists())
    check("запись о скачанном удалена", not await agent.store.list_downloads(),
          await agent.store.list_downloads())
    check("в примечании сказано, что сделали", "убрана из очереди" in (cancelled.last_result or ""),
          cancelled.last_result)

    print("  -- отмена уже докачанной раздачи --")
    tr_task = await make_task(agent)
    tr_task.id = "dl2"
    await agent.store.save_task(tr_task)
    fake.state["h1"]["done"] = True
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Gangster.S02E01.mkv").write_bytes(b"\x01" * 1024)
    done_task = await agent.cancel_task("dl2")
    assert done_task is not None
    print("  примечание:", done_task.last_result, "| папка цела:", folder.exists())
    check("докачанное не удаляем", folder.exists())
    check("и говорим об этом", "докачана" in (done_task.last_result or ""), done_task.last_result)
    agent.store.close()


def _remover(fake):
    async def remove(torrent_hash, *, delete_data=False):  # noqa: D102
        fake.removed.append(torrent_hash)
        return {"ok": True}
    return remove


async def test_hidden_until_done() -> None:
    """«Скачано на диск» — только после завершения, а не в момент постановки."""
    print("\n== пока качается, в «Скачано» ничего нет ==")
    agent, fake = build_agent()
    task = await make_task(agent)
    from core.models import Torrent

    await agent.store.record_download(
        task, Torrent(id="777", title="Гангстерленд (2 сезон: 1-5 серии из 10)",
                      size="5 ГБ", seasons=[2], episodes=[1, 5], episodes_total=10),
        "/tmp/777.torrent", "h1", torrent_name="Gangster.S02",
        episodes=[{"season": 2, "episode": n} for n in range(1, 6)],
    )
    fake.state["h1"] = {
        "hash": "h1", "name": "Gangster.S02", "percent": 12.0, "done": False,
        "rate": 0, "rate_human": "", "eta_seconds": None, "eta_human": "",
        "size": 5 * 1024**3, "size_human": "5 ГБ",
        "downloaded_human": "0,6 ГБ", "left_human": "4,4 ГБ", "status": "downloading",
    }
    agent._downloads_cache = None  # сбрасываем кэш списка (он на 3 секунды)  # noqa: SLF001
    items = await agent.downloads()
    print("  пока качается — записей в «Скачано»:", len(items))
    check("пока качается, в «Скачано» пусто", items == [], items)
    rows = await agent.store.list_downloads()
    check("но в памяти агента раздача уже есть", len(rows) == 1, len(rows))
    check("и она ещё не помечена завершённой", not rows[0].get("done_at"), rows[0].get("done_at"))

    print("  -- загрузка завершилась --")
    fake.state["h1"] = {
        "hash": "h1", "name": "Gangster.S02", "percent": 100.0, "done": True,
        "rate": 0, "rate_human": "", "eta_seconds": None, "eta_human": "",
        "size": 5 * 1024**3, "size_human": "5 ГБ",
        "downloaded_human": "5 ГБ", "left_human": "0 Б", "status": "seeding",
    }
    agent._downloads_cache = None  # noqa: SLF001
    items = await agent.downloads()
    print("  после завершения — записей:", len(items),
          "| серии:", len(items[0]["episodes"]) if items else 0)
    check("после завершения раздача в «Скачано»", len(items) == 1, items)
    rows = await agent.store.list_downloads()
    check("и помечена завершённой", bool(rows[0].get("done_at")), rows[0].get("done_at"))
    check("серии внутри одной записи", len(items[0]["episodes"]) == 5,
          items[0]["episodes"] if items else None)
    agent.store.close()


async def main() -> int:
    print("\n== прогресс загрузки ==")


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

    await test_hidden_until_done()
    await test_cancel_stops_download()
    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: прогресс загрузки отслеживается, задача ждёт завершения")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
