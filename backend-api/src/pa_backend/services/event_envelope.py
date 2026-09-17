"""事件信封：``schema_version`` 注入与容忍（backend 侧唯一出处）。

契约（与 ai-engine ``ports/event_bus.py`` 对齐）:
    · 所有跨进程消息（``job:*`` / ``result:workflow`` / ``evt:*``）的 ``data`` 字段是 JSON 字符串，
      **JSON 首键固定为 ``schema_version``**；
    · 加字段 = 向后兼容（消费端忽略未知字段，不升版本）；
    · 改语义/删字段 = 必须升版本号。

为什么 backend 还要自己写一份常量（不 import ai-engine 的）:
    两个模块刻意不共享代码（可独立替换语言实现）。版本号因此是两个常量各自维护 ——
    升版本时必须**同时**改这里与 ``ai-engine/src/ports/event_bus.py``，回归由本模块用例锁住。

容忍策略（``check_version``）:
    收到**更高**版本的 result 消息时**只告警不中断**（按已知字段尽力处理）——
    生产端先升级不应该打挂老消费端；反过来「静默错读」的风险由版本号告警覆盖。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "ENVELOPE_DATA_FIELD",
    "SUPPORTED_SCHEMA_VERSION",
    "check_version",
    "decode_fields",
    "encode_payload",
]

#: 当前支持的信封版本（与 ai-engine ``EVENT_SCHEMA_VERSION`` 必须一致）
SUPPORTED_SCHEMA_VERSION = 1

#: Redis Stream 里承载 JSON 的字段名（两侧约定，勿改）
ENVELOPE_DATA_FIELD = "data"


def encode_payload(payload: dict[str, Any]) -> dict[str, str]:
    """把业务载荷包装成 Stream 字段（``{"data": "<json>"}``）。

    参数:
        payload: 业务载荷（JSON 可序列化 dict）。
    返回:
        ``{"data": json}``；``ensure_ascii=False`` 保留中文可读性（排障时肉眼可直接看流内容）。
    注意:
        若调用方已显式携带 ``schema_version``，原样保留（回放/测试需要定向注入）。
    """
    envelope = payload if "schema_version" in payload else {"schema_version": SUPPORTED_SCHEMA_VERSION, **payload}
    return {ENVELOPE_DATA_FIELD: json.dumps(envelope, ensure_ascii=False)}


def decode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """从 Stream 字段解出 JSON 载荷。

    参数:
        fields: ``XREAD/XREVRANGE`` 返回的字段 dict（值可能是 ``str`` 或 ``bytes``）。
    返回:
        解码后的 dict。
    异常:
        KeyError: 缺少 ``data`` 字段（脏消息）。
        json.JSONDecodeError: 载荷不是合法 JSON（脏消息）。
    """
    raw = fields.get(ENVELOPE_DATA_FIELD)
    if raw is None:
        raise KeyError(f"消息缺少 {ENVELOPE_DATA_FIELD!r} 字段")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def check_version(payload: dict[str, Any]) -> str | None:
    """检查信封版本；需要告警时返回提示文案（不抛异常）。

    参数:
        payload: 解码后的载荷（可能含 ``schema_version``）。
    返回:
        None = 版本可用（含缺省/相等）；
        字符串 = 高于本端支持，调用方应打印告警后**按已知字段继续处理**。
    """
    version = payload.get("schema_version")
    if not isinstance(version, int):
        return None  # 老消息可能没有版本号：按 v1 语义处理（当前唯一版本）
    if version > SUPPORTED_SCHEMA_VERSION:
        return (
            f"消息 schema_version={version} 高于本端支持 {SUPPORTED_SCHEMA_VERSION}："
            "按已知字段尽力处理（未识别字段将忽略）"
        )
    return None
