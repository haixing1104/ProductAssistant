"""tool：Agent 工具端口（只定协议与数据结构，零第三方依赖）。

职责:
    · 描述「模型可调用的工具」：ToolSpec（名称 / 描述 / JSON Schema 参数）——
      它是 function calling 协议里 tools 数组的唯一数据来源
      （to_openai_tool() 产出 OpenAI 兼容的 tools 元素）；
    · 承载一次工具调用（ToolCall）与执行结果（ToolResult）；结果必须
      JSON 可序列化，才能作为 tool 角色消息回填给模型继续推理。

边界（红线）:
    · 本端口只承载「只读取数 / 只读校验」类工具。ai-engine 对 schema_pa_backend
      只读（products / hitl_approvals 仅 SELECT），商品状态机与商品基础信息写入
      一律归 backend；因此**不允许**给 Agent 注册任何写工具；
    · 工具执行体在 adapters 侧实现（经 ports/business_reader.py 等端口取数），
      内核 workflowcore/agent 只负责编排、注册与中间件治理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["Tool", "ToolCall", "ToolResult", "ToolSpec"]


@dataclass(frozen=True)
class ToolSpec:
    """工具对外声明：名称 + 描述 + JSON Schema 参数。

    注意:
        parameters 必须是 JSON Schema 的 object 片段
        （形如 {"type": "object", "properties": {...}, "required": [...]}），
        它会被原样塞进 function calling 请求的 tools 数组；缺省时按「无参数」下发。
    """

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_openai_tool(self) -> dict[str, Any]:
        """转成 OpenAI 兼容协议（智谱兼容网关同协议）的 tools 元素。

        返回:
            {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}；
            parameters 为空时下发空 object schema，避免部分网关拒绝缺省参数体。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用。

    注意:
        id 由模型给出，用于把执行结果以 tool 角色消息回填到同一轮
        （协议要求 tool_call_id 与 assistant 消息里的 id 一一对应）。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的执行结果（结构化、可 JSON 序列化）。

    注意:
        ok=False 表示工具未成功（未知工具名 / 参数非法 / 取数异常 / 触发调用上限），
        error 携带可读原因；无论成败都必须能回填给模型，绝不因工具异常打断整图。
    """

    call_id: str
    name: str
    ok: bool
    content: Any = ""
    error: str | None = None

    def to_message_content(self) -> str:
        """转成 tool 角色消息的 content（JSON 字符串）。

        返回:
            形如 {"ok": true, "result": ...} 的 JSON 文本；失败时额外带 "error"。
        注意:
            用 default=str 兜底非 JSON 原生类型（如 uuid / datetime），
            保证「工具返回什么都不会让消息序列化失败」。
        """
        body: dict[str, Any] = {"ok": self.ok, "result": self.content}
        if self.error:
            body["error"] = self.error
        return json.dumps(body, ensure_ascii=False, default=str)

    def to_trace(self) -> dict[str, Any]:
        """转成可写入 ListingState.agent_trace 的审计条目。

        返回:
            {"call_id": ..., "tool": ..., "ok": ..., "error": ...}（不含完整结果体，避免状态膨胀）。
        """
        entry: dict[str, Any] = {"call_id": self.call_id, "tool": self.name, "ok": self.ok}
        if self.error:
            entry["error"] = self.error
        return entry


@dataclass
class Tool:
    """工具：声明（spec）+ 执行体（handler）。

    注意:
        handler 入参是模型给出的 JSON object（已解析为 dict），
        返回值任意可序列化对象；多租户字段（org_id / product_id）
        必须由装配方在闭包中固化，**不得**由模型参数决定（见 workflowcore/agent/tools.py）。
    """

    spec: ToolSpec
    handler: Callable[[dict[str, Any]], Any]

    def run(self, arguments: dict[str, Any] | None = None) -> Any:
        """执行工具（异常由调用方的中间件负责兜底，本方法不捕获）。

        参数:
            arguments: 模型给出的参数 dict；None 按空 dict 处理。
        返回:
            handler 的原始返回值（由调用方包装为 ToolResult 并序列化）。
        """
        return self.handler(arguments or {})
