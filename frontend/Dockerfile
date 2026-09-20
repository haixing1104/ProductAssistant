# =============================================================================
# ProductAssistant | frontend/Dockerfile
# 用途 : PC 工作台构建 + 静态托管镜像（生产；多阶段 → nginx:1.27-alpine）
# 构建 : docker build -f frontend/Dockerfile frontend      # 构建上下文 = 模块目录
# 形态 : node:22-alpine 构建 vite 产物 → nginx:1.27-alpine 静态托管
# 说明 :
#   · 本模块只托管静态资源（SPA）；**/api 反代（含 SSE 关缓冲）由边缘 nginx 承担**
#     （infra/nginx/conf.d/pa.conf）—— 本镜像内不含任何业务反代配置（模块红线）；
#   · SPA 回退（try_files → /index.html）已在 frontend/nginx.conf 落地：边缘只做前缀/路由，
#     不接触静态文件，深链刷新 /products/<id> 才会落到本层由 try_files 兜住；
#   · 构建不需要注入任何 VITE_* 变量：三端接口基址都是**相对路径** /api/v1
#     （见 src/services/platform.ts 的 apiBaseUrl），同源反代天然生效。
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
# SPA 回退与静态缓存策略在本层（详见 frontend/nginx.conf 头部注释）
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

