"""agent_runtime：Agent 运行时端口（一次「模型轮次」的执行边界）。

职责:
    把 messages（OpenAI 兼容消息列表）与 tools（function calling 声明）
    交给模型，取回一次助手轮次的结果；是内核 workflowcore/agent 与
    「模型供应商实现」之间唯一的隔离点。

为什么单独抽一个端口（而不是直接调 LLMGateway）:
    · 现有 LLMGateway 面向「一次纯文本 / 结构化 JSON 生成」，语义简单；
      带工具的多轮对话返回值结构不同（content + tool_calls），单独成口可以
      在不改 llm_gateway.py 既有方法签名的前提下扩展能力（向后兼容）；
    · 将来若要换成 LangChain 官方 create_agent + 官方中间件体系，
      只需新增一个实现本端口的 adapter，内核与全部工具零改动。

实现:
    · adapters/llm_zhipu.LLMZhipuGLMGateway（真实实现：OpenAI 兼容 tools 协议）；
    · 测试用脚本化假实现（见 ai-engine/tests/test_agent_loop.py）。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

__all__ = ["AgentRuntime", "normalize_tool_calls"]


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """把不同实现返回的 tool_calls 收敛为统一结构（内核只认这一种）。

    参数:
        raw: 实现方返回的原始 tool_calls（通常来自 OpenAI 协议的
            choices[0].message.tool_calls：每项含 id / function.name / function.arguments）。
    返回:
        [{"id": str, "name": str, "arguments": dict}, ...]；
        入参非列表、条目非法或缺 name 时跳过该条（防御，不抛异常）。
    注意:
        arguments 已是 dict（实现方负责把协议里的 JSON 字符串解析好）；
        若实现方给出的是 JSON 字符串，本函数会尽力解析，失败则包成 {"_raw": ...}，
        保证「模型给什么参数都不会让内核崩」。
    """
    if not isinstance(raw, list):
        return []
    calls: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        calls.append({"id": str(item.get("id") or f"call_{index}"), "name": name, "arguments": args})
    return calls


class AgentRuntime(ABC):
    """Agent 运行时端口：执行一次模型轮次（可含工具调用请求）。"""

    @abstractmethod
    def run_turn(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
    ) -> dict:
        """执行一次模型轮次。

        参数:
            messages: OpenAI 兼容消息列表；内核会依次追加 assistant（含 tool_calls）
                与 tool（工具结果）消息，实现方应原样透传给模型。
            tools: tools 声明列表（ToolSpec.to_openai_tool() 的产物）；
                None 或空列表表示本轮不带工具（模型只能回复文本）。
            tool_choice: 工具选择策略（默认 "auto"）；取值需与协议一致。
        返回:
            {"content": str, "tool_calls": [...], "model": str | None}：
            · content：助手文本（无文本时为空串，不要返回 None）；
            · tool_calls：normalize_tool_calls() 口径的列表，空列表表示模型收敛；
            · model：本次实际使用的模型名（可缺省，仅用于审计/排障）。
        异常:
            Exception: 网络/协议错误由实现方抛自身异常类型；内核 workflowcore/agent/loop.py
                会捕获并降级（stop_reason=runtime_error），不阻断生成主链路。
        """
