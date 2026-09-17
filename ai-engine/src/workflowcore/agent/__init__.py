"""agent：Agent 内核（循环 + 中间件 + 工具注册 + 业务工具装配）。

定位:
    · 与 workflowcore/node 平级的内核子包，只依赖 ports 与 langgraph，
      不直连数据库 / Redis / HTTP（取数一律经 ports 注入）；
    · 对外只暴露三件事：AgentLoop（跑一轮研究）、middleware（治理钩子）、
      build_registry（按线程装配只读工具）。

红线:
    本包不提供任何写工具入口；Agent 的产物只进 ListingState（agent_context /
    agent_trace / tool_calls_used），不落库、不推进商品状态。
"""

from .loop import DEFAULT_SYSTEM_PROMPT, AgentLoop, AgentRunResult
from .middleware import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TURNS,
    AgentBudget,
    AgentContext,
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
    default_middlewares,
)
from .registry import ToolRegistry, build_tool
from .tools import build_business_tools, build_registry

__all__ = [
    "AgentLoop",
    "AgentRunResult",
    "DEFAULT_SYSTEM_PROMPT",
    "AgentBudget",
    "AgentContext",
    "AgentMiddleware",
    "ModelCallLimitMiddleware",
    "ToolCallLimitMiddleware",
    "ToolRetryMiddleware",
    "default_middlewares",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_TOOL_CALLS",
    "ToolRegistry",
    "build_tool",
    "build_business_tools",
    "build_registry",
]
