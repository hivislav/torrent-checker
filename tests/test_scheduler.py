#!/usr/bin/env python3
"""Проверка, что периодические задачи идут параллельно и не мешают друг другу.

Запуск (шлюз и трекер доступны):  .venv/bin/python tests/test_scheduler.py

Тест кладёт в отдельную временную базу три периодические задачи, у которых уже
наступило время проверки, поднимает настоящий шедулер агента и ждёт один тик.
Ожидание: все три задачи проверились (checks_done >= 1), у всех назначено
следующее время, ни одна не упала в failed. Это и есть «параллельно и
независимо»: задачи не ждут друг друга и не делят между собой ошибки.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import Agent  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.models import Task, new_id  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


TITLES = ["Тед Лассо", "Дюна", "Гравити Фолз"]


async def main() -> int:
    base = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="agent-sched-"))
    settings = dataclasses.replace(
        base,
        data_dir=tmp,
        db_path=tmp / "scheduler-test.sqlite3",
        torrents_dir=tmp / "torrents",
    )
    tmp.mkdir(parents=True, exist_ok=True)
    settings.torrents_dir.mkdir(parents=True, exist_ok=True)

    agent = Agent(settings)
    agent.scheduler_tick_seconds = 5  # чтобы тест не ждал 20 секунд

    created: list[Task] = []
    for title in TITLES:
        task = Task(
            id=new_id(),
            kind="periodic",
            state="monitoring",
            session_id="scheduler-test",
            user_query=f"следи за {title}",
            params={"title": title, "seen_ids": []},
            interval_minutes=settings.default_interval_minutes,
            auto_download=False,
            # Время проверки уже прошло — шедулер заберёт задачи сразу.
            next_check_at="2026-01-01T00:00:00+00:00",
        )
        await agent.store.save_task(task)
        created.append(task)
    print(f"создано задач: {len(created)}")

    await agent.start()
    try:
        print("шедулер запущен, жду проверки (до 120 c)…")
        for _ in range(24):
            await asyncio.sleep(5)
            states = [
                await agent.store.get_task(task.id) for task in created
            ]
            done = [
                s for s in states if s is not None and s.checks_done >= 1
            ]
            if len(done) == len(created):
                break

        print("\n== результат ==")
        for task in created:
            current = await agent.store.get_task(task.id)
            assert current is not None
            print(
                f"  {current.params.get('title'):<16} state={current.state:<12} "
                f"checks={current.checks_done} found={current.found_total} "
                f"next={current.next_check_at} err={current.last_error[:60]}"
            )
            check(f"«{current.params.get('title')}» проверилась", current.checks_done >= 1)
            check(f"«{current.params.get('title')}» живёт дальше", current.state == "monitoring",
                  current.state)
            check(f"«{current.params.get('title')}» без ошибок", not current.last_error,
                  current.last_error[:80])

        # Параллельность: задачи проверялись примерно в одно окно времени.
        stamps = [
            (await agent.store.get_task(task.id)).last_check_at for task in created
        ]
        check("все три проверки в одном окне (параллельно)", len(set(stamps)) <= 2, stamps)
    finally:
        await agent.stop()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: периодические задачи работают независимо")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
