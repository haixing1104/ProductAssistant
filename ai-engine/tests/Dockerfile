# =============================================================================
# ProductAssistant | ai-engine/tests/Dockerfile
# 用途: ai-engine 集成测试镜像（Python + ai-engine 运行/测试依赖）
# 仅在 CI / 本地跑集成测试时构建；绝不进入生产镜像。
#
# 构建/运行（由 infra/docker-compose.ai-test.yml 编排，宿主只需 Docker）:
#   docker compose -f infra/docker-compose.ai-test.yml up --build \
#     --abort-on-container-exit --exit-code-from ai-tests
#   docker compose -f infra/docker-compose.ai-test.yml down -v
#
# 说明:
#   · 依赖清单与开发环境完全一致（requirements.txt + requirements-dev.txt），
#     保证「容器里跑通 = 按 requirements 装依赖后本机也能跑通」；
#   · 源码由 compose 以挂载方式注入（改测试无需重建镜像），这里先 COPY 一份
#     让镜像在脱离 compose 时也能直接跑。
# =============================================================================
FROM python:3.12-slim

COPY ai-engine/requirements.txt /tmp/requirements.txt
COPY ai-engine/requirements-dev.txt /tmp/requirements-dev.txt
# 使用国内 PyPI 镜像（默认清华源，可用 --build-arg PIP_INDEX_URL=... 覆盖）；
# 额外挂一个备用源 + 放宽超时/重试：镜像站偶发读超时会让整层构建失败（实测踩过）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir \
      --index-url "$PIP_INDEX_URL" --extra-index-url "$PIP_EXTRA_INDEX_URL" \
      --timeout 300 --retries 10 \
      -r /tmp/requirements.txt -r /tmp/requirements-dev.txt

WORKDIR /work/ai-engine
COPY ai-engine /work/ai-engine
# database/sql 是集成测试夹具灌 schema 的输入（database/tests 那套测试也复用它）
COPY database /work/database

# pytest 配置见 ai-engine/pyproject.toml（pythonpath=["."] → 包根是 src）
CMD ["pytest", "-q", "tests"]
