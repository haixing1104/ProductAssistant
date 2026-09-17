"""ListingWorkflow 编排：
    Graph + Generation/Evaluation/HITL 子流程。

数据流：
START → rag_retrieve → agent_research → generate → evaluate
                                    ├─ persist（评估通过且低价不需要人工介入） → image_then_save → save_content → END
                                    ├─ retry（评估未过但未耗尽重试次数）  → generate（Reflection 重写）
                                    └─ human（评估未过且耗尽次数/高价触发）  → image_then_human → hitl → save_content / reject_end    

路由键 ↔ 实际节点（条件边返回的是「路由键」，与节点名不同，读代码时勿混淆）：
  evaluate 出口：retry→generate ｜ persist→image_then_save ｜ human→image_then_human
  hitl     出口：persist→save_content ｜ reject_end→reject_end
  说明：高价商品（售价 > 500）评估通过也走 human；审批前文案不落 product_contents，
        仅随 awaiting_human 结果的 content_snapshot 带给审批人。

agent_research（可选增强节点）:
    在 RAG 召回之后、生成之前插入一次「只读工具取证」（Agent + function calling +
    中间件预算治理，见 workflowcore/agent/）。**未注入 agent_runtime 时该节点直接 no-op**
    （返回 {}），因此现有链路、mock 降级路径与既有测试行为完全不变；
    注入后结论写入 ListingState.agent_context 供 generate 使用。

checkpoint:
    interrupt 挂起 / Command(resume) 恢复依赖 checkpointer 持久化图状态：
      · 生产：new_pg_checkpointer()（LangGraph PostgresSaver + psycopg 连接池，落 schema_pa_ai）
      · 测试/本地：new_memory_checkpointer()（InMemorySaver，进程内、退出即丢）
"""

from __future__ import annotations

import os
from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .. import node
from ...ports import (
    AgentRuntime,
    BusinessReader,
    ContentStore,
    EvalLogStore,
    EventBus,
    ImageNormalizer,
    LLMGateway,
    RAGStore,
    RuleEngine,
)
from ..agent.middleware import DEFAULT_MAX_TOOL_CALLS, DEFAULT_MAX_TURNS, AgentMiddleware
from ..state import ListingState


def new_memory_checkpointer():
    """测试/本地用的内存 checkpointer（InMemorySaver）。

    返回:
        InMemorySaver：进程内状态存储，进程退出即丢失。
    注意:
        仅用于单元测试与本地无 PG 环境演练，不可用于生产 HITL 恢复
        （生产须用 new_pg_checkpointer()，否则 interrupt 后无法跨进程 resume）。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def new_pg_checkpointer(
    runtime_dsn: str,
    *,
    schema: str,
    min_size: int = 1,
    max_size: int = 8,
    timeout_seconds: float = 10.0,
):
    """生产用 PostgreSQL checkpointer：LangGraph PostgresSaver + psycopg 连接池。

    参数:
        runtime_dsn: 运行期 DSN，必须是 role_pa_ai（schema_pa_ai 全 DML）；
            checkpoint 系列表须已由 adapters/pg_store.ensure_checkpoint_schema()
            以 role_pa_ai_setup 身份建好（见 database/sql/0002_roles_grants.sql）。
        schema: checkpoint 表所在 schema；由调用方注入（本仓 = schema_pa_ai），
            避免 graph 与 adapters 两处各写一份 schema 常量。
        min_size/max_size: 连接池上下限；worker 常驻，至少保留 1 条热连接。
        timeout_seconds: 连接池就绪等待上限（秒）。
    返回:
        PostgresSaver：连接池驱动、search_path 已固化的 saver。
    异常:
        psycopg_pool.PoolTimeout: DSN 不可达 / 权限不足，池在超时内未就绪。
        ImportError: 未安装 langgraph-checkpoint-postgres 或 psycopg-pool。
    注意:
        · 连接级参数在「连接工厂层」一次性固化，池中每条新连接自动生效
          （连接池下不能用一次性 SET，否则新连接会落到 public）：
            autocommit=True          —— PostgresSaver 强制要求（.setup() 需自动提交）；
            row_factory=dict_row     —— PostgresSaver 按列名取值；
            prepare_threshold=0      —— 兼容 PgBouncer transaction pooling；
            options="-c search_path" —— 每条连接都落到 schema_pa_ai；
        · check=check_connection：取出前探活，坏连接自动重建，避免 PG 重启 /
          网络抖动后 HITL resume 永久失败（裸 Connection 无法自愈）；
        · LANGGRAPH_STRICT_MSGPACK：官方反序列化加固（限制 checkpoint 可反序列化类型），
          此处按默认值开启，可由环境变量显式覆盖；
        · 本函数不建表（建表角色是 role_pa_ai_setup，权限红线不越界），也不关闭连接池；
          进程退出前请调 close_pg_checkpointer() 释放。
    """
    # 官方安全加固：checkpoint 反序列化白名单（未显式配置时默认开启）
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=runtime_dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout_seconds,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
            "options": f"-c search_path={schema},public",
        },
        check=ConnectionPool.check_connection,
        open=True,
        name="pa-langgraph-checkpoint",
    )
    # 启动即校验 DSN/权限：坏配置快速暴露，而不是等到第一条消息才报错
    pool.wait(timeout=timeout_seconds)
    return PostgresSaver(pool)


def close_pg_checkpointer(saver) -> None:
    """关闭 saver 持有的连接池（幂等；非池化 saver 静默跳过）。

    参数:
        saver: new_pg_checkpointer() 返回的 PostgresSaver；None 亦安全。
    返回:
        无返回值。
    """
    pool = getattr(saver, "conn", None)
    close = getattr(pool, "close", None)
    if callable(close):
        close()


def resume_command(resume_payload: dict):
    """构造 HITL 恢复命令：LangGraph Command(resume=...)。

    参数:
        resume_payload: 恢复时喂给 interrupt() 的返回值；本项目为
            {"approved": bool, "feedback": str|None}（见 node_hitl）。
    返回:
        Command 对象，供 graph.invoke(command, config=线程配置) 使用。
    """
    return Command(resume=resume_payload)


def build_workflow(
        *,
    llm_gateway: LLMGateway | None = None,
    rag_store: RAGStore | None = None,
    content_store: ContentStore | None = None,
    eval_log_store: EvalLogStore | None = None,
    event_bus: EventBus | None = None,
    rule_engine: RuleEngine | None = None,
    image_gateway=None,
    object_storage=None,
    image_normalizer: ImageNormalizer | None = None,
    asset_key_prefix: str = "img/pa",
    max_retries: int = 2,
    checkpointer=None,
    agent_runtime: AgentRuntime | None = None,
    agent_reader: BusinessReader | None = None,
    agent_middlewares: list[AgentMiddleware] | None = None,
    agent_max_turns: int = DEFAULT_MAX_TURNS,
    agent_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
):
    """组装并编译 ListingWorkflow（返回带 max_retries 的轻量包装）。

    参数:
        llm_gateway: 文本生成/评估网关；None 时节点回退确定性 mock 文案。
        rag_store: RAG 向量库；None 时 rag_retrieve 返回空上下文。
        content_store: 内容落库端口；None 时 save_content 只置终态不落库。
        eval_log_store: 评估日志端口；None 时不落 evaluation_logs。
        event_bus: 事件总线；None 时不发过程/终态事件（SSE 无输出）。
        rule_engine: 合规规则引擎；None 时评估仅走 LLM/骨架路径。
        image_gateway: AI 生图网关；None 时 node_image 降级纯文本。
        object_storage: 对象存储；None 时生图产物无处可落，同样降级纯文本。
        image_normalizer: 图片规格化端口（统一 1:1/格式/背景）；None 时不做重编码，
            仅按字节探测实测尺寸（行为与接入前一致）。
        asset_key_prefix: 生图对象键前缀（调用方按 img/<项目前缀>/{env} 注入）。
        max_retries: 评估未通过时的重试上限（< 1 会被夹到 1）。
        checkpointer: LangGraph checkpointer；None 时无持久化，
            **interrupt 无法挂起/恢复**，HITL 场景必须传入
            （生产 new_pg_checkpointer()、测试 new_memory_checkpointer()）。
        agent_runtime: Agent 运行时（带 tools 的模型）；None 时 agent_research 节点 no-op
            （行为与未接入 Agent 时完全一致，向后兼容）。
        agent_reader: Agent 取数用的业务只读端口；None 时数据库取数工具不注册。
        agent_middlewares: Agent 中间件链（预算熔断 / 工具上限 / 重试兜底）；
            None 时用 workflowcore/agent 的默认链。
        agent_max_turns: Agent 模型调用上限（预算熔断阈值）。
        agent_max_tool_calls: Agent 工具调用上限。
    返回:
        _WorkflowWrapper：invoke / resume / thread_config 三个入口。
    注意:
        依赖全部以参数注入，本函数不读环境变量、不建连接；
        连接生命周期由调用方（service 层）负责。
    """

    builder = StateGraph(ListingState)

    # 添加RAG节点：RAG 召回的历史高分转化文案片段通过Langgraph增量更新存入ListState.rag_context
    builder.add_node("rag_retrieve", partial(node.rag_retrieve_node, rag=rag_store))

    # 添加Agent研究节点（增强功能）：用只读工具取证（真实素材/历史文案/历史评估/审批意见），
    # 结论与轨迹增量写入 ListState.agent_context / agent_trace / tool_calls_used。
    # 未注入 agent_runtime 或工具表为空时节点直接 no-op，不影响原有链路。
    builder.add_node("agent_research", partial(
        node.agent_research_node,
        # 注意：这里必须用 agent_runtime（而非 runtime）——`runtime` 是 LangGraph 的保留
        # 注入名，框架会无视 partial 绑定强行注入自己的 Runtime 对象（实测踩过，见 node_agent.py）。
        agent_runtime=agent_runtime,
        reader=agent_reader,
        rule_engine=rule_engine,
        rag_store=rag_store,
        middlewares=agent_middlewares,
        max_turns=agent_max_turns,
        max_tool_calls=agent_max_tool_calls,
        event_bus=event_bus,
    )) 

    # 添加generate LLM节点生成纯文本。
    # 增量更新到ListState.generated_content字段；并发布 stage.generating / content.chunk
    # （SSE 打字机效果的数据源，见 node_generate.py）。此处必须注入 event_bus：
    # 遗漏会让前端「只看到阶段跳变、看不到正文流式输出」且排查困难（历史遗漏，已修）。
    builder.add_node("generate", partial(node.generate_text_node, gateway=llm_gateway, event_bus=event_bus))

    # 添加evaluate节点
    # Langgraph自动增量更新到ListState.evaluation_attempts、evaluation_result、last_eval_errors
    builder.add_node("evaluate", partial(
        node.evaluate_listing_node,
        gateway=llm_gateway,
        eval_log_store=eval_log_store,
        event_bus=event_bus,
        rule_engine=rule_engine,
    ))

    # 生图节点（attach）注册为两个实例：挂在不同终局分支上（image,save_content→落库；image,human→进 HITL），
    # 使评审人中断前的 content_snapshot 已含图（业务要求：审批人须看完整图文再决策）
    # 具体决策在条件边判断走哪一路分支
    builder.add_node("image_then_save", partial(
        node.gen_image_node,
        event_bus=event_bus,
        image_gateway=image_gateway,
        object_storage=object_storage,
        image_normalizer=image_normalizer,
        image_key_prefix=asset_key_prefix,
    ))

    builder.add_node("image_then_human", partial(
        node.gen_image_node,
        event_bus=event_bus,
        image_gateway=image_gateway,
        object_storage=object_storage,
        image_normalizer=image_normalizer,
        image_key_prefix=asset_key_prefix,
    ))

    # 添加入库节点，把生成的文案持久化存储
    builder.add_node("save_content", partial(
        node.save_content_node,
        content_store=content_store,
        event_bus=event_bus,
        rag_store=rag_store
    ))

    # 添加hitl节点
    # Langgraph自动增量更新到ListState的human_feedback和approval字段
    builder.add_node("hitl", node.await_human_input)

    # 添加拒绝节点
    builder.add_node("reject_end", node.reject_end_node)

    # ----------添加边-----------
    # 第1步：先RAG 检索. RAG 召回的历史高分转化文案片段通过Langgraph增量更新存入ListState.rag_context，但缺乏了event通知
    builder.add_edge(START, "rag_retrieve")

    # 第2步：Agent 只读工具取证（注入 runtime 时生效；否则 no-op 直通）。
    builder.add_edge("rag_retrieve", "agent_research")

    # 第2.1步：generate LLM生成纯文本。
    # 增量更新到ListState.generated_content字段，并通过event发布出去供backend进行SSE 推送给前端显示过程
    # （先读 Agent 结论：agent_context 会作为「事实要点」拼进生成提示词）
    builder.add_edge("agent_research", "generate")

    # 第3步：评估。对LLM生成的内容进行合规评估，并发布事件。
    # Langgraph自动增量更新到ListState.evaluation_attempts、evaluation_result、last_eval_errors（评估次数，结果，上次错误信息）
    builder.add_edge("generate", "evaluate")

    # 第4步：条件边 根据evaluate评估的结果，进行决策。一个节点，一次判断，获取到3个直接结果。
    # condition1:评估通过且低价（不触发HITL）-> 路由键 persist -> image_then_save -> save_content 入库存储
    # condition2:评估未通过，且没有耗尽尝试次数 -> 路由键 retry -> 回到 generate 重新生成
    # condition3:评估未通过且耗尽尝试次数，或者 高价(触发价格边界) -> 路由键 human -> image_then_human -> hitl 人工介入
    builder.add_conditional_edges(
        "evaluate",
        node.should_retry_or_human,
        {"retry": "generate",           # 返回之前的节点（Reflection 重写）
         "persist": "image_then_save",  # 先配图再落库（评审前图文一致）
         "human": "image_then_human"},  # 先配图再进 HITL（审批人看完整图文）
    )

    # 第4.1.a步 存储入库
    builder.add_edge("image_then_save", "save_content")
    # 第4.1.b步 结束
    builder.add_edge("save_content", END)

    # 第4.2.a步 人工介入
    builder.add_edge("image_then_human", "hitl")
    # 第4.2.b步 人工同意的话就走save(save后走4.1.b的save_content即可结束)
    builder.add_conditional_edges(
        "hitl",
        node.human_decision_route,
        {"persist": "save_content", "reject_end": "reject_end"},
    )
    # 第4.2.c步 人工拒绝后，结束
    builder.add_edge("reject_end", END)

    # 供条件边与节点读取重试上限
    max_retries = max(1, int(max_retries))

    compiled = builder.compile(checkpointer=checkpointer)
    return _WorkflowWrapper(compiled, max_retries=max_retries)


class _WorkflowWrapper:
    """轻量包装：携带 max_retries 配置并在 invoke 前注入默认值。"""

    def __init__(self, graph, *, max_retries: int):
        """初始化。

        参数:
            graph: 已编译的 LangGraph（builder.compile(checkpointer=...)）。
            max_retries: 评估未通过时的重试上限；由 invoke 注入 State 供条件边读取。
        """
        self.graph = graph
        self.max_retries = max_retries

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        """启动图并同步跑到底（或跑到 interrupt 挂起为止）。

        参数:
            state: 初始 State 增量；必填 thread_id / product_id / org_id / raw_product_info。
            config: LangGraph 运行配置；HITL 场景必须含 thread_id（见 thread_config）。
        返回:
            终态 State dict；发生 HITL 挂起时额外含 "__interrupt__"（调用方据此判定转人工）。
        注意:
            max_retries 由本包装注入，调用方无需（也不应）在 state 里自带。
        """
        state = {**state, "max_retries": self.max_retries}
        return self.graph.invoke(state, config=config)

    def resume(self, decision: dict, config: dict | None = None) -> dict:
        """HITL 恢复：以 Command(resume=decision) 从断点继续执行。

        参数:
            decision: 审批决策 dict，形如 {"approved": bool, "feedback": str|None}。
            config: 必须含与挂起时**相同**的 thread_id，否则定位不到 checkpoint。
        返回:
            恢复后的终态 State dict（approved→succeeded / rejected→rejected）。
        注意:
            同一线程的 resume 须由上层串行化（worker 用 lock:{thread_id} 保证）；
            并发恢复会造成重复落库与重复终态事件。
        """
        return self.graph.invoke(resume_command(decision), config=config)

    def thread_config(self, thread_id: str) -> dict:
        """生成 LangGraph 线程配置（thread_id 即幂等锚点）。

        参数:
            thread_id: 线程 ID（本项目 = 生成任务的 thread_id，UUID 字符串）。
        返回:
            {"configurable": {"thread_id": ...}}，供 invoke / resume 传入。
        注意:
            invoke 与 resume 必须使用同一 thread_id，否则恢复不到挂起的 checkpoint。
        """
        return {"configurable": {"thread_id": thread_id}}