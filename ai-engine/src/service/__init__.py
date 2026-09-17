"""service：入站进程层（消费 Redis Streams 任务、驱动/恢复 ListingWorkflow）。

对外导出:
    ListingWorker：消费 job:* 并按 thread_id 驱动/恢复图。
运行:
    cd ai-engine && python -m src.service
"""

from .worker import ListingWorker

__all__ = ["ListingWorker"]
