#!/usr/bin/env bash
# ============================================================
#  安徽中鼎图纸相似度匹配系统 - macOS 启动脚本
#   - Backend:  FastAPI (uvicorn)  http://127.0.0.1:50011
#   - Frontend: Vite (React)       http://127.0.0.1:50009
#
#  用法:
#    ./start.sh          启动前后端并打开浏览器
#    ./start.sh stop      停止上次启动的前后端服务
#  按 Ctrl+C 可停止本次启动的全部服务。
# ============================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BACKEND_PORT=50011
FRONTEND_PORT=50009

RUN_DIR="$SCRIPT_DIR/.run"
mkdir -p "$RUN_DIR"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
ENV_FILE="$SCRIPT_DIR/.env"

log()  { echo "[start.sh] $*"; }
warn() { echo "[start.sh][WARN] $*"; }
err()  { echo "[start.sh][ERROR] $*" >&2; }

# ------------------------------------------------------------
# stop 子命令：停止上次启动的前后端进程
# ------------------------------------------------------------
if [ "${1:-}" = "stop" ]; then
  for pid_file in "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"; do
    if [ -f "$pid_file" ]; then
      pid="$(cat "$pid_file")"
      if kill -0 "$pid" 2>/dev/null; then
        log "停止进程 $pid ($pid_file)"
        kill "$pid" 2>/dev/null || true
      fi
      rm -f "$pid_file"
    fi
  done
  log "已停止。"
  exit 0
fi

# ------------------------------------------------------------
# 1. 加载 .env（如果存在），用于 DASHSCOPE_API_KEY / MO_* 等敏感配置
# ------------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
  log "加载 .env 环境变量"
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
fi

# 写入/更新 .env 中的一个 KEY=VALUE（已存在则替换，不存在则追加）
set_env_var() {
  local key="$1" value="$2" tmp
  touch "$ENV_FILE"
  tmp="$(mktemp "${TMPDIR:-/tmp}/env.XXXXXX")"
  grep -v -E "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  mv "$tmp" "$ENV_FILE"
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

# 敏感配置项：若环境变量 / .env 中都没有，交互式录入一次并持久化写入项目根目录
# .env，后续再启动就不用重新输入。非交互终端（无 TTY）时跳过录入，只给警告。
# 参数： 环境变量名  提示语  跳过时的警告语
prompt_secret() {
  local var_name="$1" prompt_text="$2" skip_warning="$3"
  local current input_val
  current="$(eval "printf '%s' \"\${${var_name}:-}\"")"
  if [ -n "$current" ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    warn "$var_name 未设置（非交互终端，跳过录入提示）。$skip_warning"
    return 0
  fi
  echo ""
  read -r -s -p "$prompt_text" input_val
  echo ""
  if [ -n "$input_val" ]; then
    export "$var_name=$input_val"
    set_env_var "$var_name" "$input_val"
    log "已保存 $var_name 到 $ENV_FILE"
  else
    warn "未输入 $var_name。$skip_warning"
  fi
}

# 通义千问 / DashScope API Key：用于图片&文本向量化、QWEN 解析
prompt_secret "DASHSCOPE_API_KEY" \
  "请输入 DASHSCOPE_API_KEY（通义千问/DashScope API Key，直接回车跳过）: " \
  "图片/文本向量化及 QWEN 解析将不可用。"

# MatrixOne（MO）数据库连接密码
prompt_secret "MO_PASSWORD" \
  "请输入 MO_PASSWORD（MatrixOne 数据库密码，直接回车则使用内置示例密码）: " \
  "将使用代码内置的示例密码，请确认这是否符合预期。"

# PaddleOCR-VL 云 API Token（「基础解读」通道使用）
prompt_secret "PADDLEOCR_KEY" \
  "请输入 PADDLEOCR_KEY（PaddleOCR-VL 云 API Token，直接回车跳过）: " \
  "「基础解读」通道（PaddleOCR-VL 裁切视图）将不可用。"

# 登录账号：不填则关闭登录鉴权（谁都能直接用）
prompt_secret "USER_NAME" \
  "请输入 USER_NAME（页面登录用户名，直接回车则不启用登录鉴权）: " \
  "未设置登录账号，页面将不要求登录即可直接使用。"
prompt_secret "PASSWORD" \
  "请输入 PASSWORD（页面登录密码，直接回车则不启用登录鉴权）: " \
  "未设置登录密码，页面将不要求登录即可直接使用。"

# uv 首次 `uv sync` 会从 GitHub Releases 下载托管的 Python 解释器
# （python-build-standalone），国内网络访问 GitHub Releases 经常很慢/超时。
# 这里默认换成阿里云 npmmirror 的镜像，可用 .env / 环境变量覆盖。
export UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://registry.npmmirror.com/-/binary/python-build-standalone}"

# ------------------------------------------------------------
# 2. 检查 / 安装依赖工具（Homebrew, uv, Node.js）
# ------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  err "未检测到 Homebrew，请先安装： https://brew.sh"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  log "未检测到 uv，正在通过 Homebrew 安装..."
  brew install uv
fi

if ! command -v npm >/dev/null 2>&1; then
  log "未检测到 Node.js，正在通过 Homebrew 安装..."
  brew install node
fi

# ------------------------------------------------------------
# 3. 安装项目依赖
# ------------------------------------------------------------
log "同步 Python 依赖 (uv sync) ..."
uv sync

if [ ! -d "$SCRIPT_DIR/frontend/node_modules" ]; then
  log "安装前端依赖 (npm install) ..."
  (cd "$SCRIPT_DIR/frontend" && npm install)
fi

# ------------------------------------------------------------
# 4. 释放可能被占用的端口（例如上次异常退出遗留的进程）
# ------------------------------------------------------------
free_port() {
  local port="$1"
  local pids
  pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    warn "端口 $port 已被占用 (pid: $pids)，正在结束旧进程..."
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}
free_port "$BACKEND_PORT"
free_port "$FRONTEND_PORT"

# ------------------------------------------------------------
# 5. 启动后端 / 前端
# ------------------------------------------------------------
log "启动后端 (FastAPI, port $BACKEND_PORT) ..."
(
  cd "$SCRIPT_DIR/backend"
  exec uv run uvicorn app:app --port "$BACKEND_PORT"
) > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!
echo "$BACKEND_PID" > "$BACKEND_PID_FILE"

log "启动前端 (Vite, port $FRONTEND_PORT) ..."
(
  cd "$SCRIPT_DIR/frontend"
  exec npm run dev -- --port "$FRONTEND_PORT"
) > "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!
echo "$FRONTEND_PID" > "$FRONTEND_PID_FILE"

cleanup() {
  echo ""
  log "正在停止服务..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  rm -f "$BACKEND_PID_FILE" "$FRONTEND_PID_FILE"
  exit 0
}
trap cleanup INT TERM

# ------------------------------------------------------------
# 6. 等待服务就绪后打开浏览器
# ------------------------------------------------------------
wait_for_port() {
  local port="$1" name="$2" timeout="${3:-40}" waited=0
  while ! curl -s -o /dev/null "http://127.0.0.1:$port" 2>/dev/null; do
    if ! kill -0 "$4" 2>/dev/null; then
      err "$name 进程已退出，请查看日志： $5"
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge "$timeout" ]; then
      warn "$name 在 ${timeout}s 内未响应，请查看日志： $5"
      return 1
    fi
  done
  log "$name 已就绪 -> http://127.0.0.1:$port"
}

wait_for_port "$BACKEND_PORT" "后端" 60 "$BACKEND_PID" "$BACKEND_LOG" || true
wait_for_port "$FRONTEND_PORT" "前端" 40 "$FRONTEND_PID" "$FRONTEND_LOG" || true

open "http://localhost:$FRONTEND_PORT/" 2>/dev/null || true

echo ""
log "已启动："
log "  后端: http://127.0.0.1:$BACKEND_PORT   日志: $BACKEND_LOG"
log "  前端: http://127.0.0.1:$FRONTEND_PORT   日志: $FRONTEND_LOG"
log "按 Ctrl+C 停止本次启动的全部服务（或另开终端运行 ./start.sh stop）。"
echo ""

wait
