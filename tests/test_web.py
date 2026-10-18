#!/usr/bin/env python3
"""Дымовой тест интерфейса: страница, вход, отправка запроса, кнопки, панель задач.

Запуск (сайт уже поднят: scripts/run.sh start):
    .venv/bin/python tests/test_web.py

Проверяется именно то, что видит человек: что страница открывается, что вход по
паролю работает, что сообщение уходит в агента и под ним появляются кнопки, что
панель задач заполняется и что на узком экране (телефон) раскладка не ломается.
Скриншоты складываются в data/screenshots.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from core.config import get_settings  # noqa: E402

BASE = "https://127.0.0.1:8000"
SHOTS = Path(__file__).resolve().parent.parent / "data" / "screenshots"

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def run() -> int:
    settings = get_settings()
    password = settings.site_password
    SHOTS.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(settings.db_path))
    start_msg = conn.execute("SELECT IFNULL(MAX(id), 0) FROM messages").fetchone()[0]
    start_tasks = {row[0] for row in conn.execute("SELECT id FROM tasks")}
    conn.close()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox"])
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
        page = context.new_page()
        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(str(err)))

        print("\n== вход ==")
        page.goto(BASE + "/", wait_until="domcontentloaded")
        if password:
            check("страница входа открылась", page.locator("#login-form").count() == 1)
            page.fill("#password", password)
            page.click("button[type=submit]")
            # После входа страница перезагружается — ждём именно чат, а не
            # «domcontentloaded» (он успевает сработать ещё на форме входа).
            page.wait_for_selector("#chat", timeout=20000)
        check("чат открылся", page.locator("#chat").count() == 1)
        check("две кнопки режима", page.locator(".mode-btn").count() == 2)
        page.screenshot(path=str(SHOTS / "01-chat.png"), animations="disabled")

        play = page

        print("\n== отправка запроса ==")
        # Начинаем с чистого чата: иначе сообщение уйдёт в открытую задачу
        # (например, в периодическую) как уточнение.
        page.click("#panel-new-task")
        page.wait_for_timeout(400)
        page.click('.mode-btn[data-mode="oneshot"]')
        page.fill("#input", "Тед Лассо 1080p")
        # В чате уже может быть история: считаем сообщения до отправки и ждём
        # именно новое, иначе проверка увидит старый ответ.
        before_agents = page.locator(".msg--agent").count()
        page.click("#send")
        page.wait_for_function(
            "n => document.querySelectorAll('.msg--agent').length > n",
            arg=before_agents,
            timeout=120000,
        )
        page.wait_for_timeout(1500)
        # Ждём, пока в новом ответе появится либо кнопка подтверждения, либо вопрос.
        page.wait_for_selector(".msg__actions .btn, .msg--agent .msg__text", timeout=180000)
        last_agent = page.locator(".msg--agent").last
        last_text = last_agent.locator(".msg__text").inner_text()
        actions = last_agent.locator(".msg__actions .btn").all_inner_texts()
        check("в ответе есть текст о поиске",
              any(word in last_text for word in ("Ищу", "Нашёл", "По запросу")), last_text[:160])
        check("появились кнопки действий", len(actions) > 0, actions)
        page.screenshot(path=str(SHOTS / "02-answer.png"), animations="disabled")

        print("\n== панель задач ==")
        # Карточка появляется не мгновенно: агент дописывает задачу, а панель
        # обновляется раз в 5 секунд.
        cards = 0
        for _ in range(30):
            cards = page.locator("#active-tasks .task-card").count()
            past = page.locator("#past-tasks .task-card").count()
            if cards + past > 0:
                break
            page.wait_for_timeout(1000)
        check("в картотеке есть карточки", cards + past > 0, f"active={cards} past={past}")
        status = page.locator("#status-grid").inner_text()
        check("статус служб отрисован", "модель" in status, status[:120])
        page.screenshot(path=str(SHOTS / "03-panel.png"), animations="disabled")

        print("\n== удаление обновляет диск без перезагрузки ==")
        # Раньше строка «диск» в «Служебном» показывала старые цифры до
        # перезагрузки страницы: статус обновлялся раз в минуту. Проверяем,
        # что после удаления карточки статус перезапрашивается сразу.
        import shutil

        test_id = "ui-test-delete"
        test_name = "ui-test-delete.bin"
        download_dir = Path(settings.transmission_download_dir)
        test_dir = download_dir / test_name
        test_torrent = Path("/tmp") / f"{test_id}.torrent"
        db_row_added = False
        try:
            shutil.rmtree(test_dir, ignore_errors=True)
            test_dir.mkdir(parents=True)
            (test_dir / "film.bin").write_bytes(b"\x00" * (8 * 1024 * 1024))
            test_torrent.write_bytes(b"d4:infod4:name0:e")
            conn = sqlite3.connect(str(settings.db_path))
            conn.execute("DELETE FROM downloads WHERE torrent_id = ?", (test_id,))
            conn.execute(
                "INSERT INTO downloads (torrent_id, title, name, size, torrent_path, added_at) "
                "VALUES (?,?,?,?,?, datetime('now'))",
                (test_id, test_name, test_name, "8 МБ", str(test_torrent)),
            )
            conn.commit()
            conn.close()
            db_row_added = True

            card = page.locator("#download-list .task-card", has_text=test_name).first
            card.wait_for(state="visible", timeout=20000)
            page.evaluate("window.__noReload = 1")
            status_calls = []
            page.on("request", lambda req: status_calls.append(req.url)
                    if "/api/status" in req.url else None)
            card.locator("button", has_text="Удалить").first.click()
            # Подтверждение ищем по имени нашей раздачи: в истории чата могут
            # остаться старые подтверждения с такой же кнопкой.
            confirm = page.locator("#chat .msg").filter(has_text=test_name).filter(
                has_text="Удалить навсегда"
            ).last
            confirm.wait_for(state="visible", timeout=20000)
            confirm.locator("button", has_text="Удалить навсегда").first.click()
            page.wait_for_timeout(1500)
            page.wait_for_function(
                """name => [...document.querySelectorAll('#download-list .task-card')]
                     .every(el => !el.textContent.includes(name))""",
                arg=test_name,
                timeout=20000,
            )
            check("карточка удалённого исчезла", True)
            check("статус перезапрошен без перезагрузки", len(status_calls) > 0,
                  f"запросов /api/status: {len(status_calls)}")
            check("страница не перезагружалась",
                  page.evaluate("window.__noReload") == 1)
            check("файлы удалены с диска", not test_dir.exists())
        finally:
            shutil.rmtree(test_dir, ignore_errors=True)
            test_torrent.unlink(missing_ok=True)
            if db_row_added:
                conn = sqlite3.connect(str(settings.db_path))
                conn.execute("DELETE FROM downloads WHERE torrent_id = ?", (test_id,))
                # Болтовню про удаление тестовой раздачи в истории чата не оставляем:
                # её видит человек, открывший сайт.
                conn.execute("DELETE FROM messages WHERE text LIKE ?", (f"%{test_name}%",))
                conn.commit()
                conn.close()

        print("\n== секции картотеки ==")
        sections = page.evaluate(
            """() => ['active','finished','downloads'].map(name => ({
                name,
                hasToggle: !!document.querySelector(`[data-toggle="${name}"]`),
                hasBadge: !!document.querySelector(`[data-section="${name}"] .badge`),
                hasHint: !!document.querySelector(`[data-section="${name}"] .panel__hint`),
            }))"""
        )
        for item in sections:
            check(f"секция «{item['name']}» с кнопкой и счётчиком",
                  item["hasToggle"] and item["hasBadge"], item)
        check("у секций есть пояснения", all(item["hasHint"] for item in sections), sections)

        def section_state(name):
            return page.evaluate(
                """(name) => {
                    const box = document.querySelector(`[data-section="${name}"] .task-list`);
                    return { hidden: box.hidden, display: getComputedStyle(box).display,
                             items: box.children.length };
                }""",
                name,
            )

        # Каждая кнопка должна реально скрывать и показывать свой список.
        for name in ("finished", "active", "downloads"):
            before = section_state(name)
            page.click(f'[data-toggle="{name}"]')
            page.wait_for_timeout(250)
            after = section_state(name)
            changed = before["display"] != after["display"]
            check(f"«{name}»: показать/скрыть работает", changed, (before, after))
            page.click(f'[data-toggle="{name}"]')
            page.wait_for_timeout(250)

        # Состояние запоминается между перезагрузками.
        page.click('[data-toggle="finished"]')
        page.wait_for_timeout(300)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#chat", timeout=20000)
        page.wait_for_timeout(1500)
        check("состояние секции запоминается", section_state("finished")["display"] != "none",
              section_state("finished"))
        page.click('[data-toggle="finished"]')
        page.wait_for_timeout(250)
        page.screenshot(path=str(SHOTS / "08-panel.png"), animations="disabled")

        print("\n== журнал отладки ==")
        page.click("#open-debug")
        page.wait_for_selector("#debug-modal:not([hidden])", timeout=10000)
        page.wait_for_selector(".debug__item", timeout=15000)
        items = page.locator(".debug__item").count()
        check("окно журнала открылось", items > 0, items)
        # подписи фильтров выводятся заглавными (CSS text-transform)
        cats = [text.lower() for text in page.locator(".debug__filters .chip").all_inner_texts()]
        check("есть фильтры по категориям", "модель" in cats and "трекер" in cats, cats)
        titles = page.locator(".debug__title").all_inner_texts()
        check("видно вызов модели", any("модель" in t for t in titles), titles[:4])
        check("видно обращение к трекеру", any("трекер" in t for t in titles), titles[:6])
        # раскрытие подробностей
        page.locator(".debug__row").first.click()
        page.wait_for_timeout(300)
        check("подробности раскрываются", page.locator(".debug__pair").count() > 0)
        # фильтр «трекер» оставляет только mcp-события
        page.locator(".debug__filters .chip", has_text="трекер").first.click()
        page.wait_for_timeout(1200)
        only_mcp = page.locator(".debug__item").all_inner_texts()
        check("фильтр работает", bool(only_mcp) and all("трекер" in text for text in only_mcp), only_mcp[:2])
        page.screenshot(path=str(SHOTS / "06-debug.png"), animations="disabled")
        page.locator(".debug__filters .chip", has_text="всё").first.click()
        page.wait_for_timeout(600)
        page.click("#debug-close")
        page.wait_for_timeout(300)
        check("окно закрывается", page.locator("#debug-modal[hidden]").count() == 1)

        print("\n== мобильная раскладка ==")
        mobile = context.new_page()
        mobile.set_viewport_size({"width": 390, "height": 844})
        mobile.goto(BASE + "/", wait_until="domcontentloaded")
        mobile.wait_for_selector("#chat", timeout=20000)
        check("кнопка картотеки видна", mobile.locator("#panel-open").is_visible())
        # Панель на телефоне сдвинута за левый край, поэтому проверяем координату,
        # а не is_visible(): элемент с ненулевым размером считается видимым даже
        # когда он за экраном.
        box = mobile.locator("#panel").bounding_box()
        check("панель скрыта по умолчанию", bool(box) and box["x"] + box["width"] <= 1,
              box)
        mobile.click("#panel-open")
        mobile.wait_for_timeout(600)
        check("панель открылась по тапу", mobile.locator("#panel").is_visible())
        mobile.screenshot(path=str(SHOTS / "04-mobile.png"), animations="disabled")
        mobile.click("#panel-close")
        mobile.wait_for_timeout(400)

        print("\n== ошибки в консоли ==")
        # Сообщение про SSL — ожидаемое следствие самоподписанного сертификата:
        # браузер отказывает service worker'у, но само приложение работает.
        ignored = ("favicon", "ssl certificate error", "certificate error")
        noisy = [e for e in console_errors if not any(marker in e.lower() for marker in ignored)]
        check("консоль без ошибок", not noisy, noisy[:3])

        browser.close()

    # Тест ходит в трекер по-настоящему: его переписку в истории чата
    # не оставляем, иначе человек увидит чужие запросы.
    conn = sqlite3.connect(str(settings.db_path))
    conn.execute(
        "DELETE FROM messages WHERE id > ? AND (text LIKE ? OR text LIKE ?)",
        (start_msg, "%Тед Лассо%", "%ui-test-delete%"),
    )
    # Убираем только то, что завёл сам тест (его запрос — «Тед Лассо…»).
    # Чужие задачи, включая завершённые и периодические, не трогаем:
    # иначе у человека пропадает его картотека.
    placeholders = ",".join("?" for _ in start_tasks) or "''"
    test_tasks = (
        f"SELECT id FROM tasks WHERE id NOT IN ({placeholders}) "
        f"AND kind <> 'periodic' AND user_query LIKE ?"
    )
    conn.execute(
        f"DELETE FROM messages WHERE task_id IN ({test_tasks})",
        (*start_tasks, "%Тед Лассо%"),
    )
    conn.execute(
        f"DELETE FROM tasks WHERE id IN ({test_tasks})",
        (*start_tasks, "%Тед Лассо%"),
    )
    conn.commit()
    conn.close()

    print("\n=== ИТОГ ===")
    print("скриншоты:", SHOTS)
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: интерфейс работает")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
