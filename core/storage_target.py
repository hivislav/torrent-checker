"""Куда именно писать файлы: проверка тома, каталога и прав.

Один и тот же набор правил нужен в двух местах: для папки загрузок Transmission
(`/Volumes/EAGET/torrents`) и для папки с .torrent-файлами
(`/Volumes/EAGET/torrent-files`). Оба обязаны жить на внешнем диске, и оба должны
честно сообщать, если диск не смонтирован, — иначе файл молча уедет на системный
раздел или не создастся вовсе.

Отдельный модуль появился, чтобы это правило не было скопировано дважды:
`Transmission` берёт отсюда проверку тома, агент — проверку папки торрентов.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from core.torrent_meta import human_bytes


class StorageUnavailable(RuntimeError):
    """Том не смонтирован или каталог недоступен для записи."""


@dataclass
class TargetInfo:
    path: Path
    volume: Path
    mounted: bool
    writable: bool
    free: int = 0
    total: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.mounted and self.writable

    def to_dict(self) -> dict[str, object]:
        name = self.volume.name or str(self.volume)
        return {
            "path": str(self.path),
            "volume": name,
            "ok": self.ok,
            "mounted": self.mounted,
            "writable": self.writable,
            "free": self.free,
            "free_human": human_bytes(self.free),
            "total": self.total,
            "total_human": human_bytes(self.total),
            "error": self.error,
        }


def volume_root(path: Path) -> Path:
    """Точка монтирования тома: /Volumes/EAGET для /Volumes/EAGET/что-угодно.

    Отдельная ветка для /Volumes обязательна: без неё поиск «первой точки
    монтирования вверх по дереву» уходил до корня «/» (он тоже mount point), и
    агент считал смонтированным несуществующий диск.
    """
    path = Path(path)
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path("/Volumes") / parts[2]
    for candidate in (path, *path.parents):
        try:
            if os.path.ismount(str(candidate)):
                return candidate
        except OSError:
            continue
    return path.parent


def can_write(path: Path) -> bool:
    """Проверка записи настоящей пробой.

    `os.access` на внешних томах (exFAT, сетевые диски) врёт: права есть, а
    запись падает. Поэтому пробуем создать и сразу удалить пустой файл.
    """
    probe = Path(path) / ".agent-write-probe"
    try:
        probe.write_bytes(b"")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def target_info(path: Path | str) -> TargetInfo:
    """Состояние тома и каталога. Ничего не создаёт и не падает."""
    path = Path(path)
    volume = volume_root(path)
    mounted = volume.exists() and os.path.ismount(str(volume))
    if not mounted:
        return TargetInfo(
            path=path,
            volume=volume,
            mounted=False,
            writable=False,
            error=f"внешний диск «{volume.name}» не подключён ({volume} отсутствует)",
        )
    try:
        usage = shutil.disk_usage(str(volume))
    except OSError as exc:
        return TargetInfo(
            path=path,
            volume=volume,
            mounted=True,
            writable=False,
            error=f"не удалось прочитать свободное место: {exc}",
        )

    if path.exists():
        writable = can_write(path)
    else:
        # Каталога ещё нет — считаем доступным, если запись возможна в том.
        writable = can_write(volume)
    return TargetInfo(
        path=path,
        volume=volume,
        mounted=True,
        writable=writable,
        free=usage.free,
        total=usage.total,
        error="" if writable else f"каталог {path} недоступен для записи",
    )


def ensure_target(path: Path | str) -> TargetInfo:
    """Проверяет том и при необходимости создаёт каталог.

    Бросает StorageUnavailable, если диск не смонтирован или каталог не создать:
    лучше внятная ошибка, чем файл не там, где его ждут.
    """
    path = Path(path)
    info = target_info(path)
    if not info.mounted:
        raise StorageUnavailable(info.error or "внешний диск не подключён")
    if not info.writable:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(f"не удалось создать {path}: {exc}") from exc
        info = target_info(path)
        if not info.writable:
            raise StorageUnavailable(info.error or f"каталог {path} недоступен для записи")
    return info
