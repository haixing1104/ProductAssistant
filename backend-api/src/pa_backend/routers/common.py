"""路由层公共助手（统一响应封装 + 路径参数/归属校验）。

为什么把「id 解析」和「归属校验」放这里:
    多个路由组都要做「把路径里的 ``{product_id}`` 变成 UUID 并确认它属于当前租户」。
    各写各的就一定会出现差异（有的返回 403、有的返回 404、有的泄露实体是否存在）。
    这里统一：**非法格式 = 不存在 = 越权，一律 404**，不给攻击者任何探测信号。

安全口径（与 ProductPilot 一致，属本仓库的红线）:
    · 查询一律带 ``org_id``（``repositories`` 层强制，见 ``base.OrgScopedRepo``）；
    · 「不存在」与「不属于本租户」返回**同一个** 404 与同一句文案。
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status

from ..core.errors import ok  # noqa: F401  (对外暴露给各路由使用，避免两处定义)

__all__ = ["NOT_FOUND_DETAIL", "ok", "to_uuid"]

#: 统一 404 文案（不区分「不存在」与「越权」，见模块 docstring）
NOT_FOUND_DETAIL = "资源不存在或不属于当前租户"


def to_uuid(raw: str, *, detail: str | None = None) -> uuid.UUID:
    """把路径参数转成 UUID；格式非法一律 404。

    参数:
        raw: 路径里的原始字符串。
        detail: 自定义 404 文案（缺省用统一文案）。
    返回:
        ``uuid.UUID``。
    异常:
        HTTPException: 404（格式非法）。
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail or NOT_FOUND_DETAIL) from exc
