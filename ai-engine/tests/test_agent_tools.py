"""Agent 业务工具单测（假端口；不需要 PG/Milvus/真实 LLM）。

覆盖:
  · 工具按端口有无裁剪：没有 reader / 规则引擎 / RAG 时不注册对应工具
    （模型中不会出现「调了也永远拿不到数据」的入口）；
  · 多租户红线：org_id / product_id 由闭包固化，模型参数**无法**覆盖
    （工具声明里也不出现这两个字段）；
  · 参数收敛：limit / top_k 越界或非法值被夹到安全区间；
  · scan_compliance：规则快照命中映射（含 rule_id 与 blocking 判定）、空文本不扫描；
  · retrieve_similar_copy：空 query 不触发检索；top_k 收敛；
  · 注册表：未知工具收敛为失败结果（附可用工具名）；
  · 节点接线：未注入 runtime（或工具表为空）时 agent_research 节点 no-op（返回 {}），
    保证「不接 Agent」时链路行为与既有版本完全一致。

说明:
    真实取数（PostgreSQL / 审批表）与真实 function calling 见
    tests/test_agent_tools_pg.py 与 tests/test_llm_function_calling.py。
"""

from __future__ import annotations

from typing import Any

from src.adapters.rules import compile_rules_snapshot
from src.ports import AgentRuntime, BusinessReader, ToolCall
from src.workflowcore.agent import build_registry
from src.workflowcore.agent.tools import build_business_tools
from src.workflowcore.node.node_agent import agent_research_node

ORG_ID = "11111111-1111-4111-8111-111111111111"
PRODUCT_ID = "66666666-6666-4666-8666-666666666601"


class FakeReader(BusinessReader):
    """记录型假业务读取端口（用于断言租户参数与 limit 收敛）。"""

    def __init__(self) -> None:
        """初始化空记录与固定返回。"""
        self.calls: list[tuple[str, dict]] = []

    def read_product(self, *, org_id: str, product_id: str) -> dict[str, Any] | None:
        """记录调用并返回固定商品。

        参数:
            org_id: 组织 ID（被记录，用于断言不被模型覆盖）。
            product_id: 商品 ID（被记录）。
        返回:
            固定商品 dict。
        """
        self.calls.append(("read_product", {"org_id": org_id, "product_id": product_id}))
        return {"id": product_id, "org_id": org_id, "title": "测试商品", "base_price": 19.9}

    def list_content_versions(self, *, org_id: str, product_id: str, limit: int = 3) -> list[dict[str, Any]]:
        """记录调用并返回固定版本列表。

        参数:
            org_id: 组织 ID（被记录）。
            product_id: 商品 ID（被记录）。
            limit: 返回条数上限（被记录）。
        返回:
            单元素列表（内容无关断言）。
        """
        self.calls.append(("list_content_versions", {"org_id": org_id, "product_id": product_id, "limit": limit}))
        return [{"version": 1, "is_approved": False, "model_name": "glm-4-flash", "text_excerpt": "旧文案", "created_at": "t"}]

    def list_eval_logs(self, *, org_id: str, product_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """记录调用并返回固定评估记录。

        参数:
            org_id: 组织 ID（被记录）。
            product_id: 商品 ID（被记录）。
            limit: 返回条数上限（被记录）。
        返回:
            单元素列表。
        """
        self.calls.append(("list_eval_logs", {"org_id": org_id, "product_id": product_id, "limit": limit}))
        return [{"evaluator_type": "rule", "score": 60.0, "errors": ["命中违禁词"], "rule_id": "r1", "created_at": "t"}]

    def list_approvals(self, *, org_id: str, product_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """记录调用并返回固定审批记录。

        参数:
            org_id: 组织 ID（被记录）。
            product_id: 商品 ID（被记录）。
            limit: 返回条数上限（被记录）。
        返回:
            单元素列表。
        """
        self.calls.append(("list_approvals", {"org_id": org_id, "product_id": product_id, "limit": limit}))
        return [{"status": "rejected", "channel": "web", "feedback": "价格描述不实", "approver_id": None, "resolved_at": "t"}]


class FakeRAG:
    """记录型假 RAG 端口。"""

    def __init__(self) -> None:
        """初始化空记录。"""
        self.calls: list[dict] = []

    def retrieve(self, *, org_id: str, product_id: str, query: str, top_k: int = 3) -> list[str]:
        """记录检索参数并返回固定片段。

        参数:
            org_id: 组织 ID（被记录）。
            product_id: 商品 ID（被记录）。
            query: 检索文本（被记录）。
            top_k: 条数上限（被记录）。
        返回:
            固定片段列表。
        """
        self.calls.append({"org_id": org_id, "product_id": product_id, "query": query, "top_k": top_k})
        return ["历史爆款片段A"]


def _registry(**kwargs: Any):
    """装配注册表（默认使用固定 org/product）。

    参数:
        **kwargs: 透传给 build_registry 的端口（reader / rule_engine / rag_store）。
    返回:
        ToolRegistry 实例。
    """
    return build_registry(org_id=ORG_ID, product_id=PRODUCT_ID, **kwargs)


def test_tools_are_registered_only_when_ports_present():
    """工具按端口有无裁剪：缺什么端口就不注册对应工具。"""
    assert _registry().names() == []
    assert _registry(reader=FakeReader()).names() == [
        "get_product_facts",
        "get_content_history",
        "get_eval_history",
        "get_approval_history",
    ]
    rules = compile_rules_snapshot({"words": [{"id": "w1", "word": "国家级", "severity": "high"}], "rules": []})
    with_rules = _registry(reader=FakeReader(), rule_engine=rules).names()
    assert "scan_compliance" in with_rules
    with_rag = _registry(reader=FakeReader(), rag_store=FakeRAG()).names()
    assert "retrieve_similar_copy" in with_rag


def test_multitenant_fields_are_never_exposed_to_model():
    """红线断言：工具声明里不出现 org_id / product_id（模型无处下跨租户参数）。"""
    registry = _registry(reader=FakeReader(), rule_engine=compile_rules_snapshot({"words": [], "rules": []}), rag_store=FakeRAG())

    for spec in registry.specs():
        properties = (spec.parameters or {}).get("properties") or {}
        assert "org_id" not in properties, f"{spec.name} 暴露了 org_id"
        assert "product_id" not in properties, f"{spec.name} 暴露了 product_id"
        assert spec.parameters.get("type") == "object"


def test_tenant_scope_comes_from_closure_not_arguments():
    """模型即使伪造 org_id / product_id，也只会按闭包固化的租户取数。"""
    reader = FakeReader()
    registry = _registry(reader=reader)

    result = registry.run(
        ToolCall(id="c1", name="get_product_facts", arguments={"org_id": "attacker", "product_id": "attacker"})
    )

    assert result.ok is True
    assert reader.calls == [("read_product", {"org_id": ORG_ID, "product_id": PRODUCT_ID})]
    assert result.content["title"] == "测试商品"


def test_limit_argument_is_clamped():
    """参数收敛：limit 越界夹到 10，非法值回落到默认 5。"""
    reader = FakeReader()
    registry = _registry(reader=reader)

    registry.run(ToolCall(id="c1", name="get_eval_history", arguments={"limit": 999}))
    registry.run(ToolCall(id="c2", name="get_eval_history", arguments={"limit": "abc"}))

    assert reader.calls[0][1]["limit"] == 10
    assert reader.calls[1][1]["limit"] == 5


def test_scan_compliance_maps_hits_and_blocking():
    """合规自查：命中映射保留 rule_id / severity，并给出 blocking 判定。"""
    rules = compile_rules_snapshot(
        {
            "words": [{"id": "w1", "word": "国家级", "severity": "high", "source": "广告法"}],
            "rules": [{"id": "r1", "pattern": "最\\w+", "type": "regex", "severity": "high", "suggestion": "改客观描述"}],
        }
    )
    registry = _registry(rule_engine=rules)

    result = registry.run(ToolCall(id="c1", name="scan_compliance", arguments={"text": "国家级品质，最便宜"}))
    payload = result.content

    assert result.ok is True
    assert payload["blocking"] is True
    keywords = {hit["keyword"] for hit in payload["hits"]}
    assert {"国家级", "最便宜"} <= keywords
    word_hit = next(hit for hit in payload["hits"] if hit["keyword"] == "国家级")
    assert word_hit["rule_id"] == "w1" and word_hit["severity"] == "high"


def test_scan_compliance_with_empty_text_returns_empty():
    """空文本不扫描：直接返回空命中（不调用规则引擎）。"""
    class _ExplodingRules:
        """一旦被调用就抛错，用于证明空文本不会触达规则引擎。"""

        def check(self, text: str) -> list:
            """无条件抛错。

            参数:
                text: 待检查文本（忽略）。
            异常:
                AssertionError: 始终抛出。
            """
            raise AssertionError("空文本不应触发规则扫描")

    registry = _registry(rule_engine=_ExplodingRules())
    result = registry.run(ToolCall(id="c1", name="scan_compliance", arguments={"text": "  "}))

    assert result.content == {"hits": [], "blocking": False, "scanned_chars": 0}


def test_retrieve_similar_copy_guards_query_and_clamps_top_k():
    """RAG 工具：空 query 不检索；top_k 越界夹到 5；租户参数由闭包注入。"""
    rag = FakeRAG()
    registry = _registry(rag_store=rag)

    empty = registry.run(ToolCall(id="c1", name="retrieve_similar_copy", arguments={"query": " "}))
    assert empty.content == {"snippets": [], "count": 0}
    assert rag.calls == []

    registry.run(ToolCall(id="c2", name="retrieve_similar_copy", arguments={"query": "保温杯", "top_k": 99}))
    assert rag.calls[0] == {"org_id": ORG_ID, "product_id": PRODUCT_ID, "query": "保温杯", "top_k": 5}


def test_unknown_tool_is_reported_with_available_names():
    """未知工具：返回失败结果并附可用工具名（便于模型纠正）。"""
    registry = _registry(reader=FakeReader())

    result = registry.run(ToolCall(id="c1", name="drop_table", arguments={}))

    assert result.ok is False and result.error == "unknown_tool"
    assert "get_product_facts" in result.content["available_tools"]


def test_agent_research_node_is_noop_without_runtime_or_tools():
    """节点接线：未注入运行时（或工具表为空）时 no-op，不产生任何状态变更。"""
    state = {"thread_id": "t", "product_id": PRODUCT_ID, "org_id": ORG_ID, "raw_product_info": {"title": "x"}}

    assert agent_research_node(state) == {}
    assert agent_research_node(state, agent_runtime=object(), reader=None) == {}


def test_node_avoids_langgraph_reserved_runtime_param():
    """回归：节点**不得**使用 LangGraph 的保留注入名 `runtime` 作为参数名。

    成因（真实事故）：
        LangGraph 按参数名注入自己的 Runtime 对象，会**覆盖** partial 里绑定的
        `runtime=None`，于是「未注入就 no-op」失效——节点拿到一个假的运行时、
        真注入时又拿不到自己的 GatewayAgentRuntime（表现为 agent.done 恒为 runtime_error）。
        由 tests/test_worker_e2e.py 的端到端用例首次暴露。
    """
    import inspect

    parameters = inspect.signature(agent_research_node).parameters

    assert "runtime" not in parameters, "agent_research_node 不得使用保留名 runtime（改用 agent_runtime）"
    assert "agent_runtime" in parameters


class OneShotRuntime(AgentRuntime):
    """单轮取证假运行时：第一轮请求 get_product_facts，第二轮直接给结论。"""

    def __init__(self) -> None:
        """初始化轮次计数。"""
        self.turns = 0

    def run_turn(self, messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
        """回放一次工具调用，随后收敛。

        参数:
            messages: 内核传入的消息列表（忽略）。
            tools: 工具声明（忽略，但保持端口签名一致）。
            tool_choice: 工具选择策略（忽略）。
        返回:
            第一轮带 tool_calls；第二轮仅文本（收敛）。
        """
        self.turns += 1
        if self.turns == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "name": "get_product_facts", "arguments": {}}]}
        return {"content": "价格 19.9，库存充足，请按此撰写", "tool_calls": []}


def test_workflow_does_not_run_agent_when_runtime_missing():
    """回归（图级）：未注入 agent_runtime 时，图执行不得产生任何 agent 事件。

    这条断言直接覆盖「LangGraph 保留名 runtime 覆盖 partial 绑定」的事故形态：
    事故时代码里明确传了 None，但图仍执行了 agent 研究并发出
    stage.researching / agent.done（可在 evt 流里看到，由 worker E2E 用例首次暴露）。
    """
    from src.workflowcore.graph import build_workflow, new_memory_checkpointer

    class _Bus:
        """记录事件 type 的假事件总线。"""

        def __init__(self) -> None:
            """初始化空记录。"""
            self.types: list[str] = []

        def publish(self, topic: str, payload: dict) -> None:
            """记录事件类型。

            参数:
                topic: 目标 topic（本假实现忽略）。
                payload: 事件载荷。
            返回:
                无返回值。
            """
            self.types.append(str(payload.get("type")))

    bus = _Bus()
    wf = build_workflow(event_bus=bus, checkpointer=new_memory_checkpointer())
    out = wf.invoke(
        {
            "thread_id": "t-agent-noop",
            "product_id": PRODUCT_ID,
            "org_id": ORG_ID,
            "raw_product_info": {"title": "平价商品", "base_price": 10},
        },
        config=wf.thread_config("t-agent-noop"),
    )

    assert out.get("status") == "succeeded"
    assert "stage.researching" not in bus.types, bus.types
    assert not [t for t in bus.types if t.startswith("agent.")], bus.types
    # 研究结论必须保持为空（节点 no-op：既不发事件也不写状态）
    assert not out.get("agent_context"), out.get("agent_context")
    assert not out.get("agent_trace")


def test_workflow_publishes_generate_stage_events():
    """回归（图级）：generate 节点必须拿到 event_bus（否则前端看不到正文流式输出）。

    历史遗漏：generate 节点注册时漏传 event_bus，导致 stage.generating / content.chunk
    从不发出（SSE 打字机效果静默失效，且改动前无人察觉）。
    """
    from src.workflowcore.graph import build_workflow, new_memory_checkpointer

    class _Bus:
        """记录事件 type 的假事件总线。"""

        def __init__(self) -> None:
            """初始化空记录。"""
            self.types: list[str] = []

        def publish(self, topic: str, payload: dict) -> None:
            """记录事件类型。

            参数:
                topic: 目标 topic（本假实现忽略）。
                payload: 事件载荷。
            返回:
                无返回值。
            """
            self.types.append(str(payload.get("type")))

    bus = _Bus()
    wf = build_workflow(event_bus=bus, checkpointer=new_memory_checkpointer())
    wf.invoke(
        {
            "thread_id": "t-generate-events",
            "product_id": PRODUCT_ID,
            "org_id": ORG_ID,
            "raw_product_info": {"title": "平价商品", "base_price": 10},
        },
        config=wf.thread_config("t-generate-events"),
    )

    assert "stage.generating" in bus.types, bus.types
    assert "content.chunk" in bus.types, bus.types


def test_agent_research_node_writes_context_trace_and_counter():
    """节点接线（正常路径）：结论 / 轨迹 / 工具用量写入状态增量，且按闭包租户取数。"""
    reader = FakeReader()
    state = {"thread_id": "t", "product_id": PRODUCT_ID, "org_id": ORG_ID, "raw_product_info": {"title": "x"}}

    result = agent_research_node(state, agent_runtime=OneShotRuntime(), reader=reader)

    assert result["agent_context"].startswith("价格 19.9")
    assert result["tool_calls_used"] == 1
    assert [entry["event"] for entry in result["agent_trace"]] == ["model", "tool", "model"]
    assert reader.calls[0][1] == {"org_id": ORG_ID, "product_id": PRODUCT_ID}


def test_tool_spec_serializes_to_openai_tool_shape():
    """工具声明形状：符合 OpenAI 兼容协议（type=function + function.{name,description,parameters}）。"""
    registry = _registry(reader=FakeReader())
    declared = registry.to_openai_tools()

    assert declared[0]["type"] == "function"
    assert declared[0]["function"]["name"] == "get_product_facts"
    assert declared[0]["function"]["parameters"]["type"] == "object"
    assert set(declared[0]["function"]) == {"name", "description", "parameters"}
