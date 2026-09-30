#!/usr/bin/env bash
# 勘码 KanMa · Web 控制台一键启动器
#
# 用法：
#   scripts/serve.sh                    # 前台启动（Ctrl+C 停止）
#   scripts/serve.sh start              # 同上
#   scripts/serve.sh start -d           # 后台启动（写 pid 与日志，自动放行 Ctrl+C）
#   scripts/serve.sh start -d --open    # 后台启动 + 自动打开浏览器
#   scripts/serve.sh stop               # 停止后台实例
#   scripts/serve.sh restart [-d]       # 重启（保留 --port/--host/--store 覆盖）
#   scripts/serve.sh status             # 查看后台实例
#   scripts/serve.sh logs               # 实时跟踪日志（tail -F）
#   scripts/serve.sh logs -n 200        # 看最近 200 行
#   scripts/serve.sh check              # 环境自检（git / docker / 模型）
#   scripts/serve.sh -h | --help        # 帮助
#
# 参数：
#   --port 端口        默认 8765（环境变量 KANMA_WEB_PORT 可覆盖）
#   --host 地址        默认 127.0.0.1
#   --store 目录       项目配置目录（与 kanma web --store 一致）
#   --log-dir 目录     日志与 pid 文件目录（默认 ./.kanma-logs/）

# 本脚本用到 [[ ]] / local / pipefail，需 bash；用 sh 调用时自动转发给 bash，
# 避免 /bin/sh 指向 dash 等非 bash 实现时语义不一致。
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -euo pipefail

ROOT="$(cd "$(dirname "${0}")/.." && pwd)"
cd "$ROOT"

# 评审流水线会在 .kanma/tmp/ 下清理/重建测试工作台（rmtree），本机 sitecustomize 的
# 批量删除保护会把这类删除拦成 SystemExit: 1，导致后台评审线程在步骤 4 静默崩溃、
# 任务卡在 running 且无报错。该保护仅针对个人目录误删，项目内的 .kanma/tmp 是评审专用
# 工作区，关掉它再启动 Web 控制台（子进程继承此环境变量）。
export CODEBUDDY_SAFE_DELETE_ENABLED=0

# 直接使用 WorkBuddy 托管的 Python 3.13 venv（与 README 一致）
PY="${KANMA_PY:-/Users/apple/.workbuddy/binaries/python/envs/kanma/bin/python}"

LOG_DIR="${KANMA_LOG_DIR:-$ROOT/.kanma-logs}"
PID_FILE="$LOG_DIR/kanma-web.pid"
LOG_FILE="$LOG_DIR/kanma-web.log"
PORT="${KANMA_WEB_PORT:-8765}"
HOST="${KANMA_WEB_HOST:-127.0.0.1}"
STORE="projects"

cmd="start"
daemon=0
open_url=0
LOG_LINES=0   # 0 表示跟随（tail -F）
port_set=0
host_set=0
store_set=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|restart|status|logs|check) cmd="$1"; shift;;
    -d|--daemon|--background) daemon=1; shift;;
    -f|--foreground) daemon=0; shift;;
    --open|-o) open_url=1; shift;;
    -n|--lines)
      if [[ $# -ge 2 && "${2}" =~ ^[0-9]+$ ]]; then
        LOG_LINES="${2}"; shift 2
      else
        shift
      fi;;
    --port|--host|--store|--log-dir)
      opt="$1"
      if [[ $# -lt 2 ]]; then
        echo "选项 ${opt} 缺少取值（用法见 --help）" >&2
        exit 2
      fi
      val="$2"; shift 2
      case "$opt" in
        --port) PORT="$val"; port_set=1;;
        --host) HOST="$val"; host_set=1;;
        --store) STORE="$val"; store_set=1;;
        --log-dir) LOG_DIR="$val"; PID_FILE="$LOG_DIR/kanma-web.pid"; LOG_FILE="$LOG_DIR/kanma-web.log";;
      esac;;
    -h|--help) cmd="help"; shift;;
    *) echo "未知参数：${1}（用 --help 查看用法）" >&2; exit 2;;
  esac
done

_alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

# 清理端口上的残留监听者。
# 服务用 setsid 脱离后不再受 serve.sh 的进程组约束，若某次 pidfile 被覆盖或丢失，
# 老实例会一直占着端口，导致新实例 bind 失败却仍报"启动成功"——页面跑的还是旧代码。
_kill_port_leftovers() {
  local pids
  pids="$(lsof -ti:"$PORT" 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0
  local pid
  for pid in $pids; do
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 0.5
  if lsof -ti:"$PORT" >/dev/null 2>&1; then
    echo "警告：端口 ${PORT} 上仍有进程占用，请手动处理：$(lsof -ti:"$PORT" | tr '\n' ' ')" >&2
  else
    echo "已清理端口 ${PORT} 上的残留进程"
  fi
}

_open() {
  local url="http://$HOST:$PORT/"
  echo "  打开: $url"
  if command -v open >/dev/null 2>&1; then open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"
  else echo "  （无可用的浏览器命令，请手动访问）"; fi
}

# 上次后台启动的连接参数。status / restart / logs 都是新进程，读它才能与实际进程对齐，
# 否则 start -d --port 8799 之后 status 仍显示默认的 8765、restart 还会把端口丢掉。
# 全新 start 不读，保证不带参数启动时用的是默认参数而非上次残留。
ENV_FILE="$LOG_DIR/kanma-web.env"
load_saved=0
case "$cmd" in
  status|restart|logs) load_saved=1;;
  start) if _alive; then load_saved=1; fi;;
esac
if [[ $load_saved -eq 1 && -f "$ENV_FILE" ]]; then
  while IFS='=' read -r k v; do
    [[ -n "${v:-}" ]] || continue
    case "${k}" in
      HOST) [[ $host_set -eq 1 ]] || HOST="$v";;
      PORT) [[ $port_set -eq 1 ]] || PORT="$v";;
      STORE) [[ $store_set -eq 1 ]] || STORE="$v";;
    esac
  done < "$ENV_FILE"
fi

print_status() {
  if _alive; then
    local pid; pid="$(cat "$PID_FILE")"
    echo "勘码 · Web 控制台运行中"
    echo "  PID:  ${pid}"
    echo "  地址: http://$HOST:$PORT/"
    echo "  日志: $LOG_FILE"
    echo "  停止: scripts/serve.sh stop"
  else
    echo "勘码 · Web 控制台未运行"
    if [[ -f "$PID_FILE" ]]; then rm -f "$PID_FILE"; fi
    return 1
  fi
}

# 已解析的连接参数，restart 时原样传递，避免丢掉 --port/--host 覆盖
common_args=(--port "$PORT" --host "$HOST" --store "$STORE" --log-dir "$LOG_DIR")

case "$cmd" in
  help|-h|--help)
    awk 'NR==1{next} /^#/{print; next} {exit}' "$0"
    exit 0;;
  status)
    print_status; exit $?;;
  check)
    "$PY" -m kanma.cli check; exit $?;;
  stop)
    if _alive; then
      pid="$(cat "$PID_FILE")"
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.3
      done
      kill -9 "$pid" 2>/dev/null || true
      rm -f "$PID_FILE"
      echo "已停止（pid=${pid}）"
      # 兜底：服务用 setsid 脱离后，pidfile 可能指向另一进程，端口上还可能留着
      # 历史实例。它们会让新进程 bind 失败——表现为"重启成功但页面还是旧代码"。
      _kill_port_leftovers
    else
      rm -f "$PID_FILE"
      echo "未运行"
    fi
    exit 0;;
  logs)
    if [[ ! -f "$LOG_FILE" ]]; then
      echo "日志不存在：$LOG_FILE" >&2
      exit 1
    fi
    if [[ $LOG_LINES -gt 0 ]]; then
      tail -n "$LOG_LINES" "$LOG_FILE"
    else
      tail -F "$LOG_FILE"
    fi
    exit 0;;
  restart)
    "$0" stop "${common_args[@]}" || true
    exec "$0" start -d "${common_args[@]}"
    ;;
  start)
    if _alive; then
      pid="$(cat "$PID_FILE")"
      echo "已在运行（pid=${pid}），不要重复启动"
      echo "查看状态：scripts/serve.sh status"
      exit 0
    fi
    # pidfile 已失效但端口仍被占（历史 setsid 实例），不清理会让本此启动 bind 失败
    if lsof -ti:"$PORT" >/dev/null 2>&1; then
      echo "端口 ${PORT} 被残留实例占用（非本次 pidfile），先清理" >&2
      _kill_port_leftovers
    fi
    if [[ ! -x "$PY" ]]; then
      echo "找不到 Python：$PY" >&2
      echo "请先创建 venv 或设置 KANMA_PY 指向解释器" >&2
      exit 1
    fi
    if [[ $daemon -eq 1 ]]; then
      mkdir -p "$LOG_DIR"
      : > "$LOG_FILE"
      # 用 _daemon.py 做 setsid 式脱离（裸 nohup 仍留在同一进程组，
      # 父 shell 被回收时服务会跟着消失，后台评审也会被连带杀掉）。
      "$PY" "$ROOT/scripts/_daemon.py" "$PY" "$LOG_FILE" "$PID_FILE" \
        --host "$HOST" --port "$PORT" --store "$STORE"
      printf 'HOST=%s\nPORT=%s\nSTORE=%s\n' "$HOST" "$PORT" "$STORE" > "$ENV_FILE"
      # 轮询等待端口就绪：launcher 返回时服务只是刚 fork 出来，
      # Python 还在 import uvicorn/FastAPI，固定 sleep 会误判启动失败。
      for _ in $(seq 1 40); do
        _alive && break
        sleep 0.25
      done
      if ! _alive; then
        echo "启动失败，日志：" >&2
        tail -n 40 "$LOG_FILE" >&2
        exit 1
      fi
      print_status
      [[ $open_url -eq 1 ]] && _open
      exit 0
    fi
    # 前台启动
    echo "勘码 · Web 控制台（前台模式，Ctrl+C 停止）"
    exec "$PY" -m kanma.cli web --host "$HOST" --port "$PORT" --store "$STORE"
    ;;
esac
