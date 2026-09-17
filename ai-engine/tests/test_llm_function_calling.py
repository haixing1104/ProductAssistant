"""真实 function calling 冒烟（默认跳过：需 PA_TEST_LLM=1 且提供 ZHIPU_API_KEY）。

【为什么单独一组用例】
    「模型会不会真的点名调用工具」是 function calling 能否落地的第一性问题：
    协议、模型能力、网关兼容性任一环节不成立，Agent 就只剩「纯对话」。
    因此这里用**真实智谱网关**（OpenAI 兼容 tools 协议）做冒烟，而不是假网关。

【两组断言】
    ① 网关层：给出 tools 声明后，模型返回的 tool_calls 里出现被声明的工具名与 dict 参数
       （不依赖数据库，直接验证 adapter 的 tools 协议实现）；
    ② 端到端：AgentLoop + 真实取数工具（真读 PG）+ 真实 LLM，模型自行调工具并基于返回值收敛。

【运行】
    默认跳过。需要真跑时（会消耗少量 token）：
        PA_TEST_LLM=1 ZHIPU_API_KEY=... docker compose -f infra/docker-compose.ai-test.yml up --build ...
    或本机：
        PA_TEST_LLM=1 ZHIPU_API_KEY=... ZHIPU_BASE_URL=... \
        PA_TEST_PG_DSN=... PA_TEST_SUPERUSER_DSN=... PA_TEST_BACKEND_PG_DSN=... \
          python -m pytest tests/test_llm_function_calling.py -q
"""

from __future__ import annotations

import os

import pytest

from src.adapters.gateway_agent_runtime import build_agent_runtime_from_env
from src.ports import ToolSpec
from src.workflowcore.agent import AgentLoop, build_registry

LLM_ENABLED = os.getenv("PA_TEST_LLM", "0").lower() in {"1", "true", "yes"} and bool(
    os.getenv("ZHIPU_API_KEY", "").strip()
)

pytestmark = pytest.mark.skipif(
    not LLM_ENABLED,
    reason="未设置 PA_TEST_LLM=1 或缺少 ZHIPU_API_KEY：跳过真实 function calling 冒烟",
)


def test_gateway_emits_tool_call_for_registered_tool():
    """真实网关：声明工具后，模型返回 tool_calls（含工具名与 dict 参数）。"""
    runtime = build_agent_runtime_from_env()
    assert runtime is not None, "PA_TEST_LLM=1 时应能装配出真实运行时（检查 ZHIPU_API_KEY/ZHIPU_BASE_URL）"

    tools = [
        ToolSpec(
            name="get_product_facts",
            description="查询当前商品（线程绑定商品）的标题、价格与库存状态等真实基础资料",
            parameters={"type": "object", "properties": {}},
        ).to_openai_tool()
    ]
    response = runtime.run_turn(
        [
            {"role": "system", "content": "你是电商上架助手：需要事实时必须调用工具，禁止凭记忆回答。"},
            {"role": "user", "content": "请先调用工具查询本商品的真实标题与价格，再给出结论。"},
        ],
        tools=tools,
    )

    assert response["tool_calls"], f"模型未返回 tool_calls（function calling 未生效）：{response}"
    call = response["tool_calls"][0]
    assert call["name"] == "get_product_facts"
    assert isinstance(call["arguments"], dict)


def test_agent_loop_with_real_llm_and_real_tools(ai_dsn, seed_business_data):
    """端到端：真实 LLM + 真实工具 → 模型自行取数并收敛，轨迹里工具调用成功。

    参数:
        ai_dsn: role_pa_ai DSN（真实取数）。
        seed_business_data: 临时库里造好的一组业务数据。
    """
    from src.adapters.pg_store import PgBusinessReader

    runtime = build_agent_runtime_from_env()
    assert runtime is not None

    registry = build_registry(
        org_id=seed_business_data.org_id,
        product_id=seed_business_data.product_id,
        reader=PgBusinessReader(ai_dsn),
    )
    result = AgentLoop(runtime=runtime, registry=registry, max_turns=3, max_tool_calls=4).run(
        thread_id=seed_business_data.thread_id,
        question=(
            "必须先用 get_product_facts 工具查询本商品真实资料，再回答："
            "标题、价格、库存状态分别是什么？"
        ),
    )

    assert result.ok is True, f"未在预算内收敛：stop_reason={result.stop_reason} error={result.error}"
    assert result.tool_calls_used >= 1, f"模型未调用工具（trace={result.trace}）"
    tool_events = [entry for entry in result.trace if entry.get("event") == "tool"]
    assert tool_events and tool_events[0]["ok"] is True
    assert result.context.strip() != ""
