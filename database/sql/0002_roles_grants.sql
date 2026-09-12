-- ============================================================================
-- ProductAssistant | database/sql/0002_roles_grants.sql
--
-- 用途    :  落地权限矩阵（GRANT 即“机器可执行的架构红线”）
-- 执行账号 : 引导超级用户（见 infra/.env）
-- 执行顺序 : 依赖 0001_schema.sql 已建角色/schema/表；随后执行 0003_seed.sql
-- 幂等性   : 全新库初始化一次；重复执行 GRANT/DEFAULT PRIVILEGES 是幂等的
-- 说明     : 应用账号的“全权限”= 表数据 DML（SELECT/INSERT/UPDATE/DELETE），
--            DDL（含建表/加列/加外键）一律归 role_pa_admin
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) schema USAGE / CREATE（跨 schema 读写的前提是获得另一域 schema 的 USAGE）

--  Role:
-- role_pa_admin 管理员角色，拥有所有权限 (DDL)

-- role_pa_backend 负责管理backend (DML)
-- role_pa_ai 负责管理ai-engine (DML)
-- role_pa_ai_setup LangGraph建表使用（CREATE） PostgresSaver 框架建表专用

-- Schema:
-- schema_pa_backend 属 backend-api 域 负责核心业务
-- schema_pa_ai 属 ai-engine 域。
-- ----------------------------------------------------------------------------

-- role_pa_backend：读 schema_pa_ai 的 product_contents/evaluation_logs（展示与 Trace 面板）
GRANT USAGE ON SCHEMA schema_pa_backend TO role_pa_backend;
GRANT USAGE ON SCHEMA schema_pa_ai TO role_pa_backend;

-- role_pa_ai：读 schema_pa_backend 的 products/hitl_approvals（AI 只读素材/审计展示）
GRANT USAGE ON SCHEMA schema_pa_backend TO role_pa_ai;
GRANT USAGE ON SCHEMA schema_pa_ai TO role_pa_ai;

-- role_pa_ai_setup：仅 role_pa_ai 内建表权  PostgresSaver .setup()
GRANT USAGE, CREATE ON SCHEMA schema_pa_ai TO role_pa_ai_setup;


-- ----------------------------------------------------------------------------
-- 2) schema_pa_backend 逐表授权（backend-api 全权 / ai-engine 视行而定）
-- ----------------------------------------------------------------------------

-- 矩阵行：organizations / sys_users / compliance_* / generation_jobs /
--          notification_outbox —— role_pa_backend 全权，role_pa_ai 无任何权限（不授即拒）
GRANT SELECT, INSERT, UPDATE, DELETE ON
    schema_pa_backend.organizations, schema_pa_backend.sys_users,
    schema_pa_backend.compliance_words, schema_pa_backend.compliance_rules,
    schema_pa_backend.generation_jobs, schema_pa_backend.notification_outbox
 TO role_pa_backend;

 -- 矩阵行：products —— role_pa_backend 全权；role_pa_ai  仅 SELECT（AI 只读素材）
 GRANT SELECT, INSERT, UPDATE, DELETE ON schema_pa_backend.products TO role_pa_backend;
 GRANT SELECT ON schema_pa_backend.products TO role_pa_ai;

 -- 矩阵行：hitl_approvals —— role_pa_backend 全权；role_pa_ai 仅 SELECT（审计展示）
 GRANT SELECT, INSERT, UPDATE, DELETE ON schema_pa_backend.hitl_approvals TO role_pa_backend;
 GRANT SELECT ON schema_pa_backend.hitl_approvals TO role_pa_ai;

 -- --------------------------------------------------------------------------------------
-- 3) schema_pa_ai 逐表授权（role_pa_backend 仅 SELECT  只读；role_pa_ai 全权；langgraph 黑盒零接触）
-- ----------------------------------------------------------------------------------
GRANT SELECT ON
    schema_pa_ai.product_contents, schema_pa_ai.evaluation_logs TO role_pa_backend;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    schema_pa_ai.product_contents, schema_pa_ai.evaluation_logs TO role_pa_ai;

-- ------------------------------------------------------------------------------------------------
-- 4) ALTER DEFAULT PRIVILEGES —— 让“未来新建的表”自动落入同一授权模式，避免每次迁移后手动补 GRANT
-- -----------------------------------------------------------------------------------------------

-- -- role_pa_admin 在 schema_pa_backend 新建的表（后续迁移新增业务表）→ 自动全 DML 给 role_pa_backend
ALTER DEFAULT PRIVILEGES FOR ROLE role_pa_admin IN SCHEMA schema_pa_backend
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO role_pa_backend;


-- -- role_pa_admin 在 schema_pa_ai 新建的表（后续迁移新增 AI 表）→ 自动全 DML 给 role_pa_ai
ALTER DEFAULT PRIVILEGES FOR ROLE role_pa_admin IN SCHEMA schema_pa_ai
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO role_pa_ai;

-- --role_pa_ai_setup 由 PostgresSaver .setup() 新建的 checkpoint 系列表 → 自动给 role_pa_ai
ALTER DEFAULT PRIVILEGES FOR ROLE role_pa_ai_setup IN SCHEMA schema_pa_ai
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO role_pa_ai;
