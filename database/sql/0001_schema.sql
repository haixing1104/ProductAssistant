-- ============================================================================
-- ProductAssistant | database/sql/0001_schema.sql
-- 用途    : P1 初始化 —— 创建 4 个数据库账号、schema_pa_backend/schema_pa_ai 两个 schema 及全量表结构
-- 执行账号 : 引导超级用户（见 infra/.env）
-- 执行顺序 : 本文件 → 0002_roles_grants.sql → 0003_seed.sql（依赖 0001 已建角色/schema/表）→ 0004_delete_audit.sql
-- 幂等性   : 仅用于全新库初始化（docker-entrypoint-initdb.d 空卷首启 / 测试容器 / pgsql-reset.sh 重建）
-- 版本基线 : PostgreSQL 17.x（本文件语法兼容 >= 13）
-- ============================================================================


-- ----------------------------------------------------------------------------------------
-- 创建角色账号，确保backend和ai层级隔离

-- role_pa_admin 管理员角色，拥有所有权限 (DDL)
-- role_pa_backend 负责管理backend (DML)
-- role_pa_ai 负责管理ai-engine (DML)
-- role_pa_ai_setup LangGraph建表使用（CREATE） PostgresSaver 框架建表专用
-- 角色密码由调用方经 psql 变量注入（兼容 psql 13+，不使用 \getenv）：
--   -v admin_pwd=... -v backend_pwd=... -v ai_pwd=... -v ai_setup_pwd=...
-- （infra/scripts/pgsql-setup.sh init 会自动从 infra/.env 注入这些变量）
-- -----------------------------------------------------------------------------------------

CREATE ROLE role_pa_admin    LOGIN PASSWORD :'admin_pwd';
CREATE ROLE role_pa_backend  LOGIN PASSWORD :'backend_pwd';
CREATE ROLE role_pa_ai       LOGIN PASSWORD :'ai_pwd';
CREATE ROLE role_pa_ai_setup LOGIN PASSWORD :'ai_setup_pwd';

-- ----------------------------------------------------------------------------
-- Schema 即所有权边界：schema_pa_backend 属 backend-api 域，schema_pa_ai 属 ai-engine 域。
-- 属主统一为 role_pa_admin，保证后续迁移 DDL 由同一角色执行。
-- ----------------------------------------------------------------------------
CREATE SCHEMA schema_pa_backend AUTHORIZATION role_pa_admin;
CREATE SCHEMA schema_pa_ai       AUTHORIZATION role_pa_admin;


-- 以下 DDL 以属主 role_pa_admin 身份执行，确保表 owner = role_pa_admin
-- ALTER DEFAULT PRIVILEGES FOR ROLE role_pa_admin 对未来新表自动授权
SET ROLE role_pa_admin;

-- 通用 updated_at 触发器函数（放在 schema_pa_backend，供业务表复用）
CREATE FUNCTION schema_pa_backend.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ============================================================================
-- schema_pa_backend 业务表（backend-api 域；同 schema 内部允许物理外键）
-- ============================================================================

-- 多租户根表：所有业务表（除了规则表）经 org_id 归属 organizations，业务表不含自身
CREATE TABLE schema_pa_backend.organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    status      text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','suspended')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 用户主表：admin/reviewer/operator；登录账号为同一组织内唯一
CREATE TABLE schema_pa_backend.sys_users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES schema_pa_backend.organizations(id),
    username        text NOT NULL,
    hashed_password text NOT NULL,
    role            text NOT NULL DEFAULT 'operator'
                    CHECK (role IN ('admin','reviewer','operator')),
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','disabled')),
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_sys_users_org_username UNIQUE (org_id, username)
);

-- 商品主数据：状态机 草稿→生成中→待审批→已上架；active_thread_id 指向当前生成线程
-- 红线：ai-engine 对本表仅 SELECT（AI 只读素材，绝不写商品基础信息）
CREATE TABLE schema_pa_backend.products (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           uuid NOT NULL REFERENCES schema_pa_backend.organizations(id),
    sku_code         text NOT NULL,
    title            text NOT NULL,
    base_price       numeric(12,2) NOT NULL DEFAULT 0,
    stock_status     text NOT NULL DEFAULT 'in_stock'
                     CHECK (stock_status IN ('in_stock','low_stock','out_of_stock', 'preorder')),
    status           text NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','generating','waiting_approval','published','archived', 'deleted')),
    active_thread_id uuid,
    owner_id         uuid REFERENCES schema_pa_backend.sys_users(id),
    raw_images       jsonb NOT NULL DEFAULT '[]',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_products_org_sku UNIQUE (org_id, sku_code)
);

-- 人工审批记录：CAS 状态机 pending→approved/rejected；多审批渠道[预留web/feishu/dingtalk/email； 当前只做dingtalk]
CREATE TABLE schema_pa_backend.hitl_approvals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       uuid NOT NULL REFERENCES schema_pa_backend.organizations(id),
    product_id   uuid NOT NULL REFERENCES schema_pa_backend.products(id),
    thread_id    uuid NOT NULL,
    approver_id  uuid REFERENCES schema_pa_backend.sys_users(id),
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
    channel      text NOT NULL DEFAULT 'web'
                 CHECK (channel IN ('web','feishu','dingtalk','email')),
    feedback     text,
    content_snapshot jsonb,
    notified_at  timestamptz,
    expire_at    timestamptz,
    resolved_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- 合规词典（违禁词，确定性规则层数据源）：生效/过期时间 + 来源可溯源
CREATE TABLE schema_pa_backend.compliance_words (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    word         text NOT NULL,
    severity     text NOT NULL DEFAULT 'high' CHECK (severity IN ('high','medium','low')),
    effective_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    source       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- 合规正则规则（极限词/绝对化用语等）
CREATE TABLE schema_pa_backend.compliance_rules (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pattern     text NOT NULL,
    type        text NOT NULL DEFAULT 'regex' CHECK (type IN ('regex')),
    severity    text NOT NULL DEFAULT 'high' CHECK (severity IN ('high','medium','low')),
    suggestion  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- AI 生成任务权威状态源：支撑 get_workflow_status；thread_id 全链路唯一约束
CREATE TABLE schema_pa_backend.generation_jobs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id   uuid NOT NULL,
    org_id      uuid NOT NULL REFERENCES schema_pa_backend.organizations(id),
    product_id  uuid NOT NULL REFERENCES schema_pa_backend.products(id),
    status      text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','waiting_input','succeeded','failed')),
    error       text,
    timings     jsonb NOT NULL DEFAULT '{}',
    llm_usage   jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_generation_jobs_thread UNIQUE (thread_id)
);

-- 通知可靠性的 Transactional Outbox：与 hitl_approvals 同事务写入
CREATE TABLE schema_pa_backend.notification_outbox (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           uuid NOT NULL REFERENCES schema_pa_backend.organizations(id),
    channel          text NOT NULL,
    payload          jsonb NOT NULL,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','sent','failed','dlq')),
    retry_count      integer NOT NULL DEFAULT 0,
    next_retry_at    timestamptz,
    provider_msg_id  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);



-- ============================================================================
-- schema_pa_ai 表（ai-engine 域）
--  跨 schema 引用（product_id → schema_pa_backend.products）只作逻辑引用、不建物理外键
--  为 ai-engine 未来拆分独立实例预留空间
-- ============================================================================
-- AI 生成内容版本表：同商品多版本；content_data 为结构化 blocks [{type:text|image,...}]
CREATE TABLE schema_pa_ai.product_contents (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid NOT NULL,
    product_id     uuid NOT NULL,
    thread_id      uuid NOT NULL,
    version        integer NOT NULL DEFAULT 1,
    content_data   jsonb NOT NULL,
    prompt_version text,
    model_name     text,
    is_approved    boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- AI 评估与试错日志：evaluator_type rule/llm；rule_id 关联 schema_pa_backend.compliance_rules
-- （仅逻辑引用，不建物理外键）；llm_usage 支撑通过率/成本分析
CREATE TABLE schema_pa_ai.evaluation_logs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         uuid NOT NULL,
    product_id     uuid NOT NULL,
    thread_id      uuid,
    evaluator_type text NOT NULL CHECK (evaluator_type IN ('rule','llm')),
    score          numeric(5,2),
    errors         jsonb NOT NULL DEFAULT '[]',
    latency_ms     integer,
    llm_usage      jsonb NOT NULL DEFAULT '{}',
    rule_id        uuid,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- langgraph_checkpoints 系列表不在此创建：由 PostgresSaver 首次 .setup() 自动建立


-- ============================================================================
-- 索引与触发器（查询以 org_id 过滤为第一条件）
-- ============================================================================

-- schema_pa_backend 索引
CREATE INDEX idx_products_org_status      ON schema_pa_backend.products (org_id, status);
CREATE INDEX idx_hitl_approvals_org_status ON schema_pa_backend.hitl_approvals (org_id, status);
CREATE INDEX idx_hitl_approvals_thread    ON schema_pa_backend.hitl_approvals (thread_id);
CREATE INDEX idx_generation_jobs_org_status ON schema_pa_backend.generation_jobs (org_id, status);
CREATE INDEX idx_outbox_org_status_retry  ON schema_pa_backend.notification_outbox (org_id, status, next_retry_at);

-- schema_pa_ai 索引 含跨 schema 逻辑引用的查询列
CREATE UNIQUE INDEX uq_product_contents_org_product_version ON schema_pa_ai.product_contents (org_id, product_id, version);
CREATE INDEX idx_product_contents_thread ON schema_pa_ai.product_contents (thread_id);
CREATE INDEX idx_evaluation_logs_org_product ON schema_pa_ai.evaluation_logs (org_id, product_id);
CREATE INDEX idx_evaluation_logs_thread ON schema_pa_ai.evaluation_logs (thread_id);

-- updated_at 触发器：所有业务表统一由 DB 维护该列，应用无需手动赋值
CREATE TRIGGER trg_organizations_updated    BEFORE UPDATE ON schema_pa_backend.organizations      FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_sys_users_updated        BEFORE UPDATE ON schema_pa_backend.sys_users          FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_products_updated         BEFORE UPDATE ON schema_pa_backend.products           FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_hitl_approvals_updated   BEFORE UPDATE ON schema_pa_backend.hitl_approvals     FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_compliance_words_updated BEFORE UPDATE ON schema_pa_backend.compliance_words   FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_compliance_rules_updated BEFORE UPDATE ON schema_pa_backend.compliance_rules   FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_generation_jobs_updated  BEFORE UPDATE ON schema_pa_backend.generation_jobs    FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_notification_outbox_updated BEFORE UPDATE ON schema_pa_backend.notification_outbox FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_product_contents_updated BEFORE UPDATE ON schema_pa_ai.product_contents      FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();
CREATE TRIGGER trg_evaluation_logs_updated  BEFORE UPDATE ON schema_pa_ai.evaluation_logs       FOR EACH ROW EXECUTE FUNCTION schema_pa_backend.set_updated_at();

RESET ROLE;
