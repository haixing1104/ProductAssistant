"""PostgreSQL 真实适配器（ai-engine 域）：content/eval 落库 + 商品只读 + LangGraph checkpoint。

职责:
    1. 内容落库：实现 ports/content_store.py 的 ContentStore，把 AI 生成内容持久化到
       pgsql/productassistant/schema_pa_ai.product_contents；
    2. 评估落库：实现 ports/eval_log_store.py 的 EvalLogStore，把评估结果持久化到
       pgsql/productassistant/schema_pa_ai.evaluation_logs；
    3. 商品只读：ai-engine 仅以 SELECT 读取 schema_pa_backend.products 作为生成素材；
    3.b 业务只读汇总（PgBusinessReader）：把 AI 有权读到的商品素材 / 历史文案版本 /
       历史评估 / 历史审批收敛成 ports/business_reader.py 的只读端口，供 Agent 工具取数；
    4. LangGraph checkpoint 建表：ensure_checkpoint_schema() 以 role_pa_ai_setup 建表（幂等，
       表由 PostgresSaver 建在 schema_pa_ai）；运行期读写用的 saver 由
       workflowcore/graph.new_pg_checkpointer()（role_pa_ai + 连接池）提供——本模块只承担
       「需要 CREATE 权限的 DDL」这一动作，运行期连接生命周期不在这里。
    5. 清理辅助：list_pa_thread_ids() 反查某商品关联的 thread_id，供删除商品时清理
       checkpoint 系列表（LangGraph saver.delete_thread）。

数据库角色与权限（与 database/sql/0002_roles_grants.sql 一一对应）:
    - role_pa_ai：schema_pa_ai 全 DML；schema_pa_backend.products / hitl_approvals 仅 SELECT；
    - role_pa_ai_setup：schema_pa_ai 的 USAGE + CREATE，仅供 PostgresSaver.setup() 建表；
    - checkpoint 表由 role_pa_ai_setup 创建，并经 DEFAULT PRIVILEGES 自动授权给 role_pa_ai，
      运行期读写因此可以切回 role_pa_ai。

"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..ports import BusinessReader, ContentRecord, EvalRecord

if TYPE_CHECKING:
    # 仅类型检查期引入：不跑 checkpoint 的场景无需在运行期依赖 langgraph-checkpoint-postgres
    from langgraph.checkpoint.postgres import PostgresSaver

# 数据库里的AI层SCHEMA：业务表与 LangGraph checkpoint 表都落在这里
CHECKPOINT_SCHEMA = "schema_pa_ai"

# 版本号冲突兜底重试次数（正常路径已由事务级咨询锁串行化，重试只兜极端情形）
_MAX_VERSION_RETRIES = 3


def _connect(dsn: str) -> psycopg.Connection:
    """按 DSN 新建连接（autocommit + 默认 tuple 行工厂）。

    参数:
        dsn: PostgreSQL 连接串；生产环境应为 role_pa_ai 角色。
    返回:
        psycopg.Connection：autocommit 连接。调用方用 with 托管即可，
        退出 with 时提交（异常则回滚）并关闭连接，不可跨操作复用。
    """
    return psycopg.connect(dsn, autocommit=True)


def _saver_for(conn: psycopg.Connection) -> PostgresSaver:
    """用已设好 search_path 的连接构造 PostgresSaver（函数内延迟 import）。

    参数:
        conn: 已执行 `SET search_path` 的连接。
    返回:
        PostgresSaver 实例；此处不建表，建表见 ensure_checkpoint_schema()。
    注意:
        延迟 import 是为了避免运行期在不使用 checkpoint 的场景强依赖该第三方包。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    return PostgresSaver(conn)


def ensure_checkpoint_schema(setup_dsn: str) -> None:
    """初始化 LangGraph checkpoint 相关表结构（幂等，可重复执行）。

    参数:
        setup_dsn: 建表专用 DSN，应配置为 role_pa_ai_setup
            （schema_pa_ai 具备 USAGE + CREATE，见 database/sql/0002_roles_grants.sql）。
    返回:
        无返回值。
    异常:
        psycopg.Error: 连接失败或权限不足（例如误用 role_pa_ai 建表会报 insufficient_privilege）。
    注意:
        - PostgresSaver.setup() 的迁移 SQL 使用非限定表名，所以必须先 SET search_path，
          才能把 checkpoint 表建到 schema_pa_ai（本函数保留了该步骤，勿删）；
        - 用 with 托管连接，setup 结束即关闭连接（建表属一次性动作，不保留长连接）；
        - 运行期读写 checkpoint 请另用 workflowcore/graph.new_pg_checkpointer()
          （role_pa_ai + 连接池），不要复用此处的一次性建表连接。
    """
    with psycopg.connect(setup_dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"SET search_path TO {CHECKPOINT_SCHEMA}, public")
        _saver = _saver_for(conn)
        # 创建 LangGraph checkpoint 所需的表（表已存在时自动跳过）
        _saver.setup()


class PgContentStore:
    """ContentStore 的 PostgreSQL 实现：把 ContentRecord 写入 schema_pa_ai.product_contents。

    权限依据：role_pa_ai 对该表全 DML（ai-engine 独占写），role_pa_backend 仅 SELECT
    （见 database/sql/0002_roles_grants.sql），因此本适配器只应由 ai-engine 侧调用。
    """

    def __init__(self, dsn: str):
        """初始化。

        参数:
            dsn: PostgreSQL 连接串；应为 role_pa_ai 角色。
        """
        self._dsn = dsn

    def save(self, record: ContentRecord) -> ContentRecord:
        """持久化一条生成内容，版本号取该 (org_id, product_id) 的 MAX(version)+1。

        并发控制分两层：
        1) 同一事务内先取 pg_advisory_xact_lock(hashtext('org_id:product_id'))，
           按 (org_id, product_id) 串行化版本号分配，从根上消除 MAX+1 竞态；
        2) 唯一索引 uq_product_contents_org_product_version 兜底：若仍撞索引
           （例如绕过本适配器的写入），最多重试 _MAX_VERSION_RETRIES 次。

        参数:
            record: 待落库记录；读取 org_id / product_id / thread_id / content_data /
                prompt_version / model_name / is_approved 字段。
        返回:
            传入的 record 本身（便于上层链式使用）；DB 生成的 id 与 version 不回填，
            record.version 不具权威性（契约见 ports/content_store.py）。
        异常:
            psycopg.errors.UniqueViolation: 重试次数用尽仍撞版本唯一索引（并发冲突未收敛）。
            psycopg.Error: 其余数据库错误（连接失败、权限不足等）。
        注意:
            content_data 以 Jsonb 包装写入 jsonb 列；created_at / updated_at 由 DB 默认值与触发器维护；
            咨询锁键由 hashtext() 取整，理论上的哈希碰撞只会让不同商品之间额外串行，不影响正确性。
        """
        for attempt in range(_MAX_VERSION_RETRIES):
            try:
                with _connect(self._dsn) as conn:
                    # 显式事务：pg_advisory_xact_lock 为事务级锁，autocommit 下会立刻释放
                    with conn.transaction():
                        # 按 (org_id, product_id) 串行化版本号分配
                        conn.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"{record.org_id}:{record.product_id}",),
                        )
                        # 单条 INSERT...SELECT：在同一语句内计算下一版本号（唯一索引兜底）
                        conn.execute(
                            "INSERT INTO schema_pa_ai.product_contents "
                            "(id, org_id, product_id, thread_id, version, content_data, "
                            "prompt_version, model_name, is_approved) "
                            "SELECT %s, %s, %s, %s, COALESCE(MAX(version), 0) + 1, %s, %s, %s, %s "
                            "FROM schema_pa_ai.product_contents WHERE org_id = %s AND product_id = %s",
                            (
                                uuid.uuid4(), record.org_id, record.product_id, record.thread_id,
                                Jsonb(record.content_data), record.prompt_version, record.model_name,
                                record.is_approved, record.org_id, record.product_id,
                            ),
                        )
                return record
            except UniqueViolation:
                if attempt >= _MAX_VERSION_RETRIES - 1:
                    # 重试用尽：交给上层（worker 重试或失败终态）处理，不静默吞错
                    raise
        return record  # 不可达：循环内必然 return 或 raise；仅为类型检查兜底

    def purge_by_product(self, *, org_id: str, product_id: str) -> int:
        """物理清理某商品全部生成内容（admin 彻底删除用；ai-engine 独占写）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
        返回:
            实际删除的行数（rowcount）；0 表示该商品无已落库内容。
        异常:
            psycopg.Error: 数据库错误（连接失败、权限不足等）。
        注意:
            本方法只删表行、不碰对象存储；OSS 图片需先经 list_image_urls()
            取出 URL 再由上层 delete_urls 清理（顺序约束见 list_image_urls 的 docstring）。
        """
        with _connect(self._dsn) as conn:
            cur = conn.execute(
                "DELETE FROM schema_pa_ai.product_contents WHERE org_id = %s AND product_id = %s",
                (org_id, product_id),
            )
            # autocommit 下 DELETE 已生效，可直接读取 rowcount
            return cur.rowcount

    def list_image_urls(self, *, org_id: str, product_id: str) -> list[str]:
        """收集某商品全部已落库内容中的 image block URL（purge 联动删 OSS 图片用）。

        覆盖端口默认实现（ports/content_store.py 中默认返回空列表）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
        返回:
            content_data.blocks 中 type=image 的 url 列表；同 URL 多次引用由上层去重。
        异常:
            psycopg.Error: 数据库错误（连接失败、权限不足等）。
        注意:
            必须在 purge_by_product() 之前调用：行删除后 URL 无法再取回，
            会导致 OSS 侧残留孤儿对象；content_data 非 dict（历史或异常写入）时跳过该行。
        """
        urls: list[str] = []
        with _connect(self._dsn) as conn:
            rows = conn.execute(
                "SELECT content_data FROM schema_pa_ai.product_contents WHERE org_id = %s AND product_id = %s",
                (org_id, product_id),
            ).fetchall()
        # 默认 tuple 行工厂：单列查询可直接按位置解包
        for (content_data,) in rows:
            if not isinstance(content_data, dict):
                # 兜底：非 dict 的 jsonb（历史/异常写入）没有 blocks 语义
                continue
            blocks = content_data.get("blocks") or []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "image" and block.get("url"):
                    urls.append(str(block["url"]))
        return urls


class PgEvalLogStore:
    """EvalLogStore 的 PostgreSQL 实现：写入 schema_pa_ai.evaluation_logs。

    支撑前端 Trace 面板与通过率分析；role_pa_ai 全 DML，ai-engine 独占写。
    """

    def __init__(self, dsn: str):
        """初始化。

        参数:
            dsn: PostgreSQL 连接串；应为 role_pa_ai 角色。
        """
        self._dsn = dsn

    def save(self, record: EvalRecord) -> EvalRecord:
        """持久化一条评估日志。

        参数:
            record: 待落库记录；读取 org_id / product_id / thread_id / evaluator_type /
                score / errors / latency_ms / rule_id 字段。
        返回:
            传入的 record 本身；DB 生成的 id 不回填（契约见 ports/eval_log_store.py）。
        异常:
            psycopg.Error: 数据库错误，例如 evaluator_type 违反 CHECK ('rule','llm')、
                连接失败或权限不足。
        注意:
            errors 以 Jsonb 包装写入 jsonb 数组（None 归一为 []）；
            llm_usage 列不在本记录契约内，落库取 DB 默认值 '{}'。
        """
        with _connect(self._dsn) as conn:
            conn.execute(
                "INSERT INTO schema_pa_ai.evaluation_logs "
                "(id, org_id, product_id, thread_id, evaluator_type, score, errors, latency_ms, rule_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4(), record.org_id, record.product_id, record.thread_id, record.evaluator_type,
                 record.score, Jsonb(record.errors or []), record.latency_ms, record.rule_id),
            )
        return record

    def purge_by_product(self, *, org_id: str, product_id: str) -> int:
        """物理清理某商品全部评估日志（admin 彻底删除用；ai-engine 独占写）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
        返回:
            实际删除的行数（rowcount）；0 表示该商品无评估日志。
        异常:
            psycopg.Error: 数据库错误（连接失败、权限不足等）。
        注意:
            与 PgContentStore.purge_by_product 同属一次彻底删除流程，
            由上层在清理 OSS 图片之后统一调用。
        """
        with _connect(self._dsn) as conn:
            cur = conn.execute(
                "DELETE FROM schema_pa_ai.evaluation_logs WHERE org_id = %s AND product_id = %s",
                (org_id, product_id),
            )
            return cur.rowcount


class PgProductsReader:
    """商品素材只读适配器：SELECT schema_pa_backend.products（仅 SELECT 权限）。

    权限依据：database/sql/0002_roles_grants.sql 中 `GRANT SELECT ON
    schema_pa_backend.products TO role_pa_ai`，写入权限一律不授予 ai-engine。

    注意:
        ports/ 侧当前没有对应的 ProductsReader 端口抽象，
        本类属适配器侧直读；仅读取生成所需字段且返回 dict，避免把 backend 域表结构
        泄漏给上游节点。
    """

    def __init__(self, dsn: str):
        """初始化。

        参数:
            dsn: PostgreSQL 连接串；应为 role_pa_ai 角色。
        """
        self._dsn = dsn

    def read(self, *, product_id: str, org_id: str) -> dict[str, Any] | None:
        """读取单个商品素材（按 org 隔离）。

        参数:
            product_id: 商品 ID（uuid 字符串）。
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
        返回:
            商品字段 dict：id / org_id / sku_code / title / base_price /
            stock_status / status / raw_images；未命中返回 None。
        异常:
            psycopg.Error: 数据库错误（连接失败、权限不足等）。
        注意:
            取值依赖 SELECT 的列顺序（默认 tuple 行工厂）；base_price 为 numeric，
            统一转 float 便于上层计算，NULL 归一为 0.0。
        """
        with _connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT id, org_id, sku_code, title, base_price, stock_status, status, raw_images "
                "FROM schema_pa_backend.products WHERE id = %s AND org_id = %s",
                (product_id, org_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "org_id": str(row[1]),
            "sku_code": row[2],
            "title": row[3],
            "base_price": float(row[4]) if row[4] is not None else 0.0,
            "stock_status": row[5],
            "status": row[6],
            "raw_images": row[7] or [],
        }


class PgBusinessReader(BusinessReader):
    """业务只读适配器：Agent 工具取数的真实实现（全部 SELECT，无写语句）。

    权限依据（database/sql/0002_roles_grants.sql）:
        · schema_pa_backend.products       —— role_pa_ai 仅 SELECT（复用 PgProductsReader）；
        · schema_pa_backend.hitl_approvals —— role_pa_ai 仅 SELECT（历史审批）；
        · schema_pa_ai.product_contents / evaluation_logs —— role_pa_ai 全 DML（此处只读）。
        sys_users / organizations / compliance_* 对 role_pa_ai 无授权，故本适配器不提供
        组织名、用户姓名、违禁词库查询（边界说明见 ports/business_reader.py）。

    注意:
        所有查询强制带 org_id（多租户隔离的第一过滤条件），并对 limit 做上界收敛；
        返回结构固定为 JSON 友好 dict（uuid/datetime 一律转字符串），便于直接回填给模型。
    """

    def __init__(self, dsn: str) -> None:
        """初始化。

        参数:
            dsn: PostgreSQL 连接串；应为 role_pa_ai 角色（只读语义靠 SQL 保证）。
        """
        self._dsn = dsn
        self._products = PgProductsReader(dsn)

    def read_product(self, *, org_id: str, product_id: str) -> dict[str, Any] | None:
        """读取商品素材（委托 PgProductsReader，保持与生成链路同一口径）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
        返回:
            商品字段 dict；未命中返回 None。
        异常:
            psycopg.Error: 数据库错误（连接失败/权限不足）。
        """
        return self._products.read(product_id=product_id, org_id=org_id)

    def list_content_versions(self, *, org_id: str, product_id: str, limit: int = 3) -> list[dict[str, Any]]:
        """列出本商品历史文案版本（新→旧）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
            limit: 返回条数上限（1-20，超出收敛）。
        返回:
            每条含 version / is_approved / model_name / text_excerpt / created_at 的 dict 列表。
        异常:
            psycopg.Error: 数据库错误。
        """
        with psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT version, is_approved, model_name, content_data, created_at "
                "FROM schema_pa_ai.product_contents WHERE org_id = %s AND product_id = %s "
                "ORDER BY version DESC LIMIT %s",
                (org_id, product_id, max(1, min(20, int(limit)))),
            ).fetchall()
        return [
            {
                "version": int(row["version"]),
                "is_approved": bool(row["is_approved"]),
                "model_name": row["model_name"],
                "text_excerpt": _excerpt(_blocks_text(row["content_data"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def list_eval_logs(self, *, org_id: str, product_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """列出本商品历史评估记录（新→旧）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
            limit: 返回条数上限（1-20，超出收敛）。
        返回:
            每条含 evaluator_type / score / errors / rule_id / created_at 的 dict 列表。
        异常:
            psycopg.Error: 数据库错误。
        """
        with psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT evaluator_type, score, errors, rule_id, created_at "
                "FROM schema_pa_ai.evaluation_logs WHERE org_id = %s AND product_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (org_id, product_id, max(1, min(20, int(limit)))),
            ).fetchall()
        return [
            {
                "evaluator_type": row["evaluator_type"],
                "score": float(row["score"]) if row["score"] is not None else None,
                "errors": row["errors"] or [],
                "rule_id": str(row["rule_id"]) if row["rule_id"] else None,
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def list_approvals(self, *, org_id: str, product_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """列出本商品历史人工审批记录（新→旧）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件）。
            product_id: 商品 ID。
            limit: 返回条数上限（1-20，超出收敛）。
        返回:
            每条含 status / channel / feedback / approver_id / resolved_at 的 dict 列表。
        异常:
            psycopg.Error: 数据库错误。
        注意:
            approver_id 只有 UUID（sys_users 对 role_pa_ai 未授权，取不到姓名）。
        """
        with psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT status, channel, feedback, approver_id, resolved_at "
                "FROM schema_pa_backend.hitl_approvals WHERE org_id = %s AND product_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (org_id, product_id, max(1, min(20, int(limit)))),
            ).fetchall()
        return [
            {
                "status": row["status"],
                "channel": row["channel"],
                "feedback": row["feedback"],
                "approver_id": str(row["approver_id"]) if row["approver_id"] else None,
                "resolved_at": str(row["resolved_at"]) if row["resolved_at"] else None,
            }
            for row in rows
        ]


def _blocks_text(content_data: Any) -> str:
    """从 product_contents.content_data 提取纯文本。

    参数:
        content_data: 数据库 jsonb 解码结果。真实形状为
            {"blocks": [{"type": "text", "text": "..."}, {"type": "image", "url": "..."}]}
            （见 PgContentStore.save 写入的 record.content_data）；
            为兼容历史/异常写入，裸 blocks 列表（[...]）同样接受。
    返回:
        拼接后的文本（按 blocks 顺序，图片块忽略）；结构异常时返回空串（不抛异常）。
    """
    if isinstance(content_data, dict):
        blocks = content_data.get("blocks")
    elif isinstance(content_data, list):
        blocks = content_data
    else:
        return ""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def _excerpt(text: str, limit: int = 400) -> str:
    """截取文本节选（避免把整篇长文塞进模型上下文）。

    参数:
        text: 原始文本。
        limit: 最大保留字符数（超出时追加省略标记）。
    返回:
        长度不超过 limit 的节选（含 "…" 标记时略长 1 字符）。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def list_pa_thread_ids(dsn: str, *, org_id: str, product_id: str) -> list[str]:
    """反查某商品在 schema_pa_ai 出现过的全部 thread_id（去重）。

    参数:
        dsn: PostgreSQL 连接串（role_pa_ai 即可，两张表均只读）。
        org_id: 组织 ID（多租户隔离的第一过滤条件）。
        product_id: 商品 ID。
    返回:
        thread_id 字符串列表（去重、按首次出现顺序）；无记录返回 []。
    异常:
        psycopg.Error: 数据库错误（连接失败、权限不足等）。
    注意:
        用途：admin 彻底删除商品时，先取出该商品关联的线程，再逐个
        LangGraph PostgresSaver.delete_thread() 清理 checkpoints /
        checkpoint_blobs / checkpoint_writes（此前无人清理，属已知遗留项）；
        thread_id 列类型为 uuid，此处统一转 str 以匹配 LangGraph 的字符串线程键。
    """
    with _connect(dsn) as conn:
        rows = conn.execute(
            "SELECT thread_id FROM schema_pa_ai.product_contents "
            "WHERE org_id = %s AND product_id = %s AND thread_id IS NOT NULL "
            "UNION "
            "SELECT thread_id FROM schema_pa_ai.evaluation_logs "
            "WHERE org_id = %s AND product_id = %s AND thread_id IS NOT NULL",
            (org_id, product_id, org_id, product_id),
        ).fetchall()
    seen: dict[str, None] = {}
    for (thread_id,) in rows:
        if thread_id is not None:
            seen.setdefault(str(thread_id), None)
    return list(seen)


__all__ = [
    "CHECKPOINT_SCHEMA",
    "PgBusinessReader",
    "PgContentStore",
    "PgEvalLogStore",
    "PgProductsReader",
    "ensure_checkpoint_schema",
    "list_pa_thread_ids",
]





