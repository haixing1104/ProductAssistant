-- ============================================================================
-- ProductAssistant | database/sql/0007_superuser.sql  -- 平台超管标记（跨租户运维）
--
-- 用途    :  给 sys_users 增加 is_superuser 标记。带该标记的账号允许通过
--            ``X-Org-Id`` 请求头把「当前租户」覆盖为目标组织
--            （见 backend-api core/deps.get_current_user），从而看到并操作
--            所有租户的数据（商品 / 审批 / 成员 / 运维）。
-- 执行账号 : 引导超级用户（见 infra/.env）
-- 执行顺序 : 依赖 0001/0002 先执行（表结构与授权基线）
-- 幂等性   : ADD COLUMN IF NOT EXISTS —— 可重复执行
--
-- 安全（红线，改前请先读）:
--   · 该列**只能由引导脚本写入**（backend-api/src/pa_backend/tools/seed_super_admin.py）；
--     运行期任何 HTTP 入参都不得设置它，否则等于把「跨租户」变成自助开关；
--   · 授权无需补：表级 GRANT 覆盖后续新增列（见 0002_roles_grants.sql）。
-- ============================================================================

SET ROLE role_pa_admin;

ALTER TABLE schema_pa_backend.sys_users
    ADD COLUMN IF NOT EXISTS is_superuser boolean NOT NULL DEFAULT false;

RESET ROLE;
