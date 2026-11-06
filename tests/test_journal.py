"""Киножурнал: записи живут в базе и не теряются при перезагрузке.

Раньше уведомления существовали только в памяти открытой вкладки: они
приходили по SSE, попадали выборочно и исчезали вместе со страницей. Записи
о находках периодических задач (нашёл серию → поставил качать) в журнале
увидеть было нельзя вовсе.

Проверяем:
  1. запись уведомления попадает в базу и читается списком;
  2. порядок — свежие первыми, старые подрезаются;
  3. «очистить» действительно очищает;
  4. журнал одного агента виден новому агенту на той же базе (перезапуск).
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.agent import Agent  # noqa: E402
from core.config import get_settings  # noqa: E402
from core.models import Task  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def make_agent(tmp: Path) -> Agent:
    settings = dataclasses.replace(
        get_settings(), data_dir=tmp, db_path=tmp / "s.sqlite3", torrents_dir=tmp / "t"
    )
    return Agent(settings)


async def scenario() -> None:
    print("\n== киножурнал в базе ==")
    tmp = Path(tempfile.mkdtemp(prefix="journal-"))
    agent = make_agent(tmp)
    task = Task(id="j1", kind="periodic", state="monitoring", session_id="s",
                user_query="Выживалити", params={"title": "Выживалити"})
    await agent.store.save_task(task)

    await agent._publish_notification(  # noqa: SLF001
        "success", "Скачивание по задаче",
        "Новая серия по задаче «Выживалити»: нашёл серию 6 и поставил качать.",
        task_id=task.id,
    )
    await agent._publish_notification("error", "Ошибка", "Трекер не выдал файл", task_id=task.id)  # noqa: SLF001

    items = await agent.store.list_notifications()
    print("  записей:", len(items), "| первая:", (items[0] or {}).get("title"))
    check("запись о скачивании попала в журнал",
          any(item["title"] == "Скачивание по задаче" for item in items),
          [item["title"] for item in items])
    check("в записи есть время", all(item["created_at"] for item in items), items)
    check("свежие записи первыми", items and items[0]["title"] == "Ошибка",
          [item["title"] for item in items])
    check("запись помнит задачу", items and items[0]["task_id"] == "j1", items[0] if items else None)

    # Перезапуск приложения: журнал читается из той же базы.
    agent2 = make_agent(tmp)
    items2 = await agent2.store.list_notifications()
    check("журнал переживает перезапуск", len(items2) == len(items), (len(items), len(items2)))

    cleared = await agent2.store.clear_notifications()
    check("очистка сработала", cleared == len(items), cleared)
    check("после очистки пусто", not await agent2.store.list_notifications())

    # Журнал не растёт бесконечно: в базе остаются последние записи.
    for index in range(520):
        await agent2.store.add_notification("info", f"событие {index}", "текст")
    items3 = await agent2.store.list_notifications(limit=1000)
    print("  после 520 записей:", len(items3))
    check("журнал подрезается до разумного размера", len(items3) <= 500, len(items3))
    agent.store.close()
    agent2.store.close()


def main() -> int:
    asyncio.run(scenario())
    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: киножурнал хранится в базе и не теряется")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
