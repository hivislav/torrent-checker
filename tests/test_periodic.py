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


def write_torrent_named(path: Path, names: list[str], size: int = 1_900_000_000) -> None:
    """Настоящий .torrent с заданными именами файлов (как на трекере)."""
    files = [{"length": size, "path": [name]} for name in names]
    payload = {
        "announce": "http://127.0.0.1:1/announce",
        "info": {"name": "pack", "piece length": 16384, "pieces": b"\x00" * 20, "files": files},
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
        # Страница раздачи со списком файлов — бесплатная (лимит не тратит).
        self.listing_calls = 0
        # Свой набор раздач и файлов (для сценария «пачка на несколько сезонов»).
        self.custom_rows: list[dict] | None = None
        self.custom_files: dict[str, int] | None = None

    async def search_pages(self, query: str, pages: int = 2) -> list[dict]:
        if self.custom_rows is not None:
            return self.custom_rows
        return [tracker_row(self.episodes)]

    async def file_list(self, torrent_id: str) -> dict:
        self.listing_calls += 1
        names = self.names_for(torrent_id)
        return {
            "success": True,
            "files": [
                {"index": index, "name": name, "size": 1_900_000_000}
                for index, name in enumerate(names)
            ],
        }

    def names_for(self, torrent_id: str) -> list[str]:
        if self.custom_files and torrent_id in self.custom_files:
            season = 2 if torrent_id == "700" else 2
            return [
                f"Cliff.S{season:02d}E{number:02d}.mkv"
                for number in range(1, self.custom_files[torrent_id] + 1)
            ]
        return [
            f"0{number}. Звёзды в джунглях.S03.2026.WEB-DLRip.avi"
            for number in range(1, self.episodes + 1)
        ]

    async def download(self, torrent_id: str, path: Path) -> dict:
        self.downloads += 1
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        write_torrent_named(Path(path), self.names_for(torrent_id))
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
        return {
            "hash": torrent_hash,
            "name": self.torrent_name,
            "download_dir": str(self.download_dir),
            "files": [],
        }

    async def remove(self, torrent_hash, *, delete_data=False):  # noqa: D102
        return {"ok": True}

    torrent_name = "Звёзды в джунглях.S03"


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
    import json as _json
    episodes = _json.loads(rows[0]["episodes"]) if rows else []
    print("  запись о скачанном:", len(rows), "| серии:", [e["episode"] for e in episodes])
    check("одна запись на раздачу", len(rows) == 1, len(rows))
    check("в память записана серия 3", [e["episode"] for e in episodes] == [3],
          [e["episode"] for e in episodes])

    # Скачивание живёт отдельной разовой задачей: иначе прогресса не видно.
    children = [
        item for item in await agent.store.list_tasks(limit=50)
        if (item.params or {}).get("source_periodic") == task.id
    ]
    print("  задача-потомок:", [(item.id[:6], item.state, item.title) for item in children])
    check("периодическая завела задачу-потомка", len(children) == 1,
          [(item.id, item.state) for item in children])
    child = children[0] if children else None
    check("потомок качает, а не мониторит", bool(child) and child.state == "downloading",
          child.state if child else None)
    check("у потомка есть хеш для прогресса",
          bool(child) and bool((child.params or {}).get("transmission_hash")),
          (child.params or {}).get("transmission_hash") if child else None)
    check("в названии потомка видно серию",
          bool(child) and "серия 3" in child.title, child.title if child else None)
    check("мониторинг остался мониторингом",
          task.state == "monitoring" and not (task.params or {}).get("transmission_hash"),
          (task.state, (task.params or {}).get("transmission_hash")))
    # Та самая недоработка: после скачивания третьей ждём уже четвёртую.
    check("сразу после скачивания ждём 4-ю серию",
          task.params.get("next_episode") == 4, task.params.get("next_episode"))
    chat = await agent.store.history_for_task(task.id)
    notice = next((row["text"] for row in reversed(chat) if "поставил качать" in (row["text"] or "")), "")
    check("в уведомлении сказано про отдельную задачу",
          "отдельная задача" in notice, notice[:120])

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
    episodes = _json.loads(rows[0]["episodes"]) if rows else []
    print("  записей:", len(rows), "| серии:", [e["episode"] for e in episodes])
    check("по-прежнему одна запись на раздачу", len(rows) == 1, len(rows))
    check("в памяти серии 3 и 4", sorted(e["episode"] for e in episodes) == [3, 4],
          [e["episode"] for e in episodes])
    agent.store.close()


async def test_delete_keeps_progress() -> None:
    print("\n== посмотрел серию и удалил файл — прогресс не откатывается ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None

    mcp.episodes = 3
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    check("3-я серия скачана", transmission.added and transmission.added[-1]["files"] == [2],
          transmission.added[-1:] or None)

    # Кладём на диск «скачанное», чтобы удалению было что стирать.
    folder = agent.settings.transmission_download_dir / transmission.torrent_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "03. Звёзды в джунглях.S03.avi").write_bytes(b"x" * 1024)

    # Ключ сериала берём из самой записи — он вычисляется из названия раздачи.
    rows = await agent.store.list_downloads()
    series_key = rows[0]["series_key"] if rows else ""
    before = await agent.store.episodes_by_season(series_key)
    print("  ключ сериала:", series_key, "| прогресс до удаления:", before)
    await agent.perform_delete("2156284")

    check("файлов на диске больше нет", not folder.exists())
    check("в списке «скачано» пусто", not await agent.store.list_downloads())
    after = await agent.store.episodes_by_season(series_key)
    print("  прогресс после удаления:", after)
    check("прогресс сохранился (3-я серия помнится)", after.get(3) == 3, after)

    # Трекер всё ещё показывает 1-3: агент не должен качать третью заново.
    added_before = len(transmission.added)
    await agent.check_task(task)
    check("третья серия повторно не скачивается", len(transmission.added) == added_before,
          transmission.added[added_before:])

    # Появилась четвёртая — качаем именно её.
    mcp.episodes = 4
    await agent.check_task(task)
    print("  новые постановки:", transmission.added[added_before:])
    check("следующая скачана — 4-я серия",
          transmission.added[-1]["files"] == [3], transmission.added[-1])
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


async def test_failed_task_keeps_checking() -> None:
    print("\n== задача со сбоем остаётся в расписании ==")
    tmp = Path(tempfile.mkdtemp(prefix="periodic-due-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "due.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    from core.models import Task

    failed = Task(
        id="failed-periodic", kind="periodic", state="failed", session_id="checks",
        user_query="Звезды в джунглях", params={"title": "Звезды в джунглях"},
        interval_minutes=720, auto_download=True,
        next_check_at="2020-01-01T00:00:00+00:00",
    )
    await agent.store.save_task(failed)
    due = await agent.store.due_periodic()
    check("упавшая периодическая попадает в расписание",
          any(task.id == "failed-periodic" for task in due), [task.id for task in due])

    # Проверка возвращает её в работу, даже если трекер снова отвечает ошибкой.
    class BrokenSearch:
        async def search_pages(self, query: str, pages: int = 1):
            from core.mcp_client import MCPError

            raise MCPError("трекер молчит")

    agent.mcp = BrokenSearch()  # type: ignore[assignment]
    await agent.check_task(failed)
    check("после проверки состояние снова «в работе»", failed.state == "monitoring", failed.state)
    check("и следующая проверка назначена", bool(failed.next_check_at), failed.next_check_at)
    agent.store.close()


async def test_nothing_at_all() -> None:
    print("\n== сериал ещё не вышел: ничего нет, но задача живёт ==")
    tmp = Path(tempfile.mkdtemp(prefix="periodic-empty-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "empty.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)

    class EmptyMCP:
        async def search_pages(self, query: str, pages: int = 1) -> list[dict]:
            return []

        async def download(self, torrent_id: str, path: Path) -> dict:  # noqa: ARG002
            return {"success": False, "message": "пусто"}

    agent.mcp = EmptyMCP()  # type: ignore[assignment]
    agent.transmission = SimpleNamespace(  # type: ignore[assignment]
        storage_info=lambda: SimpleNamespace(ok=False, error="нет диска", to_dict=lambda: {}),
    )

    from core.models import Turn

    turn = Turn()
    await agent._start_periodic(  # noqa: SLF001
        "checks",
        "Новый сериал 2027",
        {"title": "Новый сериал 2027", "interval_minutes": 60, "auto_download": True},
        turn,
    )
    task = turn.task
    check("задача создана", task is not None, task)
    text = " ".join(message.text for message in turn.messages)
    check("сказано, что на трекере пусто", "ничего нет" in text, text[:200])
    check("задача ждёт выбора режима", task.state == "awaiting_confirm", task.state)
    labels = [action.label for action in turn.messages[-1].actions]
    check("режим спрашивается не сразу, а в резюме",
          "Качать сразу и сообщать" not in labels, labels)
    check("есть кнопка «следить строго» с условиями",
          any(label.startswith("Следить строго") for label in labels), labels)
    summary = await agent.handle_action(
        "checks", "periodic_pick", {"task_id": task.id, "variant": "strict"}
    )
    summary_labels = [action.label for action in summary.messages[-1].actions]
    print("  резюме:", summary.messages[-1].text.replace("\n", " | ")[:160])
    check("в резюме можно включить автокачку",
          "Качать сразу и сообщать" in summary_labels, summary_labels)
    check("в резюме есть «Только сообщать» и «Отмена»",
          "Только сообщать" in summary_labels and "Отмена" in summary_labels, summary_labels)

    # подтверждаем автокачку — задача уходит в мониторинг
    confirm = await agent.handle_action(
        "checks", "periodic_auto", {"task_id": task.id}
    )
    task = await agent.store.get_task(task.id)
    check("задача в работе", task.state == "monitoring", task.state)
    check("следующая проверка назначена", bool(task.next_check_at), task.next_check_at)
    check("ответ про режим", any("Взял в работу" in m.text for m in confirm.messages),
          [m.text[:40] for m in confirm.messages])

    # плановая проверка при пустой выдаче: без спама, но задача жива
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    check("после проверки всё ещё в работе", task.state == "monitoring", task.state)
    messages_now = await agent.history_for_task(task.id)
    check("повторно «ничего нет» не пришло (не чаще раза в сутки)",
          sum(1 for m in messages_now if "пока ничего нет" in m["text"]) == 0,
          [m["text"][:40] for m in messages_now])

    # через сутки — сообщаем, что продолжаем следить
    from datetime import datetime, timedelta, timezone
    import sqlite3

    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(settings.db_path))
    conn.execute("UPDATE tasks SET notify_last = ? WHERE id = ?", (old, task.id))
    conn.commit()
    conn.close()
    task = await agent.store.get_task(task.id)  # перечитываем метку из базы
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    messages_now = await agent.history_for_task(task.id)
    notices = [m["text"] for m in messages_now if "пока ничего нет" in m["text"]]
    check("раз в сутки сказано, что ничего нет", len(notices) == 1, notices)
    check("и задача продолжает работать", task.state == "monitoring", task.state)
    agent.store.close()


async def test_pending_retry() -> None:
    print("\n== трекер не отдал .torrent: задача повторяет сама ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None

    # Трекер «кончил лимит»: .torrent не отдаётся.
    original_download = mcp.download

    async def limited(torrent_id: str, path: Path) -> dict:
        return {
            "success": False,
            "message": "Вам недоступен торрент-файл. Вы использовали количество в сутки.",
        }

    mcp.download = limited  # type: ignore[assignment]
    mcp.episodes = 3
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    pending = (task.params or {}).get("pending_download") or {}
    print("  отложено:", str((pending.get("torrent") or {}).get("title", ""))[:60])
    check("отложенная раздача запомнена", bool(pending.get("torrent")), pending)
    check("причина ошибки сохранена", "сутки" in (task.last_error or ""), task.last_error)
    check("задача осталась в работе", task.state == "monitoring", task.state)
    check("в Transmission ничего не поставили", not transmission.added, transmission.added)
    watch = agent._watch_text(task)  # noqa: SLF001
    print("  карточка задачи:", watch)
    check("в карточке видно, что скачивание ждёт повтора", "ждёт повтора" in watch, watch)

    # Ручная проверка при живом лимите: честно говорит, что повтор не удался.
    turn = await agent.handle_action("s1", "check_now", {"task_id": task.id})
    text = " ".join(m.text for m in turn.messages)
    print("  ответ на «Проверить сейчас»:", text.replace("\n", " | ")[:150])
    check("ручная проверка не врёт про успех", "скачал" not in text.lower(), text[:200])

    # Лимит обновился: следующий заход скачивает сам, хотя новых серий нет.
    mcp.download = original_download  # type: ignore[assignment]
    task = await agent.store.get_task(task.id)
    assert task is not None
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  постановки после сброса лимита:", [a["files"] for a in transmission.added])
    check("скачалось после сброса лимита", bool(transmission.added), transmission.added)
    check("выбрана нужная серия (3-я)",
          bool(transmission.added) and transmission.added[-1]["files"] == [2],
          transmission.added[-1:] or None)
    check("отложенное снято", not (task.params or {}).get("pending_download"),
          task.params.get("pending_download"))
    check("ошибка задачи очищена", not (task.last_error or ""), task.last_error)

    # И «Проверить сейчас» теперь рассказывает, чего ждём.
    turn = await agent.handle_action("s1", "check_now", {"task_id": task.id})
    text = " ".join(m.text for m in turn.messages)
    print("  ответ после:", text.replace("\n", " | ")[:180])
    check("в ответе видно, какую серию ждём", "жду серию" in text, text[:200])
    check("в ответе видно время следующей проверки", "следующая проверка" in text, text[:200])
    agent.store.close()


async def test_listing_before_torrent() -> None:
    print("\n== серии проверяем со страницы трекера, .torrent — только по делу ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None

    # На трекере 1-2 выпуска, нужен 3-й: .torrent качать незачем.
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  .torrent-файлов скачано:", mcp.downloads, "| страниц раздачи:", mcp.listing_calls)
    check("лимит не тратится, когда новой серии нет", mcp.downloads == 0, mcp.downloads)
    check("и страницу раздачи не спрашивали — нового нет", mcp.listing_calls == 0,
          mcp.listing_calls)
    check("в Transmission пусто", not transmission.added, transmission.added)

    # Появился 3-й — теперь .torrent нужен, и он скачивается ровно один раз.
    mcp.episodes = 3
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  .torrent-файлов скачано:", mcp.downloads, "| страниц раздачи:", mcp.listing_calls)
    check("состав раздачи спросили у страницы", mcp.listing_calls >= 1, mcp.listing_calls)
    check("торрент-файл скачан, когда серия появилась", mcp.downloads == 1, mcp.downloads)
    check("скачана именно 3-я серия",
          bool(transmission.added) and transmission.added[-1]["files"] == [2],
          transmission.added[-1:] or None)
    check("ошибки после успеха нет", not (task.last_error or ""), task.last_error)
    agent.store.close()


async def test_pack_needs_choice() -> None:
    print("\n== пачка без выбранной серии: спрашиваем, а не качаем всё ==")
    agent, mcp, transmission = build_agent()
    pack_row = {
        "id": "700",
        "title": "Приключения Клиффа Бута (1-2 сезоны: 1-20 серии из 20) / 2026 / ДБ / WEB-DL (1080p)",
        "size": "120 ГБ", "seeds": 30, "peers": 3, "comments": 1,
        "uploadDate": "15.10.2026 в 10:00", "uploader": "x", "category": "46",
    }
    single_row = {
        "id": "701",
        "title": "Приключения Клиффа Бута (2 сезон: 5 выпуск из 20) / 2026 / ПМ / WEB-DL (1080p)",
        "size": "5 ГБ", "seeds": 12, "peers": 2, "comments": 0,
        "uploadDate": "18.10.2026 в 21:00", "uploader": "x", "category": "46",
    }
    mcp.custom_rows = [pack_row, single_row]
    mcp.custom_files = {"700": 20, "701": 1}

    # Задача как у пользователя: только название, «качать сразу», без серии и качества.
    task = await agent._task_from_params(  # noqa: SLF001
        "s1", "Приключения Клиффа Бута, проверяй раз в сутки и скачивай если есть",
        {"title": "Приключения Клиффа Бута"}, kind="periodic", interval=1440,
        auto_download=True,
    )
    task.state = "monitoring"
    await agent.store.save_task(task)

    await agent.check_task(task)
    added = [item["files"] for item in transmission.added]
    print("  постановки:", added)
    check("скачалась одна серия, а не пачка", added == [None], added)
    check("в очередь ушла одиночная серия", mcp.downloads <= 2, mcp.downloads)

    # Периодическая задача сама не качает: она заводит разовую задачу-потомка,
    # и весь диалог про пачку живёт уже в ней (она же и будет качать).
    children = [
        item for item in await agent.store.list_tasks(limit=50)
        if (item.params or {}).get("source_periodic") == task.id
    ]
    waiting = [item for item in children if item.state == "awaiting_choice"]
    print("  задачи-потомки:", [(item.id[:6], item.state) for item in children])
    check("периодическая завела задачу-потомка", bool(children),
          [(item.id, item.state) for item in children])
    check("одна из них ждёт выбора серий", len(waiting) == 1,
          [item.state for item in children])
    child = waiting[0] if waiting else None

    rows = await agent.store.history_for_task(child.id) if child else []
    notices = [
        row for row in rows
        if row["role"] == "agent"
        and ("раздача с сериями" in (row["text"] or "") or "пачка серий" in (row["text"] or ""))
    ]
    print("  уведомление:", (notices[0]["text"].splitlines()[0] if notices else "нет"))
    check("пришло уведомление про пачку", bool(notices), [r["text"][:60] for r in rows])
    labels = [action["label"] for action in (notices[0]["actions"] if notices else [])]
    check("есть кнопки «Скачать всю пачку» и «Выбрать серии»",
          "Скачать всю пачку" in labels and "Выбрать серии" in labels, labels)
    targets = [
        action["payload"].get("task_id")
        for action in (notices[0]["actions"] if notices else [])
        if action["payload"].get("task_id")
    ]
    check("кнопки ведут в задачу-потомка",
          bool(child) and bool(targets) and all(target == child.id for target in targets),
          targets)

    # Второй раз про ту же пачку не спрашиваем и пустых потомков не плодим.
    await agent.check_task(await agent.store.get_task(task.id))
    children_after = [
        item for item in await agent.store.list_tasks(limit=50)
        if (item.params or {}).get("source_periodic") == task.id
    ]
    waiting_after = [item for item in children_after if item.state == "awaiting_choice"]
    check("уведомление не дублируется", len(waiting_after) == 1,
          [(item.id[:6], item.state) for item in children_after])

    # Кнопка «Выбрать серии» показывает тот же выбор серий, что и в разовых задачах.
    task = await agent.store.get_task(task.id)
    check("мониторинг остался мониторингом", task.state == "monitoring", task.state)
    child = await agent.store.get_task(child.id)
    groups = (child.options or {}).get("episode_groups") or []
    check("серии пачки сохранены для выбора", len(groups) == 20, len(groups))
    turn = await agent.handle_action("s1", "choose_episodes", {"task_id": child.id})
    labels = [action.label for message in turn.messages for action in message.actions
              if action.kind == "toggle_episode"]
    print("  кнопки после «Выбрать серии»:", labels[:3], "… всего", len(labels))
    check("выбор серий открылся", len(labels) == 20, len(labels))

    # «Скачать всю пачку» — карточка на всю пачку, а не на одну серию.
    pack_action = next(
        (action for action in (notices[0]["actions"] if notices else [])
         if action["label"] == "Скачать всю пачку"),
        None,
    )
    check("кнопка «Скачать всю пачку» есть", pack_action is not None,
          [action["label"] for action in (notices[0]["actions"] if notices else [])])
    if pack_action is not None:
        turn = await agent.handle_action("s1", "download_all", pack_action["payload"])
        plan = (await agent.store.get_task(child.id)).options.get("plan") or {}
        print("  «вся пачка»:", {"серий": len(plan.get("selected_episodes") or []),
                                 "файлов": len(plan.get("wanted") or [])})
        check("«вся пачка» — все серии раздачи",
              len(plan.get("selected_episodes") or []) == 20,
              plan.get("selected_episodes"))
        check("и в карточке есть кнопка подтверждения",
              any(action.kind == "start_download"
                  for message in turn.messages for action in message.actions),
              [action.kind for message in turn.messages for action in message.actions])
    agent.store.close()


async def test_notify_buttons_stay_alive() -> None:
    """«Только сообщать»: кнопка «Скачать» в уведомлении должна работать.

    Настоящий случай: задача нашла раздачу, но при нажатии «Проверить сейчас»
    уведомление создавалось в этом же ходу и тут же помечалось отвеченным —
    кнопка была неактивной с рождения. Плюс по нажатию должно заводиться
    скачивание отдельной задачей, как и при автоскачивании.
    """
    print("\n== уведомление «только сообщать»: кнопка «Скачать» живая ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=False)
    assert task is not None
    mcp.episodes = 3
    await agent.handle_action("s1", "check_now", {"task_id": task.id})
    rows = await agent.store.history_for_task(task.id)
    fresh = [
        row for row in rows
        if row.get("actions")
        and any(action["kind"] == "download_new" for action in row["actions"])
    ]
    print("  уведомлений с кнопкой «Скачать»:", len(fresh))
    check("кнопка «Скачать» в уведомлении есть", bool(fresh),
          [str(r["text"])[:60] for r in rows])
    if fresh:
        payload = fresh[-1].get("payload") or {}
        check("кнопки уведомления активны", payload.get("resolved") is not True,
              payload.get("resolved"))
        torrent_id = next(
            action["payload"]["torrent_id"]
            for action in fresh[-1]["actions"]
            if action["kind"] == "download_new"
        )
        before = len(transmission.added)
        await agent.handle_action(
            "s1", "download_new", {"task_id": task.id, "torrent_id": torrent_id}
        )
        check("по кнопке раздача встала в очередь", len(transmission.added) > before,
              transmission.added[before:])
        children = [
            item for item in await agent.store.list_tasks(limit=50)
            if (item.params or {}).get("source_periodic") == task.id
        ]
        print("  задача-потомок:", [(item.id[:6], item.state) for item in children])
        check("качает отдельная задача", len(children) == 1,
              [(item.id, item.state) for item in children])
    agent.store.close()



async def test_next_episode_not_rolled_back() -> None:
    """Пустая выдача не откатывает «какую серию ждём».

    Настоящий случай: скачали 3-ю серию, трекер на следующей проверке ничего не
    вернул — и номер откатился к «начиная с 3», задача снова ждала 3-ю.
    """
    print("\n== трекер молчит: номер серии не откатывается ==")
    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None
    mcp.episodes = 3
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  после скачивания 3-й:", task.params.get("next_episode"))
    check("ждём 4-ю", task.params.get("next_episode") == 4, task.params.get("next_episode"))

    # Трекер перестал отвечать раздачами (Cloudflare, пустая выдача).
    mcp.episodes = 0
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  после пустой проверки:", task.params.get("next_episode"))
    check("номер не откатился назад", task.params.get("next_episode") == 4,
          task.params.get("next_episode"))
    check("ключ сериала запомнен", bool(task.params.get("series_key")),
          task.params.get("series_key"))
    agent.store.close()


async def test_no_duplicate_periodic() -> None:
    """Вторая задача на тот же сериал не заводится: правки идут в существующую."""
    print("\n== дубль периодической задачи не создаётся ==")
    agent, mcp, transmission = build_agent()
    first = await create_task(agent, auto=True)
    assert first is not None
    before = [t for t in await agent.store.list_tasks(limit=100) if t.kind == "periodic"]
    check("первая задача есть", len(before) == 1, [t.id for t in before])

    # Второй раз просим следить за тем же сериалом.
    turn = await agent.handle_message(
        first.session_id, "Звёзды в джунглях", "periodic"
    )
    after = [t for t in await agent.store.list_tasks(limit=100) if t.kind == "periodic"]
    print("  периодических задач:", len(after))
    check("вторая не завелась", len(after) == 1, [t.id for t in after])
    check("правка ушла в существующую",
          turn.task is not None and turn.task.id == first.id,
          turn.task.id if turn.task else None)
    text = " ".join(m.text for m in turn.messages).lower()
    check("сказано, что вторая задача не нужна", "вторую не завожу" in text, text[:160])
    agent.store.close()



async def test_refine_periodic() -> None:
    print("\n== уточнение внутри периодической задачи ==")
    from core.models import Turn

    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None
    # Проверка до уточнения: у задачи появляется «какую серию ждём».
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    before_state = task.state
    expected_episode = task.params.get("next_episode")
    check("задача в работе до уточнения", before_state == "monitoring", before_state)
    check("до уточнения известно, какую серию ждём", expected_episode == 3, expected_episode)

    # «только 4к» — уточнение, а не новый разовый поиск.
    turn = Turn()
    await agent._refine_task(task, {"quality": "2160p"}, "только 4к", turn)  # noqa: SLF001
    task = await agent.store.get_task(task.id)
    assert task is not None
    text = " ".join(message.text for message in turn.messages)
    print("  ответ:", text.replace("\n", " | ")[:190])
    print("  состояние:", task.state, "| качество:", task.params.get("quality"))
    check("задача осталась в работе", task.state == "monitoring", task.state)
    check("качество обновилось", task.params.get("quality") == "2160p", task.params.get("quality"))
    check("не ушла в «не найдено»", "Сделать периодической" not in text, text[:200])
    check("сказано, что задача продолжает следить", "продолжает следить" in text, text[:200])
    check("показано, что ждём", "Ждём" in text, text[:200])
    check("есть кнопка «Проверить сейчас»",
          any(action.label == "Проверить сейчас" for action in turn.messages[-1].actions),
          [action.label for action in turn.messages[-1].actions])
    check("следующая проверка не потерялась", bool(task.next_check_at), task.next_check_at)
    check("сезон и номер серии в задаче не сбились",
          (task.params.get("season") == 3 and task.params.get("next_episode") == expected_episode),
          (task.params.get("season"), task.params.get("next_episode")))

    # Смена названия внутри задачи: базовая линия старого сериала не мешает.
    turn = Turn()
    await agent._refine_task(task, {"title": "Другой сериал"}, "нет, это другой сериал", turn)  # noqa: SLF001
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  после смены названия:", task.params.get("title"),
          "| baseline:", task.params.get("baseline_episodes"),
          "| next_episode:", task.params.get("next_episode"))
    check("название обновилось", task.params.get("title") == "Другой сериал", task.params.get("title"))
    check("базовая линия старого сериала сброшена",
          not task.params.get("baseline_episodes") and not task.params.get("next_episode"),
          (task.params.get("baseline_episodes"), task.params.get("next_episode")))
    agent.store.close()


async def test_strict_conditions() -> None:
    """«Прайм-тайм 2026 в 4K» при наличии только 2024 года: ничего не качаем."""
    print("\n== периодическая задача не хватает чужое ==")
    from core.models import Turn

    agent, mcp, transmission = build_agent()
    mcp.custom_rows = [
        {"id": "2085056",
         "title": "Злодейка прайм-тайма / A Vilã das Nove / 2024 / ЛМ / WEB-DLRip (AVC)",
         "size": "1.95 ГБ", "seeds": 3, "peers": 0, "comments": 0,
         "uploadDate": "09.04.2025 в 07:36", "uploader": "R", "category": "17"},
        {"id": "1896787", "title": "Прайм-тайм / Prime Time / 2024 / ЛМ / WEB-DL (1080p)",
         "size": "2.4 ГБ", "seeds": 5, "peers": 1, "comments": 2,
         "uploadDate": "10.05.2025 в 10:00", "uploader": "x", "category": "17"},
    ]
    mcp.custom_files = {"2085056": 1, "1896787": 1}
    tasks_before = len(await agent.store.list_tasks(limit=100))

    turn = Turn()
    await agent._start_periodic(  # noqa: SLF001
        "s1", "Прайм-тайм 2026 скачай как появится 4к",
        {"title": "Прайм-тайм", "year": 2026, "quality": "2160p",
         "interval_minutes": 1440, "auto_download": True},
        turn,
    )
    task = turn.task
    message = turn.messages[-1]
    text = message.text
    print("  карточка:", text.replace("\n", " | ")[:220])
    labels = [action.label for action in message.actions]
    print("  кнопки:", labels)

    check("сказано, что под условия ничего нет", "ничего нет" in text, text[:200])
    check("условия названы честно", "2026" in text and "2160p" in text, text[:200])
    check("чужой фильм не показан как найденный", "Сейчас подходит" not in text, text[:120])
    check("кандидата на скачивание нет", not task.candidate, task.candidate)
    check("базовая линия пустая", not task.params.get("baseline_episodes"),
          task.params.get("baseline_episodes"))
    check("есть кнопка «следить строго» с условиями",
          any(label.startswith("Следить строго") and "2026" in label for label in labels),
          labels)
    check("на каждый похожий вариант есть кнопка",
          sum(1 for label in labels if label.startswith("Следить:")) >= 1, labels)
    check("к похожим вариантам есть «Подробнее»",
          labels.count("Подробнее") >= 1, labels)
    check("режим не спрашивается на шаге выбора",
          "Качать сразу и сообщать" not in labels, labels)
    check("задача не создала новых задач",
          len(await agent.store.list_tasks(limit=100)) == tasks_before + 1,
          (tasks_before, len(await agent.store.list_tasks(limit=100))))

    # Подтверждаем режим: ничего не скачивается, задача просто встаёт в работу.
    await agent.handle_action("s1", "periodic_pick", {"task_id": task.id, "variant": "strict"})
    await agent.handle_action("s1", "periodic_auto", {"task_id": task.id})
    task = await agent.store.get_task(task.id)
    print("  после подтверждения:", task.state, "| постановок:", transmission.added)
    check("после подтверждения ничего не качается", not transmission.added, transmission.added)
    check("задача в работе", task.state == "monitoring", task.state)
    check("подтверждение не создало задачу",
          len(await agent.store.list_tasks(limit=100)) == tasks_before + 1)

    # Проверка по расписанию тоже ничего не тянет: условия не выполнены.
    await agent.check_task(task)
    check("плановая проверка ничего не качает", not transmission.added, transmission.added)
    check("и не тратит торрент-файлы", mcp.downloads == 0, mcp.downloads)

    # Пользователь сам выбирает похожий вариант кнопкой — тогда раздача видна.
    near_action = next(
        action for action in message.actions if action.kind == "periodic_pick"
        and str(action.payload.get("variant", "")).startswith("near:")
    )
    relaxed = await agent.handle_action(
        "s1", "periodic_pick",
        {"task_id": task.id, "variant": near_action.payload["variant"]},
    )
    relaxed_text = " ".join(item.text for item in relaxed.messages)
    print("  после выбора варианта:", relaxed_text.replace("\n", " | ")[:200])
    check("после выбора варианта раздача видна", "Сейчас подходит" in relaxed_text,
          relaxed_text[:200])
    task = await agent.store.get_task(task.id)
    print("  условия варианта:", {k: task.params.get(k) for k in ("year", "quality")})
    check("условия взяты из выбранной раздачи",
          str(task.params.get("year")) == "2024", task.params.get("year"))
    check("выбор варианта не создал задачу",
          len(await agent.store.list_tasks(limit=100)) == tasks_before + 1)
    agent.store.close()


async def test_make_periodic_in_place() -> None:
    print("\n== «сделать периодической» не плодит задачи ==")
    agent, mcp, transmission = build_agent()
    mcp.custom_rows = [
        {"id": "900", "title": "Тихий омут / 2026 / ДБ / WEB-DL (1080p)", "size": "3 ГБ",
         "seeds": 5, "peers": 0, "comments": 0, "uploadDate": "18.10.2026 в 10:00",
         "uploader": "x", "category": "17"},
    ]
    mcp.custom_files = {"900": 1}
    from core.models import Turn

    turn = Turn()
    await agent._start_oneshot(  # noqa: SLF001
        "s2", "Тихий омут", {"title": "Тихий омут"}, turn,
    )
    task = turn.task
    oneshot_id = task.id
    count_before = len(await agent.store.list_tasks(limit=100))
    print("  разовая задача:", oneshot_id, "| задач в базе:", count_before)

    follow = await agent.handle_action(
        "s2", "make_periodic", {"task_id": oneshot_id, "interval_minutes": 60}
    )
    same = await agent.store.get_task(oneshot_id)
    print("  после кнопки: kind =", same.kind, "| id тот же:", same.id == oneshot_id,
          "| задач:", len(await agent.store.list_tasks(limit=100)))
    check("задача стала периодической на месте", same.kind == "periodic", same.kind)
    check("id не изменился", same.id == oneshot_id, same.id)
    check("новых задач в базе не появилось",
          len(await agent.store.list_tasks(limit=100)) == count_before,
          (count_before, len(await agent.store.list_tasks(limit=100))))
    check("показана карточка периодической задачи",
          any("проверять" in message.text for message in follow.messages),
          [message.text[:60] for message in follow.messages])
    agent.store.close()


async def test_refine_settings() -> None:
    """«Проверяй раз в сутки» и «только сообщай» меняют задачу, а не путают."""
    print("\n== уточнение расписания и режима словами ==")
    from core.models import Turn

    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None
    check("интервал из запроса", task.interval_minutes == 60, task.interval_minutes)

    turn = Turn()
    await agent._refine_task(task, {"interval_minutes": 1440}, "проверяй раз в сутки", turn)  # noqa: SLF001
    task = await agent.store.get_task(task.id)
    assert task is not None
    text = " ".join(message.text for message in turn.messages)
    print("  ответ:", text.replace("\n", " | ")[:160])
    print("  интервал:", task.interval_minutes, "| next:", task.next_check_at)
    check("интервал обновился", task.interval_minutes == 1440, task.interval_minutes)
    check("задача осталась в работе", task.state == "monitoring", task.state)
    check("сказано, что именно изменилось", "раз в сутки" in text, text[:160])
    check("расписание пересчитано", bool(task.next_check_at), task.next_check_at)

    # Режим: «только сообщай»
    turn = Turn()
    await agent._refine_task(task, {"auto_download": False}, "только сообщай", turn)  # noqa: SLF001
    task = await agent.store.get_task(task.id)
    assert task is not None
    text = " ".join(message.text for message in turn.messages)
    print("  ответ:", text.replace("\n", " | ")[:160])
    check("режим стал «только сообщать»", task.auto_download is False, task.auto_download)
    check("сказано про режим", "только сообщаю" in text or "сообщать" in text, text[:160])

    # Ничего не изменилось — честное «уже так и настроено», без новых задач.
    count = len(await agent.store.list_tasks(limit=100))
    turn = Turn()
    await agent._refine_task(task, {"interval_minutes": 1440}, "проверяй раз в сутки", turn)  # noqa: SLF001
    text = " ".join(message.text for message in turn.messages)
    print("  ответ:", text.replace("\n", " | ")[:120])
    check("повтор ничего не ломает", "настроено" in text, text[:120])
    check("уточнения не создали задач",
          len(await agent.store.list_tasks(limit=100)) == count, count)
    agent.store.close()


async def test_cancel_only_in_panel() -> None:
    """Отмена уже следящей задачи — только в карточке «В работе», не в чате."""
    print("\n== отмена: только в карточке, не в чате ==")
    from core.models import Turn

    agent, mcp, transmission = build_agent()
    task = await create_task(agent, auto=True)
    assert task is not None
    task = await agent.store.get_task(task.id)
    assert task is not None
    check("задача следит", task.state == "monitoring", task.state)

    labels = [action.label for action in agent._task_actions(task)]  # noqa: SLF001
    print("  кнопки под сообщением в monitoring:", labels)
    check("в чате есть «Проверить сейчас»", "Проверить сейчас" in labels, labels)
    check("в чате нет «Отменить задачу»", "Отменить задачу" not in labels, labels)

    # То же после уточнения задачи — отмена тоже не появляется.
    turn = Turn()
    await agent._refine_task(task, {"interval_minutes": 1440}, "проверяй раз в сутки", turn)  # noqa: SLF001
    labels = [action.label for action in turn.messages[-1].actions]
    print("  кнопки в ответе на уточнение:", labels)
    check("после уточнения отмены в чате нет", "Отменить задачу" not in labels, labels)

    # Пока задача не в работе, кнопка отмены в чате нужна: карточки может не быть под рукой.
    task.state = "awaiting_confirm"
    labels = [action.label for action in agent._task_actions(task)]  # noqa: SLF001
    print("  кнопки до старта:", labels)
    check("до старта «Отменить задачу» остаётся", "Отменить задачу" in labels, labels)
    agent.store.close()


async def test_default_interval() -> None:
    """Если период не назван — проверка раз в сутки."""
    print("\n== период по умолчанию ==")
    from core.models import Turn

    agent, mcp, transmission = build_agent()
    turn = Turn()
    await agent._start_periodic(  # noqa: SLF001
        "s1", "Сериал без названного периода",
        {"title": "Сериал без названного периода"},  # interval_minutes нет
        turn,
    )
    task = turn.task
    print("  интервал:", task.interval_minutes, "мин")
    check("по умолчанию — сутки", task.interval_minutes == 1440, task.interval_minutes)
    text = turn.messages[-1].text
    check("в карточке написано «раз в сутки»", "раз в сутки" in text, text[:120])
    check("это значение из настроек",
          task.interval_minutes == agent.settings.default_interval_minutes,
          (task.interval_minutes, agent.settings.default_interval_minutes))
    agent.store.close()


async def test_first_finding_is_not_news() -> None:
    """У задачи, созданной без находок, первая находка — не новинка.

    Настоящий случай: «Сэйлор Мун» создана, когда по названию ничего не
    нашлось (транслит), базовая линия пустая. После исправления поиска по
    алиасам проверка нашла 7 старых раздач — и они не должны прийти как
    «нашёл новое».
    """
    print("\n== первая находка после исправления поиска ==")
    import sqlite3  # noqa: F401  (для наглядности: сценарий про базу)

    agent, mcp, transmission = build_agent()
    mcp.custom_rows = [tracker_row(2)]
    from core.models import Task

    task = Task(
        id="first-finding", kind="periodic", state="monitoring", session_id="checks",
        user_query="Сэйлор Мун", interval_minutes=720, auto_download=True,
        params={
            "title": "Звёзды в джунглях",
            "type": "series",
            "seen_ids": [],
            # базовая линия снималась при создании, но нашлось тогда ноль
            "baseline_at": "2026-10-28T07:00:00+00:00",
        },
    )
    await agent.store.save_task(task)
    await agent.check_task(task)
    rows = await agent.store.history_for_task(task.id)
    notices = [row for row in rows if row["role"] == "agent"]
    print("  сообщений:", len(notices), "| постановок в очередь:", len(transmission.added))
    check("новинкой не объявлено", not transmission.added, transmission.added)
    check("и уведомления о новом нет", not notices, [r["text"][:60] for r in notices])
    stored = await agent.store.get_task(task.id)
    print("  seen_ids:", len((stored.params or {}).get("seen_ids") or []))
    check("раздача запомнена как уже виденная",
          str(tracker_row(2)["id"]) in ((stored.params or {}).get("seen_ids") or []),
          (stored.params or {}).get("seen_ids"))

    # Следующая находка — уже настоящая новость: и на странице, и в .torrent
    # серий стало больше.
    mcp.custom_rows = [tracker_row(3, upload_date="28.10.2026 в 12:00")]
    mcp.episodes = 3
    await agent.check_task(await agent.store.get_task(task.id))
    check("следующая серия считается новинкой", bool(transmission.added), transmission.added)
    agent.store.close()


async def test_check_searches_aliases() -> None:
    """Проверка периодической задачи ищет так же, как разовый поиск.

    Настоящий случай: задача «Сэйлор Мун» создана по подсказке («Красавица-воин
    Сейлор Мун»), но трекер не индексирует транслит. Разовый поиск находил
    раздачи по другим названиям, а проверка задачи искала только по title —
    и каждый раз сообщала «ничего нет».
    """
    print("\n== проверка задачи ищет по другим названиям ==")
    tmp = Path(tempfile.mkdtemp(prefix="periodic-alias-"))
    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "alias.sqlite3",
        torrents_dir=tmp / "torrent-files",
        transmission_download_dir=tmp / "torrents",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)

    row = {
        "id": "242338",
        "title": "Красавица-воин Сейлор Мун (1-5 сезонов из 5) / 1992-1997 / ПМ, СТ / DVDRip",
        "size": "62.83 ГБ",
        "seeds": 2,
        "peers": 0,
        "uploadDate": "08.07.2014 в 12:58",
    }

    class AliasMCP:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def search_pages(self, query: str, pages: int = 1) -> list[dict]:
            self.queries.append(query)
            # Транслит трекер не знает — раздача находится по другому названию.
            return [row] if "sailor" in query.lower() else []

    mcp = AliasMCP()
    agent.mcp = mcp  # type: ignore[assignment]
    agent.transmission = SimpleNamespace(  # type: ignore[assignment]
        storage_info=lambda: SimpleNamespace(ok=False, error="нет диска", to_dict=lambda: {}),
    )
    from core.models import Task

    task = Task(
        id="alias-periodic", kind="periodic", state="monitoring", session_id="checks",
        user_query="Сэйлор Мун", interval_minutes=60, auto_download=False,
        params={"title": "Сэйлор Мун", "aliases": ["Sailor Moon", "Красавица-воин Сейлор Мун"]},
    )
    await agent.store.save_task(task)
    await agent.check_task(task)
    print("  запросы к трекеру:", mcp.queries[:4])
    check("проверка не ограничилась транслитом",
          any("sailor" in query.lower() for query in mcp.queries), mcp.queries[:5])
    checks = await agent.store.list_checks("alias-periodic", limit=1)
    found = (checks[-1] or {}).get("found") if checks else None
    print("  найдено по проверке:", found)
    check("раздача нашлась по другому названию", found == 1, found)
    agent.store.close()


def test_name_matching() -> None:
    print("\n== поиск не цепляет чужие сериалы ==")
    from core.matching import title_match
    from core.parsing import parse_torrent

    def tor(title: str):
        return parse_torrent({"id": "1", "title": title})

    cases = [
        ("Ставка на любовь", "Ставка на любовь (3 сезон: 1 выпуск) / 2026 / РУ / WEBRip (1080p)", True),
        ("Ставка на любовь",
         "Крепостная (3 сезон: 1-24 серии из 24) Желанная любовь / 2021 / РУ / IPTV (1080p)", False),
        ("Человек паук", "Человек-паук 3: Враг в отражении (2026) / WEB-DL (1080p)", True),
        ("Игра престолов", "Игры престолов / 2011 / ДБ", True),
        ("Оно", "Онора / 2021 / РУ / WEB-DL (720p)", False),
    ]
    for query, title, expected in cases:
        got = title_match(query, tor(title))
        check(f"{'находит' if expected else 'не находит'}: {title[:44]}", got == expected, got)


async def main() -> int:
    await test_auto_download()
    await test_delete_keeps_progress()
    await test_notify_only()
    await test_episode_before_floor()
    await test_plan_for_periodic()
    await test_failed_task_keeps_checking()
    await test_nothing_at_all()
    await test_pending_retry()
    await test_listing_before_torrent()
    await test_pack_needs_choice()
    await test_notify_buttons_stay_alive()
    await test_next_episode_not_rolled_back()
    await test_no_duplicate_periodic()
    await test_refine_periodic()
    await test_strict_conditions()
    await test_make_periodic_in_place()
    await test_refine_settings()
    await test_cancel_only_in_panel()
    await test_default_interval()
    await test_first_finding_is_not_news()
    await test_check_searches_aliases()
    test_name_matching()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: периодические задачи качают ровно новые серии")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
