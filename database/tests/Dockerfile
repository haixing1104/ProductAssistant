# =============================================================================
# ProductAssistant | database/tests/Dockerfile
# 测试专用镜像：Python + pytest + psycopg（无需 psql）
# 仅在 CI / 本地跑测试时构建；绝不进入生产镜像。
#
# Copy和安装依赖：避免每次构建都从 PyPI 下载
#
# 构建/运行（由 infra/docker-compose.test.yml 编排，宿主只需 Docker）：
#   docker compose -f infra/docker-compose.test.yml up --build \
#     --abort-on-container-exit --exit-code-from tests
#   docker compose -f infra/docker-compose.test.yml down -v
# =============================================================================
FROM python:3.12-slim

# 说明：无需 psql —— conftest 用 psycopg 把 database/sql/*.sql 直接应用进临时库
# （把 0001 里的 psql 变量 :'admin_pwd' 等替换为字面量）。镜像更小、构建不依赖 apt。
COPY database/tests/requirements.txt /tmp/requirements.txt
# 使用国内 PyPI 镜像（默认清华源，可用 --build-arg PIP_INDEX_URL=... 覆盖）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --timeout 120 -r /tmp/requirements.txt

WORKDIR /work
COPY database /work/database
