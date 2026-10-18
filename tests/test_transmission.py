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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.transmission import Transmission  # noqa: E402

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


async def main() -> int:
    transmission = Transmission()
    print("\n== связь по RPC ==")
    status = await transmission.test_connection()
    print(" ", status)
    check("Transmission отвечает", status.get("ok") is True, status.get("error"))

    if not status.get("ok"):
        print("\n=== ИТОГ ===")
        print("FAIL: без связи с Transmission остальное проверить нельзя")
        return 1

    print("\n== постановка .torrent ==")
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
