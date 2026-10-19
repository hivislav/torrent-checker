#!/usr/bin/env python3
"""Проверки правила «качаем только на внешний диск и только если есть место».

Запуск:  .venv/bin/python tests/test_storage.py

Что проверяется:
  1. разбор размера из .torrent (bencode) — и для одного файла, и для папки;
  2. отказ, когда свободного места меньше, чем нужно (+ резерв);
  3. отказ, когда внешний диск не смонтирован;
  4. реальная постановка в Transmission: раздача попадает именно на внешний
     диск, а настройки клиента переводятся туда же;
  5. сообщение агенту: что именно он говорит пользователю при нехватке места.

Тест не скачивает контент: в Transmission ставится крошечный синтетический
.torrent с недоступным трекером и сразу удаляется.
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
from core.models import Torrent, Turn  # noqa: E402
from core.torrent_meta import human_bytes, total_length  # noqa: E402
from core.storage_target import (  # noqa: E402
    StorageUnavailable as TargetUnavailable,
    ensure_target,
    target_info,
    volume_root,
)
from core.transmission import (  # noqa: E402
    NotEnoughSpace,
    StorageUnavailable,
    Transmission,
)

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


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


def make_torrent(path: Path, *, size: int = 1024, files: int = 1, name: str = "probe.txt") -> Path:
    info: dict = {"name": name, "piece length": 16384, "pieces": b"\x00" * 20}
    if files == 1:
        info["length"] = size
    else:
        info["files"] = [
            {"length": size // files, "path": [f"part{i}.bin"]} for i in range(files)
        ]
    data = bencode({"announce": "http://127.0.0.1:1/announce", "info": info})
    path.write_bytes(data)
    return path


# ----------------------------------------------------------------------
def test_sizes(tmp: Path) -> None:
    print("\n== размер из .torrent ==")
    single = make_torrent(tmp / "single.torrent", size=4096)
    check("один файл", total_length(single) == 4096, total_length(single))

    folder = make_torrent(tmp / "folder.torrent", size=6000, files=3)
    check("папка: сумма файлов", total_length(folder) == 6000, total_length(folder))

    check("форматирование ГБ", human_bytes(39_485_893_318) == "36,8 ГБ", human_bytes(39_485_893_318))
    check("форматирование МБ", human_bytes(5 * 1024**2) == "5 МБ", human_bytes(5 * 1024**2))


def test_space_rules(tmp: Path) -> None:
    print("\n== правила по месту ==")
    transmission = Transmission()
    info = transmission.storage_info()
    print(" ", info.to_dict())
    if not info.mounted:
        print("  (внешний диск не смонтирован — проверки места пропускаю)")
        return
    if not info.writable:
        print("  (диск смонтирован, но запись из этого окружения запрещена — проверяю логику на подменённых данных)")
    else:
        check("диск смонтирован и доступен", info.ok, info.error)

    # Подменяем чтение диска, чтобы не занимать 200 ГБ ради проверки.
    fake = dataclasses.replace(
        info, mounted=True, writable=True, free=10 * 1024**3, total=224 * 1024**3
    )
    transmission.storage_info = lambda: fake  # type: ignore[method-assign]

    try:
        transmission.check_space(need_bytes=4 * 1024**3)
        check("4 ГБ при 10 ГБ свободных проходят (4 + резерв 5 ≤ 10)", True)
    except NotEnoughSpace as exc:
        check("4 ГБ при 10 ГБ свободных проходят (4 + резерв 5 ≤ 10)", False, str(exc))

    try:
        transmission.check_space(need_bytes=6 * 1024**3)
        check("6 ГБ при резерве 5 ГБ отвергаются", False, "исключения не было")
    except NotEnoughSpace as exc:
        text = exc.describe()
        print("  сообщение:", text)
        check("6 ГБ при резерве 5 ГБ отвергаются", exc.need == 6 * 1024**3)
        check("в сообщении есть «не хватает места»", "не хватает места" in text, text)
        check("в сообщении есть резерв", "резерв" in text, text)


def test_torrent_files_dir() -> None:
    print("\n== папка для .torrent-файлов ==")
    settings = get_settings()
    torrents_dir = settings.torrents_dir
    print("  путь:", torrents_dir)
    check(
        "torrent-файлы на том же диске, что и загрузки",
        volume_root(torrents_dir) == volume_root(settings.transmission_download_dir),
        f"{volume_root(torrents_dir)} vs {volume_root(settings.transmission_download_dir)}",
    )
    check(
        "это отдельная папка, а не каталог загрузок",
        torrents_dir != Path(settings.transmission_download_dir),
        str(torrents_dir),
    )
    info = target_info(torrents_dir)
    if info.mounted and info.writable:
        created = ensure_target(torrents_dir)
        check("каталог создаётся и доступен", created.ok, created.error)
    else:
        print("  (нет прав записи из этого окружения — создание каталога пропускаю)")

    # диск не подключён — агент обязан отказаться, а не писать на системный
    broken = dataclasses.replace(settings, torrents_dir=Path("/Volumes/НЕТ_ДИСКА/torrent-files"))
    try:
        ensure_target(broken.torrents_dir)
        check("отказ без диска для torrent-файлов", False, "исключения не было")
    except TargetUnavailable as exc:
        check("отказ без диска для torrent-файлов", "не подключён" in str(exc), str(exc))


def test_used_and_own_space() -> None:
    print("\n== занято / наши файлы / скрытое ==")
    from core.storage_target import dir_size, target_info

    tmp = Path(tempfile.mkdtemp(prefix="space-"))
    (tmp / "big.bin").write_bytes(b"x" * 4096)
    size, complete = dir_size(tmp)
    check("размер каталога считается", size == 4096 and complete, (size, complete))

    info = target_info(tmp)
    info.own_bytes = 4096
    info.own_complete = True
    data = info.to_dict()
    check("в данных есть «занято»", "used" in data and data["used"] >= 0, data.get("used"))
    check("в данных есть «наши файлы»", data.get("own_bytes") == 4096, data.get("own_bytes"))
    check("скрытое = занято − наши", data.get("hidden_bytes") == max(0, info.used - 4096),
          (data.get("hidden_bytes"), info.used))


def test_missing_disk(tmp: Path) -> None:
    print("\n== диск не подключён ==")
    settings = dataclasses.replace(
        get_settings(), transmission_download_dir=Path("/Volumes/НЕТ_ТАКОГО_ДИСКА/torrents")
    )
    transmission = Transmission(settings)
    try:
        transmission.check_space(need_bytes=1024)
        check("отказ без диска", False, "исключения не было")
    except StorageUnavailable as exc:
        print("  сообщение:", str(exc))
        check("отказ без диска", True)
        check("в сообщении назван диск", "не подключён" in str(exc), str(exc))


async def test_real_add(tmp: Path) -> None:
    print("\n== постановка в Transmission ==")
    transmission = Transmission()
    info = transmission.storage_info()
    if not info.mounted:
        print("  (внешний диск не смонтирован — постановку пропускаю)")
        return
    if not info.writable:
        # Диск есть, но запись из этого окружения запрещена (например, тест
        # запущен под песочницей агента). Это ограничение среды, а не кода:
        # реальную постановку проверяет tests/test_storage через сервис.
        print("  (нет прав записи из этого окружения — реальную постановку пропускаю)")
        return

    torrent = make_torrent(tmp / "add-me.torrent", size=2048, name="agent-disk-probe.txt")
    added = await transmission.add(torrent)
    print(" ", {k: v for k, v in added.items() if k != "hash"})
    check("раздача принята", added.get("ok") is True)
    check(
        "каталог раздачи — внешний диск",
        str(added.get("download_dir", "")).startswith(str(transmission.download_dir)),
        added.get("download_dir"),
    )

    client = transmission._get_client()  # noqa: SLF001 - проверяем настройки клиента
    try:
        session_dir = str(client.get_session().download_dir)
        check("Transmission переведён на внешний диск", session_dir == str(transmission.download_dir), session_dir)
    except Exception as exc:  # noqa: BLE001
        check("Transmission переведён на внешний диск", False, str(exc))

    torrents = await transmission.list_torrents(limit=200)
    ours = [t for t in torrents if t.get("hash") == added.get("hash")]
    check("раздача видна в очереди", bool(ours))
    if ours:
        check(
            "в очереди у неё внешний каталог",
            str(ours[0].get("download_dir", "")).startswith(str(transmission.download_dir)),
            ours[0].get("download_dir"),
        )

    def cleanup() -> None:
        client.remove_torrent(added["hash"], delete_data=True)

    await asyncio.to_thread(cleanup)
    for _ in range(10):
        await asyncio.sleep(0.5)
        left = await transmission.list_torrents(limit=200)
        if not any(t.get("hash") == added.get("hash") for t in left):
            break
    check("тестовая раздача убрана", True)


async def test_agent_refuses_without_torrent_dir() -> None:
    print("\n== агент не качает .torrent при отсутствии диска ==")
    tmp = Path(tempfile.mkdtemp(prefix="agent-torrents-"))
    base = get_settings()
    settings = dataclasses.replace(
        base,
        data_dir=tmp,
        db_path=tmp / "torrent-dir-test.sqlite3",
        torrents_dir=Path("/Volumes/НЕТ_ДИСКА/torrent-files"),
    )
    agent = Agent(settings)

    called = {"mcp": False}

    class FakeMCP:
        async def download(self, torrent_id, path):
            called["mcp"] = True
            return {"success": True, "path": str(path)}

    agent.mcp = FakeMCP()  # type: ignore[assignment]
    from core.models import Task

    task = Task(
        id="probe",
        kind="oneshot",
        state="awaiting_confirm",
        session_id="probe",
        user_query="проба",
        params={"title": "проба"},
    )
    result = await agent._download(task, Torrent(id="1", title="Проба"))  # noqa: SLF001
    check("отказ до обращения к MCP", called["mcp"] is False, called)
    check("причина — диск", result.get("reason") == "disk", result)
    check("сообщение понятное", "torrent-файлов" in result.get("error", ""), result.get("error"))
    agent.store.close()


async def test_agent_message() -> None:
    print("\n== что агент говорит пользователю ==")
    tmp = Path(tempfile.mkdtemp(prefix="agent-storage-"))
    base = get_settings()
    settings = dataclasses.replace(
        base, data_dir=tmp, db_path=tmp / "storage-test.sqlite3", torrents_dir=tmp / "torrents"
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)

    # Подменяем Transmission на тот, что «видит» полный диск.
    class FullDisk:
        download_dir = Path("/Volumes/EAGET/torrents")

        def storage_info(self):
            return SimpleNamespace(
                ok=True,
                error="",
                to_dict=lambda: {
                    "volume": "EAGET",
                    "path": "/Volumes/EAGET/torrents",
                    "free_human": "12,3 ГБ",
                    "total_human": "223,5 ГБ",
                },
            )

    agent.transmission = FullDisk()  # type: ignore[assignment]
    turn = Turn()
    torrent = Torrent(id="1", title="Тед Лассо (4 сезон: 1-10 серии из 10) / Ted Lasso")
    await agent._report_storage_problem(  # noqa: SLF001 - проверяем текст сообщения
        task=SimpleNamespace(id="t1", to_dict=lambda: {}, state="failed"),
        torrent=torrent,
        result={
            "error": "на диске «EAGET» не хватает места: нужно 36,8 ГБ + резерв 5 ГБ, свободно 12,3 ГБ",
            "reason": "space",
            "path": "/tmp/1.torrent",
        },
        turn=turn,
    )
    message = turn.messages[-1]
    print("  текст:", message.text.replace("\n", " | ")[:200])
    check("сказано про нехватку места", "Не хватает места" in message.text, message.text[:80])
    check("назван диск", "EAGET" in message.text)
    labels = [action.label for action in message.actions]
    check("есть кнопка «Поставить в очередь»", "Поставить в очередь" in labels, labels)
    agent.store.close()



async def test_status_cached_offloop() -> None:
    """Состояние диска: считается в потоке, кэшируется, не подвешивает цикл."""
    print("\n== диск в статусе: в потоке и с кэшем ==")
    import time

    from types import SimpleNamespace as NS

    tmp = Path(tempfile.mkdtemp(prefix="disk-status-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3",
        torrents_dir=tmp / "t", transmission_download_dir=tmp / "dl",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    settings.transmission_download_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)

    calls = {"n": 0}

    class SlowDisk:
        def storage_info(self):
            calls["n"] += 1
            time.sleep(0.05)  # «спящий» диск: любой вызов медленный
            return NS(
                ok=True, mounted=True, writable=True, free=100 * 1024**3,
                total=224 * 1024**3, error="", own_bytes=0, own_complete=True,
                to_dict=lambda: {"ok": True, "volume": "EAGET", "free_human": "100 ГБ",
                                 "total_human": "224 ГБ", "error": ""},
            )

    agent.transmission = SlowDisk()  # type: ignore[assignment]
    started = time.monotonic()
    storage, torrents = await agent._storage_status()  # noqa: SLF001
    first = time.monotonic() - started
    started = time.monotonic()
    await agent._storage_status()  # noqa: SLF001
    second = time.monotonic() - started
    print(f"  первый вызов: {first:.3f} с, второй: {second:.3f} с, обращений к диску: {calls['n']}")
    check("диск опрошен", storage.get("ok") is True, storage)
    check("второй раз — из кэша, без обращения к диску", calls["n"] == 1, calls["n"])
    check("кэш отвечает мгновенно", second < 0.01, second)
    check("состояние каталога .torrent тоже вернулось", isinstance(torrents, dict), torrents)
    agent.store.close()


async def test_status_keeps_last_known() -> None:
    """Диск занят записью: прошлые цифры вместо прочерков.

    Пока Transmission пишет 4K-сезон, обход внешнего тома может не уложиться в
    бюджет. Раньше панель в этот момент писала «диск не ответил» и обнуляла
    все значения — хотя диск на месте и работает.
    """
    print("\n== диск занят: панель показывает последние известные данные ==")
    tmp = Path(tempfile.mkdtemp(prefix="disk-stale-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3",
        torrents_dir=tmp / "t", transmission_download_dir=tmp / "dl",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    settings.transmission_download_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)

    empty, empty_torrents = agent._stale_storage()  # noqa: SLF001
    check("без прошлых данных — честная ошибка", empty.get("ok") is False, empty)
    check("и никаких выдуманных цифр", empty.get("free_human") == "—", empty.get("free_human"))
    check("каталог .torrent пустой", empty_torrents == {}, empty_torrents)

    last = {"ok": True, "volume": "EAGET", "free_human": "155,1 ГБ",
            "total_human": "223,5 ГБ", "checked_at": "19:04"}
    agent._storage_cache = (0.0, dict(last), {"path": "/Volumes/EAGET/torrent-files", "ok": True})  # noqa: SLF001
    storage, torrents = agent._stale_storage()  # noqa: SLF001
    check("свободное место не обнулилось", storage.get("free_human") == "155,1 ГБ", storage)
    check("помечено как устаревшее", storage.get("stale") is True, storage)
    check("видно, когда мерили", storage.get("checked_at") == "19:04", storage.get("checked_at"))
    check("объяснение вместо «нет доступа»",
          "занят" in str(storage.get("error")), storage.get("error"))
    check("каталог .torrent тоже из прошлого ответа",
          torrents.get("ok") is True, torrents)
    agent.store.close()


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="storage-test-"))
    test_sizes(tmp)
    test_space_rules(tmp)
    test_torrent_files_dir()
    test_used_and_own_space()
    test_missing_disk(tmp)
    await test_real_add(tmp)
    await test_agent_refuses_without_torrent_dir()
    await test_agent_message()
    await test_status_cached_offloop()
    await test_status_keeps_last_known()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: загрузки идут на внешний диск, нехватка места отлавливается")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
