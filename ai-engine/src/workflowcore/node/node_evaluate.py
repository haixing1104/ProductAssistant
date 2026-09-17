"""Evaluation 主节点：LLM Evaluator（语义兜底）→ 写回 evaluation_result 并计数。

注入 rule_engine 时，先对整篇生成文案做确定性合规扫描；
阻断级（high/medium）命中即跳过 LLM 评估、直接按规则扣分制判不通过（fail-fast，不浪费 LLM token），

命中携带 rule_id 落 evaluation_logs（evaluator_type='rule'，拦截有据可查）。

无规则引擎时以 LLM 兜底路径覆盖。

结构化输出强约束（schemas.EvalOutput）：LLM 返回必须通过 pydantic 校验
（必填键/禁多余键/类型/score 范围），失败即把校验错误回喂 LLM 自动修复（上限 2 次），
仍失败按“结构错误”安全判不通过——绝不把不符合契约的输出静默放过。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ...ports import BLOCKING_SEVERITIES, EvalLogStore, EvalRecord, LLMGateway, RuleEngine, rule_score
from ..state import EvalOutput, EVALUATION_SCHEMA_DESC, eval_output_json_schema
from ..state import ListingState, ensure_state

# 结构修复（schema 不通过时把校验错误回喂 LLM 重出）上限：不占用 Reflection 重试预算
MAX_SCHEMA_REPAIRS = 2

def _build_eval_prompt(content: str, info: dict[str, Any]) -> str:
    """构造评估提示词：事实基线 + 待评估文案 + 强 schema 说明与合法示例。

    参数:
        content: 待评估文案（通常是 generated_content 全文）。
        info: 商品素材 dict，作为事实核验基线（供 LLM 对照价格/卖点等）。
    返回:
        评估提示词字符串。
    """
    return (
        f"你是电商合规与事实一致性评估器。事实基线(供核验): {info}\n"
        f"待评估文案:\n{content}\n"
        f"请返回 JSON（key 必须齐全，不要输出其它字段）: {EVALUATION_SCHEMA_DESC}\n"
        f"合法示例：{{\"passed\": true, \"score\": 88.0, \"violations\": [], "
        f"\"facts_checked\": [{{\"fact\": \"价格与素材一致\", \"consistent\": true}}]}}"
    )

def _fmt_validation_errors(exc: ValidationError) -> list[str]:
    """把 pydantic ValidationError 转成更容易读的面向修复/日志的 “路径: 原因” 列表。

    参数:
        exc: pydantic 校验异常（EvalOutput.model_validate 抛出）。
    返回:
        ["字段路径: 原因", ...]；根级错误路径记作 "(root)"。
    注意:
        返回的字符串会原样回喂给 LLM 做结构修复，因此保持简短可读。
    """
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc") or ()) or "(root)"
        out.append(f"{loc}: {e.get('msg', 'invalid')}")
    return out

def _request_strict_eval(gateway: LLMGateway, prompt: str) -> tuple[EvalOutput | None, list[str]]:
    """向 LLM 索取 JSON 评估结果，并强制按 EvalOutput 契约校验（失败自动修复）。

    修复循环：校验不过 → 把 "路径: 原因" 追加进 prompt 重新索要，
    最多 MAX_SCHEMA_REPAIRS 次；仍不过则返回 (None, errors) 由调用方安全降级为「不通过」。
    本循环不占用 Reflection 重试预算（两套计数相互独立）。

    参数:
        gateway: LLM 网关（complete_json）。
        prompt: 初始评估提示词。
    返回:
        (EvalOutput, []) 校验通过；(None, errors) 修复次数耗尽（errors 为最后的校验错误）。
    注意:
        LLM 返回非 dict、或 json_schema 强约束未被 provider 支持时，
        一律走本地 pydantic 校验兜底——绝不把不符合契约的输出静默放过。
    """
    errors: list[str] = []
    schema = eval_output_json_schema()
    for attempt in range(MAX_SCHEMA_REPAIRS + 1):
        raw = gateway.complete_json(prompt, schema_desc=EVALUATION_SCHEMA_DESC, json_schema=schema)
        if not isinstance(raw, dict):
            errors = ["LLM 返回非 JSON 结构"]
        else:
            try:
                return EvalOutput.model_validate(raw), []
            except ValidationError as exc:
                errors = _fmt_validation_errors(exc)
        if attempt < MAX_SCHEMA_REPAIRS:
            prompt = (
                prompt
                + "\n上一轮输出不满足 JSON Schema 约束（仅返回合规 JSON 修复）："
                + "；".join(errors)
            )
    return None, errors


def evaluate_listing_node(
    state: ListingState,
    gateway: LLMGateway | None = None,
    eval_log_store: EvalLogStore | None = None,
    event_bus=None,
    rule_engine: RuleEngine | None = None,
) -> dict:
    """对 generated_content 做评估：规则先行 fail-fast，未命中再走 LLM 语义评估。

    通过→路由键 persist；未过→按次数 Reflection/HITL（条件边见 node_conditions）。
    注入 rule_engine 属可选项：None 时行为与历史完全一致（纯 LLM/骨架路径）。

    参数:
        state: 当前图状态；读 generated_content / raw_product_info / org_id / product_id /
            thread_id / evaluation_attempts。
        gateway: LLM 网关；None 时走确定性「一次通过」骨架路径（不调用真实 LLM）。
        eval_log_store: 评估日志端口；None 时不落 evaluation_logs。
        event_bus: 事件总线；None 时不发 stage.evaluating / evaluate.result 事件。
        rule_engine: 合规规则引擎；命中阻断级（high/medium）即跳过 LLM，按规则扣分判不通过。
    返回:
        增量 dict：{"evaluation_attempts", "evaluation_result", "last_eval_errors"}；
        evaluation_result 以 JSON dict 落账（channel 持久化友好），读入时由 pydantic 还原。
    注意:
        · violations 一票否决：LLM 声称 passed 但带违规点时，业务层按 False 落账；
        · 结构修复耗尽时同样按「不通过」安全降级，并写入 last_eval_errors 供反思。
    """
    state = ensure_state(state)
    # 主生成模型产出的文案
    content = state.generated_content or ""
    # 商品基本信息
    info: dict[str, Any] = state.raw_product_info or {}
    # 评估次数
    attempts = state.evaluation_attempts + 1
    # 持久化id 方便HITL 中断恢复
    thread_id = state.thread_id


    if event_bus is not None:
        # 发布事件 方便通过backend SSE 推送到前端显示执行过程
        event_bus.publish(
            f"evt:{thread_id}",
            {"type": "stage.evaluating", "data": {"attempt": attempts, "product_id": state.product_id}},
        )

    # ---- 规则先行：阻断级命中 → fail-fast，不调用 LLM ----
    blocking_hits = (
        [h for h in rule_engine.check(content) if h.severity in BLOCKING_SEVERITIES]
        if rule_engine is not None and content
        else []
    )

    if blocking_hits:
        # 规则单独裁决：确定性扣分制分数 + violations 携带 rule_id（拦截有据可查）
        result = EvalOutput(
            passed=False,
            score=rule_score(blocking_hits),
            violations=[h.to_violation() for h in blocking_hits],
            facts_checked=[],
        )
        errors: list[str] = [h.reason or h.keyword for h in blocking_hits]
        eval_type = "rule"
        rule_id = blocking_hits[0].rule_id
    elif gateway is None:
        # 骨架/无 Key：确定性“一次通过”（不调用真实 LLM），分数语义同历史基线
        result = EvalOutput(passed=True, score=90.0)
        errors = []
        eval_type = "llm"
        rule_id = None
    else:
        # ---- LLM Evaluator 语义兜底（schemas 强约束 + 结构修复 + 安全降级） ----
        model, errors = _request_strict_eval(gateway, _build_eval_prompt(content, info))
        eval_type = "llm"
        rule_id = None
        if model is None:
            # 结构/解析彻底失败：按“未通过”安全降级，原因写入 last_eval_errors 供反思与日志
            result = EvalOutput(passed=False, score=0.0)
        else:
            # violations 一票否决：LLM 声称通过但带违规点 → 业务层按 False 落账
            result = model.model_copy(update={"passed": False}) if model.violations else model
            errors = model.errors or errors  # 反思/日志优先用违规原因



    # 评估日志写库。 EvalLogStore 边界动作（经端口注入，非 Node 直连）；rule 路径带 rule_id
    if eval_log_store is not None:
        # 持久化存入pgsql/productassistant/schema_pa_ai.evaluation_logs
        eval_log_store.save(
            EvalRecord(
                org_id=state.org_id,
                product_id=state.product_id,
                thread_id=thread_id,
                evaluator_type=eval_type,
                score=result.score,
                errors=errors,
                rule_id=rule_id,
            )
        )

    # 发布事件 方便通过backend SSE 推送到前端显示执行过程
    if event_bus is not None:
        event_bus.publish(
            f"evt:{thread_id}",
            {
                "type": "evaluate.result",
                "data": {
                    "attempt": attempts,
                    "passed": result.passed,
                    "score": result.score,
                    "violations": len(result.violations),
                    "product_id": state.product_id,
                },
            },
        )

    # Langgraph自动增量更新到ListState.evaluation_attempts、evaluation_result、last_eval_errors
    return {
        "evaluation_attempts": attempts,
        # 落账为 JSON dict（channel 持久化友好），节点读入时由 pydantic 校验/还原为 EvalOutput
        "evaluation_result": result.model_dump(mode="json"),
        "last_eval_errors": errors,
    }
