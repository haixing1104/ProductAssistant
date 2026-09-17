"""输入侧合规预检用例：开关默认值（fail-safe）与「拦截原因必须回传」的行为契约。

为什么要有这组用例（2026-09 实测事故）:
    ① 开关 ``AI_ENGINE_RULE_PRECHECK`` 原先默认关 —— seed 词表里就有「最便宜」「国家级」，
       于是**标题违规也照样生成、照样上架**（实测商品「最便宜的水杯」已 published）。
       合规闸门的默认方向必须是「宁可多拦，不可漏放」，且不得因为拼写错误静默失效；
    ② 拦截只发 evt 不够：结果载荷不带 ``error`` 时 backend 无从写 ``generation_jobs.error``，
       商品详情页表现为「商品莫名回到 draft」—— 与「配图静默降级」是同一类可观测性缺陷。
"""

from __future__ import annotations

from src.adapters.redis_eventbus import RedisKeys
from src.service.__main__ import _rule_precheck_enabled_from_env
from src.service.worker import ListingWorker
from tests.test_worker_reliability import FakeStreams, StubWorkflow

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"
THREAD_ID = "55555555-5555-4555-8555-555555555501"

#: 标题含 seed 红线词「最便宜」（high）—— 预检必须拦下
RULES = {
    "words": [{"id": "w-1", "word": "最便宜", "severity": "high", "source": "广告法种子"}],
    "rules": [],
}


class _TitleReader:
    """商品素材源替身：只关心 title（预检的唯一输入）。"""

    def __init__(self, title: str) -> None:
        """初始化。

        参数:
            title: 商品标题。
        """
        self.title = title
        self.read_calls = 0

    def read(self, *, product_id: str, org_id: str):  # noqa: ARG002 端口签名固定
        """返回素材（记录调用次数）。"""
        self.read_calls += 1
        return {"title": self.title, "base_price": 3.3}


def _worker(streams: FakeStreams, *, title: str, rule_precheck: bool) -> ListingWorker:
    """构造被测 worker：替换 streams / 素材源 / 图（不连 PG、Redis、LLM）。"""
    worker = ListingWorker(
        redis_client=object(),  # streams 被整体替换，客户端只作占位
        keys=RedisKeys("test"),
        runtime_pg_dsn="postgresql://role_pa_ai:x@127.0.0.1:5432/db",
        rule_precheck=rule_precheck,
        consumer="worker-test",
    )
    worker.streams = streams
    worker.product_reader = _TitleReader(title)
    stub = StubWorkflow({})
    worker._workflow = lambda rule_engine=None: stub  # type: ignore[assignment]
    worker.stub = stub  # 供断言「有没有进图」
    return worker


# ------------------------------------------------------------ 开关默认值（fail-safe）
def test_precheck_defaults_on_when_unset(monkeypatch) -> None:
    """未配置 → 开（曾经的默认关正是这次事故的成因）。"""
    monkeypatch.delenv("AI_ENGINE_RULE_PRECHECK", raising=False)
    assert _rule_precheck_enabled_from_env() is True


def test_precheck_defaults_on_when_blank(monkeypatch) -> None:
    """空白值 → 开（与仓库其它开关一致的「空白即默认」口径）。"""
    monkeypatch.setenv("AI_ENGINE_RULE_PRECHECK", "   ")
    assert _rule_precheck_enabled_from_env() is True


def test_precheck_explicit_off_tokens(monkeypatch) -> None:
    """显式关：0/false/no/off/disable（任一写法都必须彻底关掉）。"""
    for token in ("0", "false", "FALSE", "no", "off", "disable", "disabled"):
        monkeypatch.setenv("AI_ENGINE_RULE_PRECHECK", token)
        assert _rule_precheck_enabled_from_env() is False, token


def test_precheck_unknown_token_falls_back_to_on(monkeypatch) -> None:
    """无法识别的取值（如 typo）→ 开：合规闸门不得因拼写错误静默失效。"""
    monkeypatch.setenv("AI_ENGINE_RULE_PRECHECK", "ture")
    assert _rule_precheck_enabled_from_env() is True


# ------------------------------------------------------------ 拦截行为
def _job(rules: dict | None = None) -> dict:
    """构造一条 job:generate 载荷（可选带规则快照）。"""
    payload = {"thread_id": THREAD_ID, "product_id": PRODUCT_ID, "org_id": ORG_ID}
    if rules is not None:
        payload["rules"] = rules
    return payload


def test_blocked_title_fails_with_reason_in_result() -> None:
    """标题命中阻断级词：不进图、发 failed evt，且**结果载荷带原因**（详情页可见）。"""
    streams = FakeStreams(new_message=("8-0", _job(RULES)))
    worker = _worker(streams, title="最便宜的水杯", rule_precheck=True)

    result = worker.consume_generate_once(block_ms=1)

    assert result == "blocked_input"
    assert worker.stub.invoked == 0, "命中即拦，不该浪费一次生成"

    results = [
        call[1][1]
        for call in streams.calls
        if call[0] == "xadd" and call[1][0] == worker.keys.workflow_result()
    ]
    assert results, "必须向 result:workflow 发终态结果"
    payload = results[-1]
    assert payload["result"] == "failed"
    assert "input_compliance_blocked" in payload["error"]
    assert "最便宜" in payload["error"], "原因要能定位到命中的词"

    evt = [
        call[1][1]
        for call in streams.calls
        if call[0] == "xadd" and call[1][0] == worker.keys.evt(THREAD_ID)
    ]
    assert any(item.get("type") == "failed" for item in evt)


def test_clean_title_goes_through() -> None:
    """干净标题：预检放行，正常进图（闸门只拦命中的）。"""
    streams = FakeStreams(new_message=("9-0", _job(RULES)))
    worker = _worker(streams, title="无线智能机械键盘", rule_precheck=True)

    worker.consume_generate_once(block_ms=1)

    assert worker.stub.invoked == 1


def test_precheck_off_skips_the_gate() -> None:
    """显式关闭时不拦（生成链路行为与未接入规则引擎时一致）。"""
    streams = FakeStreams(new_message=("9-0", _job(RULES)))
    worker = _worker(streams, title="最便宜的水杯", rule_precheck=False)

    worker.consume_generate_once(block_ms=1)

    assert worker.stub.invoked == 1


def test_rule_engine_absent_means_no_gate() -> None:
    """载荷不带规则快照（无规则可比）→ 不拦：没配合规词的环境不会被误拦。"""
    streams = FakeStreams(new_message=("9-0", _job(None)))
    worker = _worker(streams, title="最便宜的水杯", rule_precheck=True)

    worker.consume_generate_once(block_ms=1)

    assert worker.stub.invoked == 1


def test_result_payload_error_truncated() -> None:
    """超长原因在结果载荷里被截断（Redis 载荷不该被异常消息撑爆）。"""
    streams = FakeStreams()
    worker = _worker(streams, title="x", rule_precheck=True)
    worker._publish_outcome(
        {"thread_id": THREAD_ID, "product_id": PRODUCT_ID, "org_id": ORG_ID},
        "failed",
        error="E" * 5000,
    )

    payload = [call[1][1] for call in streams.calls if call[0] == "xadd"][-1]
    assert len(payload["error"]) == 1000


def test_approval_resume_guard_rejects_unknown_thread() -> None:
    """脏审批单（线程无 checkpoint）→ 拒绝 resume 并终态化 failed（不空跑一遍图）。

    实测（2026-09）: 对不存在的线程 resume，LangGraph 会以"空 State"重跑图 →
    节点发事件时 thread_id 为空 → ``ValueError("…收到: 'evt:'")``；现在由守卫拦下并给出可读原因。
    """
    threads = FakeStreams(new_message=("7-0", {**_job(None), "result": "approved", "feedback": "放行"}))
    worker = _worker(threads, title="x", rule_precheck=True)
    # 图替身声明"该线程没有 checkpoint"（真实实现见 _WorkflowWrapper.has_checkpoint）
    worker._workflow = lambda rule_engine=None: _NoCheckpointWorkflow()  # type: ignore[assignment]

    result = worker.consume_approval_once(block_ms=1)

    assert result == "failed"
    results = [c[1][1] for c in threads.calls if c[0] == "xadd" and c[1][0] == worker.keys.workflow_result()]
    assert results and results[-1]["result"] == "failed"
    assert "checkpoint" in results[-1]["error"]


class _NoCheckpointWorkflow:
    """图替身：has_checkpoint 恒为 False（模拟脏审批单指向的线程不存在）。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.invoked = 0

    def has_checkpoint(self, thread_id: str) -> bool:  # noqa: ARG002 端口签名
        """恒 False：线程不存在。"""
        return False

    def resume(self, decision, config=None):  # noqa: ARG002 端口签名
        """不应被调用。"""
        self.invoked += 1
        return {}

    def thread_config(self, thread_id: str) -> dict:
        """线程配置。"""
        return {"configurable": {"thread_id": thread_id}}


def test_success_outcome_has_no_error_field() -> None:
    """成功终态不带 error 字段（否则会在详情页留下"成功却显示失败原因"的假象）。"""
    streams = FakeStreams()
    worker = _worker(streams, title="x", rule_precheck=True)
    worker._publish_outcome(
        {"thread_id": THREAD_ID, "product_id": PRODUCT_ID, "org_id": ORG_ID}, "published"
    )

    payload = [call[1][1] for call in streams.calls if call[0] == "xadd"][-1]
    assert "error" not in payload

