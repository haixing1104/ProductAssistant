# ProductAssistant 产品上线助手

> **B 端商品文案工作台**：AI 生成 → 规则与模型双评估 → 人机协同终审 → 落库上架。
> 覆盖 PC Web 工作台、移动 H5、React Native 原生 App 三种客户端，外加一个纯静态作品集宣传页。
>
> 本文是**对外中文主文档**（英文版见 [`README_EN.md`](README_EN.md)），只涉及**功能与核心架构设计**：
> 每个模块解决什么问题、边界画在哪、复杂度落在什么地方、技术选型如何取舍。
> 🌐 中文 | [English](README_EN.md)

## 目录

| 章节 | 内容 |
|---|---|
| [一、项目介绍](#一项目介绍) | 业务闭环、角色与多租户、客户端形态、技术选型 |
| [二、设计原则](#二设计原则) | 五个模块的划分与依赖红线、六边形架构、契约优先、数据隔离 |
| [三、gif 预览](#三gif-预览) | 三端 7 段真实运行的动图 |
| [四、AI-engine 的编排逻辑](#四ai-engine-的编排逻辑) | LangGraph 图编排、三条任务链路、事件总线、HITL+钉钉审批、只读取证 Agent |
| [五、backend 逻辑](#五backend-逻辑) | 状态机单写、SSE 实时过程、常驻任务与幂等加固 |
| [六、frontend 逻辑](#六frontend-逻辑) | 桌面工作台、三端共享契约核心层、SSE 语义陷阱 |
| [七、mobile 逻辑](#七mobile-逻辑) | H5 与原生 App 的取舍、平台端口、三端一致性 |
| [八、部署逻辑](#八部署逻辑) | 分层部署、镜像流水线、CI/CD、队列级 Redis |
| [九、db 逻辑](#九db-逻辑) | 双 schema / 四角色、权限矩阵、向量库、测试规模 |

---

## 一、项目介绍

### 1.1 它解决什么问题

电商上架一件商品，运营要写标题、卖点、详情文案，还要逐条对照广告法与平台规则，写完再走一轮审批。
四个痛点决定了这个项目的形态：

| 痛点 | 应对 |
|---|---|
| 文案产能低、风格不稳定 | AI 生成 + **历史高转化文案的语义召回**（RAG Few-Shot）作参照 |
| 模型会**编造**价格 / 库存 / 资质 | 生成前有一轮**只读取证 Agent**，用真库素材校准「事实要点」 |
| 合规判定靠人肉，漏检代价高 | 「确定性规则引擎 + LLM 语义评估」双闸门，规则命中即 fail-fast 跳过模型 |
| 审批与生成脱节（谁改了什么说不清） | 生成 → 评估 → 人工审批的状态机闭环，全过程事件可回放 + 审批审计留痕 |

一句话：把「生成」做成一条**可观测、可中断、可追溯**的流水线（ https://www.seektruth.org.cn/welcome/ ），而不是一个聊天框。

### 1.2 一条链路的业务视图

```text
运营在 Web / H5 / App 点「生成」
   │
   ▼
backend：建任务 + 固化合规规则快照 + 投递 job:generate（Redis Streams）
   │
   ▼
ai-engine：rag_retrieve → agent_research(只读取证) → generate(流式) → evaluate(规则 + LLM)
   │                                   ├─ 通过且低价 ────────► image → save_content
   │                                   ├─ 未过且未超限 ──────► generate（Reflection 重写）
   │                                   └─ 未过超限 / 高价 ──► image → hitl（挂起等人工）
   │
   ├─ 过程事件 evt:{thread_id} ──► backend SSE ──► 三端界面（打字机 / 阶段 / 图片就绪 / 终态）
   └─ 终态结果 result:workflow ──► backend 消费器推进 products.status、建待审批单、发通知
                                        │
                                        ▼
                     人（审批中心）批准 / 驳回 ──► job:approval ──► ai-engine resume 图
                                                          └─► 落库并发布 / 驳回定案
```

### 1.3 角色、多租户与状态机

| 项 | 设计 |
|---|---|
| 角色 | `admin`（组织、成员、合规词库/规则、运维只读面）· `reviewer`（审批）· `operator`（建品、触发生成） |
| 平台超管 | `sys_users.is_superuser`：登录后顶栏出现「组织选择器」，选中哪个租户就操作哪个租户（走 `X-Org-Id` 头） |
| 多租户 | 除合规词库/规则（**全局配置**，故意不带 `org_id`）外，业务表都带 `org_id`；越权与不存在**一律 404** |
| 商品状态机 | `draft → generating → waiting_approval → published`，驳回 / 失败回落 `draft` |
| 任务状态机 | `generation_jobs`：`running → succeeded / waiting_input / failed`；超期未终态由 reaper 回收 |
| 谁能写状态 | **只有 backend**。ai-engine 只发终态结果，状态推进权不给 AI 层（红线，见第五章） |

### 1.4 四个前端产物（三种形态 + 一个宣传页）

| 产物 | 形态 | 定位 | 关键取舍 |
|---|---|---|---|
| `frontend/` | PC Web 工作台 | 运营 / 审批员的主战场：8 条路由，详情页有打字机 + 轨迹 + 驳回复盘 | 只连 `/api/v1`，绝不跨层直连 ai-engine / PG |
| `mobile-h5/` | 移动 H5 | 审批与轻量操作为主，浏览器即开即用 | 与桌面端**共享契约核心层**，页面壳各写各的 |
| `mobile-rn/` | 原生 App（iOS + Android） | 一套代码两端；6 屏与 H5 **逐条对齐** | 共享层抽出「平台端口」承载 6 个平台差异点，UI 自研零 UI 依赖 |
| `portfolio/` | 静态作品集宣传页 | 三端演示（7 段动图）+ 作品合集 + 联系方式 | 零后端依赖、可独立部署；生命周期与业务前端不同，故单独成模块 |

### 1.5 技术选型总览

| 层 | 选型 | 选它的理由 |
|---|---|---|
| 编排 | Python + **LangGraph**（StateGraph + PostgresSaver） | 需要「可中断 / 可恢复的人工审批」：图状态持久化 + `interrupt`/`resume` 是原生能力，比手写状态机可靠 |
| AI 网关 | 智谱 GLM（文本 / 生图 / embedding），OpenAI 兼容协议 | function calling 走标准 `tools` 协议，接入 Agent **零新增依赖** |
| 业务 API | **FastAPI** + async SQLAlchemy + Pydantic | 异步是 SSE 长连接的前提；Pydantic 顺手承担契约校验 |
| 桌面前端 | React 19 + TypeScript + **Vite 8** + Ant Design 6 + Zustand + React Query v5 | 三端共享 TypeScript 契约层；antd 覆盖中后台重型交互 |
| 移动端 | antd-mobile（H5）· React Native + Expo（原生） | 同一份契约核心层，两种渲染栈各自最优 |
| 存储 | PostgreSQL 17（2 schema / 4 角色）· Redis Streams（任务与事件）· Milvus（向量）· OSS（图片） | AI 层与业务层**库内物理隔离**（见第九章） |
| 部署 | Docker Compose + nginx 双层 + 阿里云 ACR + GitHub Actions | 单机 2C2G 也能跑；镜像在 CI 构建、服务器只 pull |

### 1.6 完成度与已知边界

| 阶段 | 内容 |
|---|---|
| P0~P6 | 契约冻结、backend 骨架与认证、商品 CRUD 与彻底删除、生成闭环与审批 CAS、SSE、合规词库与运维只读面 |
| P7~P11 | PC 工作台、nginx 双层生产编排 + CI/CD、移动 H5、原生 App、作品集宣传页 |
| 已知边界 | 暂未接真实电商平台（淘宝 / 京东 / 拼多多）的合规判定 |
| 测试规模 | 数据库 102 例 · backend 194 例 · ai-engine 239 例 · 四个前端各有独立用例集（桌面 92 / H5 24 / 原生 66 / 作品集 55） |

---

## 二、设计原则

### 2.1 五个模块与依赖方向

```text
backend(业务 API)      frontend(PC)   mobile-h5   mobile-rn      portfolio(静态)
      │                     │              │           │                │
      │                     └──────────────┴───────────┘                │  只调 /api/v1
      │                        （共享 @pa/core 契约核心层）              └─► 零后端依赖
      ▼
   Redis Streams（backend ↔ ai-engine 唯一业务通道）
      │
      ▼
ai-engine(AI 编排) ──► 只读业务表；绝不写 products.status
      │
      ▼
database（2 SCHEMA / 4 ROLE）        infra（容器 / nginx / CI）
```

三条硬红线（都有用例锁定）：

1. **frontend 只与 backend 交互**，绝不跨层直连 ai-engine；backend 与 ai-engine **只经 Redis Streams 通信**。
2. **ai-engine 不能直接操作 backend 的库表**：物理上也没有权限（角色矩阵 + 负向断言用例）。
3. **商品状态机只有 backend 能写**：AI 层只能发布终态结果，由 backend 消费器推进。

### 2.2 六边形（端口-适配器）架构

ai-engine 是这套原则最彻底的体现：`ports/`（13 个出站抽象）只有 `abc` 和标准库、**不含任何实现**；
`adapters/` 负责实现（PG / Redis / 智谱 / OSS / Milvus / Pillow / 规则引擎），且**不 import 内核**。
好处是「换实现不改内核」。

| 层 | 作用 | 允许 import | 红线 |
|---|---|---|---|
| `service/` 入站进程 | 消费任务、装配端口、驱动/恢复图、发布终态 | 全部 | 不含 HTTP 路由与鉴权；不写 `schema_pa_backend` |
| `workflowcore/graph` | 建图（节点 + 条件边 + checkpointer 工厂） | 内核 + ports + langgraph | 不 import adapters；不直连 DB/Redis |
| `workflowcore/node` | 纯函数：只读 State、返回增量 | ports + state | **禁止 DB/Redis/HTTP 直连**；只有 `node_hitl` 可 `interrupt()` |
| `workflowcore/agent` | Agent 子图 + 中间件 + 工具注册 | ports + langgraph | 不提供任何**写**工具；产物只进 State |
| `ports/` | 出站抽象（接口 + 契约 + 常量） | 仅标准库 | 不含实现 |
| `adapters/` | 端口实现 | ports + 三方库 | 不 import 内核 |

### 2.3 契约优先：两侧互不共享代码

Python 侧（ai-engine）与 TypeScript 侧（三端前端）不可能共享代码，因此**契约就是唯一事实源**，
靠「契约互补点 + 用例」双向锁定：

| # | 事实 | 另一侧的对策 |
|---|---|---|
| 1 | ai-engine 的规则快照**不做时间过滤** | 时效过滤在**入队瞬间**完成（backend 的 `build_rules_snapshot`） |
| 2 | `raw_images` 规范形状是 `list[str]` | 写入即规范化；响应层兼容历史 `[{url:…}]` 形状 |
| 3 | purge 的守卫是「商品行还在 → 中止清理」 | 彻底删除必须在**物理删行 + commit 之后**才投递 purge |
| 4 | purge 只清 AI 侧内容的图片 | 上传原件由 backend 自己删（`img/pa/` 白名单） |
| 5 | 合规匹配语义（最长匹配 → 左优先去重叠 → 稳定排序） | 预览匹配器逐条对齐，一致性由用例锁定 |
| 6 | 坏正则在 AI 侧**静默跳过** | 规则入参当场校验正则语法，把「配了却没拦」挡在入库前 |

### 2.4 单一事实源清单

- `frontend/src/services/streamLabels.ts`：三端**共用一份**事件渲染
- `portfolio/src/data/projects.ts`：作品集全部内容 + 入口链接；用例做**「声明即校验」**
  （写进数据的 gif 必须真实存在）。
- `ai-engine` 的 `state/schemas.py`：LLM 结构化输出的 JSON Schema 单一事实源。
- `ai-engine` 的 `ports/event_bus.py`：所有出站消息信封统一带 `schema_version`（单点注入）。
- `infra/.env.template`：环境变量**唯一键清单**；`.env` 与模板由脚本保持同向一致（幂等追加、绝不覆盖已有值）。

### 2.5 数据隔离：2 SCHEMA / 4 ROLE（细节见第九章）

- `schema_pa_backend`：业务命名空间（organizations / sys_users / products / hitl_approvals /
  compliance_words / compliance_rules / generation_jobs / notification_outbox / delete_audits）。
- `schema_pa_ai`：AI 命名空间（product_contents / evaluation_logs / LangGraph 的 checkpoint 四表）。
- 四个角色：`role_pa_admin`（DDL）· `role_pa_backend`（业务 DML）· `role_pa_ai`（AI DML + 业务只读）·
  `role_pa_ai_setup`（仅建 checkpoint 表）。
- 效果：**AI 层即使代码写错也写不动业务表**（有红线负向断言用例守着）。

---

## 三、gif 预览

全部动图来自**真实运行的界面**（真后端 + 真模型调用 + 真 Redis 事件流），录屏后重编码为 gif，
已做无损优化（`gifsicle -O3`）。资产按 `portfolio/public/demos/pa/<platform>/` 约定存放。

### 3.1 PC Web 工作台（4 段）

| 商品列表（状态徽标 / 筛选） | 触发生成（打字机 + 阶段提示） |
|---|---|
| ![商品列表](portfolio/public/demos/pa/web/01-list.gif) | ![生成过程](portfolio/public/demos/pa/web/02-generate.gif) |

| 评估未过 → Reflection 重写 | 输入侧合规拦截（不可生成） |
|---|---|
| ![重写](portfolio/public/demos/pa/web/03-revise.gif) | ![合规拦截](portfolio/public/demos/pa/web/04-blocked.gif) |

### 3.2 Mobile H5（2 段）

| 移动端触发生成 | 审批（批准 / 驳回 + 深链） |
|---|---|
| ![H5 生成](portfolio/public/demos/pa/h5/01-generate.gif) | ![H5 审批](portfolio/public/demos/pa/h5/02-approve.gif) |

### 3.3 React Native 原生 App（1 段）

| iOS + Android 一套代码：概览 |
|---|
| ![原生 App](portfolio/public/demos/pa/rn/01-overview.gif) |

---

## 四、AI-engine 的编排逻辑

### 4.1 定位

纯 AI 引擎层：**不含任何 HTTP 路由与鉴权**（与语言层、功能层解耦）。它只做三件事：

1. 消费 backend 投递的 Redis Streams 任务（`job:generate` / `job:approval` / `job:product_purge`）；
2. 驱动 `ListingWorkflow`（生成 → 评估 → HITL 人工介入 → 落库 / 驳回），把图文产物与阶段事件发布回 `evt:{thread_id}`；
3. 把终态结果发到 `result:workflow`，由 backend 推进商品状态。

### 4.2 图编排（LangGraph StateGraph）

```text
START → rag_retrieve → agent_research(可选增强) → generate → evaluate
                                                     ├─ persist(通过且低价) → image_then_save → save_content → END
                                                     ├─ retry(未过未超限)   → generate（带上次评估意见重写）
                                                     └─ human(未过超限/高价) → image_then_human → hitl ─┬─ 批准 → save_content → END
                                                                                                        └─ 驳回 → reject_end  → END
```

关键设计点：

- **运行期真正调度节点的是 LangGraph 引擎**：`workflow.py` 里没有一行 `node.xxx()` 调用，它只用
  `functools.partial` 把「节点函数 + 端口」绑成可调用对象注册，再把边连好。所以「装配期（谁 new 谁）」
  与「运行期（谁调谁）」必须分开读。
- **每个节点是纯函数**：只读 State、返回增量 dict；业务逻辑全在节点里，IO 全在端口后面。
- **`image` 节点注册两次**（`image_then_save` / `image_then_human`）：让**进 HITL 之前就配好图**，
  这样 `interrupt()` 挂起时 `content_snapshot` 里已是完整图文，**审批人看到的与最终上架一致**。
- **HITL 靠 checkpoint 而非内存**：`node_hitl.interrupt()` 挂起，审批后 `Command(resume=…)` 恢复；
  恢复时图实例是**新建的**，靠 `thread_id` 从 checkpoint 读回状态（跨消息 / 跨进程 / 跨实例）。
- **条件边是纯函数**（只读 State）：`should_retry_or_human` 决定 `retry / persist / human`；
  「高价」阈值（500）触发转人工 —— 成本与风险被显式建模进拓扑，而不是靠提示词约束。

### 4.3 三条任务链路为什么必须分开设计

| 链路 | 语义 | 复杂度落在哪 |
|---|---|---|
| ① `job:generate` | 从零生成一件商品的图文 | 最长链路：多节点 + 条件边 + Reflection 重写 + 流式事件 |
| ② `job:approval` | **跨消息 / 跨进程 / 跨图实例**恢复一次挂起的生成 | 分布式锁 + checkpoint 恢复 + 「驳回也要给终态事件」 |
| ③ `job:product_purge` | 商品彻底删除后的物理清理 | **有序的前置收集**：行删了 URL 与 thread_id 就不可逆 |

约束：

- ③ 有**前置守卫**：商品行仍存在 = 孤儿 purge 消息（删除并没生效）→ 中止并 ack，防误删有效数据。
- ③ 的顺序是「先收集图片 URL、先反查 thread_id」→ 再删内容与评估日志 → 清 checkpoint 三表 →
  删 OSS 白名单对象 → 删向量；其中 3 步是 best-effort（失败只打印、不阻断 ack），任务幂等可重投。
- ② 用 `lock:{thread_id}`（`SET NX EX` + Lua CAS 释放）串行化恢复：抢不到锁的实例**跳过该条**（返回 `busy`），
  绝不允许两个实例同时 resume 同一线程；且**先放锁再 ack**（ack 抛错也不会让锁滞留到 TTL）。
- **`interrupt()` 之前不得有副作用**：resume 会让 `node_hitl` 整个函数**重新执行**一次。
- ② 的图**不带规则快照** —— 当前无影响（resume 只从 `hitl` 往后走，后续节点不用规则引擎）；
  但若将来把 resume 后的路径接回 `evaluate`，必须补传快照。这类「现在没问题、将来会踩」的点，
  在代码里都有显式注释。

### 4.4 事件总线：两个出站键，用途不同

| 出站键 | 内容 | 消费者 | 语义 |
|---|---|---|---|
| `evt:{thread_id}` | 过程事件 + 终态事件 | backend SSE 端点 → 三端界面 | **界面实时过程的唯一数据源**（打字机 / 阶段 / 图片就绪）；`MAXLEN 2000` + TTL 7 天 |
| `result:workflow` | 图终态结果 | backend `WorkflowResultConsumer` | **驱动商品状态机**（published / awaiting_human / rejected / failed）；内容快照仅在转人工时携带 |
| `lock:{thread_id}` | 互斥令牌 | worker ↔ worker | 审批恢复串行化 |

`evt` 上的事件类型（按链路出现顺序）：`generate.started` · `stage.researching` · `agent.tool` ·
`agent.done` · `stage.generating` · `content.chunk` · `stage.evaluating` · `evaluate.result` ·
`stage.imaging` · `image.ready` · `done` · `hitl.waiting` · `approval.resumed` · `rejected` · `failed`。

> **界面只渲染业务语言**：`stop_reason` / `turns` / `tool_calls` / 内部工具名 / `score=` / `source=uploaded`
> 这类实现细节**一律不渲染**，只留在载荷里供排障（`evt` 流 / 日志 / `agent_trace`）。

### 4.5 一次任务最多触发 6 处模型调用

| # | 触发点 | 说明 |
|---|---|---|
| ① | `rag_retrieve` 召回打分 | 文案向量化（embedding） |
| ② | `agent_research` 多轮取数 | function calling 只读工具 |
| ③ | `generate` 流式写文案 | 打字机的**唯一正文来源** |
| ④ | `evaluate` 结构化评估 | JSON Schema 强约束 + 失败回喂修复 |
| ⑤ | `image_*` 生图 | CogView（含下载图床） |
| ⑥ | `save_content` 定稿入向量库 | 供后续商品的相似召回 |

**降级设计贯穿全篇**：缺配置 → 工厂返回 `None` → 节点自行降级（确定性 Mock 文案 / 跳过写向量 / 无图纯文本），**绝不阻断启动，不能阻断前端渲染，提升用户体验**。
所以每类降级都有显式留痕（启动自检 + 每次生成一行结果 + 事件载荷）。

三套**互相独立**的配额（成本评估时必须分别计入，走 `retry` 边会让 ③④ 重复执行）：

| 配额 | 默认 | 超限行为 |
|---|---|---|
| Reflection 重试（评估未过 → 重写） | 2 | 条件边转人工（HITL） |
| JSON 结构修复（输出不合契约） | 2（最多 3 次调用） | 安全降级 `passed=False, score=0`，原因写 `last_eval_errors` |
| Agent 预算（模型轮次 / 工具次数） | 2 / 3 | 中间件在下一轮前取消，用已有证据收敛 |

### 4.6 只读取证 Agent

在 `rag_retrieve` 与 `generate` 之间插入 `agent_research` 节点：用 function calling 调**只读工具**
核对真实素材与历史记录，结论写进 `agent_context`，生成提示词把它作为「事实要点」带进去。

| 它解决的问题 | 应对 | 工具 |
|---|---|---|
| 模型幻觉编造价格 / 库存 / 资质 | 真读商品表；查不到返回 `{"found": false}`（**逼模型说「查不到」而不是编造**） | `get_product_facts` |
| 反复踩同一条违规词 | 拉历史评估的违规点 + 动笔前自查 | `get_eval_history` / `scan_compliance` |
| 被同一理由反复驳回 | 拉历史审批驳回意见 | `get_approval_history` |
| 不像本店历史高转化风格 | 语义召回同租户历史文案片段 | `retrieve_similar_copy` |
| 「为什么这么写」说不清 | 逐轮审计轨迹 + 实时事件（界面实时渲染） | `agent_trace` + `agent.tool` |

红线与边界：

- **不注册任何写工具**；产物只进 State，**不落库、不推进商品状态**。
- **多租户字段由闭包固化**（`org_id` / `product_id` **不进工具参数 schema**）—— 模型**无处**下跨租户参数，
  有红线断言用例；这一条比「在提示词里叮嘱不要越权」可靠得多。
- **按端口有无动态裁剪工具**：Milvus 未配 → 不注册 `retrieve_similar_copy`。避免「调了但永远为空」的幻觉入口。
- **降级红线**：未注入 runtime → 节点 no-op（链路行为与未接入前完全一致）；LLM / 网络异常 →
  收敛为 `stop_reason=runtime_error` **不抛异常**；研究失败**绝不**把商品判 failed（研究是「增强功能」不是「必需功能」）。
- **中间件三条策略**：模型调用上限（预算熔断）/ 工具调用上限 / 只读工具重试兜底；
  `wrap_tool_call` **逆序嵌套**（列表第一个 = 最外层）。**无独立开关**，要停只能整体关 Agent。
- 子图**不挂 checkpointer**：避免把内层往返状态写进**生成线程**的 checkpoint，破坏 HITL 的 interrupt/resume 语义。

**默认开，且是可逆的**：`AI_ENGINE_AGENT_ENABLED` 未配置即视为开 —— 理由是「用真库素材 / 历史违规点 /
审批意见校准事实」应当是**默认质量下限**，而不是少数场景的奢侈品。

### 4.7 checkpoint 生产方案（HITL 的底座）

| 环节 | 角色 | 说明 |
|---|---|---|
| 建表（幂等、一次性） | `role_pa_ai_setup` | `PostgresSaver.setup()` 建 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` / `checkpoint_migrations` |
| 运行期读写 | `role_pa_ai` | `PostgresSaver` + psycopg 连接池（worker 内**进程级单例**，懒建；停机释放） |

- **池化下不能用一次性 `SET search_path`**（新连接会落回 `public`）→ 在连接工厂层固化
  `options="-c search_path=…"`、`autocommit=True`、`row_factory=dict_row`、
  `prepare_threshold=0`（兼容 PgBouncer 的 transaction pooling）。
- **自愈**：连接池探活让坏连接在取出前被重建，避免 PG 重启 / 网络抖动后 HITL resume 永久失败
  （裸连接没有这个能力）。
- **权限自动授权**：`ALTER DEFAULT PRIVILEGES` 让 setup 角色新建的 checkpoint 表**自动**授权给运行期角色,
  于是「建表 / 运行」可以分角色且无需补 GRANT。
- **反序列化安全**：默认 `LANGGRAPH_STRICT_MSGPACK=true`，限制 checkpoint 反序列化类型。

### 4.8 生产化可靠性加固（Redis Streams 是唯一业务通道）

按**队列级标准**加固，默认值即可用：

| 能力 | 关键行为 |
|---|---|
| PEL 回收 | 每轮消费前 `XAUTOCLAIM` 回收「空闲超阈值」的滞留消息；脏数据抛**可精确定位**的解码错误（带 msg_id 与原文） |
| 死信 DLQ | 投递次数超限或消息体非法 → 转 `dlq:{流}`；**先写死信再 ack**（写失败则不 ack，绝不漏消息） |
| 毒消息 vs 慢任务 | 线程锁仍在 → **跳过回收**（保护正常的慢任务）；锁已释放却仍未 ack，才是真毒消息 |
| 流保留 | evt：`MAXLEN 2000` + TTL 7 天（每次发布刷新）；job / result：`MAXLEN 10000` |
| 幂等三件套 | `done:{thread_id}` 终态标记（**仅 published 写入**）+ 线程锁 + 图自身 checkpoint |
| 可观测 | `worker:heartbeat:{consumer}`（TTL 30s，键过期即「消费停滞」信号）+ 每 20 轮打印待处理读数 |

> **ack 位置决定可靠性**：generate / purge 在 `finally` ack；approval 是「先放锁再 ack」。
> 而 `busy` 时 ack 与否是**刻意**区分的：**新投递**的重复消息直接 ack 丢弃；
> **PEL 回收**的消息抢不到锁时**不 ack**（原持有者可能只是慢，留给下轮回收）—— 写反会静默丢任务。
> 参数调优、故障演练清单（kill -9 / 重复投递 / 脏消息 / Redis 重启 / 内存吃紧）、DLQ 处置 SOP
> 属于操作手册范畴。

---

## 五、backend 逻辑

### 5.1 定位

业务 API 网关层（FastAPI + async SQLAlchemy + PostgreSQL + Redis），**不含任何 AI 逻辑**：
不调 LLM、不跑 LangGraph、不写 `schema_pa_ai` 业务表（只读 `product_contents` / `evaluation_logs`）。

- 唯一身份：**frontend 只与 backend 交互**；backend 与 ai-engine 只经 Redis Streams 通信。
- 数据库侧只以 `role_pa_backend` 连库；`delete_audits` 连 UPDATE / DELETE 权限都被 REVOKE —— **审计只能追加**。
- 商品状态机与任务状态**只有 backend 能写**：ai-engine 发终态结果，本模块的消费器负责推进。

### 5.2 生成闭环：顺序即正确性

```text
POST /products/{id}/generate
  → 守卫（商品非删除/归档态 且 无进行中任务，防重复触发）
  → 建 generation_jobs(running) + products.status='generating' + 绑定 active_thread_id
  → 入队瞬间固化 rules 快照（含 effective_at / expires_at 时效过滤）
  → commit 之后才 XADD job:generate（投递失败 → 回滚状态并报 503，绝不卡在 generating）
  → ai-engine 图执行 → result:workflow → WorkflowResultConsumer：
       published      → products.published       + jobs.succeeded
       awaiting_human → products.waiting_approval + jobs.waiting_input
                        + hitl_approvals(pending, content_snapshot) + notification_outbox
       rejected/failed→ products.draft           + jobs.failed（带回失败原因）
POST /approvals/{id}/approve|reject
  → CAS（UPDATE … WHERE status='pending'）→ commit → XADD job:approval
  → 漏投兜底：ApprovalRedriveWatchdog（Redis SETNX 节流）+ 手动补投端点
```

约束：**投递必须在 commit 之后**（否则消费端可能读不到数据）；
**CAS 必须带 `status='pending'`**（否则并发双批准）；**补投必须能识别「其实不需要投」**——
对已推进的商品再 resume 是**静默 no-op**（实测：不报错、不重跑、不重复落库、不翻转决策），
所以接口必须回 `outcome=not_needed` 而不是假装成功。

### 5.3 审批域的三条语义

| 能力 | 契约 | 为什么 |
|---|---|---|
| 查历史 | 只有**显式** `?status=all` 才不过滤；缺省仍等价 `pending` | 复盘页曾只传 `product_id`，被缺省 `pending` 过滤 → **已定案的驳回单永远查不到**。让「全部」只能显式表达，消除「不传 = 全部」的歧义 |
| 补投 | 返回 `{needed, outcome, reason}`，`outcome ∈ enqueued / not_needed / throttled / enqueue_failed` | 判据与看门狗一致：只有商品仍停在 `waiting_approval` 才真投递；节流键 1 小时，**Redis 不可用时放行**（宁可真投） |
| 放行留痕 | 内容快照里有 `violations` 时 `feedback` **必填**（否则 422），成功则与定案**同事务**写 `approval_overrides` | 合规命中转人工后，一点「批准」就能上架且不留痕 —— 事后无法回答「谁在知情下放行了哪条命中点」。人工仍可放行（业务决策），但必须留理由 |

配套两张**只增不改**的审计表：`approval_redrive_audits`（补投次数与最近结果）、
`approval_overrides`（放行人 / 命中点快照 / 理由），都在列表与详情里回显。

### 5.4 SSE：把「过程」变成一等公民

| 环节 | 设计 |
|---|---|
| 鉴权 | `stream-ticket`：用 Bearer access 换**短时票据**（默认 120s，绑定商品与 org）—— EventSource 不能带自定义头，这是唯一的正解；同时兼容 Bearer，便于直接排查 |
| 越权 | 票据 + 商品归属双重校验；越权与不存在**一律 404** |
| 状态门 | 仅 `generating` / `waiting_approval` 才回放与尾随；其余状态只发一条 `ready` 控制帧并关流（避免前端把已结束任务当「生成中」空转） |
| 回放 | 支持 `Last-Event-ID` 续传（`XRange` 用**独占下界** → 不重发） |
| 尾随 | 每 ~0.25s 增量读取；**同步 Redis 调用丢线程池**（否则阻塞事件循环） |
| 关流 | 终态关流：`done` / `rejected` / `failed` / `hitl.waiting`；空闲关流用**注释帧** `: stream-idle-close`（前端带 `Last-Event-ID` 自动续连） |

### 5.5 三个常驻任务（跟着应用生命周期启动；测试环境不启动）

| 任务 | 作用 | 失败时 |
|---|---|---|
| `WorkflowResultConsumer` | 消费 `result:workflow` 推进状态机 | 单轮异常退避重试，**不退出进程** |
| `OutboxDeliverer` | 投递审批通知（outbox → 钉钉 / 控制台） | 指数退避，达上限转 `dlq`；`last_error` **只在未送达期间存在**（送达即清除），「曾失败过」看 `retry_count` —— 否则界面会把已自愈的抖动说成投递失败 |
| `ApprovalRedriveWatchdog` | 补投「已定案但 AI 层未收到」的 resume | 同上，且带 Redis 节流防重复投递 |

终态消费器的 5 条幂等 / 边界加固：
**重复投递按「已终态跳过」**（`duplicate` / `busy` 是正常返回，不当错误上报）、**CAS 更新**、
**`content_snapshot` 仅转人工时携带**、**失败原因写回任务行**（界面直接显示
`input_compliance_blocked(商品标题): 命中违禁词「最便宜」`）、**通知走 outbox 而非同步发送**。

### 5.6 合规词库与运维面

- 词库 / 规则 CRUD 之外，额外给了**快照**与**预览**两个端点：前者返回「下一次生成会下发的规则」，
  后者用当前快照跑命中预览（位置 + 分数 + 是否阻断），与入队口径一致 —— 专治「配了却没拦」。
- **全局配置的写只给 admin**：`compliance_*` 不带 `org_id`，改一个词会影响所有组织。
- **运维面默认只读**：心跳（含 stalled 判定）/ 流长度与消费组 PEL / DLQ 概况 / 卡住任务列表；
  **唯一的变更面是「终止卡死任务」**，必须填 `reason` 并写 `job_abort_audits` 审计，DLQ 本身**刻意不提供一键重投**。
- 合规命中的三种处置态度：输入侧闸门命中 → 任务 failed + 商品回 `draft`；正文命中 → 扣分 + 重写 + 转人工
  （人工可放行但必须留理由）；提示级命中 → 参与扣分并喂给 LLM 复核。

---

## 六、frontend 逻辑

### 6.1 技术栈与红线

```text
技术栈 : React 19 + TypeScript + Vite 8 + Ant Design 6 + Zustand + React Query v5 + React Router v7
红线   : 只访问 /api/v1（dev 由 vite 代理 → :8000；生产由 nginx 同源反代）；绝不直连 ai-engine / PG
路由   : /login · / (商品) · /products/:id (详情：打字机 + 轨迹 + 驳回复盘)
         · /approvals (+ /approvals/:id?ticket= 深链) · /compliance (词库/规则/预览/快照)
         · /ops (心跳/PEL/DLQ，只读) · /members
```

### 6.2 三端共享的契约核心层

桌面端与两个移动端的 `api / services / store / types` 抽成 `@pa/core`（源目录仍是 `frontend/src/*`），
**页面壳各写各的**：契约变更只改一处，三端 UI 仍可各自最优。

代价是一条纪律：**改共享层必须同时跑三端的用例**（各端有独立脚本，同一套「以文件级 ✓ 判定 +
自带超时」的收尾口径）。

### 6.3 三处 SSE 语义

前端注意事项：

1. `hitl.waiting` 在本项目是**终态**（服务端随即关流）→ 前端必须**停重连**并显示「已转人工审批」；
2. `ready` 是 backend 的**控制帧**（无进行中任务）→ 显示提示并停止，不空转重连；
3. 注释帧（`: stream-idle-close` / `: stream-error`）**不触发 `onmessage`** → 必须文本层区分
   「空闲关流（续连）」与「读流异常（限量重试）」。

三端共用同一份 `streamLabels.ts` 渲染事件，由用例用**真实事件载荷**锁定，并含一条负向断言：
渲染结果**不得出现** `stop_reason` / `turns` / `tool_calls` 等内部字段。

---

## 七、mobile 逻辑

### 7.1 为什么是 H5 + 原生两套，而不是只做一套

| 形态 | 定位 | 取舍 |
|---|---|---|
| H5（React + antd-mobile） | 分享链接即开、免安装；以审批等轻量操作为主 | 复用桌面端契约核心层，页面壳另写；与桌面端同源后端 |
| 原生 App（React Native + Expo） | iOS + Android 一套代码；需要原生能力与更稳的现场 | 6 屏与 H5 **逐条对齐**；UI 为**自研薄 UI（零 UI 依赖）** |

### 7.2 共享层抽出的「平台端口」

三端（Web / H5 / RN）需求相同、实现不同的 6 个点抽成 `platform.ts`：
**接口基址 / 会话回跳 / 过期事件 / 定时器 / JWT 解码 / SSE 与二进制传输**。

正因为抽掉了这 6 点，`http.ts`（单飞续签 + 401 重放）与 `sse.ts`（`hitl.waiting` 终态 /
`ready` 不重连 / 注释帧三态）才能**三端共用一份实现** —— 否则每个端都会长出一份迟早语义漂移的拷贝。

### 7.3 移动端的实现口径

- 布局用**表格替代**（窄屏下的信息密度取舍），关键字段用两列键值卡；
- 图片查看交给原生 / 浏览器能力，不引 UI 库；
- 真机联调**不需要 CORS**：手机与开发机同网段时访问 `http://<本机局域网IP>:5174`，
  与桌面端一样走 vite 的 `/api` 代理（这也是「不直连后端」的额外收益）；
- 原生端如实标注**验证边界**：依赖版本锁定（Expo SDK / RN 版本总表）与「哪些交互只在真机验过、
  哪些只在模拟器验过」都逐条标注，而不是笼统说「已支持双端」。

### 7.4 三端一致性靠什么保证

1. 都只走 `/api/v1`，共用同一份 TypeScript 类型与 API 客户端；
2. 事件渲染共用 `streamLabels.ts`（见第六章）；
3. 三端各有独立用例集，**改共享层三端都要跑**；
4. 页面与交互**逐条对齐**（H5 与 RN 的屏数、深链、状态处理一一对应），差异只允许出现在「平台端口」那 6 点里。

---

## 八、部署逻辑

### 8.1 分层：本地只在宿主机放数据库

| 层 | 内容 | 理由 |
|---|---|---|
| 宿主机 | PostgreSQL 17 | 数据落在本地磁盘：便于 GUI 连接、备份与破坏性重置 |
| 容器（开发） | Redis / etcd / MinIO / Milvus | 依赖重、版本敏感、需要「一键销毁重建」；Milvus 依赖前两者 healthy |
| 宿主进程（开发） | backend / ai-engine / 前端与移动端 dev server | **热更新**与断点调试的体验，容器做不到；就绪探测探的是**真实依赖**（如后端 `/readyz` 要 PG+Redis 都通、ai-engine 要出现心跳键），不看进程存活 |
| 容器（生产） | nginx(edge) · frontend · mobile-h5 · portfolio · backend-api · ai-engine · redis ·（`rag` profile：etcd / minio / milvus / milvus-init） | 与开发同构，减少「本地能跑线上不行」 |

Milvus 全家桶走独立 profile：不启用 RAG 时不必为它付 2C2G 的内存 —— 这是「单机也能部署」的关键取舍。

### 8.2 生产拓扑与路由分流

- **nginx 双层**：外层 `edge` 终结 TLS，按路径分流 —— `/` 桌面端 · `/h5` 移动端 · `/api` backend ·
  静态宣传页独立；**SSE 路径单独关闭缓冲**（否则打字机会退化成一口气全吐）。
- 前端与宣传页构建产物是纯静态，运行期不需要 Node。
- **只有一个对外出口 `/api/v1`**：跨域与鉴权都在同源内完成，前端不需要 CORS 配置。

### 8.3 镜像流水线：CI 构建、服务器只 pull

```text
GitHub Actions
  ├─ 矩阵构建 5 个镜像：backend / ai-engine / frontend / mobile-h5 / portfolio
  ├─ 额外把第三方基础镜像（nginx / redis / postgres 等）转推到同一仓库
  └─ 裸 docker push 到阿里云 ACR
        ├─ CI 用**公网**域名推送（runner 在海外）
        └─ ECS 同地域用 VPC 域名拉取（免公网流量费）
ECS：docker compose pull && up -d → 跑验收脚本
```

注意事项：

1. **不能用 buildx 的 `push: true`**：它产出 OCI 索引 / 证明清单（含 `platform=unknown/unknown`），
   ACR 个人版直接拒绝（`unknown manifest`）→ 改为「本地构建 + 裸 `docker push`」。
2. **基础镜像也必须转推**：ECS 连不上 Docker Hub，**即使宿主已有同名缓存**，`compose pull` 仍会失败。

### 8.4 CI / CD 与验收

| 工作流 | 触发 | 内容 |
|---|---|---|
| `ci.yml` | PR / push | 4 组 job：backend 测试 · ai-engine 测试 · 三前端矩阵（类型检查 0 错误 + 用例 + 生产构建）· **环境变量契约自检** |
| `deploy.yml` | 主分支 / 手动 | 构建推 ACR → SSH 到 ECS → `compose pull` + `up -d` → 跑验收脚本 |

**环境变量契约自检**：`infra/.env.template` 是唯一键清单 ——
缺键 = ERROR、未登记键 / 弱口令 = WARN、生产模式（`--prod`）连弱口令也阻断。
它把「上线才发现少个环境变量」提前到了 PR 阶段，代价只是维护一份模板。

### 8.5 Redis 生产环境依赖「队列」而非「缓存」

| 项 | 设置 | 原因 |
|---|---|---|
| `maxmemory-policy` | `noeviction` | 队列里的消息**不能被随机淘汰** |
| 持久化 | AOF everysec | 任务与事件的可靠性下限 |
| 危险命令 | 禁用 `FLUSHALL` / `CONFIG` 等 | 防误操作 |
| 网络 | **不暴露宿主端口** | 只允许容器网络内访问 |
| 容量 | `MAXLEN` 裁剪 + TTL | 流不会无限增长 |

---

## 九、db 逻辑

### 9.1 双 schema：AI 与业务在**库内**物理隔离

| schema | 归属 | 表 |
|---|---|---|
| `schema_pa_backend` | backend | organizations · sys_users · products · hitl_approvals · compliance_words · compliance_rules · generation_jobs · notification_outbox · delete_audits |
| `schema_pa_ai` | ai-engine | product_contents · evaluation_logs · checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations |

### 9.2 四角色最小权限

| 角色 | 权限 | 用途 |
|---|---|---|
| `role_pa_admin` | 全部（DDL） | 初始化 / 迁移 / 引导超管 |
| `role_pa_backend` | 业务 schema DML | backend 运行期 |
| `role_pa_ai` | AI schema DML + 业务表**只读** | ai-engine 运行期（写业务表会被 PostgreSQL 拒绝，有红线负向断言用例） |
| `role_pa_ai_setup` | 建 checkpoint 表 | 仅建表，运行期不用 |

### 9.3 迁移、种子与向量集合

`database/sql/` 按序幂等：`0001_schema.sql`（角色 + schema + 全量 DDL）·
`0002_roles_grants.sql`（最小权限矩阵落地）· `0003_seed.sql`（合规词库 demo 种子）·
`0004_delete_audit.sql`（彻底删除审计）· 后续增量（如 `0006_approval_audits.sql`、`0007_superuser.sql`）。

- **纪律**：`database/` 目录只放初始化 SQL、迁移脚本与集合初始化脚本，**绝不放业务代码**。
- Milvus 集合初始化也在 `database/milvus/`（`pa_listing_vec`：商品文案语义向量库，供 RAG Few-Shot 召回）。
- 审计类表统一「**只增不改**」：授权只给 SELECT / INSERT，历史无法被改写。

### 9.4 为什么敢让 AI 层连同一个库

因为靠的是**权限**而不是约定：AI 层对业务表只有 SELECT，节点与模型层都不碰业务写路径；
`delete_audits` 连 UPDATE / DELETE 都被 REVOKE。测试用**一次性容器**执行（临时 PG + 临时 Redis，
真造业务数据），跑完 `down -v` 整体销毁容器，**宿主机数据库全程不被触碰** —— 这也是「单人项目也敢跑破坏性测试」的前提。

### 9.5 测试规模

| 套件 | 例数 | 内容 |
|---|---|---|
| 数据库 | 102 | 权限矩阵 88 + 红线 6 + 迁移/约束 8（权限逐条实测，不看 DDL 下结论） |
| backend | 194 | 认证 / RBAC / CAS / SSE / 两侧一致性契约（容器内跑临时 PG + Redis） |
| ai-engine | 239 | 图 / Agent / 工具 / 可靠性；大部分纯内存可跑，少量需容器化 PG + Redis（未配置时自动 skip） |
| 桌面端 | 92 | 页面语义护栏 / SSE 控制帧 / 审批留痕 |
| 移动 H5 | 24 | 与桌面端同口径的关键交互 |
| 原生 App | 66 | 屏幕交互与共享契约层 |
| 作品集 | 55 | 「声明即校验」等静态页门禁 |
