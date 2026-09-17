"""worker 可靠性路径的纯内存单测（假 Redis Streams / 假商品源 / 桩图；不需要 PG、Redis、LLM）。

覆盖（P0 可靠性加固的行为口径）:
  · PEL 回收：回收到的消息会被真正处理；回收失败不阻断取新消息；
  · 毒消息判定：`锁仍存在 → 跳过`（保护慢任务）、`投递次数达上限 → 转死信`、`脏数据 → 转死信`；
  · 幂等：`done` 标记命中 → duplicate（不重复生成）；仅 published 写标记（awaiting_human 不写）；
  · busy 语义：**新投递**抢锁失败 → ack 丢弃；**回收**抢锁失败 → 不 ack（留给下轮，避免丢任务）；
  · 保留策略：`_publish` 带 evt maxlen + 刷新 TTL；`_publish_outcome` 带 result 流 maxlen；
  · 观测：heartbeat / pending_stats。

为什么必须钉住这些语义:
    「busy 时到底 ack 不 ack」是本次设计的核心取舍 —— 写反了不会报错、只会静默丢任务或误判毒消息，
    唯一能守住它的就是这一组用例。
"""

from __future__ import annotations

import os

import redis

from src.adapters.redis_eventbus import MessageDecodeError, RedisKeys
from src.service.worker import ListingWorker, _default_consumer_name

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"
THREAD_ID = "55555555-5555-4555-8555-555555555501"

JOB_DATA = {"thread_id": THREAD_ID, "product_id": PRODUCT_ID, "org_id": ORG_ID}


class FakeStreams:
    """`worker.streams` 替身：记录调用序列，按脚本返回，覆盖回收 / 死信 / 幂等路径。

    关键开关:
        claimed            —— claim_stale 返回的滞留消息列表 `[(msg_id, payload, raw)]`
        new_message        —— read_next 返回的 `(msg_id, payload)`；None 表示空轮询
        read_error         —— read_next 抛出的异常（模拟脏数据）
        fail_claim         —— claim_stale 抛 RedisError（模拟回收阶段故障）
        delivery_counts    —— msg_id → 投递次数（毒消息判定）
        lock_held          —— lock_exists 返回值（True = 有实例正在处理）
        lock_available     —— acquire_lock 能否抢到（False = 抢不到 → busy）
        done               —— is_done 返回值（True = 该线程已 published）
    """

    def __init__(
        self,
        *,
        claimed: list | None = None,
        new_message: tuple | None = None,
        read_error: Exception | None = None,
        fail_claim: bool = False,
        delivery_counts: dict | None = None,
        lock_held: bool = False,
        lock_available: bool = True,
        done: bool = False,
    ) -> None:
        """初始化脚本化行为。"""
        self.claimed = claimed or []
        self.new_message = new_message
        self.read_error = read_error
        self.fail_claim = fail_claim
        self.delivery_counts = delivery_counts or {}
        self.lock_held = lock_held
        self.lock_available = lock_available
        self.done = done
        self.calls: list[tuple] = []

    def _log(self, name: str, *args, **kwargs) -> None:
        """记录一次调用。"""
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        """调用序列的命令名（断言顺序 / 是否发生用）。"""
        return [c[0] for c in self.calls]

    def kwargs_of(self, name: str) -> dict:
        """取指定命令第一次调用的关键字参数。"""
        for call in self.calls:
            if call[0] == name:
                return call[2]
        return {}

    # ---- 被 worker 调用的方法 ----
    def claim_stale(self, stream, group, *, min_idle_ms, count):
        """回收滞留消息（脚本化）。"""
        self._log("claim_stale", stream, min_idle_ms=min_idle_ms, count=count)
        if self.fail_claim:
            raise redis.exceptions.RedisError("claim 失败（测试注入）")
        return list(self.claimed)

    def read_next(self, stream, group=None, block_ms=None):
        """读新消息（脚本化）。"""
        self._log("read_next", stream, block_ms=block_ms)
        if self.read_error is not None:
            raise self.read_error
        return self.new_message

    def ack(self, stream, msg_id, group=None):
        """确认消费。"""
        self._log("ack", stream, msg_id)

    def deliveries_of(self, stream, msg_id, group=None):
        """投递次数。"""
        self._log("deliveries_of", stream, msg_id)
        return self.delivery_counts.get(msg_id, 1)

    def lock_exists(self, thread_id):
        """线程锁是否仍被持有。"""
        self._log("lock_exists", thread_id)
        return self.lock_held

    def acquire_lock(self, thread_id, *, ttl_seconds):
        """抢线程锁（lock_available=False 时返回 None = 抢不到）。"""
        self._log("acquire_lock", thread_id, ttl_seconds=ttl_seconds)
        return "lock-token" if self.lock_available else None

    def release_lock(self, thread_id, token):
        """释放线程锁。"""
        self._log("release_lock", thread_id, token)
        return True

    def is_done(self, thread_id):
        """终态幂等标记是否存在。"""
        self._log("is_done", thread_id)
        return self.done

    def mark_done(self, thread_id, *, ttl_seconds):
        """写终态幂等标记。"""
        self._log("mark_done", thread_id, ttl_seconds=ttl_seconds)
        return True

    def move_to_dlq(self, stream, msg_id, **kwargs):
        """转死信。"""
        self._log("move_to_dlq", stream, msg_id, **kwargs)
        return "dlq-1"

    def xadd(self, stream, payload, *, maxlen=None):
        """写流（事件 / 终态结果）。"""
        self._log("xadd", stream, payload, maxlen=maxlen)
        return "1-0"

    def expire(self, stream, ttl_seconds):
        """设 TTL（记录为关键字参数，便于断言 ttl 取值）。"""
        self._log("expire", stream, ttl_seconds=ttl_seconds)
        return True

    def heartbeat(self, *, ttl_seconds):
        """刷心跳。"""
        self._log("heartbeat", ttl_seconds=ttl_seconds)

    def pending_count(self, stream, group=None):
        """待处理条数。"""
        self._log("pending_count", stream)
        return 0


class FakeProductReader:
    """商品素材源替身（避免真连 PG）。"""

    def __init__(self, product: dict | None = None) -> None:
        """初始化。

        参数:
            product: 返回的素材；None 时返回一份最小可用素材。
        """
        self.product = product if product is not None else {"title": "测试商品", "base_price": 19.9}
        self.read_calls = 0

    def read(self, *, product_id: str, org_id: str):
        """读取素材（记录调用次数）。"""
        self.read_calls += 1
        return self.product


class StubWorkflow:
    """图替身：invoke/resume 直接返回脚本化状态（不建图、不跑节点）。"""

    def __init__(self, out: dict) -> None:
        """初始化。

        参数:
            out: invoke/resume 的返回状态。
        """
        self.out = out
        self.invoked = 0

    def invoke(self, state, config=None):
        """执行一次图。"""
        self.invoked += 1
        return dict(self.out)

    def resume(self, decision, config=None):
        """恢复一次图。"""
        self.invoked += 1
        return dict(self.out)

    def thread_config(self, thread_id: str) -> dict:
        """线程配置。"""
        return {"configurable": {"thread_id": thread_id}}


def _worker(streams: FakeStreams, *, workflow_out: dict | None = None, **overrides):
    """构造被测 worker：替换 streams / 商品源 / 图，其余保持真实实现。

    参数:
        streams: 假流对象。
        workflow_out: 桩图的返回状态（默认 `{}` → 视为 published 终态）。
        **overrides: 覆盖 ListingWorker 的其它构造参数（如 max_deliveries）。
    返回:
        `(worker, stub_workflow)`。
    """
    worker = ListingWorker(
        redis_client=object(),  # streams 被整体替换，客户端只作占位
        keys=RedisKeys("test"),
        runtime_pg_dsn="postgresql://role_pa_ai:x@127.0.0.1:5432/db",
        rule_precheck=False,
        consumer="worker-test",
        **overrides,
    )
    worker.streams = streams
    worker.product_reader = FakeProductReader()
    stub = StubWorkflow(workflow_out or {})
    worker._workflow = lambda rule_engine=None: stub  # type: ignore[assignment]
    return worker, stub


# --------------------------------------------------- PEL 回收：接管与处理
def test_claimed_message_is_processed_and_acked() -> None:
    """回收到的滞留消息会被真正处理：进图 → 落终态 → 写幂等标记 → ack。"""
    streams = FakeStreams(claimed=[("7-0", dict(JOB_DATA), None)])
    worker, stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "done"
    assert stub.invoked == 1
    assert "mark_done" in streams.names()
    assert any(c[0] == "ack" and c[1][1] == "7-0" for c in streams.calls)


def test_claimed_message_skipped_while_lock_held() -> None:
    """锁仍存在（可能只是慢任务）→ 不接管、不 ack、不转死信。"""
    streams = FakeStreams(claimed=[("7-0", dict(JOB_DATA), None)], lock_held=True)
    worker, stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result is None  # 没有可处理的新消息
    assert stub.invoked == 0
    assert "move_to_dlq" not in streams.names()
    assert "ack" not in streams.names()  # 关键：留在 PEL 等下轮（锁过期后接管）


def test_claimed_message_over_max_deliveries_goes_to_dlq() -> None:
    """投递次数达上限 → 转死信（锁已释放却仍未 ack，是真毒消息），不进图。"""
    streams = FakeStreams(
        claimed=[("7-0", dict(JOB_DATA), None)],
        delivery_counts={"7-0": 5},
    )
    worker, stub = _worker(streams, max_deliveries=5)

    result = worker.consume_generate_once(block_ms=1)

    assert result is None
    assert stub.invoked == 0
    assert streams.kwargs_of("move_to_dlq")["reason"] == "max_deliveries"
    assert streams.kwargs_of("move_to_dlq")["deliveries"] == 5


def test_claimed_dirty_message_goes_to_dlq() -> None:
    """回收到的脏数据（payload 解析失败）→ 直接留证送死信，不阻塞其余消息。"""
    streams = FakeStreams(claimed=[("8-0", None, "{坏数据")])
    worker, _stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result is None
    assert streams.kwargs_of("move_to_dlq")["reason"] == "json_decode_error"
    assert streams.kwargs_of("move_to_dlq")["raw"] == "{坏数据"


def test_dirty_new_message_goes_to_dlq() -> None:
    """新读到的脏数据（MessageDecodeError）同样送死信 —— 历史行为是每轮抛异常、消息永不清。"""
    streams = FakeStreams(
        read_error=MessageDecodeError("pa:test:job:generate", "9-0", "{坏数据")
    )
    worker, _stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result is None
    assert streams.kwargs_of("move_to_dlq")["reason"] == "json_decode_error"
    assert streams.kwargs_of("move_to_dlq")["raw"] == "{坏数据"


def test_claim_failure_still_reads_new_message() -> None:
    """回收阶段故障（Redis 抖动）不阻断取新消息：本轮照常消费。"""
    streams = FakeStreams(
        fail_claim=True,
        new_message=("1-0", dict(JOB_DATA)),
    )
    worker, stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "done"
    assert stub.invoked == 1


# ------------------------------------------------------- 幂等：终态短路与标记
def test_duplicate_thread_is_short_circuited() -> None:
    """done 标记命中 → backend 重复投递 / 回收重投都不再生成：直接 ack + duplicate。"""
    streams = FakeStreams(new_message=("1-0", dict(JOB_DATA)), done=True)
    worker, stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "duplicate"
    assert stub.invoked == 0
    assert "ack" in streams.names()
    assert "mark_done" not in streams.names()


def test_published_writes_done_marker_with_configured_ttl() -> None:
    """published 终态写幂等标记，TTL 取配置（默认 7 天）。"""
    streams = FakeStreams(new_message=("1-0", dict(JOB_DATA)))
    worker, _stub = _worker(streams, done_ttl_seconds=3600)

    assert worker.consume_generate_once(block_ms=1) == "done"
    assert streams.kwargs_of("mark_done")["ttl_seconds"] == 3600


def test_awaiting_human_does_not_write_done_marker() -> None:
    """转人工（interrupted）保持「未完成」语义：不写标记，否则审批后的重复投递会被误拒。"""
    out = {
        "__interrupt__": [{"value": "pending"}],
        "raw_product_info": {"base_price": 100.0},
        "generated_content": "待审文案",
    }
    streams = FakeStreams(new_message=("1-0", dict(JOB_DATA)))
    worker, _stub = _worker(streams, workflow_out=out)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "interrupted"
    assert "mark_done" not in streams.names()
    # 终态结果带 content_snapshot（审批中心展示 AI 生成详情用）
    outcome_calls = [c for c in streams.calls if c[0] == "xadd" and c[1][0] == "pa:test:result:workflow"]
    assert outcome_calls and outcome_calls[0][1][1]["result"] == "awaiting_human"
    assert "content_snapshot" in outcome_calls[0][1][1]


# ---------------------------------------------------- busy：ack 与不 ack 的取舍
def test_busy_on_new_delivery_acks_message() -> None:
    """**新投递**抢锁失败 = 重复投递 → ack 丢弃（处理任意一条结果相同）。"""
    streams = FakeStreams(new_message=("1-0", dict(JOB_DATA)), lock_available=False)
    worker, stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "busy"
    assert stub.invoked == 0
    assert "ack" in streams.names()


def test_busy_on_reclaimed_message_keeps_it_in_pel() -> None:
    """**回收**抢锁失败 → 不 ack：原持有者可能只是慢，ack 掉就等于丢任务。"""
    streams = FakeStreams(claimed=[("7-0", dict(JOB_DATA), None)], lock_available=False)
    worker, stub = _worker(streams)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "busy"
    assert stub.invoked == 0
    assert "ack" not in streams.names()


def test_approval_busy_on_reclaimed_keeps_it_in_pel() -> None:
    """审批链路同口径：回收 + 抢锁失败不 ack（避免重复 resume 造成重复落库）。"""
    approval = {"thread_id": THREAD_ID, "result": "approved", "product_id": PRODUCT_ID, "org_id": ORG_ID}
    streams = FakeStreams(claimed=[("7-0", dict(approval), None)], lock_available=False)
    worker, stub = _worker(streams)

    result = worker.consume_approval_once(block_ms=1)

    assert result == "busy"
    assert stub.invoked == 0
    assert "ack" not in streams.names()


# ------------------------------------------------------------ 保留策略与观测
def test_publish_applies_evt_maxlen_and_ttl() -> None:
    """worker 自己发的过程事件同样带 maxlen，并在每次发布后刷新 TTL。"""
    streams = FakeStreams()
    worker, _stub = _worker(streams, stream_maxlen_evt=300, evt_ttl_seconds=120)

    worker._publish(THREAD_ID, {"type": "generate.started"})

    assert streams.names() == ["xadd", "expire"]
    assert streams.kwargs_of("xadd")["maxlen"] == 300
    assert streams.kwargs_of("expire")["ttl_seconds"] == 120


def test_publish_outcome_applies_result_maxlen() -> None:
    """终态结果流按 job 上限裁剪（保留窗口够排查，且不会无界增长）。"""
    streams = FakeStreams()
    worker, _stub = _worker(streams, stream_maxlen_job=5000)

    worker._publish_outcome(dict(JOB_DATA), "published")

    assert streams.kwargs_of("xadd")["maxlen"] == 5000
    assert streams.calls[0][1][0] == "pa:test:result:workflow"


def test_heartbeat_and_pending_stats() -> None:
    """心跳刷新 + 三条任务流的待处理读数（观测入口）。"""
    streams = FakeStreams()
    worker, _stub = _worker(streams, heartbeat_ttl_seconds=45)

    worker.heartbeat()
    stats = worker.pending_stats()

    assert streams.kwargs_of("heartbeat")["ttl_seconds"] == 45
    assert stats == {"job:generate": 0, "job:approval": 0, "job:product_purge": 0}


def test_default_consumer_name_is_unique_per_process() -> None:
    """默认消费者名带 hostname 与 pid（多实例不共用名字，PEL 归属才不会串）。"""
    name = _default_consumer_name()
    assert name.startswith("worker-")
    assert name.endswith(f"-{os.getpid()}")

