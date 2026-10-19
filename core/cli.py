#!/usr/bin/env python3
"""CLI-адаптер агента — доказательство, что ядро не зависит от веба.

Тот же `core.agent.Agent`, что и на сайте, но без HTTP, браузера и шаблонов:

    .venv/bin/python -m core.cli "Тед Лассо 1080p LostFilm"
    .venv/bin/python -m core.cli --mode periodic "следи за Дюной, качай сразу"
    .venv/bin/python -m core.cli --tasks
    .venv/bin/python -m core.cli --cancel <id>

Если этот файл работает, агент готов к переезду в Telegram-бота: там будет
ровно такой же цикл «получил текст → вызвал handle_message → показал ответ».
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import Agent  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    agent = Agent()
    await agent.start(with_scheduler=False)
    try:
        if args.tasks:
            tasks = await agent.list_tasks()
            if not tasks:
                print("задач нет")
            for task in tasks:
                line = (
                    f"[{task['state']:<16}] {task['kind']:<8} "
                    f"{(task.get('params') or {}).get('title') or task['user_query']}"
                )
                if task.get("interval_minutes"):
                    line += f" · каждые {task['interval_minutes']} мин"
                print(line, f"({task['id']})")
            return 0

        if args.cancel:
            task = await agent.cancel_task(args.cancel)
            print("отменена" if task else "не найдена")
            return 0

        if not args.text:
            print("укажите запрос, например: python -m core.cli \"Дюна 1080p\"")
            return 2

        turn = await agent.handle_message("cli", " ".join(args.text), args.mode)
        for message in turn.messages:
            print(f"\n[{message.role}] {message.text}")
            for action in message.actions:
                # Ссылка — это адрес, а не действие: печатаем адрес, а не payload.
                if action.kind == "link":
                    print(f"    ↗ «{action.label}»: {action.url}")
                    continue
                print(f"    → кнопка «{action.label}» ({action.kind}: {action.payload})")

        # Простейший сценарий подтверждения прямо в терминале.
        pending = [a for m in turn.messages for a in m.actions if a.kind == "confirm_download"]
        if pending and not args.no_confirm:
            answer = input("\nСкачать? [y/N] ").strip().lower()
            if answer in ("y", "yes", "д", "да"):
                follow = await agent.handle_action("cli", "confirm_download", pending[0].payload)
                for message in follow.messages:
                    print(f"\n[{message.role}] {message.text}")
        return 0
    finally:
        await agent.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Агент «Кинозал» без интерфейса")
    parser.add_argument("text", nargs="*", help="запрос на русском")
    parser.add_argument("--mode", default="oneshot", choices=["oneshot", "periodic", "chat"])
    parser.add_argument("--tasks", action="store_true", help="показать задачи")
    parser.add_argument("--cancel", metavar="ID", help="отменить задачу")
    parser.add_argument("--no-confirm", action="store_true", help="не спрашивать подтверждение")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
