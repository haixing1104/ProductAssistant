"""ai-engine 的日志装配：给日志加**时间戳**，并让 ``logging`` 真正输出。

问题背景（2026-09 实测）:
    ai-engine 的日志全走 ``print(f"[worker] …")``，**没有时间戳**；而 ``logging.getLogger``
    打出的行在没有任何 handler 时只放行 WARNING（``logging.lastResort``）。
    结果是跨进程排查（backend → Redis → ai-engine）只能靠 Redis 消息 ID / DB 时间反推时序 ——
    这次修「生成卡住」就是这么绕出来的，代价很高。

做法（保持既有 ``[worker] …`` 前缀口径不变，grep 习惯不被破坏）:
    · ``log(msg)`` = 带时间戳的 print（``flush=True`` 保证逐行实时，配合 dev-up 的 tee）；
    · ``configure_logging()`` = ``logging.basicConfig``，给 ``workflowcore`` 里那些
      ``logging.getLogger(__name__)`` 的行装上 handler（否则 WARNING 以下静默丢失）。

为什么输出到 stdout 而不是文件:
    进程日志归编排层（容器 stdout / dev-up.sh 的 tee + 轮转）；应用自己写文件会引出
    「谁负责切割」「多副本写同一文件」这类不属于应用的问题。
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

__all__ = ["configure_logging", "log", "LOG_TIME_FORMAT"]

#: 时间戳格式（与 backend 的 ``basicConfig(datefmt=…)`` 保持一致，便于两份日志对齐看）
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def log(message: str) -> None:
    """带时间戳输出一行日志（保持调用方自带的 ``[worker] …`` 前缀）。

    参数:
        message: 日志正文（调用方负责前缀口径）。
    """
    print(f"{datetime.now().strftime(LOG_TIME_FORMAT)} {message}", flush=True)


def configure_logging(level: str | int | None = None) -> None:
    """给 root logger 装上 handler（幂等；不覆盖既有 handler）。

    参数:
        level: 级别（字符串或 int）；None → 读 ``AI_ENGINE_LOG_LEVEL``，缺省 INFO。
    注意:
        ``force=False``：pytest 等已配置 handler 的环境不会被清空（幂等、不抢管）。
    """
    resolved = level if level is not None else os.getenv("AI_ENGINE_LOG_LEVEL", "INFO")
    numeric = (
        resolved
        if isinstance(resolved, int)
        else getattr(logging, str(resolved).upper(), logging.INFO)
    )
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt=LOG_TIME_FORMAT,
        stream=sys.stdout,
    )
