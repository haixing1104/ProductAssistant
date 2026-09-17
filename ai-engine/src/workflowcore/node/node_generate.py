"""Generation 主节点：拼 Prompt → LLMGateway 生成 → 写回 generated_content。"""

from __future__ import annotations

from typing import Any
from ...ports import LLMGateway
from ..state import ListingState, ensure_state

def _build_generation_prompt(state: ListingState, attempts: int) -> str:
    """构造生成/反思改写提示词。attempts>0 时携带上轮评估意见做自我纠错（Reflection）。

    参数:
        state: 当前图状态；读 raw_product_info / rag_context / agent_context / reject_guidance /
            last_eval_errors / evaluation_result。
        attempts: 已完成的评估次数；>0 表示这是一次 Reflection 重写。
    返回:
        完整生成提示词字符串。
    """

    # 商品基础资料（来自 backend-api products，AI 只读素材）
    info: dict[str, Any] = state.raw_product_info or {}
    title = str(info.get("title") or "商品")
    # 提示词
    prompt = f"为商品「{title}」撰写中文电商详情文案。\n基础资料: {info}\n"

    # RAG 召回的历史高转化文案（Few-Shot 上下文）
    ctx = state.rag_context or ""
    if ctx:
        prompt += f"参考以下历史高转化文案(Few-Shot)：\n{ctx}\n"
    # Agent 只读工具取证出的「事实要点」（价格/库存/历史违规点/审批意见）：
    # 优先级高于基础资料里的初值，且明确要求不得与之冲突
    facts = state.agent_context or ""
    if facts:
        prompt += f"事实要点（由只读工具核对得出，不得与之冲突）：\n{facts}\n"
    # 人工审批驳回意见（backend 随任务下发）：**最高优先级**的改写指令 ——
    # 审批人是决策者，其意见必须逐条解决，而不是被当成"参考"。
    # （历史上的缺口：驳回意见只落 hitl_approvals.feedback，从不进提示词，等于白写。）
    guidance = (state.reject_guidance or "").strip()
    if guidance:
        prompt += (
            "上一轮人工审批驳回意见（必须逐条解决、不得回避或降级为建议）："
            f"{guidance}\n"
        )
    # 如果attempts > 0, 就参考上一轮评估的附加错误自我纠错
    if attempts > 0:
        errors = state.last_eval_errors or (state.evaluation_result.errors if state.evaluation_result else [])
        if errors:
            prompt += "上一轮评估意见（请针对性改进）：" + "；".join(str(e) for e in errors) + "\n"
    prompt += "输出要求：标题 + 卖点列表 + 详情段落，全中文，避免违禁与极限词。"
    return prompt

def generate_text_node(
    state: ListingState,
    gateway: LLMGateway | None = None,
    event_bus=None,
) -> dict:
    """生成商品文案。支持流式增量输出（SSE 打字机效果）。

    流式流程：
      1. 发 stage.generating 阶段事件
      2. 逐块调用 gateway.generate_text_stream() → 每块按 ≤40 字符切微块后发 content.chunk
      3. 拼合完整内容返回
    无 gateway（骨架纯函数态）时产出确定性的 Mock 文案。

    参数:
        state: 当前图状态；读 raw_product_info / rag_context / evaluation_attempts / last_eval_errors。
        gateway: 文本生成网关；None 时产出确定性 Mock 文案（便于无 Key 环境跑通全链路）。
        event_bus: 事件总线；None 时不发过程事件（SSE 无打字机输出，落库不受影响）。
    返回:
        增量 dict：{"generated_content": ...}。
    注意:
        本节点只产出 generated_content；图文 blocks 由 node_image 组装。
    """

    state = ensure_state(state)
    # 商品基础资料（来自 backend-api products，AI 只读素材）
    info: dict[str, Any] = state.raw_product_info or {}
    title = str(info.get("title") or "商品")

    # 已完成的评估次数（用于 Reflection 重试上限判断）
    attempts = state.evaluation_attempts

    # 发布事件 方便通过backend SSE 推送到前端显示执行过程
    if event_bus is not None:
        event_bus.publish(
            f"evt:{state.thread_id}",
            {"type": "stage.generating", "data": {"attempt": attempts + 1, "product_id": state.product_id}},
        )

    MAX_CHUNK = 40  # 每微块最大字符数，SSE 打字机粒度

    def _publish_chunks(text: str) -> None:
        """将文本切为 ≤MAX_CHUNK 的微块逐块发布（SSE 打字机粒度）。

        参数:
            text: 待发布的文本片段（可为空串，空串不发布）。
        返回:
            无返回值。
        注意:
            event_bus 为 None 时直接返回（不发事件）。
        """
        if event_bus is None:
            return
        pos = 0
        while pos < len(text):
            micro = text[pos:pos + MAX_CHUNK]
            event_bus.publish(
                f"evt:{state.thread_id}",
                {"type": "content.chunk", "data": {"text": micro}},
            )
            pos += MAX_CHUNK

    if gateway is None:
        content = f"【Mock 文案】{title}：{info.get('selling_points', '核心卖点突出，场景化表达')}"
        _publish_chunks(content)
    else:
        content_parts: list[str] = []
        for chunk in gateway.generate_text_stream(_build_generation_prompt(state, attempts)):
            content_parts.append(chunk)
            _publish_chunks(chunk)
        content = "".join(content_parts)

    # Langgraph自动增量更新到ListState.generated_content里
    return {"generated_content": content}