"""路由注册表：``main.create_app`` 只认这一个 ``ALL_ROUTERS``。

为什么集中在这里（而不是在 main 里逐个 import）:
    · 新增路由组只改一处，main.py 保持稳定；
    · 顺序可见：auth → users → products → contents → evaluations → oss → approvals，
      便于人肉核对「哪些能力对外暴露了」。

阶段进度:
    P1：空列表（只有 ``/healthz`` ``/readyz`` 探针，直接挂在 main 上）；
    P2：auth / users；
    P3：products / contents / evaluations / oss；
    P4：approvals（并把 result 消费器 / 通知投递器 / 补投守护挂进 lifespan）；
    P5：stream（SSE：票据 + 回放 + 尾随）；
    P6（当前）：compliance（词库/规则 CRUD + 快照预览）/ ops（心跳·PEL·DLQ 只读）。
"""

from __future__ import annotations

from fastapi import APIRouter

from .approvals_router import router as approvals_router
from .auth_router import router as auth_router
from .compliance_router import router as compliance_router
from .contents_router import router as contents_router
from .evaluations_router import router as evaluations_router
from .members_router import router as members_router
from .ops_router import router as ops_router
from .oss_router import router as oss_router
from .products_router import router as products_router
from .stream_router import router as stream_router

__all__ = ["ALL_ROUTERS"]

ALL_ROUTERS: list[APIRouter] = [
    auth_router,
    members_router,
    products_router,
    contents_router,
    evaluations_router,
    oss_router,
    approvals_router,
    stream_router,
    compliance_router,
    ops_router,
]


