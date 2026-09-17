"""渠道解析：决定「哪些渠道要发」与「用哪个 sender 发」。

两种模式（对应 ``NOTIFY_MODE``）:
    · ``console``（默认）：**总是**返回 ``("dingtalk",)``（用控制台 sender 模拟投递）。
      这样本地开发不走真实 webhook 也能把「建单 → outbox → 投递 → sent」全链路跑通并验证；
    · ``live``：只返回**配了凭据**的渠道（钉钉需要 ``NOTIFY_DINGTALK_WEBHOOK_URL``）。
      没有凭据就返回空 —— 宁可「不出站」，也不要用半配置去撞运行时错误。

组合方式:
    ``resolve_channels`` 决定写 outbox 的渠道集合；``build_senders`` 决定每个渠道怎么发。
    两者都只依赖 ``Settings``，便于测试用一份假配置覆盖。
"""

from __future__ import annotations

from ...core.config import Settings
from .adapters import ConsoleSender, DingTalkSender, Sender

__all__ = ["build_senders", "is_live_mode", "resolve_channels"]

#: 渠道名（与 DB 的 ``notification_outbox.channel`` 取值一致）
CHANNEL_DINGTALK = "dingtalk"


def is_live_mode(settings: Settings) -> bool:
    """是否真实出站（``NOTIFY_MODE=live``，大小写与空白容错）。"""
    return settings.notify_mode.strip().lower() == "live"


def resolve_channels(settings: Settings) -> tuple[str, ...]:
    """返回本次运行需要写 outbox 的渠道集合。

    参数:
        settings: 应用配置（读 ``notify_mode`` / ``notify_dingtalk_webhook_url``）。
    返回:
        渠道名元组（可能为空 = 不出站）。
    """
    if is_live_mode(settings):
        return (CHANNEL_DINGTALK,) if settings.notify_dingtalk_webhook_url.strip() else ()
    return (CHANNEL_DINGTALK,)


def build_senders(settings: Settings, channels: tuple[str, ...]) -> dict[str, Sender]:
    """为每个渠道构造 sender。

    参数:
        settings: 应用配置。
        channels: ``resolve_channels`` 的结果。
    返回:
        ``{渠道名: Sender}``；``live`` 模式下缺凭据的渠道不会出现在结果里
        （投递器遇到「有渠道名但没 sender」会判定 DLQ 并告警，见 ``deliverer``）。
    """
    if not is_live_mode(settings):
        return {channel: ConsoleSender(prefix=channel) for channel in channels}
    senders: dict[str, Sender] = {}
    webhook = settings.notify_dingtalk_webhook_url.strip()
    if CHANNEL_DINGTALK in channels and webhook:
        senders[CHANNEL_DINGTALK] = DingTalkSender(webhook)
    return senders
