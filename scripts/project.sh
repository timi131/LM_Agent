#!/bin/bash

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/.lm-agent.pid"
LOG_FILE="$ROOT/.lm-agent.log"
WEB_URL="http://127.0.0.1:8000"

ollama_ready() {
  curl -fsS --max-time 1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1
}

ensure_ollama() {
  if ollama_ready; then
    return
  fi
  open -a Ollama 2>/dev/null || true
  for _ in {1..30}; do
    if ollama_ready; then
      return
    fi
    sleep 1
  done
  echo "Ошибка: Ollama не запустилась за 30 секунд" >&2
  exit 1
}

server_pid() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return
    fi
    rm -f "$PID_FILE"
  fi
}

action_start() {
  local pid
  pid="$(server_pid)"
  if [[ -n "$pid" ]]; then
    echo "Сервер уже запущен (PID $pid)."
    return
  fi
  if curl -fsS --max-time 1 "$WEB_URL/api/info" >/dev/null 2>&1; then
    echo "Сервер уже запущен: $WEB_URL"
    return
  fi

  ensure_ollama
  cd "$ROOT"
  uv sync
  nohup uv run lm-agent-web >>"$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" >"$PID_FILE"

  for _ in {1..30}; do
    if curl -fsS --max-time 1 "$WEB_URL/api/info" >/dev/null 2>&1; then
      echo "Сервер запущен: $WEB_URL"
      echo "PID: $pid"
      return
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Ошибка: сервер завершился. Лог: $LOG_FILE" >&2
      exit 1
    fi
    sleep 1
  done

  kill "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "Ошибка: сервер не ответил за 30 секунд. Лог: $LOG_FILE" >&2
  exit 1
}

action_stop() {
  local pid
  pid="$(server_pid)"
  if [[ -z "$pid" ]]; then
    echo "Сервер не запущен."
    return
  fi
  kill "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  rm -f "$PID_FILE"
  echo "Сервер остановлен."
}

action_status() {
  local pid
  pid="$(server_pid)"
  if [[ -n "$pid" ]] && curl -fsS --max-time 1 "$WEB_URL/api/info" >/dev/null 2>&1; then
    echo "Сервер запущен (PID $pid, $WEB_URL)."
  elif curl -fsS --max-time 1 "$WEB_URL/api/info" >/dev/null 2>&1; then
    echo "Сервер запущен: $WEB_URL"
  else
    echo "Сервер не запущен."
  fi

  if ollama_ready; then
    echo "Ollama запущена."
  else
    echo "Ollama не запущена."
  fi
}

action_open() {
  local pid
  pid="$(server_pid)"
  if [[ -z "$pid" ]]; then
    action_start
  fi
  open "$WEB_URL"
}

action_terminal() {
  ensure_ollama
  cd "$ROOT"
  uv run lm-agent
}

action_restart() {
  action_stop
  action_start
}

case "${1:-}" in
  start) action_start ;;
  stop) action_stop ;;
  restart) action_restart ;;
  status) action_status ;;
  open) action_open ;;
  terminal) action_terminal ;;
  *)
    echo "Использование: ./scripts/project.sh <start|stop|restart|status|open|terminal>"
    exit 2
    ;;
esac
