"""registry：Agent 工具注册表（名称 → 工具；产出 function calling 声明 + 分发执行）。

职责:
    · 持有本线程允许模型调用的工具集合（每次任务现场装配，见 tools.py）；
    · 产出 tools 声明（ToolSpec.to_openai_tool()）供 function calling 使用；
    · 按模型给出的名字分发执行，并把「未知工具 / 执行异常」统一收敛为 ToolResult，
      使调用方（workflowcore/agent/loop.py）永远拿到结构化结果。

设计要点:
    · 名称唯一：重复注册覆盖先前定义并给出可读覆盖语义（便于按环境替换实现）；
    · 未知工具不抛异常：返回 ok=False 的结果回填给模型，模型可自行改用已注册工具；
    · 只读红线：本模块不提供任何写工具构造入口，写能力一律不进 Agent。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from ...ports import Tool, ToolCall, ToolResult, ToolSpec

__all__ = ["ToolRegistry", "build_tool"]


def build_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    handler: Callable[[dict[str, Any]], Any],
) -> Tool:
    """按显式 JSON Schema 构造一个工具（不做签名自省，声明与实现可审计）。

    参数:
        name: 工具名（function calling 的 function.name，须全局唯一且为英文小写下划线）。
        description: 给模型的用途说明（决定模型是否会选它，务必写清「什么时候用」）。
        parameters: JSON Schema object 片段；None 表示无参数。
        handler: 执行体（入参为模型给的 dict，返回任意可序列化对象）。
    返回:
        Tool 实例（spec + handler）。
    """
    return Tool(
        spec=ToolSpec(name=name, description=description, parameters=parameters or {}),
        handler=handler,
    )


class ToolRegistry:
    """工具注册表：声明产出 + 按名分发（顺序稳定，便于测试与审计）。"""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        """初始化。

        参数:
            tools: 初始工具集合；None 表示空表（此时 Agent 会退化为纯对话，无工具可调）。
        """
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.add(tool)

    def add(self, tool: Tool) -> None:
        """注册工具（同名覆盖，保留首次插入顺序）。

        参数:
            tool: 待注册工具。
        返回:
            无返回值。
        注意:
            同名覆盖用于「按环境替换实现」，例如集成测试把真实取数工具换成固定夹具；
            覆盖不会改变该名字在声明列表中的位置，保证 tools 数组顺序稳定。
        """
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名取工具；不存在返回 None（不抛异常）。

        参数:
            name: 工具名。
        返回:
            Tool 实例或 None。
        """
        return self._tools.get(name)

    def names(self) -> list[str]:
        """返回全部工具名（注册顺序）。

        返回:
            工具名字符串列表。
        """
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        """返回全部工具声明（注册顺序）。

        返回:
            ToolSpec 列表。
        """
        return [tool.spec for tool in self._tools.values()]

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """产出 function calling 的 tools 声明数组。

        返回:
            [{"type": "function", "function": {...}}, ...]；空表返回 []。
        """
        return [tool.spec.to_openai_tool() for tool in self._tools.values()]

    def run(self, call: ToolCall) -> ToolResult:
        """执行一次工具调用（未知工具收敛为失败结果，不抛异常）。

        参数:
            call: 模型给出的工具调用（name + arguments）。
        返回:
            成功：ToolResult(ok=True, content=handler 返回值)；
            未知工具：ToolResult(ok=False, error="unknown_tool")。
        异常:
            Exception: handler 自身抛出的异常（如 DB 连接失败）会向上抛，
                由中间件（middleware.ToolRetryMiddleware）兜底为重试 / 失败结果。
        """
        tool = self.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                content={"available_tools": self.names()},
                error="unknown_tool",
            )
        return ToolResult(call_id=call.id, name=call.name, ok=True, content=tool.run(call.arguments))

    def __len__(self) -> int:
        """工具数量（供调用方判断「有没有工具可用」）。"""
        return len(self._tools)
