"""RuleEngine 确定性实现：AC 自动机违禁词典 + 预编译正则（无 IO、纯内存）。

职责:
    实现 ports/rule_engine.py 的 RuleEngine：把 backend 随 job 投递的 rules 快照
    编译为内存匹配器，对「待发布文本」输出带 rule_id 的确定性命中清单，
    供 evaluate 节点在调用 LLM 评估前做 fail-fast。

数据形状:
    对齐 backend 入队 job:generate 载荷里的 rules 快照：
    payload = {
        "words": [{"id": str, "word": str, "severity": "high"|"medium"|"low", "source": str|None}],
        "rules": [{"id": str, "pattern": str, "type": "regex", "severity": "...", "suggestion": str|None}],
    }

实现要点:
    · 违禁词走 AC 自动机（trie + fail 指针 + outputs 沿 fail 链合并）：一次建表、
      单趟扫描文本 O(n)，命中按「位置优先取最长」去重叠，避免逐词扫全文；
    · compliance_rules.pattern 为广告法正则（seed 含 lookahead 例外写法），
      构造期预编译、检查期逐条 finditer；
    · 匹配统一输出 RuleHit，阻断语义由调用方按 BLOCKING_SEVERITIES 决定（Node 只认端口）。

注意:
    · 未做大小写与全半角归一：词表出现英文/全角条目时会漏报，需在 backend 侧统一词形；
    · pattern 由 backend 单写并质检（ai-engine 对 compliance_* 零 DB 权限）：
      本层不设超时，灾难性回溯正则的防护（质检/长度上限）由 backend 入队前负责；
    · 同一片段被 word 与 regex（或多条正则）同时命中会重复上报，去重由打分层负责。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from ..ports.rule_engine import RuleEngine, RuleHit, Severity

__all__ = ["CompiledRules", "compile_rules_snapshot"]

# 快照载荷缺失/非法 severity 时的兜底：一律按 high 处理（宁可严，不可漏）。
_DEFAULT_SEVERITY: Severity = "high"

# 严重级权重：high=0 最优先；用于结果排序与「同起点同长度取更高严重级」的择优。
_SEVERITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _severity(value: Any) -> Severity:
    """防御快照载荷的非法严重级：一律按 high 处理（宁可严，不可漏）。

    参数:
        value: 快照里的 severity 原始值（JSON 反序列化结果，可能是任意类型）。
    返回:
        合法的 Severity 字面量；缺失或非法时返回 _DEFAULT_SEVERITY。
    """
    return cast(Severity, value) if value in {"high", "medium", "low"} else _DEFAULT_SEVERITY


@dataclass
class _WordMeta:
    """违禁词元数据（规则 id / 词面 / 严重级 / 来源）。

    注意:
        同一词面允许在快照中出现多条（rule_id 与 severity 不同）：同词面即同词长，
        候选按「严重级更重优先」择优，故命中 severity 必为该词面最高级，rule_id 取
        对应最高级的那一条（严重级也相同时取快照中更靠前的一条）。
    """

    rule_id: str | None
    word: str
    severity: Severity
    source: str | None = None


@dataclass
class _RegexMeta:
    """正则规则元数据（规则 id / 预编译 pattern / 严重级 / 修改建议）。"""

    rule_id: str | None
    compiled: re.Pattern[str]
    severity: Severity
    suggestion: str | None = None


class _TrieNode:
    """AC 自动机节点：子节点 + fail 指针 + depth + 命中载荷（words 下标）。

    注意:
        outputs 存放「以本节点结尾」的全部词下标，含沿 fail 链继承来的更短后缀词；
        合并顺序保证其按词长升序，故 outputs[-1] 是最长命中。但算命中起点必须用
        words[idx] 自身长度：fail 链上的后缀词与本体同尾不同起（起点更靠后）。
    """

    __slots__ = ("children", "depth", "fail", "outputs")

    def __init__(self, depth: int = 0) -> None:
        """初始化节点。

        参数:
            depth: 节点在 trie 中的深度（等于该路径字符串长度）；根节点为 0。
        """
        self.children: dict[str, _TrieNode] = {}
        self.depth = depth
        self.fail: _TrieNode | None = None
        self.outputs: list[int] = []  # 命中到本节点结尾的违禁词下标


def _build_automaton(words: list[str]) -> _TrieNode:
    """建 trie + BFS 补 fail 指针，并沿 fail 链合并 outputs。

    参数:
        words: 违禁词面列表；下标即 outputs 中存放的词下标。
    返回:
        根节点；每个词尾节点的 outputs 含自身下标，其后代沿 fail 链继承更短后缀词。
    注意:
        outputs 的词长升序由「先拼 fail 链、后接自身」保证：fail 指向严格更短的真后缀，
        因此 outputs[-1] 恒为最长命中；子节点 depth 恒为父节点 depth + 1。
    """
    root = _TrieNode()
    for idx, word in enumerate(words):
        node = root
        for ch in word:
            nxt = node.children.get(ch)
            if nxt is None:
                nxt = _TrieNode(node.depth + 1)
                node.children[ch] = nxt
            node = nxt
        node.outputs.append(idx)
    queue: deque[_TrieNode] = deque()
    for child in root.children.values():
        child.fail = root
        queue.append(child)
    while queue:
        node = queue.popleft()
        for ch, child in node.children.items():
            fail = node.fail
            while fail is not None and ch not in fail.children:
                fail = fail.fail
            child.fail = fail.children[ch] if fail is not None and ch in fail.children else root
            if child.fail is not root:
                child.outputs = child.fail.outputs + child.outputs
            queue.append(child)
    return root


def _prefer(cand: tuple[int, int, int], cur: tuple[int, int, int]) -> bool:
    """判断同起点的两个候选谁优先：词长更长 > 严重级更重 > 词下标更小。

    参数:
        cand: 候选 (词长, 严重级权重, 词下标)。
        cur: 当前已记录的 (词长, 严重级权重, 词下标)。
    返回:
        True 表示候选更优、应替换当前记录；同起点因此只保留唯一候选，结果确定可复现。
    注意:
        三个分量一律保持自然值（不做「取负编码」），避免负下标被误当作索引使用。
    """
    if cand[0] != cur[0]:
        return cand[0] > cur[0]
    if cand[1] != cur[1]:
        return cand[1] < cur[1]
    return cand[2] < cur[2]


class CompiledRules(RuleEngine):
    """把规则快照编译为内存匹配器（构造完成即可反复 check，无 IO）。

    词自动机按「位置优先取最长命中」去重叠；正则逐条 finditer。
    返回 hits 按 severity(high>medium>low) 稳定排序，供 evaluate 直接取阻断集。
    """

    def __init__(self, words: list[_WordMeta], regexes: list[_RegexMeta]) -> None:
        """编译词自动机并缓存元数据。

        参数:
            words: 违禁词元数据列表（元素顺序即词下标顺序）。
            regexes: 已预编译的正则规则元数据列表。
        注意:
            构造期完成建表（O(Σ词长)）；实例无状态，可反复 check 并跨 job 复用。
        """
        self._word_meta = words
        self._regex_meta = regexes
        self._word_len = [len(w.word) for w in words]
        self._root = _build_automaton([w.word for w in words])

    def _scan_words(self, text: str) -> list[RuleHit]:
        """单趟 AC 扫描 → 每个起点保留最长命中 → 左优先贪心去重叠。

        参数:
            text: 待检查文本。
        返回:
            word 类 RuleHit 列表；命中区间两两不重叠，keyword 必为词表内的词面。
        注意:
            1) 同起点按 (词长降序, 严重级升序, 词下标升序) 择优，结果确定可复现；
            2) 已选命中占据 [start, start+len) 后，起点落在该区间内的候选一律丢弃
               （既定重叠策略：左优先取最长，区间内的更短词会被吞掉，属有意为之）；
            3) 复杂度 O(n + k log k)，k 为去重后的候选起点数。
        """
        best: dict[int, tuple[int, int, int]] = {}  # start -> (词长, 严重级权重, 词下标)
        node = self._root
        for end, ch in enumerate(text):
            while node is not self._root and ch not in node.children:
                node = node.fail
            nxt = node.children.get(ch)
            node = nxt if nxt is not None else self._root
            for idx in node.outputs:
                length = self._word_len[idx]
                start = end - length + 1
                cand = (length, _SEVERITY_ORDER.get(self._word_meta[idx].severity, 0), idx)
                prev = best.get(start)
                if prev is None or _prefer(cand, prev):
                    best[start] = cand
        hits: list[RuleHit] = []
        cursor = 0  # 下一个允许作为命中起点的位置
        for start in sorted(best):
            if start < cursor:
                continue
            length, _, idx = best[start]
            meta = self._word_meta[idx]
            hits.append(
                RuleHit(
                    rule_id=meta.rule_id,
                    kind="word",
                    keyword=text[start : start + length],
                    severity=meta.severity,
                    reason=f"命中违禁词「{meta.word}」" + (f"（{meta.source}）" if meta.source else ""),
                )
            )
            cursor = start + length
        return hits

    def _scan_regex(self, text: str) -> list[RuleHit]:
        """逐条正则扫描文本（finditer），返回正则类命中。

        参数:
            text: 待检查文本。
        返回:
            regex 类 RuleHit 列表；未命中返回 []。
        注意:
            不跨条目去重：同一片段被多条正则命中时逐条上报（各自携带 rule_id 保留溯源），
            重复扣分由打分层按片段处理；pattern 由 backend 质检，本层不做复杂度/超时防护。
        """
        hits: list[RuleHit] = []
        for meta in self._regex_meta:
            for m in meta.compiled.finditer(text):
                hits.append(
                    RuleHit(
                        rule_id=meta.rule_id,
                        kind="regex",
                        keyword=m.group(0),
                        severity=meta.severity,
                        reason=meta.suggestion or f"命中广告法规则「{meta.compiled.pattern[:40]}」",
                    )
                )
        return hits

    def check(self, text: str) -> list[RuleHit]:
        """扫描文本返回全部确定性命中（词类 + 正则类），按严重级稳定排序。

        参数:
            text: 待检查文本；空串直接返回 []（不进入扫描循环）。
        返回:
            RuleHit 列表；同严重级内按 keyword 字典序，结果确定可复现。
        注意:
            不跨 kind 去重：同一片段被 word 与 regex 同时命中会各报一条，保留审计溯源。
        """
        if not text:
            return []
        hits = self._scan_words(text) + self._scan_regex(text)
        hits.sort(key=lambda h: (_SEVERITY_ORDER.get(h.severity, 0), h.keyword))
        return hits


def compile_rules_snapshot(payload: dict | None) -> CompiledRules | None:
    """把 backend job 载荷里的 rules 快照编译为匹配器；无快照返回 None（行为同现版本）。

    参数:
        payload: job:generate 载荷中的 rules 快照（含 words 与 rules 两个数组）。
    返回:
        CompiledRules 实例；payload 非 dict（无快照）时返回 None，调用方按「无规则」处理；
        快照存在但条目全被跳过时返回空匹配器（check 恒返回 []），语义等价于无规则命中。
    注意:
        · 坏条目防御性跳过（词面为空、正则语法错），绝不因规则数据质量问题中断生成链路；
        · severity 缺失或非法一律按 high 兜底（宁可严，不可漏）；
        · payload 由 backend 单写并质检，本函数不做正则复杂度/长度校验（无 ReDoS 防护）。
    """
    if not isinstance(payload, dict):
        return None
    words: list[_WordMeta] = []
    regexes: list[_RegexMeta] = []
    for item in payload.get("words") or []:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        words.append(
            _WordMeta(
                rule_id=str(item["id"]) if item.get("id") else None,
                word=word,
                severity=_severity(item.get("severity")),
                source=str(item["source"]) if item.get("source") else None,
            )
        )
    for item in payload.get("rules") or []:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "")
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue  # 坏正则由 backend 数据侧质检拦截，此处跳过不崩进程
        regexes.append(
            _RegexMeta(
                rule_id=str(item["id"]) if item.get("id") else None,
                compiled=compiled,
                severity=_severity(item.get("severity")),
                suggestion=str(item["suggestion"]) if item.get("suggestion") else None,
            )
        )
    return CompiledRules(words, regexes)
