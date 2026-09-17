"""llm_gateway:LLM 出站端口（只定接口，不含具体供应商实现）。

实现层见 adapters/llm_zhipu.py；以后如需扩展其他LLM也可以实现本文件即可。
本文件保持纯接口与标准库依赖。
"""
from abc import ABC, abstractmethod
from typing import Iterator


class LLMGateway(ABC):
    """LLM 大模型调用网关接口。

    协议约定：同步阻塞返回；网络 / 协议错误由实现方抛自身异常类型，
    供上层熔断与重试决策。
    """

    @abstractmethod
    def generate_text(self, prompt: str, *, model: str | None = None, temperature: float | None = None) -> str:
        """生成文案（商品主文案生成）。

        参数:
            prompt: 用户提示词。
            model: 覆盖默认模型；None 表示使用实现方默认值。
            temperature: 采样温度；None 表示使用实现方默认值。
        返回:
            生成的文案文本。
        """

    def generate_text_stream(self, prompt: str, *, model: str | None = None, temperature: float | None = None) -> Iterator[str]:
        """生成文案（流式增量）。

        默认实现回退到非流式 generate_text，一次性产出完整文本；
        支持 SSE（服务器推送事件） 的实现方应覆盖为逐块产出。

        参数:
            prompt: 用户提示词。
            model: 覆盖默认模型。
            temperature: 采样温度。
        产出:
            文本增量分片。
        """
        yield from self.generate_text(prompt, model=model, temperature=temperature)

    @abstractmethod
    def complete_json(
            self,
            prompt: str,
            *,
            schema_desc: str = "",
            json_schema: dict | None = None,
            model: str | None = None,
    ) -> dict:
        """结构化 JSON 输出（LLM Evaluator/事实核验）。

        参数:
            prompt: 用户提示词。
            schema_desc: 期望 JSON 结构的文本描述（提示用）。
            json_schema: JSON Schema；provider 原生支持时透传强约束，否则由调用方本地校验兜底。
            model: 覆盖默认模型。
        返回:
            解析后的 dict；调用方仍需本地 schema 校验。
        """

    def chat_with_tools(
            self,
            messages: list[dict],
            *,
            tools: list[dict] | None = None,
            tool_choice: str = "auto",
            model: str | None = None,
            temperature: float | None = None,
    ) -> dict:
        """带工具的对话（function calling）：模型可在回复里「点名」调用工具。

        默认实现直接抛 NotImplementedError —— 这是**刻意**的向后兼容设计：
        未实现 tools 协议的网关（含各测试用假网关）不会被强制改造，
        调用方（workflowcore/agent/loop.py）收到异常后按 stop_reason=runtime_error 优雅降级，
        因此「新增能力」不影响现有 generate_text / complete_json 链路。

        参数:
            messages: OpenAI 兼容消息列表；多轮工具调用时，内核会依次追加
                assistant（含 tool_calls）与 tool（工具结果）消息。
            tools: tools 声明列表（ports/tool.ToolSpec.to_openai_tool() 的产物）；
                None / 空列表表示本轮不带工具。
            tool_choice: 工具选择策略（默认 "auto"）。
            model: 覆盖默认模型。
            temperature: 采样温度。
        返回:
            {"content": str, "tool_calls": [...], "model": str | None}，
            tool_calls 口径见 ports/agent_runtime.normalize_tool_calls()。
        异常:
            NotImplementedError: 当前实现不支持 function calling。
            Exception: 网络/协议错误由实现方抛自身异常类型。
        注意:
            工具调用的编排、次数治理与结果回填都不属于本方法职责——
            它们分别由 workflowcore/agent/loop.py 与 middleware.py 承担。
        """
        raise NotImplementedError("当前 LLM 实现未提供 chat_with_tools（function calling）支持")
