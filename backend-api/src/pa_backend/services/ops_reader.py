"""运维只读面：消费心跳 / 消费组待处理（PEL） / 死信队列（DLQ） / 卡住的生成任务。

为什么 backend 要提供这一面:
    ai-engine 那边把可靠性做得很足（PEL 回收、DLQ、心跳、幂等标记），但这些能力**只有能被看见**
    才有运维价值。没有这一面时，判断「worker 是否在消费」「消息是否积压」「有没有毒消息」的唯一办法
    是登上 Redis 手工敲命令 —— 而 Redis 在容器里、生产还不暴露端口。

设计约束（**只读**）:
    · 本模块只做 ``XLEN`` / ``XINFO GROUPS`` / ``XRANGE`` / ``SCAN`` / ``TTL`` 与**只读 SELECT**
      （卡住任务清单），**不写任何键、不改任何行**；
      处置属变更操作，必须有明确 SOP —— 唯一例外是 ``POST /ops/jobs/{id}/abort``（见 ops_router）；
    · 每个命令**独立 try/except**：某个流不存在（还没被消费过）不能让整页报错 ——
      运维面板最怕「一个指标缺失就整页红」；
    · 全部要求 ``admin``：键名与消息内容属于内部运维信息。

心跳判据（重要）:
    ``pa:{env}:worker:heartbeat:{consumer}`` 是带 TTL 的键（ai-engine 默认 30s 刷新一次），
    **键过期即代表没有进程在消费**。因此「一个心跳键都没有」= 消费侧完全停滞，
    而不是「一切正常」—— 这一点必须在响应里显式表达（``stalled``），否则面板会显示成绿色。

卡住任务判据（与 ``GenerationJobReaper`` 同一口径）:
    ``generation_jobs.status ∈ (running, waiting_input)`` 且 ``updated_at`` 超过
    ``BACKEND_JOB_STALE_MINUTES``。**刻意不读** ``pa:{env}:lock:{thread_id}`` —— 那是
    worker↔worker 的内部互斥键，契约规定 backend 不得读写（``core/keys.py`` 未暴露）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings
from ..core.keys import RedisKeys
from ..core.redis_client import new_async_redis
from ..models.orm import GenerationJob, Product
from .event_envelope import decode_fields
from .generation_job_reaper import STALE_CANDIDATE_STATUSES

__all__ = ["OpsReader"]

#: 契约流（job 三类 + result），运维面板逐条显示长度与消费组状态
CONTRACT_STREAMS = ("job_generate", "job_approval", "job_product_purge", "workflow_result")

#: DLQ 单次最多回看多少条消息
DLQ_MAX_ENTRIES = 100

#: 卡住任务清单最多列出多少条（面板只为「看见并终止」，不需要全量）
STUCK_JOBS_LIMIT = 50


class OpsReader:
    """运维只读读取器（一次请求一个实例，用完 ``close``）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        session: AsyncSession | None = None,
        org_id: str | None = None,
    ) -> None:
        """初始化。

        参数:
            settings: 应用配置（读 ``env`` / ``redis_url`` / ``job_stale_minutes``）。
            session: DB 会话（只用于**只读** SELECT：卡住任务清单）。为 None 时该项返回空列表
                —— 让「只想要 Redis 指标」的调用方（与既有单测）不必依赖数据库。
            org_id: 当前租户。**只影响 DB 读取的租户过滤**（Redis 指标是环境级的，天然跨租户）；
                为 None 时数据库项返回空列表，避免「忘了传租户」变成跨租户泄露。
        """
        self._settings = settings
        self._keys = RedisKeys(settings.env)
        self._client = new_async_redis(settings)
        self._session = session
        self._org_id = org_id

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 关闭失败不影响已生成的响应
            pass

    async def overview(self) -> dict[str, Any]:
        """汇总：worker 心跳 / 契约流与消费组 / DLQ 概况 / 卡住的生成任务。

        返回:
            ``{"env", "generated_at", "workers", "stalled", "streams", "dlq", "stuck_jobs"}``。
        """
        workers = await self._workers()
        return {
            "env": self._settings.env,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "workers": workers,
            # 「一个心跳都没有」= 消费侧完全停滞（不是健康）—— 必须显式表达
            "stalled": not any(item["alive"] for item in workers),
            "streams": await self._streams(),
            "dlq": await self._dlq_summary(),
            "stuck_jobs": await self._stuck_jobs(),
        }

    async def _stuck_jobs(self) -> list[dict[str, Any]]:
        """列出超期未推进的生成任务（面板据此提供「终止」入口）。

        判据与 ``GenerationJobReaper`` 完全一致（同一常量），因此**面板里看到的就是会被回收的**。
        只读 SELECT；**必带租户过滤**（admin 也是租户内角色，不得看到别的租户的任务）——
        缺 session 或缺 org_id 时返回空列表（宁可少显示，不可越权显示）。
        """
        if self._session is None or self._org_id is None:
            return []
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=max(1, self._settings.job_stale_minutes))
        stmt = (
            select(GenerationJob, Product.sku_code, Product.status)
            .join(Product, Product.id == GenerationJob.product_id)
            .where(
                GenerationJob.org_id == self._org_id,
                GenerationJob.status.in_(STALE_CANDIDATE_STATUSES),
                GenerationJob.updated_at <= cutoff,
            )
            .order_by(GenerationJob.updated_at)
            .limit(STUCK_JOBS_LIMIT)
        )
        try:
            rows = (await self._session.execute(stmt)).all()
        except Exception as exc:  # noqa: BLE001 单指标失败不让整屏报错（与其它指标同一取舍）
            return [{"error": f"查询失败：{type(exc).__name__}"}]
        out: list[dict[str, Any]] = []
        for job, sku_code, product_status in rows:
            updated = job.updated_at
            if updated is not None and updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            out.append(
                {
                    "job_id": str(job.id),
                    "thread_id": str(job.thread_id),
                    "product_id": str(job.product_id),
                    "sku_code": sku_code,
                    "product_status": product_status,
                    "job_status": job.status,
                    "age_seconds": int((now - updated).total_seconds()) if updated else None,
                }
            )
        return out

    async def _workers(self) -> list[dict[str, Any]]:
        """列出心跳键（消费进程）及其存活状态。"""
        prefix = self._keys.heartbeat("")
        out: list[dict[str, Any]] = []
        try:
            async for key in self._client.scan_iter(match=f"{prefix}*", count=100):
                ttl = await self._client.ttl(key)
                out.append(
                    {
                        "consumer": key[len(prefix) :],
                        "key": key,
                        "ttl_seconds": int(ttl) if ttl is not None else -1,
                        "alive": bool(ttl is not None and ttl > 0),
                    }
                )
        except Exception as exc:  # noqa: BLE001 单个指标失败不让整页报错
            out.append({"error": f"scan 失败：{type(exc).__name__}"})
        return sorted(out, key=lambda item: item.get("consumer", ""))

    async def _streams(self) -> list[dict[str, Any]]:
        """逐条返回契约流的长度与消费组状态（pending 持续增长 = 消费能力不足）。"""
        out: list[dict[str, Any]] = []
        for name in CONTRACT_STREAMS:
            stream = getattr(self._keys, name)()
            item: dict[str, Any] = {"name": name, "key": stream}
            try:
                item["length"] = int(await self._client.xlen(stream))
            except Exception as exc:  # noqa: BLE001
                item["length"] = None
                item["error"] = f"xlen 失败：{type(exc).__name__}"
            try:
                groups = await self._client.xinfo_groups(stream)
                item["groups"] = [
                    {
                        "name": group.get("name"),
                        "consumers": group.get("consumers"),
                        "pending": group.get("pending"),
                        "last_delivered_id": group.get("last-delivered-id"),
                        "lag": group.get("lag"),
                    }
                    for group in groups
                ]
            except Exception:  # noqa: BLE001 流不存在 / 未建组 → 正常状态，不视作错误
                item["groups"] = []
            out.append(item)
        return out

    async def _dlq_summary(self) -> list[dict[str, Any]]:
        """列出死信流的消息数（``pa:{env}:dlq:*``）。"""
        prefix = self._keys.dlq("")
        out: list[dict[str, Any]] = []
        try:
            async for key in self._client.scan_iter(match=f"{prefix}*", count=100):
                out.append(
                    {"domain": key[len(prefix) :], "key": key, "length": int(await self._client.xlen(key))}
                )
        except Exception as exc:  # noqa: BLE001
            out.append({"error": f"scan 失败：{type(exc).__name__}"})
        return sorted(out, key=lambda item: item.get("domain", ""))

    async def dlq_entries(self, domain: str, *, limit: int = 20) -> dict[str, Any]:
        """回看某个死信流里的消息（默认最新在前）。

        参数:
            domain: 死信域（如 ``job:generate``；对应键 ``pa:{env}:dlq:job:generate``）。
            limit: 条数上限（<= ``DLQ_MAX_ENTRIES``）。
        返回:
            ``{"domain", "key", "length", "entries": [{"seq", "payload"}]}``。
        注意:
            只读：**不做 ack / 删除 / 重投** —— 重投必须先确认「为什么它成了毒消息」
            （根因多半是商品数据或外部依赖问题，盲目重投只会再造一条毒消息）。
        """
        stream = self._keys.dlq(domain)
        length = int(await self._client.xlen(stream))
        raw = await self._client.xrevrange(
            stream, max="+", min="-", count=min(max(1, limit), DLQ_MAX_ENTRIES)
        )
        entries: list[dict[str, Any]] = []
        for message_id, fields in raw:
            entry: dict[str, Any] = {"seq": message_id}
            try:
                entry["payload"] = decode_fields(fields)
            except Exception as exc:  # noqa: BLE001 脏消息在 DLQ 里出现属正常
                entry["payload"] = None
                entry["decode_error"] = f"{type(exc).__name__}: {exc}"
            entries.append(entry)
        return {"domain": domain, "key": stream, "length": length, "entries": entries}
