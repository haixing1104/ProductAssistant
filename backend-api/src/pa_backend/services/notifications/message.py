"""通知消息构造：把「待审单 + 商品 + AI 快照」压成渠道无关的载荷。

为什么载荷与渠道解耦:
    outbox 里存的是**渠道无关的语义载荷**，各渠道 sender 自己决定怎么排版。
    若在写库时就把 markdown 拼死，将来加飞书卡片/邮件模板就得回头改历史数据。

摘要口径（面向审批人的最小信息量）:
    · 谁（商品标题/SKU）· 为什么（``reason``：高价 or 质量重试耗尽）
    · 评分与违规点数量（让审批人先判断严不严重）
    · 正文节选（太长会淹没重点，故截断到 ``_EXCERPT_LIMIT``）
    · 「去审批」深链（一次性签名票据，见 ``core/security.create_approval_ticket``）
"""

from __future__ import annotations

from typing import Any

from ...core.config import Settings
from ...core.security import create_approval_ticket

__all__ = ["EXCERPT_LIMIT", "build_approval_message"]

#: 正文节选长度（超出截断，避免通知过长反而没人看）
EXCERPT_LIMIT = 200

#: 转人工原因的可读文案
REASON_LABELS = {
    "high_value": "高价商品需人工放行",
    "quality_exhausted": "自动重试已用尽，质量仍不达标",
}


def _excerpt(snapshot: dict[str, Any] | None) -> str:
    """从 content_snapshot 里抽取纯文本节选（只取 text block）。"""
    blocks = ((snapshot or {}).get("content") or {}).get("blocks") or []
    text = "".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
    text = text.strip()
    if len(text) <= EXCERPT_LIMIT:
        return text
    return text[:EXCERPT_LIMIT] + "…"


def build_approval_message(
    *,
    product,
    approval,
    settings: Settings,
) -> dict[str, Any]:
    """构造渠道无关的通知载荷（写入 outbox 的 ``payload``）。

    参数:
        product: 商品 ORM 实例（读 title/sku_code/id）。
        approval: 待审单 ORM 实例（读 id/thread_id/content_snapshot/expire_at）。
        settings: 应用配置（读前端入口，用于拼深链）。
    返回:
        载荷 dict：``title`` / ``sku_code`` / ``product_id`` / ``approval_id`` / ``thread_id`` /
        ``reason`` / ``reason_label`` / ``score`` / ``violation_count`` / ``excerpt`` /
        ``deep_link`` / ``expires_at``。
    """
    snapshot = approval.content_snapshot or {}
    evaluation = snapshot.get("evaluation_result") or {}
    errors = evaluation.get("errors") or []
    reason = str(snapshot.get("reason") or "unknown")
    ticket = create_approval_ticket(
        approval_id=str(approval.id),
        org_id=str(approval.org_id),
        product_id=str(approval.product_id),
        settings=settings,
    )
    base = settings.frontend_public_base_url.rstrip("/")
    return {
        "title": product.title,
        "sku_code": product.sku_code,
        "product_id": str(product.id),
        "approval_id": str(approval.id),
        "thread_id": str(approval.thread_id),
        "reason": reason,
        "reason_label": REASON_LABELS.get(reason, "需要人工审批"),
        "score": evaluation.get("score"),
        "violation_count": len(errors) if isinstance(errors, list) else 0,
        "evaluation_attempts": snapshot.get("evaluation_attempts"),
        "excerpt": _excerpt(snapshot),
        "deep_link": f"{base}/approvals/{approval.id}?ticket={ticket}",
        "expires_at": approval.expire_at.isoformat() if approval.expire_at else None,
    }
