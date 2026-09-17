"""node：内核节点层（纯函数：只读 State、返回增量 dict）。

约定（红线）:
    · 节点不直连任何 DB/Redis/HTTP —— 一切外部能力经 ports 注入，None 时按确定性 mock/降级；
    · 返回「部分字段 dict」由 LangGraph 自动合并进 ListingState（无需手动改 state）；
    · 只有 node_hitl 允许 interrupt() 挂起，且依赖 checkpointer 持久化（见 graph.new_pg_checkpointer）。

节点清单与流转拓扑见 workflowcore/graph/workflow.py 的模块 docstring。
"""

from .node_rag import rag_retrieve_node
from .node_agent import agent_research_node
from .node_generate import generate_text_node
from .node_evaluate import evaluate_listing_node
from .node_image import gen_image_node
from .node_conditions import human_decision_route, should_retry_or_human
from .node_hitl import await_human_input
from .node_save_content import save_content_node
from .node_reject import reject_end_node

__all__ = [

    "rag_retrieve_node",
    "agent_research_node",
    "generate_text_node",
    "evaluate_listing_node",
    "gen_image_node",
    "human_decision_route",
    "should_retry_or_human",
    "await_human_input",
    "save_content_node",
    "reject_end_node",
]