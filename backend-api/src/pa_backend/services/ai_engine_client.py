"""backend ⇄ ai-engine 的唯一业务通道（Redis Streams）。

三条出站（backend → ai-engine，全部经 ``core/keys.py`` 的契约键）:
    · ``job:generate``        ``{thread_id, product_id, org_id, rules}``
    · ``job:approval``        ``{thread_id, product_id, org_id, result, feedback}``
    · ``job:product_purge``   ``{product_id, org_id}``

一条入站（ai-engine → backend）:
    · ``result:workflow``     由 ``WorkflowResultConsumer`` 消费（P4）；
      本类只提供 ``read_thread_events`` 给 SSE（P5）与测试用。

时序红线（``purge_product`` 最典型）:
    **必须在数据库 commit 之后再 XADD**。理由：ai-engine 的 ``_handle_purge`` 有一个前置守卫 ——
    若 ``products`` 行仍存在（= 删除还没生效），它会**中止清理并 ack 丢弃**这条消息。
    所以「先投递后提交」会让 purge 永久丢失（商品行删了、AI 侧数据却留着）。

幂等语义（读法上要正确）:
    ai-engine 侧重复投递会返回 ``duplicate``（终态幂等标记）或 ``busy``（线程锁未释放）——
    这些是**正常返回**，不是错误，路由层不得据此报 5xx。
"""

from __future__ import annotations

import uuid
from typing import Any

import redis

from ..core.config import Settings
from ..core.keys import RedisKeys
from ..middleware.observability import current_request_id
from .event_envelope import decode_fields, encode_payload

__all__ = ["AIEngineClient"]

#: job / result 流的保留上限（与 ai-engine 侧 ``AI_ENGINE_STREAM_MAXLEN_JOB`` 默认值一致）
_STREAM_MAXLEN = 10000


def _current_request_id() -> str:
    """取当前 HTTP 请求的关联 ID（非请求上下文返回空串）。

    为什么要跨模块取: ``middleware/observability`` 用 ContextVar 持有它（请求内任意深度可读），
    在这里入队时把它写进载荷，后续 ai-engine / result 消费器的日志就能与这条 HTTP 请求对上。
    """
    try:
        return current_request_id()
    except Exception:  # noqa: BLE001 取 ID 失败绝不能影响投递
        return ""


class AIEngineClient:
    """投递与读取封装（同步 Redis：投递在请求路径里很短，不值得为此引入异步客户端生命周期）。"""

    def __init__(self, settings: Settings, redis_client: redis.Redis | None = None) -> None:
        """初始化。

        参数:
            settings: 应用配置（读 ``env`` 决定键前缀）。
            redis_client: 可注入的同步客户端（测试用）；None 时由调用方通过 ``connect`` 提供。
        """
        self._settings = settings
        self._keys = RedisKeys(settings.env)
        self._redis = redis_client

    # ------------------------------------------------------------------ 连接
    def connect(self, client: redis.Redis) -> "AIEngineClient":
        """注入同步 Redis 客户端（链式调用，便于 ``with`` 之外的手工生命周期）。"""
        self._redis = client
        return self

    def _client(self) -> redis.Redis:
        """取客户端（未注入时懒建一个，调用方负责关闭）。"""
        if self._redis is None:
            from ..core.redis_client import new_sync_redis

            self._redis = new_sync_redis(self._settings)
        return self._redis

    # ------------------------------------------------------------------ 投递
    def trigger_generation(self, *, thread_id: str, product_id: str, org_id: str, rules: dict, guidance: str | None = None) -> str:
        """投递生成任务（``rules`` 是**入队瞬间固化**的合规快照，见 ``rules_snapshot``）。

        返回:
            消息 ID（str）。
        说明:
            载荷带 ``request_id``（若调用方处于 HTTP 请求上下文）—— 一次生成要跨
            backend → Redis → ai-engine 三段日志，这个 ID 是唯一能把三段串起来的东西。
            取不到（后台任务/测试直调）就不带该字段（ai-engine 侧宽容读取）。
            ``guidance``（可选）: 最近一次驳回意见，随任务下发，让下一轮生成**确定性地**
            针对性改写（历史上该意见只落库、进不了提示词，见 repositories/approvals.py）。
        """
        request_id = _current_request_id()
        payload = {
            "thread_id": str(thread_id),
            "product_id": str(product_id),
            "org_id": str(org_id),
            "rules": rules,
        }
        if guidance:
            payload["guidance"] = guidance
        if request_id:
            payload["request_id"] = request_id
        return self._xadd(self._keys.job_generate(), payload)

    def resume_approval(
        self, *, thread_id: str, product_id: str, org_id: str, result: str, feedback: str | None
    ) -> str:
        """投递审批结果（``result ∈ approved|rejected``）。

        载荷携带 ``product_id``/``org_id``：ai-engine 恢复图后用它们把终态结果发回
        ``result:workflow``（否则 resume 之后的终态无人认领）。
        """
        payload = {
            "thread_id": str(thread_id),
            "product_id": str(product_id),
            "org_id": str(org_id),
            "result": result,
            "feedback": feedback,
        }
        return self._xadd(self._keys.job_approval(), payload)

    def trigger_product_purge(self, *, product_id: str, org_id: str) -> str:
        """投递商品彻底删除任务（**必须在 products 行删除并 commit 之后调用**，见模块 docstring）。"""
        return self._xadd(self._keys.job_product_purge(), {"product_id": str(product_id), "org_id": str(org_id)})
    def _xadd(self, stream: str, payload: dict[str, Any]) -> str:
        """统一落盘（信封注入 + MAXLEN 近似裁剪）。

        参数:
            stream: 目标流键。
            payload: 业务载荷。
        返回:
            消息 ID。
        """
        message_id = self._client().xadd(stream, encode_payload(payload), maxlen=_STREAM_MAXLEN, approximate=True)
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)

    # ------------------------------------------------------------------ 读取
    def read_thread_events(self, *, thread_id: str, last_id: str = "0-0", count: int = 200) -> list[dict]:
        """读取某线程的事件流（SSE 回放 / 测试断言用）。

        参数:
            thread_id: 线程 ID。
            last_id: 起始 ID（``"0-0"`` = 全部历史；SSE 续传时传 ``Last-Event-ID`` 之后）。
            count: 单次上限。
        返回:
            ``[{"seq": <stream id>, "payload": {...}}, ...]``，按流内顺序。
        注意:
            本方法不做 schema_version 处理 —— 事件是**过程流**（前端只关心 ``type``/``data``），
            版本容忍只在 result 消费路径上做（见 ``workflow_result_consumer``）。
        """
        entries = self._client().xrange(self._keys.evt(thread_id), min=last_id, max="+", count=count)
        out: list[dict] = []
        for message_id, fields in entries:
            out.append({"seq": message_id, "payload": decode_fields(fields)})
        return out

    def close(self) -> None:
        """关闭自建连接（注入的客户端由调用方管理，不在此关闭）。"""
        if self._redis is not None:
            try:
                self._redis.close()
            except Exception as exc:  # noqa: BLE001 关闭失败不掩盖业务结果
                print(f"[ai-engine-client] 关闭 Redis 失败: {exc!r}")
            self._redis = None


def new_thread_id() -> str:
    """生成线程 ID（uuid4 字符串；``thread_id`` 是全链路幂等锚点）。"""
    return str(uuid.uuid4())
