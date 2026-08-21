# syntax=docker/dockerfile:1
# 后端镜像：FastAPI + uv，运行在 Ubuntu 24.04 上。
# 构建上下文是项目根目录（需要根目录的 pyproject.toml / uv.lock），
# 用法： docker build -f backend/Dockerfile -t zhongding-backend .

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

# ---------------------------------------------------------------------------
# apt 源换成阿里云镜像。不依赖 sed 去改基础镜像自带的 sources 文件——Ubuntu
# 24.04 官方镜像的默认源不一定是能直接文本替换的 http://archive.ubuntu.com
# （新版可能用 mirror+file:/etc/apt/apt-mirrors.txt 之类的镜像列表间接跳转，
# sed 匹配不上就静默什么也不改，构建时又走回未加速/不可达的源导致失败）。
# 直接整份重写成阿里云源，格式、内容都完全可控。
#
# 用 http 不用 https：这一步是全新容器里第一次装包，ca-certificates 本身
# 还没装（就在下面这条 apt-get install 里才装），这时候 apt 走 https 会因为
# 证书链验证不了而全部 Failed to fetch（先有鸡还是先有蛋的问题）。apt 的完整
# 性靠 Release 文件的 GPG 签名保证，不依赖传输层 TLS，用 http 是官方镜像站
# 通用的标准做法，不是安全降级。
# ---------------------------------------------------------------------------
RUN rm -f /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list && \
    { \
        echo "deb http://mirrors.aliyun.com/ubuntu/ noble main restricted universe multiverse"; \
        echo "deb http://mirrors.aliyun.com/ubuntu/ noble-updates main restricted universe multiverse"; \
        echo "deb http://mirrors.aliyun.com/ubuntu/ noble-backports main restricted universe multiverse"; \
        echo "deb http://mirrors.aliyun.com/ubuntu/ noble-security main restricted universe multiverse"; \
    } > /etc/apt/sources.list

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 安装 uv：走 PyPI 阿里云镜像（pip install），不经 GitHub Releases——
# 避免国内网络访问 astral.sh / GitHub 慢或超时。
# ---------------------------------------------------------------------------
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip3 install --no-cache-dir --break-system-packages uv

# uv 自身的 PyPI 源、以及它下载「托管 Python 解释器」的源，都换成国内镜像
# （python-build-standalone 默认从 GitHub Releases 下载，同样容易很慢/超时；
# 阿里云 npmmirror 正好也镜像了这份数据）。
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ \
    UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-managed

# pyproject.toml 要求 >=3.14，用 uv 自己托管安装（不依赖系统 python3 的版本）
RUN uv python install 3.14

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY backend/ ./backend/
# 文档生成模版（doc_generator.py 依赖，位于项目根目录）
COPY 模版文件.docx ./模版文件.docx

RUN uv sync --frozen

# 图纸/片段/上传/生成结果/原始 PDF/样例数据等数据目录不打进镜像，运行时用
# volume 挂载（见 docker-compose.yml），这里只建好空目录避免首次挂载前路径缺失报错。
RUN mkdir -p /app/图纸_old /app/片段 /app/upload /app/outputs "/app/212份图纸" \
    "/app/局部图片/密封件"

WORKDIR /app/backend

EXPOSE 50011

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:50011/health || exit 1

CMD ["/app/.venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "50011"]
