# ProductAssistant 产品上线助手

## 原则
- 项目架构分为backend(后端)、frontend（前端）、ai-engine（AI Langgraph编排层）、database（数据库层）、infra（容器配置层） 5个模块。模块直接互相解耦，方便更换其他语言实现。

- frontend只与backend交互，绝不跨层对接ai-engine；backend与ai-engine也互相隔离，不能让ai-engine去直接操作backend的数据库表

- 只有pgsql在本地层，Redis / Milvus 以及backend，ai-engine, frontend都在容器层

- 项目名称为ProductAssdistant 产品上线助手，简称pa, 本项目中出现的pa开头命名的，如无特殊说明即代表项目名


- database目录只放初始化SQL、迁移脚本与集合初始化脚本，绝不存放业务代码
- database包含2个SCHEMA和4个ROLE以及多个TABLE
  - SCHEMA（确保AI层和Backend互相隔离）
    - schema_pa_backend 核心业务命名空间（给backend使用）
        - organizations 
        - sys_users 
        - products 
        - hitl_approvals
        - compliance_words
        - compliance_rules
        - generation_jobs
        - notification_outbox
        - delete_audits
    - schema_pa_ai AI相关命名空间（给ai-engine使用）
        - product_contents 
        - evaluation_logs
        - langgraph_checkpoints(postgresSaver创建)
  - ROLE
    - role_pa_admin 管理员角色，拥有所有权限 (DDL)
    - role_pa_backend 负责管理backend (DML)
    - role_pa_ai 负责管理ai-engine (DML)
    - role_pa_ai_setup LangGraph建表使用（CREATE）



## 前置条件（必须先完成，后续所有步骤都依赖）

### 一、安装并验证 Docker（宿主机，全程国内镜像）

> 需要 Docker 的场景：`infra/docker-compose.yml`（Redis/Milvus）、`infra/docker-compose.test.yml`（数据库测试容器）。

Ubuntu / Debian —— 使用**阿里云 docker-ce 镜像源**（国内速度快，本仓库即以此方式安装）：
```bash
# 1) 清理可能冲突的旧包
sudo apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
# 2) 基础依赖
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
# 3) 导入阿里云 docker-ce GPG key
sudo install -m 0755 -d /usr/share/keyrings
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
sudo chmod a+r /usr/share/keyrings/docker-archive-keyring.gpg
# 4) 写入 apt 源（阿里云镜像；VERSION_CODENAME 在 22.04 上即 jammy）
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
# 5) 安装 Engine + 插件（Compose 现为插件，无需独立的 docker-compose）
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
# 6) 启动并设为开机自启
sudo systemctl enable --now docker
# 7)（可选）免 sudo：加入 docker 组后需重新登录（或 newgrp docker）生效
sudo usermod -aG docker "$USER"
```

> 一键脚本（备选，同样走阿里云镜像）：
> `curl -fsSL https://get.docker.com | sudo bash -s docker --mirror Aliyun`

**配置国内镜像加速**（拉镜像走国内节点；本仓库实测配置）：
```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://dockerproxy.net"
  ]
}
EOF
sudo systemctl restart docker
```
> 其他可用加速地址：腾讯云 `https://mirror.ccs.tencentyun.com`、阿里云个人加速器 `https://<你的ID>.mirror.aliyuncs.com`（阿里云容器镜像服务控制台可获取）。

RHEL / 阿里云 Alibaba Cloud Linux：
```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

macOS：安装 Docker Desktop for Mac（自带 `docker compose`）；若拉镜像慢，在 Docker Desktop → Settings → Docker Engine 里加入上面同样的 `registry-mirrors`。

验证 Docker：
```bash
docker --version                              # 期望：Docker version 2x.x
docker compose version                        # 期望：Docker Compose version vX.Y.Z（注意是子命令 docker compose）
docker info --format '{{.ServerVersion}} storage={{.Driver}}'
docker info | grep -A3 'Registry Mirrors'     # 应列出上面配置的国内加速地址
docker run --rm hello-world                   # 端到端：拉镜像 + 起容器 + 退出
```
> 本仓库已在 **Ubuntu 22.04 + 阿里云 docker-ce 源**、`docker 29.8.0 / Compose v5.5.1 / storage=overlayfs` 上验证通过。

常见问题：
- `permission denied while trying to connect to the Docker daemon socket` → 未加入 docker 组：`sudo usermod -aG docker $USER` 后**重新登录**（或 `newgrp docker`）。
- `Cannot connect to the Docker daemon` → 服务未启动：`sudo systemctl start docker`（或 `sudo service docker start`）。
- 拉镜像超时 / 卡住 → 检查加速是否生效：`docker info | grep -A3 'Registry Mirrors'`；改完 `/etc/docker/daemon.json` **必须** `sudo systemctl restart docker`。

> 附：本项目 Python 测试依赖默认走**清华 PyPI 源**（见 `database/tests/Dockerfile` 的 `ARG PIP_INDEX_URL`），可用 `--build-arg PIP_INDEX_URL=...` 覆盖。

### 二、安装 PostgreSQL（宿主机本地实例）

> 需要本地 PostgreSQL 的场景：`backend-api` / `ai-engine` 连接的 `productassistant` 库。
> **建库 / 建角色 / 建表由 `infra/scripts/pgsql-setup.sh init` 自动完成，无需手工建库。**

Ubuntu / Debian（发行版自带版本即可；本仓库运行在 22.04 自带的 14.24 上）：
```bash
sudo apt-get update
sudo apt-get install -y postgresql postgresql-client
sudo systemctl enable --now postgresql
```
如需 PostgreSQL 17（PGDG 官方源）：
```bash
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
sudo sh -c 'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list'
sudo apt-get update && sudo apt-get install -y postgresql-17 postgresql-client-17
```

RHEL / 阿里云 Alibaba Cloud Linux：
```bash
sudo dnf install -y postgresql-server postgresql
sudo postgresql-setup --initdb          # 初始化数据目录
sudo systemctl enable --now postgresql
```

macOS（Homebrew）：
```bash
brew install postgresql@17
brew services start postgresql@17
```

验证 PostgreSQL：
```bash
psql --version                                 # 期望：psql (PostgreSQL) >= 13；本仓库为 14.24
systemctl is-active postgresql                 # 期望：active
pg_isready -h 127.0.0.1 -p 5432                # 期望：127.0.0.1:5432 - accepting connections
ss -ltn | grep 5432                            # 确认监听地址/端口
sudo -u postgres psql -c 'SELECT version();'   # 期望：PostgreSQL 1x.x（Debian/Ubuntu 默认 peer 认证）
```

本项目对本地 PG 的三点要求（与 `infra/.env` 保持一致）：
1. 监听 `127.0.0.1:5432`（对应 `.env` 的 `POSTGRES_HOST=localhost` / `POSTGRES_PORT=5432`）；
2. 存在可用的超级用户 `postgres` 且支持 **peer 认证**（脚本默认以 `sudo -u postgres psql` 连接；如改走 TCP，请设置 `PGPASSWORD`）；
3. 无需预先建库建角色 —— 直接执行下一步 `pgsql-setup.sh init`。

> 说明：本机 PostgreSQL 与测试用的 `pgsql-test` 容器是**两个互相独立的实例**；`infra/docker-compose.test.yml` 不依赖本机 PostgreSQL。


### 三、PostgreSQL

#### database目录结构

```
database/
├── sql/
│   ├── 0001_schema.sql        # 角色 + schema + 全量表 DDL
│   ├── 0002_roles_grants.sql  # 四账号最小权限矩阵落地
│   ├── 0003_seed.sql          # demo 种子（合规词库）
│   ├── 0004_delete_audit.sql  # admin 彻底删除审计表
├── milvus/                    # 待补：Milvus 集合初始化（pa_{env}_listing_vec）
└── tests/
    ├── conftest.py                # 容器测试公共夹具（pytest fixtures，见 docker-compose.test.yml）
    ├── test_permission_matrix.py  # 权限矩阵实测
    └── test_migrations.py         # 幂等重放 + 约束/GRANT 语义
```


#### 启动pgsql并建库建表DDL
```
./infra/scripts/pgsql-setup.sh init
```

#### 验证pgsql
```
./infra/scripts/pgsql-setup.sh verify
```

#### 重置pgsql（⚠️ 破坏性，仅限非生产 会先自动备份到 `infra/backups/`，再 DROP 库与 role_pa_* 并重建。）
```
ALLOW_DESTRUCTIVE=1 ./infra/scripts/pgsql-reset.sh -f
```

#### 运行数据库测试（一次性容器，宿主机只需 Docker）
```bash
docker compose -f infra/docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
docker compose -f infra/docker-compose.test.yml down -v
```
> 起临时 `postgres:17` + 测试容器跑 pytest（共 **102 例**：权限矩阵 88 + 红线 6 + 迁移/约束 8）；
> 跑完 `down -v` 即销毁，**不碰本机生产库**。本地手工核对仍可用上面的「验证数据库执行情况」。

#### 脚本调用关系

```
infra/docker-compose.test.yml
   ├─ pgsql-test : postgres:17 临时库（healthcheck 就绪后）
   └─ tests      : 容器内跑 pytest（depends_on: pgsql-test healthy）
                       │
                       ▼
        database/tests/conftest.py  ← pytest 自动加载该文件
          ├─ _apply_schema（会话级/autouse）: 用 psycopg 把 database/sql/*.sql 灌入临时库
          └─ 对外提供夹具: superuser / ai_dsn / backend_dsn / apply_sql_file
                 │
                 ├──► test_permission_matrix.py   用 superuser / ai_dsn / backend_dsn
                 └──► test_migrations.py          用 superuser / apply_sql_file
```





