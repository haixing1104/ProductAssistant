"""RuleEngine 端口：确定性合规规则层的匹配边界。

职责:
    对「待发布文本」（AI 生成的整篇文案）做违禁词/极限词确定性判定，
    输出带 rule_id 的命中清单，供 evaluate 节点在调用 LLM 评估前做 fail-fast
    （命中高危词时不必浪费 LLM token；命中可溯源回 schema_pa_backend 的
    compliance_words / compliance_rules 两张表）。

约束:
    本端口是纯内存匹配（无 DB/Redis/HTTP IO），规则数据在构造期注入；
    ai-engine 对 compliance_words / compliance_rules 零 DB 权限
    （见 database/sql/0002_roles_grants.sql；ai-engine 另持有 schema_pa_backend
    的 products / hitl_approvals 只读授权），因此采用「backend 入队 job:generate
    时随带规则快照、由 worker 侧解析快照并注入」的方式，
    快照解析见 adapters/rules.py 的 compile_rules_snapshot()。

说明:
    · 匹配语义（最长命中 / 重叠处理 / 排序）由实现方定义，但必须满足两条不变量：
      1) kind == "word" 的命中，其 keyword 必为规则词表内的词面；
      2) 同一词面存在多条规则时，命中 severity 取其中的最高级（不得降级）。
    · 去重口径：引擎输出「无损事实」——同一文本片段被多条规则命中时逐条上报并各自
      携带 rule_id 以便审计溯源；重复扣分应在打分层按片段去重后处理。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

# 严重级：high=红线（阻断）/ medium=阻断但可容忍 / low=仅参考（不阻断）。
Severity = Literal["high", "medium", "low"]

# 阻断级：命中即 fail-fast（跳过 LLM 评估直接判不通过）。low 不否决，但要"有影响、有痕迹"
# ——被喂进 LLM 复核，并按 SEVERITY_PENALTY 扣分、写入 violations（见 node_evaluate）。
BLOCKING_SEVERITIES = frozenset({"high", "medium"})

# 扣分制（score 语义：max(0, 100 - Σ罚分)）。
# low 罚 5 分：**不否决**，但必须有可见影响 —— 历史实现里 low 记 0 罚分且被 evaluate 直接丢弃，
# 结果是"管理员配了低危词，命中后什么都没发生"（配置形同虚设）。未知 severity 仍记 0 罚分，
# 不抛异常，保证规则数据异常不阻断打分链路。
SEVERITY_PENALTY: dict[Severity, float] = {"high": 40.0, "medium": 20.0, "low": 5.0}


@dataclass(frozen=True)
class RuleHit:
    """一次确定性命中的结构化描述（rule_id 支持「拦截有据可查」）。

    注意:
        本记录不携带 span（起止下标）：当前契约只保证 keyword 与 reason；
        若打分层需按位置去重、或前端需高亮命中位置，应先扩展本端口契约。
    """

    rule_id: str | None
    kind: Literal["word", "regex"]
    keyword: str  # 命中的词（word）或正则命中片段（regex）
    severity: Severity
    reason: str  # 命中原因/修改建议（优先 compliance_rules.suggestion）

    def to_violation(self) -> dict[str, Any]:
        """转为 evaluation_result.violations 条目（schemas.EvalViolation 契约）。

        返回:
            {"keyword": ..., "reason": ..., "rule_id": ..., "severity": ...} 四元组字典。
        注意:
            severity 一并透出（low 也要能在审批中心看见"这是提示级命中"），
            与 schemas.EvalViolation.severity 的可选字段对齐。
        """
        return {
            "keyword": self.keyword,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "severity": self.severity,
        }


def rule_score(hits: list[RuleHit]) -> float:
    """规则单独裁决时的确定性分数：满分 100 按命中严重级扣分，下限 0。

    参数:
        hits: RuleEngine.check() 返回的命中清单（可含 low）。
    返回:
        max(0.0, 100.0 - Σ SEVERITY_PENALTY[hit.severity])；未命中（空列表）返回 100.0。
    注意:
        未知 severity 记 0 罚分而非抛异常，保证规则数据异常不阻断打分链路。
    """
    return max(0.0, 100.0 - sum(SEVERITY_PENALTY.get(h.severity, 0.0) for h in hits))


class RuleEngine(ABC):
    """合规规则引擎端口（纯内存匹配，无副作用）。"""

    @abstractmethod
    def check(self, text: str) -> list[RuleHit]:
        """扫描文本返回确定性命中清单；未命中返回 []。

        返回的 hits 含全部 severity（含 low），是否阻断由调用方按
        BLOCKING_SEVERITIES 决定，保证 low 语义演进无需改本接口。

        参数:
            text: 待检查文本（AI 生成的整篇文案或其片段）；空串返回 []。
        返回:
            RuleHit 列表：word 类按「位置优先取最长命中」去重叠，regex 类逐条 finditer；
            整体按 severity(high>medium>low) 再按 keyword 稳定排序（见实现方 docstring）。
        注意:
            实现方不得丢弃 rule_id（审计溯源）；同一片段的多条命中允许重复上报，
            是否按片段去重由打分层决定（引擎保持无损）。
        """
