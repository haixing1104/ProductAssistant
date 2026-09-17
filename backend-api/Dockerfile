# =============================================================================
# ProductAssistant | backend-api/Dockerfile
# 用途 : backend-api 运行镜像（本地生产模拟 / 正式部署基线）
# 构建 : docker build -f backend-api/Dockerfile backend-api     # 构建上下文 = 模块目录
# 运行 : uvicorn（非 root，:8000）；健康检查打 /healthz（存活语义，不查依赖）
# 红线 : 依赖只用 requirements.txt（stable）；密钥一律环境变量注入，不落镜像。
# =============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 构建期依赖源可覆盖（默认走国内镜像：与 ai-engine/backend 的构建习惯一致）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 非 root 运行（生产最小权限）
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# 先装依赖（利用层缓存：requirements 不变则跳过重装）
COPY requirements.txt ./
RUN pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" --extra-index-url "${PIP_EXTRA_INDEX_URL}" \
      --timeout 300 --retries 10 \
      -r requirements.txt

# 仅拷贝源码（tests/.venv 由 .dockerignore 排除）
COPY src ./src

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

CMD ["uvicorn", "pa_backend.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
