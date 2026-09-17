"""智谱 GLM 真实 adapter（OpenAI 兼容 chat/completions，单供应商基线）。

环境变量:
    - LLM_PROVIDER：供应商选择；mock 表示显式回退内存 mock。
    - ZHIPU_API_KEY：必填；缺失时工厂返回 None（回退 mock）。
    - ZHIPU_BASE_URL：GLM 兼容网关基址（不含 /chat/completions）。
    - ZHIPU_GENERATE_MODEL：文案生成默认模型。
    - ZHIPU_EVALUATE_MODEL：JSON 评估默认模型；缺省时与生成模型一致。

职责:
    - 将 LLMGateway 端口映射到智谱 OpenAI 兼容 HTTP 协议（非流式 + SSE 服务器流式推送事件）。
    - 并提供 chat_with_tools（function calling）：请求携带 tools / tool_choice，
      响应解析 choices[0].message.tool_calls，供 workflowcore/agent 的 Agent 循环使用。
    - 仅依赖标准库，保持 ai-engine 无新增第三方依赖。
"""

from __future__ import annotations
import os
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from typing import Iterator, Optional

from ..ports.llm_gateway import LLMGateway
from ..ports.agent_runtime import normalize_tool_calls

# DNS 预解析超时（秒）：getaddrinfo 获取host address 不受 urlopen 的 socket timeout 约束，
# 弱网/沙箱下可能无限期挂起，导致生成任务永不返回。
DNS_RESOLVE_TIMEOUT_SECONDS = 8.0

# 传输与 DNS 的模块级别名：运行期 _urlopen 会被下方覆盖为带预检的实现，
# 单测可替换这两个名字以注入假 HTTP / 假解析。
_urlopen = urllib.request.urlopen
_resolve = socket.getaddrinfo

# 限时 DNS 解析专用线程池：2 个 worker，避免解析阻塞主流程。
_dns_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zhipu-dns")


class LLMZhipuError(RuntimeError):
    """智谱 API 调用异常（HTTP / 网络 / DNS / 响应格式）。"""


def _resolve_host_with_timeout(host: str) -> None:
    """限时预解析主机名，避免 getaddrinfo 无限阻塞主流程。

    参数:
        host: 待解析的域名。
    异常:
        LLMZhipuError: 解析超时或失败；调用方据此快速失败，不进入 HTTP 阶段。
    """
    # 将 socket.getaddrinfo 提交到线程池执行，主线程限时等待结果。
    fut = _dns_executor.submit(_resolve, host, None)
    try:
        fut.result(timeout=DNS_RESOLVE_TIMEOUT_SECONDS)
    except _FutureTimeout as exc:
        raise LLMZhipuError(
            f"解析 LLM 域名超时（{host} > {DNS_RESOLVE_TIMEOUT_SECONDS:.0f}s），请检查网络/DNS"
        ) from exc
    except OSError as exc:
        raise LLMZhipuError(f"解析 LLM 域名失败（{host}）: {exc}") from exc


def _open_with_dns_preflight(req: urllib.request.Request, timeout: Optional[float] = None):
    """真实传输入口：先限时解析域名，再走 urllib；DNS 故障快速失败而非无限挂起。

    参数:
        req: 已构造好的请求对象。
        timeout: 传给 urlopen 的 socket 超时（秒）。
    返回:
        urlopen 返回的响应上下文管理器。
    异常:
        LLMZhipuError: 域名解析失败或超时。
    """
    # 从请求 URL 中取出主机名；无主机名（异常 URL）时跳过预检。
    host = urllib.parse.urlparse(req.full_url).hostname
    if host:
        _resolve_host_with_timeout(host)
    # 解析成功后才真正发起 HTTP 请求。
    return urllib.request.urlopen(req, timeout=timeout)


_urlopen = _open_with_dns_preflight


def _extract_json(text: str) -> dict:
    """从 LLM 返回文本中提取 JSON；解析失败时返回“不通过”兜底结构。

    容错策略：先直接 json.loads；若带 ```json 围栏则剥离围栏后再解析。

    参数:
        text: LLM 原始输出。
    返回:
        解析出的 dict；非对象或解析失败时返回含 passed/score/violations 的兜底结构。
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").strip().removesuffix("```").strip()
    try:
        # 反序列化后仅接受 dict；非对象（如数组）按“不通过”结构返回。
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {"passed": False, "score": 0.0,
                                                  "violations": [{"reason": "非 JSON 对象", "keyword": ""}],
                                                  "facts_checked": [], "errors": ["LLM 返回非对象"]}
    except json.JSONDecodeError:
        # 解析失败（大模型输出非 JSON 或乱码）同样返回“不通过”结构。
        return {"passed": False, "score": 0.0, "violations": [{"reason": raw[:200], "keyword": ""}],
                "facts_checked": [], "errors": ["LLM 输出无法解析为 JSON"]}


def _build_schema_hint(schema_desc: str, json_schema: dict | None) -> str:
    """生成结构化输出的文本约束。

    智谱 OpenAI 兼容接口仅支持 response_format={"type": "json_object"}，
    官方做法是在系统消息中描述期望的 JSON 结构；原生 json_schema 强校验
    由调用方本地完成（见 ports/llm_gateway.LLMGateway.complete_json）。

    参数:
        schema_desc: 期望 JSON 结构的文本描述，优先使用。
        json_schema: JSON Schema；schema_desc 为空时序列化为文本兜底。
    返回:
        拼接到系统消息尾部的约束文本；两者均为空时返回空串。
    """
    if schema_desc:
        return f"输出必须是合法 JSON，并严格符合以下结构说明：\n{schema_desc}"
    if json_schema is not None:
        return ("输出必须是合法 JSON，并严格符合以下 JSON Schema：\n"
                + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":")))
    return ""


_SYSTEM_PROMPT = "你是专业的中文电商文案撰写与合规评估助手，严格按用户要求输出。"


def _system_content(schema_hint: str = "") -> str:
    """拼装系统消息：基础人设 + 可选的结构化输出约束。

    参数:
        schema_hint: 结构化输出约束文本（如 EVALUATION_SCHEMA_DESC）；空串表示不附加。
    返回:
        系统消息文本。
    """
    if schema_hint:
        return f"{_SYSTEM_PROMPT}\n\n{schema_hint}"
    return _SYSTEM_PROMPT


def _build_payload(prompt: str, *, model: str, temperature: float, stream: bool,
                   json_mode: bool = False, schema_hint: str = "") -> dict:
    """构造 chat/completions 请求体（非流式与流式共用，避免两条路径逻辑漂移）。

    参数:
        prompt: 用户提示词。
        model: 请求使用的模型。
        temperature: 采样温度。
        stream: 是否开启 SSE 流式。
        json_mode: 为 True 时携带 response_format=json_object。
        schema_hint: 结构化输出约束文本，拼接到系统消息尾部。
    返回:
        可直接 json.dumps 的请求体 dict。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_content(schema_hint)},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "stream": stream,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _build_request(base_url: str, payload: dict, api_key: str, *, stream: bool) -> urllib.request.Request:
    """构造 HTTP POST 请求（含鉴权头；流式额外声明 Accept: text/event-stream）。

    参数:
        base_url: 兼容网关基址。
        payload: _build_payload 产出的请求体。
        api_key: 智谱 API Key。
        stream: 是否流式；流式需声明 Accept 以防网关/客户端缓冲整段响应。
    返回:
        已就绪的请求对象。
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _build_tool_payload(messages: list[dict], *, model: str, temperature: float,
                        tools: list[dict] | None, tool_choice: str) -> dict:
    """构造带 tools 的 chat/completions 请求体（OpenAI 兼容 function calling 协议）。

    参数:
        messages: 已拼好的消息列表（system/user/assistant(含 tool_calls)/tool）。
        model: 本次请求使用的模型。
        temperature: 采样温度。
        tools: tools 声明数组（ports/tool.ToolSpec.to_openai_tool() 的产物）。
        tool_choice: 工具选择策略（默认 "auto"）；tools 为空时不下发该字段。
    返回:
        可直接 json.dumps 的请求体 dict。
    注意:
        与 _build_payload 分开实现（而不是加参数）：现有文本/JSON 两条路径的报文形状
        保持不变，避免「加一个能力、动三条链路」。
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    return payload


def _parse_assistant_message(message: dict) -> dict:
    """解析 choices[0].message → 统一口径的助手轮次。

    参数:
        message: OpenAI 兼容响应里的 message 对象（含 content / tool_calls）。
    返回:
        {"content": str, "tool_calls": [{"id","name","arguments"}]}；
        tool_calls 经 ports.agent_runtime.normalize_tool_calls 收敛（参数 JSON 字符串已解析）。
    """
    return {
        "content": str(message.get("content") or "").strip(),
        "tool_calls": normalize_tool_calls(message.get("tool_calls")),
    }


def _raise_for_url_error(exc: urllib.error.URLError) -> None:
    """统一 HTTP/网络错误映射（把 urllib 异常收敛为 LLMZhipuError）。

    HTTPError 是 URLError 子类：优先按 HTTP 状态报告，其余按网络错误报告。

    参数:
        exc: urllib 抛出的异常（HTTPError 或一般 URLError）。
    异常:
        LLMZhipuError: 始终抛出；消息携带 HTTP 状态码 + 最多 200 字节响应体，或网络错误原因。
    注意:
        本函数不返回（纯收敛后抛出），调用方无需判断返回值。
    """
    if isinstance(exc, urllib.error.HTTPError):
        # 只读取 200 字节（足够携带错误信息），非 UTF-8 字节以 replace 兜底。
        detail = exc.read(200).decode("utf-8", "replace")
        raise LLMZhipuError(f"zhipu HTTP {exc.code}: {detail}") from exc
    raise LLMZhipuError(f"zhipu 网络错误: {exc.reason}") from exc


def build_zhipu_gateway_from_env(
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    gen_model: str | None = None,
    eval_model: str | None = None,
) -> LLMZhipuGLMGateway | None:
    """按入参/环境变量构建智谱 Gateway；未配置时返回 None，由上层回退 mock。

    参数:
        provider / api_key / base_url / gen_model / eval_model: 显式覆盖值；为 None 时
            分别读取 LLM_PROVIDER / ZHIPU_API_KEY / ZHIPU_BASE_URL /
            ZHIPU_GENERATE_MODEL / ZHIPU_EVALUATE_MODEL。
    返回:
        LLMZhipuGLMGateway 实例；None 表示未配置（回退内存 mock）。
    """
    provider = provider if provider is not None else os.getenv("LLM_PROVIDER", "")
    if provider == "mock":
        return None
    key = api_key if api_key is not None else os.getenv("ZHIPU_API_KEY")
    if not key:
        return None
    if provider and provider != "zhipu":
        # 当前仅支持单个供应商。
        return None
    return LLMZhipuGLMGateway(
        api_key = key,
        base_url=base_url or os.getenv("ZHIPU_BASE_URL"),
        model=gen_model or os.getenv("ZHIPU_GENERATE_MODEL"),
        eval_model=eval_model or os.getenv("ZHIPU_EVALUATE_MODEL") or None,
    )


class LLMZhipuGLMGateway(LLMGateway):
    """智谱 GLM 同步实现（端口签名为同步协议）。

    网络超时 / HTTP 错误统一抛 LLMZhipuError，供上层熔断与重试决策。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        eval_model: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        """初始化同步 Gateway。

        参数:
            api_key: 智谱 API Key，必填。
            base_url: 兼容网关基址（末尾斜杠会被去除）。
            model: 默认生成模型。
            eval_model: 默认评估模型；缺省时回落到 model。
            timeout: 单次 HTTP 请求超时（秒）。
        异常:
            ValueError: api_key 为空。
        """
        if not api_key:
            raise ValueError("LLMZhipuGLMGateway 需要 api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.eval_model = eval_model or model
        self.timeout = timeout

    def _chat(self, prompt: str, *, model: str, temperature: float,
              json_mode: bool, schema_hint: str = "") -> str:
        """非流式 chat/completions 调用，返回首条回复文本。

        参数:
            prompt: 用户提示词。
            model: 本次请求使用的模型。
            temperature: 采样温度。
            json_mode: 为 True 时携带 response_format=json_object。
            schema_hint: 结构化输出的文本约束，拼接到系统消息尾部。
        返回:
            去除首尾空白后的回复文本。
        异常:
            LLMZhipuError: 网络错误、HTTP 错误或响应结构缺失。
        """
        # 结构化约束经系统消息下发（智谱无原生 json_schema，仅支持 json_object）。
        payload = _build_payload(prompt, model=model, temperature=temperature,
                                 stream=False, json_mode=json_mode, schema_hint=schema_hint)
        req = _build_request(self.base_url, payload, self.api_key, stream=False)
        # 发起 HTTP 请求并读取响应体。
        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            # HTTPError 是 URLError 子类，统一在映射函数中按 HTTP 状态区分。
            _raise_for_url_error(exc)
        data = json.loads(body)
        # 二次拦截：部分网关把错误放在 200 响应体的 error 字段中。
        if data.get("error"):
            raise LLMZhipuError(f"zhipu error: {data['error']}")
        try:
            # OpenAI 协议非流式标准字段：choices[0].message.content。
            content = data["choices"][0]["message"]["content"]
        except(KeyError, IndexError, TypeError) as exc:
            raise LLMZhipuError(f"zhipu 响应缺少 choices[0].message.content") from exc
        return str(content).strip()

    def _chat_stream(self, prompt: str, *, model: str, temperature: float) -> Iterator[str]:
        """流式 chat/completions 调用，逐块产出 SSE 增量文本。

        参数:
            prompt: 用户提示词。
            model: 本次请求使用的模型。
            temperature: 采样温度。
        产出:
            每个 SSE data 块中 choices[0].delta.content 的非空增量。
        异常:
            LLMZhipuError: 网络错误或 HTTP 错误（在首次迭代时抛出）。
        """
        payload = _build_payload(prompt, model=model, temperature=temperature, stream=True)
        req = _build_request(self.base_url, payload, self.api_key, stream=True)

        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                # 逐行读取：跳过空行与 [DONE] 结束（流式标准结束符）标志。
                for line in resp:
                    line = line.decode("utf-8").strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        data = json.loads(line)
                        # SSE 流式标准增量格式 choices[0].delta.content。
                        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError):
                        continue
        except urllib.error.URLError as exc:
            # HTTPError 是 URLError 子类，统一在映射函数中按 HTTP 状态区分。
            _raise_for_url_error(exc)

    def generate_text(self, prompt: str, *, model: str | None = None, temperature: float | None = None) -> str:
        """生成文案（非流式）。

        参数:
            prompt: 用户提示词。
            model: 覆盖默认生成模型。
            temperature: 采样温度；None 表示使用实现方默认值 0.7（智谱区间 (0,1]）。
        返回:
            模型输出的文案文本。
        """
        return self._chat(
            prompt,
            model=model or self.model,
            temperature=0.7 if temperature is None else temperature,
            json_mode=False,
        )

    def generate_text_stream(self, prompt: str, *, model: str | None = None, temperature: float | None = None) -> Iterator[str]:
        """流式生成：逐块返回 tokens（用于 SSE 实时打字机效果）。

        参数:
            prompt: 用户提示词。
            model: 覆盖默认生成模型。
            temperature: 采样温度；None 表示使用实现方默认值 0.7（智谱区间 (0,1]）。
        产出:
            文本增量分片。
        """
        yield from self._chat_stream(
            prompt,
            model=model or self.model,
            temperature=0.7 if temperature is None else temperature,
        )

    def complete_json(self, prompt: str, *, schema_desc: str = "",
                      json_schema: dict | None = None, model: str | None = None) -> dict:
        """请求 json_object 结构化输出；解析失败时返回“不通过”结构。

        兜底目的是让评估节点安全走 Reflection/HITL；本地强 schema 校验
        （schemas.EvalOutput）由 evaluate 节点负责。智谱 OpenAI 兼容接口无原生
        json_schema 支持，故 schema_desc / json_schema 统一转为系统消息中的文本
        约束下发（json_schema 仅在 schema_desc 为空时兜底序列化）。

        参数:
            prompt: 用户提示词。
            schema_desc: 期望 JSON 结构的文本描述，作为系统消息约束下发。
            json_schema: JSON Schema；schema_desc 为空时序列化后作为约束下发，
                最终强校验仍由调用方本地完成。
            model: 覆盖默认评估模型。
        返回:
            经 _extract_json 解析后的 dict。
        """
        content = self._chat(
            prompt,
            model=model or self.eval_model,
            json_mode=True,
            temperature=0.7,
            schema_hint=_build_schema_hint(schema_desc, json_schema),
        )
        return _extract_json(content)

    def chat_with_tools(self, messages: list[dict], *, tools: list[dict] | None = None,
                        tool_choice: str = "auto", model: str | None = None,
                        temperature: float | None = None) -> dict:
        """带工具的多轮对话（function calling）：模型可返回 tool_calls 请求调用工具。

        智谱 OpenAI 兼容协议与官方一致：请求携带 tools / tool_choice，
        响应取 choices[0].message（content + 可选的 tool_calls[].function.{name,arguments}）。

        参数:
            messages: OpenAI 兼容消息列表；多轮时由 Agent 循环依次追加
                assistant（含 tool_calls）与 tool（工具结果）消息。
            tools: tools 声明数组；None/空表示本轮不带工具（退化为纯文本对话）。
            tool_choice: 工具选择策略（默认 "auto"）。
            model: 覆盖默认生成模型。
            temperature: 采样温度；None 时用 0.2（工具选择场景偏确定性）。
        返回:
            {"content": str, "tool_calls": [{"id","name","arguments"}], "model": str}。
        异常:
            LLMZhipuError: 网络/HTTP 错误、响应缺 choices[0].message 或网关 error 字段。
        注意:
            · 复用既有的 DNS 预检、超时与错误映射（_urlopen / _raise_for_url_error），
              不新增传输实现；
            · 工具执行与次数治理不在本方法内（见 workflowcore/agent/loop.py 与 middleware.py）。
        """
        payload = _build_tool_payload(
            messages,
            model=model or self.model,
            temperature=0.2 if temperature is None else temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        req = _build_request(self.base_url, payload, self.api_key, stream=False)
        try:
            with _urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            # HTTPError 是 URLError 子类，统一在映射函数中按 HTTP 状态区分。
            _raise_for_url_error(exc)
        data = json.loads(body)
        if data.get("error"):
            raise LLMZhipuError(f"zhipu error: {data['error']}")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMZhipuError("zhipu 响应缺少 choices[0].message") from exc
        parsed = _parse_assistant_message(message)
        parsed["model"] = str(data.get("model") or payload["model"])
        return parsed


__all__ = ["LLMZhipuError", "LLMZhipuGLMGateway", "build_zhipu_gateway_from_env"]
