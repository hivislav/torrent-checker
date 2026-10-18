"""Размер раздачи из .torrent-файла (bencode).

Зачем свой разборщик вместо строки «36.77 ГБ» с трекера: перед постановкой в
Transmission нужно точно знать, сколько места потребуется. Строка с сайта —
перевод, а не число; в самом .torrent лежит точный размер в байтах
(`info.length` для одного файла или сумма `info.files[].length` для папки).

Модуль сознательно крошечный: bencode-декодер без зависимостей, только для
чтения заголовка. Чужой код сюда не тянем — здесь нечего ломать.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class TorrentMetaError(RuntimeError):
    pass


def _decode(data: bytes, index: int = 0) -> tuple[Any, int]:
    """Декодирует одно значение bencode, возвращает (значение, новая позиция)."""
    if index >= len(data):
        raise TorrentMetaError("неожиданный конец файла")
    char = data[index : index + 1]

    if char == b"i":  # integer: i<число>e
        end = data.find(b"e", index + 1)
        if end == -1:
            raise TorrentMetaError("незакрытое целое")
        try:
            return int(data[index + 1 : end]), end + 1
        except ValueError as exc:
            raise TorrentMetaError("битое целое") from exc

    if char == b"l":  # list: l<значения>e
        index += 1
        items: list[Any] = []
        while data[index : index + 1] != b"e":
            value, index = _decode(data, index)
            items.append(value)
        return items, index + 1

    if char == b"d":  # dictionary: d<ключ><значение>...e
        index += 1
        result: dict[bytes, Any] = {}
        while data[index : index + 1] != b"e":
            key, index = _decode(data, index)
            value, index = _decode(data, index)
            if isinstance(key, bytes):
                result[key] = value
        return result, index + 1

    if char.isdigit():  # byte string: <длина>:<байты>
        colon = data.find(b":", index)
        if colon == -1:
            raise TorrentMetaError("нет разделителя длины")
        try:
            length = int(data[index:colon])
        except ValueError as exc:
            raise TorrentMetaError("битая длина строки") from exc
        start = colon + 1
        return data[start : start + length], start + length

    raise TorrentMetaError(f"неизвестный тип bencode: {char!r}")


def total_length(torrent_path: Path) -> int | None:
    """Сколько байт займёт раздача. None — если посчитать не удалось."""
    path = Path(torrent_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TorrentMetaError(f"не прочитать {path.name}: {exc}") from exc

    value, _ = _decode(data, 0)
    if not isinstance(value, dict):
        raise TorrentMetaError("верхний уровень .torrent — не словарь")

    info = value.get(b"info")
    if not isinstance(info, dict):
        raise TorrentMetaError("в .torrent нет словаря info")

    length = info.get(b"length")
    if isinstance(length, int):
        return length

    files = info.get(b"files")
    if isinstance(files, list):
        total = 0
        for item in files:
            if isinstance(item, dict) and isinstance(item.get(b"length"), int):
                total += item[b"length"]
        return total or None

    return None


def count_files(torrent_path: Path) -> int:
    """Сколько файлов внутри раздачи (для человеческого описания)."""
    try:
        value, _ = _decode(Path(torrent_path).read_bytes(), 0)
    except (OSError, TorrentMetaError):
        return 0
    if not isinstance(value, dict):
        return 0
    info = value.get(b"info")
    if not isinstance(info, dict):
        return 0
    if isinstance(info.get(b"length"), int):
        return 1
    files = info.get(b"files")
    return len(files) if isinstance(files, list) else 0


def human_bytes(value: float | int | None) -> str:
    """Байты в «36.8 ГБ» — с десятичной запятой, как принято по-русски."""
    if value is None:
        return "неизвестно"
    number = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(number) < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{int(number)} Б"
            text = f"{number:.1f}".rstrip("0").rstrip(".")
            return f"{text.replace('.', ',')} {unit}"
        number /= 1024
    return f"{number:.1f} ТБ"
