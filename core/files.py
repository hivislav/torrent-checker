"""Удаление файлов раздачи — навсегда и только там, где разрешено.

Transmission на macOS штатно «удаляет» данные через Finder API, то есть
перемещает их в `.Trashes` на том же томе. Место при этом не освобождается, пока
корзину не очистят вручную. Поэтому данные стирает сам агент — обычными
`os.remove`/`shutil.rmtree`, которые корзину не используют вовсе.

Главный риск здесь — удалить не то. Пути приходят из Transmission RPC, а он
отдаёт их как есть, поэтому перед удалением каждый путь проверяется:

  * путь приводится к реальному (`os.path.realpath` — снимает `..` и симлинки);
  * он обязан лежать внутри одного из разрешённых корней (каталог загрузок,
    его `.incomplete`, папка torrent-файлов);
  * сам корень удалять нельзя.

Всё, что не прошло проверку, не удаляется и возвращается вызывающему с причиной.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from core.torrent_meta import human_bytes


@dataclass
class DeletionResult:
    deleted: list[str] = field(default_factory=list)
    # Пути, отклонённые проверкой: они вне разрешённых папок, это важно показать.
    refused: list[str] = field(default_factory=list)
    # Путей нет на диске: нормально, если их папка уже удалена.
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    freed_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and not self.refused

    def summary(self) -> str:
        parts = [f"удалено объектов: {len(self.deleted)}"]
        if self.freed_bytes:
            parts.append(f"освобождено {human_bytes(self.freed_bytes)}")
        if self.refused:
            parts.append(f"не тронуто (вне разрешённых папок): {len(self.refused)}")
        if self.errors:
            parts.append("ошибки: " + "; ".join(self.errors[:3]))
        return ", ".join(parts)


def _norm(path: str | Path) -> str:
    """Канонический путь без хвостового слэша (realpath снимает симлинки)."""
    try:
        real = os.path.realpath(str(path))
    except OSError:
        real = str(path)
    return real.rstrip(os.sep) or os.sep


def _allocated_size(path: str) -> int:
    """Сколько блоков реально занято — для недокачанных файлов это не st_size.

    Transmission создаёт файл нужной длины сразу и дописывает кусками: st_size
    у такого файла — полный размер серии (6 ГБ), а занято может быть 600 МБ.
    Считать «освобождено» по st_size значит обещать место, которого не было.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return 0
    blocks = getattr(stat, "st_blocks", None)
    if blocks:
        return int(blocks) * 512
    return int(stat.st_size)


def path_size(path: Path) -> int:
    """Сколько занимает файл или папка (для отчёта «освобождено»)."""
    try:
        if path.is_file():
            return _allocated_size(str(path))
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                total += _allocated_size(os.path.join(root, name))
        return total
    except OSError:
        return 0


def is_within(path: Path, roots: Iterable[Path]) -> bool:
    """Лежит ли путь внутри одного из разрешённых корней."""
    target = _norm(path)
    for root in roots:
        base = _norm(root)
        if target == base:
            continue  # сам корень удалять нельзя
        if target.startswith(base + os.sep):
            return True
    return False


def delete_path(path: Path, roots: Iterable[Path], result: DeletionResult) -> None:
    """Удаляет файл или папку без корзины, если путь в разрешённом корне."""
    path = Path(path)
    if not os.path.lexists(path):
        result.missing.append(str(path))
        return
    if not is_within(path, roots):
        result.refused.append(str(path))
        return
    size = path_size(path)
    try:
        if path.is_symlink() or path.is_file():
            os.remove(path)
        else:
            shutil.rmtree(path)
    except OSError as exc:
        result.errors.append(f"не удалось удалить {path}: {exc}")
        return
    result.deleted.append(str(path))
    result.freed_bytes += size


def delete_paths(paths: Iterable[Path], roots: Iterable[Path]) -> DeletionResult:
    result = DeletionResult()
    roots = list(roots)
    # Короткие пути первыми: сначала удаляется папка раздачи, и вложенные файлы
    # исчезают вместе с ней. Их «не найдено» — норма, а не проблема.
    for path in sorted({str(p) for p in paths}, key=len):
        delete_path(Path(path), roots, result)
    return result
