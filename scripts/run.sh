#!/usr/bin/env bash
#
# Запуск агента «Кинозал».
#
# Поднимаются ДВА процесса:
#
#   1. шлюз Cloudflare (services.gate_service)  — 127.0.0.1:8123
#      Держит настоящий Chrome: без него kinozal.me отвечает 403 «Just a moment…».
#      Наружу не выставляется, ходит по сайту под нашей учёткой.
#
#   2. сайт (web.app) — 0.0.0.0:8000 по HTTPS
#      Слушает 0.0.0.0, иначе проброс порта на роутере (внешний адрес → этот мак)
#      упирается в «connection refused», хотя сервис работает.
#
# HTTPS без домена: сертификат самоподписанный, браузер один раз предупредит.
# В сертификат вписаны адреса, по которым сайт открывают (петля, адрес в
# домашней сети). Появится домен — вместо самоподписанного ставится настоящий.
#
# Использование:
#   tools/run.sh start              # поднять в фоне и напечатать адреса
#   tools/run.sh start --foreground # то же, но в текущем окне терминала
#   tools/run.sh stop | restart | status | logs
#   tools/run.sh cert               # перевыпустить самоподписанный сертификат
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
RUN_DIR="$ROOT/data/run"
LOG_DIR="$ROOT/data/logs"
TLS_DIR="$ROOT/data/tls"
CERT_FILE="$TLS_DIR/site.crt"
KEY_FILE="$TLS_DIR/site.key"
WEB_PID="$RUN_DIR/web.pid"
GATE_PID="$RUN_DIR/gate.pid"
WEB_LOG="$LOG_DIR/web.log"
GATE_LOG="$LOG_DIR/gate.log"

mkdir -p "$RUN_DIR" "$LOG_DIR" "$TLS_DIR" "$ROOT/data/torrents"

# Значение настройки: сначала окружение, потом .env.
setting() {
  local key="$1" default="${2:-}"
  local value="${!key:-}"
  if [ -n "$value" ]; then printf '%s' "$value"; return; fi
  if [ -f "$ROOT/.env" ]; then
    value="$(grep -E "^${key}=" "$ROOT/.env" | tail -1 | cut -d= -f2- || true)"
    if [ -n "$value" ]; then printf '%s' "$value"; return; fi
  fi
  printf '%s' "$default"
}

SITE_HOST="$(setting SITE_HOST 0.0.0.0)"
SITE_PORT="$(setting SITE_PORT 8000)"
SITE_TLS="$(setting SITE_TLS 1)"
SITE_TLS_NAMES="$(setting SITE_TLS_NAMES 127.0.0.1,localhost)"
GATE_HOST="$(setting GATE_HOST 127.0.0.1)"
GATE_PORT="$(setting GATE_PORT 8123)"
PUBLIC_URL="$(setting PUBLIC_URL https://127.0.0.1:8000)"

# ----------------------------------------------------------------------
ensure_cert() {
  mkdir -p "$TLS_DIR"
  if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
    # Перевыпуск, если список адресов в сертификате не совпадает с настройками.
    local current
    current="$(openssl x509 -in "$CERT_FILE" -noout -ext subjectAltName 2>/dev/null | tail -1 | tr -d ' ' || true)"
    local expected=""
    IFS=',' read -ra names <<< "$SITE_TLS_NAMES"
    for name in "${names[@]}"; do
      name="$(echo "$name" | tr -d ' ')"
      if [[ "$name" =~ ^[0-9.]+$ ]]; then
        expected+="IPAddress:${name},"
      else
        expected+="DNS:${name},"
      fi
    done
    # openssl печатает строку SAN без префикса, поэтому сравниваем с ней как есть.
    # Раньше здесь стояло сравнение с "subjectAltName=…" — оно не совпадало
    # никогда, и сертификат молча перевыпускался при каждом запуске. Из-за этого
    # слетало доверие к нему в браузере (доверяли одному сертификату, а сервер
    # отдавал новый).
    if [ "$current" = "${expected%,}" ] || [ -z "$current" ]; then
      echo "сертификат на месте: $CERT_FILE"
      return
    fi
    echo "адреса в сертификате изменились — перевыпускаю"
  fi

  local san=""
  IFS=',' read -ra names <<< "$SITE_TLS_NAMES"
  for name in "${names[@]}"; do
    name="$(echo "$name" | tr -d ' ')"
    [ -z "$name" ] && continue
    if [[ "$name" =~ ^[0-9.]+$ ]]; then
      san+="IP:${name},"
    else
      san+="DNS:${name},"
    fi
  done
  san="${san%,}"
  echo "выпускаю самоподписанный сертификат для: $san"
  openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
    -keyout "$KEY_FILE" -out "$CERT_FILE" \
    -subj "/CN=kinozal-agent" \
    -addext "subjectAltName=${san}" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" >/dev/null 2>&1
  chmod 600 "$KEY_FILE"
  echo "готово: $CERT_FILE"
}

# ----------------------------------------------------------------------
# Запуск под caffeinate: пока агент работает, система не засыпает от простоя, а
# внешний диск не уходит в сон. Без этого macOS через 10 минут тишины паркует
# диск (pmset disksleep 10): Transmission блокируется на записи, первый же
# запрос RPC отваливается по таймауту, и после простоя всё «тормозит».
# Отключается строкой CAFFEINATE=0 в .env.
keep_awake_prefix() {
  local value
  value="$(setting CAFFEINATE 1)"
  if [ "$value" = "0" ] || ! command -v caffeinate >/dev/null 2>&1; then
    return 0
  fi
  printf 'caffeinate -i -m'
}

# background start: пишет pid в файл и подменяет себя нужной командой, чтобы
# pid в файле был именно у процесса (иначе остановка убивала бы caffeinate,
# а сервер оставался жить).
start_bg() {
  local pid_file="$1" log_file="$2"
  shift 2
  local prefix
  prefix="$(keep_awake_prefix)"
  if [ -n "$prefix" ]; then
    # shellcheck disable=SC2086
    nohup $prefix bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$pid_file" "$@" \
      >>"$log_file" 2>&1 &
  else
    nohup "$@" >>"$log_file" 2>&1 &
    echo $! > "$pid_file"
  fi
}

is_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
pid_of() { [ -f "$1" ] && cat "$1" || echo ""; }

stop_one() {
  local pid_file="$1" name="$2"
  if is_running "$pid_file"; then
    local pid; pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
    echo "остановлен $name (pid $pid)"
  else
    echo "$name не запущен"
  fi
  rm -f "$pid_file"
}

start_gate() {
  if is_running "$GATE_PID"; then
    echo "шлюз уже работает (pid $(pid_of "$GATE_PID"))"
    return
  fi
  echo "поднимаю шлюз Cloudflare на ${GATE_HOST}:${GATE_PORT} (Chrome стартует при первом запросе)"
  start_bg "$GATE_PID" "$GATE_LOG" "$PY" -m uvicorn services.gate_service:app \
    --host "$GATE_HOST" --port "$GATE_PORT" --log-level info
  for _ in $(seq 1 40); do
    if curl -sf "http://${GATE_HOST}:${GATE_PORT}/health" >/dev/null 2>&1; then
      echo "шлюз отвечает"
      return
    fi
    sleep 0.5
  done
  echo "ВНИМАНИЕ: шлюз не ответил за 20 секунд — смотрите $GATE_LOG"
}

start_web() {
  if is_running "$WEB_PID"; then
    echo "сайт уже работает (pid $(pid_of "$WEB_PID"))"
    return
  fi
  local extra=()
  local scheme="http"
  if [ "$SITE_TLS" = "1" ]; then
    ensure_cert >/dev/null
    extra=(--ssl-keyfile "$KEY_FILE" --ssl-certfile "$CERT_FILE")
    scheme="https"
  fi
  echo "запускаю сайт на ${SITE_HOST}:${SITE_PORT} (${scheme})"
  start_bg "$WEB_PID" "$WEB_LOG" "$PY" -m uvicorn web.app:app \
    --host "$SITE_HOST" --port "$SITE_PORT" --log-level info "${extra[@]}"
  for _ in $(seq 1 40); do
    if curl -skf "${scheme}://127.0.0.1:${SITE_PORT}/health" >/dev/null 2>&1; then
      echo "сайт отвечает"
      return
    fi
    sleep 0.5
  done
  echo "ВНИМАНИЕ: сайт не ответил за 20 секунд — смотрите $WEB_LOG"
}

print_urls() {
  local scheme="http"
  [ "$SITE_TLS" = "1" ] && scheme="https"
  echo
  echo "  агент:   ${PUBLIC_URL}  (внешний адрес из PUBLIC_URL)"
  echo "  локально: ${scheme}://127.0.0.1:${SITE_PORT}/"
  echo "  шлюз:    http://${GATE_HOST}:${GATE_PORT}/status (только с этой машины)"
  echo "  логи:    $LOG_DIR/"
  echo
  local password
  password="$(setting SITE_PASSWORD '')"
  if [ -n "$password" ]; then
    echo "  вход по паролю: он задан в .env (SITE_PASSWORD)"
  else
    echo "  вход без пароля (SITE_PASSWORD пуст) — не оставляйте так при пробросе наружу!"
  fi
}

cmd_status() {
  if is_running "$WEB_PID"; then echo "сайт:  запущен (pid $(pid_of "$WEB_PID"))"; else echo "сайт:  остановлен"; fi
  if is_running "$GATE_PID"; then echo "шлюз:  запущен (pid $(pid_of "$GATE_PID"))"; else echo "шлюз:  остановлен"; fi
  local scheme="http"; [ "$SITE_TLS" = "1" ] && scheme="https"
  if curl -skf --max-time 5 "${scheme}://127.0.0.1:${SITE_PORT}/health" >/dev/null 2>&1; then
    echo "проверка: ${scheme}://127.0.0.1:${SITE_PORT}/health — отвечает"
  else
    echo "проверка: ${scheme}://127.0.0.1:${SITE_PORT}/health — не отвечает"
  fi
  if curl -sf --max-time 8 "http://${GATE_HOST}:${GATE_PORT}/status" >/dev/null 2>&1; then
    echo "шлюз-статус: $(curl -s --max-time 8 "http://${GATE_HOST}:${GATE_PORT}/status")"
  fi
}

case "${1:-start}" in
  start)
    if [ "${2:-}" = "--foreground" ]; then
      ensure_cert
      echo "запуск в текущем терминале: Ctrl+C остановит сайт"
      "$PY" -m uvicorn services.gate_service:app --host "$GATE_HOST" --port "$GATE_PORT" &
      GATE_BG=$!
      trap 'kill $GATE_BG 2>/dev/null || true' EXIT
      sleep 2
      local_extra=()
      [ "$SITE_TLS" = "1" ] && local_extra=(--ssl-keyfile "$KEY_FILE" --ssl-certfile "$CERT_FILE")
      "$PY" -m uvicorn web.app:app --host "$SITE_HOST" --port "$SITE_PORT" "${local_extra[@]}"
      exit 0
    fi
    start_gate
    start_web
    print_urls
    ;;
  stop)
    stop_one "$WEB_PID" "сайт"
    stop_one "$GATE_PID" "шлюз"
    ;;
  restart)
    "$0" stop
    # Пауза нужна не для красоты: старый шлюз гасит свой Chrome, и если новый
    # стартует в тот же момент, второй экземпляр с тем же профилем не поднимет
    # порт отладки и упадёт с «Failed to connect to browser».
    sleep 3
    "$0" start
    ;;
  status) cmd_status ;;
  cert) ensure_cert ;;
  logs)
    tail -n 80 "$WEB_LOG" "$GATE_LOG" 2>/dev/null || true
    ;;
  *)
    echo "использование: $0 {start [--foreground]|stop|restart|status|cert|logs}" >&2
    exit 1
    ;;
esac
