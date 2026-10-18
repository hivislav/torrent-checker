"""Обход Cloudflare на kinozal.me.

Почему это отдельный сервис, а не строчка в HTTP-клиенте.

kinozal.me закрыт managed-challenge'ом Cloudflare: на `browse.php`, `details.php`,
`login.php`, `download.php` любой обычный HTTP-клиент получает 403 с заголовком
`cf-mitigated: challenge` и страницей «Just a moment…». Проверено: curl, httpx,
node-fetch, curl_cffi с любым impersonate (включая chrome131/136) — все получают
403. Проходит только настоящий Chrome, который реально исполняет JS-задачу.

Отсюда схема из двух ступеней:

1. **Chrome через CDP (nodriver)** один раз решает challenge и получает cookie
   `cf_clearance`. Именно этот браузер — единственный «человек» во всей схеме.
2. **curl_cffi** дальше ходит по сайту с этим cookie и с User-Agent браузера:
   быстро (десятки миллисекунд вместо секунд) и без нагрузки на браузер.

Cookie `cf_clearance` привязан к паре (IP, User-Agent) и живёт ограниченное
время. Когда он протухает, запрос снова упирается в 403 — тогда gate будит
браузер, тот проходит challenge заново, и запрос повторяется. Браузер при этом
не нужно держать «на экране»: окно сдвинуто за пределы рабочего стола
(`--window-position=-2400,-2400`), пользователь его не видит.

Headless-режим проверен отдельно и НЕ работает: Cloudflare отличает его и
задача не решается никогда. Поэтому GATE_HEADLESS по умолчанию 0.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import nodriver as uc
from curl_cffi import requests as curl_requests

from core.config import Settings, get_settings

log = logging.getLogger("gate")

# Признаки того, что мы упёрлись в challenge, а не получили настоящую страницу.
CHALLENGE_MARKERS = (
    "Just a moment",
    "Один момент",
    "cf-mitigated",
    "Выполнение проверки безопасности",
    "Enable JavaScript and cookies",
    "challenge-platform",
)

# Импульсы TLS-отпечатка для curl_cffi: берём первый, который поддерживает
# установленная версия. Важно, чтобы отпечаток был «свежим» — старые Chrome
# Cloudflare отбивает чаще.
IMPERSONATE_CANDIDATES = (
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome",
)


@dataclass
class FetchResult:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    via: str = "curl"  # curl | browser
    challenged: bool = False
    # Cookie, которые сервер выдал этим ответом (сессия трекера: pass, uid…).
    # Берём их из cookie-jar клиента, а не из заголовка Set-Cookie: заголовков
    # бывает несколько и они склеиваются запятыми, на которых ломается разбор
    # (в одном из cookie встречается дата с запятой).
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def text_cp1251(self) -> str:
        """kinozal отдаёт страницы в windows-1251."""
        return self.body.decode("cp1251", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.body)


class ChallengeBlocked(RuntimeError):
    """Challenge не удалось пройти — дальше по сайту идти бессмысленно."""


class LoginFailed(RuntimeError):
    """Сайт ответил «неверный логин/пароль». Это не про Cloudflare."""


def looks_like_challenge(status: int, body: bytes, url: str = "") -> bool:
    if status not in (403, 503):
        if "__cf_chl" in url:
            return True
        return False
    head = body[:4096].decode("utf-8", errors="replace")
    return any(marker in head for marker in CHALLENGE_MARKERS)


def _free_port() -> int:
    """Свободный локальный порт для отладки Chrome."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CloudflareGate:
    """Держит Chrome живым и раздаёт запросы к kinozal.me."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._browser: uc.Browser | None = None
        self._tab: uc.Tab | None = None
        self._ua: str | None = None
        self._clearance_at: float = 0.0
        self._cookies: dict[str, str] = {}
        self._impersonate: str | None = None
        self._browser_lock = asyncio.Lock()
        self._debug_port = _free_port()
        self._chrome_process: asyncio.subprocess.Process | None = None
        self._site_lock = asyncio.Lock()  # один вход в сайт за раз

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self._ensure_browser()

    async def stop(self) -> None:
        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception:  # noqa: BLE001 - остановка не должна падать
                pass
            self._browser = None
            self._tab = None
        if self._chrome_process is not None and self._chrome_process.returncode is None:
            try:
                self._chrome_process.terminate()
                await asyncio.wait_for(self._chrome_process.wait(), timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self._chrome_process.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._chrome_process = None
        # Даже если браузер «остановлен», процесс мог остаться — тогда следующий
        # запуск упрётся в занятый профиль.
        await asyncio.to_thread(self._kill_stale_chrome, self.settings.gate_profile_dir)

    async def _ensure_browser(self) -> uc.Browser:
        if self._browser is not None and self._tab is not None:
            return self._browser
        profile = self.settings.gate_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        # Два процесса не должны запускать Chrome с одним профилем одновременно
        # (например, старый шлюз ещё гасит свой браузер, а новый уже стартует):
        # второй экземпляр передаёт вкладку первому и не поднимает свой порт
        # отладки. Держим межпроцессную блокировку на время запуска.
        import fcntl
        import os as _os

        lock_path = profile / ".launch.lock"

        def take_lock() -> int:
            fd = _os.open(lock_path, _os.O_CREAT | _os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)  # flock возвращает None, fd нужен нам
            return fd

        lock_fd = await asyncio.to_thread(take_lock)
        try:
            return await self._ensure_browser_locked()
        finally:
            def release() -> None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    _os.close(lock_fd)
                except OSError:
                    pass

            await asyncio.to_thread(release)

    async def _ensure_browser_locked(self) -> uc.Browser:
        """Запускает Chrome сам и подключается к уже открытому порту отладки.

        Почему не «просто uc.start()»: nodriver после запуска ждёт порт отладки
        всего ~2,7 секунды (0,25 + 5 × 0,5). Холодный старт Chrome с нашим
        профилем занимает больше, и подключение стабильно падало с «Failed to
        connect to browser», хотя браузер поднимался. Поэтому процесс запускаем
        сами, ждём готовности порта сколько нужно, и только потом подключаемся
        (nodriver это умеет: если заданы host и port, он attach'ится к живому
        браузеру).
        """
        if self._browser is not None and self._tab is not None:
            return self._browser
        profile = self.settings.gate_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._kill_stale_chrome, profile)

        args = [
            self.settings.gate_chrome_path,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={self._debug_port}",
            "--remote-debugging-host=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-features=Translate,OptimizationHints",
            "--window-size=1280,900",
            # Окно за пределами экрана: браузер должен быть «настоящим», но
            # мешать пользователю он не должен.
            "--window-position=-2400,-2400",
        ]
        if self.settings.gate_headless:
            args.insert(1, "--headless=new")
        # Chrome не может инициализировать свою песочницу внутри чужой (например,
        # под песочницей агента), поэтому она отключена — этим браузером
        # управляем только мы, по петле.
        args.append("--no-sandbox")

        last_error: Exception | None = None
        for attempt in range(3):
            log.info(
                "запускаю Chrome для обхода Cloudflare (профиль %s, порт %s, попытка %s/3)",
                profile,
                self._debug_port,
                attempt + 1,
            )
            try:
                if self._chrome_process is not None and self._chrome_process.returncode is None:
                    self._chrome_process.kill()
                    await asyncio.sleep(1)
                self._chrome_process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if not await self._wait_for_debug_port(timeout=45):
                    raise ChallengeBlocked("порт отладки Chrome не открылся за 45 секунд")

                browser = await uc.start(
                    host="127.0.0.1",
                    port=self._debug_port,
                    user_data_dir=str(profile),
                    browser_executable_path=self.settings.gate_chrome_path,
                    sandbox=False,
                )
                tab = await browser.get("about:blank")
                self._browser = browser
                self._tab = tab
                try:
                    self._ua = await tab.evaluate("navigator.userAgent")
                except Exception:  # noqa: BLE001
                    self._ua = None
                log.info("Chrome подключён (порт отладки %s)", self._debug_port)
                return browser
            except Exception as exc:  # noqa: BLE001 - пробуем ещё раз после уборки
                last_error = exc
                log.warning("Chrome не поднялся: %s", str(exc).replace("\n", " ")[-160:])
                self._browser = None
                self._tab = None
                if self._chrome_process is not None and self._chrome_process.returncode is None:
                    self._chrome_process.kill()
                await asyncio.to_thread(self._kill_stale_chrome, profile)
                await asyncio.sleep(2.0 + attempt * 1.5)
        raise ChallengeBlocked(f"не удалось запустить Chrome: {last_error}")

    async def _wait_for_debug_port(self, timeout: float) -> bool:
        """Ждёт, пока Chrome откроет порт отладки (и ответит по HTTP)."""
        import httpx

        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{self._debug_port}/json/version"
        async with httpx.AsyncClient(timeout=3) as client:
            while time.time() < deadline:
                try:
                    response = await client.get(url)
                    if response.status_code == 200 and "webSocketDebuggerUrl" in response.text:
                        return True
                except Exception:  # noqa: BLE001 - порт ещё закрыт
                    pass
                if self._chrome_process is not None and self._chrome_process.returncode is not None:
                    log.warning("Chrome завершился с кодом %s", self._chrome_process.returncode)
                    return False
                await asyncio.sleep(0.4)
        return False

    @staticmethod
    def _kill_stale_chrome(profile: Path) -> None:
        """Убивает только Chrome, запущенный с НАШИМ профилем.

        Чужой браузер пользователя не трогаем: фильтр — точный путь
        `--user-data-dir=<профиль>`.
        """
        import os
        import signal
        import subprocess

        # ВАЖНО: без ведущих "--". BSD-шный pgrep принимает шаблон, начинающийся
        # с дефиса, за опцию ("illegal option -- -"), молча ничего не находит —
        # и осиротевший Chrome остаётся держать профиль, из-за чего следующий
        # запуск падает с «Failed to connect to browser».
        marker = f"user-data-dir={profile}"
        try:
            listing = subprocess.run(
                ["pgrep", "-f", marker], capture_output=True, text=True, timeout=10
            )
        except Exception:  # noqa: BLE001
            return
        pids = [int(pid) for pid in listing.stdout.split() if pid.strip().isdigit()]
        if not pids:
            return
        log.info("останавливаю прошлый Chrome с нашим профилем: %s", pids)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
        for _ in range(20):
            alive = []
            for pid in pids:
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except OSError:
                    continue
            if not alive:
                return
            time.sleep(0.25)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
        # Chrome держит в профиле SingletonLock/SingletonCookie/SingletonSocket.
        # Если процесс убили не по-хорошему, эти файлы остаются и следующий
        # запуск передаёт вкладку «мёртвому» экземпляру: свой порт отладки он не
        # поднимает, и подключение падает с «Failed to connect to browser».
        time.sleep(0.5)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (profile / name).unlink()
            except OSError:
                continue

    async def _get_tab(self) -> uc.Tab:
        await self._ensure_browser()
        assert self._tab is not None
        return self._tab

    # ------------------------------------------------------------------
    # Cookie / clearance
    # ------------------------------------------------------------------
    async def export_cookies(self) -> dict[str, str]:
        await self._ensure_browser()
        assert self._browser is not None
        cookies = await self._browser.cookies.get_all()
        self._cookies = {c.name: c.value for c in cookies if c.value}
        return dict(self._cookies)

    @property
    def user_agent(self) -> str:
        return self._ua or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        )

    def _pick_impersonate(self) -> str:
        if self._impersonate:
            return self._impersonate
        for name in IMPERSONATE_CANDIDATES:
            try:
                curl_requests.get("https://example.com", impersonate=name, timeout=1)
            except Exception:  # noqa: BLE001 - сеть не важна, важна поддержка имени
                continue
            self._impersonate = name
            return name
        self._impersonate = "chrome"
        return self._impersonate

    def _session(self) -> curl_requests.Session:
        session = curl_requests.Session(impersonate=self._pick_impersonate())
        origin = self.settings.kinozal_base_url
        for name, value in self._cookies.items():
            session.cookies.set(name, value, domain=".kinozal.me", path="/")
        session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": origin + "/",
            }
        )
        return session

    async def ensure_clearance(self, force: bool = False) -> None:
        """Гарантирует, что у нас есть рабочий cf_clearance."""
        fresh_for = 600.0  # 10 минут считаем cookie свежим и браузер не трогаем
        if not force and self._cookies.get("cf_clearance") and (
            time.time() - self._clearance_at < fresh_for
        ):
            return
        async with self._browser_lock:
            if not force and self._cookies.get("cf_clearance") and (
                time.time() - self._clearance_at < fresh_for
            ):
                return
            tab = await self._get_tab()
            target = self.settings.kinozal_base_url + "/"
            ok = await self._pass_challenge(tab, target)
            if not ok:
                raise ChallengeBlocked("Cloudflare не пропустил даже главную страницу")
            await self.export_cookies()
            self._clearance_at = time.time()
            log.info("cf_clearance получен (cookies: %s)", ", ".join(sorted(self._cookies)))

    async def _pass_challenge(self, tab: uc.Tab, url: str, timeout: float = 45.0) -> bool:
        """Открывает страницу в браузере и ждёт, пока challenge решится."""
        deadline = time.time() + timeout
        try:
            await tab.get(url)
        except Exception as exc:  # noqa: BLE001 - таймауты загрузки не смертельны
            log.debug("навигация %s: %s", url, exc)
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            try:
                html = await tab.get_content()
            except Exception:  # noqa: BLE001
                continue
            current = tab.url or ""
            if "__cf_chl" in current:
                continue
            if not looks_like_challenge(200, html.encode("utf-8", "replace"), current):
                return True
        return False

    async def refresh_clearance(self, url: str | None = None) -> None:
        async with self._browser_lock:
            tab = await self._get_tab()
            target = url or (self.settings.kinozal_base_url + "/")
            ok = await self._pass_challenge(tab, target)
            if not ok:
                # вторая попытка: иногда первая упирается в «retry token»
                ok = await self._pass_challenge(tab, self.settings.kinozal_base_url + "/")
            await self.export_cookies()
            self._clearance_at = time.time()
            if not ok:
                raise ChallengeBlocked(f"Cloudflare не пропустил {target}")

    # ------------------------------------------------------------------
    # Запросы
    # ------------------------------------------------------------------
    async def fetch(
        self,
        url: str,
        method: str = "GET",
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        browser_fallback: bool = True,
        retries: int = 2,
        timeout: float = 45.0,
    ) -> FetchResult:
        """GET/POST к kinozal.me с автоматическим прохождением challenge."""
        await self.ensure_clearance()
        last: FetchResult | None = None
        for attempt in range(retries + 1):
            result = await self._curl(url, method, data, headers, timeout)
            if not looks_like_challenge(result.status, result.body, result.url):
                self._remember_cookies(result)
                if result.cookies:
                    self._cookies.update(result.cookies)
                return result
            last = result
            log.info("challenge на %s (попытка %s), обновляю cf_clearance", url, attempt + 1)
            await self.refresh_clearance(url)
        if browser_fallback:
            result = await self._browser_fetch(url, method, data, headers)
            if not looks_like_challenge(result.status, result.body, result.url):
                await self.export_cookies()
                return result
            last = result
        return last or FetchResult(status=0, url=url, challenged=True)

    async def _curl(
        self,
        url: str,
        method: str,
        data: dict[str, str] | None,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> FetchResult:
        def run() -> FetchResult:
            session = self._session()
            try:
                if method.upper() == "POST":
                    response = session.post(
                        url,
                        data=data,
                        headers=headers or {},
                        timeout=timeout,
                        allow_redirects=True,
                    )
                else:
                    response = session.get(
                        url, headers=headers or {}, timeout=timeout, allow_redirects=True
                    )
                try:
                    issued = {k: v for k, v in session.cookies.items() if v}
                except Exception:  # noqa: BLE001 - форма cookie-jar может отличаться
                    issued = {}
                return FetchResult(
                    status=response.status_code,
                    url=str(response.url),
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=response.content,
                    via="curl",
                    cookies=issued,
                )
            finally:
                session.close()

        return await asyncio.to_thread(run)

    async def _browser_fetch(
        self,
        url: str,
        method: str,
        data: dict[str, str] | None,
        headers: dict[str, str] | None,
    ) -> FetchResult:
        """Запасной путь: тот же запрос делает сам Chrome (он точно не бот)."""
        async with self._browser_lock:
            tab = await self._get_tab()
            if method.upper() == "POST":
                # Форма отправляется страницей: так запрос выглядит ровно как
                # нажатие кнопки в браузере.
                fields = {k: v for k, v in (data or {}).items()}
                js = """
                async (args) => {
                    const [url, fields] = args;
                    const body = new URLSearchParams(fields);
                    const r = await fetch(url, {
                        method: 'POST', body,
                        credentials: 'include',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'}
                    });
                    const t = await r.text();
                    return {status: r.status, url: r.url, text: t};
                }
                """
                try:
                    payload = await tab.evaluate(js, [url, fields], await_promise=True)
                except Exception as exc:  # noqa: BLE001
                    return FetchResult(status=0, url=url, via="browser", body=str(exc).encode())
                if not payload:
                    return FetchResult(status=0, url=url, via="browser")
                return FetchResult(
                    status=int(payload.get("status") or 0),
                    url=str(payload.get("url") or url),
                    body=str(payload.get("text") or "").encode(),
                    via="browser",
                )
            ok = await self._pass_challenge(tab, url)
            html = await tab.get_content()
            return FetchResult(
                status=200 if ok else 403,
                url=tab.url or url,
                body=html.encode("utf-8", "replace"),
                via="browser",
                challenged=not ok,
            )

    def _remember_cookies(self, result: FetchResult) -> None:
        """Запасной путь: разбирает Set-Cookie, если cookie-jar ничего не дал."""
        raw = result.headers.get("set-cookie")
        if not raw:
            return
        for chunk in raw.split(","):
            part = chunk.split(";")[0].strip()
            if "=" in part:
                name, _, value = part.partition("=")
                self._cookies[name.strip()] = value.strip()

    # ------------------------------------------------------------------
    # Логин на трекер
    # ------------------------------------------------------------------
    async def login(self, force: bool = False) -> dict[str, str]:
        """Входит на kinozal.me и складывает cookies в браузерный профиль.

        Логин делается НАСТОЯЩИМ Chrome: POST на /takelogin.php для curl_cffi
        Cloudflare отбивает challenge'ом, а браузерная отправка формы проходит.
        """
        async with self._site_lock:
            if not force and await self.is_logged_in():
                return dict(self._cookies)
            await self.ensure_clearance()
            username = self.settings.kinozal_username
            password = self.settings.kinozal_password
            if not username or not password:
                raise LoginFailed("KINOZAL_USERNAME/KINOZAL_PASSWORD не заданы в .env")

            # Быстрый путь: обычный POST на /takelogin.php. Cloudflare пропускает
            # его, если cf_clearance свежий, — и это в разы быстрее браузера.
            try:
                cookies = await self._login_http(username, password)
                if cookies:
                    return cookies
            except LoginFailed:
                raise
            except Exception as exc:  # noqa: BLE001 - падаем в браузерный путь
                log.warning("быстрый вход не удался (%s), пробую через браузер", exc)

            async with self._browser_lock:
                tab = await self._get_tab()
                last_note = ""
                for attempt in range(2):
                    login_page = self.settings.kinozal_base_url + "/login.php"
                    await self._pass_challenge(tab, login_page)
                    if not await self._fill_login(tab, username, password):
                        last_note = "не нашёл форму входа"
                        log.warning("login.php: %s (попытка %s)", last_note, attempt + 1)
                        continue
                    await self._submit_login(tab)
                    for _ in range(24):
                        await asyncio.sleep(1.5)
                        html = await self._safe_content(tab)
                        if "Выход" in html and "takelogin" not in (tab.url or ""):
                            await self.export_cookies()
                            log.info("вход выполнен")
                            return dict(self._cookies)
                        # Сайт сам говорит, что не так — не крутим Cloudflare зря.
                        problem = self._login_error(html)
                        if problem:
                            raise LoginFailed(problem)
                        if "__cf_chl" in (tab.url or ""):
                            continue
                    last_note = "после отправки формы вход не состоялся"
                    log.warning("login.php: %s", last_note)
                raise LoginFailed(
                    last_note or "не удалось войти на kinozal.me — проверьте логин и пароль"
                )

    @staticmethod
    def _login_error(html: str) -> str:
        """Вытаскивает понятную причину отказа из страницы входа."""
        for marker in (
            "Неверно указан пароль",
            "Неверно указан логин",
            "неверный пароль",
            "Неверный пароль",
            "Аккаунт заблокирован",
            "ваш аккаунт заблокирован",
        ):
            index = html.find(marker)
            if index != -1:
                tail = html[index : index + 160]
                tail = re.sub(r"<[^>]+>", " ", tail)
                return re.sub(r"\s+", " ", tail).strip() or marker
        return ""

    async def _login_http(self, username: str, password: str) -> dict[str, str] | None:
        """Вход обычным POST-запросом. None — если Cloudflare помешал."""
        result = await self.fetch(
            self.settings.kinozal_base_url + "/takelogin.php",
            method="POST",
            data={"username": username, "password": password, "touser": "1"},
            retries=0,
        )
        html = result.text_cp1251()
        problem = self._login_error(html)
        if problem:
            raise LoginFailed(problem)
        if result.challenged:
            return None
        if "pass" in self._cookies or "uid" in self._cookies:
            log.info("вход выполнен прямым POST-запросом")
            return dict(self._cookies)
        # Иногда сессию выставляет браузер (когда запрос ушёл через него).
        await self.export_cookies()
        if "pass" in self._cookies or "uid" in self._cookies:
            log.info("вход выполнен, cookie забраны из браузера")
            return dict(self._cookies)
        return None

    async def _fill_login(self, tab: uc.Tab, username: str, password: str) -> bool:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                user_el = await tab.query_selector("input#username")
                pass_el = await tab.query_selector("input#password")
            except Exception:  # noqa: BLE001
                user_el = pass_el = None
            if user_el and pass_el:
                try:
                    await user_el.click()
                    await user_el.send_keys(username)
                    await pass_el.click()
                    await pass_el.send_keys(password)
                    return True
                except Exception as exc:  # noqa: BLE001
                    log.debug("не удалось заполнить форму: %s", exc)
                    return False
            await asyncio.sleep(1.5)
        return False

    async def _submit_login(self, tab: uc.Tab) -> bool:
        try:
            button = await tab.query_selector("input[type=submit]")
            if button:
                await button.click()
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            await tab.evaluate("document.forms[0].submit()")
        except Exception:  # noqa: BLE001
            return False
        return False

    async def _safe_content(self, tab: uc.Tab) -> str:
        try:
            return await tab.get_content()
        except Exception:  # noqa: BLE001
            return ""

    async def is_logged_in(self) -> bool:
        if "pass" not in self._cookies and "uid" not in self._cookies:
            await self.export_cookies()
        if "pass" not in self._cookies and "uid" not in self._cookies:
            return False
        try:
            result = await self.fetch(self.settings.kinozal_base_url + "/", retries=1)
        except Exception:  # noqa: BLE001
            return False
        return "Выход" in result.text_cp1251()

    # ------------------------------------------------------------------
    # Утилиты для сервиса
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "browser": "up" if self._browser is not None else "down",
            "ua": self.user_agent,
            "impersonate": self._impersonate,
            "cookies": sorted(self._cookies),
            "clearance_age_sec": round(time.time() - self._clearance_at, 1)
            if self._clearance_at
            else None,
            "logged_in": "pass" in self._cookies or "uid" in self._cookies,
        }
