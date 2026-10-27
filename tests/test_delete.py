#!/usr/bin/env python3
"""Проверки удаления раздачи: файлы, .torrent и запись в памяти агента.

Запуск:  .venv/bin/python tests/test_delete.py

Главное, что здесь проверяется:
  1. удаление не уходит в корзину — файлы стираются обычным unlink/rmtree;
  2. агент не может удалить что-то вне разрешённых папок (защита от ошибочных
     путей из RPC и от симлинков, ведущих наружу);
  3. удаление раздачи убирает и данные, и .torrent-файл, и запись в downloads.

Реальная сеть и Transmission не нужны: очередь подменяется заглушкой, а файлы
создаются во временном каталоге.
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
from core.files import delete_paths, is_within  # noqa: E402
from core.models import Task, Torrent  # noqa: E402

FAILS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def test_paths() -> None:
    print("\n== защита путей ==")
    root = Path(tempfile.mkdtemp(prefix="delete-root-"))
    outside = Path(tempfile.mkdtemp(prefix="delete-outside-"))
    (root / "film").mkdir()
    (root / "film" / "part1.mkv").write_bytes(b"a" * 1024)
    (root / "film" / "part2.mkv").write_bytes(b"b" * 2048)
    (outside / "чужой.txt").write_bytes(b"c")
    (root / "link").symlink_to(outside)

    check("внутри папки — можно", is_within(root / "film" / "part1.mkv", [root]))
    check("сама папка — нельзя", not is_within(root, [root]))
    check("снаружи — нельзя", not is_within(outside / "чужой.txt", [root]))
    check("симлинк наружу раскрывается", not is_within(root / "link" / "чужой.txt", [root]))

    result = delete_paths(
        [root / "film", root / "film" / "part1.mkv", outside / "чужой.txt", root / "нет-такого"],
        [root],
    )
    print("  ", result.summary())
    check("папка удалена", not (root / "film").exists())
    check("вложенные не считаются ошибкой", len(result.missing) >= 1, result.missing)
    check("чужой файл не тронут", (outside / "чужой.txt").exists())
    check("чужой путь помечен как отказ", len(result.refused) == 1, result.refused)
    check("освобождён объём", result.freed_bytes >= 3072, result.freed_bytes)
    check("корзина не используется", not list(root.glob(".Trash*")), "появилась корзина")


async def test_agent_delete() -> None:
    print("\n== удаление раздачи агентом ==")
    tmp = Path(tempfile.mkdtemp(prefix="delete-agent-"))
    downloads = tmp / "torrents"
    torrents_dir = tmp / "torrent-files"
    downloads.mkdir(parents=True, exist_ok=True)
    torrents_dir.mkdir(parents=True, exist_ok=True)

    settings = dataclasses.replace(
        get_settings(),
        data_dir=tmp,
        db_path=tmp / "delete-test.sqlite3",
        torrents_dir=torrents_dir,
        transmission_download_dir=downloads,
    )
    agent = Agent(settings)

    # Раскладываем «скачанное»: папка раздачи на диске и .torrent-файл.
    film = downloads / "Фильм.2026.WEB-DL.1080p"
    film.mkdir()
    (film / "Фильм.mkv").write_bytes(b"x" * 4096)
    (downloads / ".incomplete").mkdir(exist_ok=True)
    partial = downloads / ".incomplete" / "Фильм.2026.WEB-DL.1080p"
    partial.mkdir()
    (partial / "Фильм.mkv.part").write_bytes(b"y" * 1024)
    torrent_file = torrents_dir / "777.torrent"
    torrent_file.write_bytes(b"d4:infod4:name4:testee")

    task = Task(
        id="del-task",
        kind="oneshot",
        state="done",
        session_id="checks",
        user_query="Фильм",
        params={"title": "Фильм"},
    )
    await agent.store.save_task(task)
    await agent.store.record_download(
        task,
        Torrent(id="777", title="Фильм (2026)", size="5 ГБ"),
        str(torrent_file),
        "hash777",
        torrent_name="Фильм.2026.WEB-DL.1080p",
    )

    # Заглушка Transmission: раздача «в очереди», данных не трогаем.
    class FakeTransmission:
        download_dir = downloads
        removed: list[str] = []

        async def find_by_hash(self, torrent_hash):
            return {
                "hash": torrent_hash,
                "name": "Фильм.2026.WEB-DL.1080p",
                "download_dir": str(downloads),
                "files": ["Фильм.2026.WEB-DL.1080p/Фильм.mkv"],
            }

        async def remove(self, torrent_hash, *, delete_data=False):
            self.removed.append(torrent_hash)
            return {"ok": True}

    fake = FakeTransmission()
    agent.transmission = fake  # type: ignore[assignment]

    confirm = await agent.request_delete("777")
    text = confirm.messages[0].text
    labels = [action.label for action in confirm.messages[0].actions]
    check("просит подтверждение", "Удалить раздачу" in text, text[:80])
    check("предупреждает про корзину", "минуя корзину" in text, text[:120])
    check("есть кнопка удаления", "Удалить навсегда" in labels, labels)
    check("ничего не удалено до подтверждения", film.exists() and torrent_file.exists())

    result = await agent.perform_delete("777")
    message = result.messages[-1].text
    print("  ", message.replace("\n", " | ")[:200])
    check("раздача убрана из очереди", fake.removed == ["hash777"], fake.removed)
    check("папка раздачи удалена", not film.exists())
    check("недокачанное удалено", not partial.exists())
    check("torrent-файл удалён", not torrent_file.exists())
    check("запись в памяти убрана", await agent.store.get_download("777") is None)
    check("в отчёте есть «освобождено»", "освобождено" in message, message)
    agent.store.close()


def test_unicode_names() -> None:
    """Удаление находит имена в любом написании Unicode.

    exfat хранит имя так, как его создали (составное «ё» или разложенное), а
    список всегда отдаёт разложенное. Настоящий случай: папка «Звёзды в
    джунглях» (7 ГБ) не удалилась — «No such file or directory», — а запись о
    раздаче агент уже снял, и место осталось занятым.
    """
    print("\n== удаление не спотыкается об Unicode ==")
    import os
    import tempfile
    import unicodedata

    from core.files import delete_paths, resolve_existing

    root = Path(tempfile.mkdtemp(prefix="unicode-del-"))
    for label, dir_form, file_form in (
        ("nfc-nfc", "NFC", "NFC"),
        ("nfc-nfd", "NFC", "NFD"),
        ("nfd-nfc", "NFD", "NFC"),
        ("nfd-nfd", "NFD", "NFD"),
    ):
        target = root / label
        target.mkdir()
        folder = unicodedata.normalize(dir_form, "Звёзды Тест")
        (target / folder).mkdir()
        for name in ("файл.bin", "серия.mkv"):
            (target / folder / unicodedata.normalize(file_form, name)).write_bytes(b"\0" * 1024)
        # Агент помнит путь в составном виде — как пришёл из своей базы.
        wanted = target / unicodedata.normalize("NFC", "Звёзды Тест")
        result = delete_paths([wanted], [target])
        check(f"{label}: папка удалена", not result.errors and not os.listdir(target),
              (result.errors, os.listdir(target)))
        check(f"{label}: место посчитано", result.freed_bytes >= 2048, result.freed_bytes)

    check("путь ищется по нормализованному имени",
          resolve_existing(root / "нет такого") is None)

    # штатная проверка «только свои папки» продолжает работать
    outside = Path(tempfile.mkdtemp(prefix="outside-"))
    (outside / "чужое").mkdir()
    refused = delete_paths([outside / "чужое"], [root])
    check("вне разрешённых папок не удаляем", bool(refused.refused), refused.refused)
    check("чужое осталось", (outside / "чужое").exists())
    import shutil

    shutil.rmtree(outside, ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)


async def main() -> int:
    test_paths()
    await test_agent_delete()
    test_unicode_names()

    print("\n=== ИТОГ ===")
    if FAILS:
        for name in FAILS:
            print("FAIL:", name)
        return 1
    print("OK: удаление стирает файлы без корзины и не выходит за свои папки")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
