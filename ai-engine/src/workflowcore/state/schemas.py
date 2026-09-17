"""LLM 结构化输出契约（JSON Schema 强约束的单一事实源）。

设计：
  · 一份 pydantic model 同时充当「契约定义 / 解析校验器 / prompt schema 描述生成器」；
  · real adapter 尚未提供原生 json_schema 时（智谱当前仅 json_object），由 evaluate 节点
    在本模块做本地严格校验 + 失败自动修复，保证“输出不符合契约”永远不被静默放过；
  · EvalOutput 只承载契约字段，server 侧附加信息（结构修复错误、反思意见）经
    ListingState.last_eval_errors 单独携带，不污染 LLM 输出契约。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.types import StrictBool

class EvalViolation(BaseModel):
    """单条违规点：LLM 语义违规，或规则引擎确定性命中。

    约定:
        · extra="forbid"（多字段即契约破坏，由 evaluate 节点触发结构修复）；
        · violations 非空时业务层一票否决（EvalOutput.passed 按 False 落账）；
        · rule_id 仅在规则引擎命中时回填（审计溯源），纯 LLM 违规为 None。
    """

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(description="命中的违规词/极限词")
    reason: str = Field(description="违规原因/建议")
    # 规则引擎命中时回填对应 compliance_rules/compliance_words 行 id
    rule_id: str | None = None

class FactChecked(BaseModel):
    """单条事实一致性核验：fact 对照 raw_product_info 的事实基线。

    注意:
        consistent 走 StrictBool —— LLM 若以字符串 "true"/"yes" 回传属契约破坏，
        拒绝并通过结构修复回喂重出，不静默接受。
    """

    model_config = ConfigDict(extra="forbid")

    fact: str = Field(description="被核验的事实描述")
    # bool 走 strict：LLM 若以字符串 "true"/"yes" 回传属契约破坏，拒绝并触发修复
    consistent: StrictBool = Field(description="与商品素材事实基线是否一致")

class EvalOutput(BaseModel):
    """LLM Evaluator 输出契约（JSON Schema 强约束目标）。

    prompt 中要求的 key 与约束与本模型一致：passed/score/violations/facts_checked
    必填、不得出现额外字段；violations 非空时业务层一票否决（passed 按 False 落账）。
    """
    model_config = ConfigDict(extra="forbid")

    passed: StrictBool = Field(description="是否通过（业务层还会用 violations 一票否决）")
    score: float = Field(ge=0, le=100, description="质量分 0-100（LLM 语义评分，不设及格线）")
    violations: list[EvalViolation] = Field(default_factory=list)
    facts_checked: list[FactChecked] = Field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """面向 Reflection/evaluation_logs 的错误摘要（violations 一票否决时喂给重写提示）。"""
        return [v.reason or v.keyword for v in self.violations]

def eval_output_json_schema() -> dict:
    """导出 JSON Schema（provider 原生 json_schema 能力就绪后透传给网关，如 OpenAI strict 模式）。"""
    return EvalOutput.model_json_schema()

# Prompt 内嵌的紧凑 schema 描述（LLM 友好文本；本地严格校验以 EvalOutput 模型为准）
EVALUATION_SCHEMA_DESC = (
    '{"passed": bool, "score": number(0-100), '
    '"violations": [{"keyword": str, "reason": str}], '
    '"facts_checked": [{"fact": str, "consistent": bool}]}'
)


