"""ORM 模型（SQLAlchemy 2.x，async 口径）。

红线（本模块的架构约束，勿越界）:
    · **只在既有表上做 DML**：DDL/迁移归 ``database/sql/*.sql``，本文件不建表、不迁移；
    · 字段基线以 ``database/sql/0001_schema.sql`` 为准（含 ``0004`` 的 delete_audits），
      改列必须同步 SQL 文件，否则要到运行时才以 ``UndefinedColumn`` 暴露；
    · ``checkpoints`` / ``checkpoint_blobs`` / ``checkpoint_writes`` / ``checkpoint_migrations``
      由 ai-engine 的 ``PostgresSaver.setup()`` 自维护，**backend 零接触，故不映射**（role_pa_backend 也无权限）；
    · 跨 schema 引用（``schema_pa_ai.*.product_id`` → ``schema_pa_backend.products``）只作逻辑引用、
      **不建物理外键**（为 ai-engine 未来拆独立实例预留）。

字段口径与 SQL 对齐要点:
    · ``products.raw_images`` 是 **jsonb 数组**；本模块写入的规范形状是**公有 URL 字符串数组**
      ``["https://…"]``（ai-engine ``node_image._uploaded_image_urls()`` 的规范形状；
      它同时兼容 ``[{"url":…}]`` 与单体字符串，但我们要写入规范形状而不是依赖兼容）；
    · 状态枚举（``stock_status`` / ``status`` / ``role`` / ``channel`` …）的 CHECK 约束在 DB 侧，
      ORM 侧不重复声明以免两处漂移；非法取值由数据库拒绝（红线用例锁住）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: schema 名（与 database/sql 一致；写成常量避免字符串散落各处拼错）
SCHEMA_BACKEND = "schema_pa_backend"
SCHEMA_AI = "schema_pa_ai"


class Base(DeclarativeBase):
    """声明基类。"""


def _uuid_pk() -> Mapped[uuid.UUID]:
    """业务主键统一 UUID：应用侧生成 + 保留 DB 默认（``gen_random_uuid()``）。"""
    return mapped_column(Uuid, primary_key=True, default=uuid4, server_default=text("gen_random_uuid()"))


class _TimestampMixin:
    """``created_at`` / ``updated_at``：DB 侧默认值与触发器维护，应用无需赋值。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


# ============================ schema_pa_backend（backend 域，全 DML）============================


class Organization(_TimestampMixin, Base):
    """多租户根表（所有业务表的 ``org_id`` 都指向它）。"""

    __tablename__ = "organizations"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")


class SysUser(_TimestampMixin, Base):
    """用户主表（``role ∈ admin|reviewer|operator``；``username`` 仅**组织内**唯一）。"""

    __tablename__ = "sys_users"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.organizations.id"))
    username: Mapped[str] = mapped_column(Text)
    hashed_password: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="operator")
    status: Mapped[str] = mapped_column(Text, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Product(_TimestampMixin, Base):
    """商品主数据（状态机 ``draft→generating→waiting_approval→published``，另有 ``archived``/``deleted``）。

    注：``deleted`` 是 PA 相对 PP 多出的软删态 —— 彻底删除由 ``DELETE /products/{id}/purge`` 触发。
    """

    __tablename__ = "products"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.organizations.id"))
    sku_code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    stock_status: Mapped[str] = mapped_column(Text, default="in_stock")
    status: Mapped[str] = mapped_column(Text, default="draft")
    active_thread_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey(f"{SCHEMA_BACKEND}.sys_users.id"), nullable=True
    )
    raw_images: Mapped[list] = mapped_column(JSONB, default=list)


class HitlApproval(_TimestampMixin, Base):
    """人工审批记录（CAS 状态机 ``pending→approved|rejected``）。

    ``content_snapshot``：转人工时由 ``result:workflow`` 携带、本模块落库的「AI 生成详情」快照
    （结构见 backend-api/README §2.3）——批准前文案不落 ``product_contents``，审批人只能看它。
    """

    __tablename__ = "hitl_approvals"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.organizations.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.products.id"))
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey(f"{SCHEMA_BACKEND}.sys_users.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, default="pending")
    channel: Mapped[str] = mapped_column(Text, default="web")
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComplianceWord(_TimestampMixin, Base):
    """违禁词（确定性规则层数据源）。

    **无 ``org_id``**：规则表是全局的（README 明确「除规则表外都带 org_id」），
    因此入队快照也是全局的，越权防护靠「只有 admin 能改」（P6 的 CRUD）。
    时效窗口（``effective_at``/``expires_at``）在**入队瞬间**由 backend 过滤
    —— ai-engine 侧不做时间过滤（见 backend-api/README §2.6）。
    """

    __tablename__ = "compliance_words"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    word: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, default="high")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)


class ComplianceRule(_TimestampMixin, Base):
    """合规正则规则（极限词/绝对化用语；无时间维度，全量下发）。"""

    __tablename__ = "compliance_rules"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    pattern: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text, default="regex")
    severity: Mapped[str] = mapped_column(Text, default="high")
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)


class GenerationJob(_TimestampMixin, Base):
    """AI 生成任务的**权威状态源**（``thread_id`` 全表唯一）。

    全链路幂等的锚点：``products.active_thread_id`` 指向当前线程，本表回答「这个线程现在什么状态」。
    ``timings`` / ``llm_usage`` 列已存在，但 ai-engine 目前未回传计量（README todo）→ 保持默认 ``{}``。
    """

    __tablename__ = "generation_jobs"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.organizations.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.products.id"))
    status: Mapped[str] = mapped_column(Text, default="running")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    timings: Mapped[dict] = mapped_column(JSONB, default=dict)
    llm_usage: Mapped[dict] = mapped_column(JSONB, default=dict)


class NotificationOutbox(_TimestampMixin, Base):
    """通知可靠性 Outbox：与 ``hitl_approvals`` **同事务**写入，投递器消费后置 sent/failed/dlq。"""

    __tablename__ = "notification_outbox"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(f"{SCHEMA_BACKEND}.organizations.id"))
    channel: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="pending")
    retry_count: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class DeleteAudit(Base):
    """admin 彻底删除审计。

    权限：``role_pa_backend`` 仅有 **SELECT/INSERT**（``0004`` 显式 REVOKE UPDATE/DELETE）
    —— 审计表只能追加，不能改也不能删（否则等于没有审计）。本模型**只用于插入与查询**。
    """

    __tablename__ = "delete_audits"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sku_code: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


# ======================= schema_pa_ai（ai-engine 域；backend **只读** 两表）=======================


class JobAbortAudit(Base):
    """运维手工终止生成任务的审计（``0005_job_abort_audit.sql``）。

    权限：``role_pa_backend`` 仅 **SELECT/INSERT**（0005 显式 REVOKE UPDATE/DELETE）
    —— 与 ``delete_audits`` 同一红线：审计只能追加。本模型**只用于插入与查询**。

    存在的理由：卡死的生成任务过去只能改库且不留痕；本表记录「谁/何时/为什么」终止了哪个任务。
    """

    __tablename__ = "job_abort_audits"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    aborted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ApprovalRedriveAudit(Base):
    """审批补投审计（``0006_approval_audits.sql``）。

    权限：``role_pa_backend`` 仅 **SELECT/INSERT**（0006 显式 REVOKE UPDATE/DELETE）。

    存在的理由（2026-09 实测）：补投按钮此前**没有任何痕迹** —— 点了之后既看不到"投了几次、
    结果如何"，也分不清"引擎已收到（无需补投）"与"投出去了但没消费"。本表让每次动作可审计。
    """

    __tablename__ = "approval_redrive_audits"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    #: enqueued / not_needed / throttled / enqueue_failed（见 approval_service.redrive）
    outcome: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    redriven_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ApprovalOverride(Base):
    """人工放行「带评估命中点」的审计（``0006_approval_audits.sql``）。

    权限：``role_pa_backend`` 仅 **SELECT/INSERT**（0006 显式 REVOKE UPDATE/DELETE）。

    存在的理由（2026-09 实测）：合规命中 + 转人工后，审批人一点「批准」就能上架，
    既没有强制说明、也没有留痕 —— 出了合规问题无法回答"谁在知情下放行的"。
    本表记录放行时的命中点快照与理由。
    """

    __tablename__ = "approval_overrides"
    __table_args__ = {"schema": SCHEMA_BACKEND}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    approval_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    violation_count: Mapped[int] = mapped_column(Integer)
    #: 命中点快照（evaluation_result.violations；JSONB，便于回查"当时到底命中了什么"）
    violations: Mapped[list] = mapped_column(JSONB, default=list)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ProductContent(_TimestampMixin, Base):
    """AI 生成内容版本表（本模块**仅 SELECT**：版本切换与图文展示；写归 ai-engine）。"""

    __tablename__ = "product_contents"
    __table_args__ = {"schema": SCHEMA_AI}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)  # 逻辑引用，无物理外键
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(default=1)
    content_data: Mapped[dict] = mapped_column(JSONB, default=dict)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_approved: Mapped[bool] = mapped_column(default=False)


class EvaluationLog(_TimestampMixin, Base):
    """AI 评估/试错日志（本模块**仅 SELECT**：Trace 面板数据源；写归 ai-engine）。"""

    __tablename__ = "evaluation_logs"
    __table_args__ = {"schema": SCHEMA_AI}
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)  # 逻辑引用，无物理外键
    thread_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    evaluator_type: Mapped[str] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    errors: Mapped[list] = mapped_column(JSONB, default=list)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    llm_usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

