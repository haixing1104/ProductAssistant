"""state：内核状态层（图状态 Schema + LLM 结构化输出契约）。

导出:
    ListingState / ensure_state —— 图全量状态模型与「dict→模型」归一函数；
    EvalOutput / EvalViolation / FactChecked —— LLM Evaluator 的结构化输出契约；
    EVALUATION_SCHEMA_DESC / eval_output_json_schema —— prompt 内嵌描述与 JSON Schema 导出。
"""

from .schemas import EvalOutput, EvalViolation, FactChecked, EVALUATION_SCHEMA_DESC, eval_output_json_schema
from .state import ListingState, ensure_state

__all__ = [
    "EvalOutput",
    "EvalViolation",
    "FactChecked",
    "EVALUATION_SCHEMA_DESC",
    "eval_output_json_schema",
    "ListingState",
    "ensure_state"
]