"""渠道适配器：把语义载荷发出去（钉钉 webhook / 控制台）。

协议约定:
    ``Sender.send(payload) -> provider_msg_id``；失败必须抛 ``NotificationSendError``
    （由投递器决定重试/退避/DLQ）—— 适配器不自行吞错，否则「通知没发出去」会变成静默失败。

为什么只实装钉钉:
    ``infra/.env.template`` 当前只登记了 ``NOTIFY_DINGTALK_WEBHOOK_URL``（README 也写明
    「当前只实现了钉钉」）。加飞书/邮件时只需在此新增一个 sender 并在 ``resolver`` 里挂上，
    写入侧与投递器**一行都不用改** —— 这正是 outbox 的价值。

钉钉细节（踩过的点）:
    · 必须用 ``markdown`` 消息类型才有排版；``title`` 用于会话列表预览；
    · 返回体是 ``{"errcode":0,"errmsg":"ok"}`` —— HTTP 200 也可能是业务失败（如关键词不合规、
      被限流），所以要看 ``errcode``，只看状态码会把失败当成功。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

__all__ = ["ConsoleSender", "DingTalkSender", "NotificationSendError", "Sender"]

logger = logging.getLogger(__name__)

#: webhook 调用超时（秒）：投递器有重试，单次调用不允许长挂
_SEND_TIMEOUT_SECONDS = 10.0


class NotificationSendError(RuntimeError):
    """投递失败（网络/HTTP/对方业务错误）。由投递器决定重试策略。"""


class Sender(Protocol):
    """渠道 sender 协议（鸭子类型即可，无需继承）。"""

    def send(self, payload: dict[str, Any]) -> str | None:
        """发送一条通知。

        参数:
            payload: 语义载荷（见 ``message.build_approval_message``）。
        返回:
            ``provider_msg_id``（对方回执标识；没有则 None）。
        异常:
            NotificationSendError: 发送失败。
        """
        ...


class ConsoleSender:
    """控制台 sender：只打印（``NOTIFY_MODE=console``），本地全链路可跑通。"""

    def __init__(self, *, prefix: str = "approval") -> None:
        """初始化。

        参数:
            prefix: 打印前缀（便于在日志里区分）。
        """
        self._prefix = prefix

    def send(self, payload: dict[str, Any]) -> str | None:
        """打印通知摘要（不发起任何网络请求）。"""
        print(
            f"[notify:{self._prefix}] {payload.get('reason_label')} | SKU={payload.get('sku_code')} "
            f"| 商品={payload.get('title')} | 评分={payload.get('score')} "
            f"| 违规点={payload.get('violation_count')} | 去审批={payload.get('deep_link')}"
        )
        return f"console-{payload.get('approval_id')}"


class DingTalkSender:
    """钉钉群机器人 webhook sender。"""

    def __init__(self, webhook_url: str) -> None:
        """初始化。

        参数:
            webhook_url: 群机器人 webhook 地址（含 access_token）。
        """
        self._webhook_url = webhook_url

    def _markdown(self, payload: dict[str, Any]) -> tuple[str, str]:
        """拼 markdown 标题与正文。"""
        title = f"待审批：{payload.get('title')}"
        lines = [
            f"### {payload.get('reason_label')}",
            f"- 商品：{payload.get('title')}（SKU `{payload.get('sku_code')}`）",
        ]
        if payload.get("score") is not None:
            lines.append(f"- AI 评分：{payload.get('score')}（违规点 {payload.get('violation_count')} 个）")
        if payload.get("excerpt"):
            lines.append(f"- 正文节选：{payload.get('excerpt')}")
        lines.append(f"- [去审批]({payload.get('deep_link')})")
        return title, "\n".join(lines)

    def send(self, payload: dict[str, Any]) -> str | None:
        """POST 到钉钉 webhook。

        参数:
            payload: 语义载荷。
        返回:
            ``provider_msg_id``（钉钉成功时通常不返回消息 ID，故为 None）。
        异常:
            NotificationSendError: 网络异常 / HTTP 非 2xx / ``errcode != 0``。
        """
        title, text = self._markdown(payload)
        body = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        try:
            response = httpx.post(self._webhook_url, json=body, timeout=_SEND_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:  # 连接/超时/DNS
            raise NotificationSendError(f"钉钉投递网络异常：{exc!r}") from exc
        if response.status_code >= 400:
            raise NotificationSendError(f"钉钉投递 HTTP {response.status_code}: {response.text[:200]}")
        try:
            result = response.json()
        except ValueError as exc:  # 非 JSON 响应（网关页面等）
            raise NotificationSendError(f"钉钉响应不是 JSON：{response.text[:200]}") from exc
        errcode = result.get("errcode")
        if errcode not in (0, None):
            raise NotificationSendError(f"钉钉业务错误 errcode={errcode} errmsg={result.get('errmsg')!r}")
        return result.get("provider_msg_id")
