"""合规词库 / 规则路由（P6）：CRUD + 快照预览。

权限模型:
    · **读**（列表 / 快照 / 预览）：``admin`` 与 ``reviewer`` —— 词表不是机密，
      而「为什么这条被判违规」是审批人必须能查到的东西；
    · **写**（增删改）：**仅 admin** —— 这两张表是**全局配置（无 org_id）**，
      改一个词会影响所有组织的生成结果，必须收口。

为什么要有预览接口:
    规则是「写进库 → 下次生成才生效」，运营无法当场验证一条正则会不会命中；
    而坏正则在 ai-engine 侧是**静默跳过**的（看起来生效、实际没拦）。
    预览用**与入队完全一致的快照**跑一遍匹配并回显命中位置与分数，把这类问题挡在写入前。

变更留痕（已知缺口，写在明面上）:
    合规词表/规则的增删改**目前没有审计表**（``delete_audits`` 只覆盖商品彻底删除）。
    在「只有 admin 能改」且改动影响面可由 ``evaluation_logs.rule_id`` 反查的前提下风险可控；
    要补审计建议新增 ``0005_compliance_audit.sql``（P8 候选）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.deps import CurrentUser, get_db, require_roles
from ..core.errors import ApiError
from ..repositories.compliance import ComplianceRepo
from ..schemas.api import (
    CompliancePreviewRequest,
    ComplianceRuleCreateRequest,
    ComplianceRuleUpdateRequest,
    ComplianceWordCreateRequest,
    ComplianceWordUpdateRequest,
    serialize_rule,
    serialize_word,
)
from ..services.compliance_matcher import compile_matcher, is_blocking, rule_score
from .common import ok, to_uuid

__all__ = ["router"]

router = APIRouter(prefix="/compliance", tags=["compliance"])

#: 读角色（审批人也要能查词表）
READ_ROLES = ("admin", "reviewer")
#: 写角色（全局配置，仅管理员）
WRITE_ROLES = ("admin",)


def _repo(session: AsyncSession) -> ComplianceRepo:
    """构造合规仓储（全局表，不需要租户上下文）。"""
    return ComplianceRepo(session)


# ---------------------------------------------------------------- 词条


@router.get("/words")
async def list_words(
    response: Response,
    severity: str | None = Query(default=None, description="high/medium/low"),
    active_only: bool = Query(default=False, description="只看当前生效（已生效且未过期）"),
    keyword: str | None = Query(default=None, description="词面模糊匹配"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """词条列表（分页；总数走 ``X-Total-Count``）。"""
    items, total = await _repo(session).list_words(
        severity=severity, active_only=active_only, keyword=keyword, offset=offset, limit=limit
    )
    # 总数放响应头而不是包一层 {items,total}：与其它列表接口同一契约（前端 unwrapPage 读它）
    response.headers["X-Total-Count"] = str(total)
    return ok([serialize_word(item) for item in items])


@router.post("/words")
async def create_word(
    body: ComplianceWordCreateRequest,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """新增词条（词面重复 → 409：重复词会让同一片段被重复上报扣分）。"""
    repo = _repo(session)
    if await repo.find_word(body.word) is not None:
        raise ApiError(409, f"该词已存在：{body.word}")
    record = await repo.create_word(
        word=body.word,
        severity=body.severity,
        source=body.source,
        effective_at=body.effective_at,
        expires_at=body.expires_at,
    )
    await session.commit()
    return ok(serialize_word(record))


@router.patch("/words/{word_id}")
async def update_word(
    word_id: str,
    body: ComplianceWordUpdateRequest,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """更新词条（改名时同样做重复检测）。"""
    repo = _repo(session)
    record = await repo.get_word(to_uuid(word_id))
    if record is None:
        raise ApiError(404, "词条不存在")
    # exclude_unset 才能区分「没传这个字段」与「显式传了 null」——
    # 直接 model_dump() 会把未传字段也带上默认值，等于每次 PATCH 都把其它字段重置一遍
    fields = body.model_dump(exclude_unset=True)
    new_word = fields.get("word")
    if new_word and new_word != record.word:
        if await repo.find_word(new_word) is not None:
            raise ApiError(409, f"该词已存在：{new_word}")
        record.word = new_word
    for field in ("severity", "source", "effective_at", "expires_at"):
        if field in fields:
            setattr(record, field, fields[field])
    if record.effective_at and record.expires_at and record.expires_at <= record.effective_at:
        # 先回滚再抛：上面的 setattr 只改了内存对象，不回滚会让本请求内后续读取看到非法值
        await session.rollback()
        raise ApiError(400, "expires_at 必须晚于 effective_at")
    await session.commit()
    return ok(serialize_word(record))


@router.delete("/words/{word_id}")
async def delete_word(
    word_id: str,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """删除词条（物理删除；配置数据不做软删，避免「删了但还在拦」的语义混乱）。"""
    repo = _repo(session)
    record = await repo.get_word(to_uuid(word_id))
    if record is None:
        raise ApiError(404, "词条不存在")
    deleted_id = str(record.id)
    await repo.delete_word(record)
    await session.commit()
    return ok({"id": deleted_id, "deleted": True})


# ---------------------------------------------------------------- 规则


@router.get("/rules")
async def list_rules(
    response: Response,
    severity: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """正则规则列表（分页）。"""
    items, total = await _repo(session).list_rules(severity=severity, offset=offset, limit=limit)
    # 同词条列表：总数走响应头（前端 unwrapPage 的统一契约）
    response.headers["X-Total-Count"] = str(total)
    return ok([serialize_rule(item) for item in items])


@router.post("/rules")
async def create_rule(
    body: ComplianceRuleCreateRequest,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """新增正则规则（语法在入参校验里就拦住；重复 pattern → 409）。"""
    repo = _repo(session)
    if await repo.find_rule(body.pattern) is not None:
        raise ApiError(409, "该正则已存在")
    record = await repo.create_rule(
        pattern=body.pattern, severity=body.severity, suggestion=body.suggestion
    )
    await session.commit()
    return ok(serialize_rule(record))


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: ComplianceRuleUpdateRequest,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """更新正则规则。"""
    repo = _repo(session)
    record = await repo.get_rule(to_uuid(rule_id))
    if record is None:
        raise ApiError(404, "规则不存在")
    fields = body.model_dump(exclude_unset=True)
    new_pattern = fields.get("pattern")
    if new_pattern and new_pattern != record.pattern:
        if await repo.find_rule(new_pattern) is not None:
            raise ApiError(409, "该正则已存在")
        record.pattern = new_pattern
    for field in ("severity", "suggestion"):
        if field in fields:
            setattr(record, field, fields[field])
    await session.commit()
    return ok(serialize_rule(record))


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*WRITE_ROLES)),
) -> dict:
    """删除正则规则。"""
    repo = _repo(session)
    record = await repo.get_rule(to_uuid(rule_id))
    if record is None:
        raise ApiError(404, "规则不存在")
    deleted_id = str(record.id)
    await repo.delete_rule(record)
    await session.commit()
    return ok({"id": deleted_id, "deleted": True})


# ---------------------------------------------------------------- 快照与预览


@router.get("/snapshot")
async def get_snapshot(
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """返回**下一次生成会下发**的规则快照（与 ``job:generate`` 载荷里的 ``rules`` 完全一致）。

    用途：排查「规则明明配了却没拦」——先确认快照里到底有没有它（时效过滤是最常见原因）。
    """
    snapshot = await _repo(session).snapshot()
    return ok(
        {
            "words_count": len(snapshot["words"]),
            "rules_count": len(snapshot["rules"]),
            "snapshot": snapshot,
        }
    )


@router.post("/preview")
async def preview(
    body: CompliancePreviewRequest,
    session: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_roles(*READ_ROLES)),
) -> dict:
    """对给定文本跑一次**与入队一致**的规则匹配（命中明细 + 位置 + 分数 + 是否阻断）。

    为什么用「当前快照」而不是「库里的全部规则」:
        与生成链路保持同一口径 —— 预览说会拦，实际生成就一定会拦（含时效过滤）；
        否则预览结论反而会误导运营。
    """
    matcher = compile_matcher(await _repo(session).snapshot())
    hits = matcher.check(body.text)
    words_count, rules_count = matcher.rule_count
    # score/blocked 只由**确定性规则层**决定（预览不调用模型，也不做语义兜底评估）：
    # 与 ai-engine 的 rule_score 同值，避免「预览说不拦、真实生成却转人工」的口径漂移
    return ok(
        {
            "text_length": len(body.text),
            "words_count": words_count,
            "rules_count": rules_count,
            "score": rule_score(hits),
            "blocked": any(is_blocking(hit.severity) for hit in hits),
            "hits": [
                {
                    "rule_id": hit.rule_id,
                    "kind": hit.kind,
                    "keyword": hit.keyword,
                    "severity": hit.severity,
                    "reason": hit.reason,
                    "start": hit.start,
                    "end": hit.end,
                    "blocking": is_blocking(hit.severity),
                }
                for hit in hits
            ],
        }
    )
