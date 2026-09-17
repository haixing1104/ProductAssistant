"""Agent 工具 × 真实 PostgreSQL 集成测试（数据只造在一次性测试容器里）。

【为什么这组用例存在】
    「Agent 能取到业务数据」必须可证伪：本用例在临时库里**真实写入**一组业务数据
    （见 tests/conftest.py::seed_business_data），再用 role_pa_ai 走真实工具读回来逐字段比对，
    证明工具拿到的就是库里那一行，而不是 mock 出来的漂亮结果。

【数据生命周期】
    docker compose -f infra/docker-compose.ai-test.yml up --build（起 pgsql-test 临时库）
    → 用例造数 / 断言 / 清理
    → docker compose ... down -v 整体销毁；宿主机的 productassistant 库全程不被触碰。

【覆盖】
    ① 四个取数工具都能读到真实行（商品素材 / 历史文案 / 历史评估 / 历史审批）；
    ② 多租户隔离：换一个 org/product 查询时读不到别人的数据；
    ③ 红线（负向）：role_pa_ai 对 schema_pa_backend.products 无写权限（UPDATE 必被拒绝）；
    ④ 端到端：AgentLoop + 真实取数工具 + 脚本化「模型」→ 结论里出现真实库里的标题与价格。

【运行】
    未设置 PA_TEST_PG_DSN 时全组自动 skip（宿主机只跑单测不受影响）。
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from src.adapters.pg_store import PgBusinessReader  # noqa: E402
from src.ports import AgentRuntime, ToolCall  # noqa: E402
from src.workflowcore.agent import AgentLoop, build_registry  # noqa: E402

pytestmark = pytest.mark.usefixtures("seed_business_data")


class RealDataRuntime(AgentRuntime):
    """脚本化「模型」：先依次调用取数工具，再基于工具结果给出结论。

    说明:
        这不是假工具——它调用的是真实注册表里的真实工具（真读 PG）；
        仅「模型」本身被脚本化，以隔离 LLM/网络不确定性，专注验证取数链路。
    """

    def __init__(self) -> None:
        """初始化轮次与收集到的工具返回值。"""
        self.turns = 0
        self.tool_outputs: list[str] = []

    def run_turn(self, messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
        """回放：第 1 轮调 get_product_facts；第 2 轮读工具消息并给结论。

        参数:
            messages: 内核传入的消息列表（第 2 轮起含 role=tool 的结果）。
            tools: 工具声明（忽略）。
            tool_choice: 工具选择策略（忽略）。
        返回:
            助手轮次（带 tool_calls 或最终结论）。
        """
        self.turns += 1
        if self.turns == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "name": "get_product_facts", "arguments": {}}]}
        self.tool_outputs = [str(m.get("content")) for m in messages if m.get("role") == "tool"]
        return {"content": "已核对：以工具返回的真实素材为准撰写文案", "tool_calls": []}


def _registry(ai_dsn: str):
    """装配真实工具注册表（role_pa_ai 只读取数）。

    参数:
        ai_dsn: role_pa_ai 连接串。
    返回:
        ToolRegistry 实例（4 个数据库取数工具）。
    """
    return build_registry(
        org_id="unused",
        product_id="unused",
        reader=PgBusinessReader(ai_dsn),
    )


def test_tools_read_real_rows_from_test_database(ai_dsn, seed_business_data):
    """四个取数工具读到的就是库里真实那一行（逐字段比对）。"""
    reader = PgBusinessReader(ai_dsn)
    scoped = build_registry(org_id=seed_business_data.org_id, product_id=seed_business_data.product_id, reader=reader)

    product = scoped.run(ToolCall(id="1", name="get_product_facts", arguments={})).content
    assert product["title"] == "集成测试商品·真空保温杯"
    assert product["base_price"] == pytest.approx(19.9)
    assert product["stock_status"] == "in_stock"
    assert product["raw_images"] == [{"url": "https://example.test/cover.png"}]

    versions = scoped.run(ToolCall(id="2", name="get_content_history", arguments={"limit": 5})).content
    assert [item["version"] for item in versions["items"]] == [2, 1]
    assert "第 2 版文案" in versions["items"][0]["text_excerpt"]
    assert versions["items"][0]["model_name"] == "glm-4-flash"

    logs = scoped.run(ToolCall(id="3", name="get_eval_history", arguments={"limit": 5})).content
    assert logs["count"] == 1
    assert logs["items"][0]["evaluator_type"] == "rule"
    assert logs["items"][0]["score"] == pytest.approx(60.0)
    assert "国家级" in logs["items"][0]["errors"][0]

    approvals = scoped.run(ToolCall(id="4", name="get_approval_history", arguments={"limit": 5})).content
    assert approvals["count"] == 1
    assert approvals["items"][0]["status"] == "rejected"
    assert approvals["items"][0]["feedback"] == "价格描述需与素材一致"


def test_tools_are_scoped_to_org_and_product(ai_dsn, seed_business_data):
    """多租户隔离（真实 SQL 生效）：换 org/product 查询读不到别人的数据。"""
    reader = PgBusinessReader(ai_dsn)
    other_org = "99999999-9999-4999-8999-999999999999"
    scoped = build_registry(org_id=other_org, product_id=seed_business_data.product_id, reader=reader)

    assert scoped.run(ToolCall(id="1", name="get_product_facts", arguments={})).content == {"found": False}
    assert scoped.run(ToolCall(id="2", name="get_content_history", arguments={})).content["count"] == 0
    assert scoped.run(ToolCall(id="3", name="get_eval_history", arguments={})).content["count"] == 0
    assert scoped.run(ToolCall(id="4", name="get_approval_history", arguments={})).content["count"] == 0


def test_role_pa_ai_cannot_write_products(ai_dsn, seed_business_data):
    """红线（负向）：role_pa_ai 对 schema_pa_backend.products 无写权限。"""
    from psycopg.errors import InsufficientPrivilege

    with psycopg.connect(ai_dsn, autocommit=True) as conn:
        with pytest.raises(InsufficientPrivilege):
            conn.execute(
                "UPDATE schema_pa_backend.products SET title = %s WHERE id = %s",
                ("被 AI 改写", seed_business_data.product_id),
            )


def test_agent_loop_uses_real_tools_and_returns_real_facts(ai_dsn, seed_business_data):
    """端到端：AgentLoop + 真实取数工具 → 工具结果里出现库里的真实标题与价格。"""
    runtime = RealDataRuntime()
    registry = build_registry(
        org_id=seed_business_data.org_id,
        product_id=seed_business_data.product_id,
        reader=PgBusinessReader(ai_dsn),
    )
    result = AgentLoop(runtime=runtime, registry=registry, max_turns=3).run(
        thread_id=seed_business_data.thread_id,
        question="核对本商品的真实素材与历史记录",
    )

    assert result.ok is True
    assert result.tool_calls_used == 1
    assert runtime.tool_outputs, "第二轮必须能看到 tool 角色消息"
    assert "集成测试商品·真空保温杯" in runtime.tool_outputs[0]
    assert "19.9" in runtime.tool_outputs[0]
    # 轨迹里记录了真实调用过的工具名（可审计）
    assert [entry.get("tool") for entry in result.trace if entry["event"] == "tool"] == ["get_product_facts"]
