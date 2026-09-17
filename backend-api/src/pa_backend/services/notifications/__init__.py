"""审批通知：消息构造 / 渠道适配 / 出站解析 / outbox 写入 / 投递器 / 投递状态只读视图。

设计要点（为什么不是「建完审批单直接发一条 HTTP」）:
    · **可靠性**：外部 HTTP 必然抖动。直接发失败就等于「审批人永远不知道有单要审」，
      而审批是流程的卡点。所以走 **Transactional Outbox**：与 ``hitl_approvals`` **同事务**
      写入 ``notification_outbox``，再由独立投递器带重试/退避/DLQ 消费；
    · **多渠道同构**：``channel`` 只是 outbox 的一列，加渠道 = 加一个 sender，
      写入侧与投递侧都不用改（PA 当前只实装钉钉，飞书/邮件留位见 BACKEND_NOTIFY 说明）；
    · **模式分离**：``NOTIFY_MODE=console`` 只打印（本地全链路可跑通），``live`` 才真实出站，
      且**只有配了凭据的渠道**才会被解析出来 —— 避免「配置半成品」在运行期报一堆错；
    · **可观测**：``reader`` 提供投递状态的只读视图（审批中心直接展示「通知发出去没有」）——
      没有这一面时，「单子建了但没人收到通知」在界面上看不出任何区别。
"""

from __future__ import annotations

from .adapters import ConsoleSender, DingTalkSender, NotificationSendError, Sender
from .deliverer import OutboxDeliverer
from .reader import notifications_by_approval
from .resolver import build_senders, resolve_channels
from .writer import enqueue_pending_notifications

__all__ = [
    "ConsoleSender",
    "DingTalkSender",
    "NotificationSendError",
    "OutboxDeliverer",
    "Sender",
    "build_senders",
    "enqueue_pending_notifications",
    "notifications_by_approval",
    "resolve_channels",
]
