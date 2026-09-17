"""evaluate 节点的规则层用例：阻断级 fail-fast、低危命中"不否决但必须留痕"。

为什么要有这组用例（2026-09 实测）:
    ① 规则层是唯一能**确定性**拦住违禁词的环节（LLM 会漏），必须锁死"命中即不通过、且不调 LLM"；
    ② low（提示级）命中过去被 evaluate 直接丢弃：既不扣分、也不进 violations，
       管理员配的低危词命中后"什么都没发生"（配置形同虚设、审批人看不到痕迹）。
       现在的契约是：low **不否决**（passed 不被改写），但参与扣分、进入 violations（带 severity）、
       并被喂给 LLM 复核。
"""

from __future__ import annotations

from src.adapters.rules import compile_rules_snapshot
from src.workflowcore.node.node_evaluate import evaluate_listing_node

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"
THREAD_ID = "55555555-5555-4555-8555-555555555501"

SNAPSHOT = {
    "words": [
        {"id": "w-high", "word": "最便宜", "severity": "high", "source": "广告法种子"},
        {"id": "w-low", "word": "最好", "severity": "low", "source": None},
    ],
    "rules": [],
}


class FakeGateway:
    """LLM 网关替身：记录被调用的提示词，返回脚本化 JSON。"""

    def __init__(self, payload: dict | None = None, *, explode: bool = False) -> None:
        """初始化。

        参数:
            payload: complete_json 返回的 dict（None 时返回"一次通过"）。
            explode: True 时被调用即抛错（用于断言"阻断级命中不该调 LLM"）。
        """
        self.payload = payload
        self.explode = explode
        self.prompts: list[str] = []

    def complete_json(self, prompt, **kwargs):  # noqa: ARG002 端口签名
        """返回脚本化评估结果。"""
        self.prompts.append(prompt)
        if self.explode:
            raise AssertionError("阻断级命中时不得调用 LLM")
        if self.payload is not None:
            return self.payload
        return {"passed": True, "score": 90.0, "violations": [], "facts_checked": []}


class FakeEvalLogStore:
    """评估日志端口替身（记录落库记录，供断言 rule_id / evaluator_type）。"""

    def __init__(self) -> None:
        """初始化空记录列表。"""
        self.records: list = []

    def save(self, record):
        """记录一条。"""
        self.records.append(record)
        return record

    def purge_by_product(self, **kwargs):  # noqa: ARG002 端口签名
        """不实现清理（用例不使用）。"""
        return 0


def _state(content: str) -> dict:
    """构造节点入参（最小可用 State）。"""
    return {
        "thread_id": THREAD_ID,
        "product_id": PRODUCT_ID,
        "org_id": ORG_ID,
        "raw_product_info": {"title": "测试商品", "base_price": 19.9},
        "generated_content": content,
    }


def test_blocking_hit_fails_fast_without_llm() -> None:
    """阻断级（high）命中：直接判不通过、落 rule 日志，且**不调用 LLM**。"""
    gateway = FakeGateway(explode=True)
    store = FakeEvalLogStore()

    out = evaluate_listing_node(
        _state("全网最便宜的水杯"),
        gateway=gateway,
        eval_log_store=store,
        rule_engine=compile_rules_snapshot(SNAPSHOT),
    )

    result = out["evaluation_result"]
    assert result["passed"] is False
    assert result["score"] == 60.0  # 100 - 40（high）
    assert [v["keyword"] for v in result["violations"]] == ["最便宜"]
    assert result["violations"][0]["rule_id"] == "w-high"
    assert result["violations"][0]["severity"] == "high"
    assert store.records[-1].evaluator_type == "rule"
    assert store.records[-1].rule_id == "w-high"


def test_low_hit_does_not_veto_but_is_visible_and_scored() -> None:
    """低危命中：passed 不被改写，但扣分 + 进 violations（带 severity）+ 喂给 LLM 复核。"""
    gateway = FakeGateway()  # LLM 说"一次通过 90 分"
    store = FakeEvalLogStore()

    out = evaluate_listing_node(
        _state("这款水杯最好用"),
        gateway=gateway,
        eval_log_store=store,
        rule_engine=compile_rules_snapshot(SNAPSHOT),
    )

    result = out["evaluation_result"]
    assert result["passed"] is True, "low 不得否决（否决权在 high/medium 与人工）"
    assert result["score"] == 85.0  # LLM 90 - low 罚 5
    assert [v["keyword"] for v in result["violations"]] == ["最好"]
    assert result["violations"][0]["severity"] == "low"
    assert "最好" in gateway.prompts[0], "低危命中必须进提示词让 LLM 复核"
    assert store.records[-1].evaluator_type == "llm"

    # 落账的 violations 必须能被状态模型还原（回归：曾把裸 dict 塞进 model_copy 的 violations，
    # 导致 EvalOutput.errors 取 .reason 直接炸 + pydantic 序列化告警）
    from src.workflowcore.state import EvalOutput

    restored = EvalOutput.model_validate(result)
    assert restored.errors == ["命中违禁词「最好」"], "低危命中要出现在反思/日志摘要里"


def test_low_hit_visible_in_skeleton_path_too() -> None:
    """无 LLM（骨架路径）时，低危命中同样要扣分并留下痕迹（不能只在真实 LLM 路径生效）。"""
    out = evaluate_listing_node(
        _state("这款水杯最好用"),
        gateway=None,
        rule_engine=compile_rules_snapshot(SNAPSHOT),
    )

    result = out["evaluation_result"]
    assert result["passed"] is True
    assert result["score"] == 85.0  # 骨架基线 90 - 5
    assert [v["keyword"] for v in result["violations"]] == ["最好"]


def test_no_hits_keeps_legacy_behaviour() -> None:
    """无命中：分数/行为与历史完全一致（规则层不该给"干净文案"额外惩罚）。"""
    out = evaluate_listing_node(
        _state("无线智能机械键盘，手感舒适"),
        gateway=FakeGateway(),
        rule_engine=compile_rules_snapshot(SNAPSHOT),
    )

    result = out["evaluation_result"]
    assert result["passed"] is True
    assert result["score"] == 90.0
    assert result["violations"] == []


def test_without_rule_engine_llm_path_unchanged() -> None:
    """未注入规则引擎：行为与接入前一致（纯 LLM 路径）。"""
    gateway = FakeGateway({"passed": True, "score": 77.0, "violations": [], "facts_checked": []})

    out = evaluate_listing_node(_state("最便宜的水杯"), gateway=gateway, rule_engine=None)

    result = out["evaluation_result"]
    assert result["passed"] is True and result["score"] == 77.0
    assert "低危" not in gateway.prompts[0]
