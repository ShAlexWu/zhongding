# syntax=docker/dockerfile:1
# 前端镜像：Vite 构建静态产物 + nginx 提供服务/反代后端。
# 构建上下文是 frontend/ 目录本身：
#   docker build -f frontend/Dockerfile -t zhongding-frontend frontend

# ---------------------------------------------------------------------------
# 构建阶段
# ---------------------------------------------------------------------------
FROM node:22-slim AS build

# npm 源换成阿里巴巴官方的 npmmirror（国内访问快、稳定）
RUN npm config set registry https://registry.npmmirror.com

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# 运行阶段：nginx 提供静态文件 + 反代 /api /health /static 到后端容器
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 50009

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:50009/ >/dev/null || exit 1
