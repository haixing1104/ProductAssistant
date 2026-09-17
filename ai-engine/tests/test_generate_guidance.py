"""驳回意见进入生成提示词的契约（W8）。

为什么要有这组用例（2026-09 实测缺口）:
    驳回意见此前只落 ``hitl_approvals.feedback``，**没有任何确定性路径**进入下一轮生成：
    唯一的消费者是 Agent 的 ``get_approval_history`` 工具（要靠模型自己决定去查）。
    于是"驳回 → 重新生成"实质只是"重跑一次"，审批人的意见大概率白写。
    现在链路是确定性的：backend 触发时查最近一条驳回意见 → 载荷 ``guidance`` →
    ``ListingState.reject_guidance`` → 生成/反思提示词。
"""

from __future__ import annotations

from src.adapters.redis_eventbus import RedisKeys
from src.service.worker import ListingWorker
from src.workflowcore.node.node_generate import _build_generation_prompt
from src.workflowcore.state import ListingState
from tests.test_worker_reliability import FakeStreams

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"
THREAD_ID = "55555555-5555-4555-8555-555555555501"

FEEDBACK = "生成的图不够科技感，要体现科技感；文案不要出现极限词"


def _state(**kwargs) -> ListingState:
    """构造最小可用 State。"""
    base = {
        "thread_id": THREAD_ID,
        "product_id": PRODUCT_ID,
        "org_id": ORG_ID,
        "raw_product_info": {"title": "无线智能机械键盘", "base_price": 399},
    }
    return ListingState(**{**base, **kwargs})


def test_prompt_carries_reject_guidance() -> None:
    """带驳回意见时，提示词必须显式要求"逐条解决"（而不是当成可选参考）。"""
    prompt = _build_generation_prompt(_state(reject_guidance=FEEDBACK), attempts=0)

    assert FEEDBACK in prompt
    assert "驳回意见" in prompt and "逐条解决" in prompt


def test_prompt_without_guidance_unchanged() -> None:
    """无驳回意见（从未被驳回）：提示词不含该段，行为与历史一致。"""
    prompt = _build_generation_prompt(_state(), attempts=0)

    assert "驳回意见" not in prompt
    assert "无线智能机械键盘" in prompt


def test_blank_guidance_treated_as_absent() -> None:
    """空白意见（老数据/空串）不产生空提示段。"""
    prompt = _build_generation_prompt(_state(reject_guidance="   "), attempts=1)

    assert "驳回意见" not in prompt


class _CapturingWorkflow:
    """图替身：记录 invoke 收到的 state（验证 worker 把载荷 guidance 传进图）。"""

    def __init__(self) -> None:
        """初始化空记录。"""
        self.states: list[dict] = []

    def invoke(self, state, config=None):  # noqa: ARG002 端口签名
        """记录 state 并返回 published 终态。"""
        self.states.append(dict(state))
        return {"status": "succeeded"}

    def resume(self, decision, config=None):  # noqa: ARG002 端口签名
        """记录 state 并返回 succeeded。"""
        return {"status": "succeeded"}

    def thread_config(self, thread_id: str) -> dict:
        """线程配置。"""
        return {"configurable": {"thread_id": thread_id}}


class _Reader:
    """商品素材源替身。"""

    def read(self, *, product_id: str, org_id: str):  # noqa: ARG002 端口签名
        """返回最小素材。"""
        return {"title": "无线智能机械键盘", "base_price": 399}


def _worker(streams: FakeStreams) -> tuple[ListingWorker, _CapturingWorkflow]:
    """构造被测 worker（替换 streams / 素材源 / 图）。"""
    worker = ListingWorker(
        redis_client=object(),
        keys=RedisKeys("test"),
        runtime_pg_dsn="postgresql://role_pa_ai:x@127.0.0.1:5432/db",
        rule_precheck=False,
        consumer="worker-test",
    )
    worker.streams = streams
    worker.product_reader = _Reader()
    stub = _CapturingWorkflow()
    worker._workflow = lambda rule_engine=None: stub  # type: ignore[assignment]
    return worker, stub


def test_worker_passes_payload_guidance_into_graph_state() -> None:
    """载荷 ``guidance`` → 图状态 ``reject_guidance``（链路确定性的一环）。"""
    job = {
        "thread_id": THREAD_ID,
        "product_id": PRODUCT_ID,
        "org_id": ORG_ID,
        "rules": {"words": [], "rules": []},
        "guidance": FEEDBACK,
    }
    worker, stub = _worker(FakeStreams(new_message=("8-0", job)))

    worker.consume_generate_once(block_ms=1)

    assert stub.states[0]["reject_guidance"] == FEEDBACK


def test_worker_without_guidance_sets_empty_string() -> None:
    """载荷没有 guidance（老消息/后端未下发）→ 空串，行为与历史一致。"""
    job = {"thread_id": THREAD_ID, "product_id": PRODUCT_ID, "org_id": ORG_ID}
    worker, stub = _worker(FakeStreams(new_message=("8-0", job)))

    worker.consume_generate_once(block_ms=1)

    assert stub.states[0]["reject_guidance"] == ""
