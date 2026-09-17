"""business_reader：业务数据只读端口（Agent 工具取数的唯一合法入口）。

职责:
    把「ai-engine 有权读到的业务数据」收敛成 4 个只读方法，供 Agent 工具取数：
      · read_product             → 当前商品素材（价格 / 库存 / 状态 / 图片）
      · list_content_versions    → 本商品历史文案版本（schema_pa_ai.product_contents）
      · list_eval_logs           → 本商品历史评估记录（schema_pa_ai.evaluation_logs）
      · list_approvals           → 本商品历史人工审批记录（schema_pa_backend.hitl_approvals）

权限依据（database/sql/0002_roles_grants.sql，逐条对齐，勿凭印象扩展）:
    role_pa_ai 有权限的表：schema_pa_backend.products（仅 SELECT）、
        schema_pa_backend.hitl_approvals（仅 SELECT）、
        schema_pa_ai.product_contents / evaluation_logs（全 DML）。
    role_pa_ai 无权限的表：organizations / sys_users / compliance_words /
        compliance_rules / generation_jobs / notification_outbox（不授即拒）。
    因此本端口**不提供**、也不应新增以下能力（会直接拒绝或被数据库拒绝）：
      ✗ 组织名 / 用户姓名（sys_users 不可读 → 审批人只能给 approver_id）
      ✗ 违禁词库 / 合规正则直读（compliance_* 不可读 →
        合规数据只能走 job:generate 载荷里的 rules 快照，见 adapters/rules.py）
      ✗ 任务状态机（generation_jobs 不可读；商品状态只能读 products.status 的只读快照）
      ✗ 任何写入（商品基础信息与状态推进归 backend）

多租户:
    所有方法强制携带 org_id 作为第一过滤条件；实现方不得提供「不带 org_id」的重载，
    Agent 工具侧也从 State 注入 org_id，绝不由模型参数决定（见 workflowcore/agent/tools.py）。

说明:
    实现见 adapters/pg_store.PgBusinessReader（只读 SQL，无写语句）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

__all__ = ["BusinessReader"]


class BusinessReader(ABC):
    """业务数据只读端口：Agent 工具的取数边界（只 SELECT，不写任何表）。"""

    @abstractmethod
    def read_product(self, *, org_id: str, product_id: str) -> dict[str, Any] | None:
        """读取单个商品素材（当前商品的价格 / 库存 / 状态 / 图片等）。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件，必填）。
            product_id: 商品 ID（uuid 字符串）。
        返回:
            商品字段 dict（至少含 id / org_id / sku_code / title / base_price /
            stock_status / status / raw_images）；未命中返回 None（由工具转成明确提示，
            而不是抛异常）。
        异常:
            Exception: 数据库错误（连接失败、权限不足等）由实现方抛出；
                工具层经中间件兜底为失败结果，不阻断生成主链路。
        """

    @abstractmethod
    def list_content_versions(
        self, *, org_id: str, product_id: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        """列出本商品的历史文案版本（新→旧），供生成阶段避免重复踩坑。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件，必填）。
            product_id: 商品 ID。
            limit: 返回条数上限（实现方保证 >= 1）。
        返回:
            每条含 version / is_approved / model_name / text_excerpt / created_at 的 dict 列表；
            无版本时返回 []（新商品冷启动属正常，不报错）。
        注意:
            只返回节选文本（text_excerpt），不回传完整 blocks，避免把长文塞进模型上下文。
        """

    @abstractmethod
    def list_eval_logs(
        self, *, org_id: str, product_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """列出本商品的历史评估记录（新→旧），供生成时规避历史违规点。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件，必填）。
            product_id: 商品 ID。
            limit: 返回条数上限（实现方保证 >= 1）。
        返回:
            每条含 evaluator_type / score / errors / rule_id / created_at 的 dict 列表；
            无记录时返回 []。
        """

    @abstractmethod
    def list_approvals(
        self, *, org_id: str, product_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """列出本商品的历史人工审批记录（新→旧），供生成参考驳回意见。

        参数:
            org_id: 组织 ID（多租户隔离的第一过滤条件，必填）。
            product_id: 商品 ID。
            limit: 返回条数上限（实现方保证 >= 1）。
        返回:
            每条含 status / channel / feedback / approver_id / resolved_at 的 dict 列表；
            无记录时返回 []。
        注意:
            approver_id 只有 id，没有姓名——sys_users 对 role_pa_ai 未授权（见模块 docstring）。
        """
