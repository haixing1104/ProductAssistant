"""backend-api 日志装配：给 root logger 装上 handler（修掉「访问日志被丢弃」+「无时间戳」）。

问题背景（2026-09 实测）:
    uvicorn 只配置它自己的 ``uvicorn.*`` logger，**root logger 没有任何 handler**；
    而 Python 的 ``logging.lastResort`` 只放行 **WARNING 及以上**。
    后果：``middleware/observability`` 里 ``pa_backend.access`` 的 **INFO 访问日志一条都没输出**
    —— 实测 ``grep -c 'request_id=' /tmp/padev/backend.log`` = 0，
    也就是「用一个 request_id 串起一次 HTTP 请求所有日志」的能力**实际不可用**；
    同时全部日志都没有时间戳，跨进程时序只能靠 Redis 消息 ID / DB 时间反推。

做法:
    在应用工厂（``create_app``）最早处调用 ``configure_logging(settings)``：
    ``logging.basicConfig`` 给 root 装一个 stdout handler，格式 = 时间 + 级别 + logger 名 + 消息。
    · **不 force**：pytest 等已装 handler 的环境不会被清空（幂等、不抢管）；
    · uvicorn 的 logger 自带 handler 且 ``propagate=False`` → 不会重复打印；
    · 级别由 ``BACKEND_LOG_LEVEL`` 控制（登记在 ``infra/.env.template`` + ``env-check.sh``）。

为什么输出到 stdout 而不是文件:
    进程日志归编排层（容器 stdout / systemd journal / ``scripts/dev-up.sh`` 的 tee + 轮转）。
    应用自己写文件会引出「谁负责切割」「多副本写同一文件」这些不属于应用的问题。
"""

from __future__ import annotations

import logging
import sys

from .config import Settings

__all__ = ["configure_logging", "LOG_TIME_FORMAT"]

#: 时间戳格式（与 ai-engine 的 ``logging_setup.LOG_TIME_FORMAT`` 一致，两份日志可对齐看）
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(settings: Settings) -> None:
    """按配置装配 root logger（幂等；已装 handler 时不覆盖）。

    参数:
        settings: 应用配置（读 ``log_level``，见 ``BACKEND_LOG_LEVEL``）。
    """
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt=LOG_TIME_FORMAT,
        stream=sys.stdout,
    )
