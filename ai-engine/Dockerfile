# =============================================================================
# ProductAssistant | ai-engine/Dockerfile
# 用途 : ai-engine 运行镜像（消费 job:generate / job:approval / job:product_purge，
#        驱动 ListingWorkflow，把终态与阶段事件发回 Redis Streams）
# 构建 : docker build -f ai-engine/Dockerfile ai-engine       # 构建上下文 = 模块目录
# 运行 : python -m src.service（**非 root**；不监听任何端口，纯出站消费）
# 红线 :
#   · 依赖只用 requirements.txt；凭据一律环境变量注入，**不落镜像**；
#   · CMD 必须用 **exec form**（不加 shell 包装）—— SIGTERM 才能直达 PID 1，
#     src/service/__main__.py 的优雅停机（退出消费循环 → 关 checkpointer 连接池 → 关 Redis）
#     才会生效。写成 shell form 时信号被 /bin/sh 吞掉，容器只能等 stop_grace_period
#     超时后被 SIGKILL（表现为「每次发布都硬杀，正在跑的图被截断」）。
# 说明 :
#   · 源码按 PEP 420 命名空间包组织（包根是 src）→ 入口 `python -m src.service`；
#   · 不 COPY tests（那是 ai-engine/tests/Dockerfile 测试镜像的职责）；
#   · 无 EXPOSE：worker 不出站也不入站监听，Redis/PG/Milvus 一律走内部网络的服务名。
# =============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 构建期依赖源可覆盖：CI（海外 runner）覆盖为官方源更快；国内构建默认走清华 + 阿里云
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 非 root 运行（与 backend-api 同口径：生产最小权限；固定 uid 便于运维识别归属）
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# 先装依赖（利用层缓存：requirements 不变则跳过重装）
COPY requirements.txt ./
RUN pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" --extra-index-url "${PIP_EXTRA_INDEX_URL}" \
      --timeout 300 --retries 10 \
      -r requirements.txt

# 仅源码（tests / .venv 由 ai-engine/.dockerignore 排除）
COPY src ./src

USER appuser

# exec form：见文件头「红线」第二条
CMD ["python", "-m", "src.service"]
