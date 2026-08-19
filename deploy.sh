#!/usr/bin/env bash
# ============================================================
#  图纸相似度匹配系统 - Docker 部署脚本（目标环境：Ubuntu 24.04）
#
#  职责：
#   1. 检查/安装 Docker Engine + Docker Compose 插件（阿里云源），
#      并配置国内镜像加速器；已装好则跳过。
#   2. 交互式收集 .env 里的各项配置（含密钥），写入项目根目录 .env
#      （已在 .gitignore 中，不会被提交）。
#   3. docker compose build + up，起前端（对外 8004）与后端两个容器。
#
#  用法：
#    sudo ./deploy.sh          构建并启动
#    sudo ./deploy.sh stop     停止（docker compose down）
#    sudo ./deploy.sh logs     跟随查看两个容器日志
# ============================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"
HOST_PORT=8004

log()  { echo "[deploy.sh] $*"; }
warn() { echo "[deploy.sh][WARN] $*"; }
err()  { echo "[deploy.sh][ERROR] $*" >&2; }

# 除 stop/logs 外的正常流程都需要能装系统包、改 /etc/docker、起容器，要 root
_need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "请用 root 运行（sudo ./deploy.sh），需要安装系统包 / 配置 Docker。"
    exit 1
  fi
}

# ------------------------------------------------------------
# 子命令：stop / logs
# ------------------------------------------------------------
if [ "${1:-}" = "stop" ]; then
  docker compose down
  log "已停止。"
  exit 0
fi
if [ "${1:-}" = "logs" ]; then
  docker compose logs -f
  exit 0
fi

_need_root

# 健康检查要用；即便下面判定 Docker 已装好、跳过安装分支，也确保宿主机有 curl
command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }

# ------------------------------------------------------------
# 1. Docker Engine + Compose 插件：已装好则跳过，否则用阿里云源装
# ------------------------------------------------------------
_docker_ready() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

if _docker_ready; then
  log "检测到 Docker Engine 与 Docker Compose 插件均已安装，跳过安装步骤。"
else
  log "未检测到完整的 Docker 环境，开始通过阿里云源安装 Docker Engine + Compose 插件…"

  if [ -f /etc/os-release ]; then . /etc/os-release; fi
  if [ "${ID:-}" != "ubuntu" ]; then
    warn "本脚本按 Ubuntu 24.04 编写，当前系统 ID=${ID:-未知}，继续尝试但不保证成功。"
  fi

  apt-get update -y
  apt-get install -y ca-certificates curl gnupg

  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc

  UBUNTU_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
  ARCH="$(dpkg --print-architecture)"
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu ${UBUNTU_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  systemctl enable docker >/dev/null 2>&1 || true
  systemctl start docker >/dev/null 2>&1 || service docker start

  if _docker_ready; then
    log "Docker Engine + Compose 插件安装成功。"
  else
    err "安装后仍检测不到 Docker Engine / Compose 插件，请手动排查后重试。"
    exit 1
  fi
fi

# ------------------------------------------------------------
# 2. 配置国内镜像加速器（daemon.json），避免 docker pull 基础镜像很慢
# ------------------------------------------------------------
DAEMON_JSON=/etc/docker/daemon.json
mkdir -p /etc/docker

_write_daemon_json() {
  cat > "$DAEMON_JSON" <<'JSON'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com",
    "https://hub.rat.dev"
  ]
}
JSON
}

if [ -f "$DAEMON_JSON" ] && grep -q "registry-mirrors" "$DAEMON_JSON" 2>/dev/null; then
  log "daemon.json 已配置过 registry-mirrors，跳过（如需更换镜像源请自行编辑 $DAEMON_JSON）。"
else
  log "写入 Docker 镜像加速器配置到 $DAEMON_JSON …"
  _write_daemon_json
  systemctl restart docker 2>/dev/null || service docker restart
  log "已重启 Docker 使镜像加速器生效。"
fi
# 加速器列表可能失效，如遇 docker pull 失败请自行更新上面的地址后重跑一次本脚本。

# ------------------------------------------------------------
# 3. 交互式收集 .env（密钥不进 git，仅写本地文件，chmod 600）
# ------------------------------------------------------------
set_env_var() {
  local key="$1" value="$2" tmp
  touch "$ENV_FILE"
  tmp="$(mktemp "${TMPDIR:-/tmp}/env.XXXXXX")"
  grep -v -E "^${key}=" "$ENV_FILE" > "$tmp" 2>/dev/null || true
  mv "$tmp" "$ENV_FILE"
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

get_env_var() {
  [ -f "$ENV_FILE" ] || return 0
  # key 在 .env 里还不存在时 grep 找不到匹配、以非 0 退出——这是正常情况（返回空
  # 字符串即可），不是错误；但脚本开着 set -e -o pipefail，管道最后一个失败会
  # 直接把整个脚本杀掉（且不打印任何提示）。用 || true 吞掉这个「没找到」的退出码。
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

# prompt_value KEY 提示文案 默认值 是否敏感(1隐藏/0明文)
prompt_value() {
  local key="$1" label="$2" default="$3" secret="$4" cur input
  cur="$(get_env_var "$key")"
  [ -z "$cur" ] && cur="$default"

  if [ ! -t 0 ]; then
    warn "$key 非交互终端，使用默认/已存值。"
    [ -n "$cur" ] && set_env_var "$key" "$cur"
    return 0
  fi

  if [ "$secret" = "1" ]; then
    local hint="无"
    [ -n "$cur" ] && hint="已设置，回车保留"
    read -r -s -p "$label [$hint]: " input
    echo ""
  else
    read -r -p "$label [$cur]: " input
  fi
  [ -z "$input" ] && input="$cur"
  set_env_var "$key" "$input"
}

log "配置部署参数（直接回车 = 使用方括号里的默认值/保留已有值）："
echo ""
prompt_value DASHSCOPE_API_KEY "DASHSCOPE_API_KEY（通义千问/DashScope，用于向量化与 QWEN 解析）" "" 1
prompt_value PADDLEOCR_KEY     "PADDLEOCR_KEY（PaddleOCR-VL 云 API Token，用于「基础解读」）" "" 1
echo ""
prompt_value MO_HOST     "MO_HOST（MatrixOne 数据库地址）" "freetier-01.cn-hangzhou.cluster.matrixonecloud.cn" 0
prompt_value MO_PORT     "MO_PORT" "6001" 0
prompt_value MO_USER     "MO_USER" "" 1
prompt_value MO_PASSWORD "MO_PASSWORD" "" 1
prompt_value MO_DB       "MO_DB" "zhongding" 0
prompt_value MO_CHARSET  "MO_CHARSET" "utf8mb4" 0
echo ""

if [ -z "$(get_env_var DASHSCOPE_API_KEY)" ]; then
  warn "DASHSCOPE_API_KEY 未设置：图片/文本向量化及 QWEN 解析将不可用。"
fi
if [ -z "$(get_env_var MO_PASSWORD)" ]; then
  warn "MO_PASSWORD 未设置：后端将无法连接 MatrixOne，图纸检索/匹配会失败。"
fi

# ------------------------------------------------------------
# 4. 数据目录（volume 挂载点）确保存在，避免 compose 用 root 建目录导致权限问题
# ------------------------------------------------------------
mkdir -p "$SCRIPT_DIR/图纸_old" "$SCRIPT_DIR/片段" "$SCRIPT_DIR/upload" "$SCRIPT_DIR/outputs" "$SCRIPT_DIR/212份图纸"

# ------------------------------------------------------------
# 5. 构建并启动
# ------------------------------------------------------------
log "构建镜像（docker compose build）…"
docker compose build

log "启动容器（docker compose up -d）…"
docker compose up -d

# ------------------------------------------------------------
# 6. 等待健康检查通过
# ------------------------------------------------------------
log "等待服务就绪…"
waited=0
until curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; do
  sleep 2
  waited=$((waited + 2))
  if [ "$waited" -ge 90 ]; then
    warn "90s 内未探测到健康检查通过，可能仍在启动，用 './deploy.sh logs' 查看日志排查。"
    break
  fi
done

echo ""
log "部署完成："
log "  访问地址： http://<服务器IP>:${HOST_PORT}/"
log "  健康检查： http://<服务器IP>:${HOST_PORT}/health"
log "  查看日志： sudo ./deploy.sh logs"
log "  停止服务： sudo ./deploy.sh stop"
echo ""
