#!/usr/bin/env python3
"""Проверка шлюза Cloudflare: challenge, поиск, вход, скачивание .torrent.

Запуск:  .venv/bin/python tests/test_gate.py
Что проверяется:
  1. Chrome поднимается и проходит managed-challenge Cloudflare;
  2. curl_cffi с полученным cf_clearance отдаёт настоящую страницу раздач;
  3. вход на трекер под учёткой из .env даёт сессию (cookie pass);
  4. download.php отдаёт валидный .torrent (bencode начинается с 'd').
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.cf_gate import CloudflareGate  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE = "https://kinozal.me"


async def main() -> int:
    gate = CloudflareGate()
    failures: list[str] = []
    try:
        await gate.start()
        print("\n--- 1. clearance ---")
        await gate.ensure_clearance()
        print("cookies:", sorted(gate._cookies))
        if "cf_clearance" not in gate._cookies:
            failures.append("нет cf_clearance")

        print("\n--- 2. поиск ---")
        result = await gate.fetch(f"{BASE}/browse.php?s=matrix")
        html = result.text_cp1251()
        print(f"status={result.status} via={result.via} len={len(html)} t_peer={'t_peer' in html}")
        if result.status != 200 or "t_peer" not in html:
            failures.append(f"поиск не отдал таблицу раздач (status={result.status})")
        else:
            import re

            titles = re.findall(r'class="r[01]">([^<]+)</a>', html)[:3]
            for t in titles:
                print("   ", t[:100])

        print("\n--- 3. вход ---")
        cookies = await gate.login()
        print("cookies:", sorted(cookies))
        if "pass" not in cookies:
            failures.append("вход не дал cookie pass")

        print("\n--- 4. скачивание .torrent ---")
        import re

        ids = re.findall(r"details\.php\?id=(\d+)", html)
        if not ids:
            failures.append("не нашёл id раздачи для скачивания")
        else:
            target = ids[0]
            response = await gate.fetch(f"{BASE}/download.php?id={target}")
            body = response.body
            print(f"id={target} status={response.status} bytes={len(body)} head={body[:12]!r}")
            if not body.startswith(b"d"):
                failures.append("download.php вернул не torrent-файл")
                Path("/tmp/bad_download.html").write_bytes(body)
            else:
                out = Path("/tmp/gate_test.torrent")
                out.write_bytes(body)
                print("сохранён:", out)

        print("\n--- статус ---")
        print(gate.status())
    finally:
        await gate.stop()

    print("\n=== ИТОГ ===")
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("OK: все четыре шага прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
