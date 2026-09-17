"""EvalLogStore 端口：
评估后落库，持久化存入pgsql/productassistant/schema_pa_ai.evaluation_logs；
并提供 admin 彻底删除商品时的物理清理能力（purge_by_product）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EvalRecord:
    """一次评估的落库载荷，字段对齐 evaluation_logs"""

    org_id: str
    product_id: str
    thread_id: str
    evaluator_type: str  # rule / llm
    score: float | None = None  # 综合得分（规则与 LLM 评估合并后；DB 列为 numeric(5,2)）
    errors: list = None  # 失败明细（jsonb 数组；None 由 __post_init__ 归一为 []）
    rule_id: str | None = None  # 命中的合规规则 ID（逻辑引用 schema_pa_backend.compliance_rules）
    latency_ms: int | None = None  # 评估耗时（毫秒），供性能与成本分析

    def __post_init__(self) -> None:
        """归一默认值：errors 为 None 时置为 []（对齐 DB 列 jsonb NOT NULL DEFAULT '[]'）。"""
        if self.errors is None:
            self.errors = []


class EvalLogStore(ABC):
    """评估/试错日志写库端口（支撑前端 Trace 面板与通过率分析）。"""

    @abstractmethod
    def save(self, record: EvalRecord) -> EvalRecord:
        """持久化一条评估日志并返回传入的记录（id 由适配器/DB 生成，不回填）。

        参数:
            record: 待落库记录（EvalRecord）。
        返回:
            传入的 record 本身；DB 生成的 id 不回填。
        注意:
            llm_usage（成本/用量分析）不在本记录契约内，落库取 DB 列默认值 '{}'。
        """

    @abstractmethod
    def purge_by_product(self, *, org_id: str, product_id: str) -> int:
        """物理清理某商品全部评估日志（admin 彻底删除用；ai-engine 独占写）。

        参数:
            org_id: 组织 ID。
            product_id: 商品 ID。
        返回:
            实际删除的行数；0 表示该商品无评估日志。
        """
