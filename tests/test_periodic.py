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
    check("можно включить автокачку", "Качать сразу и сообщать" in labels, labels)

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
    check("состав раздачи спросили у страницы", mcp.listing_calls >= 1, mcp.listing_calls)
    check("в Transmission пусто", not transmission.added, transmission.added)

    # Появился 3-й — теперь .torrent нужен, и он скачивается ровно один раз.
    mcp.episodes = 3
    await agent.check_task(task)
    task = await agent.store.get_task(task.id)
    assert task is not None
    print("  .torrent-файлов скачано:", mcp.downloads)
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

    rows = await agent.store.history_for_task(task.id)
    notices = [row for row in rows if row["role"] == "agent" and ("раздача с сериями" in (row["text"] or "") or "пачка серий" in (row["text"] or ""))]
    print("  уведомление:", (notices[0]["text"].splitlines()[0] if notices else "нет"))
    check("пришло уведомление про пачку", bool(notices), [r["text"][:60] for r in rows])
    labels = [action["label"] for action in (notices[0]["actions"] if notices else [])]
    check("есть кнопки «Скачать всю пачку» и «Выбрать серии»",
          "Скачать всю пачку" in labels and "Выбрать серии" in labels, labels)

    # Второй раз то же самое не повторяем.
    before = len([row for row in await agent.store.history_for_task(task.id)
                  if ("раздача с сериями" in (row["text"] or "") or "пачка серий" in (row["text"] or ""))])
    await agent.check_task(await agent.store.get_task(task.id))
    after = len([row for row in await agent.store.history_for_task(task.id)
                 if ("раздача с сериями" in (row["text"] or "") or "пачка серий" in (row["text"] or ""))])
    check("уведомление не дублируется", before == after, (before, after))

    # Кнопка «Выбрать серии» показывает тот же выбор серий, что и в разовых задачах.
    task = await agent.store.get_task(task.id)
    groups = (task.options or {}).get("episode_groups") or []
    check("серии пачки сохранены для выбора", len(groups) == 20, len(groups))
    turn = await agent.handle_action("s1", "choose_episodes", {"task_id": task.id})
    labels = [action.label for message in turn.messages for action in message.actions
              if action.kind == "toggle_episode"]
    print("  кнопки после «Выбрать серии»:", labels[:3], "… всего", len(labels))
    check("выбор серий открылся", len(labels) == 20, len(labels))
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
    await test_refine_periodic()
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
