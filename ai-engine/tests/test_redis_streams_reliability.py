"""Redis Streams 可靠性原语的纯内存单测（不需要真实 Redis 服务）。

覆盖（P0 可靠性加固）:
  · 流保留：`_xadd` / `RedisStreams.xadd` 的 maxlen 透传，以及「None = 不裁剪」；
  · PEL 回收：`claim_stale` 的返回解包（redis-py 3 元组 / 旧版 2 元组）、脏数据保留原文；
  · 死信：`move_to_dlq` 的「先写死信、再 ack」顺序；写失败时**不 ack**（宁可留 PEL 也不丢消息）；
  · 毒消息素材：`deliveries_of` 对 dict / tuple 两种返回形状的解析；
  · 观测：`pending_count` 吞异常返回 -1、`heartbeat` 写键、`expire` 对 ttl<=0 短路；
  · 幂等标记：`mark_done` 的 `SET NX EX` 参数、`is_done` / `lock_exists`；
  · 异常契约：`read_next` 抛 `MessageDecodeError`（携带 msg_id 与原文），供调用方送死信；
  · keyspace：`dlq` / `done` / `heartbeat` 三个新键的命名。

为什么这些用例必须存在:
    这些路径只有在「worker 崩溃 / 收到脏消息 / 消息重复投递」时才走到，手工验证几乎不可能；
    单元边界（参数、调用顺序、异常携带的信息）一旦漂移，只会在故障现场暴露。
"""

from __future__ import annotations

import json

import pytest
import redis

from src.adapters.redis_eventbus import (
    MessageDecodeError,
    RedisEventBus,
    RedisKeys,
    RedisStreams,
    _xadd,
)
from src.ports import EVENT_SCHEMA_VERSION

ENV = "test"
THREAD_ID = "55555555-5555-4555-8555-555555555501"


class Call:
    """一次被记录的 Redis 调用（命令名 + 位置参数 + 关键字参数）。"""

    def __init__(self, name: str, args: tuple, kwargs: dict) -> None:
        """记录调用。

        参数:
            name: 命令名。
            args: 位置参数。
            kwargs: 关键字参数。
        """
        self.name = name
        self.args = args
        self.kwargs = kwargs


class ScriptedRedis:
    """按脚本返回结果的 Redis 替身：记录调用序列，便于断言「参数」与「顺序」。

    只实现被可靠性原语用到的命令；未脚本化的命令返回 None。
    `fail_on` 中的命令会抛 RedisError（用于验证失败路径的取舍）。
    """

    def __init__(self, responses: dict | None = None, fail_on: set | None = None) -> None:
        """初始化。

        参数:
            responses: 命令名 → 返回值。
            fail_on: 需要抛 RedisError 的命令名集合。
        """
        self.calls: list[Call] = []
        self.responses = responses or {}
        self.fail_on = fail_on or set()

    def _record(self, name: str, *args, **kwargs):
        """记录一次调用并返回脚本化结果（命中 fail_on 时抛 RedisError）。"""
        self.calls.append(Call(name, args, kwargs))
        if name in self.fail_on:
            raise redis.exceptions.RedisError(f"{name} 失败（测试注入）")
        return self.responses.get(name)

    def names(self) -> list[str]:
        """调用序列的命令名（断言顺序用）。"""
        return [c.name for c in self.calls]

    def xadd(self, name, fields, **kwargs):
        """XADD（单独实现：需保留 kwargs，不能用通用的 _record 把 kwargs 当位置参数记）。"""
        self.calls.append(Call("xadd", (name, fields), kwargs))
        if "xadd" in self.fail_on:
            raise redis.exceptions.RedisError("xadd 失败（测试注入）")
        return self.responses.get("xadd")

    def xgroup_create(self, *args, **kwargs):
        """XGROUP CREATE（ensure_group 用）。"""
        return self._record("xgroup_create", *args, **kwargs)

    def xreadgroup(self, *args, **kwargs):
        """XREADGROUP。"""
        return self._record("xreadgroup", *args, **kwargs)

    def xautoclaim(self, *args, **kwargs):
        """XAUTOCLAIM。"""
        return self._record("xautoclaim", *args, **kwargs)

    def xpending_range(self, *args, **kwargs):
        """XPENDING <min> <max> <count>（查单条消息的投递次数）。"""
        return self._record("xpending_range", *args, **kwargs)

    def xpending(self, *args, **kwargs):
        """XPENDING（待处理总量）。"""
        return self._record("xpending", *args, **kwargs)

    def xack(self, *args, **kwargs):
        """XACK。"""
        return self._record("xack", *args, **kwargs)

    def expire(self, *args, **kwargs):
        """EXPIRE。"""
        return self._record("expire", *args, **kwargs)

    def set(self, *args, **kwargs):
        """SET（NX/EX）。"""
        return self._record("set", *args, **kwargs)

    def exists(self, *args, **kwargs):
        """EXISTS。"""
        return self._record("exists", *args, **kwargs)


def _streams(client: ScriptedRedis, consumer: str = "worker-test") -> RedisStreams:
    """构造被测 RedisStreams（注入替身客户端）。

    参数:
        client: 脚本化 Redis 替身。
        consumer: 消费者名。
    返回:
        RedisStreams 实例。
    """
    return RedisStreams(client, RedisKeys(ENV), consumer=consumer)


# --------------------------------------------------------------- keyspace 命名
def test_new_keys_follow_keyspace_convention() -> None:
    """dlq / done / heartbeat 三个新键沿用 `pa:{env}:...` 前缀（环境隔离靠前缀而非实例）。"""
    keys = RedisKeys(ENV)
    assert keys.dlq("job:generate") == "pa:test:dlq:job:generate"
    assert keys.done(THREAD_ID) == f"pa:test:done:{THREAD_ID}"
    assert keys.heartbeat("worker-1") == "pa:test:worker:heartbeat:worker-1"


# ------------------------------------------------------------------ 流保留策略
def test_xadd_applies_maxlen_and_schema_version() -> None:
    """_xadd 带 maxlen 时透传近似裁剪参数，且信封仍是 `data` + schema_version。"""
    client = ScriptedRedis()
    _xadd(client, "pa:test:evt:x", {"type": "t"}, maxlen=2000)
    call = client.calls[0]
    assert call.args[0] == "pa:test:evt:x"
    assert call.kwargs == {"maxlen": 2000, "approximate": True}
    payload = json.loads(call.args[1]["data"])
    assert payload["schema_version"] == EVENT_SCHEMA_VERSION
    assert payload["type"] == "t"


def test_xadd_without_maxlen_does_not_trim() -> None:
    """maxlen=None / 0 → 不传裁剪参数（行为与历史一致）。"""
    for maxlen in (None, 0):
        client = ScriptedRedis()
        _xadd(client, "pa:test:evt:x", {"type": "t"}, maxlen=maxlen)
        assert client.calls[0].kwargs == {}


def test_streams_xadd_passes_maxlen() -> None:
    """RedisStreams.xadd 把 maxlen 透传给底层（job/result 流兜底控长走这条路径）。"""
    client = ScriptedRedis()
    _streams(client).xadd("pa:test:result:workflow", {"result": "published"}, maxlen=10000)
    assert client.calls[0].kwargs["maxlen"] == 10000


def test_event_bus_publish_applies_maxlen_and_ttl() -> None:
    """RedisEventBus.publish 用 maxlen 裁剪并刷新 TTL（线程事件流用完即弃）。"""
    client = ScriptedRedis()
    bus = RedisEventBus(client, RedisKeys(ENV), maxlen=2000, ttl_seconds=600)
    bus.publish(f"evt:{THREAD_ID}", {"type": "content.chunk"})
    assert client.names() == ["xadd", "expire"]
    assert client.calls[0].kwargs["maxlen"] == 2000
    assert client.calls[1].args == (f"pa:test:evt:{THREAD_ID}", 600)


def test_event_bus_without_retention_is_unchanged() -> None:
    """未注入 maxlen/ttl（默认 None）时行为与加固前一致：只 XADD，不 EXPIRE。"""
    client = ScriptedRedis()
    RedisEventBus(client, RedisKeys(ENV)).publish(f"evt:{THREAD_ID}", {"type": "done"})
    assert client.names() == ["xadd"]
    assert client.calls[0].kwargs == {}


# ------------------------------------------------------------------ PEL 回收
def test_claim_stale_unpacks_three_tuple_and_keeps_dirty_raw() -> None:
    """XAUTOCLAIM 的 3 元组返回被正确解包；脏数据 payload=None 且保留原文。"""
    good = {"data": json.dumps({"thread_id": THREAD_ID})}
    bad = {"data": "{不是 JSON"}
    client = ScriptedRedis(responses={"xautoclaim": ["0-0", [("1-0", good), ("2-0", bad)], ["3-0"]]})
    claimed = _streams(client).claim_stale("pa:test:job:generate", min_idle_ms=60000, count=5)
    assert claimed == [("1-0", {"thread_id": THREAD_ID}, None), ("2-0", None, "{不是 JSON")]
    # 参数口径：start_id 固定 "0-0"、count 透传、consumer 取实例名（走位置参数以避开版本差异）
    assert client.calls[0].args == (
        "pa:test:job:generate",
        "ai-engine",
        "worker-test",
        60000,
        "0-0",
        5,
    )


def test_claim_stale_tolerates_legacy_two_tuple() -> None:
    """旧版 redis-py 返回 2 元组 (next_id, messages) 时同样可用（兼容性护栏）。"""
    good = {"data": json.dumps({"thread_id": THREAD_ID})}
    client = ScriptedRedis(responses={"xautoclaim": ["0-0", [("1-0", good)]]})
    claimed = _streams(client).claim_stale("pa:test:job:generate", min_idle_ms=1000)
    assert claimed == [("1-0", {"thread_id": THREAD_ID}, None)]


def test_claim_stale_empty_returns_empty_list() -> None:
    """无滞留消息时返回空列表，不抛异常（空回收是常态）。"""
    client = ScriptedRedis(responses={"xautoclaim": ["0-0", [], []]})
    assert _streams(client).claim_stale("pa:test:job:generate", min_idle_ms=1000) == []


# ------------------------------------------------------------------ 死信投递
def test_move_to_dlq_writes_before_ack() -> None:
    """顺序红线：先写死信、再 ack 原消息；死信体携带原流 / 原 ID / 原因 / 载荷。"""
    client = ScriptedRedis()
    _streams(client).move_to_dlq(
        "pa:test:job:generate",
        "1-0",
        dlq_stream="pa:test:dlq:job:generate",
        deliveries=5,
        reason="max_deliveries",
        payload={"thread_id": THREAD_ID},
        maxlen=10000,
    )
    assert client.names() == ["xadd", "xack"]
    call = client.calls[0]
    assert call.args[0] == "pa:test:dlq:job:generate"
    assert call.kwargs["maxlen"] == 10000
    body = json.loads(call.args[1]["data"])
    assert body["_original_stream"] == "pa:test:job:generate"
    assert body["_original_id"] == "1-0"
    assert body["_deliveries"] == 5
    assert body["_reason"] == "max_deliveries"
    assert body["_payload"] == {"thread_id": THREAD_ID}
    assert body["schema_version"] == EVENT_SCHEMA_VERSION
    assert client.calls[1].args[0] == "pa:test:job:generate"


def test_move_to_dlq_keeps_message_when_write_fails() -> None:
    """写死信失败时**绝不 ack**：消息留在 PEL 等下轮重试，而不是凭空消失。"""
    client = ScriptedRedis(fail_on={"xadd"})
    with pytest.raises(redis.exceptions.RedisError):
        _streams(client).move_to_dlq(
            "pa:test:job:generate",
            "1-0",
            dlq_stream="pa:test:dlq:job:generate",
            reason="json_decode_error",
            raw="{坏数据",
        )
    assert "xack" not in client.names()


def test_move_to_dlq_stores_raw_for_dirty_payload() -> None:
    """脏数据场景改用 _raw 留证（没有可解析的 payload）。"""
    client = ScriptedRedis()
    _streams(client).move_to_dlq(
        "pa:test:job:generate",
        "9-0",
        dlq_stream="pa:test:dlq:job:generate",
        reason="json_decode_error",
        raw="{坏数据",
    )
    body = json.loads(client.calls[0].args[1]["data"])
    assert body["_raw"] == "{坏数据"
    assert "_payload" not in body


# ------------------------------------------------------------ 投递次数与观测
def test_deliveries_of_parses_dict_and_tuple() -> None:
    """投递次数支持 redis-py 的 dict 形状与旧版 tuple 形状；查询不到返回 0。"""
    client = ScriptedRedis(responses={"xpending_range": [{"times_delivered": 3}]})
    assert _streams(client).deliveries_of("s", "1-0") == 3
    client = ScriptedRedis(responses={"xpending_range": [("1-0", "w", 100, 4)]})
    assert _streams(client).deliveries_of("s", "1-0") == 4
    client = ScriptedRedis(responses={"xpending_range": []})
    assert _streams(client).deliveries_of("s", "1-0") == 0


def test_pending_count_swallows_redis_errors() -> None:
    """观测查询失败返回 -1（监控不能把消费主循环搞挂）。"""
    client = ScriptedRedis(responses={"xpending": {"pending": 7}})
    assert _streams(client).pending_count("s") == 7
    client = ScriptedRedis(fail_on={"xpending"})
    assert _streams(client).pending_count("s") == -1


def test_expire_skips_non_positive_ttl() -> None:
    """ttl<=0 表示「不设 TTL」：直接短路，不把 0 当成「立即过期」。"""
    client = ScriptedRedis()
    assert _streams(client).expire("s", 0) is False
    assert client.names() == []
    client = ScriptedRedis(responses={"expire": 1})
    assert _streams(client).expire("s", 600) is True


# ------------------------------------------------------- 幂等标记与线程锁探测
def test_mark_done_uses_set_nx_ex_and_is_done_uses_exists() -> None:
    """幂等标记：SET NX EX（只写一次），查询用 EXISTS；线程锁探测同样走 EXISTS。"""
    client = ScriptedRedis(responses={"set": True, "exists": 1})
    streams = _streams(client)
    assert streams.mark_done(THREAD_ID, ttl_seconds=604800) is True
    assert client.calls[0].args[0] == f"pa:test:done:{THREAD_ID}"
    assert client.calls[0].kwargs == {"nx": True, "ex": 604800}
    assert streams.is_done(THREAD_ID) is True
    assert streams.lock_exists(THREAD_ID) is True


def test_mark_done_returns_false_when_already_marked() -> None:
    """已存在标记时 SET NX 返回 None → False（调用方可据此判断「重复终态」）。"""
    client = ScriptedRedis(responses={"set": None})
    assert _streams(client).mark_done(THREAD_ID, ttl_seconds=60) is False


def test_heartbeat_writes_timestamp_with_ttl() -> None:
    """心跳：写时间戳 + EX（键过期即代表消费停滞）。"""
    client = ScriptedRedis()
    _streams(client, consumer="worker-9").heartbeat(ttl_seconds=30)
    key, value = client.calls[0].args
    assert key == "pa:test:worker:heartbeat:worker-9"
    assert int(value) > 0
    assert client.calls[0].kwargs == {"ex": 30}


# --------------------------------------------------------------- 异常契约
def test_read_next_raises_message_decode_error_with_msg_id() -> None:
    """脏数据抛 MessageDecodeError 且携带 msg_id / 原文（调用方据此送死信）。"""
    client = ScriptedRedis(
        responses={"xreadgroup": [["pa:test:job:generate", [("5-0", {"data": "{坏数据"})]]]}
    )
    with pytest.raises(MessageDecodeError) as excinfo:
        _streams(client).read_next("pa:test:job:generate", block_ms=1)
    assert excinfo.value.msg_id == "5-0"
    assert excinfo.value.raw == "{坏数据"
    assert excinfo.value.stream == "pa:test:job:generate"


def test_read_next_returns_none_on_timeout() -> None:
    """空轮询（XREADGROUP 返回空）→ None，不是错误。"""
    client = ScriptedRedis(responses={"xreadgroup": None})
    assert _streams(client).read_next("pa:test:job:generate", block_ms=1) is None
