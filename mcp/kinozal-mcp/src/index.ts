#!/usr/bin/env node
/**
 * MCP-сервер kinozal-mcp (форк под kinozal.me).
 *
 * Инструменты те же, что в оригинале:
 *   search_torrents(query, page)          — поиск раздач;
 *   get_torrent_details(id)               — подробности раздачи;
 *   download_torrent(id, outputPath, ..)  — скачать .torrent на диск.
 *
 * Единственное отличие внутри — весь HTTP идёт через локальный шлюз Cloudflare
 * (см. src/kinozal.ts). Агенту (core/mcp_client.py) этот файл отдаёт обычный
 * MCP-протокол по stdio, поэтому UI и агент ничего не знают про Cloudflare.
 */
import { config } from "dotenv";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { BASE_URL, GATE_URL, searchTorrents, getTorrentDetails, downloadTorrent } from "./kinozal.js";

config();

const server = new Server(
  { name: "kinozal-mcp", version: "1.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: "search_torrents",
        description:
          "Поиск раздач на kinozal.me по строке запроса. Возвращает список раздач " +
          "с названием (в названии видны сезон, серии, озвучка и качество), размером, " +
          "сидами, пирами, датой заливки и id.",
        inputSchema: {
          type: "object",
          properties: {
            query: { type: "string", description: "Строка поиска (фильм, сериал, музыка)" },
            page: { type: "number", description: "Страница выдачи, начиная с 0", default: 0 },
          },
          required: ["query"],
        },
      },
      {
        name: "get_torrent_details",
        description: "Подробная информация о раздаче по её id (жанр, качество, аудио, размер, сиды).",
        inputSchema: {
          type: "object",
          properties: { id: { type: "string", description: "id раздачи с kinozal.me" } },
          required: ["id"],
        },
      },
      {
        name: "download_torrent",
        description:
          "Скачать .torrent по id раздачи в указанный путь. Вход на трекер уже выполнен " +
          "на стороне шлюза, логин/пароль передавать не нужно.",
        inputSchema: {
          type: "object",
          properties: {
            id: { type: "string", description: "id раздачи с kinozal.me" },
            outputPath: { type: "string", description: "Путь, куда сохранить .torrent" },
          },
          required: ["id", "outputPath"],
        },
      },
    ],
  };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  if (!args) {
    throw new Error("Missing arguments");
  }

  try {
    switch (name) {
      case "search_torrents": {
        const query = String(args.query);
        const page = args.page ? Number(args.page) : 0;
        const results = await searchTorrents(query, page);
        return {
          content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
        };
      }

      case "get_torrent_details": {
        const details = await getTorrentDetails(String(args.id));
        return {
          content: [{ type: "text", text: JSON.stringify(details, null, 2) }],
        };
      }

      case "download_torrent": {
        const id = String(args.id);
        const outputPath = String(args.outputPath);
        const result = await downloadTorrent(
          id,
          outputPath,
          args.username ? String(args.username) : process.env.KINOZAL_USERNAME,
          args.password ? String(args.password) : process.env.KINOZAL_PASSWORD
        );
        return {
          content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
          isError: !result.success,
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : String(error);
    return {
      content: [{ type: "text", text: `Ошибка: ${errorMessage}` }],
      isError: true,
    };
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`kinozal-mcp запущен: ${BASE_URL} через шлюз ${GATE_URL}`);
}

main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
