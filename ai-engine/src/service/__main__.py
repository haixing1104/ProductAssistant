"""ListingWorker 守护入口：`cd ai-engine && python -m src.service`。

职责：阻塞式消费 job:generate / job:approval / job:product_purge，驱动 ListingWorkflow。

权限模型（database/sql/0002_roles_grants.sql）:
  · AI_ENGINE_SETUP_PG_DSN = role_pa_ai_setup（仅供首次 .setup() 建 checkpoint 表，需 CREATE）
  · AI_ENGINE_PG_DSN       = role_pa_ai（运行期读写 checkpoint 与 schema_pa_ai 业务表，仅 DML）

环境变量:
  PA_ENV                  环境标识，参与 Redis 键前缀 pa:{env}:...（默认 dev）
  REDIS_URL               Redis 连接串（默认 redis://localhost:6379/0）
  AI_ENGINE_PG_DSN        运行期 DSN；缺省时用 POSTGRES_* + role_pa_ai / ROLE_PA_AI_PWD 拼装
  AI_ENGINE_SETUP_PG_DSN  建表 DSN；缺省时用 POSTGRES_* + role_pa_ai_setup / ROLE_PA_AI_SETUP_PWD 拼装
  AI_ENGINE_RULE_PRECHECK 置 1/true/yes 时对商品标题做阻断级违禁词预检（默认关）
  —— Redis Streams 可靠性（PEL 回收 / 死信 / 流保留 / 心跳）——
  AI_ENGINE_PEL_MIN_IDLE_MS      PEL 滞留判定（毫秒，默认 60000）：空闲超过该值的未 ack 消息回收重试
  AI_ENGINE_PEL_CLAIM_BATCH      每轮单次回收条数上限（默认 10；0 = 关闭回收）
  AI_ENGINE_MAX_DELIVERIES       毒消息上限（默认 5）：达到后转 pa:{env}:dlq:{流} 并 ack 原消息
  AI_ENGINE_JOB_LOCK_TTL_SECONDS 生成任务线程锁 TTL（默认 300）：兼作回收时「仍在处理中」判据
  AI_ENGINE_STREAM_MAXLEN_JOB    job/result 流保留上限（默认 10000，近似裁剪）
  AI_ENGINE_STREAM_MAXLEN_EVT    evt 流保留上限（默认 2000，只影响断线回放深度）
  AI_ENGINE_EVT_TTL_SECONDS      evt 流 TTL（默认 604800 = 7 天，每次发布刷新）
  AI_ENGINE_DONE_TTL_SECONDS     终态幂等标记存活期（默认 604800，仅 published 写入）
  AI_ENGINE_HEARTBEAT_TTL_SECONDS 心跳键 TTL（默认 30；键过期即代表消费停滞）
  AI_ENGINE_AGENT_ENABLED Agent 研究节点开关（function calling 只读取数；**默认开**，
                          置 0/false/no 关闭；未配置 LLM 凭据时节点自动 no-op，不会报错）
  AI_ENGINE_AGENT_MAX_TURNS      Agent 模型调用上限（**默认 2**，预算熔断阈值）
  AI_ENGINE_AGENT_MAX_TOOL_CALLS Agent 工具调用上限（**默认 3**）
  IMAGE_NORMALIZE_ENABLED  置 0/false/no 关闭图片规格化（AI 图与上传图；默认开启）
  IMAGE_NORMALIZE_SIZE     规格化目标尺寸（默认 1000x1000，1:1）
  IMAGE_NORMALIZE_FORMAT   规格化输出格式 jpeg|png|webp（默认 jpeg）
  IMAGE_NORMALIZE_QUALITY  规格化输出质量 1-100（默认 85；png 忽略）
  IMAGE_NORMALIZE_BACKGROUND 补边背景色（默认 #FFFFFF）
  IMAGE_NORMALIZE_UPSCALE  置 1/true/yes 允许放大（默认关，避免小图被插值放大变糊）
  ZHIPU_IMAGE_WATERMARK    置 1/true/yes 保留平台水印（默认关闭；需账户已签免责声明）
  ZHIPU_IMAGE_MODEL        生图模型（glm-image 与 cogview 系尺寸规则不同，见适配器）

优雅停机：SIGINT/SIGTERM → 退出消费循环 → worker.close() 释放 checkpointer 连接池 → 关 Redis。
"""

from __future__ import annotations

import os
import signal
import time

import redis

from ..adapters.redis_eventbus import RedisKeys
from .worker import ListingWorker, _default_consumer_name


def _derive_role_dsn(role: str, password_env_var: str) -> str | None:
    """按 infra/.env 的键拼装某数据库角色的 DSN（密码缺失时返回 None）。

    参数:
        role: 数据库角色名（role_pa_ai / role_pa_ai_setup）。
        password_env_var: 该角色密码所在环境变量名。
    返回:
        DSN 字符串；对应密码未配置时返回 None（由调用方决定是报错还是退回显式 DSN）。
    """
    password = os.getenv(password_env_var)
    if not password:
        return None
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "productassistant")
    return f"postgresql://{role}:{password}@{host}:{port}/{name}"


def _agent_enabled_from_env() -> bool:
    """Agent 研究节点开关（**默认开**；置 0/false/no 关闭）。

    为什么单独抽成函数（而不是内联在 main() 里）:
        默认值是「行为开关」，必须能被单测钉住 —— 内联时只能靠启动 main() 人工观察，
        谁把默认值改回 "0" 都不会被任何测试发现（回归保护见 tests/test_agent_default.py）。

    返回:
        True: 启用 Agent 研究（装配 AgentRuntime，agent_research 节点参与链路）；
        False: 关闭（不装配 runtime → 节点 no-op，行为与未接入 Agent 时完全一致）。
    注意:
        · 取值口径与仓库其它「默认开」开关保持一致（见 adapters/image_pillow._truthy）：
          **未配置或纯空白 → 用默认（开）**；`1/true/yes/on`（大小写与两侧空白容错）→ 开；
          其余（含 `0/false/no/off`）→ 关 —— 拼写错误宁可少跑（省 token）不可多花钱；
        · 本开关只决定「是否装配 Agent 运行时」。即使返回 True，若没有可用 LLM 凭据，
          build_agent_runtime_from_env() 仍返回 None → 节点 no-op（第二道门，不报错）。
    """
    raw = os.getenv("AI_ENGINE_AGENT_ENABLED")
    if raw is None or not raw.strip():
        return True  # 未配置 / 空白 → 默认开
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """读取整数型环境变量（未配置/空白/非法值一律回落到默认值）。

    参数:
        name: 环境变量名。
        default: 缺省值。
    返回:
        解析后的整数；解析失败时打印提示并返回 default —— 一个手写错的数字不该让 worker 起不来。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        print(f"[worker] 环境变量 {name}={raw!r} 不是整数，回落到默认值 {default}")
        return default


def main() -> None:
    """装配依赖并进入消费循环（阻塞）。

    异常:
        SystemExit: 运行期 DSN 既不显式配置也无法从 POSTGRES_* / ROLE_PA_AI_PWD 拼装。
    """
    env = os.getenv("PA_ENV", "dev")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    rule_precheck = os.getenv("AI_ENGINE_RULE_PRECHECK", "0").lower() in {"1", "true", "yes"}
    # Agent 研究（默认开）：开启后会在图内产生额外的模型调用与只读取数 ——
    # 默认预算已相应收紧为 2 轮模型调用 / 3 次工具（见 workflowcore/agent/middleware.py 的常量）；
    # 置 AI_ENGINE_AGENT_ENABLED=0 可关闭（一行 env 即恢复「与未接入 Agent 时完全一致」）。
    agent_enabled = _agent_enabled_from_env()
    agent_max_turns = int(os.getenv("AI_ENGINE_AGENT_MAX_TURNS", "2") or 2)
    agent_max_tool_calls = int(os.getenv("AI_ENGINE_AGENT_MAX_TOOL_CALLS", "3") or 3)
    # Redis Streams 可靠性参数（默认值即推荐值；生产建议按实测任务时长调整 PEL 空闲阈值与锁 TTL）
    pel_min_idle_ms = _env_int("AI_ENGINE_PEL_MIN_IDLE_MS", 60000)
    pel_claim_batch = _env_int("AI_ENGINE_PEL_CLAIM_BATCH", 10)
    max_deliveries = _env_int("AI_ENGINE_MAX_DELIVERIES", 5)
    job_lock_ttl_seconds = _env_int("AI_ENGINE_JOB_LOCK_TTL_SECONDS", 300)
    stream_maxlen_job = _env_int("AI_ENGINE_STREAM_MAXLEN_JOB", 10000)
    stream_maxlen_evt = _env_int("AI_ENGINE_STREAM_MAXLEN_EVT", 2000)
    evt_ttl_seconds = _env_int("AI_ENGINE_EVT_TTL_SECONDS", 604800)
    done_ttl_seconds = _env_int("AI_ENGINE_DONE_TTL_SECONDS", 604800)
    heartbeat_ttl_seconds = _env_int("AI_ENGINE_HEARTBEAT_TTL_SECONDS", 30)
    # 消费者名（PEL 归属标识）：多实例必须唯一，hostname+pid 天然唯一
    consumer = _default_consumer_name()

    runtime_dsn = os.getenv("AI_ENGINE_PG_DSN") or _derive_role_dsn("role_pa_ai", "ROLE_PA_AI_PWD")
    setup_dsn = os.getenv("AI_ENGINE_SETUP_PG_DSN") or _derive_role_dsn(
        "role_pa_ai_setup", "ROLE_PA_AI_SETUP_PWD"
    )
    if not runtime_dsn:
        raise SystemExit(
            "缺少运行期数据库 DSN：请设置 AI_ENGINE_PG_DSN（role_pa_ai），"
            "或提供 POSTGRES_HOST/POSTGRES_PORT/POSTGRES_DB + ROLE_PA_AI_PWD 以便拼装。"
        )
    if not setup_dsn:
        # 建表是一次性动作：生产可只给运行期 DSN（表由发布流程/迁移预先建好）
        print("[worker] 未提供 AI_ENGINE_SETUP_PG_DSN：跳过 checkpoint 建表（假定表已存在）")

    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=3.0,
        socket_timeout=10.0,  # > 单次 XREADGROUP BLOCK 上限，避免阻塞读被误判超时
        health_check_interval=30,
        retry_on_timeout=False,
    )

    # 惰性构建真实适配器：未配置凭据即返回 None，由节点回退确定性 mock（不影响启动）
    from ..adapters.gateway_agent_runtime import build_agent_runtime_from_env
    from ..adapters.image_pillow import build_image_normalizer_from_env
    from ..adapters.llm_image_cogview import build_image_cogview_gateway_from_env
    from ..adapters.llm_zhipu import build_zhipu_gateway_from_env
    from ..adapters.milvus_rag_store import build_milvus_rag_store_from_env
    from ..adapters.oss import build_oss_storage_from_env

    # Agent 研究（默认开）：关闭时不装配（零成本短路）；启用但缺凭据 → 节点 no-op。
    # 启动提示是刻意的（可观测性）：默认开之后必须让人知道 Agent 在跑、预算多少、如何关。
    agent_runtime = build_agent_runtime_from_env() if agent_enabled else None
    if not agent_enabled:
        print("[worker] Agent 研究已关闭（AI_ENGINE_AGENT_ENABLED=0）→ agent_research 节点 no-op")
    elif agent_runtime is None:
        print(
            "[worker] Agent 研究已启用但无可用 LLM 凭据 → 节点将 no-op"
            "（需 ZHIPU_API_KEY；如需显式关闭请设 AI_ENGINE_AGENT_ENABLED=0）"
        )
    else:
        print(
            f"[worker] Agent 研究已启用（max_turns={agent_max_turns}, "
            f"max_tool_calls={agent_max_tool_calls}）；如需关闭请设 AI_ENGINE_AGENT_ENABLED=0"
        )

    worker = ListingWorker(
        redis_client=client,
        keys=RedisKeys(env),
        runtime_pg_dsn=runtime_dsn,
        setup_pg_dsn=setup_dsn,
        llm_gateway=build_zhipu_gateway_from_env(),
        rag_store=build_milvus_rag_store_from_env(),
        image_gateway=build_image_cogview_gateway_from_env(),
        object_storage=build_oss_storage_from_env(),
        image_normalizer=build_image_normalizer_from_env(),
        rule_precheck=rule_precheck,
        agent_runtime=agent_runtime,
        agent_max_turns=agent_max_turns,
        agent_max_tool_calls=agent_max_tool_calls,
        consumer=consumer,
        pel_min_idle_ms=pel_min_idle_ms,
        pel_claim_batch=pel_claim_batch,
        max_deliveries=max_deliveries,
        job_lock_ttl_seconds=job_lock_ttl_seconds,
        stream_maxlen_job=stream_maxlen_job,
        stream_maxlen_evt=stream_maxlen_evt,
        evt_ttl_seconds=evt_ttl_seconds,
        done_ttl_seconds=done_ttl_seconds,
        heartbeat_ttl_seconds=heartbeat_ttl_seconds,
    )
    print(
        f"[worker] env={env} redis={redis_url} consumer={consumer} 开始消费 "
        "job:generate / job:approval / job:product_purge（Ctrl+C 退出）"
    )
    # 可靠性参数刻意打印：默认开的能力必须让人一眼看到生效值与调参入口（同 Agent 日志的取舍）
    print(
        f"[worker] 可靠性：PEL 回收(空闲>{pel_min_idle_ms}ms, 单批{pel_claim_batch}) "
        f"死信阈值={max_deliveries} 线程锁={job_lock_ttl_seconds}s "
        f"流保留(job={stream_maxlen_job}, evt={stream_maxlen_evt}, evt_ttl={evt_ttl_seconds}s) "
        f"幂等标记TTL={done_ttl_seconds}s 心跳TTL={heartbeat_ttl_seconds}s "
        f"死信流=pa:{env}:dlq:*"
    )

    running = True

    def _stop(_signum, _frame) -> None:  # noqa: ANN001 - signal handler 签名
        """SIGINT/SIGTERM 处理器：只置退出标志，真正的资源释放在 main() 的 finally。

        参数:
            _signum: 信号编号（本处理器不使用，仅为 signal 回调签名所需）。
            _frame: 当前栈帧（本处理器不使用，仅为 signal 回调签名所需）。
        返回:
            无返回值。
        """
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    loop_count = 0
    try:
        while running:
            loop_count += 1
            # 三类任务每轮各取一条：消费组按 thread_id 幂等驱动/恢复
            try:
                worker.consume_generate_once(block_ms=1000)
                worker.consume_approval_once(block_ms=1000)
                worker.consume_product_purge_once(block_ms=1000)
            except Exception as exc:  # noqa: BLE001 基础设施异常跳过本轮，消息仍在流中可重投
                print(f"[worker] 消费异常（跳过本轮）: {exc!r}")
            # 心跳：让外部能区分「进程活着」与「消费停滞」（键随 TTL 过期即代表停滞）
            try:
                worker.heartbeat()
            except Exception as exc:  # noqa: BLE001 心跳失败不影响消费主流程
                print(f"[worker] 心跳刷新失败（忽略）: {exc!r}")
            # 周期性待处理读数（每 20 轮 ≈ 1 分钟）：pending 持续增长 = 消费能力不足或消息卡死
            if loop_count % 20 == 0:
                try:
                    print(f"[worker] 待处理读数 {worker.pending_stats()}")
                except Exception as exc:  # noqa: BLE001 观测失败不影响消费
                    print(f"[worker] 待处理读数失败（忽略）: {exc!r}")
            # 空轮询轻量退避，避免空转打满 CPU
            time.sleep(0.1)
    finally:
        # 先放 checkpointer 连接池再关 Redis：进程退出前释放全部外部连接
        worker.close()
        client.close()
        print("[worker] 已优雅关闭")


if __name__ == "__main__":
    main()
