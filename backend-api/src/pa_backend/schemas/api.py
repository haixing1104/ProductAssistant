"""请求/响应模型（Pydantic v2）：入参与出参的显式契约。

为什么入参要在这里收敛（而不是直接在路由签名里写 dict）:
    · 校验规则集中一处（长度/枚举），错误由 ``RequestValidationError`` 统一转 400；
    · 出参显式列出字段 —— 避免把 ``hashed_password`` 这类字段「顺手序列化」出去
      （这是 Web 服务最常见的信息泄露之一）。

口令策略:
    最小长度 8 且非纯空白；**不**强制大小写/符号（长度优先于复杂度是现代口径），
    真正的防线是登录限流 + bcrypt。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: 可被 admin 通过成员接口创建的角色（``admin`` 只能由注册流程产生，防提权）
ASSIGNABLE_ROLES = ("reviewer", "operator")


class RegisterRequest(BaseModel):
    """注册即开租户（新组织 + 首个 admin）。"""

    org_name: str = Field(min_length=1, max_length=128, description="组织名称（租户）")
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("org_name", "username")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """去空白后不得为空（避免「用户名是空格」这类脏数据进库）。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("不得为空白")
        return stripped


class LoginRequest(BaseModel):
    """登录。

    ``org_name`` 为什么是可选的:
        PA 的 ``sys_users`` 唯一约束是 ``(org_id, username)`` —— 用户名**只在组织内唯一**。
        因此跨组织可能存在同名账号：此时必须用 ``org_name`` 消歧，否则无法判断登谁。
        只有一个匹配账号时无需提供（正常用户零感知）。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    org_name: str | None = Field(default=None, max_length=128, description="同名账号跨组织时的消歧项")


class TokenResponse(BaseModel):
    """登录/续签响应（refresh 走 HttpOnly Cookie，不出现在响应体里）。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="access 有效期（秒）")


class CurrentUserResponse(BaseModel):
    """当前登录身份（供前端角色化 UI）。"""

    id: str
    org_id: str
    username: str
    role: str


class MemberCreateRequest(BaseModel):
    """admin 新增成员。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(description="reviewer（可审批）或 operator（仅操作）")

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        """用户名去首尾空白。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("不得为空白")
        return stripped

    @field_validator("role")
    @classmethod
    def _role_allowed(cls, value: str) -> str:
        """只允许 reviewer / operator（``admin`` 只能由注册产生）。"""
        if value not in ASSIGNABLE_ROLES:
            raise ValueError(f"role 只能是 {' / '.join(ASSIGNABLE_ROLES)}")
        return value


class MemberResponse(BaseModel):
    """成员信息（**绝不含** ``hashed_password``）。"""

    id: uuid.UUID
    username: str
    role: str
    status: str
    last_login_at: str | None = None


# ============================== 商品（P3）=================================

#: 与 DB CHECK 一致（PA 比 PP 多 ``preorder``）
STOCK_STATUS_VALUES = ("in_stock", "low_stock", "out_of_stock", "preorder")


class ProductCreateRequest(BaseModel):
    """新建商品。"""

    model_config = ConfigDict(extra="forbid")

    sku_code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    base_price: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("9999999999.99"))
    stock_status: str = "in_stock"
    raw_images: list[str] = Field(default_factory=list, description="上传图公有 URL 列表（规范形状：字符串数组）")

    @field_validator("sku_code", "title")
    @classmethod
    def _strip(cls, value: str) -> str:
        """去首尾空白且不得为空。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("不得为空白")
        return stripped

    @field_validator("stock_status")
    @classmethod
    def _stock_status(cls, value: str) -> str:
        """库存枚举与 DB CHECK 对齐（避免「进库才报 500」）。"""
        if value not in STOCK_STATUS_VALUES:
            raise ValueError(f"stock_status 只能是 {'/'.join(STOCK_STATUS_VALUES)}")
        return value


class ProductUpdateRequest(BaseModel):
    """部分更新商品（未提供的字段不动；``raw_images`` 为整体覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=512)
    base_price: Decimal | None = Field(default=None, ge=0)
    stock_status: str | None = None
    raw_images: list[str] | None = None

    @field_validator("stock_status")
    @classmethod
    def _stock_status(cls, value: str | None) -> str | None:
        """库存枚举校验（None 表示不改）。"""
        if value is None:
            return None
        if value not in STOCK_STATUS_VALUES:
            raise ValueError(f"stock_status 只能是 {'/'.join(STOCK_STATUS_VALUES)}")
        return value

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        """标题去空白（空串按「未提供」处理会误删标题，故直接拒绝）。"""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("title 不得为空白")
        return stripped


class PurgeRequest(BaseModel):
    """彻底删除请求体（``DELETE`` 带 body：原因必须留痕）。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        """原因去空白且不得为空（审计要求可追溯）。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason 不得为空白")
        return stripped


class PresignRequest(BaseModel):
    """OSS 预签名直传请求。"""

    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    content_type: str = Field(description="图片 MIME 类型（必须与实际字节一致）")
    filename: str = Field(default="upload", max_length=255, description="仅用于扩展名兜底与展示")

    @field_validator("content_type")
    @classmethod
    def _content_type(cls, value: str) -> str:
        """只允许图片类型（白名单，见 ``services/oss.ALLOWED_CONTENT_TYPES``）。"""
        from ..services.oss import ALLOWED_CONTENT_TYPES

        if value not in ALLOWED_CONTENT_TYPES:
            raise ValueError(f"content_type 只支持 {'/'.join(ALLOWED_CONTENT_TYPES)}")
        return value


def serialize_product(product) -> dict:
    """商品 → 响应体（``raw_images`` 规范化：**只保留字符串 URL**）。

    为什么在这里做规范化而不是直接回传列值:
        ``products.raw_images`` 是 jsonb，历史/其它写入方可能塞进 ``{"url": …}`` 形状。
        响应层统一收敛成字符串数组，前端就不必兼容两种形状；同时避免把无关字段透出。
    """
    raw_images = [
        item if isinstance(item, str) else (item.get("url") if isinstance(item, dict) else None)
        for item in (product.raw_images or [])
    ]
    return {
        "id": str(product.id),
        "org_id": str(product.org_id),
        "sku_code": product.sku_code,
        "title": product.title,
        "base_price": float(product.base_price) if product.base_price is not None else 0.0,
        "stock_status": product.stock_status,
        "status": product.status,
        "active_thread_id": str(product.active_thread_id) if product.active_thread_id else None,
        "raw_images": [url for url in raw_images if url],
        "created_at": product.created_at.isoformat() if product.created_at else None,
        "updated_at": product.updated_at.isoformat() if product.updated_at else None,
    }


def serialize_content(content) -> dict:
    """``product_contents`` → 响应体（图文 blocks 原样透传，前端复用同一渲染器）。"""
    return {
        "id": str(content.id),
        "version": content.version,
        "content_data": content.content_data,
        "is_approved": content.is_approved,
        "model_name": content.model_name,
        "prompt_version": content.prompt_version,
        "thread_id": str(content.thread_id),
        "created_at": content.created_at.isoformat() if content.created_at else None,
    }


def serialize_eval_log(log) -> dict:
    """``evaluation_logs`` → 响应体（AI 思考轨迹：规则/LLM 判定 + 违规明细 + 用量）。"""
    return {
        "id": str(log.id),
        "thread_id": str(log.thread_id) if log.thread_id else None,
        "evaluator_type": log.evaluator_type,
        "score": float(log.score) if log.score is not None else None,
        "errors": log.errors,
        "latency_ms": log.latency_ms,
        "llm_usage": log.llm_usage,
        "rule_id": str(log.rule_id) if log.rule_id else None,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# ============================== 审批（P4）=================================


class ApprovalDecisionRequest(BaseModel):
    """审批决定请求体（``feedback`` 可选；驳回时强烈建议填写，作为重写输入）。"""

    model_config = ConfigDict(extra="forbid")

    feedback: str | None = Field(default=None, max_length=2000)

    @field_validator("feedback")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        """去空白；空串归一为 None（避免把空意见写进库）。"""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def snapshot_summary(snapshot: dict | None) -> dict:
    """从 ``content_snapshot`` 抽出列表页要展示的摘要字段。

    为什么列表不直接返回整份快照:
        快照含完整图文 blocks（可能几十 KB），列表页有 N 条 —— 全量返回会把列表接口拖慢。
        摘要给「够判断要不要点进去」的信息，详情页再取完整快照。
    """
    snapshot = snapshot or {}
    evaluation = snapshot.get("evaluation_result") or {}
    errors = evaluation.get("errors") or []
    return {
        "reason": snapshot.get("reason"),
        "score": evaluation.get("score"),
        "violation_count": len(errors) if isinstance(errors, list) else 0,
        "evaluation_attempts": snapshot.get("evaluation_attempts"),
    }


def serialize_approval(approval, product=None, *, include_snapshot: bool = True) -> dict:
    """审批单 → 响应体（``include_snapshot=False`` 时只给摘要，供列表接口）。

    注意:
        ``approver_id`` 只有 ID、没有姓名 —— ``sys_users`` 对 ai-engine 未授权，
        但 backend 侧可以自行 join 补全（P7 前端需要时再加 join）。
    """
    payload = {
        "id": str(approval.id),
        "org_id": str(approval.org_id),
        "product_id": str(approval.product_id),
        "thread_id": str(approval.thread_id),
        "status": approval.status,
        "channel": approval.channel,
        "feedback": approval.feedback,
        "approver_id": str(approval.approver_id) if approval.approver_id else None,
        "snapshot_summary": snapshot_summary(approval.content_snapshot),
        "created_at": approval.created_at.isoformat() if approval.created_at else None,
        "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
        "expire_at": approval.expire_at.isoformat() if approval.expire_at else None,
    }
    if product is not None:
        payload["product_title"] = product.title
        payload["sku_code"] = product.sku_code
        payload["product_status"] = product.status
    if include_snapshot:
        payload["content_snapshot"] = approval.content_snapshot
    return payload


# ============================== 合规词库 / 规则（P6）=================================


SEVERITY_VALUES = ("high", "medium", "low")


class ComplianceWordCreateRequest(BaseModel):
    """新增违禁词。"""

    model_config = ConfigDict(extra="forbid")

    word: str = Field(min_length=1, max_length=64)
    severity: str = "high"
    source: str | None = Field(default=None, max_length=128)
    effective_at: datetime | None = Field(default=None, description="生效时间；缺省=立即生效")
    expires_at: datetime | None = Field(default=None, description="过期时间；缺省=长期有效")

    @field_validator("word")
    @classmethod
    def _strip_word(cls, value: str) -> str:
        """词面去首尾空白（两侧空白会让词永远匹配不上，属隐性错误）。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("词面不得为空白")
        return stripped

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str) -> str:
        """严重级枚举与 ai-engine 的判定口径对齐（high/medium 阻断，low 仅提示）。"""
        if value not in SEVERITY_VALUES:
            raise ValueError(f"severity 只能是 {'/'.join(SEVERITY_VALUES)}")
        return value

    @model_validator(mode="after")
    def _window_order(self) -> "ComplianceWordCreateRequest":
        """``expires_at`` 必须晚于 ``effective_at``（否则词一入库就过期，属配置错误）。"""
        if self.effective_at and self.expires_at and self.expires_at <= self.effective_at:
            raise ValueError("expires_at 必须晚于 effective_at")
        return self


class ComplianceWordUpdateRequest(BaseModel):
    """更新违禁词（未提供的字段不动）。"""

    model_config = ConfigDict(extra="forbid")

    word: str | None = Field(default=None, min_length=1, max_length=64)
    severity: str | None = None
    source: str | None = Field(default=None, max_length=128)
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("word")
    @classmethod
    def _strip_word(cls, value: str | None) -> str | None:
        """词面去空白（None = 不改）。"""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("词面不得为空白")
        return stripped

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str | None) -> str | None:
        """严重级枚举校验。"""
        if value is not None and value not in SEVERITY_VALUES:
            raise ValueError(f"severity 只能是 {'/'.join(SEVERITY_VALUES)}")
        return value


class ComplianceRuleCreateRequest(BaseModel):
    """新增合规正则规则。"""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=512, description="Python 正则（与 ai-engine re 模块同语义）")
    severity: str = "high"
    suggestion: str | None = Field(default=None, max_length=512, description="命中时给模型的改写建议")

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        """**入口即做正则语法校验**：坏正则写进库只会被 ai-engine 静默跳过（规则看起来生效其实没有）。

        只校验语法，不做复杂度/ReDoS 防护（与 ai-engine 同口径：该项由写入侧人工把关 + 长度上限）。
        """
        import re as _re

        try:
            _re.compile(value)
        except _re.error as exc:
            raise ValueError(f"正则语法错误：{exc}") from exc
        return value

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str) -> str:
        """严重级枚举校验。"""
        if value not in SEVERITY_VALUES:
            raise ValueError(f"severity 只能是 {'/'.join(SEVERITY_VALUES)}")
        return value


class ComplianceRuleUpdateRequest(BaseModel):
    """更新合规正则规则（未提供的字段不动）。"""

    model_config = ConfigDict(extra="forbid")

    pattern: str | None = Field(default=None, min_length=1, max_length=512)
    severity: str | None = None
    suggestion: str | None = Field(default=None, max_length=512)

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str | None) -> str | None:
        """正则语法校验（None = 不改）。"""
        import re as _re

        if value is None:
            return None
        try:
            _re.compile(value)
        except _re.error as exc:
            raise ValueError(f"正则语法错误：{exc}") from exc
        return value

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str | None) -> str | None:
        """严重级枚举校验。"""
        if value is not None and value not in SEVERITY_VALUES:
            raise ValueError(f"severity 只能是 {'/'.join(SEVERITY_VALUES)}")
        return value


class CompliancePreviewRequest(BaseModel):
    """快照预览请求：对一段文本跑一次**与入队完全一致**的规则匹配。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20000, description="待检查文本（标题/正文/整篇文案）")


def serialize_word(record) -> dict:
    """词条 → 响应体（含是否生效，前端可直接标注「已过期/未生效」）。"""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    moment = _dt.now(_tz.utc)
    return {
        "id": str(record.id),
        "word": record.word,
        "severity": record.severity,
        "source": record.source,
        "effective_at": record.effective_at.isoformat() if record.effective_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "is_active": bool(
            (record.effective_at is None or record.effective_at <= moment)
            and (record.expires_at is None or record.expires_at > moment)
        ),
    }


def serialize_rule(record) -> dict:
    """正则规则 → 响应体。"""
    return {
        "id": str(record.id),
        "pattern": record.pattern,
        "type": record.type,
        "severity": record.severity,
        "suggestion": record.suggestion,
    }
