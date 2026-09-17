"""合规匹配器（backend 侧预览用）：与 ai-engine 的 ``adapters/rules.CompiledRules`` **逐条对齐**。

为什么 backend 需要自己实现一份（不能 import ai-engine 的）:
    仓库红线：两个模块刻意不共享代码（可独立替换语言实现）。因此这里按「同名同义 + 各自单点」维护，
    与 keyspace、``event_envelope`` 同一纪律：**改一侧必须同步改另一侧**。

与 ai-engine 的对齐点（逐条核对过源码，勿凭印象改）:
    ① 词类：同起点择优 ``词长降序 → 严重级(high<medium<low) → 词表下标升序``；
       随后**左优先贪心去重叠**（起点落在已选区间内的候选一律丢弃）；
    ② 正则类：按规则顺序逐条 ``finditer``，**不跨条目去重**（同一片段被多条命中各报一条，保留溯源）；
    ③ 词与正则**不互相去重**（同一片段可同时被两类命中）；
    ④ 最终排序：``(严重级权重, keyword)`` 稳定排序；
    ⑤ severity 缺失/非法 → 一律按 ``high`` 兜底（宁可严，不可漏）；
    ⑥ 词面为空 / 正则语法错 → 该条目**跳过**，绝不因规则数据质量问题中断链路。

与 ai-engine 的**刻意差异**（本模块是预览，不是生产链路）:
    · 词类不做 AC 自动机（预览面对的是人写的短文本，可读性优先）：用「逐起点比对词表」实现；
      候选集合与 AC 扫描**等价**（AC 的 outputs 沿 fail 链给出的也是所有以该位置结尾的词）；
    · 额外返回 ``start``/``end``（ai-engine 的 ``RuleHit`` 不带 span）——前端要高亮命中位置；
    · 不设正则复杂度防护（与 ai-engine 同口径：质检责任在写入侧），但 CRUD 入口做语法预校验。

分数口径（与 ai-engine ``ports/rule_engine`` 同值）:
    ``BLOCKING_SEVERITIES = {high, medium}``；``rule_score = max(0, 100 - Σ罚分)``，罚分 high=40/medium=20/low=0。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BLOCKING_SEVERITIES",
    "SEVERITY_ORDER",
    "SEVERITY_PENALTY",
    "ComplianceHit",
    "ComplianceMatcher",
    "compile_matcher",
    "is_blocking",
    "normalize_severity",
    "rule_score",
]

#: 严重级权重（high 最优先）；与 ai-engine ``_SEVERITY_ORDER`` 同值
SEVERITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
#: 命中即阻断的严重级（与 ai-engine ``ports/rule_engine.BLOCKING_SEVERITIES`` 同值）
BLOCKING_SEVERITIES = frozenset({"high", "medium"})
#: 规则单独裁决的扣分制（与 ai-engine ``SEVERITY_PENALTY`` 同值）
SEVERITY_PENALTY: dict[str, float] = {"high": 40.0, "medium": 20.0, "low": 0.0}

#: 缺失/非法 severity 的兜底
_DEFAULT_SEVERITY = "high"


def normalize_severity(value: Any) -> str:
    """把任意 severity 值归一为 ``high``/``medium``/``low``（非法一律 high）。"""
    return value if value in SEVERITY_ORDER else _DEFAULT_SEVERITY


@dataclass(frozen=True)
class ComplianceHit:
    """一次确定性命中（字段对齐 ai-engine ``RuleHit``，另附 span 供前端高亮）。"""

    rule_id: str | None
    kind: str  # word / regex
    keyword: str
    severity: str
    reason: str
    start: int
    end: int


def is_blocking(severity: str) -> bool:
    """该严重级是否阻断（与 ai-engine 的 fail-fast 判据一致）。"""
    return normalize_severity(severity) in BLOCKING_SEVERITIES


def rule_score(hits: list[ComplianceHit]) -> float:
    """规则单独裁决的确定性分数：满分 100 按命中严重级扣分，下限 0（同 ai-engine）。"""
    return max(0.0, 100.0 - sum(SEVERITY_PENALTY.get(hit.severity, 0.0) for hit in hits))


class ComplianceMatcher:
    """编译后的匹配器（词表 + 预编译正则）。"""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        """编译快照载荷（形状与 ``job:generate`` 的 ``rules`` 完全一致）。

        参数:
            payload: ``{"words": [{id,word,severity,source}], "rules": [{id,pattern,severity,suggestion}]}``；
                None / 非 dict 视为无规则（``check`` 恒返回 []）。
        """
        words: list[tuple[str, str | None, str, str | None]] = []
        for item in (payload or {}).get("words") or []:
            if not isinstance(item, dict):
                continue
            word = str(item.get("word") or "").strip()
            if not word:
                continue  # 空词面跳过（与 ai-engine 一致）
            words.append(
                (
                    word,
                    str(item["id"]) if item.get("id") else None,
                    normalize_severity(item.get("severity")),
                    str(item["source"]) if item.get("source") else None,
                )
            )
        regexes: list[tuple[re.Pattern[str], str | None, str, str | None]] = []
        for item in (payload or {}).get("rules") or []:
            if not isinstance(item, dict):
                continue
            pattern = str(item.get("pattern") or "")
            if not pattern:
                continue
            try:
                compiled = re.compile(pattern)
            except re.error:
                continue  # 坏正则跳过（写入侧已做语法校验，这里是运行期兜底）
            regexes.append(
                (
                    compiled,
                    str(item["id"]) if item.get("id") else None,
                    normalize_severity(item.get("severity")),
                    str(item["suggestion"]) if item.get("suggestion") else None,
                )
            )
        self._words = words
        self._regexes = regexes

    @property
    def rule_count(self) -> tuple[int, int]:
        """``(词条数, 正则条数)``（预览响应回显，便于确认用的是哪份规则）。"""
        return len(self._words), len(self._regexes)

    def check(self, text: str) -> list[ComplianceHit]:
        """扫描文本返回全部确定性命中（词类 + 正则类，按严重级稳定排序）。"""
        if not text:
            return []
        hits = self._scan_words(text) + self._scan_regex(text)
        hits.sort(key=lambda hit: (SEVERITY_ORDER.get(hit.severity, 0), hit.keyword))
        return hits

    @staticmethod
    def _prefer(cand: tuple[int, int, int], cur: tuple[int, int, int]) -> bool:
        """同起点候选择优：词长更长 > 严重级更重 > 词表下标更小（与 ai-engine ``_prefer`` 同判据）。"""
        if cand[0] != cur[0]:
            return cand[0] > cur[0]
        if cand[1] != cur[1]:
            return cand[1] < cur[1]
        return cand[2] < cur[2]

    def _scan_words(self, text: str) -> list[ComplianceHit]:
        """词类扫描：逐起点择优 → 左优先贪心去重叠（见模块 docstring ①）。"""
        best: dict[int, tuple[int, int, int]] = {}  # start -> (词长, 严重级权重, 词表下标)
        for start in range(len(text)):
            for idx, (word, _rule_id, severity, _source) in enumerate(self._words):
                if not text.startswith(word, start):
                    continue
                cand = (len(word), SEVERITY_ORDER[severity], idx)
                cur = best.get(start)
                if cur is None or self._prefer(cand, cur):
                    best[start] = cand
        hits: list[ComplianceHit] = []
        cursor = 0  # 下一个允许作为命中起点的位置（左优先贪心去重叠）
        for start in sorted(best):
            if start < cursor:
                continue
            word_len, _, idx = best[start]
            word, rule_id, severity, source = self._words[idx]
            hit_end = start + word_len
            hits.append(
                ComplianceHit(
                    rule_id=rule_id,
                    kind="word",
                    keyword=text[start:hit_end],
                    severity=severity,
                    reason=f"命中违禁词「{word}」" + (f"（{source}）" if source else ""),
                    start=start,
                    end=hit_end,
                )
            )
            cursor = hit_end
        return hits

    def _scan_regex(self, text: str) -> list[ComplianceHit]:
        """正则类扫描：按规则顺序 ``finditer``，不跨条目去重（见模块 docstring ②）。"""
        hits: list[ComplianceHit] = []
        for compiled, rule_id, severity, suggestion in self._regexes:
            for match in compiled.finditer(text):
                hits.append(
                    ComplianceHit(
                        rule_id=rule_id,
                        kind="regex",
                        keyword=match.group(0),
                        severity=severity,
                        reason=suggestion or f"命中广告法规则「{compiled.pattern[:40]}」",
                        start=match.start(),
                        end=match.end(),
                    )
                )
        return hits


def compile_matcher(payload: dict[str, Any] | None) -> ComplianceMatcher:
    """由快照载荷编译匹配器（与 ai-engine ``compile_rules_snapshot`` 的入参形状一致）。"""
    return ComplianceMatcher(payload)
