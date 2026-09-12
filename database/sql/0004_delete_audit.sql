-- ============================================================================
-- ProductAssistant | database/sql/0004_delete_audit.sql  -- admin 彻底删除条目的审计表
-- 授权： role_pa_backend INSERT/SELECT ;  role_pa_ai 无权限
-- ============================================================================

SET ROLE role_pa_admin;

CREATE TABLE IF NOT EXISTS schema_pa_backend.delete_audits (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    product_id    uuid NOT NULL,
    sku_code      text NOT NULL,
    title         text NOT NULL,
    actor_user_id uuid NOT NULL,
    reason        text NOT NULL,
    deleted_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delete_audits_org ON schema_pa_backend.delete_audits (org_id, deleted_at);

GRANT SELECT, INSERT ON schema_pa_backend.delete_audits TO role_pa_backend;

REVOKE UPDATE, DELETE ON schema_pa_backend.delete_audits FROM role_pa_backend;

RESET ROLE;
