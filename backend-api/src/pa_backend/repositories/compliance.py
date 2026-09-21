"""合规规则仓储：为生成任务提供「入队瞬间固化」的规则快照，并提供词库/规则的 CRUD。

边界（``database/sql/0002_roles_grants.sql``）:
    ``compliance_words`` / ``compliance_rules`` 由 ``role_pa_backend`` 全权读写；
    ``role_pa_ai`` 对这些表**零权限** —— 所以规则只能经 ``job:generate`` 载荷随带，
    这也正是「快照必须在 backend 侧固化」的原因。

租户维度（容易误判）:
    这两张表**没有 ``org_id`` 列**（全局规则表，其余业务表都带 ``org_id``）。
    因此本仓储不带租户过滤；写侧的越权防护靠「只有 admin 能改」（路由层 ``require_roles``）。

写入口径（P6）:
    表上**没有唯一约束**，故新增/改名一律先 ``find_word`` / ``find_rule`` 查重（重复词会让
    同一片段被重复上报、重复扣分）；正则语法在**入参校验层**就拦（坏正则被 ai-engine 静默跳过）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.orm import ComplianceRule, ComplianceWord

__all__ = ["ComplianceRepo", "build_rules_snapshot"]


async def build_rules_snapshot(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """构造入队快照：当前生效的违规词 + 全部正则规则。

    参数:
        session: DB 会话。
        now: 时间基准（默认当前 UTC；测试可注入以验证时效过滤）。
    返回:
        形如 ``{"words": [{"id","word","severity","source"}], "rules": [{"id","pattern","severity","suggestion"}]}``
        —— **键名与 ai-engine ``compile_rules_snapshot`` 读取的键逐一对齐**。
    注意:
        时效过滤（``effective_at <= now`` 且未过期）**必须在这里做**：
        ai-engine 侧不读库、也不做时间判断（其 ``src/`` 内无 ``effective_at`` 引用），
        漏过滤会让「已过期词」长期继续拦截。
    """
    moment = now or datetime.now(timezone.utc)
    words = (
        (
            await session.execute(
                select(ComplianceWord).where(
                    ComplianceWord.effective_at <= moment,
                    or_(ComplianceWord.expires_at.is_(None), ComplianceWord.expires_at > moment),
                )
            )
        )
        .scalars()
        .all()
    )
    rules = (await session.execute(select(ComplianceRule))).scalars().all()
    return {
        "words": [
            {"id": str(word.id), "word": word.word, "severity": word.severity, "source": word.source}
            for word in words
        ],
        "rules": [
            {
                "id": str(rule.id),
                "pattern": rule.pattern,
                "severity": rule.severity,
                "suggestion": rule.suggestion,
            }
            for rule in rules
        ],
    }


class ComplianceRepo:
    """合规规则读写（全局表，无租户维度）。

    租户口径（容易误判，故再强调一次）:
        ``compliance_words`` / ``compliance_rules`` **没有 org_id** —— 它们是全局配置，
        改动会影响**所有组织**的生成结果。因此写侧必须收口到 ``admin``（路由层保证），
        且**写入前做重复检测**（表上没有唯一约束，重复词会让同一片段被重复上报扣分）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """初始化。

        参数:
            session: DB 会话。
        """
        self.session = session

    async def list_words(
        self,
        *,
        severity: str | None = None,
        active_only: bool = False,
        keyword: str | None = None,
        offset: int = 0,
        limit: int = 50,
        now: datetime | None = None,
    ) -> tuple[list[ComplianceWord], int]:
        """分页列出词条（可选按严重级 / 仅生效中 / 词面模糊过滤）。

        参数:
            severity: ``high`` / ``medium`` / ``low``；None = 全部。
            active_only: True 时只返回当前生效（``effective_at <= now < expires_at``）。
            keyword: 词面子串（大小写不敏感）。
            offset / limit: 分页。
            now: 时间基准（测试注入）。
        返回:
            ``(items, total)``；按「严重级 → 词面」排序，便于人工核对词表。
        """
        moment = now or datetime.now(timezone.utc)
        stmt = select(ComplianceWord)
        if severity:
            stmt = stmt.where(ComplianceWord.severity == severity)
        if active_only:
            stmt = stmt.where(
                ComplianceWord.effective_at <= moment,
                or_(ComplianceWord.expires_at.is_(None), ComplianceWord.expires_at > moment),
            )
        if keyword:
            stmt = stmt.where(ComplianceWord.word.ilike(f"%{keyword}%"))
        total = int(
            (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(ComplianceWord.severity, ComplianceWord.word)
                    .offset(max(0, offset))
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def get_word(self, word_id: uuid.UUID) -> ComplianceWord | None:
        """按 id 取词条（不存在返回 None）。"""
        return (
            await self.session.execute(select(ComplianceWord).where(ComplianceWord.id == word_id))
        ).scalar_one_or_none()

    async def find_word(self, word: str) -> ComplianceWord | None:
        """按词面精确查找（写入前的重复检测用）。"""
        return (
            await self.session.execute(select(ComplianceWord).where(ComplianceWord.word == word))
        ).scalar_one_or_none()

    async def create_word(
        self,
        *,
        word: str,
        severity: str,
        source: str | None = None,
        effective_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ComplianceWord:
        """新增词条（调用方负责 commit 与去重判定）。"""
        record = ComplianceWord(
            word=word,
            severity=severity,
            source=source,
            expires_at=expires_at,
            **({"effective_at": effective_at} if effective_at else {}),
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def delete_word(self, record: ComplianceWord) -> None:
        """删除词条（合规词表是配置数据，允许物理删除；本表无变更历史，属已知缺口）。"""
        await self.session.delete(record)
        await self.session.flush()

    async def list_rules(
        self, *, severity: str | None = None, offset: int = 0, limit: int = 50
    ) -> tuple[list[ComplianceRule], int]:
        """分页列出正则规则（可按严重级过滤）。"""
        stmt = select(ComplianceRule)
        if severity:
            stmt = stmt.where(ComplianceRule.severity == severity)
        total = int(
            (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        )
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(ComplianceRule.severity, ComplianceRule.pattern)
                    .offset(max(0, offset))
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def get_rule(self, rule_id: uuid.UUID) -> ComplianceRule | None:
        """按 id 取规则。"""
        return (
            await self.session.execute(select(ComplianceRule).where(ComplianceRule.id == rule_id))
        ).scalar_one_or_none()

    async def find_rule(self, pattern: str) -> ComplianceRule | None:
        """按正则串精确查找（写入前的重复检测用）。"""
        return (
            await self.session.execute(select(ComplianceRule).where(ComplianceRule.pattern == pattern))
        ).scalar_one_or_none()

    async def create_rule(
        self, *, pattern: str, severity: str, suggestion: str | None = None
    ) -> ComplianceRule:
        """新增正则规则（调用方负责 commit、语法校验与去重判定）。"""
        record = ComplianceRule(pattern=pattern, type="regex", severity=severity, suggestion=suggestion)
        self.session.add(record)
        await self.session.flush()
        return record

    async def delete_rule(self, record: ComplianceRule) -> None:
        """删除正则规则。"""
        await self.session.delete(record)
        await self.session.flush()

    async def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """见模块级 ``build_rules_snapshot``（实例方法形式，便于依赖注入）。"""
        return await build_rules_snapshot(self.session, now=now)
