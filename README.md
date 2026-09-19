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
        - checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
          （LangGraph PostgresSaver.setup() 创建；建表角色 role_pa_ai_setup，运行期读写 role_pa_ai）
  - ROLE
    - role_pa_admin 管理员角色，拥有所有权限 (DDL)
    - role_pa_backend 负责管理backend (DML)
    - role_pa_ai 负责管理ai-engine (DML)
    - role_pa_ai_setup LangGraph建表使用（CREATE）


## 前置条件（必须先完成，后续所有步骤都依赖）

### 一、安装并验证 Docker（宿主机，全程国内镜像）

> 需要 Docker 的场景：`infra/docker-compose.yml`（Redis/Milvus）、`infra/docker-compose.test.yml`（数据库测试容器）、
> `infra/docker-compose.ai-test.yml`（ai-engine 集成测试容器：临时 PG + Redis + pytest）。

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
├── milvus/init_collections.py     # 初始化 Milvus 向量集合（商品文案语义向量库，RAG Few-Shot 召回用）
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

#### 引导平台超管（**自助注册默认关闭，这是唯一的进系统方式**）
```
cd backend-api
set -a && . ../infra/.env && set +a
PYTHONPATH=src python -m pa_backend.tools.seed_super_admin     # 交互输入口令（不回显）
```
> 会建/复用一个「平台组织」+ 一个 `is_superuser` 的 admin 账号；口令只进内存（bcrypt 后入库），
> 不写 `.env`、不进日志。**用户名不要用 `admin`**（跨组织同名会登录 400），默认 `superadmin`。
> 登录后界面顶部出现「组织选择器」，选中哪个租户就操作哪个租户的数据（走 `X-Org-Id` 头）。
> 轮换口令：加 `--reset-password` 重跑。详见 `backend-api/README.md` 的「平台超管与自助注册」。
>
> ⚠️ 已初始化过的库要**手工**执行新增迁移 `database/sql/0007_superuser.sql`
> （`pgsql-setup.sh init` 检测到 `role_pa_admin` 已存在即拒绝重跑）。

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

### 四、ai-engine 模块

####  预算熔断待实现todo list
- （把成本守卫接线 → 已落地 Agent 侧「模型调用上限」中间件，见下方「Agent / Function Calling / 中间件」小节；
  生成主链路的 token 预算统计仍未接线）、
把 worker 并发度提上来、
把可观测性补上、
eval_log_store链路有没有被前端实际用到）
RAG相关链路的质量是否有保证和展示。
没有集成淘宝，京东，拼多多，真实平台的合规判定。

#### 模块介绍
- ai-engine 模块为纯AI引擎层，不含任何HTTP路由与鉴权，以达到语言层和功能层的解耦。
- 该模块负责
    - 消费backend-api投递的 入栈 Redis Streams 任务(job:generate / job:approval / job:product_purge)
    - 驱动ListingWorkflow (生成->评估->HITL接入钉钉/飞书/Web后台/邮箱->落库/驳回)， 并把图文产物与阶段事件发布回 evt:{thread_id}。
    - 所有出站消息（evt:* / result:workflow）的信封统一携带 schema_version（当前 =1，ports/event_bus.py 单点注入）。
    - 审批恢复全程持 lock:{thread_id}（adapters/redis_eventbus.py::acquire_lock/release_lock，SET NX EX + Lua CAS），未抢到锁的实例跳过该条，防重复 resume。
- 六边形架构
    - src/service 入站进程
    - src/workflowcore/graph 内核编排层    
    - src/workflowcore/node 内核编排层
    - src/workflowcore/state 内核编排层
    - src/ports: 出站端口抽象层(提供出站的抽象接口，无实现)
    - src/adapters 出站端口实现层
    - 调用关系（分层调用链 / 三条任务链路 / 事件消费 / 6 处模型调用点 / 重试配额）见下方「#### 调用关系」小节
    
#### src/ports: 出站端口抽象层（实现层在src/adapters）
- src/ports 出站端口抽象：
  - 基础设施
    - object_storage_server.py(ObjectStorageServer): 把AI生成的图上传到阿里云OSS获取到public url；另有 get_bytes：按 URL 取回**本 bucket 受管对象**字节（供上传图规格化重落），第三方域名直接返回 None（防 SSRF）
    - event_bus.py(EventBus): 事件发布与版本治理。把图执行过程中的阶段/内容/终态事件写进 `evt:{thread_id}` 这条 Redis Stream__，它是前端 SSE 实时过程（打字机、阶段提示、图片就绪、终态信号）唯一的数据源
  - 模型调用
    - llm_gateway.py(LLMGateway )
    - llm_image_gateway.py(LLMImageGenGateway)
    - embedding_gateway.py(EmbeddingGateway)
  - 图片处理
    - image_normalizer.py(ImageNormalizer / NormalizedImage): 把任意来源的图片字节规格化为统一电商规格（EXIF 方向修正 → 等比缩放 → 白底补边 → 统一编码），返回**实测输出尺寸**（元数据以字节事实为准）；实现见 adapters/image_pillow.py，未注入时节点回退「不规格化 + 按字节探测」
  - 合规规则 
    - rule_engine.py():对“待发布文本”（AI 生成的整篇文案）做违禁词/极限词确定性判定
  - 写存储  
    - content_store.py(ContentRecord\ContentStore): 通过AI生成内容后，持久化存入 pgsql\productassistant\schema_pa_ai.product_contents
    - eval_log_store.py(EvalRecord\EvalLogStore):评估后落库，持久化存入pgsql\productassistant\schema_pa_ai.evaluation_logs
    - rag_store.py(RAGStore): Milvus pa_listing_vec,向量化存储， 用于生成阶段的相似召回
  - Agent（Agent 只读研究：工具协议 / 取数边界 / 运行时可替换点）
    - tool.py(ToolSpec\ToolCall\ToolResult\Tool): function calling 的工具协议（声明 = OpenAI 兼容 tools 元素）
    - business_reader.py(BusinessReader): Agent 取数唯一合法入口（products / product_contents /
      evaluation_logs / hitl_approvals 四类只读查询；compliance_*/sys_users 无授权故不提供）
    - agent_runtime.py(AgentRuntime): 一次「模型轮次」的执行边界（换 LangChain 官方 create_agent 只改 adapter）

#### src/workflowcore 内核编排层
- state 层 （ModelState和LLM结构化输出契约Schema）
  - /workflowcore/state/schemas.py 
  - /workflowcore/state/state.py 
- node 层 （流程节点）
  - node_rag.py 检索历史高转化文案作 Few-Shot 上下文
  - node_agent.py Agent 只读取数取证（function calling + 中间件预算治理；未注入 runtime 时 no-op）
  - node_generate.py 主生成文案（Reflection 时带上次评估意见重写）
  - node_evaluate.py 规则引擎 + LLM 评估，落 evaluation_logs
  - node_image.py 配图（有上传图：取回→规格化→转存自有 OSS；无上传图：CogView 生图→规格化→落 OSS；无图降级纯文本；宽高只写**实测值**）
  - image_probe.py 纯 stdlib 图片尺寸/MIME 探测（PNG/JPEG/GIF/WEBP/BMP，畸形字节不抛异常）
  - node_conditions.py 条件边（决定流转方向）should_retry_or_human / human_decision_route
  - node_hitl.py HITL 挂起点（interrupt() 暂停）
  - node_save_content.py 落 product_contents + 写 RAG + 发终态事件
  - node_reject.py 驳回终态收口
- graph 层（workflow.py）编排逻辑如下： 
START → rag_retrieve → agent_research（可选增强，未注入 runtime 时直通） → generate → evaluate
                                    ├─ persist（评估通过且低价不需要人工介入） → image_then_save → save_content → END
                                    ├─ retry（评估未过但未耗尽重试次数）  → generate（Reflection 重写）
                                    └─ human（评估未过且耗尽次数/高价触发）  → image_then_human → hitl → save_content / reject_end                               

#### 调用关系（分层调用链 · 链路拓扑 · 事件消费 · 模型调用点）

> **读代码建议**：先看本节总调用链 → 再看 `graph/workflow.py` 的模块 docstring（拓扑）→ 最后按需下钻节点。
> **核心认知**：运行期真正调度节点的是 **LangGraph 引擎**。`workflow.py` 里**没有任何一行** `node.xxx()` 调用，
> 它只做两件事：用 `functools.partial` 把「节点函数 + 端口」绑成一个可调用对象 `add_node` 注册，再把边连好。
> 所以「谁调用谁」必须区分 **装配期** 与 **运行期**。

##### 0) 四个正交视角（混在一起讲必然绕不清）

| 视角 | 回答什么 | 在哪看 |
|---|---|---|
| **A 装配期** | 谁 `new` 谁、哪个端口绑到哪个节点 | `service/__main__.py`、`worker._workflow()`、`graph/workflow.build_workflow()` |
| **B 运行期** | 一条消息进来后的函数调用顺序 | `worker.consume_*_once()` → 节点函数 |
| **C 依赖方向** | 谁能 import 谁（分层红线） | 各文件顶部 import（**注意：import 方向 ≠ 运行期调用方向**） |
| **D 事件消费** | 事件写哪个键、谁消费、终态结果谁接 | `adapters/redis_eventbus.py` 键空间 + 下方第 6 节 |

##### 1) 总调用链（运行期，一条命令到一次落库）

```text
python -m src.service
└─ service/__main__.main()
   ├─① 读环境变量（PA_ENV / REDIS_URL / AI_ENGINE_* / IMAGE_NORMALIZE_* / ZHIPU_*）
   ├─② 解析两个 DSN：AI_ENGINE_PG_DSN=role_pa_ai（运行期）
   │     AI_ENGINE_SETUP_PG_DSN=role_pa_ai_setup（建表）；缺省用 POSTGRES_* + ROLE_PA_AI*_PWD 拼装
   ├─③ redis.Redis.from_url(...)   （decode_responses=True；socket_timeout 10s > XREADGROUP BLOCK 上限）
   ├─④ 工厂造 6 类适配器（**缺配置 → None，由节点降级，绝不阻断启动**）
   ├─⑤ ListingWorker(端口们, DSN, keys)
   │     ├─ RedisStreams      ：XREADGROUP / XACK / lock:{thread_id}（SET NX EX + Lua CAS）
   │     ├─ PgProductsReader  ：图输入素材（粗粒度，喂 raw_product_info）
   │     ├─ PgBusinessReader  ：Agent 工具取数（细粒度，同一个 role_pa_ai DSN）
   │     ├─ ensure_checkpoint_schema(setup_dsn)：一次性建 checkpoints/checkpoint_blobs/…（给了 setup DSN 才做）
   │     └─ _checkpointer = None：进程级单例，懒建（PostgresSaver + psycopg 连接池）
   └─⑥ while running: consume_generate_once() / consume_approval_once() / consume_product_purge_once()
         └─ 单条任务：read_next → 取商品 → 编译规则快照 → build_workflow() → wf.invoke() / wf.resume()
               └─【LangGraph 引擎按拓扑回调节点】
                    rag_retrieve → agent_research → generate → evaluate
                                                     ↑            │
                                                     └── retry ───┤（条件边 should_retry_or_human）
                                                                  ├─ persist → image_then_save  → save_content → END
                                                                  └─ human   → image_then_human → hitl ─┬─ persist    → save_content → END
                                                                                                       └─ reject_end → END
                    └─ agent_research 内部（可选增强）：AgentLoop.run() → 子图 agent↔tools
                          → AgentRuntime.run_turn() → LLMGateway.chat_with_tools()
   finally: worker.close()（先放 checkpointer 连接池）→ client.close()
```

##### 2) 各层的作用与红线（视角 C：依赖方向单向向下）

| 层 | 目录 | 作用 | 允许 import | 红线 |
|---|---|---|---|---|
| **入站进程层** | `service/` | 消费 Redis Streams 任务、装配端口、驱动/恢复图、发布终态结果 | `workflowcore` / `ports` / `adapters` / `redis` / `psycopg` | 不含任何 HTTP 路由与鉴权；**不写 `schema_pa_backend`** |
| **内核-编排层** | `workflowcore/graph/` | 建图（节点 + 条件边 + checkpointer 工厂）；**唯一 import langgraph 的编排层** | `workflowcore/node`、`workflowcore/agent/middleware`、`ports`、`langgraph` | 不 import `adapters`；不直连 DB/Redis |
| **内核-节点层** | `workflowcore/node/` | 纯函数：只读 State、返回增量 dict；业务逻辑全在这里 | `ports`、`workflowcore/agent`、`workflowcore/state` | **禁止任何 DB/Redis/HTTP 直连**；只有 `node_hitl` 允许 `interrupt()` |
| **内核-Agent 层** | `workflowcore/agent/` | Agent 循环（子图）+ 中间件 + 工具注册；与 `node` 平级 | `ports`、`langgraph` | **不提供任何写工具入口**；产物只进 State，不落库 |
| **内核-状态层** | `workflowcore/state/` | `ListingState`（pydantic）+ `EvalOutput` 等 LLM 结构化输出契约 | `pydantic`、`ports` 的类型 | 是 LangGraph 的 State 单一事实源 |
| **端口层** | `ports/` | 13 个出站抽象（接口 + 契约 + 常量），只有 `abc`/标准库 | 仅标准库 | **不含任何实现** |
| **适配器层** | `adapters/` | 端口实现：PG / Redis / 智谱 LLM 与生图 / OSS / Milvus / Pillow / 规则引擎 | `ports` + 第三方库 | 不 import `workflowcore` / `service` |

##### 3) 视角 A：装配期——谁 `new` 谁

**`__main__.main()` 里的 6 个工厂**（缺配置返回 `None`，由节点降级）：

| 工厂 | 造出的实现 | 绑到端口 | 未配置时链路表现 |
|---|---|---|---|
| `build_zhipu_gateway_from_env()` | `LLMZhipuGLMGateway` | `LLMGateway` | 生成走确定性 Mock 文案；评估走「默认一次通过 90 分」 |
| `build_milvus_rag_store_from_env()` | `MilvusRAGStore`（**内部再建 `ZhipuEmbeddingGateway`**） | `RAGStore` + `EmbeddingGateway` | 空 Few-Shot 上下文；`retrieve_similar_copy` 工具**不注册** |
| `build_image_cogview_gateway_from_env()` | `LLMImageCogviewGateway` | `LLMImageGenGateway` | 无上传图时降级纯文本 |
| `build_oss_storage_from_env()` | `OSSObjectStorage` | `ObjectStorageServer` | 图不落自有 bucket，同样降级 |
| `build_image_normalizer_from_env()` | `PillowImageNormalizer` | `ImageNormalizer` | 不重编码，只按字节探测尺寸（行为同接入前） |
| `build_agent_runtime_from_env()` | `GatewayAgentRuntime`（**内部复用「生成用哪套模型」**） | `AgentRuntime` | `agent_research` 节点 **no-op**（返回 `{}`） |

**`worker._workflow()` 每任务调用 `build_workflow(...)`**（图实例每任务新建，checkpointer 复用进程级单例）：

| `build_workflow` 参数 | 每任务新建 还是 复用 | 最终消费它的节点 |
|---|---|---|
| `llm_gateway` | worker 持有（复用） | `generate`、`evaluate` |
| `rag_store` | worker 持有（复用） | `rag_retrieve`、`save_content`、`agent_research` |
| `content_store=PgContentStore(dsn)` | **每任务新建**（廉价，只持 DSN） | `save_content` |
| `eval_log_store=PgEvalLogStore(dsn)` | **每任务新建** | `evaluate` |
| `event_bus=RedisEventBus(...)` | **每任务新建** | 所有节点 |
| `checkpointer=self._get_checkpointer()` | **进程级单例**（懒建 PostgresSaver+池） | `hitl`（interrupt/resume） |
| `rule_engine=compile_rules_snapshot(msg.rules)` | **每任务从消息快照编译** | `evaluate`、`agent_research`、worker 标题预检 |
| `image_gateway` / `object_storage` / `image_normalizer` | worker 持有（复用） | `image_then_save`、`image_then_human` |
| `asset_key_prefix=f"img/pa/{env}"` | **每任务拼装** | `image_*`（对象键前缀） |
| `agent_runtime` / `agent_reader` / `agent_middlewares` / `agent_max_*` | worker 持有（复用） | `agent_research` |

##### 4) 视角 B：三条任务链路 + 一条可选子链路

### 链路① `job:generate`（生成主链路，最长）

| # | 调用 | 关键契约 / 失败行为 |
|---|---|---|
| 1 | `streams.read_next("pa:{env}:job:generate", group="ai-engine", block_ms=1000)` | 内部先 `ensure_group()`（幂等建组）；`>` 只读本组未投递过的消息；超时返回 `None`（正常空轮询，非错误） |
| 2 | `self._rule_engine_from_message(data)` → `compile_rules_snapshot(data["rules"])` | **入队瞬间固化的规则快照**；老消息/无 `rules` → `None`（引擎关闭，评估只走 LLM） |
| 3 | `self.product_reader.read(product_id, org_id)` | 读 `schema_pa_backend.products`（`role_pa_ai` 仅 SELECT）；`None` → 返回 `missing_product` |
| 4 | 【可选】`engine.check(product["title"])` 阻断级预检 | 命中 → 发 `failed` 事件 + `failed` 结果 → 返回 `blocked_input`（**不浪费一次注定违规的生成**） |
| 5 | `self._publish(thread_id, {"type":"generate.started"})` | 写 `evt:{thread_id}` |
| 6 | `input_state = {thread_id, product_id, org_id, raw_product_info}` | 图初始 State 增量 |
| 7 | `wf = self._workflow(rule_engine=engine)` → `build_workflow(...)` | **视角 A**：partial 绑端口（见第 3 节表） |
| 8 | `wf.invoke(input_state, config=wf.thread_config(thread_id))` | 包装层注入 `max_retries` 后 `graph.invoke()`；**thread_id 即幂等锚点** |
| 9 | ★ 图内执行（下表展开） | 由 LangGraph 引擎调度 |
| 10 | `interrupted = "__interrupt__" in out` | 挂起判定（HITL） |
| 11 | 挂起时：`_review_snapshot(out)` + 发 `hitl.waiting` 事件 | 快照含 `content.blocks`（图文）+ `evaluation_result` + `reason` |
| 12 | `_publish_outcome(data, "published"\|"awaiting_human", content_snapshot=…)` | 写 `result:workflow`；`content_snapshot` **仅在转人工时带** |
| 13 | `finally: streams.ack("job:generate", msg_id)` | **无论成功失败都 ack**（异常已终态化，避免消息滞留 PEL 被反复重投） |
| — | `except` → `_terminalize_failed(data, exc, stage="generate")` | 发 `failed` 事件 + `failed` 结果；两个发布动作各自 try/except，**不再上抛**（否则掩盖原始异常） |

**第 9 步展开——图内每个节点被回调时都调了什么**：

| 节点（图内名） | 节点函数 | 内部调用链 | 返回增量 |
|---|---|---|---|
| `rag_retrieve` | `node.rag_retrieve_node` | `rag_store.retrieve(org_id, product_id, query=title, top_k=3)` → `MilvusRAGStore._search` → `embed_gateway.embed()` ★模型① | `rag_context` |
| `agent_research` | `node.agent_research_node` | `agent_runtime is None → return {}`（no-op）；否则 `build_registry()` → `AgentLoop(...).run()`（**子链路见下**） | `agent_context` / `agent_trace` / `tool_calls_used` |
| `generate` | `node.generate_text_node` | 发 `stage.generating` → `gateway.generate_text_stream(prompt)` ★模型③ → 每 ≤40 字发 `content.chunk`（打字机）；`gateway=None` → Mock 文案 | `generated_content` |
| `evaluate` | `node.evaluate_listing_node` | 发 `stage.evaluating` →【规则先行】`rule_engine.check(content)` 命中阻断级即 **fail-fast 跳过 LLM** → 否则 `_request_strict_eval()` → `gateway.complete_json(json_schema)` ★模型④ → `EvalOutput.model_validate`（失败回喂修复 ≤2 次）→ `eval_log_store.save(EvalRecord)` | `evaluation_attempts` / `evaluation_result` / `last_eval_errors` |
| （条件边） | `node.should_retry_or_human` | 纯函数读 State → `retry` / `persist` / `human` | 路由键 |
| `image_then_save`<br>`image_then_human` | `node.gen_image_node`（**同一函数注册两次**） | 有 `raw_images`：逐张 `object_storage.get_bytes()` → `image_normalizer.normalize(ai_marked=False)` → `put_bytes()`；无：`image_gateway.generate_image()` ★模型⑤ → `normalize(ai_marked=True)` → `put_bytes()`；失败逐张回退/整体降级 | `content_blocks` / `image_attached` / `image_error` |
| `hitl` | `node.await_human_input` | `interrupt(payload)` **挂起**；resume 后本函数**重新执行**，`interrupt()` 直接返回 decision | `human_feedback` / `approval` |
| `save_content` | `node.save_content_node` | `content_store.save(ContentRecord)` → `schema_pa_ai.product_contents` → `rag_store.upsert()` ★模型⑥ → Milvus → 发 `done` | `status="succeeded"` |
| `reject_end` | `node.reject_end_node` | 无任何存储副作用，仅置 `status="rejected"` | `status` / `approval` |

> ❓ 为什么 `image` 注册两次：同一函数挂两条终局分支，让**进 HITL 之前就配好图** ——
> 这样 `interrupt()` 挂起时 `content_snapshot` 里已是完整图文，审批人看到的与最终上架一致。

### 链路② `job:approval`（HITL 恢复链路，跨消息 / 跨进程 / 跨图实例）

| # | 调用 | 关键契约 |
|---|---|---|
| 1 | `streams.read_next("pa:{env}:job:approval", ...)` | 载荷：`{thread_id, result:"approved"\|"rejected", feedback, product_id, org_id}` |
| 2 | `streams.acquire_lock(thread_id, ttl=300)` | `SET NX EX` 抢分布式锁；**抢不到 → ack + 返回 `busy`**（绝不允许两个实例同时 resume 同一线程） |
| 3 | `self._publish({"type":"approval.resumed"})` | 先给 SSE 一个「审批已回传」信号 |
| 4 | `wf = self._workflow()` | ★ **图实例是新建的**：恢复靠的不是同一个 Python 对象，而是 checkpointer 里的 `thread_id` 状态 |
| 5 | `wf.resume({"approved":…, "feedback":…}, config=wf.thread_config(thread_id))` | 内部 `graph.invoke(Command(resume=decision), config)` |
| 6 | 图从 `hitl` 继续：`node_hitl` 重跑 → `interrupt()` 返回 decision → 条件边 `human_decision_route` | `persist` → `save_content`（落库 + 写向量 + `done` 事件）→ `status=succeeded`；`reject_end` → `status=rejected`（**无任何存储副作用**） |
| 7 | `status=="rejected"` 时补发 `rejected` 终态事件 | 与 `save_content` 的 `done` 对齐：驳回也要给 SSE 一个终态，否则前端按钮永远停在「AI 生成中」 |
| 8 | `_publish_outcome(data, "published"\|"rejected")` | — |
| 9 | `finally: release_lock(thread_id, token)` **然后** `ack(...)` | **先放锁再 ack**：即便 ack 遇 Redis 抖动抛错，锁也不会滞留到 TTL 过期 |

> ⚠️ 注意：链路②建的图**不带规则快照**（`self._workflow()` 未传 `rule_engine`）。
> 由于 resume 只从 `hitl` 往后走（`save_content` / `reject_end` 都不用规则引擎），**当前无影响**；
> 但如果将来把 resume 后的路径接回 `evaluate`，这里必须补传规则快照。

### 链路③ `job:product_purge`（商品彻底删除，物理清理）

| # | 调用 | 关键契约 |
|---|---|---|
| 1 | `streams.read_next("pa:{env}:job:product_purge", ...)` | 载荷：`{product_id, org_id}` |
| 2 | **前置守卫**：`product_reader.read(...) is not None` | 商品行仍存在 = **孤儿 purge 消息**（删除并未生效）→ 中止并 ack，返回 `skipped_product_exists`（防误删有效数据） |
| 3 | `content_store.list_image_urls(org_id, product_id)` | **必须先收集**：行删掉后 URL 不可逆丢失（也供 OSS 白名单删除） |
| 4 | `list_pa_thread_ids(dsn, org_id, product_id)` | **必须先反查**：checkpoint 表按 `thread_id` 组织 |
| 5 | `content_store.purge_by_product()` / `PgEvalLogStore.purge_by_product()` | 删 `product_contents` / `evaluation_logs` |
| 6 | `saver.delete_thread(thread_id)` ×N | 清 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`（best-effort） |
| 7 | `object_storage.delete_urls(image_urls)` | 只删「本 bucket + `img/pa/` 前缀」白名单内的对象（前缀必须与 `oss._key_from_url` 同源） |
| 8 | `rag_store.delete_by_product()` | 删 Milvus 向量（best-effort） |
| 9 | `finally: ack(...)` | 3/6/7/8 全是 best-effort：失败只打印，不阻断 ack（任务幂等可重投） |

### 子链路 `agent_research`（Agent 子图，默认开、可关）

```text
node.agent_research_node(state, agent_runtime, reader, rule_engine, rag_store, middlewares, max_*, event_bus)
  ├─ agent_runtime is None → return {}                      ← 未开启即 no-op，链路与接入前完全一致
  ├─ build_registry(org_id, product_id, reader, rule_engine, rag_store)
  │     └─ tools.build_business_tools()  ★多租户字段由闭包固化，**不进工具参数 schema**
  │     └─ 按端口有无裁剪工具（Milvus/规则快照缺失 → 该工具不注册，避免「调了但永远为空」的幻觉入口）
  ├─ len(registry) == 0 → return {}
  ├─ event_bus.publish({"type": "stage.researching"})
  └─ AgentLoop(runtime, registry, middlewares, max_turns, max_tool_calls, event_bus).run(
         thread_id=state.thread_id, question=_build_question(state))
       │
       ├─ run()：初始化 messages=[system, user] → self._graph.invoke(recursion_limit=max_turns*2+2)
       │
       └─【子图 agent ↔ tools，builder.compile() **不挂 checkpointer**】
             START → agent ─┬─(有 tool_calls 且未超限)→ tools ─→ agent（循环）
                            └─(无工具调用 / 触发预算 / 运行时异常) → END
             ① _agent_node:
                  ModelCallLimitMiddleware.before_model(ctx)   ← 预算熔断：超限则**不再发起模型调用**
                  AgentRuntime.run_turn(messages, tools=registry.to_openai_tools(), tool_choice="auto")
                      └─ GatewayAgentRuntime.run_turn → LLMGateway.chat_with_tools()  ★模型②（多轮）
                  ToolCallLimitMiddleware / after_model 钩子
             ② _tools_node → _dispatch(call, ctx)：中间件洋葱
                  for mw in reversed(middlewares): handler = _bind_wrap(mw, ctx, handler)
                  最内层 = ToolRegistry.run(call)
                      ├─ 未知工具 → ok=False 结果回填（模型可换工具，循环不断）
                      └─ handler 抛异常 → ToolRetryMiddleware 重试 ≤max_attempts，仍失败兜底为失败结果
                  每调用一次发 {"type": "agent.tool"}
             ③ run() 收尾：归一 stop_reason（completed / model_call_limit / runtime_error…）
                  发 {"type": "agent.done"}，返回 AgentRunResult
```

**为什么子图不挂 checkpointer**：避免把内层往返状态写进**生成线程**的 checkpoint，破坏 HITL 的 interrupt/resume 语义。

**Agent 的降级红线**：`runtime=None` → 节点 no-op；LLM/网络异常 → 收敛为 `stop_reason=runtime_error` **不抛异常**；
研究是「增强」不是「必需」，失败绝不让整条商品生成判 failed。

##### 5) 视角 D：事件消费关系（两条出站总线，用途完全不同）

| 出站键 | 内容 | 谁消费 | 语义 |
|---|---|---|---|
| `pa:{env}:evt:{thread_id}` | 过程事件 + 终态事件 | backend SSE 端点（✅ 已实现，P5）→ 前端 | **前端实时过程（打字机/阶段/图片就绪）唯一数据源**；已做保留策略：`MAXLEN ~ AI_ENGINE_STREAM_MAXLEN_EVT`（默认 2000，只影响断线回放深度）+ 每次发布刷新 `EXPIRE AI_ENGINE_EVT_TTL_SECONDS`（默认 7 天，到期整流回收） |
| `pa:{env}:result:workflow` | 图终态结果 | backend `WorkflowResultConsumer`（✅ 已实现，P4） | **驱动商品状态机**（published / awaiting_human / rejected / failed）；`content_snapshot` 仅在 `awaiting_human` 时携带 |
| `pa:{env}:lock:{thread_id}` | 互斥令牌 | worker ↔ worker | 审批恢复串行化（`SET NX EX` + Lua CAS 释放） |

**`evt:{thread_id}` 的完整事件清单**（按链路出现顺序）：

| 事件 `type` | 发布者 | 触发时机 | 前端用途 |
|---|---|---|---|
| `generate.started` | `worker` | 素材就绪、即将建图 | 进入「AI 生成中」 |
| `stage.researching` | `node_agent` | Agent 工具表非空 | 阶段提示「🔎 正在核对商品资料…」 |
| `agent.tool` | `AgentLoop._tools_node` | 每次工具调用完成后 | 「🛠 已核对：商品素材」（**载荷键名是 `tool`**，内部工具名映射成业务名；`ok=false` → 「（未取到）」） |
| `agent.done` | `AgentLoop.run` | 子图收敛后 | 阶段行：`completed` → 「✔ 资料核对完成」；`runtime_error` → 「⚠ 本次未参考历史资料（服务异常），文案按商品素材生成」；**预算/工具到顶（默认预算下的常态）与缺字段 → 不出行**。`stop_reason`/`turns`/`tool_calls` **只在载荷里**，不进界面（排障看 `evt` 流 / 日志 / `agent_trace`） |
| `stage.generating` | `node_generate` | 生成前（含 `attempt`） | 打字机预备 |
| `content.chunk` | `node_generate` | 流式每 ≤40 字一次 | **打字机正文**（唯一正文来源） |
| `stage.evaluating` | `node_evaluate` | 评估前（含 `attempt`） | 阶段提示 |
| `evaluate.result` | `node_evaluate` | 评估后 | 「✔ 合规评估：95 分（通过）」；载荷另有 `violations` 计数（未渲染） |
| `stage.imaging` | `node_image` | 配图前（`source=uploaded\|ai`） | 「🎨 配图整理中…（来源：上传素材 / AI 生成）」 |
| `image.ready` | `node_image` | **AI 图**落库后（上传图分支不发） | 图片就绪 |
| `done` | `node_save_content` | 成功落库后 | 终态：完成 |
| `hitl.waiting` | `worker` | 检测到 `__interrupt__` | 终态：等待人工审批（**没有它前端会一直转圈**） |
| `approval.resumed` | `worker` | 收到 approval 后、resume 前 | 审批已回传 |
| `rejected` | `worker` | resume 后 `status=rejected` | 终态：驳回 |
| `failed` | `worker` | 异常收口 / 输入违禁词拦截 | 终态：失败（前端复位按钮） |

> **阶段流/状态行只出现业务语言**（2026-09 用户反馈「这是非业务的数据，没人知道这是什么意思」）：
> `stop_reason` / `turns` / `tool_calls` / 内部工具名 / `score=` / `source=uploaded` / `（Agent）` 这类实现细节
> **一律不渲染**，只留在载荷里（排障看各事件对应的 `evt:{thread_id}` / 日志 / `agent_trace`）。
> 三端（H5 / RN / 桌面）共用**一份**实现 `frontend/src/services/streamLabels.ts`（`formatEvent` 出明细行、
> `statusLine` 出状态行）—— 桌面旧的那份拷贝已删除；那份拷贝正是 `agent.tool` 键名两边一起读错的温床
> （生产者发 `tool`、两边都读 `name` → 界面上工具名常年空白）。回归由 `tests/streamLabels.test.ts` 用
> **真实事件载荷**锁定（含一条"渲染结果不得出现上述内部字段"的负向断言）。

> **两条写入路径，信封结构一致（已核对实现）**：节点内走注入的 `EventBus.publish()`，`worker._publish()` 则直接走 `RedisStreams.xadd()` ——
> 两者最终都经 `_xadd()` 落盘（`adapters/redis_eventbus.py`），所以信封**完全相同**：字段 `data` = JSON 字符串，JSON 首键为 `schema_version`。
> 消费端按同一套结构解析即可，**无需按来源分流**。（历史注释与本文曾写「worker 事件不带信封」，与实际实现不符，已更正。）

##### 6) 6 处模型调用点（一次任务最多触发全部）

| # | 触发点 | 端口方法 | 真实实现 | 缺配置时 |
|---|---|---|---|---|
| ① | `rag_retrieve` 召回 query 向量化 | `EmbeddingGateway.embed` | `ZhipuEmbeddingGateway`（经 `MilvusRAGStore` 持有） | RAG 整体关闭 |
| ② | `agent_research` 多轮取数 | `LLMGateway.chat_with_tools` | `LLMZhipuGLMGateway.chat_with_tools`（OpenAI 兼容 tools 协议） | 节点 no-op |
| ③ | `generate` 流式写文案 | `LLMGateway.generate_text_stream` | `_chat_stream`（SSE 逐块解析） | Mock 文案 |
| ④ | `evaluate` 结构化评估 | `LLMGateway.complete_json` | `_chat` + `_extract_json` | 默认一次通过 90 分 |
| ⑤ | `image_*` 生图（+下载图床） | `LLMImageGenGateway.generate_image` | `LLMImageCogviewGateway` | 降级纯文本 |
| ⑥ | `save_content` 定稿入向量库 | `EmbeddingGateway.embed` | `ZhipuEmbeddingGateway`（经 `MilvusRAGStore` 持有） | 跳过写向量 |

> 走 `retry` 边重写会让 ③④ **重复执行**；②③④⑤ 各自有独立的配额（下表）。

##### 7) 重试计数与配额（三套独立计数 + 三个额外护栏）

| 配额 | 默认值 | 计数字段 | 超限行为 |
|---|---|---|---|
| **① Reflection 重试**（评估未过 → 重写） | `build_workflow(max_retries=2)`，worker 未覆盖 → **2** | `ListingState.evaluation_attempts` | 条件边 `should_retry_or_human` 返回 `human` → 转人工（HITL） |
| **② JSON 结构修复**（评估输出不合契约） | `MAX_SCHEMA_REPAIRS=2`（即最多 **3** 次调用） | `node_evaluate` 内局部循环 `attempt` | 安全降级为 `passed=False, score=0.0`，原因写入 `last_eval_errors` |
| **③ Agent 预算**（模型调用 / 工具调用） | `max_turns=2`、`max_tool_calls=3`（`AI_ENGINE_AGENT_MAX_TURNS` / `…_TOOL_CALLS`；默认开之后刻意收紧） | `AgentBudget.turns_used` / `tool_calls_used` → `ListingState.tool_calls_used` | 中间件在下一轮前取消 → `stop_reason=model_call_limit`（用已有证据收敛） |
| ④ 工具内部重试 | `ToolRetryMiddleware(max_attempts=2)` | 局部循环 | 兜底为失败 `ToolResult`，循环继续 |
| ⑤ 子图递归上限 | `recursion_limit = max_turns*2+2` | LangGraph 内部 | 子图直接结束 |
| ⑥ 消费侧并发保护 | `approval_lock_ttl_seconds=300` | `lock:{thread_id}` | 未抢到锁的实例跳过该条（返回 `busy`） |

> **三套计数互相独立**：`max_retries`（图级反思）↛ `MAX_SCHEMA_REPAIRS`（节点内结构修复）↛ `max_turns/max_tool_calls`（Agent 预算）。
> 一次任务最坏情况会把这些额度叠加消耗，成本评估时必须分别计入。

##### 8) 端口 → 适配器 → 消费者（谁用了我）

| 端口（`ports/`） | 适配器（`adapters/`） | 消费者 |
|---|---|---|
| `LLMGateway` | `llm_zhipu.LLMZhipuGLMGateway` | `node_generate`（③）、`node_evaluate`（④）、`GatewayAgentRuntime`（②）、`zhipu_embedding`（①⑥） |
| `LLMImageGenGateway` | `llm_image_cogview.LLMImageCogviewGateway` | `node_image`（⑤） |
| `EmbeddingGateway` | `zhipu_embedding.ZhipuEmbeddingGateway` | `MilvusRAGStore`（内部持有，不直接进图） |
| `RAGStore` | `milvus_rag_store.MilvusRAGStore` | `node_rag`、`node_save_content`、`agent/tools.retrieve_similar_copy` |
| `RuleEngine` | `rules.CompiledRules`（`compile_rules_snapshot`） | `node_evaluate`、`agent/tools.scan_compliance`、worker 标题预检 |
| `ContentStore` | `pg_store.PgContentStore` | `node_save_content`（+ worker purge 的 `list_image_urls` / `purge_by_product`） |
| `EvalLogStore` | `pg_store.PgEvalLogStore` | `node_evaluate`（+ purge） |
| `EventBus` | `redis_eventbus.RedisEventBus` | 全部节点（发过程事件） |
| `ObjectStorageServer` | `oss.OSSObjectStorage` | `node_image`（`put_bytes` / `get_bytes` / `delete_urls`） |
| `ImageNormalizer` | `image_pillow.PillowImageNormalizer` | `node_image` |
| `BusinessReader` | `pg_store.PgBusinessReader` | `agent/tools` 四个只读取数工具 |
| `AgentRuntime` | `gateway_agent_runtime.GatewayAgentRuntime` | `node_agent` → `AgentLoop` |
| `Tool` / `ToolCall` / `ToolResult` / `ToolSpec` | `agent/registry.build_tool` | `agent/tools` → `ToolRegistry` |

> ⚠️ `PgProductsReader`（喂图输入素材）与 `PgBusinessReader`（喂 Agent 工具）是**两个不同的类**，但共用同一个 `role_pa_ai` DSN。

##### 9) 关键约束与易踩坑（读代码时逐条验证过）

1. **`partial` 参数名不能叫 `runtime`** —— 那是 LangGraph 的保留注入名，框架会无视 `partial` 强行注入自己的 Runtime 对象，导致「未注入就 no-op」失效（`node_agent.py`、`workflow.py` 各有一处血泪注释，回归断言在 `tests/test_agent_tools.py`）。
2. **`generate` 节点必须注入 `event_bus`** —— 漏了会「只看到阶段跳变、看不到正文流式输出」，排查极难（代码注释标注为历史遗漏已修）。
3. **节点的异常策略是刻意相反的**：`node_rag.rag.retrieve()` 的异常**故意不捕获**（由 worker 统一终态化 failed，避免静默降级掩盖向量库故障）；而 `node_save_content.rag_store.upsert()` 失败**必须吞掉**（RAG 是可选增强，best-effort）。
4. **图实例轻、连接池重**：`_workflow()` 每任务新建图；`_get_checkpointer()` 是进程级单例（懒建 + `close()` 释放）。
5. **ack 位置决定可靠性**：generate / purge 在 `finally` ack；approval 是「先放锁再 ack」。**未 ack ≠ 卡死**：worker 每轮消费前用 `claim_stale()`（XAUTOCLAIM）回收滞留消息，投递次数超限（`AI_ENGINE_MAX_DELIVERIES`）转死信 `pa:{env}:dlq:{流}`；「线程锁仍在」的消息跳过回收（保护正常的慢任务）。运维口径见 `infra/docs/redis-production.md`。
6. **`interrupt()` 之前不得有副作用**：resume 会让 `node_hitl` 整个函数**重新执行**一次。
7. **图片元数据以字节事实为准**（本轮修复）：`image` block 的 `width/height` 只写**实测值**（规格化输出尺寸或字节探测值），拿不到就不写；`put_bytes` 的 `Content-Type` 必须与字节真格式一致 —— OSS SigV1 预签名把它算进签名串，传错会签名不匹配。
8. **幂等三件套**（可靠性加固）：`done:{thread_id}` 终态标记（**仅 published 写入**，awaiting_human / failed 保持未完成语义）→ 重复投递直接返回 `duplicate`；线程锁（`AI_ENGINE_JOB_LOCK_TTL_SECONDS`）→ 并发处理返回 `busy`；图自身 checkpoint → 恢复语义。三者叠加后「backend 重复投递 / PEL 回收重投」都不会重复落库。
9. **`busy` 时 ack 与否是刻意的**（写反会静默丢任务）：**新投递**的重复消息直接 ack 丢弃；**PEL 回收**的消息抢不到锁时**不 ack**（原持有者可能只是慢，留给下轮回收）。回归断言在 `tests/test_worker_reliability.py`。

##### 生产化可靠性加固（PEL 回收 / 死信 / 流保留 / 心跳）

Redis Streams 是 backend ↔ ai-engine 的**唯一业务通道**，因此按队列级标准加固（默认值即可用）：

| 能力 | 实现 | 关键行为 |
|---|---|---|
| **PEL 回收** | `RedisStreams.claim_stale()`（`XAUTOCLAIM`）+ `worker._pull_or_reclaim()` | 每轮消费前回收「空闲 > `AI_ENGINE_PEL_MIN_IDLE_MS`」的滞留消息；`read_next` 遇脏数据抛 `MessageDecodeError`（携带 msg_id 与原文，可精确定位并清理） |
| **死信（DLQ）** | `move_to_dlq()` | 投递次数 ≥ `AI_ENGINE_MAX_DELIVERIES` 或消息体非法 → 转 `pa:{env}:dlq:{流}`；**先写死信再 ack**（写失败则不 ack，绝不漏消息） |
| **毒消息 vs 慢任务** | `lock_exists()` 探测 | 线程锁仍在 → **跳过回收**（保护正常的慢任务）；锁已释放却仍未 ack → 才是真毒消息 |
| **流保留** | `_xadd(maxlen=…)` + `expire()` | evt 默认 `MAXLEN 2000` + `TTL 7 天`（每次发布刷新）；job / result 默认 `MAXLEN 10000` |
| **幂等** | `done:{thread_id}` + 线程锁 | 重复投递返回 `duplicate` / `busy`（见上文第 8、9 条） |
| **可观测** | 心跳键 + 待处理读数 | `pa:{env}:worker:heartbeat:{consumer}`（TTL 30 s，键过期即「消费停滞」告警信号）；循环每 20 轮打印 `待处理读数 {…}` |

> 参数调优组合、故障演练清单（kill -9 / 重复投递 / 脏消息 / Redis 重启 / 内存吃紧）、DLQ 处理 SOP、托管 Redis 对照清单：**`infra/docs/redis-production.md`**。
> 生产编排：`infra/docker-compose.redis-prod.yml`（requirepass / `maxmemory-policy noeviction` / AOF everysec / 禁用 FLUSHALL、CONFIG 等 / **不暴露宿主端口**）。
> 端到端验收：`./infra/scripts/redis-verify.sh`（起临时 Redis 容器，断言裁剪 / 回收 / 死信 / 脏数据 / 幂等 / 锁，DB=15 与 dev/prod 隔离）。

##### 10) 本仓边界：哪些调用关系**还不在代码里**

`frontend/`（桌面工作台，P7）、`mobile-h5/`（移动端 H5，P9）与 `mobile-rn/`（移动端**原生 App**，Expo SDK 57，本轮新增）均已落地；`backend-api` 已在 P0~P4 落地（见下方「### 五、backend-api 模块」）：

| 现实中应存在的一环 | 状态 | 影响 |
|---|---|---|
| backend 投递 `job:generate` / `job:approval` / `job:product_purge` | ✅ 已实现（`backend-api/src/pa_backend/services/ai_engine_client.py`） | 载荷含入队瞬间固化的 `rules` 合规快照 |
| backend `WorkflowResultConsumer` 消费 `result:workflow` 推 `products.status` | ✅ 已实现（`services/workflow_result_consumer.py`，含 5 条幂等/边界加固） | 终态结果有人接了；5 条加固见 `backend-api/README.md` §2.5 |
| SSE 端点消费 `evt:{thread_id}` 推前端 | ✅ 已实现（**P5**，`routers/stream_router.py` + `services/stream_reader.py`） | 打字机/阶段/图片就绪事件已可推送；短时票据鉴权、`Last-Event-ID` 续传、空闲关流均落地 |
| `hitl_approvals.content_snapshot` 落库（审批中心展示 AI 生成详情） | ✅ 已实现（`services/workflow_result_consumer._create_pending_approval`） | 列本就在 `0001_schema.sql`，无需迁移 |
| 前端界面（React + antd） | ✅ 已实现（**P7**，`frontend/`） | 8 条路由（登录/商品/详情/审批/深链/合规/运维/成员），走 backend-api 的 43 个端点；只连 `/api/v1`（不直连 ai-engine/PG） |
| 移动端 H5（React + antd-mobile） | ✅ 已实现（**P9**，`mobile-h5/`） | 登录 / 商品（列表+详情+SSE 实时生成）/ 审批（列表+详情+深链+批准驳回+补投）/ 我的；**与桌面端共享契约核心层**（`@pa/core` → `frontend/src/{api,services,store,types}`，页面壳各写各的）；只连 `/api/v1` |
| 移动端原生 App（React Native + Expo） | ✅ 已实现（**P10**，`mobile-rn/`） | iOS + Android 一套代码；与 H5 的页面/组件**逐条对齐**（6 屏 + 深链），并**复用同一份契约核心层**：共享层新增「平台端口」（`frontend/src/services/platform.ts`）承载 6 个平台差异点（接口基址 / 会话回跳 / 过期事件 / 定时器 / JWT 解码 / SSE 与二进制传输），因此 `http.ts`（单飞续签 + 401 重放）与 `sse.ts`（`hitl.waiting` 终态 / `ready` 不重连 / 注释帧三态）**三端共用一份**；UI 为自研薄 UI（零 UI 依赖）；版本锁定与真机验证边界见 `mobile-rn/README.md` |
| 作品集宣传页（**静态**，`portfolio/`） | ✅ 已实现（**P11**） | 个人作品合集（数据驱动，加作品=加一条数据）+ 四端演示位（Web / H5 / RN-Android / RN-iOS）+ 联系方式；**纯静态零后端依赖**（不调 `/api`，可独立部署到任意静态托管）；演示环境（域名 + 只读演示账号 + 成本护栏）未接入前「进入系统」是禁用占位态 + 邮件联系 |

**当前仓库内可独立跑通的部分**：`__main__` → worker → 图 → 节点 → 适配器 → PG/Redis/OSS/Milvus，
以及 `tests/` 里用 `InMemorySaver` 的图级测试（207 个用例，其中 195 个纯内存可跑、12 个需容器化 PG+Redis 否则自动 skip）。

---

#### Agent / Function Calling / 中间件（可选增强）

> **开关口径（容易读错，先看这里）**：本小节三件事只有**一个**开关 —— `AI_ENGINE_AGENT_ENABLED`（**默认开**），
> 它同时决定这三件事是否参与链路（置 `0/false/no/off` → `agent_research` 节点 no-op）。
> 其中**「中间件」没有独立开关**：Agent 一旦启用，`AgentLoop` 就以 `default_middlewares()` 装配三条策略
> （模型调用上限 / 工具调用上限 / 工具重试兜底），要停只能整体关 Agent；想换策略需在代码层注入 `agent_middlewares`（无环境变量可调）。
>
> **两道门**：① `AI_ENGINE_AGENT_ENABLED` —— 未配置 / 空白 → 默认**开**；置 `0/false/no/off` 关闭；
> ② 启用但无可用 LLM 凭据 → 仍 no-op，并在启动时打印提示（**不会报错，但别误以为 Agent 在跑**）。
> 启动日志会明确给出「Agent 研究已启用（max_turns=2, max_tool_calls=3）」或「已关闭」，可直接核对生效状态。
> 默认开的取舍与「何时该关」见下方「##### 4) 默认开 + 何时该关（成本与质量取舍）」。

在 `rag_retrieve` 与 `generate` 之间插入 **`agent_research`** 节点：用 function calling 调**只读工具**
核对真实素材与历史记录，结论写入 `ListingState.agent_context`（生成提示词会带上它作为「事实要点」）。
未注入 `agent_runtime` 时该节点**直接 no-op**，链路行为与接入前完全一致。

##### 1) 作用：生成前置的「事实核对员」

**一句话定位**：Agent 只做两件事 —— **取证 → 交付要点**；不写文案、不落库、不推进商品状态（红线）。

```text
START → rag_retrieve → 【agent_research】 → generate → evaluate → …
         ↑历史文案召回      ↑本阶段新增：真库取证      ↑事实要点进提示词
```

| 它解决的问题 | Agent 的应对 | 用哪个工具 |
|---|---|---|
| 模型编造价格 / 库存 / 资质 | 真读商品表；查不到返回 `{"found": false}`（**逼模型说「查不到」而不是编造**） | `get_product_facts` |
| 反复踩同一条违规词 | 拉历史评估的违规点；并支持**动笔前自查** | `get_eval_history` / `scan_compliance` |
| 被同一理由反复驳回 | 拉历史审批驳回意见 | `get_approval_history` |
| 不像本店历史高转化风格 | 语义召回同租户历史文案片段 | `retrieve_similar_copy` |
| 「为什么这么写」说不清 | 逐轮审计轨迹 + 实时事件（SSE 可见「AI 正在核对什么」） | `agent_trace` + `agent.tool` / `agent.done` |

##### 2) 三层能力与落点

| 能力 | 落点 | 默认状态 / 开关 | 说明 |
|---|---|---|---|
| Function Calling | `ports/llm_gateway.chat_with_tools()`（默认 `NotImplementedError`，向后兼容）+ `adapters/llm_zhipu.chat_with_tools()` | 端口默认不可用；真实实现**随 Agent 启用而参与链路** | 智谱 OpenAI 兼容协议：请求带 `tools` / `tool_choice`，解析 `choices[0].message.tool_calls`；复用既有 DNS 预检/超时/错误映射，**零新增依赖** |
| Agent 循环 | `workflowcore/agent/loop.py`（LangGraph 两节点子图 `agent ↔ tools` + 条件边） | **默认开** —— 未配置即开；置 `AI_ENGINE_AGENT_ENABLED=0/false/no/off` 关闭（缺凭据自动 no-op） | 子图**不挂 checkpointer**（不污染生成线程的 checkpoint/HITL 恢复语义）；状态是纯 JSON dict 消息，不引入任何 LangChain 消息对象 |
| 中间件 | `workflowcore/agent/middleware.py`（`before_model` / `after_model` / `wrap_tool_call`） | **无独立开关，随 Agent 常开** —— `middlewares=None` → `default_middlewares()` 三条 | 三个内置策略：**模型调用上限（预算熔断）**、工具调用上限、只读工具重试 + 异常兜底 |

边界（红线）：**不注册任何写工具**；商品状态推进仍只走 `result:workflow` → backend；Agent 产物只进 State，不落库。

##### 3) 工具集与「数据能不能真拿到」对照表

| 工具 | 数据来源 | role_pa_ai 权限 |
|---|---|---|
| `get_product_facts` | `schema_pa_backend.products` | 仅 SELECT ✅ |
| `get_content_history` | `schema_pa_ai.product_contents` | 全 DML（此处只读）✅ |
| `get_eval_history` | `schema_pa_ai.evaluation_logs` | 全 DML（此处只读）✅ |
| `get_approval_history` | `schema_pa_backend.hitl_approvals` | 仅 SELECT ✅ |
| `scan_compliance` | job 载荷里的 **rules 快照**（`adapters/rules.compile_rules_snapshot`） | `compliance_words` / `compliance_rules` **无授权** → 只能走快照 |
| `retrieve_similar_copy` | Milvus `pa_listing_vec`（经 RAGStore 端口） | 未配 Milvus 时该工具**不注册** |

**刻意不做**（无权限，硬做只会失败或被拒）：
- 组织名 / 用户姓名（`sys_users` 不可读 → 审批人只能给 `approver_id`）；
- 违禁词库直读（`compliance_*` 不可读）、任务状态机（`generation_jobs` 不可读）。

多租户：`org_id` / `product_id` 由 `node_agent` 注入闭包固化，**不进工具参数 schema**
（模型无处下跨租户参数，见 `tests/test_agent_tools.py` 的红线断言）。

##### 4) 默认开 + 何时该关（成本与质量取舍）

Agent **默认开启** —— `AI_ENGINE_AGENT_ENABLED` 未配置 / 空白即视为开，置 `0/false/no/off` 关闭。
理由：它带来的「用真库素材 / 历史违规点 / 审批意见校准事实」应当是**默认质量下限**，而非少数场景的奢侈品。
但它确实有成本与副作用，所以**默认预算被刻意收紧**（2 轮 / 3 次），并且**随时可一行 env 回退**（见本节末尾）。

| 维度 | 默认开带来的收益 | 默认开的代价（务必监控） |
|---|---|---|
| **质量** | 「事实要点」拼进生成提示词，压制造假（价格 / 库存 / 资质）与重复违规、重复驳回 | `agent_context` **会改写最终文案** —— 若 Agent 产出低质/幻觉要点，会污染该商品文案；且目前**无 A/B 数据**证明收益 |
| **成本** | 只在「真的需要取证」时发生工具调用（不硬跑满预算） | 单任务模型调用数 4 次 → **最多 8 次以上**；`retrieve_similar_copy` 工具内部**还会再触发一次 embed**；多轮累积上下文使 token **远超线性**；**token 级计量仍未接线**（只有次数上限兜底）。依据：`__main__.py` 注释「开启后会在图内产生额外的模型调用与只读取数」 |
| **延迟** | 与生成串行、不引入额外线程/依赖 | 卡在 `rag_retrieve` 与 `generate` 之间：最多 2 轮**同步**模型调用 + 最多 3 次工具取数（真查 PG），期间**无流式输出**，前端只能看阶段提示 |
| **数据面** | 只读；多租户字段由闭包固化，**不进工具参数 schema**（红线有测试锁定） | 读面扩大：真查 `products` / `product_contents` / `evaluation_logs` / `hitl_approvals` |
| **可用性** | 研究失败**不影响主链路**：收敛为 `stop_reason`，绝不把商品判 `failed`；缺凭据自动 no-op | 依赖供应商 tools 协议（目前仅 `LLMZhipuGLMGateway` 实现）；**中间件无独立开关**，要停只能整体关 |

**什么时候该关掉它**（置 `AI_ENGINE_AGENT_ENABLED=0`，一行 env 立即恢复「与未接入 Agent 时完全一致」）：

- **成本敏感期**：token 账单需要压降时；
- **取证没跑通**：`agent.done` 里 `stop_reason` 大量非 `completed`（等于白花钱），或 `agent.trace` 里工具 `ok=false` 居多；
- **无正收益**：同批商品「开 / 关」对比后 `evaluation_logs.score` 未改善（却仍在改写文案）；
- **排障二分**：怀疑文案异常由 `agent_context` 注入的事实要点引起时，先关掉再复现。

**默认预算（为何是 2 轮 / 3 次，以及怎么调）**：

| 环境变量 | 默认 | 含义 | 调整建议 |
|---|---|---|---|
| `AI_ENGINE_AGENT_MAX_TURNS` | **2** | 模型调用轮次上限（预算熔断阈值） | ⚠️ **不要低于 2**：1 轮来不及「工具结果 → 结论」的第二次往返，等于放弃取证（`tests/test_agent_tools.py` 的接线用例恰好卡在该边界） |
| `AI_ENGINE_AGENT_MAX_TOOL_CALLS` | **3** | 工具调用次数上限 | 3 次 ≈ 够取「商品事实 + 一条历史证据 + 一次合规自查」；复杂类目可调到 5–8 |

**回退路径（默认开是可逆的）**：

```bash
export AI_ENGINE_AGENT_ENABLED=0   # 立即回到「Agent 完全不参与链路」的历史行为（有回归测试锁定）
```

##### 5) 预算与降级（为什么敢放进生成链路）

- 预算熔断：模型调用次数上限（默认 4 轮）——超限**不再发起模型调用**，用已有证据收敛；
- 工具调用上限（默认 8 次）：超限的调用被拦下并立即收敛；
- 工具异常：`ToolRetryMiddleware` 重试（只读工具无副作用）+ 兜底为失败结果，绝不打断主链路；
- 运行时（LLM/网络）异常：收敛为 `stop_reason=runtime_error`，生成/评估照常进行；
- 轨迹：`ListingState.agent_trace` 记录逐轮模型/工具调用，事件 `agent.tool` / `agent.done` 进 `evt:{thread_id}`（SSE 可见「AI 正在核对什么」）。

##### 6) 已实现 / 明确没做（边界清单）

**已实现（6 组）**

| 能力 | 落点 | 说明 |
|---|---|---|
| 工具协议 | `ports/tool.py` | `ToolSpec`（`to_openai_tool()` 产出 tools 元素）/ `ToolCall` / `ToolResult`（`to_message_content()` 回填、`to_trace()` 审计）/ `Tool`；**零第三方依赖** |
| Agent 运行时端口 | `ports/agent_runtime.py` | `run_turn(messages, tools, tool_choice)` 是内核与供应商之间的**唯一隔离点**；`normalize_tool_calls()` 收敛异构返回值（脏数据跳过、不抛异常） |
| Function Calling 实现 | `adapters/llm_zhipu.py` | `_build_tool_payload()` + `chat_with_tools()`：OpenAI 兼容 `tools` / `tool_choice`（tools 为空则不下发），解析 `choices[0].message.{content,tool_calls}`；温度默认 `0.2`（工具选择偏确定性）；复用既有 DNS 预检/超时/错误映射，**零新增依赖** |
| Agent 循环（子图） | `workflowcore/agent/loop.py` | `agent ↔ tools` 两节点 + 双条件边（`_route` / `_route_after_tools`）；**不挂 checkpointer**；`AgentBudget` 计数由 loop 独占写；`recursion_limit=max_turns*2+2`；运行时异常收敛为 `stop_reason=runtime_error`（**不抛异常**） |
| 中间件 | `workflowcore/agent/middleware.py` | 3 个内置策略（模型调用上限 / 工具调用上限 / 重试兜底）+ `before_model` / `after_model` / `wrap_tool_call` 三个可覆盖钩子；`wrap_tool_call` **逆序嵌套**（列表第一个 = 最外层） |
| 工具集 + 节点产出 | `workflowcore/agent/tools.py`、`node/node_agent.py` | 6 个只读工具（按端口有无**动态裁剪**、多租户字段**闭包固化**、参数 `_bounded_int` 收敛）；产出 `agent_context` / `agent_trace` / `tool_calls_used` 与 `stage.researching` / `agent.tool` / `agent.done` 事件 |

**明确没做（边界，避免误读）**

| 没做 | 说明 |
|---|---|
| ❌ 任何**写**工具 | 红线：ai-engine 对 `schema_pa_backend` 只读；Agent 产物只进 State，**不落库、不推进商品状态** |
| ❌ 多 Agent / 任务规划 / 长期记忆 / 自主 RAG 循环 | 单轮取证即收敛（≤4 轮），是「助手」不是「自治体」 |
| ❌ 流式输出 | `chat_with_tools` 同步非流式 → `stage.researching` 期间前端**无正文可看**（延迟代价） |
| ❌ 中间件的独立开关 | 无环境变量；只能代码层注入 `agent_middlewares` **替换**默认链 |
| ❌ token 级成本计量 | 只有**调用次数**上限；token 预算统计仍未接线（见待实现 todo list） |
| ❌ 效果 A/B 验证 | **已默认开但质量收益仍未量化**（它会改写最终文案，见「##### 4) 默认开 + 何时该关」）；上线后应按该节监控 `evaluation_logs.score` 与 `agent.done` 的 `stop_reason` |

#### checkpoint 生产方案（LangGraph PostgresSaver）

`node_hitl.interrupt()` 挂起与 `Command(resume)` 恢复都依赖 checkpointer 持久化图状态。
本仓按「建表 / 运行」两角色分离落地：

| 环节 | 角色 | 实现 | 说明 |
|---|---|---|---|
| 建表（幂等，一次性） | `role_pa_ai_setup`（schema USAGE+CREATE） | `adapters/pg_store.ensure_checkpoint_schema()` | `SET search_path` 后调 `PostgresSaver.setup()`，建 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` |
| 运行期读写 | `role_pa_ai`（业务表 DML） | `workflowcore/graph.new_pg_checkpointer()` | `PostgresSaver(psycopg_pool.ConnectionPool)`，池化 + 探活 + 自动重连 |

要点：
- **连接池与连接级参数**：在连接工厂层固化 `autocommit=True`（PostgresSaver 强制）、
  `row_factory=dict_row`（按列名取值）、`prepare_threshold=0`（兼容 PgBouncer transaction pooling）、
  `options="-c search_path=schema_pa_ai,public"`。**池化下不能用一次性 `SET`**，否则新连接会落到 `public`。
- **自愈**：`check=ConnectionPool.check_connection` 让坏连接在取出前被探活重建，
  避免 PG 重启 / 网络抖动后 HITL resume 永久失败（裸 `Connection` 无此能力）。
- **权限自动授权**：`database/sql/0002_roles_grants.sql` 中
  `ALTER DEFAULT PRIVILEGES FOR ROLE role_pa_ai_setup ... GRANT ... TO role_pa_ai`
  让 setup 新建的 checkpoint 表自动授权给运行期角色，建表与运行可分离且无需补 GRANT。
- **安全加固**：`new_pg_checkpointer()` 默认 `os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")`，
  限制 checkpoint 反序列化类型（官方反序列化加固项）。
- **生命周期**：worker 内**进程级单例**（`ListingWorker._get_checkpointer()` 懒建并复用，
  避免"每条消息新建一条连接"），停机时 `ListingWorker.close()` → `close_pg_checkpointer()` 释放连接池。
- **删商品联动清理**：`job:product_purge` 先反查该商品的 thread_id
  （`adapters/pg_store.list_pa_thread_ids()`，走 `product_contents`/`evaluation_logs` 的 thread 索引），
  删完内容/日志后逐个 `saver.delete_thread()` 清 checkpoint 三表。

#### 环境变量契约（infra/.env.template → infra/.env）

`infra/.env.template` 是入库的**唯一键清单**（键名 + 默认值 + 说明注释）；`infra/.env` 是本机真值文件（已被 `.gitignore` 忽略）。
两者由脚本保持**同向一致**，日常只需维护 `.env`：

| 场景 | 命令 | 行为 |
|---|---|---|
| 首次 / 新机器 | `./infra/scripts/env-init.sh` | `.env` 不存在 → 由模板生成（权限 600），随后替换全部 `CHANGE_ME` |
| 代码新增变量后 | ① 先改 `infra/.env.template` ② `./infra/scripts/env-init.sh` | 只追加 `.env` 缺失的键，**绝不覆盖已有值**（幂等，可反复执行） |
| 日常自检 | `./infra/scripts/env-check.sh` | 缺键 = ERROR；未登记键 / 弱口令 = WARN |
| 生产上线前 | `./infra/scripts/env-check.sh --prod` | 弱口令 / 占位 / 空值也计 ERROR（阻断，退出码非 0） |

> 判定口径：模板里**未注释**的键必须在 `.env` 中；**已注释**的键 = 可选覆盖项（如两个 DSN，缺省时由 `POSTGRES_* + ROLE_PA_AI*_PWD` 自动拼装）。
> 模板中的 `PA_ENV=dev` 只是本地默认 —— 生产必须显式改 `prod`（它决定 Redis 键前缀 `pa:{env}:...`，消费端与生产端必须一致）。

运行：
```bash
cd ai-engine
pip install -r requirements.txt
# 两个 DSN：建表用 setup 角色（仅首次/迁移需要），运行用运行期角色
export AI_ENGINE_SETUP_PG_DSN='postgresql://role_pa_ai_setup:<pwd>@localhost:5432/productassistant'
export AI_ENGINE_PG_DSN='postgresql://role_pa_ai:<pwd>@localhost:5432/productassistant'
export REDIS_URL='redis://localhost:6379/0'      # Redis 未起：先 docker compose --env-file infra/.env -f infra/docker-compose.yml up -d
export PA_ENV=dev
# Agent 研究（默认开，可关）：图内会多一轮「只读工具取证」的模型调用，代价与关闭时机见上文
export AI_ENGINE_AGENT_ENABLED=1                 # 未配置即视为开；置 0 关闭（需 ZHIPU_API_KEY 可用，否则节点自动 no-op）
export AI_ENGINE_AGENT_MAX_TURNS=2               # 模型调用上限（预算熔断阈值；不建议低于 2）
export AI_ENGINE_AGENT_MAX_TOOL_CALLS=3          # 工具调用上限
# AI 配图：生图模型 + 水印开关（默认关）+ 图片规格化（默认开：1:1 / 白底补边 / 不放大）
export ZHIPU_IMAGE_MODEL=glm-image               # 生图模型；请求尺寸会按模型规格自动收敛
export ZHIPU_IMAGE_WATERMARK=0                   # 0=关闭平台水印（需已签免责声明）；1=保留
export IMAGE_NORMALIZE_SIZE=1000x1000            # AI 图与上传图统一到该正方形规格
# 只给 ROLE_PA_AI_PWD / ROLE_PA_AI_SETUP_PWD + POSTGRES_* 时，两个 DSN 会自动拼装
# 也可直接从 infra/.env 载入（进程本身不解析 .env 文件）：
#   set -a; source ../infra/.env; set +a
python -m src.service
```

测试（`ai-engine/tests`）：

单测（纯内存：Agent 循环/中间件/工具、interrupt/resume + 线程隔离，无需 PG/Redis/LLM）：
```bash
cd ai-engine
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```
集成（一次性容器：真实 PG + Redis + 真造业务数据；**跑完 down -v 整体销毁，宿主库全程不被触碰**）：
```bash
docker compose -f infra/docker-compose.ai-test.yml up --build \
  --abort-on-container-exit --exit-code-from ai-tests
docker compose -f infra/docker-compose.ai-test.yml down -v
# 可选：真实 function calling 冒烟（会消耗少量 token；默认跳过）
PA_TEST_LLM=1 ZHIPU_API_KEY=<key> docker compose -f infra/docker-compose.ai-test.yml up --build ...
```
上述集成容器覆盖：Agent 工具真读业务数据（`tests/test_agent_tools_pg.py`）、
role_pa_ai 写 products 被拒的红线负向断言、worker 端到端（投递 job:generate → 落库 → 事件/终态结果，
`tests/test_worker_e2e.py`）、PostgresSaver 建表与读写（`tests/test_pg_checkpointer.py`）。

> **备选路径（镜像构建被镜像站卡住时）**：若 `pgsql/milvus` 等镜像已在本机，可直接起**一次性容器**
> 代替 `ai-tests` 镜像构建（同样是「跑完即删、宿主机库零写入」）：
> ```bash
> docker run -d --name pa-ai-test-pg -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
>   -e POSTGRES_DB=productassistant_test -p 55432:5432 postgres:17
> docker run -d --name pa-ai-test-redis -p 56379:6379 redis:7.4-alpine redis-server --appendonly no
> # 然后在宿主 venv 里按上面的 PA_TEST_* 变量跑 pytest（DSN 指向 127.0.0.1:55432 / 56379）
> docker rm -f pa-ai-test-pg pa-ai-test-redis      # 用完即删，无卷残留
> ```

只跑某一路（宿主机直连临时容器，DSN 指向 55432/56379，仍不是宿主库）：
```bash
PA_TEST_SUPERUSER_DSN='postgresql://postgres:postgres@127.0.0.1:55432/productassistant_test' \
PA_TEST_PG_DSN='postgresql://role_pa_ai:pa_ai_pwd123@127.0.0.1:55432/productassistant_test' \
PA_TEST_BACKEND_PG_DSN='postgresql://role_pa_backend:pa_backend_pwd123@127.0.0.1:55432/productassistant_test' \
PA_TEST_PG_SETUP_PG_DSN='postgresql://role_pa_ai_setup:pa_ai_setup_pwd123@127.0.0.1:55432/productassistant_test' \
PA_TEST_REDIS_URL='redis://127.0.0.1:56379/15' \
  python -m pytest -q
```

#### AI 配图：尺寸口径与合规

**尺寸不一致的两条根因与修法**

| 根因 | 位置 | 修法 |
|---|---|---|
| block 的 width/height 写的是**请求值**（1024x1024），与真实出图尺寸可能不符（glm-image 默认 1280x1280） | node_image 旧实现 | 改为**实测值**：注入规格化器时用规格化输出尺寸，否则用 image_probe 读字节真实尺寸；两者都拿不到就不写该字段（**绝不写请求值**） |
| 请求尺寸与模型规格不匹配 → 网关静默回退默认尺寸 | adapters/llm_image_cogview.resolve_image_size() | 按模型收敛尺寸并记 warning：glm-image 默认 1280x1280、自定义 1024-2048 且 32 的整数倍；cogview-4 / cogview-3-flash 默认 1024x1024、自定义 512-2048 且被 16 整除 |

**规格统一（ImageNormalizer / adapters/image_pillow.py，默认开启）**

- 口径：1:1、1000x1000、白底**补边**（等比缩放 + 居中补边，**不裁剪** —— 裁剪会切掉商品本体）、**默认不放大**（小图保持原始像素密度）、jpeg q85；
- 覆盖两类图：AI 生图产物（ai_marked=True）与运营上传图（取回字节 → 规格化 → 转存新对象键 upload-{n}.jpg，**绝不覆盖原图**）；
- put_bytes 的 Content-Type 与对象键后缀跟随**字节真实格式**（image_probe.probe_content_type）—— OSS SigV1 预签名把 Content-Type 算进签名串，传错会签名不匹配；
- 未注入或显式关闭（IMAGE_NORMALIZE_ENABLED=0）时行为与接入前一致（回归红线由 tests/test_image_node.py 锁定）。

**水印与合规（重要）**

- 平台侧：请求体携带 watermark_enabled，**默认 false**（关闭平台显式 + 隐式水印）。账户前提是已在智谱控制台签署免责声明（个人中心-安全管理-去水印管理）；未签账号传 false 会被拒 → node_image 降级纯文本并记 image_error；
- 我方义务：按《人工智能生成合成内容标识办法》（2025-09-01 施行）第九条，关闭显式标识后标识义务转移到使用者；本项目在规格化时对 AI 产物写入 AI 生成标识元数据（EXIF Software / ImageDescription: DigitalSourceType=trainedAlgorithmicMedia）来履行；
- 红线：**不得**对已有标识做裁剪/涂抹/覆盖（第十条禁止恶意删除、篡改、隐匿标识）；若把 ZHIPU_IMAGE_WATERMARK=1 打开（保留平台隐式水印），则**不要再让 Pillow 重编码** AI 产物（重编码会丢弃元数据）；
- 上传图按实拍图处理（ai_marked=False），**绝不冒挂** AI 标识；image block 的 source 字段保留 uploaded / ai_generated，供审计与平台申报。

#### 目录结构

```
ai-engine/
├── pyproject.toml           # 仅 pytest 工具配置（pythonpath=["."]，包根是 src → 入口 python -m src.service）
├── requirements.txt         # 运行依赖（langgraph / langgraph-checkpoint-postgres / psycopg / psycopg-pool / redis / pymilvus / pillow ...）
├── requirements-dev.txt     # 测试依赖（pytest）
├── src/                     # 内核（PEP 420 命名空间包 src.*；内部相对导入如 from ..adapters 均相对 src）
│   ├── workflowcore/        # 内核编排层（唯一 import langgraph 的层级）
│   │   ├── graph/
│   │   │   ├── workflow.py   # 编排：StateGraph + 条件边；checkpointer 工厂
│   │   │   │                 #   new_memory_checkpointer()（测试/本地 InMemorySaver）
│   │   │   │                 #   new_pg_checkpointer()  （生产 PostgresSaver + 连接池）
│   │   │   │                 #   close_pg_checkpointer()（停机释放连接池）
│   │   │   └── __init__.py   # 导出 build_workflow / resume_command / 上述三个工厂
│   │   ├── node/             # 节点层：纯函数，只读 State、返回增量；禁止任何 DB/IO 直连
│   │   │   ├── node_rag.py             # RAG 召回（Few-Shot 上下文）
│   │   │   ├── node_agent.py           # Agent 只读取数取证（未注入 runtime 时 no-op）
│   │   │   ├── node_generate.py        # 生成（流式增量 → content.chunk）
│   │   │   ├── node_evaluate.py        # 评估（规则 fail-fast → LLM 语义评估 + schema 强校验/修复）
│   │   │   ├── node_image.py           # 配图（上传图→规格化重落 / CogView 生图→规格化→OSS；失败降级纯文本）
│   │   │   ├── image_probe.py          # 纯 stdlib 尺寸/MIME 探测（尺寸元数据的事实来源）
│   │   │   ├── node_hitl.py            # HITL interrupt() 暂停
│   │   │   ├── node_save_content.py    # 成功终态落库 + 写向量库 + done 事件
│   │   │   ├── node_reject.py          # 驳回终态（不落内容）
│   │   │   └── node_conditions.py      # 条件边（重试/落库/转人工；高价阈值 500）
│   │   ├── agent/            # Agent 内核（循环 + 中间件 + 工具注册/装配）
│   │   │   ├── loop.py                 # agent ↔ tools 两节点子图（LangGraph，无 checkpointer）
│   │   │   ├── middleware.py           # before_model / after_model / wrap_tool_call 钩子
│   │   │   │                           #   内置：模型调用上限（预算熔断）/ 工具上限 / 重试兜底
│   │   │   ├── registry.py             # 工具注册表（声明 → OpenAI 兼容 tools；按名分发）
│   │   │   └── tools.py                # 只读工具集（org/product 由闭包固化，不进参数 schema）
│   │   └── state/
│   │       ├── state.py                # ListingState（pydantic BaseModel，LangGraph 原生支持）
│   │       │                           #   含 agent_context / agent_trace / tool_calls_used
│   │       └── schemas.py              # EvalOutput 等 LLM 结构化输出契约（JSON Schema 强约束单一事实源）
│   ├── ports/               # 端口抽象（13）：LLMGateway / LLMImageGenGateway / EmbeddingGateway /
│   │                        #   RAGStore / ContentStore / EvalLogStore / EventBus / RuleEngine /
│   │                        #   ObjectStorageServer / BusinessReader（Agent 只读取数）/ Tool /
│   │                        #   AgentRuntime（Agent 运行时可替换点）/ ImageNormalizer（图片规格化）
│   ├── adapters/            # 端口实现：pg_store（content/eval 落库 + 商品与业务只读 + checkpoint 建表）/
│   │                        #   redis_eventbus / rules / llm_zhipu（含 chat_with_tools）/
│   │                        #   gateway_agent_runtime（网关 → AgentRuntime 适配）/
│   │                        #   llm_image_cogview（水印开关 + 模型尺寸收敛）/ oss（含 get_bytes）/
│   └── service/             # 入站进程层
│       ├── worker.py        # ListingWorker：消费 job:*、进程级复用池化 checkpointer、发布 evt/result
│       └── __main__.py      # 进程入口 `cd ai-engine && python -m src.service`（SIGINT/SIGTERM 优雅停机）
└── tests/                   # pytest（pythonpath=["."] / testpaths=["tests"]，见 ai-engine/pyproject.toml）
    ├── conftest.py                      # 集成夹具：临时容器库灌 schema + 造/清业务数据（DSN 未设则 skip）
    ├── test_workflow_checkpoint.py      # 图级：interrupt/resume + 驳回 + 线程隔离（内存 checkpointer，无需 PG）
    ├── test_agent_loop.py               # Agent 循环：工具往返/预算熔断/工具上限/未知工具/运行时降级/事件
    ├── test_agent_middleware.py         # 中间件：上限边界/重试兜底/洋葱顺序/after_model 改写
    ├── test_agent_tools.py              # 工具：端口裁剪/多租户红线（org 不进 schema）/参数收敛/节点 no-op
    ├── test_agent_tools_pg.py           # 集成：工具真读业务数据 + role_pa_ai 写 products 被拒（红线）
    ├── test_worker_e2e.py               # 集成：投递 job:generate → 落库/事件/终态结果（需 PG + Redis）
    ├── test_llm_function_calling.py     # 冒烟：真实 function calling（需 PA_TEST_LLM=1 + ZHIPU_API_KEY）
    └── test_pg_checkpointer.py          # 集成：PostgresSaver 建表/读写/search_path/角色红线/delete_thread
                                         #   （需 PA_TEST_PG_DSN，未配置则自动 skip）
    ├── test_image_probe.py              # 图片：PNG/JPEG/GIF/WEBP/BMP 真实宽高与 MIME 探测（畸形字节不抛异常）
    ├── test_image_normalize.py          # 图片：规格化（不裁剪/不放大/EXIF 转正/alpha 合成/AI 标识/环境变量工厂）
    ├── test_image_node.py               # image 节点：实测尺寸元数据/格式后缀/上传图重落与回退/失败降级红线
    ├── test_image_cogview_payload.py    # 生图：watermark_enabled 契约 + 模型尺寸收敛矩阵 + 图床下载流程
    └── Dockerfile                       # 集成测试镜像（由 infra/docker-compose.ai-test.yml 构建）
```

---

### 五、backend-api 模块（P0~P4 已落地）

> 模块级契约细节、API 面清单、与 ProductPilot 的取舍：见 **`backend-api/README.md`**（该文件是 backend 侧契约的单一事实源）。

#### 模块介绍

- backend-api 为业务 API 网关层（FastAPI + async SQLAlchemy + PostgreSQL + Redis），**不含任何 AI 逻辑**：
  不调 LLM、不跑 LangGraph、不写 `schema_pa_ai` 业务表（只读 `product_contents` / `evaluation_logs`）。
- 唯一身份：**frontend 只与 backend 交互**（绝不跨层直连 ai-engine）；backend 与 ai-engine **只经 Redis Streams 通信**。
- 数据库角色：只以 `role_pa_backend` 连库（`database/sql/0002_roles_grants.sql` 矩阵）；
  `delete_audits` 仅 SELECT/INSERT（`0004` 已 REVOKE UPDATE/DELETE —— 审计只能追加）。
- 商品状态机（`products.status`）与任务状态（`generation_jobs.status`）**只有 backend 能写**：
  ai-engine 把终态发到 `result:workflow`，由本模块的消费器推进。

#### 闭环（一条命令到一次状态推进）

```text
POST /api/v1/products/{id}/generate
└─ ProductService.trigger_generation
   ├─① 守卫：商品非 deleted/archived、且无进行中任务（防重复触发）
   ├─② 建 generation_jobs(running) + products.status='generating' + 绑定 active_thread_id
   ├─③ 入队瞬间固化 rules 合规快照（含 effective_at/expires_at 时效过滤）
   ├─④ commit 之后 XADD job:generate（投递失败 → 回滚状态并报 503，绝不卡在 generating）
   └─⑤ ai-engine ListingsWorker 消费 → 图执行 → 发回 result:workflow
      └─ WorkflowResultConsumer（常驻）
         ├─ published      → products.published      + jobs.succeeded
         ├─ awaiting_human → products.waiting_approval + jobs.waiting_input
         │                   + hitl_approvals(pending, content_snapshot) + notification_outbox
         ├─ rejected       → products.draft          + jobs.failed
         └─ failed         → products.draft          + jobs.failed
POST /api/v1/approvals/{id}/approve|reject
└─ ApprovalService.decide（CAS：UPDATE … WHERE status='pending'）→ commit → XADD job:approval
   └─ ai-engine resume 图 → 再发 result:workflow（published / rejected）
      漏投兜底：ApprovalRedriveWatchdog（Redis SETNX 节流）与 POST /approvals/{id}/redrive
```

#### 实时过程（SSE，P5）

```text
GET /api/v1/products/{id}/stream-ticket      ← Bearer access 换短时票据（kind=sse，绑定商品，默认 120s）
GET /api/v1/products/{id}/stream?ticket=…    ← SSE：回放 + 尾随（无票据时兼容 Bearer，便于 curl 排查）
   ├─① 校验票据（kind / 绑定商品 / org）+ 商品归属 → 越权与不存在一律 404
   ├─② 状态门：products.status ∈ {generating, waiting_approval} 才回放/尾随；
   │     其余状态 → 只发一条 ready 控制帧并关流（避免前端把已结束任务当「生成中」空转）
   ├─③ 回放 evt:{thread_id}（支持 Last-Event-ID 续传；XRange 用独占下界 → 不重发）
   ├─④ 每 ~0.25s 增量尾随（同步 Redis 丢线程池；见 services/stream_reader 的踩坑说明）
   └─⑤ 终态关流：done / rejected / failed / hitl.waiting
         空闲关流：注释帧 `: stream-idle-close`（前端带 Last-Event-ID 自动续连）
```

#### 三个常驻任务（`create_app` 的 lifespan 启动；`PA_ENV=test` 不启动）

| 任务 | 作用 | 失败时 |
|---|---|---|
| `WorkflowResultConsumer` | 消费 `result:workflow` 推进状态机 | 单轮异常退避重试，**不退出进程** |
| `OutboxDeliverer` | 投递审批通知（outbox → 钉钉/控制台） | 指数退避重试，达上限转 `dlq`；`payload.last_error` **只在未送达期间存在**（送达即清除）——「曾失败过」看 `retry_count > 0`，界面据此显示「已发送（重试 N 次后成功）」 |
| `ApprovalRedriveWatchdog` | 补投「已定案但 ai-engine 未收到」的 resume | 同上，且带 Redis 节流防重复投递 |

#### 一键启动（本地四进程 + 完整 RAG 链路）

> 作品集宣传页（`portfolio/`，:5175）**不在这四进程内**：它零后端依赖、常年独立部署，
> 单独一条命令起 —— `./scripts/dev-landing.sh`（理由见下方「作品集宣传页（`portfolio/`，P11）」段）。

```bash
./scripts/dev-up.sh          # 前台：四路日志实时滚动（[backend]/[ai-engine]/[frontend]/[mobile]），Ctrl-C 一键收盘
./scripts/dev-up.sh --detach # 后台：日志落盘 /tmp/padev/*.log，用 ./scripts/dev-logs.sh 跟进
./scripts/dev-down.sh        # 停四进程（容器保留）；--with-infra 连容器一起停（数据卷保留）
./scripts/dev-landing.sh     # 作品集宣传页（portfolio/，:5175，纯静态无后端依赖；Ctrl-C 停）
./scripts/test-frontend.sh   # 桌面端用例（自带超时与「以文件级 ✓ 判定」的收尾逻辑，见 frontend/README）
./scripts/test-mobile.sh     # 移动端 H5 用例（同一判定口径，见 mobile-h5/README）
./scripts/test-mobile-rn.sh  # 移动端原生 App 用例（Jest + RNTL，同一判定口径，见 mobile-rn/README）
```

脚本做的事（每一步都有可读输出，失败给出确切处置命令）：

1. **前置自检**：`infra/.env` 存在并导出 → `env-check.sh` 快检 → 四个模块的 venv/node_modules 齐备 →
   PG 可达（`pg_isready`）→ 8000/5173/5174 端口空闲（**占用则指名占用者，不静默换端口**）→ 无上次遗留进程；
2. **基础设施**：Redis 缺失自动拉起；etcd/minio/milvus 一并拉起（Milvus 依赖前两者 healthy，
   首启需拉镜像 + 30s 缓冲，长等待每 10s 打印进度）→ Milvus healthy 后**幂等**初始化集合
   `pa_listing_vec`（`database/milvus/init_collections.py`）；容器若已由 `docker run` 手工起（无 compose 标签）
   则直接 `docker start` 复用，避免同名冲突；
3. **四进程**：各自独立**会话/进程组**启动（`setsid --fork`），日志实时加前缀回显 + 同时落盘**原始行**
   （可 grep/回溯）；`PYTHONUNBUFFERED=1` 保证 Python 逐行实时；
4. **就绪探测**（探真实依赖，不看进程存活）：backend `/readyz`（PG+Redis 都通才算好）、
   frontend 与 mobile 返回含 `id="root"` 的 SPA、ai-engine 出现 `pa:{env}:worker:heartbeat:*`（真在消费）；
   Milvus 未就绪**不阻塞**启动，只告警「RAG 降级」（worker 本身支持降级运行）；
5. **停止**：按进程组 `TERM → 2s → KILL`，连 vite 的 node 子进程、uvicorn 的 reloader 一起收，
   再用项目级 `pkill` 兜底 —— 保证不留孤儿占住端口。

常用开关：`--no-ai`（只起前后端，省内存）、`--no-mobile`（不起移动端 H5）、`--no-infra`（不动容器）、
`--no-reload`、`--strict-env`（env-check 的 WARN 也阻断）。

> 移动端真机联调：手机与开发机同网段时直接访问 `http://<本机局域网IP>:5174`
> —— 它和桌面端一样走 vite 的 `/api` 代理，**不需要 CORS**、也不需要改 `BACKEND_CORS_ORIGINS`。

#### 日志与排障（四进程 + 跨进程串接）

```bash
./scripts/dev-logs.sh all            # 或 backend / ai-engine / frontend / mobile（带服务前缀实时跟随）
grep -a 'POST /api/v1/products'      /tmp/padev/backend.log   # 触发生过生成没
grep -a '/stream?ticket'             /tmp/padev/backend.log   # SSE 连了几次（「生成后 0 次」= 前端没重连）
grep -a 'result-consumer'            /tmp/padev/backend.log   # 结果是否被消费、商品被推进到什么状态
grep -a 'request_id=<id>'            /tmp/padev/*.log         # 一次生成跨 backend/ai-engine/result 三段日志串起来
grep -a '配图'                        /tmp/padev/ai-engine.log # 配图结果 / 降级原因（见下方「没有配图」条目）
```

- 日志落盘 `${PAD_LOG_DIR:-/tmp/padev}/{backend,ai-engine,frontend}.log`，**每次启动把上一轮滚成 `.log.1`**
  （保留 `PAD_LOG_KEEP` 份，默认 3）—— 不再清空历史现场；
- 三份日志都带**时间戳**（backend 由 `core/logging.py`、ai-engine 由 `service/logging_setup.py` 装配，
  级别可用 `BACKEND_LOG_LEVEL` / `AI_ENGINE_LOG_LEVEL` 调）；
- `X-Request-Id` 由前端注入 → backend 写进 `job:generate` → ai-engine 与 result 消费器日志打印 → 全链路可 grep（见 backend README §2.2）；
- **商品卡在「生成中」、按钮点不动**：自动兜底是 reaper（超期回收）+ 守卫宽容（再点一次生成即自愈）；
  要立刻处置去 `/ops` 面板「卡住任务」→ 终止（写审计）。完整 SOP 见 `backend-api/README.md` §六。
- **详情页一直没有配图（任务却是 succeeded）**：配图失败一律降级纯文本（宁可无图也不丢已通过文案），
  所以业务上「成功」不等于「有图」。两处显式留痕：
  - **启动自检**（`service/__main__.py` → `adapters/oss.probe_oss_bucket`，只读 HEAD，不写对象/不建桶）：
    `OSS_BUCKET` 指向不存在的桶时启动即打印
    `⚠ 对象存储自检失败（配图与上传图转存将降级为纯文本）：bucket=… 不存在（HTTP 404 NoSuchBucket）…`；
  - **每次生成一行结果**（`worker.image_outcome_line`）：
    `[worker] 配图结果 thread_id=… image_attached=True` 或
    `[worker] 配图降级为纯文本 thread_id=… image_attached=False reason=AI 配图失败…OSS put HTTP 404…`。
  排查顺序：① 看这一行的 `reason`（生图 / 规格化 / OSS 上传三段其一）；② `NoSuchBucket` = 桶没建或桶名/地域不对
    （2026-09 实测：`OSS_BUCKET` 配成了从未创建的桶 → 每次都静默降级）；③ `SignatureDoesNotMatch` = AK 与桶不匹配。
- **驳回后进详情页看不到内容**：两个独立原因，都已修 ——
  ① 「已保存内容」在**批准前不写 `product_contents`**（设计如此：驳回走 reject_end，不落库），
     被驳回的图文去「审批与驳回复盘」看；
  ② 复盘卡此前按 `product_id` 查审批历史时**没传 status**，被 backend 缺省 `pending` 过滤掉 → 已定案单
     永远查不到（界面显示「该商品还没有审批记录」）。现在前端显式传 `status=all`；
     另：内容/轨迹/审批三路查询失败会显示可重试的告警，不再被当成"没有数据"。
- **审批中心「补投」点了没变化**：这是正确的 —— 补投只对「已定案但引擎没收到（商品仍停在待审批）」
  有意义。判据与补投守护一致：商品已推进（published/draft）说明引擎已消费，此时再 resume 是
  **静默 no-op**（实测：不报错、不重跑、不重复落库、不翻转决策），后端直接回 `not_needed`，
  界面提示「无需补投」。按钮现在**只在商品仍停在待审批时出现**，且每次补投都落 `approval_redrive_audits`
  （详情抽屉显示次数与最近结果；1 小时节流，重复点击不会重复投递）。
- **合规命中后为什么还能上架 / 命中没影响**：三层原因 ——
  ① 输入侧闸门 `AI_ENGINE_RULE_PRECHECK` 原先**默认关**（且只扫商品标题）：标题含「最便宜」也照样生成上架
     （实测商品 A-3C-0023）。现在**默认开**，命中即任务 failed + 商品回 draft；
  ② 生成正文的命中是"扣分 + 重写 + 转人工"，**人工可放行**（业务决策），但现在带命中点批准**必须写理由**
     并落 `approval_overrides` 审计（谁在知情下放行了哪条命中点）；
  ③ low（提示级）命中此前被直接丢弃（既不扣分也不可见），现在参与扣分、写入 violations、喂给 LLM 复核。
- **任务失败但看不到原因**：`result:workflow` 现在带 `error`（合规拦截 / 异常摘要）→ backend 写入
  `generation_jobs.error` → 详情页「最近一次任务失败原因」直接显示
  `input_compliance_blocked(商品标题): 命中违禁词「最便宜」（广告法种子）`。


#### 运行与测试

```bash
# 依赖隔离：本模块与 ai-engine 各自独立 venv，严禁合并
cd backend-api && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
# 本地运行（宿主已起 PG；Redis 见 infra/docker-compose.yml）
# 提示：日常直接用 ./scripts/dev-up.sh 起全栈；下面是与脚本等价的手工命令
set -a && source ../infra/.env && set +a
.venv/bin/uvicorn pa_backend.main:app --app-dir src --port "${BACKEND_PORT:-8000}"

# 测试（宿主机只需 Docker：一次性 PG + Redis + pytest，160 例）
docker compose -f infra/docker-compose.backend-test.yml up --build \
  --abort-on-container-exit --exit-code-from backend-tests
docker compose -f infra/docker-compose.backend-test.yml down -v
```

#### 契约互补点（两侧互不共享代码，靠契约对齐 —— 改一处必须改另一处）

| # | 事实 | backend 的对应做法 |
|---|---|---|
| 1 | ai-engine 的 `compile_rules_snapshot` **不做时间过滤** | 时效过滤在**入队瞬间**完成（`repositories/compliance.build_rules_snapshot`） |
| 2 | `products.raw_images` 规范形状 = `list[str]` 公有 URL 数组 | 写入即规范化；响应层同时兼容历史 `[{url:…}]` 形状（`schemas.serialize_product`） |
| 3 | `_handle_purge` 守卫：商品行仍存在 → **中止清理并 ack 丢弃** | 彻底删除**物理删行 + commit 之后**才投递 `job:product_purge`（见 `backend-api/README.md` §2.7） |
| 4 | purge 只清 `product_contents` 的 image，**不含** `products.raw_images` | 上传原件由 backend 自己删（`services/oss.delete_urls`，`img/pa/` 白名单） |
| 5 | 重复投递返回 `duplicate`/`busy` 属正常 | 消费器按「已终态跳过」处理，不当作错误上报 |
| 6 | 合规匹配语义（最长匹配 → 左优先去重叠 → 正则 finditer → 按严重级稳定排序） | 预览匹配器 `services/compliance_matcher.py` 逐条对齐；一致性由用例锁定（改一侧必须改另一侧） |
| 7 | 坏正则在 ai-engine 侧**静默跳过** | `/compliance/rules` 入参当场做语法校验，把「看起来生效其实没拦」挡在入库前 |

#### 合规词库与运维面（P6）

```text
GET|POST /api/v1/compliance/words          → 词库 CRUD（读：admin/reviewer；写：仅 admin）
PATCH|DELETE /api/v1/compliance/words/{id}
GET|POST /api/v1/compliance/rules          → 正则规则 CRUD（入参即校验正则语法）
PATCH|DELETE /api/v1/compliance/rules/{id}
GET  /api/v1/compliance/snapshot           → 下一次生成会下发的规则快照（排查「配了却没拦」）
POST /api/v1/compliance/preview            → 文本命中预览（位置 + 分数 + 是否阻断，
                                              用「当前快照」跑，与入队口径一致）
GET  /api/v1/ops/overview                  → worker 心跳（含 stalled 判定）/ 流长度与消费组 PEL / DLQ 概况
GET  /api/v1/ops/dlq?domain=job:generate   → 死信回看（只读；重投走 backend-api/README 的 SOP）
```

两条刻意的设计取舍：
- **全局配置的写只给 admin**：`compliance_*` 没有 `org_id`，改一个词会影响**所有组织**的生成结果；
- **运维面只读**：DLQ 里的消息是「重试到上限仍失败」的，盲目重投只会再造一条毒消息，
  处置必须走人工 SOP（先判根因）——所以接口里刻意不提供「一键重投」。

#### 阶段进度（本模块）

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 契约冻结 + `infra/.env.template` 登记 `BACKEND_*` + `env-check.sh` 扩展扫描范围 | ✅ |
| P1 | 骨架（config/db/security/deps/keys/middleware/统一信封/探针）+ 容器化测试编排 | ✅ |
| P2 | 认证（注册/登录/续签轮换/登出）+ 成员 RBAC + 登录失败限流 | ✅ |
| P3 | 商品 CRUD/彻底删除（软删已按业务决定移除，仅保留显式 405）+ OSS 预签名 + CSV 导入 + 内容版本与轨迹只读 | ✅ |
| P4 | 生成触发 + `result:workflow` 消费闭环 + 审批 CAS/resume + 通知 outbox + 补投守护 | ✅ |
| P5 | SSE（`stream-ticket` + `stream`：回放 + 尾随 + 终态/空闲关流 + Last-Event-ID 续传） | ✅ |
| P6 | 合规词库/规则 CRUD + 快照预览；运维只读面（心跳/PEL/DLQ） | ✅ |
| P7 | frontend（React 19 + antd 6 + Vite 8）：登录/商品/详情（打字机 + 轨迹）/审批（含深链）/合规/运维/成员，8 条路由 | ✅ |
| P8 | nginx 双层 + 生产 compose + CI + 全栈冒烟 | ⏳ 待做 |
| P9 | mobile-h5（React + antd-mobile）：登录 / 商品 / 详情（SSE）/ 审批（含深链）/ 我的；与桌面端共享契约核心层 | ✅ |
| P10 | mobile-rn（React Native + Expo 57）：iOS + Android 一套代码（6 屏 + 深链）；共享层抽出「平台端口」承载 6 个平台差异点 | ✅ |
| P11 | portfolio（作品集宣传页）：数据驱动的作品合集 + 四端演示位 + 联系方式；纯静态零后端依赖（Tailwind v4 只装在本模块） | ✅ |

#### 前端（`frontend/`，P7）

```text
技术栈 : React 19 + TypeScript + Vite 8 + Ant Design 6 + Zustand + React Query v5 + React Router v7
红线   : 只访问 /api/v1（dev 由 vite 代理 → :8000；生产由 nginx 同源反代，P8）；绝不直连 ai-engine / PG
路由   : /login · / (商品) · /products/:id (详情：SSE 打字机 + 轨迹 + 驳回复盘) · /approvals (+ /approvals/:id?ticket=)
         · /compliance (词库/规则/预览/快照) · /ops (心跳/PEL/DLQ，只读) · /members
校验   : npx tsc --noEmit（0 错误）· npm test（48 例）· npm run build（生产构建 1617 modules）
```

三处**照抄 ProductPilot 会错**的 PA 语义（详见 `frontend/README.md` 的表）：

1. `hitl.waiting` 在 PA 是**终态**（服务端随即关流）→ 前端必须停重连并显示「已转人工审批」；
2. `ready` 是 backend 的控制帧（无进行中任务）→ 显示提示并停止，不空转重连；
3. 注释帧（`: stream-idle-close` / `: stream-error`）不触发 `onmessage` → 必须文本层区分
   「空闲关流（续连）」与「读流异常（限量重试）」。

另外：删除**只有彻底删除**一条路径（软删端点已下线 → 405）、错误信封是 `{code,data,message}`（不是 `detail`）、
OSS 预签名需 `product_id`（图片上传在商品详情页）。

#### 作品集宣传页（`portfolio/`，P11）

```text
定位   : 个人作品合集展示页（第一个作品即 ProductAssistant）；将来加作品只加一条数据
技术栈 : React 19 + TypeScript + Vite 8 + Tailwind CSS 4（原子化 CSS **只装在本模块**）
入口   : http://localhost:5175（纯静态、无需后端；启动：./scripts/dev-landing.sh，也可独立部署到任意静态托管）
数据   : src/data/projects.ts（唯一内容事实源）· 资产约定 public/demos/<slug>/<platform>.<ext>
校验   : npx tsc --noEmit（0 错误）· npm test（3 文件 21 例）· npm run build（CSS 22KB / JS 230KB，gzip 4.5KB / 73.5KB）
门禁   : 用例里有一条「声明即校验」——数据里写了 video/gif/poster 就必须在 public/ 下真实存在，
         挡住「素材文件名写错 → 上线后是一块空白」（静态页上这类错没人会及时发现）
```

三处刻意的设计取舍（详见 `portfolio/README.md`）：

1. **单独开模块而不是在 `frontend/` 加路由**：作品集是跨项目的长期资产，生命周期与业务前端不同；
   本模块零后端依赖、自包含，将来要拆成独立仓库可原样搬走（`main.tsx` 里没有 `/api`、没有代理）；
2. **四端演示「一次只挂载一个播放器」**：手机上 4 个视频同时自动播会掉帧发热；顺带做到「未点开的端零下载」，代价是切回来重新加载（片段短，可接受）；
3. **「进入系统」三态**：演示环境未接入时是**禁用占位态 + 邮件联系**（访客不会点到一个 404），
   接入时只改 `data/projects.ts` 里的 `links.live` 一行 —— 占位态另有一条用例守着，改状态时会提醒你同步断言。




