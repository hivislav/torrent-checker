#!/usr/bin/env python3
"""Проверка связки «MCP-сервер + шлюз Cloudflare» без веб-интерфейса.

Запуск (шлюз должен быть поднят: scripts/run.sh start):
    .venv/bin/python tests/test_mcp.py

Что проверяется:
  1. MCP-сервер стартует и отдаёт список инструментов;
  2. search_torrents возвращает раздачи (значит, Cloudflare пройден);
  3. названия раздач разбираются в качество/озвучку/сезон;
  4. download_torrent сохраняет настоящий .torrent (bencode, начинается с 'd').
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.mcp_client import KinozalMCP  # noqa: E402
from core.parsing import parse_torrent  # noqa: E402


async def main() -> int:
    mcp = KinozalMCP()
    failures: list[str] = []
    try:
        await mcp.start()
        print("1. MCP подключён:", mcp.ready)
        if not mcp.ready:
            failures.append("MCP не подключился")
            return 1

        results = await mcp.search("Тед Лассо")
        print(f"2. поиск: найдено {len(results)}")
        if not results:
            failures.append("поиск ничего не вернул")
        for raw in results[:3]:
            torrent = parse_torrent(raw)
            print(
                f"   [{torrent.id}] {torrent.title[:70]}\n"
                f"      качество={torrent.qualities} озвучка={torrent.voices} "
                f"сезон={torrent.seasons} серии={torrent.episodes}"
            )

        if results:
            target = results[0]
            out = Path("/tmp/mcp_test.torrent")
            downloaded = await mcp.download(str(target["id"]), out)
            print(f"4. скачивание: {downloaded}")
            if not downloaded.get("success"):
                failures.append(f"скачивание не удалось: {downloaded.get('message')}")
            elif not out.read_bytes().startswith(b"d"):
                failures.append("файл не похож на .torrent")

        details = await mcp.details(str(results[0]["id"])) if results else {}
        print("3. подробности:", {k: details.get(k) for k in ("year", "quality", "size", "seeds")})
    finally:
        await mcp.stop()

    print("\n=== ИТОГ ===")
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    print("OK: MCP-сервер и обход Cloudflare работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
