/**
 * kinozal-mcp — форк https://github.com/tokezooo/kinozal-mcp под kinozal.me.
 *
 * Что изменено относительно оригинала (и почему):
 *
 * 1. BASE_URL: kinozal.tv -> kinozal.me (зеркало из задачи).
 * 2. Транспорт. Оригинал ходил node-fetch'ем напрямую. На kinozal.me стоит
 *    managed-challenge Cloudflare: любой не-браузерный клиент получает
 *    403 «Just a moment…». Поэтому все запросы идут через локальный шлюз
 *    (GATE_URL, по умолчанию http://127.0.0.1:8123), который держит настоящий
 *    Chrome и раздаёт cookie cf_clearance. Парсинг остался здесь, в Node.
 * 3. Кодировка. kinozal отдаёт windows-1251; шлюз уже перекодировал страницу
 *    в utf-8, поэтому decodeWindows1251 здесь больше не нужен.
 * 4. download_torrent. Вход на трекер делает шлюз (POST-форму Cloudflare
 *    пропускает только из браузера), а файл качается тем же шлюзом в base64 —
 *    результат проверяется на bencode, чтобы вместо .torrent не сохранилась
 *    HTML-страница «Вход».
 */
import * as cheerio from "cheerio";
import { writeFile } from "fs/promises";

const BASE_URL = (process.env.KINOZAL_BASE_URL || "https://kinozal.me").replace(/\/+$/, "");
const GATE_URL = (process.env.KINOZAL_GATE_URL || "http://127.0.0.1:8123").replace(/\/+$/, "");

export interface TorrentSearchResult {
  id: string;
  title: string;
  size: string;
  seeds: number;
  peers: number;
  comments: number;
  uploadDate: string;
  uploader: string;
  category: string;
}

export interface TorrentDetails {
  id: string;
  title: string;
  originalTitle: string;
  year: string;
  genre: string;
  director: string;
  cast: string;
  description: string;
  quality: string;
  video: string;
  audio: string;
  size: string;
  seeds: number;
  peers: number;
  uploadDate: string;
  updateDate: string;
}

interface GateResponse {
  status: number;
  url: string;
  text?: string;
  body_b64?: string;
  bytes: number;
  via?: string;
  headers?: Record<string, string>;
}

/** Запрос через шлюз Cloudflare (единственный способ увидеть страницы kinozal.me). */
async function gateFetch(
  path: string,
  options: {
    method?: string;
    data?: Record<string, string>;
    binary?: boolean;
    retries?: number;
  } = {}
): Promise<GateResponse> {
  const url = path.startsWith("http") ? path : `${BASE_URL}${path}`;
  const response = await fetch(`${GATE_URL}/fetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      method: options.method || "GET",
      data: options.data,
      binary: Boolean(options.binary),
      retries: options.retries ?? 2,
      headers: {},
    }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`шлюз Cloudflare вернул ${response.status}: ${detail.slice(0, 300)}`);
  }
  return (await response.json()) as GateResponse;
}

/** Поиск раздач на kinozal.me. */
export async function searchTorrents(
  query: string,
  page: number = 0
): Promise<TorrentSearchResult[]> {
  const url = `/browse.php?s=${encodeURIComponent(query)}&page=${page}`;
  const response = await gateFetch(url);
  if (response.status !== 200) {
    throw new Error(`kinozal.me ответил ${response.status} на поиск «${query}»`);
  }
  const html = response.text || "";
  if (/Нет активных раздач|Найдено 0 раздач/i.test(html)) {
    return [];
  }

  const $ = cheerio.load(html);
  const results: TorrentSearchResult[] = [];

  $("table.t_peer tr.bg, table.t_peer tr.first").each((_, element) => {
    const $row = $(element);
    const $cells = $row.find("td");
    if ($cells.length < 8) return;

    const $titleCell = $cells.eq(1);
    const $link = $titleCell.find("a");
    const href = $link.attr("href") || "";
    const idMatch = href.match(/id=(\d+)/);
    if (!idMatch) return;

    const categoryImg = $cells.eq(0).find("img").attr("src") || "";
    const categoryMatch = categoryImg.match(/cat\/(\d+)\.gif/);

    results.push({
      id: idMatch[1],
      title: $link.text().trim(),
      comments: parseInt($cells.eq(2).text().trim(), 10) || 0,
      size: $cells.eq(3).text().trim(),
      seeds: parseInt($cells.eq(4).text().trim(), 10) || 0,
      peers: parseInt($cells.eq(5).text().trim(), 10) || 0,
      uploadDate: $cells.eq(6).text().trim(),
      uploader: $cells.eq(7).find("a").text().trim(),
      category: categoryMatch ? categoryMatch[1] : "unknown",
    });
  });

  return results;
}

/** Разбирает блок «Ключ: значение», разделённый <br/>, в объект. */
function parseLabelBlock($: cheerio.CheerioAPI, selector: string): Record<string, string> {
  const block = $(selector).first();
  if (!block.length) return {};
  const html = (block.html() || "").replace(/<br\s*\/?>/gi, "\n");
  const text = cheerio.load(`<div>${html}</div>`).root().text();
  const out: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const match = line.match(/^([^:]{2,40}):\s*(.+)$/);
    if (match) out[match[1].trim()] = match[2].trim();
  }
  return out;
}

/**
 * Подробности раздачи по id.
 *
 * Разметка kinozal.me: вся «шапка» (название, год, жанр, режиссёр) — это один
 * <h2> с <br/>, техданные — блок #tabs, а размер/сиды/пиры/даты лежат в
 * боковом <ul> отдельными <li>. Поэтому парсим блоками, а не поиском подстроки
 * (иначе значение «Год выпуска» превращалось во весь текст шапки).
 */
export async function getTorrentDetails(id: string): Promise<TorrentDetails> {
  const response = await gateFetch(`/details.php?id=${id}`);
  if (response.status !== 200) {
    throw new Error(`kinozal.me ответил ${response.status} на details.php?id=${id}`);
  }
  const $ = cheerio.load(response.text || "");
  const head = parseLabelBlock($, "div.bx1 h2");
  const tech = parseLabelBlock($, "#tabs");
  const side = (label: string): string => {
    const node = $("li").filter((_, element) => $(element).text().includes(label)).first();
    return node.find(".floatright").first().text().trim();
  };

  const descriptionNode = $("p")
    .filter((_, element) => $(element).text().trim().startsWith("О фильме:"))
    .first();

  return {
    id,
    title: head["Название"] || tech["Название"] || "",
    originalTitle: head["Оригинальное название"] || "",
    year: head["Год выпуска"] || "",
    genre: head["Жанр"] || "",
    director: head["Режиссер"] || "",
    cast: head["В ролях"] || "",
    description: descriptionNode.text().replace(/^О фильме:\s*/, "").trim().slice(0, 2000),
    quality: tech["Качество"] || "",
    video: tech["Видео"] || "",
    audio: tech["Аудио"] || "",
    size: tech["Размер"] || side("Вес"),
    seeds: parseInt(side("Раздают"), 10) || 0,
    peers: parseInt(side("Скачивают"), 10) || 0,
    uploadDate: side("Залит"),
    updateDate: side("Обновлен"),
  };
}

/**
 * Скачивание .torrent.
 *
 * Логин выполняет шлюз: форму входа Cloudflare пропускает только из настоящего
 * браузера. Затем файл забирается тем же шлюзом в base64 и проверяется: он
 * обязан начинаться с 'd' (bencode-словарь). Иначе вместо торрента пришла
 * HTML-страница (например, «Вход»), и лучше честно вернуть ошибку.
 */
export async function downloadTorrent(
  id: string,
  outputPath: string,
  _username?: string,
  _password?: string
): Promise<{ success: boolean; message: string; path?: string }> {
  try {
    const loginResponse = await fetch(`${GATE_URL}/login`, { method: "POST" });
    if (!loginResponse.ok) {
      const detail = await loginResponse.text();
      if (loginResponse.status === 401) {
        return {
          success: false,
          message:
            "kinozal.me не принял логин или пароль. Откройте «Доступы» в интерфейсе " +
            `агента и проверьте их. Ответ сайта: ${detail.slice(0, 200)}`,
        };
      }
      return {
        success: false,
        message: `не удалось войти на kinozal.me: ${detail.slice(0, 300)}`,
      };
    }
    const login = (await loginResponse.json()) as { ok?: boolean; cookies?: string[] };
    if (!login.ok) {
      return {
        success: false,
        message:
          "не удалось войти на kinozal.me: сервер не выдал сессию. " +
          "Проверьте логин и пароль в настройках агента.",
      };
    }

    const response = await gateFetch(`/download.php?id=${id}`, { binary: true, retries: 1 });
    if (response.status !== 200 || !response.body_b64) {
      return { success: false, message: `download.php вернул ${response.status}` };
    }
    const buffer = Buffer.from(response.body_b64, "base64");
    if (buffer.length === 0) {
      return { success: false, message: "download.php вернул пустой ответ" };
    }
    if (buffer[0] !== 0x64 /* 'd' */) {
      // Вместо .torrent трекер отдаёт страницу с уведомлением: чаще всего
      // «Вам недоступен торрент-файл — использовано количество в сутки»,
      // реже — просьбу войти. Вытаскиваем суть, чтобы агент сказал по-русски,
      // а не показывал человеку кусок HTML.
      const notice = extractNotice(buffer);
      return {
        success: false,
        message: notice || "трекер вернул страницу вместо torrent-файла",
      };
    }
    await writeFile(outputPath, buffer);
    return {
      success: true,
      message: `torrent сохранён (${buffer.length} байт)`,
      path: outputPath,
    };
  } catch (error) {
    return {
      success: false,
      message: `ошибка: ${error instanceof Error ? error.message : String(error)}`,
    };
  }
}

/**
 * Достаёт из страницы-заглушки фразу-объяснение, почему вместо .torrent
 * пришёл HTML: «Вам недоступен торрент-файл…», «Войдите на сайт…» и т.п.
 */
function extractNotice(buffer: Buffer): string {
  let text = "";
  try {
    text = new TextDecoder("windows-1251").decode(buffer);
  } catch {
    text = buffer.toString("utf8");
  }
  text = text
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    // Конец строки/блока — граница предложения: иначе «…скачивания Вы
    // использовали…» склеивается в одну нечитаемую фразу.
    .replace(/<br\s*\/?>/gi, ". ")
    .replace(/<\/(p|div|li|td|tr|h\d)>/gi, ". ")
    // Заголовок-уведомление у kinozal оформлен <span class=u9>, а следом идёт
    // <div class=pad5x5> с причиной: граница span→div — конец предложения.
    .replace(/<\/span>\s*(?=<div)/gi, ". ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/\s+/g, " ")
    .replace(/(?:\.\s*){2,}/g, ". ")
    .replace(/\s+\./g, ".")
    .trim();

  const markers = ["Вам недоступен", "Вы использовали", "Войдите", "Вход", "Недостаточно прав"];
  for (const marker of markers) {
    const at = text.indexOf(marker);
    if (at === -1) continue;
    // Берём одну-две первые фразы: весь футер страницы человеку не нужен.
    const tail = text.slice(at, at + 400);
    let out = "";
    for (const sentence of tail.split(/(?<=\.)\s+/)) {
      if (out && out.length + sentence.length > 180) break;
      out = out ? `${out} ${sentence}` : sentence;
      if (out.length > 60) break;
    }
    return out.trim().slice(0, 300);
  }
  return "";
}

export { BASE_URL, GATE_URL };
