"""合规匹配器纯函数用例：把「与 ai-engine 逐条对齐」的语义**钉死**。

为什么值得单独测:
    预览的结论会直接影响运营判断（「这条文案会不会被拦」）。若本地匹配器与 ai-engine 的实现
    在某些边界上不一致（最长匹配、去重叠、排序、severity 兜底），预览就会**说谎** ——
    而说谎的预览比没有预览更危险。

覆盖的边界（与 ``services/compliance_matcher`` 模块 docstring 的编号一一对应）:
    ① 同起点取最长；② 左优先贪心去重叠；③ 词与正则不互相去重；④ 排序口径；
    ⑤ severity 非法按 high；⑥ 空词面/坏正则跳过。
"""

from __future__ import annotations

from pa_backend.services.compliance_matcher import (
    BLOCKING_SEVERITIES,
    compile_matcher,
    is_blocking,
    rule_score,
)

WORDS = [
    {"id": "w1", "word": "最便宜", "severity": "high", "source": "广告法"},
    {"id": "w2", "word": "最便宜啊", "severity": "medium", "source": None},
    {"id": "w3", "word": "最好", "severity": "low", "source": None},
]
RULES = [
    {"id": "r1", "pattern": "(第一|顶级)", "severity": "high", "suggestion": "改为客观描述"},
    {"id": "r2", "pattern": r"100%", "severity": "low", "suggestion": None},
]


def _matcher():
    """构造一份包含词与正则的快照匹配器。"""
    return compile_matcher({"words": WORDS, "rules": RULES})


def test_longest_match_wins_at_same_start():
    """① 同起点择优：更长的词胜出（``最便宜啊`` 吃掉 ``最便宜``）。"""
    hits = _matcher().check("这款最便宜啊")
    assert [hit.keyword for hit in hits] == ["最便宜啊"]
    assert hits[0].rule_id == "w2"
    assert hits[0].severity == "medium"


def test_leftmost_greedy_dedup_skips_overlapping_candidates():
    """② 左优先贪心去重叠：已选区间内的起点被丢弃（``最好`` 与 ``最好用`` 场景）。"""
    matcher = compile_matcher({"words": [{"id": "a", "word": "最好", "severity": "high"},
                                        {"id": "b", "word": "好用手", "severity": "high"}], "rules": []})
    hits = matcher.check("最好用手")
    assert [hit.keyword for hit in hits] == ["最好"]  # 起点 0 命中后，起点 1 的候选被丢弃
    assert hits[0].rule_id == "a"


def test_word_and_regex_hits_are_not_deduplicated():
    """③ 词与正则不互相去重：同一片段被两类命中时各报一条（保留审计溯源）。"""
    matcher = compile_matcher(
        {
            "words": [{"id": "w", "word": "第一", "severity": "high"}],
            "rules": [{"id": "r", "pattern": "第一", "severity": "high", "suggestion": "改写"}],
        }
    )
    hits = matcher.check("全国第一")
    assert sorted(hit.kind for hit in hits) == ["regex", "word"]
    assert {hit.rule_id for hit in hits} == {"w", "r"}


def test_sorting_is_by_severity_then_keyword():
    """④ 排序口径：先按严重级（high→medium→low），同级按 keyword 字典序。"""
    hits = _matcher().check("最便宜最好第一100%")
    assert [hit.severity for hit in hits] == sorted(
        [hit.severity for hit in hits], key=lambda sev: {"high": 0, "medium": 1, "low": 2}[sev]
    )
    high_hits = [hit.keyword for hit in hits if hit.severity == "high"]
    assert high_hits == sorted(high_hits)


def test_invalid_severity_falls_back_to_high():
    """⑤ severity 缺失/非法 → 按 high 兜底（宁可严不可漏），并计入阻断。"""
    matcher = compile_matcher({"words": [{"id": "x", "word": "绝对", "severity": "SEVERE"}], "rules": []})
    hits = matcher.check("绝对有效")
    assert hits[0].severity == "high"
    assert is_blocking(hits[0].severity) is True


def test_blank_word_and_broken_pattern_are_skipped():
    """⑥ 空词面与坏正则被跳过（不抛异常：规则数据质量问题不得中断链路）。"""
    matcher = compile_matcher(
        {
            "words": [{"id": "blank", "word": "   ", "severity": "high"}],
            "rules": [{"id": "bad", "pattern": "(未闭合", "severity": "high"}],
        }
    )
    assert matcher.check("任意文本") == []
    assert matcher.rule_count == (0, 0)


def test_empty_text_returns_no_hits():
    """空文本直接返回空命中（与 ai-engine 的短路一致）。"""
    assert _matcher().check("") == []


def test_score_and_blocking_thresholds():
    """分数口径：high 扣 40、medium 扣 20、low 扣 5，下限 0；high/medium 视为阻断。

    low 从 0 改为 5（2026-09）: 低危词原先"命中后既不扣分也不可见"，等于管理员配了没用；
    现在它不阻断、但必须留下可见影响（ai-engine 侧同步把 low 计入分数与 violations）。
    """
    hits = _matcher().check("最便宜啊最好100%")  # medium(20) + low(5) + low(5)
    assert rule_score(hits) == 70.0
    assert is_blocking("high") and is_blocking("medium") and not is_blocking("low")
    assert BLOCKING_SEVERITIES == {"high", "medium"}
    heavy = _matcher().check("最便宜啊" + "第一" * 5)  # high×5 + medium×1 → 下限 0
    assert rule_score(heavy) == 0.0


def test_none_snapshot_is_treated_as_no_rules():
    """无快照（None）等价于无规则：不报错、恒返回空命中。"""
    assert compile_matcher(None).check("最便宜") == []
    assert compile_matcher(None).rule_count == (0, 0)


def test_span_is_reported_for_ui_highlighting():
    """命中带 start/end（前端高亮用）；额外契约，ai-engine 侧不提供。"""
    hits = _matcher().check("xx第一xx")
    assert hits[0].start == 2 and hits[0].end == 4
