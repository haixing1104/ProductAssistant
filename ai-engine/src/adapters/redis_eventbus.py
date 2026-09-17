"""Redis Streams 适配器：EventBus 端口实现 + keyspace 工具 + 分布式锁。
（核心在于对Redis的Stream类型的生产、消费队列使用）


职责A---->RedisStreams作用：
    1.入站（backend → ai-engine）：后端把"生成任务"、"审批回传"、"商品删除"等指令投递到 Redis Stream，本模块负责消费这些任务。
    2.出站（ai-engine → backend）：AI 引擎执行图（workflow）的最终结果，通过本模块写入 Redis Stream供后端消费。
    3.分布式锁： `SET NX EX` 原子抢占 + Lua CAS 释放（防止 TTL 过期后误删他人新锁）恢复/审批互斥锁（同一线程只允许一个 worker 恢复）
    4.Redis Stream拿到的最终结果，面对的最终消费者是backend的业务层

职责B---->RedisEventBus作用：
    1.从入站和出站的中间过程（Graph workflow图过程）中产生的所有中间事件（节点开始/中间节点结束/失败等），通过RedisEventBus发布任务`evt:{thread_id}`，出站提供给SSE，再由SSE推送给前端使用。
    2.RedisEventBus只管workflow的中间过程（可能包含多个子节点），不管workflow的整体最终结果
    3.RedisEventBus发布的中间过程，面对的最终消费者是用于frontend前端展示


实现要点:
    1. 所有出站消息(包括RedisEventBus的中间节点过程出站+RedisStreams的最终结果出站消息)统一经 _xadd() 落盘
        → 数据体的信封都必定携带 schema_version（版本纪律见 ports/event_bus.py；常量单点注入，避免各节点各写一套）；
    2. 分布式锁 = `SET NX EX` 原子抢占 + Lua CAS 释放（防止 TTL 过期后误删他人新锁）；
    3. 同步 API（redis-py 同步客户端），面向 worker 进程使用，不适用于 asyncio 事件循环。
    4. 三类数据,存储在同一个Redis实例，通过keyspace前缀区分。pa:{env}:{domain}:{suffix}
        4.1 Redis.Stream类型（入站任务流，消费任务，负责任务投递、审批回传、结果回传。）
            · job:generate / job:approval / job:product_purge —— backend → ai-engine（任务/审批回传）
            · result:workflow —— ai-engine → backend（图终态结果，backend 消费器推进商品状态机）
        4.2 Redis.Stream类型（中间节点事件日志与中断的回放源 RedisEventBus）
            · evt:{thread_id} —— ai-engine → backend（单线程增量事件，SSE 回放源）\
        4.3 Redis.String类型。分布式锁（String + TTL + Lua）恢复/审批互斥锁（同一线程只允许一个 worker 恢复）
            · lock:{thread_id} —— 恢复/审批分布式锁（本文件的 acquire_lock/release_lock）

keyspace键模板：
    1.job_generate
        - pa:{env}:job:generate
        - Stream类型
        - backend → ai-engine（生成任务）
    2.job_approval
        - pa:{env}:job:approval
        - Stream类型
        - backend → ai-engine（HITL 审批回传）
    3.job_product_purge
        - pa:{env}:job:product_purge
        - Stream类型
        - backend → ai-engine（商品删除清理）
    4.workflow_result
        - pa:{env}:result:workflow
        - Stream类型
        - ai-engine → backend（图终态结果）
    5.evt
        - pa:{env}:evt:{thread_id}
        - Stream类型
        - ai-engine → backend（SSE 回放源）
    6.lock
        - pa:{env}:lock:{thread_id}
        - String+TTL
        - worker ↔ worker（互斥）
    7.dlq
        - pa:{env}:dlq:{domain}（如 pa:{env}:dlq:job:generate）
        - Stream类型
        - worker → 运维（死信：投递次数超限的毒消息，需人工介入）
    8.done
        - pa:{env}:done:{thread_id}
        - String+TTL
        - worker → worker（终态幂等标记；仅 published 写入，防重复投递重复生成）
    9.heartbeat
        - pa:{env}:worker:heartbeat:{consumer}
        - String+TTL
        - worker → 运维（消费停滞探测：键过期即说明没有进程在消费）


依赖与部署:
    · 依赖 redis-py（见 requirements.txt）；本模块顶层导入，import 本文件即需该依赖；
    · 本地部署见 infra/docker-compose.yml：镜像 redis:7.4-alpine、`--appendonly yes`（AOF）、
      宿主端口 `${REDIS_PORT:-6379}`；容器内互访用服务名 `redis:6379`；
    · 连接所有权：本模块只持有调用方注入的 client，不创建也不负责关闭
      （生命周期由 redis-py 连接池与调用方托管）。

注意:
    · 流保留：`evt:{thread_id}` 由调用方在建流时按 maxlen 近似裁剪 + 设 TTL
      （RedisEventBus(maxlen=…, ttl_seconds=…) / worker 的 stream_maxlen_evt），避免无界增长；
    · PEL 回收：read_next 只读 `>`（本组尚未投递过的消息），worker 崩溃后未 ack 的消息会滞留在
      待处理列表 —— 由 worker 在每轮消费前调用 claim_stale()（XAUTOCLAIM）回收；
      处理不了的消息（投递次数超限 = 毒消息）由 move_to_dlq() 转死信流后再 ack。
    · 消费者名：PEL 按消费者归属，多进程部署必须注入唯一 consumer（默认 "worker" 只适合单进程），
      否则多个实例同名会互相“接管”对方的滞留消息。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import redis

from ..ports import EventBus
from ..ports.event_bus import with_schema_version

__all__ = ["MessageDecodeError", "RedisEventBus", "RedisKeys", "RedisStreams"]


class MessageDecodeError(Exception):
    """消息体不是合法 JSON（脏数据）。携带流名与 msg_id，便于调用方直接送死信流。

    为什么单独定义（不直接用 json.JSONDecodeError）:
        read_next 在内部完成 JSON 解码，解码失败时调用方拿不到 msg_id —— 而「把这条脏消息
        转 DLQ 或 ack 掉」恰恰必须用 msg_id。本异常把 msg_id 与原始文本一起带出来，
        避免脏数据永久滞留 PEL（历史行为：每轮 read_next 都抛，日志刷屏且消息永远不消失）。

    属性:
        stream: 完整流键名。
        msg_id: 该消息的 Stream ID（可直接用于 ack / move_to_dlq）。
        raw: 原始 `data` 字段文本（尽力解码为 str；用于死信排查）。
        cause: 触发失败的底层异常（JSONDecodeError）。
    """

    def __init__(self, stream: str, msg_id: Any, raw: str, cause: Exception | None = None) -> None:
        """初始化。

        参数:
            stream: 完整流键名。
            msg_id: 消息 ID（str 或 bytes）。
            raw: 原始 data 文本。
            cause: 底层异常（可为 None）。
        返回:
            无返回值。
        """
        super().__init__(f"{stream} 的消息 {msg_id} 不是合法 JSON: {cause!r}")
        self.stream = stream
        self.msg_id = msg_id
        self.raw = raw
        self.cause = cause

#: Redis lua脚本。释放锁的 CAS：仅当值仍是自己的 token 才删（避免 TTL 过期后误删他人刚获得的锁）。
#: 必须走 Lua 的原子性的原因：GET + DEL 是两次网络往返，中间可能被 TTL 过期驱逐或他人抢占，
#: 只有服务端单脚本执行才能保证「判断 + 删除」不可分割。
#: 约定：KEYS[1] = 锁键，ARGV[1] = 本次持有的 token；返回 1 = 删除成功，0 = 未删除（非本 token/已过期）。
_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""

#: 事件 topic 前缀：EventBus.publish 的 topic 约定形如 `evt:{thread_id}`。
_EVT_PREFIX = "evt:"

#: 默认消费组名：同环境的多个 worker 实例共用同一组，从而对任务流做分片消费（互不重复）。
_DEFAULT_GROUP = "ai-engine"

#: 默认消费者名：仅适合单进程 worker；多进程部署必须由调用方注入唯一名（见 RedisStreams.__init__）。
_DEFAULT_CONSUMER = "worker"


def _xadd(
    r: redis.Redis,
    stream: str,
    payload: dict[str, Any],
    *,
    maxlen: int | None = None,
    approximate: bool = True,
) -> str | bytes:
    """向Redis追加写入一条事件消息：字段 `data` = 已补 schema_version 的 JSON 信封。

    参数:
        r: redis-py 客户端（由调用方注入，本函数不创建也不关闭）。
        stream: 目标流键名（完整键名，如 `pa:dev:evt:{thread_id}`）。
        payload: 事件载荷（JSON 可序列化 dict）。
        maxlen: 保留的最大消息条数（近似裁剪 `XADD ... MAXLEN ~`）；None = 不裁剪。
            只影响「断线回放深度」，不影响正在读取的游标（游标是 ID，不随裁剪回退）。
        approximate: True 用 `~` 近似裁剪（按节点整块丢弃，性能好、长度可能略超上限）；
            False 为精确裁剪（O(N)，只在必须严格控长时使用）。
    返回:
        XADD 生成的 stream ID（形如 `1712345678901-0`；由 Redis 保证单调递增，即回放顺序游标）。
        类型随后端 decode_responses 配置为 str 或 bytes（本模块内部不解析该值）。
    注意:
        · `ensure_ascii=False` 保证中文在 Redis 里直接可读，排查问题时无需再解转义；
        · XADD 是追加上语义，同一条逻辑事件重复写入会产生两条消息（去重归消费端）；
        · 本函数是 job / result / evt 三类出站消息的**唯一落盘路径**，避免信封结构漂移；
        · maxlen 裁剪丢的是最旧消息：只影响「断线后能回放多久」，正在消费的游标不受影响。
    """
    kwargs: dict[str, Any] = {}
    if maxlen is not None and int(maxlen) > 0:
        kwargs["maxlen"] = int(maxlen)
        kwargs["approximate"] = approximate
    return r.xadd(
        stream,
        {"data": json.dumps(with_schema_version(payload), ensure_ascii=False)},
        **kwargs,
    )


def _decode_message(stream: str, msg_id: Any, fields: dict) -> dict[str, Any]:
    """从 XREADGROUP / XAUTOCLAIM 返回的字段里解出消息体（JSON dict）。

    参数:
        stream: 完整流键名（仅用于异常信息）。
        msg_id: 消息 ID（仅用于异常信息）。
        fields: 字段 dict（字段键与 `data` 值都可能是 str 或 bytes）。
    返回:
        解析后的消息 dict。
    异常:
        MessageDecodeError: `data` 字段缺失或不是合法 JSON；异常对象携带 msg_id 与原文。
    注意:
        把「兼容 str/bytes + JSON 解析」收敛到一处，供 read_next 与 claim_stale 共用，
        避免两条读取路径对脏数据的处理口径漂移。
    """
    raw = None
    if fields:
        raw = fields.get(b"data")
        if raw is None:
            raw = fields.get("data")
    if raw is None:
        raise MessageDecodeError(stream, msg_id, "")
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MessageDecodeError(stream, msg_id, text, exc) from exc


def _evt_thread_id(topic: str) -> str:
    """把事件 topic 归一化为裸 thread_id（EventBus.publish 入参的宽容层）。

    参数:
        topic: `evt:{thread_id}`（约定形式）或裸 `thread_id`（兼容历史调用）。
    返回:
        裸 thread_id（可直接喂给 RedisKeys.evt()）。
    异常:
        ValueError: 空值，或传入的是完整键名/含 `:` 的脏值（如 `pa:dev:evt:xxx`、`evt:evt:xxx`）。
            这类输入会拼出双前缀的错误键名（`pa:dev:evt:pa:dev:evt:xxx`），
            宁可早失败也不要静默写出脏数据、让 SSE 侧永远读不到事件。
    注意:
        只做前缀归一 + 基本防呆，不校验 thread_id 形态（UUID 由上游保证）。
    """
    candidate = topic.removeprefix(_EVT_PREFIX) if topic.startswith(_EVT_PREFIX) else topic
    if not candidate or ":" in candidate:
        raise ValueError(f"topic 需为 'evt:{{thread_id}}' 或裸 thread_id，收到: {topic!r}")
    return candidate


class RedisKeys:
    """keyspace 命名工具（模板 `pa:{env}:{domain}:{suffix}`）。

    注意:
        · 所有方法的参数都是**裸业务 id**（如 thread_id），不是键名；返回值才是完整键名；
        · 生产端与消费端必须用同一 env 构造实例，否则读写落在不同键上会静默失效
          （表现为「任务投递了但没人消费」）。
    """

    def __init__(self, env: str):
        """初始化 keyspace 构造器。

        参数:
            env: 环境标识（如 dev / test / prod），参与键名前缀，决定数据隔离域。
        """
        self.env = env

    def job_generate(self) -> str:
        """生成任务流（backend → ai-engine）：入站 `job:generate` 任务的消费源。

        返回:
            完整键名 `pa:{env}:job:generate`。
        """
        return f"pa:{self.env}:job:generate"

    def job_approval(self) -> str:
        """审批回传流（backend → ai-engine）：HITL 审批结果，驱动被中断的图 resume。

        返回:
            完整键名 `pa:{env}:job:approval`。
        """
        return f"pa:{self.env}:job:approval"

    def job_product_purge(self) -> str:
        """商品彻底删除流（backend → ai-engine）：触发该商品的 ai 侧数据清理（含向量/检查点）。

        返回:
            完整键名 `pa:{env}:job:product_purge`。
        """
        return f"pa:{self.env}:job:product_purge"

    def workflow_result(self) -> str:
        """图终态结果流（ai-engine → backend）：backend WorkflowResultConsumer 消费推进商品状态。

        返回:
            完整键名 `pa:{env}:result:workflow`。
        """
        return f"pa:{self.env}:result:workflow"

    def evt(self, thread_id: str) -> str:
        """单线程事件流（ai-engine → backend）：SSE 实时过程与断线回放的数据源。

        参数:
            thread_id: 裸线程 id（勿传键名；publish 侧由 _evt_thread_id 归一化后传入）。
        返回:
            完整键名 `pa:{env}:evt:{thread_id}`。
        注意:
            每个线程一条独立流：线程之间事件互不干扰，且便于按线程做保留/清理。
        """
        return f"pa:{self.env}:evt:{thread_id}"

    def lock(self, thread_id: str) -> str:
        """恢复/审批互斥锁键（worker ↔ worker互斥，同一线程只允许一个 worker 持锁，防重复 resume）。

        参数:
            thread_id: 裸线程 id。
        返回:
            完整键名 `pa:{env}:lock:{thread_id}`。
        """
        return f"pa:{self.env}:lock:{thread_id}"

    def dlq(self, domain: str) -> str:
        """死信流键（worker → 运维）：投递次数超限仍处理失败的毒消息落到这里。

        参数:
            domain: 原始流的领域名（如 `job:generate` / `job:approval`）。
        返回:
            完整键名 `pa:{env}:dlq:{domain}`。
        注意:
            死信只做「留证 + 告警」，不做自动重投：能自动处理的消息不该进死信，
            进死信的消息需要人工判断（脏数据要修生产者、业务异常要看日志）。
        """
        return f"pa:{self.env}:dlq:{domain}"

    def done(self, thread_id: str) -> str:
        """终态幂等标记键（worker ↔ worker）：防「backend 重复投递 / PEL 回收」导致重复生成。

        参数:
            thread_id: 裸线程 id。
        返回:
            完整键名 `pa:{env}:done:{thread_id}`。
        注意:
            只在图跑到 **published** 终态后写入；awaiting_human（待审批）与 failed（可重试）
            都不写 —— 否则会把「允许重试」和「等待审批」误判为已完成而拒绝后续消息。
        """
        return f"pa:{self.env}:done:{thread_id}"

    def heartbeat(self, consumer: str) -> str:
        """worker 心跳键（worker → 运维）：值 = 最近一次心跳时间戳，TTL 到期即说明消费停滞。

        参数:
            consumer: 消费者名（与 RedisStreams 注入的一致）。
        返回:
            完整键名 `pa:{env}:worker:heartbeat:{consumer}`。
        """
        return f"pa:{self.env}:worker:heartbeat:{consumer}"


class RedisStreams:
    """XADD/XREADGROUP 封装（同步；worker 进程使用）。

    入站任务消费

    注意:
        · 面向同步 worker 进程（redis-py 同步客户端），不要用于 asyncio 事件循环；
        · 不拥有 client 所有权：client 由调用方注入并在其作用域内管理；
        · 消费者名语义：消费组内「已投递未确认」的消息按消费者名归属，多进程部署必须为
          每个进程注入唯一 consumer（默认 "worker" 只适合单进程）。
    """

    def __init__(self, client: redis.Redis, keys: RedisKeys, *, consumer: str = _DEFAULT_CONSUMER) -> None:
        """注入客户端与 keyspace。

        参数:
            client: redis-py 同步客户端（调用方创建，本对象不负责关闭）。
            keys: keyspace 工具（决定 env 隔离域）。
            consumer: 消费者名；多进程部署应传唯一值（如 `worker-{hostname}-{pid}`），
                否则多进程共用同一名字会让未确认消息归属混乱、宕机后无法定向重投。
        注意:
            默认值保持与历史行为一致（"worker"），单进程 worker 无需改动；
            扩容到多进程时再显式注入唯一名。
        """
        self.r = client
        self.keys = keys
        self.consumer = consumer

    def ensure_group(self, stream: str, group: str = _DEFAULT_GROUP) -> None:
        """幂等创建Redis的消费组（MKSTREAM 会自动建流）。

        参数:
            stream: 完整流键名。
            group: 消费组名（默认 ai-engine）。
        返回:
            None。
        异常:
            redis.exceptions.ResponseError: **只容忍 BUSYGROUP**（组已存在，视为幂等成功）；
                其余服务端错误（键类型不是 stream 的 WRONGTYPE、ACL 权限不足、参数非法等）
                原样上抛——把配置错误误判成「组已存在」继续跑，只会把问题推迟到消费端静默丢事件。
        注意:
            · `id="0"` 的语义：消费组的 last-delivered-id 从 0-0 起算，因此 read_next 的 `>`
              **会把「本组尚未投递过」的消息从流头开始全部投递**（含建组之前入队的任务）——
              这正是 job 消费需要的「worker 重启不丢任务」；代价是新建组/重建组时会重放历史
              中尚未投递的消息（已 ack 的消息不会重放），消费端必须具备幂等；
              若只想消费建组之后到达的消息，应改用 `id="$"`（会丢掉建组前入队的历史）；
            · 已投递但未 ack 的消息**不会**被 `>` 再次投递（PEL 不在 `>` 的投递范围内）；
            · 本方法每轮调用都幂等，代价是一次 XGROUP CREATE 往返（如需可在外层缓存已建组的集合）。
        """
        try:
            self.r.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise  # 非「组已存在」→ 真实错误（类型/权限/参数），必须上抛而非静默吞
            # BUSYGROUP：组已存在，幂等语义下无需处理

    def xadd(self, stream: str, payload: dict[str, Any], *, maxlen: int | None = None) -> str | bytes:
        """写入一条消息：信封统一补 `schema_version`（job/result 消息也走此路径）。

        参数:
            stream: 完整流键名（如 `keys.job_generate()`）。
            payload: 消息载荷（JSON 可序列化 dict）。
            maxlen: 保留上限（近似裁剪）；None = 不裁剪。用于给 job/result 流兜底控长。
        返回:
            XADD 生成的 stream ID（回放/对账游标；类型随 decode_responses 配置为 str 或 bytes）。
        注意:
            与 RedisEventBus.publish 共用 _xadd：信封结构（`data` 字段 + schema_version）
            单点定义，避免两条写路径出现口径漂移。
        """
        return _xadd(self.r, stream, payload, maxlen=maxlen)

    def read_next(
        self, stream: str, group: str = _DEFAULT_GROUP, block_ms: int = 2000
    ) -> tuple[str | bytes, dict[str, Any]] | None:
        """从Redis消费组读一条新消息（'>' 之后未确认），未消费返回 None。

        参数:
            stream: 完整流键名。
            group: 消费组名（默认 ai-engine）。
            block_ms: 阻塞等待上限（毫秒）；超时即返回 None，属正常的空轮询而非错误。
        返回:
            `(msg_id, payload)` 元组，payload 已 JSON 解码为 dict；无新消息（超时）返回 None。
            msg_id 的类型随 decode_responses 配置为 str 或 bytes，可直接回传给 ack()。
        异常:
            MessageDecodeError: 消息体不是合法 JSON（脏数据）。异常对象携带 msg_id 与原始文本，
                调用方据此把该消息转死信流（move_to_dlq）或显式 ack 丢弃 —— 否则它会永久滞留 PEL。
                （历史行为是抛 json.JSONDecodeError：调用方拿不到 msg_id，无法定位与清理该消息。）
        注意:
            · 消费者名取实例的 `self.consumer`（多进程需注入唯一名，见 __init__）；
            · `>` 语义 = 投递「本组尚未投递过」的消息（含建组前的历史，见 ensure_group），
              但**不会重放**已投递给本消费者且未 ack 的消息（PEL 请用 claim_stale() 回收）；
            · 兼容 decode_responses=True/False：字段键与 data 值都可能是 str 或 bytes；
            · 每条读到的消息都必须由调用方在处理成功后调用 ack()，否则永远留在待处理列表。
        """
        self.ensure_group(stream, group)
        resp = self.r.xreadgroup(group, self.consumer, {stream: ">"}, count=1, block=block_ms)
        if not resp:
            return None
        _, messages = resp[0]
        msg_id, fields = messages[0]
        return msg_id, _decode_message(stream, msg_id, fields)

    def ack(self, stream: str, msg_id: str | bytes, group: str = _DEFAULT_GROUP) -> None:
        """消息处理完后，向Redis确认消息已成功消费（从待处理列表移出）。

        参数:
            stream: 完整流键名。
            msg_id: read_next 返回的消息 ID（str 或 bytes）。
            group: 消费组名（默认 ai-engine）。
        返回:
            None（XACK 返回值被有意忽略：重复 ack 不报错，幂等安全）。
        注意:
            处理失败时**不要** ack：留在 PEL 至少可人工排查（当前无自动重投）；
            只有「已成功产生副作用」或「确认可安全丢弃」的消息才 ack。
        """
        self.r.xack(stream, group, msg_id)

    # -------------------------------------- PEL 回收 / 死信 / 保留 / 幂等标记
    def claim_stale(
        self,
        stream: str,
        group: str = _DEFAULT_GROUP,
        *,
        min_idle_ms: int,
        count: int = 10,
        consumer: str | None = None,
    ) -> list[tuple[str | bytes, dict[str, Any] | None, str | None]]:
        """把「空闲超过 min_idle_ms」的滞留消息转交给本消费者（XAUTOCLAIM）。

        解决的问题：worker 崩溃/被杀或被 kill -9 后，已投递给它但未 ack 的消息永远躺在
        待处理列表（PEL）里 —— `>` 不会重投，Redis 也不会自动转移，任务就此卡死。
        本方法按「空闲时长」把这类消息抢过来重试，重试仍失败则由调用方送死信流。

        参数:
            stream: 完整流键名。
            group: 消费组名。
            min_idle_ms: 最小空闲时间（毫秒）：只有这么久未 ack 的消息才会被接管。
                ⚠️ 应**大于单次任务最长耗时**，否则正常的慢任务会被别的实例抢走重复执行
                （重复执行的后果由 done 幂等标记与线程锁兜底，但会浪费 LLM 调用）。
            count: 单次最多接管条数（避免一次拉太多阻塞主循环取新消息）。
            consumer: 接管者名字；None 用实例的 self.consumer。
        返回:
            列表，元素为 `(msg_id, payload, raw)`，顺序 = 流内顺序：
              · payload 为解析后的 dict；消息体不是合法 JSON 时 payload=None 且 raw 保留原文
                （调用方可直接送死信，不会因为一条脏数据阻塞整批回收）；
              · 无滞留消息时返回空列表。
        注意:
            · **不 ack** 任何消息：接管后处理权在调用方（处理成功再 ack）；
            · 只扫一批（start_id 固定 "0-0"，最多 count 条）不做内部循环：回收是「每轮消费前的
              顺手清理」，不应长时间占用主循环；
            · XAUTOCLAIM 会让消息的 delivery count +1，调用方用 deliveries_of() 判毒消息；
            · 兼容 redis-py 不同版本的返回结构（>=4.x 为 3 元组 (next_id, messages, deleted)，
              更早为 2 元组），并且 start_id/count 走位置参数以避开不同版本的关键字名差异；
            · Redis 版本要求 >= 6.2（本仓镜像 7.4）。
        """
        who = consumer or self.consumer
        resp = self.r.xautoclaim(stream, group, who, int(min_idle_ms), "0-0", int(count))
        messages = resp[1] if len(resp) > 1 else []
        claimed: list[tuple[str | bytes, dict[str, Any] | None, str | None]] = []
        for msg_id, fields in messages:
            if not fields:
                continue
            try:
                claimed.append((msg_id, _decode_message(stream, msg_id, fields), None))
            except MessageDecodeError as exc:
                claimed.append((msg_id, None, exc.raw))
        return claimed

    def deliveries_of(self, stream: str, msg_id: str | bytes, group: str = _DEFAULT_GROUP) -> int:
        """查某条待处理消息已被投递（含重投）的次数；不在 PEL 或查不到时返回 0。

        用途：毒消息判定 —— 投递次数达到上限仍失败的消息应转死信，而不是无限重试。

        参数:
            stream: 完整流键名。
            msg_id: 消息 ID（来自 read_next / claim_stale）。
            group: 消费组名。
        返回:
            投递次数（times_delivered）；消息已被 ack/删除时返回 0。
        注意:
            次数由 Redis 在 PEL 里自己维护（每次 XREADGROUP/XCLAIM 投递 +1），
            因此不需要在消息体或额外键里另存重试计数（少一处状态就少一处不一致）。
        """
        entries = self.r.xpending_range(stream, group, msg_id, msg_id, 1)
        if not entries:
            return 0
        entry = entries[0]
        value: Any = None
        if isinstance(entry, dict):
            value = entry.get("times_delivered")
            if value is None:
                value = entry.get(b"times_delivered")
        elif len(entry) > 3:  # 兼容 tuple 形式：(id, consumer, idle_ms, deliveries)
            value = entry[3]
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def move_to_dlq(
        self,
        stream: str,
        msg_id: str | bytes,
        *,
        dlq_stream: str,
        group: str = _DEFAULT_GROUP,
        deliveries: int = 0,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        raw: str | None = None,
        maxlen: int | None = None,
    ) -> str | bytes:
        """把一条处理不了的消息转存到死信流，然后 ack 原消息（**顺序不可颠倒**）。

        参数:
            stream: 原流键名。
            msg_id: 原消息 ID。
            dlq_stream: 死信流键名（`RedisKeys.dlq(domain)`）。
            group: 消费组名。
            deliveries: 已投递次数（留证用）。
            reason: 死信原因（如 `max_deliveries` / `json_decode_error`）。
            payload: 原消息载荷（无法解析时为 None，改用 raw 留证）。
            raw: 原消息原文（脏数据场景）。
            maxlen: 死信流保留上限；None = 不裁剪。
        返回:
            死信流中新消息的 Stream ID。
        异常:
            redis.exceptions.RedisError: 写死信失败时原样上抛，**且不会 ack 原消息**。
        注意:
            · 先 XADD 死信成功、再 XACK 原消息：任一步失败都不会让消息凭空消失
              （最坏是重复一条死信，可人工去重；反过来「先 ack 后写失败」就是丢消息）；
            · 死信消息同样走 _xadd：信封结构（data + schema_version）与其它出站消息一致，
              排查时用同一套解析逻辑即可。
        """
        envelope: dict[str, Any] = {
            "_original_stream": stream,
            "_original_id": str(msg_id),
            "_deliveries": int(deliveries),
            "_reason": reason,
        }
        if payload is not None:
            envelope["_payload"] = payload
        if raw is not None:
            envelope["_raw"] = raw
        dlq_id = _xadd(self.r, dlq_stream, envelope, maxlen=maxlen)
        self.ack(stream, msg_id, group=group)
        return dlq_id

    def pending_count(self, stream: str, group: str = _DEFAULT_GROUP) -> int:
        """待处理（已投递未 ack）消息条数；查询失败返回 -1。

        用途：可观测 —— pending 持续增长说明消费能力不足、或存在卡死/毒消息。
        注意：本方法只读且吞掉 Redis 异常（返回 -1），监控查询不应把业务主循环搞挂。
        """
        try:
            info = self.r.xpending(stream, group)
        except redis.exceptions.RedisError:
            return -1
        if not info:
            return 0
        value: Any = info.get("pending", 0) if isinstance(info, dict) else (info[0] if info else 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def expire(self, stream: str, ttl_seconds: int) -> bool:
        """给流设 TTL（秒）；返回是否设置成功。

        用途：线程事件流（`evt:{thread_id}`）用完即弃 —— 长期保留既无业务价值，又会让
        键与内存随商品数无界增长。TTL 到期由 Redis 自动回收整条流。
        注意：ttl_seconds <= 0 视为「不设 TTL」并返回 False（显式关闭而非误把 0 当立即过期）。
        """
        if int(ttl_seconds) <= 0:
            return False
        return bool(self.r.expire(stream, int(ttl_seconds)))

    def lock_exists(self, thread_id: str) -> bool:
        """线程锁是否仍被持有（回收时的毒消息判定用）。

        语义：锁还在 ⇒ 可能有实例正在正常处理这条消息（慢任务），不该判成毒消息；
        锁不在却仍未 ack ⇒ 处理方已崩溃/异常退出（generate/approval 的正常路径都会 ack），
        累计投递次数到上限后即可安全转死信。
        """
        return bool(self.r.exists(self.keys.lock(thread_id)))

    def mark_done(self, thread_id: str, *, ttl_seconds: int) -> bool:
        """写终态幂等标记（`SET NX EX`）。True=本次写入成功；False=已存在（重复终态）。

        只在图跑到 **published** 终态时调用：让「backend 重复投递 / PEL 回收」的同一线程
        不再重复生成（消费侧命中标记后直接 ack 并返回 duplicate）。
        TTL 决定幂等窗口（`AI_ENGINE_DONE_TTL_SECONDS`）：窗口外的重复投递会被当成新任务。
        """
        return bool(self.r.set(self.keys.done(thread_id), "1", nx=True, ex=int(ttl_seconds)))

    def is_done(self, thread_id: str) -> bool:
        """该线程是否已有 published 终态标记（重复消息的快速短路判据）。"""
        return bool(self.r.exists(self.keys.done(thread_id)))

    def heartbeat(self, *, ttl_seconds: int) -> None:
        """刷新本消费者心跳键（值 = 当前时间戳）。

        用途：可观测/告警 —— 键随 TTL 过期即说明「没有进程在消费」，这是进程存活探针
        之外的另一种信号（进程活着但消费停滞时，只有心跳能暴露）。
        """
        self.r.set(self.keys.heartbeat(self.consumer), str(int(time.time())), ex=int(ttl_seconds))

    # ----------------------------------------------------------- 分布式锁
    def acquire_lock(self, thread_id: str, *, ttl_seconds: int = 300) -> str | None:
        """获取 `lock:{thread_id}`（`SET NX EX`，值=本次持有的随机 token）。

        用于「恢复/审批全程持锁」：同一线程只允许一个 worker 在恢复，避免重复 resume
        触发重复副作用（重复落库/重复通知）。返回 token 表示持有成功；None 表示已被他人持有。
        TTL 为安全网：持锁进程崩溃时锁会自动过期，不会永久堵死该线程的后续恢复。

        参数:
            thread_id: 裸线程 id（内部经 keys.lock() 拼完整键名）。
            ttl_seconds: 锁自动过期时间（秒），默认 300；应显著大于单次恢复的耗时。
        返回:
            本次持有的随机 token（释放锁时必须回传同一值）；抢锁失败返回 None。
        注意:
            · `SET NX EX` 是单命令原子操作，不存在「先查后抢」的竞态窗口；
            · 无自动续期：一次恢复耗时若超过 ttl_seconds，锁会中途过期，理论上存在
              双持锁窗口（需要强保证时应改为可续期的看门狗模式，本文件不实现）；
            · 调用方必须用 try/finally 保证 release_lock 被调用（正常路径不依赖 TTL 过期）。
        """
        token = uuid.uuid4().hex
        got = self.r.set(self.keys.lock(thread_id), token, nx=True, ex=ttl_seconds)
        return token if got else None

    def release_lock(self, thread_id: str, token: str) -> bool:
        """释放锁：Lua CAS（仅当值仍是自己的 token 才删），避免 TTL 过期后误删他人新锁。

        参数:
            thread_id: 裸线程 id。
            token: acquire_lock 返回的 token（必须回传原值）。
        返回:
            True = 确实删除的是自己持有的锁；False = 锁已过期或已被他人重新获得（CAS 未命中）。
        注意:
            False 不是异常：属「锁自然失效」的正常分支，调用方可安全忽略；
            实现依赖 _RELEASE_LOCK_LUA 在服务端原子完成「比较 + 删除」。
        """
        return bool(self.r.eval(_RELEASE_LOCK_LUA, 1, self.keys.lock(thread_id), token))


class RedisEventBus(EventBus):
    """事件发布：写入 evt:{thread_id} 事件流（ai-engine → backend，SSE 回放/实时轮询源）。

    出站任务发布

    注意:
        只发布、不消费：读流/回放由 backend SSE 侧负责（消费端 backend-api/ 尚未落地）；
        实例无状态，可在多个节点/线程间复用（redis-py 连接池线程安全）。
    """

    def __init__(
        self,
        client: redis.Redis,
        keys: RedisKeys,
        *,
        maxlen: int | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """注入客户端与 keyspace。

        参数:
            client: redis-py 同步客户端（调用方创建，本对象不负责关闭）。
            keys: keyspace 工具（决定 env 隔离域）。
            maxlen: 事件流保留上限（近似裁剪）；None = 不裁剪。
                线程事件流是「过程回放」用途：只保留最近 N 条即可（断线重连够用）。
            ttl_seconds: 事件流 TTL（每次发布刷新）；None = 不设 TTL。
                线程结束后由最后一次刷新起算，到期自动回收整条流（防键/内存无界增长）。
        注意:
            端口 `EventBus.publish` 的签名不变（不含 maxlen/ttl）：保留策略属于**实现细节**，
            由装配方（worker）按部署需要注入，避免把存储策略泄漏进端口契约。
        """
        self.r = client
        self.keys = keys
        self.maxlen = maxlen
        self.ttl_seconds = ttl_seconds

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """把一条过程事件写入该线程的事件流。

        参数:
            topic: `evt:{thread_id}`（也接受裸 thread_id，见 _evt_thread_id）。
            payload: 事件载荷；信封由 _xadd 统一补 schema_version。
        返回:
            None。
        异常:
            ValueError: topic 不是 `evt:{thread_id}` / 裸 thread_id（例如误传完整键名）。
            redis.exceptions.RedisError: 连接或写入失败原样透出，由调用方决定重试或降级。
        注意:
            · XADD 返回的 stream ID（`毫秒-序号`，由 Redis 保证单调递增）**就是**前端回放的
              顺序游标，因此无需再自增业务 seq 字段；
            · XADD 的返回值有意丢弃（端口契约 publish -> None）：若消费端需要对账或断点续传，
              应先扩展 ports/event_bus.py 的返回契约，而不是在此私自加返回值；
            · 事件顺序 = 本进程调用顺序（同一线程内）；
            · 每次发布都刷新 TTL（长任务不会因为总时长超过 TTL 而中途丢事件流）。
        """
        stream = self.keys.evt(_evt_thread_id(topic))
        _xadd(self.r, stream, payload, maxlen=self.maxlen)
        if self.ttl_seconds and int(self.ttl_seconds) > 0:
            self.r.expire(stream, int(self.ttl_seconds))
