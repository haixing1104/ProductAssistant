# =============================================================================
# ProductAssistant | frontend/Dockerfile
# 用途 : 前端构建 + 静态托管镜像（生产模拟基线）
# 构建 : docker build -f frontend/Dockerfile frontend   （构建上下文 = 模块目录）
# 形态 : 多阶段 —— node:22-alpine 构建 vite 产物 → nginx:1.27-alpine 静态托管
# 说明 : 本层只托管静态资源（SPA）；/api 反代（含 SSE 关缓冲）由边缘 nginx 承担 ——
#        其配置属基础设施层，P8 在 infra/nginx/ 落地（本模块不含业务反代配置）
# =============================================================================

# --- stage 1: 构建 ---
FROM node:22-alpine AS build
WORKDIR /app
# 构建期 npm 源可覆盖（网络受限/加速：如 npmmirror）；CI 默认官方源
ARG NPM_REGISTRY="https://registry.npmjs.org"
# 先装依赖（锁文件不变则命中缓存）
COPY package.json package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY}" && npm ci
# 源码 + 构建（npm run build = tsc --noEmit && vite build）
COPY . .
RUN npm run build

# --- stage 2: 运行（仅静态产物 + nginx）---
FROM nginx:1.27-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
