# =============================================================================
# ProductAssistant | mobile-h5/Dockerfile
# 用途 : 移动端 H5 构建 + 静态托管镜像（生产；多阶段 → nginx:1.27-alpine）
# 构建 : docker build -f mobile-h5/Dockerfile .     # ⚠️ 构建上下文 = **仓库根**，不是模块目录
# 运行 : 容器内 nginx:80 只服务静态资源；边缘 nginx（infra/nginx）负责
#        /m/ 前缀剥离与 /api 同源反代
#
# ⚠️ 为什么上下文必须取仓库根（最容易改成模块目录而踩坑的一处）:
#    mobile-h5/vite.config.ts 把 `@pa/core` 别名指到 `../frontend/src` —— 业务契约层
#    （http.ts 的内存 access + 单飞续签 + 401 重放、sse.ts 的三种控制帧语义）是
#    **三端共用同一份源码**（复制一份 = 未来每次修复都要做两遍）。
#    Docker 无法 COPY 上下文之外的文件，所以上下文取仓库根，再用相对路径把两个模块
#    都 COPY 进来。代价是上下文变大 → 由**根 .dockerignore** 兜住
#    （node_modules / .venv / dist / coverage 全部排除，实测上下文只余源码与演示素材）。
#
# 说明 : 生产访问路径是 /m/ —— vite 的 `base` 与 BrowserRouter 的 `basename` 都已设 "/m/"，
#        两者必须同时存在（只改一处 = 白屏或资源 404，见 infra/docs/deploy.md §10）。
# =============================================================================

# --- stage 1: 构建（上下文 = 仓库根）---
FROM node:22-alpine AS build
WORKDIR /repo

# 构建期 npm 源可覆盖（国内构建：--build-arg NPM_REGISTRY=https://registry.npmmirror.com）；
# CI（海外 runner）默认官方源更快
ARG NPM_REGISTRY="https://registry.npmjs.org"

# 先装依赖（锁文件不变则命中缓存）：只 COPY 两个包文件 → 改源码不会让 npm ci 层失效
COPY mobile-h5/package.json mobile-h5/package-lock.json ./mobile-h5/
RUN cd mobile-h5 && npm config set registry "${NPM_REGISTRY}" && npm ci

# 契约层（@pa/core 的落点）+ 本模块源码：两者缺一构建即失败
COPY frontend/src ./frontend/src
COPY mobile-h5 ./mobile-h5

# npm run build = tsc --noEmit && vite build → mobile-h5/dist
RUN cd mobile-h5 && npm run build

# --- stage 2: 运行（仅静态产物 + nginx）---
FROM nginx:1.27-alpine AS runtime

# SPA 回退（try_files → /index.html）与静态缓存策略在本层，不在边缘 nginx
COPY mobile-h5/nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /repo/mobile-h5/dist /usr/share/nginx/html

EXPOSE 80
