"""运维只读面：消费心跳 / 消费组待处理（PEL） / 死信队列（DLQ）。

为什么 backend 要提供这一面:
    ai-engine 那边把可靠性做得很足（PEL 回收、DLQ、心跳、幂等标记），但这些能力**只有能被看见**
    才有运维价值。没有这一面时，判断「worker 是否在消费」「消息是否积压」「有没有毒消息」的唯一办法
    是登上 Redis 手工敲命令 —— 而 Redis 在容器里、生产还不暴露端口。

设计约束（**只读**）:
    · 本模块只做 ``XLEN`` / ``XINFO GROUPS`` / ``XRANGE`` / ``SCAN`` / ``TTL``，**不写任何键**；
      重投/清理属变更操作，必须有明确 SOP（见 README），不在接口里「顺手」提供；
    · 每个命令**独立 try/except**：某个流不存在（还没被消费过）不能让整页报错 ——
      运维面板最怕「一个指标缺失就整页红」；
    · 全部要求 ``admin``：键名与消息内容属于内部运维信息。

心跳判据（重要）:
    ``pa:{env}:worker:heartbeat:{consumer}`` 是带 TTL 的键（ai-engine 默认 30s 刷新一次），
    **键过期即代表没有进程在消费**。因此「一个心跳键都没有」= 消费侧完全停滞，
    而不是「一切正常」—— 这一点必须在响应里显式表达（``stalled``），否则面板会显示成绿色。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.config import Settings
from ..core.keys import RedisKeys
from ..core.redis_client import new_async_redis
from .event_envelope import decode_fields

__all__ = ["OpsReader"]

#: 契约流（job 三类 + result），运维面板逐条显示长度与消费组状态
CONTRACT_STREAMS = ("job_generate", "job_approval", "job_product_purge", "workflow_result")

#: DLQ 单次最多回看多少条消息
DLQ_MAX_ENTRIES = 100


class OpsReader:
    """运维只读读取器（一次请求一个实例，用完 ``close``）。"""

    def __init__(self, settings: Settings) -> None:
        """初始化。

        参数:
            settings: 应用配置（读 ``env`` / ``redis_url``）。
        """
        self._settings = settings
        self._keys = RedisKeys(settings.env)
        self._client = new_async_redis(settings)

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 关闭失败不影响已生成的响应
            pass

    async def overview(self) -> dict[str, Any]:
        """汇总：worker 心跳 / 契约流与消费组 / DLQ 概况。

        返回:
            ``{"env", "generated_at", "workers", "stalled", "streams", "dlq"}``。
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
        }

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
