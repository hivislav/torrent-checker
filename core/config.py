#!/usr/bin/env python3
"""Чтение настроек из окружения и .env — без внешних зависимостей.

Правило одно и то же для всех запусков: значение сначала берётся из окружения,
и только если его там нет — из .env. Поэтому `SITE_PORT=9000 tools/run.sh` важнее
строки в файле, а сам файл можно спокойно править руками.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Снимаем кавычки, если значение ими обёрнуто.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def _as_list(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Все настройки приложения в одном месте."""

    root: Path
    data_dir: Path

    # --- сайт ---
    site_host: str
    site_port: int
    site_tls: bool
    site_tls_names: list[str]
    public_url: str
    site_password: str

    # --- LLM ---
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout: float

    # --- kinozal ---
    kinozal_base_url: str
    kinozal_username: str
    kinozal_password: str

    # --- Transmission ---
    transmission_host: str
    transmission_port: int
    transmission_username: str
    transmission_password: str
    # Куда складывать загрузки. Всегда внешний диск: при отключённом диске
    # агент честно откажется ставить раздачу, а не зальёт её на внутренний.
    transmission_download_dir: Path
    transmission_min_free_gb: float
    # Потолок скорости скачивания, МБ/с (0 — без ограничения). Transmission
    # пишет на диск в том же потоке, который отвечает на RPC: на предельной
    # скорости внешний SSD перестаёт успевать, запись блокирует поток, и API
    # замолкает на минуты. Небольшой запас скорости оставляет демону дыхание.
    transmission_speed_limit_mbps: float
    # Кэш записи Transmission, МБ. Маленький кэш (по умолчанию 4) — это частые
    # мелкие записи на exfat; крупная пачка пишется дольше, но реже.
    transmission_cache_mb: int

    # --- обход Cloudflare ---
    gate_host: str
    gate_port: int
    gate_chrome_path: str
    gate_headless: bool
    gate_profile_dir: Path
    # Как часто шлюз сам обновляет cf_clearance, пока работает (минуты).
    # 0 — не обновлять: тогда первый запрос после долгого простоя ждёт, пока
    # Chrome заново пройдёт challenge.
    gate_keepalive_minutes: int

    # --- контекст переписки ---
    # Сколько последних сообщений видит модель при разборе запроса. Больше —
    # точнее уточнения («нет, я про 4 сезон»), но дороже по токенам.
    context_messages: int
    # Сколько минут незакрытая задача считается «текущей»: старые задачи
    # не должны ловить новые запросы («Извне» через час после «Человека-паука»).
    active_task_ttl_minutes: int

    # --- прочее ---
    torrents_dir: Path
    db_path: Path
    default_interval_minutes: int
    min_interval_minutes: int

    extra: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env = _read_env_file(ROOT / ".env")

    def get(key: str, default: str = "") -> str:
        value = os.environ.get(key)
        if value is None:
            value = env.get(key, default)
        return value

    def resolve(value: str) -> Path:
        """Относительные пути считаем от корня проекта.

        Это важно не для красоты: MCP-сервер запускается со своим рабочим
        каталогом (mcp/kinozal-mcp), и относительный путь к папке торрентов
        превратился бы там в другой каталог — файл просто некуда было бы писать.
        """
        path = Path(value)
        return path if path.is_absolute() else (ROOT / path)

    data_dir = resolve(get("DATA_DIR", "data"))

    # Имена для самоподписанного сертификата: петля, адрес в домашней сети и
    # внешний адрес. PUBLIC_URL разбираем, чтобы адрес не приходилось дублировать.
    # По умолчанию — только петля: адрес мака в домашней сети задаётся в .env,
    # чтобы он не уезжал в репозиторий вместе с кодом.
    tls_names = _as_list(
        get("SITE_TLS_NAMES"),
        ["127.0.0.1", "localhost"],
    )

    return Settings(
        root=ROOT,
        data_dir=data_dir,
        site_host=get("SITE_HOST", "0.0.0.0"),
        site_port=_as_int(get("SITE_PORT"), 8000),
        site_tls=_as_bool(get("SITE_TLS"), True),
        site_tls_names=tls_names,
        public_url=get("PUBLIC_URL", "https://127.0.0.1:8000"),
        site_password=get("SITE_PASSWORD", ""),
        llm_api_key=get("DEEPSEEK_API_KEY"),
        llm_base_url=get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        llm_model=get("DEEPSEEK_MODEL", "deepseek-flash"),
        llm_temperature=float(get("LLM_TEMPERATURE", "0.2") or 0.2),
        llm_max_tokens=_as_int(get("LLM_MAX_TOKENS"), 2400),
        llm_timeout=float(get("LLM_TIMEOUT", "120") or 120),
        kinozal_base_url=get("KINOZAL_BASE_URL", "https://kinozal.me").rstrip("/"),
        kinozal_username=get("KINOZAL_USERNAME"),
        kinozal_password=get("KINOZAL_PASSWORD"),
        transmission_host=get("TRANSMISSION_HOST", "127.0.0.1"),
        transmission_port=_as_int(get("TRANSMISSION_PORT"), 9091),
        transmission_username=get("TRANSMISSION_USERNAME", ""),
        transmission_password=get("TRANSMISSION_PASSWORD", ""),
        transmission_download_dir=resolve(
            get("TRANSMISSION_DOWNLOAD_DIR", "/Volumes/EAGET/torrents")
        ),
        transmission_min_free_gb=float(get("TRANSMISSION_MIN_FREE_GB", "5") or 5),
        transmission_speed_limit_mbps=float(get("TRANSMISSION_SPEED_LIMIT_MBPS", "70") or 0),
        transmission_cache_mb=_as_int(get("TRANSMISSION_CACHE_MB"), 32),
        gate_host=get("GATE_HOST", "127.0.0.1"),
        gate_port=_as_int(get("GATE_PORT"), 8123),
        gate_chrome_path=get(
            "GATE_CHROME_PATH",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
        gate_headless=_as_bool(get("GATE_HEADLESS"), False),
        gate_profile_dir=resolve(get("GATE_PROFILE_DIR", str(data_dir / "chrome-profile"))),
        gate_keepalive_minutes=_as_int(get("GATE_KEEPALIVE_MINUTES"), 12),
        # .torrent-файлы лежат рядом с загрузками, на том же внешнем диске, но
        # в отдельной папке: их удобно разбирать руками, а Transmission не
        # путает их с данными раздач.
        torrents_dir=resolve(get("TORRENTS_DIR", "/Volumes/EAGET/torrent-files")),
        db_path=resolve(get("DB_PATH", str(data_dir / "agent.sqlite3"))),
        context_messages=_as_int(get("CONTEXT_MESSAGES"), 20),
        active_task_ttl_minutes=_as_int(get("ACTIVE_TASK_TTL_MINUTES"), 30),
        default_interval_minutes=_as_int(get("DEFAULT_INTERVAL_MINUTES"), 1440),
        min_interval_minutes=_as_int(get("MIN_INTERVAL_MINUTES"), 5),
        extra=env,
    )


def gate_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return f"http://{s.gate_host}:{s.gate_port}"
