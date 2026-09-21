# =============================================================================
# ProductAssistant | backend-api/tests/Dockerfile
# 用途: backend-api 测试镜像（Python + 运行/测试依赖 + pytest）
# 仅在 CI / 本地跑测试时构建；绝不进入生产镜像。
#
# 构建/运行（由 infra/docker-compose.backend-test.yml 编排，宿主只需 Docker）:
#   docker compose -f infra/docker-compose.backend-test.yml up --build \
#     --abort-on-container-exit --exit-code-from backend-tests
#   docker compose -f infra/docker-compose.backend-test.yml down -v
#
# 说明:
#   · 依赖清单与开发环境完全一致（requirements.txt + requirements-dev.txt），
#     保证「容器里跑通 = 按 requirements 装依赖后本机也能跑通」；
#   · 源码由 compose 以挂载方式注入（改代码/测试无需重建镜像），这里先 COPY 一份
#     让镜像在脱离 compose 时也能直接跑；
#   · database/sql 是夹具灌 schema 的输入（database/tests、ai-engine/tests 那两套也复用它）。
# =============================================================================
FROM python:3.12-slim

COPY backend-api/requirements.txt /tmp/requirements.txt
COPY backend-api/requirements-dev.txt /tmp/requirements-dev.txt
# 国内 PyPI 镜像（默认清华源，可用 --build-arg PIP_INDEX_URL=... 覆盖）；
# 额外挂备用源 + 放宽超时/重试：镜像站偶发读超时会整层构建失败（实测踩过）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir \
      --index-url "$PIP_INDEX_URL" --extra-index-url "$PIP_EXTRA_INDEX_URL" \
      --timeout 300 --retries 10 \
      -r /tmp/requirements.txt -r /tmp/requirements-dev.txt

WORKDIR /work/backend-api
COPY backend-api /work/backend-api
COPY database /work/database

# pytest 配置见 backend-api/pyproject.toml（pythonpath=["src"] / testpaths=["tests"]）
CMD ["pytest", "-q", "tests"]
