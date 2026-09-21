"""ListingWorker：

    消费 Redis Streams 任务，以 thread_id 驱动/恢复 ListingWorkflow。

职责边界：
  · 消费 job:generate → 读 schema_pa_backend.products 素材 → 运行图；
  · 消费 job:approval → Command(resume=...) 恢复（CAS 幂等在 backend-api 侧完成）；
  · 事件经 evt:{thread_id} 发布（SSE 回放源）；
  · 可选增强：注入 agent_runtime 后，图内在 RAG 召回与生成之间插入一次「只读 Agent 研究」
    （function calling 取真实素材/历史评估/审批意见 + 中间件预算熔断），
    结论写入 ListingState.agent_context；**未注入时该节点 no-op**，链路行为不变。
  · 图终态把结果发布到 result:workflow（published/awaiting_human/rejected）

    由 backend 的 WorkflowResultConsumer 推进 products.status（ai-engine 不写 schema_pa_backend）；
  · awaiting_human 结果额外携带 content_snapshot（完整文案+评估），backend 建档 hitl_approvals 时
    写入新列，供审批中心“AI 生成详情”展示（批准前文案不落 product_contents）。
    不包含任何 HTTP 路由与鉴权。

checkpoint 生命周期：
  · 进程内单例（new_pg_checkpointer：PostgresSaver + psycopg 连接池，落 schema_pa_ai），
    由 _get_checkpointer() 懒建、各任务复用；进程退出由 close() 释放连接池。

可靠性（队列级；验收脚本 infra/scripts/redis-verify.sh）：
  · PEL 回收：每轮消费前 claim_stale()（XAUTOCLAIM）接管「空闲超时未 ack」的消息；
    投递次数达上限或消息体非法 → move_to_dlq() 转 pa:{env}:dlq:{流}（先写死信再 ack）；
  · 毒消息与慢任务区分：线程锁仍在 → 跳过回收（保护正常慢任务），锁已释放却仍未 ack 才是毒消息；
  · 幂等：done:{thread_id} 终态标记（**仅 published 写入**）+ 线程锁（job_lock_ttl_seconds）；
  · 保留：evt 流 MAXLEN + TTL（每次发布刷新）、job/result 流 MAXLEN；心跳键供「消费停滞」告警。
"""
from __future__ import annotations

import logging
import os
import socket

import redis
from pydantic import BaseModel

from ..adapters.pg_store import (
    CHECKPOINT_SCHEMA,
    PgBusinessReader,
    PgContentStore,
    PgEvalLogStore,
    PgProductsReader,
    ensure_checkpoint_schema,
    list_pa_thread_ids,
)

from ..adapters.redis_eventbus import MessageDecodeError, RedisEventBus, RedisKeys, RedisStreams
from ..adapters.rules import compile_rules_snapshot
from ..workflowcore.agent.middleware import DEFAULT_MAX_TOOL_CALLS, DEFAULT_MAX_TURNS
from ..workflowcore.graph import build_workflow, close_pg_checkpointer, new_pg_checkpointer
from ..workflowcore.node.node_conditions import is_high_value_product
from ..ports import BLOCKING_SEVERITIES, ObjectStorageServer
from .logging_setup import log


logger = logging.getLogger(__name__)


def _default_consumer_name() -> str:
    """生成默认消费者名（全局唯一）。

    为什么必须唯一：PEL（待处理列表）里的消息按「消费者名」归属 —— 多个实例共用同一个名字时，
    「谁持有哪条未 ack 消息」会互相覆盖，崩溃后无法定向回收（XAUTOCLAIM 也无法区分归属）。
    默认用 `worker-{hostname}-{pid}`：单机多进程与多机部署都天然唯一，无需额外配置。

    返回:
        形如 `worker-a1b2c3-12345` 的消费者名。
    """
    try:
        host = socket.gethostname() or "unknown"
    except OSError:  # 极少数受限容器/沙箱里 gethostname 会失败，降级为 unknown 而不是让 worker 起不来
        host = "unknown"
    return f"worker-{host}-{os.getpid()}"


def _jsonify(value):
    """把 channel 值收敛为 JSON 安全的纯 dict。

    参数:
        value: 任意 channel 值；pydantic BaseModel 实例会被展平。
    返回:
        BaseModel 实例 → model_dump(mode="json")；其余类型原样返回。
    注意:
        用 mode="json" 保证 UUID/datetime 等可被 JSON 序列化（写 Redis Streams 的前提）。
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def image_outcome_line(thread_id: str, out: dict) -> str:
    """把配图结果/降级原因渲染成一行进程日志（可观测性）。

    为什么需要（2026-09 实测）:
        `node_image` 失败只把原因写进 State 的 `image_error`（随后只落 checkpoint）；
        `OSS_BUCKET` 指向不存在的桶时，每次生成都「生图成功、上传失败、静默降级纯文本」，
        而日志里只有「开始生成 / 待处理读数」—— 前端表现为「永远没有配图」，后端表现为
        「任务成功」，排障时完全无迹可循。这行日志就是那次的教训：**降级必须留痕**。

    参数:
        thread_id: 线程 ID（日志只取前 8 位，保持与其他行同口径）。
        out: 图终态快照（invoke/resume 的返回；含 image_attached / image_error）。
    返回:
        单行日志文本：配图成功写 `image_attached=True`；降级写 `image_attached=False reason=…`。
    """
    short = str(thread_id or "")[:8]
    error = out.get("image_error")
    if error:
        return f"[worker] 配图降级为纯文本 thread_id={short} image_attached=False reason={error}"
    return f"[worker] 配图结果 thread_id={short} image_attached={out.get('image_attached')}"


class ListingWorker:
    """一次取一条消息处理（消费组 worker 语义）。
    AI Engine 侧的 Redis Streams 消费组 worker，按“取一条消息 → 处理 → ack”的语义运行"""

    def __init__(
        self,
        *,
        redis_client: redis.Redis,
        keys: RedisKeys,
        runtime_pg_dsn: str,
        setup_pg_dsn: str | None = None,
        llm_gateway=None,
        rag_store=None,
        image_gateway=None,
        object_storage: ObjectStorageServer | None = None,
        image_normalizer=None,
        group: str = "ai-engine",
        rule_precheck: bool = True,
        approval_lock_ttl_seconds: int = 300,
        agent_runtime=None,
        agent_middlewares=None,
        agent_max_turns: int = DEFAULT_MAX_TURNS,
        agent_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        consumer: str | None = None,
        pel_min_idle_ms: int = 60000,
        pel_claim_batch: int = 10,
        max_deliveries: int = 5,
        job_lock_ttl_seconds: int = 300,
        stream_maxlen_job: int = 10000,
        stream_maxlen_evt: int = 2000,
        evt_ttl_seconds: int = 604800,
        done_ttl_seconds: int = 604800,
        heartbeat_ttl_seconds: int = 30,
    ):
        """初始化。

        参数:
            redis_client: Redis 客户端（decode_responses=True）；本类不管理其生命周期。
            keys: Redis 键空间构造器。env 必须与 backend 投递端一致，
                否则任务会落在不同键上而静默不消费。
            runtime_pg_dsn: 运行期 DSN，应为 role_pa_ai（业务表 DML + checkpoint 读写）。
            setup_pg_dsn: 建表 DSN，应为 role_pa_ai_setup（仅首次建 checkpoint 系列表）；
                None 表示跳过建表（假定表已存在）。
            llm_gateway: 文本生成/评估网关；None 时节点回退确定性 mock 文案。
            rag_store: RAG 向量库；None 表示关闭召回（等价空 Few-Shot 上下文）。
            image_gateway: AI 生图网关；None 时 node_image 降级纯文本。
            object_storage: 对象存储（OSS）；None 时生图产物不落自有 bucket，同样降级。
            image_normalizer: 图片规格化端口（统一 1:1/格式/背景）；None 时不做重编码，
                仅按字节探测实测尺寸（行为与接入前一致）。
            group: Redis Streams 消费组名（默认 ai-engine）。
            rule_precheck: 是否对商品标题做阻断级违禁词预检（默认 True，命中直接失败回 draft）。
            approval_lock_ttl_seconds: 审批恢复分布式锁 TTL（秒）；持锁进程崩溃后的自动解锁上限。
            agent_runtime: Agent 研究运行时（含 tools 的模型）；None 时 agent_research 节点 no-op
                （默认关闭：Agent 属可选增强，需显式注入才产生额外 LLM/工具开销）。
            agent_middlewares: Agent 中间件链（预算熔断 / 工具上限 / 重试兜底）；None 用默认链。
            agent_max_turns: Agent 模型调用上限（预算熔断阈值）。
            agent_max_tool_calls: Agent 工具调用上限。
            consumer: 消费者名（PEL 归属标识）；None 时用 `worker-{hostname}-{pid}` 自动生成唯一名。
                多实例部署必须唯一，否则未 ack 消息归属混乱、崩溃后无法定向回收。
            pel_min_idle_ms: PEL 滞留判定阈值（毫秒）：空闲超过该时长的未 ack 消息会被回收重试。
                ⚠️ 应大于单次任务最长耗时，否则慢任务会被重复接管（浪费 LLM 调用）。
            pel_claim_batch: 每轮消费前单次回收条数上限（0 = 关闭回收）。
            max_deliveries: 毒消息上限：投递次数达到该值仍失败的消息转死信流（DLQ）并从原流 ack。
            job_lock_ttl_seconds: 生成任务线程锁 TTL（秒）；兼作回收时「是否仍在处理中」的判据。
            stream_maxlen_job: job / result 流的保留上限（近似裁剪），None/0 = 不裁剪。
            stream_maxlen_evt: evt 事件流保留上限（只影响断线回放深度）。
            evt_ttl_seconds: evt 流 TTL（秒，每次发布刷新）；线程结束后到期自动回收整条流。
            done_ttl_seconds: 终态幂等标记 `done:{thread_id}` 的存活期（仅 published 写入）。
            heartbeat_ttl_seconds: 心跳键 TTL（秒）；键过期即代表消费停滞（告警信号）。
        异常:
            psycopg.Error: setup_pg_dsn 非空时会在构造期调 ensure_checkpoint_schema()（fail-fast），
                DSN 不可达或权限不足将直接抛出，而不是等到第一条任务才暴露。
        注意:
            checkpointer 不在构造期创建：首次 _get_checkpointer() 时才建池（懒加载单例），
            以便未配 DSN 的纯单测场景也能构造本类。
        """
        self.r = redis_client
        self.keys = keys
        self.group = group
        # 消费者名：PEL 归属的唯一标识（多实例必须唯一，见 _default_consumer_name）
        self.consumer = consumer or _default_consumer_name()
        self.streams = RedisStreams(redis_client, keys, consumer=self.consumer)
        # 审批恢复分布式锁 TTL（秒）：持锁进程崩溃后的自动解锁上限
        self.approval_lock_ttl_seconds = approval_lock_ttl_seconds
        # 生成任务线程锁 TTL（秒）：防同一线程被并发处理，兼作回收时的「仍在处理中」判据
        self.job_lock_ttl_seconds = int(job_lock_ttl_seconds)
        # PEL 回收与死信：空闲阈值 / 单批条数 / 毒消息投递上限
        self.pel_min_idle_ms = int(pel_min_idle_ms)
        self.pel_claim_batch = int(pel_claim_batch)
        self.max_deliveries = int(max_deliveries)
        # 流保留：job/result 按条数裁剪；evt 额外设 TTL（到期整条流回收）
        self.stream_maxlen_job = int(stream_maxlen_job)
        self.stream_maxlen_evt = int(stream_maxlen_evt)
        self.evt_ttl_seconds = int(evt_ttl_seconds)
        # 终态幂等标记存活期（仅 published 写入，防重复投递重复生成）
        self.done_ttl_seconds = int(done_ttl_seconds)
        # 心跳 TTL（消费停滞探测）
        self.heartbeat_ttl_seconds = int(heartbeat_ttl_seconds)
        self.runtime_dsn = runtime_pg_dsn
        self.llm_gateway = llm_gateway
        self.rag_store = rag_store
        # 图文配图能力：未配置时 node_image 节点自动降级纯文本
        self.image_gateway = image_gateway
        self.object_storage = object_storage
        self.image_normalizer = image_normalizer
        # 输入素材预检：命中阻断级违禁词不进图，源头拦截省一轮生成
        self.rule_precheck = rule_precheck
        # Agent 研究（增强功能）：runtime=None 时节点 no-op，不产生额外开销
        self.agent_runtime = agent_runtime
        self.agent_middlewares = agent_middlewares
        self.agent_max_turns = int(agent_max_turns)
        self.agent_max_tool_calls = int(agent_max_tool_calls)
        self.product_reader = PgProductsReader(runtime_pg_dsn)
        # Agent 取数端口（只读）：与 product_reader 同 DSN，复用同一角色权限
        self.business_reader = PgBusinessReader(runtime_pg_dsn)
        self._setup_done = False
        if setup_pg_dsn:
            ensure_checkpoint_schema(setup_pg_dsn)
            self._setup_done = True
        # 进程级单例：池化 PostgresSaver 只建一次（懒加载），避免每条消息新建连接
        self._checkpointer = None

    def _get_checkpointer(self):
        """懒建并复用进程级 checkpointer（PostgresSaver + 连接池，落 schema_pa_ai）。

        返回:
            PostgresSaver：池化 saver（search_path 已固化，坏连接自动重建）。
        异常:
            psycopg_pool.PoolTimeout: DSN 不可达 / 权限不足（由调用方终态化 failed）。
        注意:
            必须在 ensure_checkpoint_schema() 之后首次调用：建表用 role_pa_ai_setup，
            运行期读写用 role_pa_ai，两个角色不可混用（见 database/sql/0002_roles_grants.sql）。
        """
        if self._checkpointer is None:
            self._checkpointer = new_pg_checkpointer(self.runtime_dsn, schema=CHECKPOINT_SCHEMA)
        return self._checkpointer

    def close(self) -> None:
        """优雅停机：释放 checkpointer 连接池（幂等，可重复调用）。"""
        close_pg_checkpointer(self._checkpointer)
        self._checkpointer = None


    def heartbeat(self) -> None:
        """刷新本 worker 的心跳键（由消费循环每轮调用；键过期即代表消费停滞）。"""
        self.streams.heartbeat(ttl_seconds=self.heartbeat_ttl_seconds)

    def pending_stats(self) -> dict[str, int]:
        """三条任务流的待处理（已投递未 ack）条数；查询失败为 -1（可观测用，不抛异常）。"""
        return {
            "job:generate": self.streams.pending_count(self.keys.job_generate(), self.group),
            "job:approval": self.streams.pending_count(self.keys.job_approval(), self.group),
            "job:product_purge": self.streams.pending_count(self.keys.job_product_purge(), self.group),
        }

    def _workflow(self, *, rule_engine=None):
        """组装 workflow（每条任务新建图实例，但 checkpointer 复用进程级单例）。

        参数:
            rule_engine: 由本线程任务消息里的规则快照编译而来（入队瞬间固化版本）；
                None 表示该任务不带规则快照，评估仅走 LLM/骨架路径。
        返回:
            _WorkflowWrapper（invoke / resume / thread_config）。
        注意:
            各端口实现每任务新建（廉价：只持 DSN/客户端引用）；
            唯一的重资源 checkpointer 由 _get_checkpointer() 复用，不在此新建。
        """
        return build_workflow(
            llm_gateway=self.llm_gateway,
            rag_store=self.rag_store,
            content_store=PgContentStore(self.runtime_dsn),
            eval_log_store=PgEvalLogStore(self.runtime_dsn),
            event_bus=RedisEventBus(
                self.r,
                self.keys,
                maxlen=self.stream_maxlen_evt,
                ttl_seconds=self.evt_ttl_seconds,
            ),
            checkpointer=self._get_checkpointer(),
            rule_engine=rule_engine,
            image_gateway=self.image_gateway,
            object_storage=self.object_storage,
            image_normalizer=self.image_normalizer,
            # 图片统一收拢在 bucket 的 img/pa 目录（后缀随实际编码格式，jpeg→jpg）：
            # AI 图 {asset_key_prefix}/{org_id}/{product_id}/{thread_id}/cover-1.jpg
            # 上传图 {asset_key_prefix}/{org_id}/{product_id}/{thread_id}/upload-{n}.jpg
            asset_key_prefix=f"img/pa/{self.keys.env}",
            # Agent 研究（只读取数）：runtime=None 时节点 no-op；
            # 工具取数走 PgBusinessReader（role_pa_ai 只读权限），本任务规则快照复用同一 engine
            agent_runtime=self.agent_runtime,
            agent_reader=self.business_reader,
            agent_middlewares=self.agent_middlewares,
            agent_max_turns=self.agent_max_turns,
            agent_max_tool_calls=self.agent_max_tool_calls,
        )

    def _rule_engine_from_message(self, data: dict):
        """从 job 消息载荷 rules 快照编译规则引擎（方案 A：backend 入队随带，线程内版本固化）。

        老消息/无 rules 字段 → None（引擎关闭，链路行为与历史版本一致）。

        参数:
            data: job 消息载荷 dict；读可选的 rules 字段（规则快照）。
        返回:
            编译后的 RuleEngine；无 rules 或载荷非 dict 时返回 None。
        """
        rules = data.get("rules") if isinstance(data, dict) else None
        if not rules:
            return None
        return compile_rules_snapshot(rules)



    def _pull_or_reclaim(
        self,
        stream: str,
        *,
        block_ms: int,
        domain: str,
        has_thread_lock: bool,
    ) -> tuple[str | bytes, dict, bool] | None:
        """取一条待处理消息：优先回收本组滞留消息（XAUTOCLAIM），否则读新消息。

        为什么先回收：`read_next` 只读 `>`（本组尚未投递过的消息），worker 崩溃后「已投递未 ack」
        的消息不会重投 —— 不主动回收就会永久卡在 PEL 里没人管。

        参数:
            stream: 完整流键名。
            block_ms: 读新消息时的阻塞等待上限（毫秒）；回收是即时查询、不阻塞。
            domain: 流领域名（拼死信流键，如 `job:generate`）。
            has_thread_lock: 消息是否受线程锁保护（generate/approval=True，purge=False）；
                为 True 时「锁仍存在」的消息会被跳过（视为正在处理的慢任务）。
        返回:
            `(msg_id, payload, reclaimed)`；reclaimed=True 表示来自 PEL 回收；
            无可用消息返回 None（含「脏数据/毒消息已就地转死信」的情况）。
        异常:
            redis.exceptions.RedisError: 仅**读新消息**时原样上抛（由主循环退避重试）；
                回收阶段的 Redis 异常只打印不抛（回收失败不应阻断正常取新消息）。
        注意:
            单批回收后即返回第一条可处理消息，其余留待下一轮：保证回收不挤压新消息的时延。
        """
        # ① 回收滞留消息（本进程或其它实例崩溃后遗留）
        if self.pel_claim_batch > 0:
            try:
                claimed = self.streams.claim_stale(
                    stream,
                    self.group,
                    min_idle_ms=self.pel_min_idle_ms,
                    count=self.pel_claim_batch,
                )
            except redis.exceptions.RedisError as exc:
                log(f"[worker] PEL 回收失败（本轮跳过，不影响取新消息）: {exc!r}")
                claimed = []
            for msg_id, payload, raw in claimed:
                pulled = self._handle_claimed(
                    stream, msg_id, payload, raw, domain=domain, has_thread_lock=has_thread_lock
                )
                if pulled is not None:
                    return pulled
        # ② 读新消息（`>` 语义：只看本组尚未投递过的）
        try:
            msg = self.streams.read_next(stream, group=self.group, block_ms=block_ms)
        except MessageDecodeError as exc:
            # 脏数据：连 thread_id 都读不出来，重试没有意义 → 留证后丢弃（否则每轮都抛、永远不清）
            self.streams.move_to_dlq(
                exc.stream,
                exc.msg_id,
                dlq_stream=self.keys.dlq(domain),
                group=self.group,
                reason="json_decode_error",
                raw=exc.raw,
                maxlen=self.stream_maxlen_job,
            )
            log(f"[worker] 收到无法解析的消息，已转死信 {stream} {exc.msg_id}")
            return None
        if msg is None:
            return None  # 空轮询（XREADGROUP 超时）：正常，非错误
        msg_id, payload = msg
        return msg_id, payload, False

    def _handle_claimed(
        self,
        stream: str,
        msg_id: str | bytes,
        payload: dict | None,
        raw: str | None,
        *,
        domain: str,
        has_thread_lock: bool,
    ) -> tuple[str | bytes, dict, bool] | None:
        """判定一条滞留消息：该接管、该跳过、还是该转死信。

        判定顺序（后两者都返回 None，让调用方看下一条）：
            ① 脏数据 → 转死信（无法重试）；
            ② 有线程锁且锁仍存在 → 跳过（可能只是原持有者的**慢任务**，不是毒消息）；
            ③ 投递次数 ≥ max_deliveries → 转死信（锁已释放却仍未 ack，才是真毒消息）。

        参数:
            stream: 完整流键名。
            msg_id: 消息 ID。
            payload: 解析后的载荷；None 表示消息体不是合法 JSON。
            raw: 原文（payload 为 None 时用于留证）。
            domain: 领域名（拼死信流键）。
            has_thread_lock: 是否受线程锁保护。
        返回:
            `(msg_id, payload, True)` 表示可接管处理；None 表示本条已处理完（跳过/转死信）。
        注意:
            delivery count 由 XAUTOCLAIM 在服务端 +1，因此首次回收时计数 ≥ 2
            （首次投递 + 本次接管）—— max_deliveries=5 意味着最多接管 4 次。
        """
        if payload is None:
            self.streams.move_to_dlq(
                stream,
                msg_id,
                dlq_stream=self.keys.dlq(domain),
                group=self.group,
                reason="json_decode_error",
                raw=raw,
                maxlen=self.stream_maxlen_job,
            )
            log(f"[worker] 滞留消息无法解析，已转死信 {stream} {msg_id}")
            return None
        deliveries = self.streams.deliveries_of(stream, msg_id, group=self.group)
        thread_id = payload.get("thread_id")
        if has_thread_lock and thread_id and self.streams.lock_exists(thread_id):
            log(
                f"[worker] 滞留消息仍在处理中（锁存在），本轮不接管 {stream} {msg_id} "
                f"交付次数={deliveries}"
            )
            return None
        if deliveries >= self.max_deliveries:
            self.streams.move_to_dlq(
                stream,
                msg_id,
                dlq_stream=self.keys.dlq(domain),
                group=self.group,
                deliveries=deliveries,
                reason="max_deliveries",
                payload=payload,
                maxlen=self.stream_maxlen_job,
            )
            log(f"[worker] 交付次数达上限（{deliveries}），已转死信 {stream} {msg_id}")
            return None
        log(f"[worker] 回收滞留消息并重试 {stream} {msg_id} 交付次数={deliveries}")
        return msg_id, payload, True

    def consume_generate_once(self, *, block_ms: int = 1000) -> str | None:
        """处理一条生成任务（消费组语义：取一条 → 跑图 → ack）。

        取消息顺序 =「先回收本组滞留消息，再读新消息」（见 _pull_or_reclaim）：
        worker 崩溃后未 ack 的任务会在这里被重新捞起来跑，而不是永远卡在 PEL 里没人管。

        参数:
            block_ms: XREADGROUP 阻塞等待上限（毫秒）。
        返回:
            done / interrupted / failed / missing_product / blocked_input / busy / duplicate；
            无消息时返回 None。
            · busy      —— 线程已被（本进程或他进程）持锁处理：新投递的重复消息直接 ack，
                           回收到的消息不 ack（可能只是原持有者的慢任务，留给下轮回收）；
            · duplicate —— 该线程已有 published 终态（幂等标记命中），不再重复生成。
        注意:
            走到底的每条消息都会 ack（异常经 _terminalize_failed 终态化，不留在 PEL 反复重投）；
            唯一例外是「回收到的消息 + 抢锁失败」，此时故意不 ack。
        """
        pulled = self._pull_or_reclaim(
            self.keys.job_generate(), block_ms=block_ms, domain="job:generate", has_thread_lock=True
        )
        if pulled is None:
            return None
        msg_id, data, reclaimed = pulled
        return self._handle_generate(msg_id, data, reclaimed=reclaimed)

    def _handle_generate(self, msg_id: str | bytes, data: dict, *, reclaimed: bool) -> str:
        """执行一条已取出的生成任务，返回处理结论。

        参数:
            msg_id: 消息 ID（用于 ack）。
            data: 任务载荷（thread_id / product_id / org_id / rules?）。
            reclaimed: 是否来自 PEL 回收（决定「抢锁失败」时 ack 还是保留）。
        返回:
            done / interrupted / failed / missing_product / blocked_input / busy / duplicate。
        注意:
            幂等三件套：① done 终态标记（短路）② 线程锁（并发互斥）③ 图自身 checkpoint 恢复。
            仅当图跑到 published 终态才写 done 标记 —— awaiting_human（待审批）与 failed（可重试）
            保持「未完成」语义，否则会把「等待审批」「允许重试」误判成已完成而拒绝后续消息。
        """
        thread_id = data.get("thread_id")
        # ① 终态幂等：已 published 的线程直接短路（backend 重复投递 / PEL 回收重投都不再生成）
        if thread_id and self.streams.is_done(thread_id):
            log(f"[worker] 线程已 published，跳过重复生成 thread_id={str(thread_id)[:8]}")
            self.streams.ack(self.keys.job_generate(), msg_id, group=self.group)
            return "duplicate"
        # ② 线程锁：防同一线程被并发处理（多实例部署 + 重复投递双保险）
        token = (
            self.streams.acquire_lock(thread_id, ttl_seconds=self.job_lock_ttl_seconds)
            if thread_id
            else None
        )
        if thread_id and token is None:
            if reclaimed:
                # 回收场景：锁还在 ⇒ 原持有者可能只是慢（还没崩）。
                # 不 ack 也不处理，留给下一轮回收（锁 TTL 过期后即可接管）—— 否则会丢任务。
                log(
                    f"[worker] 回收消息仍在处理中（锁未释放），本轮不接管 thread_id={str(thread_id)[:8]}"
                )
                return "busy"
            # 新投递场景：同一线程已在处理 = backend 重复投递 → ack 丢弃是安全的
            log(f"[worker] 重复投递（线程已在处理），跳过 thread_id={str(thread_id)[:8]}")
            self.streams.ack(self.keys.job_generate(), msg_id, group=self.group)
            return "busy"
        try:
            engine = self._rule_engine_from_message(data)
            product = self.product_reader.read(product_id=data["product_id"], org_id=data["org_id"])
            if product is None:
                return "missing_product"
            # 请求关联 ID（可选）：把这条 worker 日志与 backend 那次 HTTP 触发请求串起来
            request_id = data.get("request_id") or "-"
            log(
                f"[worker] 开始生成 thread_id={str(data['thread_id'])[:8]} "
                f"product={data['product_id']} request_id={request_id}"
            )
            # 输入素材预检：商品标题含阻断级违禁词 → 不浪费一次注定违规的生成
            if engine is not None and self.rule_precheck:
                blocked = [
                    h for h in engine.check(str(product.get("title") or "")) if h.severity in BLOCKING_SEVERITIES
                ]
                if blocked:
                    reasons = [h.reason or h.keyword for h in blocked]
                    log(
                        f"[worker] 输入素材命中违禁词，阻止生成 thread_id={str(data['thread_id'])[:8]}"
                        f"（字段=商品标题）: {reasons}"
                    )
                    self._publish(
                        data["thread_id"],
                        {
                            "type": "failed",
                            "data": {
                                "product_id": data["product_id"],
                                "org_id": data["org_id"],
                                "reason": "input_compliance_blocked",
                                "errors": reasons,
                            },
                        },
                    )
                    # 原因随结果回传 → backend 写 generation_jobs.error → 详情页「最近一次任务失败原因」
                    # 直接显示「标题命中违禁词「最便宜」」。不回传的话，操作者只看到商品莫名回到 draft。
                    self._publish_outcome(
                        data,
                        "failed",
                        error=f"input_compliance_blocked(商品标题): {'；'.join(reasons)}",
                    )
                    return "blocked_input"
            self._publish(
                data["thread_id"],
                {"type": "generate.started", "data": {"product_id": data["product_id"], "org_id": data["org_id"]}},
            )
            input_state = {
                "thread_id": data["thread_id"],
                "product_id": data["product_id"],
                "org_id": data["org_id"],
                "raw_product_info": product,
                # 最近一次驳回意见（可选）：backend 在触发时查 hitl_approvals 下发，
                # 让"按意见重写"成为确定性行为（而非依赖 Agent 主动去查历史审批）
                "reject_guidance": data.get("guidance") or "",
            }
            wf = self._workflow(rule_engine=engine)
            out = wf.invoke(input_state, config=wf.thread_config(data["thread_id"]))
            # 配图结果（含降级原因）落进程日志：只写 State.image_error 的话排障时完全看不见
            log(image_outcome_line(data["thread_id"], out))
            # 终态结果 → result:workflow：backend 据此把商品推为已上架 / 待审批
            interrupted = "__interrupt__" in out
            # 挂起时从图状态快照打包“审批内容快照”（完整文案+评估），随 awaiting_human 结果发送，
            # backend 建档 hitl_approvals(pending) 时落库 → 审批中心可展示 AI 生成详情
            review_snapshot = self._review_snapshot(out) if interrupted else None
            if interrupted:
                # 事件契约：图挂起（高价 >500 / 质量重试耗尽）需人工审批——在 evt 流给 SSE
                # 一个明确的“等待审批”标记，否则详情页流结束后只剩空闲重连，
                # 前端永远等不到任何信号去刷新商品状态，按钮会一直停留在“AI 生成中”。
                self._publish(
                    data["thread_id"],
                    {"type": "hitl.waiting", "data": {"product_id": data["product_id"], "org_id": data["org_id"]}},
                )
            result = "awaiting_human" if interrupted else "published"
            self._publish_outcome(data, result, content_snapshot=review_snapshot)
            # ③ 终态幂等标记：仅 published 写入（awaiting_human 待审批、failed 可重试，都保持「未完成」）
            if not interrupted and thread_id:
                self.streams.mark_done(thread_id, ttl_seconds=self.done_ttl_seconds)
            return "interrupted" if interrupted else "done"
        except Exception as exc:  # noqa: BLE001 LLM/DNS/网络/DB 异常：终态化 failed，绝不静默丢任务
            self._terminalize_failed(data, exc, stage="generate")
            return "failed"
        finally:
            # 先放锁再 ack（与审批链路同一取舍）：ack 若遇 Redis 抖动抛错，锁也不会滞留在该线程上
            if token is not None:
                self.streams.release_lock(thread_id, token)
            self.streams.ack(self.keys.job_generate(), msg_id, group=self.group)


    def consume_product_purge_once(self, *, block_ms: int = 1000) -> str | None:
        """消费 admin 彻底删除任务：物理清理该商品的 pa_ai 数据 + 其 OSS 图片对象（幂等）。

        OSS 清理策略：删 product_contents 行前先收集 content_data 里所有 image url，
        行删完后 best-effort delete（对象删除失败不阻塞本任务 ack，由生命周期规则兜底）。
        覆盖范围 = AI 生图产物（{asset_key_prefix}/.../cover-*.png）与内容内复制的运营上传图 URL。

        参数:
            block_ms: XREADGROUP 阻塞等待上限（毫秒）。
        返回:
            purged / skipped_product_exists；无消息时返回 None。
        注意:
            · 前置守卫：商品行仍存在时判定为孤儿 purge 消息 → 中止并 ack（防误删有效数据）；
            · 顺序约束：必须先 list_image_urls / list_pa_thread_ids 再删行，否则 URL 与线程不可逆丢失；
            · OSS/RAG/checkpoint 清理均为 best-effort，失败只打印不阻断本任务 ack。
        """
        pulled = self._pull_or_reclaim(
            self.keys.job_product_purge(),
            block_ms=block_ms,
            domain="job:product_purge",
            has_thread_lock=False,
        )
        if pulled is None:
            return None
        msg_id, data, _reclaimed = pulled
        return self._handle_purge(msg_id, data)

    def _handle_purge(self, msg_id: str | bytes, data: dict) -> str:
        """执行一条商品清理任务（已从流中取出）：主体逻辑与接入回收前完全一致。

        参数:
            msg_id: 消息 ID（用于 ack）。
            data: 任务载荷（product_id / org_id）。
        返回:
            purged / skipped_product_exists。
        注意:
            清理是幂等的物理删除，因此**不加线程锁**：并发清理同一商品结果一致（无害），
            而「因为抢不到锁而错过清理」的代价更大（脏数据与 OSS 对象残留）。
        """
        try:
            product_id = data["product_id"]
            org_id = data["org_id"]
            # 前置守卫（遗留加固 2026-09）：后端在“商品删除已提交”后才发 purge 消息；
            # 若收到孤儿消息（商品行仍存在 = 删除并未生效，如旧版本同事务先 XADD 后 commit 回滚），
            # 物理清 pa_ai 会造成数据丢失，必须中止并 ack 丢弃。
            if self.product_reader.read(product_id=product_id, org_id=org_id) is not None:
                log(
                    f"[worker] purge 中止：商品仍存在（疑似孤儿 purge 消息）org={org_id} "
                    f"product={product_id}，跳过清理"
                )
                return "skipped_product_exists"
            content_store = PgContentStore(self.runtime_dsn)
            # ① 先收集该商品全部已落库内容的 image URL（行还在，供白名单删除）
            image_urls = content_store.list_image_urls(org_id=org_id, product_id=product_id)
            # ①.b 收集关联线程：checkpoint 系列表按 thread_id 组织，必须在删行前反查
            thread_ids = list_pa_thread_ids(self.runtime_dsn, org_id=org_id, product_id=product_id)
            removed_c = content_store.purge_by_product(org_id=org_id, product_id=product_id)
            removed_e = PgEvalLogStore(self.runtime_dsn).purge_by_product(org_id=org_id, product_id=product_id)
            # ①.c checkpoint 清理（best-effort）：清 checkpoints / checkpoint_blobs /
            # checkpoint_writes。修复历史遗留——此前删除商品无人清理 checkpoint 表，
            # 线程级状态快照会永久残留；失败不阻断 ack（本任务幂等，可重投）。
            removed_ckpt = 0
            if thread_ids:
                try:
                    saver = self._get_checkpointer()
                    for thread_id in thread_ids:
                        saver.delete_thread(thread_id)
                        removed_ckpt += 1
                except Exception as exc:  # noqa: BLE001 清理失败不阻断本任务终态
                    log(f"[worker] checkpoint 清理失败（忽略）: {exc!r}")
            log(
                f"[worker] 已物理清理 pa_ai org={org_id} product={product_id} "
                f"(contents={removed_c}, logs={removed_e}, threads={removed_ckpt})"
            )
            # ② 图片对象 best-effort 清理：只删本 bucket + img/pa/{env}/ 白名单前缀（幂等）。
            #    该前缀必须与 adapters/oss.py 的受管前缀（img/pa/）同源，否则 URL 会被
            #    白名单校验拦下而静默不删（对象残留）。
            if self.object_storage is not None and image_urls:
                deleted = self.object_storage.delete_urls(image_urls)
                log(f"[worker] OSS 图片清理 product={product_id}: targets={len(image_urls)} deleted={deleted}")
            # ③ 向量库 best-effort 清理：删除该商品全部文案向量（RAG 可选，失败不影响 purge）
            if self.rag_store is not None:
                try:
                    removed_v = self.rag_store.delete_by_product(org_id=org_id, product_id=product_id)
                    log(f"[worker] Milvus 向量清理 product={product_id}: removed={removed_v}")
                except Exception as exc:  # noqa: BLE001
                    log(f"[worker] Milvus 向量清理失败（忽略）: {exc!r}")
            return "purged"
        finally:
            self.streams.ack(self.keys.job_product_purge(), msg_id, group=self.group)

    def consume_approval_once(self, *, block_ms: int = 1000) -> str | None:
        """处理一条审批回传；恢复图（result=approved/rejected）。

        并发保护「恢复全程持 lock:{thread_id}」：恢复前抢占该线程的分布式锁，
        未抢到说明另一 worker 正在恢复同一线程 → **直接 ack 跳过**（返回 "busy"），
        避免重复 resume 造成重复落库/重复终态事件；持锁期间崩溃由 TTL 自动释放。

        参数:
            block_ms: XREADGROUP 阻塞等待上限（毫秒）。
        返回:
            succeeded / rejected / failed / busy；无消息时返回 None。
        """
        pulled = self._pull_or_reclaim(
            self.keys.job_approval(), block_ms=block_ms, domain="job:approval", has_thread_lock=True
        )
        if pulled is None:
            return None
        msg_id, data, reclaimed = pulled
        return self._handle_approval(msg_id, data, reclaimed=reclaimed)

    def _handle_approval(self, msg_id: str | bytes, data: dict, *, reclaimed: bool) -> str:
        """执行一条审批恢复（已从流中取出）：主体逻辑与接入回收前完全一致。

        参数:
            msg_id: 消息 ID（用于 ack）。
            data: 审批载荷（thread_id / result / feedback / product_id / org_id）。
            reclaimed: 是否来自 PEL 回收（决定「抢锁失败」时 ack 还是保留）。
        返回:
            succeeded / rejected / failed / busy。
        注意:
            「回收 + 抢锁失败」**不 ack**：原持有者可能仍在恢复（重复 resume 会产生重复落库），
            留给下一轮回收，等锁 TTL 过期后再接管；
            「新投递 + 抢锁失败」ack 丢弃：同一线程的重复审批消息，处理任意一条结果相同。
        """
        thread_id = data["thread_id"]
        token = self.streams.acquire_lock(thread_id, ttl_seconds=self.approval_lock_ttl_seconds)
        if token is None:
            if reclaimed:
                log(
                    f"[worker] 回收的审批消息仍在恢复中（锁未释放），本轮不接管 thread_id={str(thread_id)[:8]}"
                )
                return "busy"
            log(f"[worker] 审批恢复已被他处持锁，跳过 thread_id={str(thread_id)[:8]}")
            self.streams.ack(self.keys.job_approval(), msg_id, group=self.group)
            return "busy"
        try:
            self._publish(
                data["thread_id"],
                {
                    "type": "approval.resumed",
                    "data": {"thread_id": data["thread_id"], "result": data.get("result")},
                },
            )
            wf = self._workflow()
            # 守卫：线程必须已有 checkpoint。脏审批单（库被重置 / 手工造的数据）指向的线程
            # 在图上并不存在 —— 直接 resume 会以"空 State"重跑一遍图，节点发事件时 thread_id
            # 为空抛 ValueError('evt:')，现场极难理解（2026-09 实测）。这里显式判定并给出可读原因。
            if hasattr(wf, "has_checkpoint") and not wf.has_checkpoint(data["thread_id"]):
                self._terminalize_failed(
                    data,
                    RuntimeError("线程不存在或已被清理（无 checkpoint）：拒绝 resume 以免空 State 重跑"),
                    stage="approval",
                )
                return "failed"
            decision = {"approved": data.get("result") == "approved", "feedback": data.get("feedback")}
            out = wf.resume(decision, config=wf.thread_config(data["thread_id"]))
            # 配图结果（含降级原因）同样落日志：resume 路径也会重跑 image 节点（HITL 分支）
            log(image_outcome_line(data["thread_id"], out))
            # 恢复终态：approved→（路由键 persist）save_content→published；rejected→reject_end→rejected（商品回 draft）
            if out.get("status") == "rejected":
                # 终态事件契约：与 save_content 发 done 对齐，驳回也要给 SSE 一个终态，
                # 否则详情页实时流等不到终态、按钮永远停在“AI 生成中”。
                self._publish(
                    data["thread_id"],
                    {
                        "type": "rejected",
                        "data": {
                            "thread_id": data["thread_id"],
                            "product_id": data.get("product_id"),
                            "org_id": data.get("org_id"),
                        },
                    },
                )
            if "product_id" in data and "org_id" in data:
                result = "rejected" if out.get("status") == "rejected" else "published"
                self._publish_outcome(data, result)
            return out.get("status") or "resumed"
        except Exception as exc:  # noqa: BLE001 恢复失败同样终态化 failed，避免卡在“等待审批/生成中”
            self._terminalize_failed(data, exc, stage="approval")
            return "failed"
        finally:
            # 先放锁再 ack：即便 ack 遇 Redis 抖动抛错，锁也不会滞留——
            # 锁滞留会让该线程后续恢复被误判“繁忙”直到 TTL 过期（本类调用方不回退游标，消息仍可重投）。
            self.streams.release_lock(thread_id, token)
            self.streams.ack(self.keys.job_approval(), msg_id, group=self.group)

    def _terminalize_failed(self, data: dict, exc: Exception, *, stage: str) -> None:
        """LLM/网络/DB 异常统一收口：向 evt 发 failed 终态事件、向 result:workflow 发 failed 结果，
        由 backend 消费器把商品回 draft（job failed、active_thread 清空），前端据此复位“AI 生成中”。

        参数:
            data: 原始任务载荷（读 thread_id / product_id / org_id）。
            exc: 触发失败的异常（仅用于日志，不决定终态语义）。
            stage: 失败阶段标识（generate / approval），仅用于日志。
        注意:
            两个发布动作各自 try/except 包裹：任一失败都不再上抛，
            因为本函数处于异常处理路径上，再抛会掩盖原始异常。
        """
        thread_id = data.get("thread_id")
        log(f"[worker] {stage} 任务失败 thread_id={str(thread_id or '')[:8]}: {exc!r}")
        try:
            self._publish(
                thread_id,
                {"type": "failed", "data": {"product_id": data.get("product_id"), "org_id": data.get("org_id")}},
            )
        except Exception as pub_exc:  # noqa: BLE001 事件发布失败不阻断 ack
            log(f"[worker] 发布 failed 事件失败: {pub_exc!r}")
        try:
            if data.get("product_id") and data.get("org_id"):
                self._publish_outcome(
                    data,
                    "failed",
                    error=f"{stage}: {type(exc).__name__}: {exc}",
                )
        except Exception as pub_exc:  # noqa: BLE001
            log(f"[worker] 发布 failed 结果失败: {pub_exc!r}")

    def _publish(self, thread_id: str, payload: dict) -> None:
        """写入 evt:{thread_id}（供 SSE 回放/实时尾随；worker 侧不依赖 EventBus 实例）。

        参数:
            thread_id: 线程 ID（决定事件流键名 evt:{thread_id}）。
            payload: 事件 dict（须 JSON 可序列化）。
        注意:
            直接走 RedisStreams.xadd 而不复用 EventBus，是为了让 worker 在
            「端口未注入」的极简场景下也能发事件；**信封结构与节点事件完全一致**
            （RedisStreams.xadd 内部同样经 _xadd 补 `data` 字段与 schema_version）。
            历史注释曾写「此处发的过程事件不带信封」，与实际实现不符，已更正 ——
            消费端（SSE）按同一套 `{"data": "<json>"}` 结构解析即可，无需按来源分流。
        """
        stream = self.keys.evt(thread_id)
        self.streams.xadd(stream, payload, maxlen=self.stream_maxlen_evt)
        # 每次发布都刷新 TTL：长任务的早中期事件不会因为「总时长 > TTL」而中途被整流回收
        self.streams.expire(stream, self.evt_ttl_seconds)

    def _publish_outcome(
        self, data: dict, result: str, *, content_snapshot: dict | None = None, error: str | None = None
    ) -> None:
        """图终态结果发布到 result:workflow（backend WorkflowResultConsumer 消费推进商品状态机）。

        只读 Redis 无 DB 写入，“ai-engine 不写 schema_pa_backend”；幂等：终态只执行一次。
        content_snapshot 仅在 awaiting_human（转人工）时携带：backend 建档 hitl_approvals 时
        落库 content_snapshot 列，供审批中心展示“AI 生成详情”。

        参数:
            data: 原始任务载荷；取 thread_id / product_id / org_id 作为结果三要素。
            result: 终态标识（published / awaiting_human / rejected / failed）。
            content_snapshot: 仅 awaiting_human 时携带的“待审文案+评估依据”快照；其余终态为 None。
            error: 仅 failed 时携带的失败原因（合规拦截 / 异常摘要）；published 等成功终态不传。
                为什么需要它（2026-09 实测）: 消费器只按 result 推进状态机，从不写 ``job.error``，
                而商品详情页的「最近一次任务失败原因」读的正是这个字段 —— 不回传原因时，
                失败在界面上表现为「商品莫名回到 draft」，合规拦截尤其看不见（等于静默降级）。
        _字段契约_:
            ``error`` 由 backend 消费器截断后写入 ``generation_jobs.error``；长度上限 1000 字符。
        注意:
            只读 Redis、无 DB 写入（ai-engine 不写 schema_pa_backend）；
            终态只应发布一次（调用方保证路径唯一）。
        """
        fields: dict = {
            "thread_id": data["thread_id"],
            "product_id": data["product_id"],
            "org_id": data["org_id"],
            "result": result,
        }
        if content_snapshot is not None:
            fields["content_snapshot"] = content_snapshot
        if error:
            # 失败原因随结果回传（截断保护：异常消息可能很长）→ backend 写 generation_jobs.error
            fields["error"] = str(error)[:1000]
        # 回传 backend 的请求关联 ID（可选字段）：backend 侧消费日志据此与「触发那次生成」的
        # HTTP 请求对齐 —— 一次生成跨三段日志（backend → Redis → ai-engine），这是唯一的串接手段
        request_id = data.get("request_id")
        if request_id:
            fields["request_id"] = request_id
        self.streams.xadd(
            self.keys.workflow_result(),
            fields,
            maxlen=self.stream_maxlen_job,
        )

    def _review_snapshot(self, out: dict) -> dict | None:
        """从挂起时的图状态快照打包“审批内容快照”（随 awaiting_human 结果发送）。

        批准前该文案不落 product_contents（高价=评估通过也须人工放行；质量耗尽=从未通过），
        只有本快照能把“待审文案+评估依据”带给审批人。
          reason              → high_value / quality_exhausted（与 workflowcore/node/node_conditions.py 同一判定）
          content.blocks      → 对齐 product_contents.content_data，前端可复用现有 blocks 渲染
          evaluation_result   → 最后一份评估结果（通过/评分/违规点），供审批人判断放行或驳回
          evaluation_attempts → 已评估次数

        参数:
            out: 挂起时的图状态（wf.invoke 的返回；含 raw_product_info / content_blocks /
                generated_content / evaluation_result / evaluation_attempts）。
        返回:
            快照 dict（reason / content.blocks / evaluation_result / evaluation_attempts）；
            结构固定，供 backend 落 hitl_approvals.content_snapshot 列。
        注意:
            仅在 "__interrupt__" 出现（即转人工）时调用；approval 未定，故不携带审批结论。
        """
        raw = out.get("raw_product_info") or {}
        # 图文详情：优先用 node_image 已拼好的 content_blocks（text+image，评审人看到与上架一致的图文）；
        # 无配图（老链路/降级）回退纯文本 blocks。
        blocks = out.get("content_blocks") or [{"type": "text", "text": out.get("generated_content") or ""}]
        return {
            "reason": "high_value" if is_high_value_product(raw) else "quality_exhausted",
            "content": {"blocks": blocks},
            # 图状态落账为 JSON dict（EvalOutput model_dump），此处仅防御性兜底 model 实例
            "evaluation_result": _jsonify(out.get("evaluation_result")) or {},
            "evaluation_attempts": int(out.get("evaluation_attempts") or 0),
        }