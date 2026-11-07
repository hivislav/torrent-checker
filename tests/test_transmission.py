#!/usr/bin/env python3
"""Проверка Transmission: связь по RPC и постановка .torrent в очередь.

Запуск:  .venv/bin/python tests/test_transmission.py

Тест не тянет настоящую раздачу: он собирает крошечный корректный .torrent с
недоступным трекером, ставит его на паузу через наш клиент, убеждается, что он
появился в очереди, и сразу удаляет. Так проверяется весь путь
«файл → Transmission.add → запись в очереди», не засоряя загрузки.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.transmission import Transmission, daemon_pids  # noqa: E402

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
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        body = b"".join(bencode(key) + bencode(value[key]) for key in sorted(value))
        return b"d" + body + b"e"
    raise TypeError(type(value))


def make_multi_torrent(path: Path, files: int = 4) -> None:
    """Торрент из нескольких файлов — для проверки выбора нужного файла."""
    torrent = {
        "announce": "http://127.0.0.1:1/announce",
        "info": {
            "name": "kinozal-agent-multi",
            "piece length": 16384,
            "pieces": b"\x00" * 20,
            "files": [{"length": 1024, "path": [f"episode-{i}.txt"]} for i in range(files)],
        },
    }
    path.write_bytes(bencode(torrent))


def make_torrent(path: Path) -> None:
    payload = b"kinozal-agent self test"
    torrent = {
        "announce": "http://127.0.0.1:1/announce",
        "info": {
            "name": "kinozal-agent-self-test.txt",
            "length": len(payload),
            "piece length": 16384,
            "pieces": b"\x00" * 20,
        },
    }
    path.write_bytes(bencode(torrent))


async def test_busy_pause() -> None:
    """Демон занят диском: пауза вместо шторма повторов.

    Transmission пишет на диск в том же потоке, что отвечает на RPC. Пока идёт
    большая запись, ответа нет; панель, обновляющаяся каждые 5 секунд, не должна
    каждый раз ждать полный бюджет и плодить потоки.
    """
    print("\n== демон занят диском: пауза вместо шторма повторов ==")
    client = Transmission()
    calls = {"count": 0}

    def slow() -> dict:
        calls["count"] += 1
        time.sleep(0.4)
        return {}

    try:
        await client._run(slow, budget=0.2)  # noqa: SLF001 - проверяем внутренности
        check("первый вызов упал по таймауту", False, "не упал")
    except Exception as exc:  # noqa: BLE001
        check("первый вызов падает как «занят»",
              type(exc).__name__ == "TransmissionBusy", type(exc).__name__)
    paused = client.busy_for()
    check("после обрыва включилась пауза", paused > 0, round(paused, 2))

    started = time.monotonic()
    try:
        await client._run(slow, budget=0.2)  # noqa: SLF001
        check("во время паузы вызов падает сразу", False, "не упал")
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        check("во время паузы вызов падает сразу",
              elapsed < 0.1 and type(exc).__name__ == "TransmissionBusy",
              (round(elapsed, 3), type(exc).__name__))
    # Один повтор — ради мёртвого сокета после простоя, но уже с коротким
    # бюджетом, а не с полным: ожидание не удваивается.
    check("был ровно один дешёвый повтор", calls["count"] == 2, calls)

    answer = await client._run(lambda: {"ok": True}, budget=2.0, probe=True)  # noqa: SLF001
    check("прощупывающий вызов разрешён и снимает паузу",
          answer == {"ok": True} and client.busy_for() == 0, client.busy_for())
    check("pid демона читается", isinstance(daemon_pids(), list), daemon_pids())


async def main() -> int:
    transmission = Transmission()
    print("\n== связь по RPC ==")
    status = await transmission.test_connection()
    print(" ", status)
    check("Transmission отвечает", status.get("ok") is True, status.get("error"))

    await test_busy_pause()

    if not status.get("ok"):
        print("\n=== ИТОГ ===")
        print("FAIL: без связи с Transmission остальное проверить нельзя")
        return 1

    print("\n== постановка .torrent ==")
    # Выбор одного файла из пачки: Transmission по умолчанию считает выбранными
    # ВСЕ файлы, поэтому нужен и files-wanted, и files-unwanted. Без этого
    # качался весь сезон вместо одной серии («Ставка на любовь», серия 4).
    print("\n== из пачки качается только выбранный файл ==")
    multi_path = Path("/tmp/agent-selftest-multi.torrent")
    make_multi_torrent(multi_path, files=4)
    multi = await transmission.add(multi_path, files_wanted=[3])
    print("  add(files_wanted=[3]):", multi.get("files_selected"))

    def read_multi() -> dict:
        client = transmission._get_client()  # noqa: SLF001 - тест про внутренности
        torrent = client.get_torrent(multi["hash"])
        return {
            "status": str(getattr(torrent, "status", "")),
            "selected": [item.selected for item in torrent.get_files()],
        }

    multi_state = await asyncio.to_thread(read_multi)
    print("  состояние:", multi_state)
    check("выбран ровно один файл из пачки",
          multi_state["selected"] == [False, False, False, True], multi_state["selected"])
    check("раздача запущена, а не на паузе",
          multi_state["status"] not in ("stopped", ""), multi_state["status"])

    def drop_multi() -> None:
        client = transmission._get_client()  # noqa: SLF001
        client.remove_torrent(multi["hash"], delete_data=True)

    await asyncio.to_thread(drop_multi)
    multi_path.unlink(missing_ok=True)

    torrent_path = Path("/tmp/agent-selftest.torrent")
    make_torrent(torrent_path)
    print("  собран тестовый torrent:", torrent_path, torrent_path.stat().st_size, "байт")

    added = await transmission.add(torrent_path)
    print("  add():", added)
    check("torrent принят Transmission", added.get("ok") is True, added)
    check("вернулся хеш", bool(added.get("hash")), added)

    torrents = await transmission.list_torrents(limit=200)
    found = [t for t in torrents if t.get("hash") == added.get("hash")]
    check("раздача видна в очереди", bool(found), added.get("hash"))

    # Добавленная раздача должна сразу качаться, а не стоять на паузе: раньше
    # выбор файлов шёл вторым вызовом, Transmission застревал на нуле и помогал
    # только ручной «пауза → старт».
    if found:
        def read_state() -> dict:
            client = transmission._get_client()  # noqa: SLF001 - тест про внутренности
            torrent = client.get_torrent(added["hash"])
            return {
                "status": str(getattr(torrent, "status", "")),
                "files_wanted": getattr(torrent, "files_wanted", None),
            }

        state = await asyncio.to_thread(read_state)
        print("  состояние после add:", state)
        check("после добавления раздача не на паузе",
              state["status"] not in ("stopped", ""), state)
        check("выбор файлов применился при добавлении",
              state["files_wanted"] in (None, []), state)

    # Убираем за собой, чтобы тест не оставлял мусора в загрузках.
    if found:
        def cleanup() -> None:
            client = transmission._get_client()  # noqa: SLF001 - тест проверяет внутренности
            client.remove_torrent(added["hash"], delete_data=True)

        await asyncio.to_thread(cleanup)
        # Transmission убирает раздачу не мгновенно — даём ему пару секунд.
        removed = False
        for _ in range(10):
            await asyncio.sleep(0.5)
            torrents_after = await transmission.list_torrents(limit=200)
            if not any(t.get("hash") == added.get("hash") for t in torrents_after):
                removed = True
                break
        check("раздача удалена после теста", removed)
    torrent_path.unlink(missing_ok=True)

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: Transmission принимает торренты")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
