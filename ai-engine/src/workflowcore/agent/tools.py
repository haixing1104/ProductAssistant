"""tools：Agent 业务工具集（全部只读；多租户字段由闭包固化，绝不交给模型）。

数据来源与权限（逐条对齐 database/sql/0002_roles_grants.sql）:
    · get_product_facts        → BusinessReader.read_product         （products，仅 SELECT）
    · get_content_history      → BusinessReader.list_content_versions（schema_pa_ai，全 DML）
    · get_eval_history         → BusinessReader.list_eval_logs       （schema_pa_ai，全 DML）
    · get_approval_history     → BusinessReader.list_approvals       （hitl_approvals，仅 SELECT）
    · scan_compliance          → RuleEngine.check                    （规则快照：来自 job:generate 载荷，
                                                                        compliance_* 表对 role_pa_ai 无授权）
    · retrieve_similar_copy    → RAGStore.retrieve                   （Milvus，未配置时整体不注册该工具）

多租户红线:
    org_id / product_id 一律取自图状态（由 node_agent 注入闭包），
    **不进入** 模型可见的参数 schema：模型即使想查别的租户也无处下参数
    （工具声明里根本没有 org_id 字段）。

为什么工具在注册期按端口有无裁剪:
    Milvus / 规则快照都可能缺失（未配置 RAG、老消息无 rules）。缺失即不注册该工具，
    模型只会看到「真的能读到数据」的工具，不会出现「调了但永远返回空」的幻觉入口。
"""

from __future__ import annotations

from typing import Any

from ...ports import BLOCKING_SEVERITIES, BusinessReader, RAGStore, RuleEngine
from .registry import ToolRegistry, build_tool

__all__ = ["build_business_tools", "build_registry"]

#: 单次 scan_compliance 的最大扫描字符数（防止把整篇超长文塞进工具与提示词）
MAX_COMPLIANCE_SCAN_CHARS = 4000


def _bounded_int(value: Any, default: int, *, low: int = 1, high: int = 10) -> int:
    """把模型给的 limit 参数收敛到安全区间（脏参数不抛异常）。

    参数:
        value: 模型给的原始值（可能是字符串 / 负数 / 缺失）。
        default: 无法解析或缺失时的默认值。
        low: 允许的最小值（含）。
        high: 允许的最大值（含），防止模型要求超大 limit 拖垮查询。
    返回:
        位于 [low, high] 的整数；解析失败时返回 default 收敛后的值。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def build_business_tools(
    *,
    org_id: str,
    product_id: str,
    reader: BusinessReader | None = None,
    rule_engine: RuleEngine | None = None,
    rag_store: RAGStore | None = None,
) -> list:
    """按当前线程（org_id + product_id）装配只读工具列表。

    参数:
        org_id: 组织 ID（多租户隔离，闭包固化）。
        product_id: 商品 ID（当前生成任务的目标商品，闭包固化）。
        reader: 业务只读端口；None 表示数据库侧取数工具全部不注册。
        rule_engine: 合规规则引擎（来自 job 载荷规则快照）；None 时 scan_compliance 不注册。
        rag_store: RAG 向量库端口；None 时 retrieve_similar_copy 不注册。
    返回:
        Tool 列表（顺序稳定，便于测试与审计）；所有工具**只读**。
    """
    tools: list = []

    if reader is not None:
        def _product_facts(_arguments: dict[str, Any]) -> Any:
            """读取当前商品的真实素材（标题 / 价格 / 库存 / 状态 / 图片）。

            参数:
                _arguments: 模型给出的参数（本工具无参数；多租户字段由闭包固化）。
            返回:
                商品字段 dict；未命中时返回 {"found": False}（让模型明确「查不到」而非编造）。
            """
            product = reader.read_product(org_id=org_id, product_id=product_id)
            return product if product is not None else {"found": False}

        def _content_history(arguments: dict[str, Any]) -> Any:
            """读取本商品历史文案版本（新→旧，含评分/版本/节选）。

            参数:
                arguments: 模型给出的参数；支持 limit（返回条数，1-10）。
            返回:
                {"items": [...], "count": n}；无版本时 items 为空列表。
            """
            limit = _bounded_int(arguments.get("limit"), 3, low=1, high=10)
            items = reader.list_content_versions(org_id=org_id, product_id=product_id, limit=limit)
            return {"items": items, "count": len(items)}

        def _eval_history(arguments: dict[str, Any]) -> Any:
            """读取本商品历史评估记录（评估方式 / 分数 / 违规原因 / rule_id）。

            参数:
                arguments: 模型给出的参数；支持 limit（返回条数，1-10）。
            返回:
                {"items": [...], "count": n}；无记录时 items 为空列表。
            """
            limit = _bounded_int(arguments.get("limit"), 5, low=1, high=10)
            items = reader.list_eval_logs(org_id=org_id, product_id=product_id, limit=limit)
            return {"items": items, "count": len(items)}

        def _approval_history(arguments: dict[str, Any]) -> Any:
            """读取本商品历史人工审批记录（结论 / 渠道 / 驳回意见）。

            参数:
                arguments: 模型给出的参数；支持 limit（返回条数，1-10）。
            返回:
                {"items": [...], "count": n}；无记录时 items 为空列表。
            """
            limit = _bounded_int(arguments.get("limit"), 5, low=1, high=10)
            items = reader.list_approvals(org_id=org_id, product_id=product_id, limit=limit)
            return {"items": items, "count": len(items)}

        tools.extend(
            [
                build_tool(
                    name="get_product_facts",
                    description="查询本商品的真实基础资料（标题/价格/库存状态/上架状态/原始图片）。所有事实陈述必须以此为准。",
                    parameters={"type": "object", "properties": {}},
                    handler=_product_facts,
                ),
                build_tool(
                    name="get_content_history",
                    description="查询本商品历史已生成的文案版本（含是否批准、模型名、文本节选），用于避免重复沿用被否决的写法。",
                    parameters={
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "description": "返回条数，1-10，默认 3"}},
                    },
                    handler=_content_history,
                ),
                build_tool(
                    name="get_eval_history",
                    description="查询本商品历史评估记录（分数与违规原因、命中的规则 id），用于规避已发生过的违规点。",
                    parameters={
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "description": "返回条数，1-10，默认 5"}},
                    },
                    handler=_eval_history,
                ),
                build_tool(
                    name="get_approval_history",
                    description="查询本商品历史人工审批结论与驳回意见，用于满足审批人反复提出的要求。",
                    parameters={
                        "type": "object",
                        "properties": {"limit": {"type": "integer", "description": "返回条数，1-10，默认 5"}},
                    },
                    handler=_approval_history,
                ),
            ]
        )

    if rule_engine is not None:
        def _scan_compliance(arguments: dict[str, Any]) -> Any:
            """对本线程草稿文本做确定性合规自检（命中违禁词 / 极限词正则）。

            参数:
                arguments: 模型给出的参数；必须含 text（待检查文本）。
            返回:
                {"hits": [...], "blocking": bool, "scanned_chars": n}；
                text 缺失或为空时返回 {"hits": [], "blocking": false, "scanned_chars": 0}。
            注意:
                规则数据来自 backend 随 job:generate 投递的规则快照（compliance_* 表对
                role_pa_ai 无授权，见模块 docstring）；本工具只做「自查提示」，
                最终裁决仍由 evaluate 节点 + RuleEngine 完成。
            """
            text = str(arguments.get("text") or "").strip()[:MAX_COMPLIANCE_SCAN_CHARS]
            if not text:
                return {"hits": [], "blocking": False, "scanned_chars": 0}
            hits = rule_engine.check(text)
            return {
                "hits": [
                    {
                        "keyword": hit.keyword,
                        "severity": hit.severity,
                        "reason": hit.reason,
                        "rule_id": hit.rule_id,
                    }
                    for hit in hits
                ],
                "blocking": any(hit.severity in BLOCKING_SEVERITIES for hit in hits),
                "scanned_chars": len(text),
            }

        tools.append(
            build_tool(
                name="scan_compliance",
                description="对给定文本做违禁词/极限词自检，返回命中词与修改建议（用于动笔前自查，不能替代最终评估）。",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "待检查的文案文本"}},
                    "required": ["text"],
                },
                handler=_scan_compliance,
            )
        )

    if rag_store is not None:
        def _retrieve_similar(arguments: dict[str, Any]) -> Any:
            """按语义召回同租户历史高转化文案片段（Few-Shot 参考）。

            参数:
                arguments: 模型给出的参数；支持 query（检索文本）与 top_k（条数 1-5）。
            返回:
                {"snippets": [...], "count": n}；无命中时 snippets 为空列表。
            注意:
                检索由端口实现强制附加 org_id 过滤；未配置 Milvus 时本工具不会注册。
            """
            query = str(arguments.get("query") or "").strip()
            top_k = _bounded_int(arguments.get("top_k"), 3, low=1, high=5)
            if not query:
                return {"snippets": [], "count": 0}
            snippets = rag_store.retrieve(
                org_id=org_id, product_id=product_id, query=query, top_k=top_k
            )
            return {"snippets": list(snippets or []), "count": len(snippets or [])}

        tools.append(
            build_tool(
                name="retrieve_similar_copy",
                description="按语义检索同租户历史高转化文案片段，作为写法参考（不保证与本品类完全一致）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索文本，通常是商品标题或卖点"},
                        "top_k": {"type": "integer", "description": "返回条数，1-5，默认 3"},
                    },
                    "required": ["query"],
                },
                handler=_retrieve_similar,
            )
        )

    return tools


def build_registry(*, org_id: str, product_id: str, reader: BusinessReader | None = None,
                   rule_engine: RuleEngine | None = None, rag_store: RAGStore | None = None) -> ToolRegistry:
    """装配工具注册表（供 AgentLoop 使用）。

    参数:
        org_id: 组织 ID（多租户隔离，闭包固化）。
        product_id: 商品 ID（当前任务目标商品）。
        reader: 业务只读端口；None 时不注册数据库取数工具。
        rule_engine: 合规规则引擎（job 载荷规则快照）；None 时不注册 scan_compliance。
        rag_store: RAG 向量库端口；None 时不注册 retrieve_similar_copy。
    返回:
        ToolRegistry；一个工具都没装配上时为空注册表（此时 Agent 退化为纯对话，不产生工具调用）。
    """
    return ToolRegistry(
        build_business_tools(
            org_id=org_id,
            product_id=product_id,
            reader=reader,
            rule_engine=rule_engine,
            rag_store=rag_store,
        )
    )
