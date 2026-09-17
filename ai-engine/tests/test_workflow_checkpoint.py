"""checkpoint 契约测试（纯内存，无需 PG/Redis）。

覆盖：
  · new_memory_checkpointer() 返回可用的 InMemorySaver（回归：曾误 import
    langgraph.checkpoint.memory.PostgresSaver 并引用未导入的 InMemorySaver，
    调用即 ImportError/NameError）；
  · interrupt 挂起 → Command(resume) 恢复；恢复后继续跑完终态；
  · 驳回分支走 reject_end；
  · 会话隔离：同 saver 下不同 thread_id 状态互不串。

说明：不注入 LLM/存储端口，节点按各自契约回退确定性 mock 行为
（evaluate 无 gateway → 确定性通过；image 无生图/存储 → 降级纯文本），
因此本用例只验证「编排 + checkpoint 语义」，不验证外部依赖。
"""

from __future__ import annotations

import uuid

import pytest

from src.workflowcore.graph import build_workflow, new_memory_checkpointer

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"

# 高价（> 500）→ 评估通过也须人工放行 → interrupt 挂起（见 node_conditions）
HIGH_VALUE_INFO = {"title": "高价商品", "base_price": 600}
LOW_VALUE_INFO = {"title": "平价商品", "base_price": 19.9}


def _workflow():
    """组装带内存 checkpointer 的工作流（支撑 interrupt/resume）。"""
    return build_workflow(checkpointer=new_memory_checkpointer(), max_retries=2)


def _state(thread_id: str, info: dict) -> dict:
    """构造 invoke 入参（必填：thread_id / product_id / org_id / raw_product_info）。"""
    return {
        "thread_id": thread_id,
        "product_id": PRODUCT_ID,
        "org_id": ORG_ID,
        "raw_product_info": info,
    }


def test_new_memory_checkpointer_is_inmemory_saver():
    """内存工厂必须返回 langgraph 的 InMemorySaver（回归：此前该函数是坏的）。"""
    from langgraph.checkpoint.memory import InMemorySaver

    assert isinstance(new_memory_checkpointer(), InMemorySaver)


def test_high_value_product_interrupts_then_resume_approves():
    """高价商品：评估通过 → HITL 挂起 → 批准 → save_content → succeeded。"""
    wf = _workflow()
    thread_id = str(uuid.uuid4())
    config = wf.thread_config(thread_id)

    out = wf.invoke(_state(thread_id, HIGH_VALUE_INFO), config=config)
    # 挂起：LangGraph 以 __interrupt__ 标记，且此刻尚未落库（status 为空）
    assert "__interrupt__" in out
    assert out.get("status") != "succeeded"
    payload = out["__interrupt__"][0].value
    assert payload["kind"] == "hitl.approval"
    assert payload["reason"] == "high_value"
    assert payload["thread_id"] == thread_id

    resumed = wf.resume({"approved": True, "feedback": "放行"}, config=config)
    assert resumed.get("status") == "succeeded"
    assert resumed.get("approval") == "approved"


def test_high_value_product_rejection_ends_rejected():
    """高价商品：HITL 驳回 → reject_end → status=rejected（不落 product_contents）。"""
    wf = _workflow()
    thread_id = str(uuid.uuid4())
    config = wf.thread_config(thread_id)

    assert "__interrupt__" in wf.invoke(_state(thread_id, HIGH_VALUE_INFO), config=config)

    resumed = wf.resume({"approved": False, "feedback": "文案不合规"}, config=config)
    assert resumed.get("status") == "rejected"
    assert resumed.get("approval") == "rejected"


def test_low_value_product_persists_without_interrupt():
    """低价商品：评估通过 → 直接落库终态，不触发人工审批。"""
    wf = _workflow()
    thread_id = str(uuid.uuid4())

    out = wf.invoke(_state(thread_id, LOW_VALUE_INFO), config=wf.thread_config(thread_id))
    assert "__interrupt__" not in out
    assert out.get("status") == "succeeded"


def test_threads_are_isolated_in_same_checkpointer():
    """同一 saver 下：A 线程挂起不影响 B 线程（thread_id 即状态隔离锚点）。"""
    wf = _workflow()
    thread_a, thread_b = str(uuid.uuid4()), str(uuid.uuid4())

    out_a = wf.invoke(_state(thread_a, HIGH_VALUE_INFO), config=wf.thread_config(thread_a))
    out_b = wf.invoke(_state(thread_b, LOW_VALUE_INFO), config=wf.thread_config(thread_b))

    assert "__interrupt__" in out_a, "高价线程应挂起"
    assert "__interrupt__" not in out_b and out_b.get("status") == "succeeded", "平价线程应独立跑完"

    # A 恢复后仍是 A 的商品信息，未被 B 覆盖
    resumed_a = wf.resume({"approved": True}, config=wf.thread_config(thread_a))
    assert resumed_a.get("status") == "succeeded"
    assert resumed_a["raw_product_info"]["title"] == HIGH_VALUE_INFO["title"]


@pytest.mark.parametrize("decision", [{"approved": True}, {"approved": False}])
def test_resume_is_idempotent_per_decision(decision):
    """resume 语义稳定：同一决策值分别走通「批准/驳回」两条终态。"""
    wf = _workflow()
    thread_id = str(uuid.uuid4())
    config = wf.thread_config(thread_id)
    assert "__interrupt__" in wf.invoke(_state(thread_id, HIGH_VALUE_INFO), config=config)

    resumed = wf.resume(decision, config=config)
    expected = "succeeded" if decision["approved"] else "rejected"
    assert resumed.get("status") == expected
