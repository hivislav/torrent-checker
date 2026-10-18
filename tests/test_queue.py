#!/usr/bin/env python3
"""Проверки списка «Скачано на диск»: он показывает ВСЮ очередь Transmission.

Запуск:  .venv/bin/python tests/test_queue.py

Зачем: торрент можно добавить прямо в Transmission (или запись потерять при
обслуживании), и он всё равно должен быть виден в карточке — иначе список
расходится с реальностью. Тест подменяет Transmission и проверяет слияние
наших записей с очередью, а также удаление раздачи, о которой записи нет.
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
from core.models import Task, Torrent  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


class FakeTransmission:
    def __init__(self, download_dir: Path) -> None:
        self.download_dir = download_dir
        self.queue = [
            {"id": 1, "name": "Наш торрент", "hash": "h1", "status": "seeding",
             "progress": 100.0, "rate_download": 0, "size": 1000,
             "download_dir": str(download_dir)},
            {"id": 2, "name": "Чужой торрент", "hash": "h2", "status": "seeding",
             "progress": 100.0, "rate_download": 0, "size": 2000,
             "download_dir": str(download_dir)},
        ]
        self.removed: list[str] = []

    def storage_info(self):  # noqa: D102
        return SimpleNamespace(
            ok=True, mounted=True, writable=True, free=100 * 1024**3,
            total=224 * 1024**3, error="", own_bytes=0, own_complete=True,
            to_dict=lambda: {"volume": "EAGET", "path": str(self.download_dir),
                             "free_human": "100 ГБ", "total_human": "224 ГБ", "ok": True},
        )

    def min_free_bytes(self) -> int:  # noqa: D102
        return 5 * 1024**3

    async def list_torrents(self, limit: int = 50):  # noqa: D102
        return list(self.queue)

    async def progress(self, hashes):  # noqa: D102
        result = {}
        for item in self.queue:
            if item["hash"] in hashes:
                result[item["hash"]] = {
                    "hash": item["hash"], "name": item["name"], "percent": 100.0,
                    "done": True, "rate": 0, "rate_human": "", "eta_seconds": None,
                    "eta_human": "", "size": item["size"], "size_human": "2 КБ",
                    "downloaded_human": "2 КБ", "left_human": "0 Б", "status": "seeding",
                }
        return result

    async def find_by_hash(self, torrent_hash):  # noqa: D102
        for item in self.queue:
            if item["hash"] == torrent_hash:
                return {"hash": torrent_hash, "name": item["name"],
                        "download_dir": str(self.download_dir), "files": []}
        return None

    async def remove(self, torrent_hash, *, delete_data=False):  # noqa: D102
        self.removed.append(torrent_hash)
        return {"ok": True}


def build() -> tuple[Agent, FakeTransmission]:
    tmp = Path(tempfile.mkdtemp(prefix="queue-"))
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "q.sqlite3",
        torrents_dir=tmp / "torrents", transmission_download_dir=tmp / "dl",
    )
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)
    settings.transmission_download_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    fake = FakeTransmission(settings.transmission_download_dir)
    agent.transmission = fake  # type: ignore[assignment]
    return agent, fake


async def main() -> int:
    print("\n== очередь Transmission видна в карточке ==")
    agent, fake = build()

    task = Task(id="t", kind="oneshot", state="done", session_id="s",
                user_query="наш", params={"title": "Наш торрент"})
    await agent.store.save_task(task)
    await agent.store.record_download(
        task, Torrent(id="777", title="Наш торрент", size="1 КБ"),
        "/tmp/777.torrent", "h1", torrent_name="Наш торрент",
    )

    items = await agent.downloads()
    print("  записей:", [(item["source"], item["title"]) for item in items])
    check("наша запись на месте", any(item["source"] == "agent" for item in items), items)
    check("чужой торрент тоже виден", any(item["title"] == "Чужой торрент" for item in items), items)
    check("дубликатов нет",
          len([item for item in items if item.get("hash") == "h1"]) == 1,
          [(item["source"], item.get("hash")) for item in items])
    foreign = next(item for item in items if item["title"] == "Чужой торрент")
    check("у чужого есть хеш для удаления", foreign["hash"] == "h2", foreign.get("hash"))
    check("у чужого есть прогресс", (foreign.get("progress") or {}).get("percent") == 100.0,
          foreign.get("progress"))

    print("\n== удаление раздачи, о которой нет записи ==")
    confirm = await agent.request_delete("", "h2")
    text = confirm.messages[0].text
    labels = [action.label for action in confirm.messages[0].actions]
    check("просит подтверждение", "Удалить раздачу" in text, text[:80])
    check("в кнопке есть хеш",
          any(action.payload.get("hash") == "h2" for action in confirm.messages[0].actions),
          [(a.label, a.payload) for a in confirm.messages[0].actions])
    check("есть кнопка удаления", "Удалить навсегда" in labels, labels)

    # Файлов на диске нет — удаление не должно падать, но из очереди уберёт.
    folder = agent.settings.transmission_download_dir / "Чужой торрент"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "file.mkv").write_bytes(b"x" * 1024)
    result = await agent.perform_delete("", "h2")
    print("  отчёт:", result.messages[-1].text.replace("\n", " | ")[:120])
    check("раздача убрана из очереди", fake.removed == ["h2"], fake.removed)
    check("файлы удалены", not folder.exists())
    check("запись о нашем торренте не тронута",
          await agent.store.get_download("777") is not None)
    agent.store.close()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: список показывает всю очередь, чужое удаляется по хешу")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
