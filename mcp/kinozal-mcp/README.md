# kinozal-mcp (форк под kinozal.me)

MCP-сервер для торрент-трекера **kinozal.me**. Основа —
[tokezooo/kinozal-mcp](https://github.com/tokezooo/kinozal-mcp), переработанный
под зеркало `kinozal.me` и под обход Cloudflare.

## Что изменено относительно оригинала

| Что | Было | Стало |
|---|---|---|
| Адрес трекера | `kinozal.tv` | `kinozal.me` (`KINOZAL_BASE_URL`) |
| Транспорт | `node-fetch` напрямую | через локальный шлюз `KINOZAL_GATE_URL` (по умолчанию `http://127.0.0.1:8123`) |
| Вход на трекер | POST из Node | выполняет шлюз: Cloudflare пропускает форму входа только из браузера |
| Скачивание | сохранение как есть | файл дополнительно проверяется на bencode (`d…`), иначе возвращается понятная ошибка вместо HTML-страницы «Вход» |
| Разбор страницы раздачи | поиск подстрок | блоки `<h2>`, `#tabs` и боковой список (разметка `.me`) |

Зачем шлюз: `browse.php`, `details.php`, `login.php` и `download.php` на
kinozal.me закрыты managed-challenge Cloudflare — любой не-браузерный клиент
получает `403` и страницу «Just a moment…».

## Инструменты

| Инструмент | Аргументы | Что делает |
|---|---|---|
| `search_torrents` | `query`, `page` | поиск раздач: название (в нём видны сезон, серии, озвучка, качество), размер, сиды/пиры, дата заливки, id |
| `get_torrent_details` | `id` | подробности раздачи: год, жанр, режиссёр, качество, видео, аудио, размер, сиды |
| `download_torrent` | `id`, `outputPath` | скачивает `.torrent` по указанному пути |

Логин и пароль трекера передавать не нужно — их знает шлюз (он берёт их из
настроек агента).

## Сборка и запуск

```bash
cd mcp/kinozal-mcp
npm install
npm run build

# запуск как обычного MCP-сервера по stdio
KINOZAL_BASE_URL=https://kinozal.me \
KINOZAL_GATE_URL=http://127.0.0.1:8123 \
node build/index.js
```

Отдельно этот сервер нужен редко: агент запускает его сам и общается по MCP
(`core/mcp_client.py`).
