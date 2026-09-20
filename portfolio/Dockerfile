# =============================================================================
# ProductAssistant | portfolio/Dockerfile
# 用途 : 作品集宣传页构建 + 静态托管镜像（生产；多阶段 → nginx:1.27-alpine）
# 构建 : docker build -f portfolio/Dockerfile portfolio     # 构建上下文 = 模块目录
# 运行 : 容器内 nginx:80；边缘 nginx 把它挂在 /welcome/（前缀剥离）
# 说明 :
#   · 本模块**零后端依赖**（无 /api 代理、不连任何服务）→ 镜像里没有任何上游配置；
#   · 演示动图（public/demos/pa/**，约 19MB）随产物进镜像 → 构建上下文必须包含它们
#     （母带 demo-index/ 已在根 .gitignore 与根 .dockerignore 中排除，不会进上下文）；
#   · 构建期会跑 `tsc --noEmit && vite build`：类型错误即构建失败（门禁在构建里，
#     不依赖 CI 单独再跑一遍 tsc）；
#   · 生产入口地址来自源码数据 src/data/projects.ts 的 links.live / links.liveH5
#     （门禁要求 https），因此**构建不需要注入任何 VITE_* 变量** —— 产物里不会出现 localhost。
# =============================================================================

# --- stage 1: 构建 ---
FROM node:22-alpine AS build
WORKDIR /app

# 构建期 npm 源可覆盖（国内：--build-arg NPM_REGISTRY=https://registry.npmmirror.com）
ARG NPM_REGISTRY="https://registry.npmjs.org"

COPY package.json package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY}" && npm ci

COPY . .
RUN npm run build        # = tsc --noEmit && vite build → dist/

# --- stage 2: 运行（仅静态产物 + nginx）---
FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
