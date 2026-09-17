"""ListingState：ListingWorkflow 的全量状态 Schema。

生产化：状态由 pydantic model 承载（LangGraph 原生支持 pydantic state），
字段级类型/约束校验在“节点读入”时生效；节点返回部分字段 dict 由 LangGraph 按
字段合并

编排与节点间的数据交换只通过本 State 在内存中传递；
禁止在 State 里放 DB 连接 / Repository 等 IO 对象。
字段均为业务数据或结果。

动态/JSONB 类数据（raw_product_info 素材、human_feedback）保持宽松 dict，
只约束其容器存在性，避免每条新列/渠道演进都触发状态模型迁移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from .schemas import EvalOutput

class ListingState(BaseModel):
    """商品上架文图生成工作流的状态类型。

    必填（invoke 输入）：thread_id / product_id / org_id / raw_product_info；
    其余字段由各节点逐步填充，读 State 后返回增量更新（带默认值以支撑部分更新）。
    """
    model_config = ConfigDict(extra="forbid")

    thread_id: str = ""
    product_id: str = ""
    org_id: str = ""

    # 商品基础资料（来自 backend-api products，AI 只读素材）
    raw_product_info: dict[str, Any] = Field(default_factory=dict)
    # RAG 召回的历史高转化文案（Few-Shot 上下文）
    rag_context: str = ""
    # Agent 研究结论（只读工具取证后的要点；node_agent 产出、node_generate 消费）
    agent_context: str = ""
    # Agent 调用轨迹（逐轮模型/工具记录，JSON 安全；供审计与排障，不参与落库）
    agent_trace: list[dict] = Field(default_factory=list)
    # Agent 已消耗的工具调用次数（预算治理观测值；上限由中间件控制）
    tool_calls_used: int = 0
    # 主生成模型产出的文案
    generated_content: str = ""
    # 最终落库/审批快照的结构化 blocks（图文唯一事实源，对齐 product_contents.content_data）：
    #   [{type:"text", text}, {type:"image", url, alt, source}...] —— node_generate/node_image 节点逐步填充
    content_blocks: list[dict] = Field(default_factory=list)
    # 配图结果（事件/日志/降级判定）：node_image 节点执行后置位；失败时 content_blocks 仅含 text
    image_attached: bool = False
    image_error: str | None = None

    # LLM Evaluator 的结构化结果（schemas.EvalOutput，见 schemas.py）
    evaluation_result: EvalOutput | None = None
    # 上一轮评估的附加错误（结构修复失败等 server 侧信息，供 Reflection 意见拼装）
    last_eval_errors: list[str] = Field(default_factory=list)
    # 已完成的评估次数（用于 Reflection 重试上限判断）
    evaluation_attempts: int = 0
    # 重试上限（build_workflow 注入，默认 2）
    max_retries: int = 2

    # HITL 审批返回内容Command(resume=...) 恢复后注入
    human_feedback: dict[str, Any] | None = None
    # HITL 审批结论：approved / rejected / 空
    approval: str = ""

    # 图执行终态：running / succeeded / rejected / failed / 空
    status: str = ""
    error: str | None = None


def ensure_state(state: ListingState | dict) -> ListingState:
    """把任意 state 归一为 ListingState 实例（dict 入参用于纯函数单测/进程外调用）。

    参数:
        state: ListingState 实例或等价 dict（键须为 ListingState 已声明字段）。
    返回:
        ListingState 实例（入参已是实例则原样返回，不做拷贝）。
    异常:
        pydantic.ValidationError: 键不是已声明字段（model_config extra="forbid"）
            或字段类型/约束不满足时抛出。
    注意:
        LangGraph 传入节点的是「通道值 dict」，可能含 __interrupt__ 等非 State 键；
        该场景由 LangGraph 内部先剥离，节点侧只应收到 State 字段。
    """
    return state if isinstance(state, ListingState) else ListingState(**state)


