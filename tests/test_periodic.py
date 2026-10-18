#!/usr/bin/env python3
"""Сценарий периодической задачи: «качай новые серии, начиная с третьей».

Запуск:  .venv/bin/python tests/test_periodic.py

Тест воспроизводит поведение kinozal.me: одна и та же раздача обновляется по
мере выхода выпусков («3 сезон: 1-2 выпуски из 14» → «1-3» → «1-4»). Проверяется
весь путь: постановка задачи, первая проверка, появление серии, автоскачивание
только новой серии, повторная проверка без ложных срабатываний и вариант
«только сообщать».

Сеть не нужна: трекер, Transmission и диск заменены заглушками, но .torrent
пишется настоящий (bencode), поэтому разбор файлов идёт по-честному.
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
from core.models import Torrent  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


# ----------------------------------------------------------------------
# Вспомогательное: настоящий .torrent с заданным числом выпусков
# ----------------------------------------------------------------------
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


def write_torrent(path: Path, episodes: int, root: str = "Звёзды в джунглях.S03.2026.WEB-DLRip") -> None:
    files = [
        {
            "length": 1_900_000_000,
            "path": [f"0{n}. Звёзды в джунглях.S03.2026.WEB-DLRip.avi"],
        }
        for n in range(1, episodes + 1)
    ]
    payload = {
        "announce": "http://127.0.0.1:1/announce",
        "info": {"name": root, "piece length": 16384, "pieces": b"\x00" * 20, "files": files},
    }
    path.write_bytes(bencode(payload))


def tracker_row(episodes: int, upload_date: str = "16.10.2026 в 06:38") -> dict:
    return {
        "id": "2156284",  # одна и та же раздача, как на трекере
        "title": (
            f"Звёзды в джунглях (3 сезон: 1-{episodes} выпуски из 14) Карибский сезон / "
            "2026 / РУ / WEB-DLRip"
        ),
        "size": "3,5 ГБ",
        "seeds": 20,
        "peers": 4,
        "comments": 1,
        "uploadDate": upload_date,
        "uploader": "someone",
        "category": "46",
    }


class FakeMCP:
    """Трекер: отдаёт раздачу с текущим числом выпусков."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.episodes = 2
        self.downloads = 0

    async def search_pages(self, query: str, pages: int = 2) -> list[dict]:
        return [tracker_row(self.episodes)]

    async def download(self, torrent_id: str, path: Path) -> dict:
        self.downloads += 1
        write_torrent(Path(path), self.episodes)
        return {"success": True, "message": "ok", "path": str(path)}

    async def start(self) -> None:  # noqa: D102
        return None

    async def stop(self) -> None:  # noqa: D102
        return None


class FakeTransmission:
    """Transmission: запоминает, что и с какими файлами поставили в очередь."""

    def __init__(self, download_dir: Path) -> None:
        self.download_dir = download_dir
        self.added: list[dict] = []

    def storage_info(self):
        return SimpleNamespace(
            ok=True,
            mounted=True,
            writable=True,
            free=200 * 1024**3,
            total=224 * 1024**3,
            error="",
            own_bytes=0,
            own_complete=True,
            to_dict=lambda: {
                "volume": "EAGET",
                "path": str(self.download_dir),
                "free_human": "200 ГБ",
                "total_human": "224 ГБ",
                "ok": True,
            },
        )

    def min_free_bytes(self) -> int:
        return 5 * 1024**3

    async def add(self, path, *, need_bytes=None, files_wanted=None):
        self.added.append({"path": str(path), "files": files_wanted})
        return {
            "ok": True,
            "name": "Звёзды в джунглях.S03",
            "hash": f"hash{len(self.added)}",
            "download_dir": str(self.download_dir),
            "files_selected": files_wanted or [],
        }

    async def find_by_hash(self, torrent_hash):  # noqa: D102
        return None

    async def remove(self, torrent_hash, *, delete_data=False):  # noqa: D102
        return {"ok": True}


def build_agent() -> tuple[Agent, FakeMCP, FakeTransmission]:
    tmp = Path(tempfile.mkdtemp(prefix="periodic-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "periodic.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    settings.transmission_download_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    mcp = FakeMCP(tmp)
    transmission = FakeTransmission(settings.transmission_download_dir)
    agent.mcp = mcp  # type: ignore[assignment]
    agent.transmission = transmission  # type: ignore[assignment]
    return agent, mcp, transmission


async def create_task(agent: Agent, *, auto: bool, parsed_episode_from: int = 3):
    """Заводит периодическую задачу так же, как это делает чат."""
    from core.models import Turn

    turn = Turn()
    parsed = {
        "title": "Звёзды в джунглях",
        "season": 3,
        "episode_from": parsed_episode_from,
        "interval_minutes": 60,
        "auto_download": auto,
        "intent": "monitor",
    }
    await agent._start_periodic("s1", "звёзды в джунглях третий сезон", parsed, turn)  # noqa: SLF001
    task = None
    for message in turn.messages:
        if message.task_id:
            task = await agent.store.get_task(message.task_id)
    if task is None:
        raise RuntimeError("задача не создана: " + str([m.text for m in turn.messages][:3]))
    # Подтверждаем так, как это делает пользователь кнопкой.
    await agent.handle_action("s1", "periodic_auto" if auto else "periodic_notify",
                              {"task_id": task.id})
    return await agent.store.get_task(task.id)


async def test_auto_download() -> None:
    print("\n== «качай новую серию, начиная с третьей, каждый час» ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None
    print("  задача:", task.state, "| интервал:", task.interval_minutes, "| авто:", task.auto_download)
    check("задача следит автоматически", task.state == "monitoring" and task.auto_download)
    check("интервал из запроса — 60 минут", task.interval_minutes == 60, task.interval_minutes)
    check(
        "базовая линия: сейчас на трекере 1-2",
        (task.params.get("baseline_episodes") or {}).get("3") == 2,
        task.params.get("baseline_episodes"),
    )

    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    check("первая проверка: нового нет", task.checks_done >= 1 and not transmission.added,
          (task.checks_done, transmission.added))

    print("\n  -- на трекере появился 3-й выпуск (та же раздача) --")
    mcp.episodes = 3
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    added = transmission.added
    print("  постановки в Transmission:", [(a["files"]) for a in added])
    check("серия найдена и скачана автоматически", len(added) == 1, added)
    check("выбран только 3-й файл", added and added[-1]["files"] == [2], added[-1:] or None)
    rows = await agent.store.list_downloads()
    check("в память записана серия 3", [r["episode"] for r in rows] == [3], [r["episode"] for r in rows])

    print("\n  -- проверка без изменений --")
    before = len(transmission.added)
    await agent.check_task(task)
    check("повторно не качает", len(transmission.added) == before, transmission.added)

    print("\n  -- появился 4-й выпуск --")
    mcp.episodes = 4
    await agent.check_task(task)
    check("скачана только 4-я серия", transmission.added[-1]["files"] == [3],
          transmission.added[-1])
    rows = await agent.store.list_downloads()
    check("в памяти серии 3 и 4", sorted(r["episode"] for r in rows) == [3, 4],
          [r["episode"] for r in rows])
    agent.store.close()


async def test_notify_only() -> None:
    print("\n== «проверяй наличие новых серий, начиная с третьей» ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=False)
    assert task is not None
    check("задача только сообщает", task.state == "monitoring" and not task.auto_download)

    mcp.episodes = 3
    await agent.check_task(task)
    check("сама ничего не качает", not transmission.added, transmission.added)

    history = await agent.store.history("s1", limit=20)
    notices = [row for row in history if row.get("kind") == "notice"]
    print("  уведомления:", [row["text"][:70].replace("\n", " ") for row in notices][-1:])
    check("пришло уведомление о новом", bool(notices), len(notices))
    actions = notices[-1].get("actions") if notices else []
    check(
        "в уведомлении есть кнопка скачивания",
        any("скачать" in str(action.get("label", "")).lower() for action in actions),
        actions,
    )

    # Пользователь подтверждает — вот тут и происходит скачивание.
    await agent.handle_action("s1", "download_new", {"task_id": task.id, "torrent_id": "2156284"})
    check("по подтверждению скачалось", bool(transmission.added), transmission.added)
    agent.store.close()


async def test_episode_before_floor() -> None:
    print("\n== запрос «начиная с третьей», а на трекере только 1-2 ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None
    await agent.check_task(task)
    check("серии 1-2 не качаются", not transmission.added, transmission.added)

    # Более того: старая раздача вообще не должна считаться новой,
    # даже если у неё поменялась дата заливки.
    mcp.episodes = 2
    await agent.check_task(task)
    check("по-прежнему ничего не скачано", not transmission.added, transmission.added)
    agent.store.close()


async def test_plan_for_periodic() -> None:
    print("\n== что именно берётся из пачки ==")
    from core.episodes import parse_files, pick_new_episodes

    tmp = Path(tempfile.mkdtemp(prefix="periodic-files-"))
    path = tmp / "pack.torrent"
    write_torrent(path, 5)
    from core.torrent_meta import file_list

    entries = parse_files(file_list(path))
    picked = pick_new_episodes(entries, after_episode=2, season=3)
    print("  серии в пачке 1-5, floor=3 →", picked["episodes"], "| индексы:", picked["indexes"])
    check("взяты серии 3,4,5", picked["episodes"] == [3, 4, 5], picked["episodes"])
    check("индексы совпадают", picked["indexes"] == [2, 3, 4], picked["indexes"])


async def main() -> int:
    await test_auto_download()
    await test_notify_only()
    await test_episode_before_floor()
    await test_plan_for_periodic()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: периодические задачи качают ровно новые серии")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
