-- ============================================================================
-- ProductAssistant | database/sql/0005_job_abort_audit.sql  -- 「运维手工终止生成任务」审计表
-- 授权： role_pa_backend INSERT/SELECT ;  role_pa_ai 无权限
-- 幂等： IF NOT EXISTS / DROP TRIGGER IF EXISTS，可重复执行（pgsql-setup.sh 逐文件灌入）
--
-- 为什么需要它:
--   卡在 running 的生成任务会让商品永久 409（前端按钮也因 status='generating' 被禁用），
--   过去只能改数据库且**不留痕**。本表给「谁在什么时候、因为什么终止了哪个任务」留下可审计记录，
--   与 delete_audits（0004）同一形态：**只增不改**（REVOKE UPDATE/DELETE）。
-- ============================================================================

SET ROLE role_pa_admin;

CREATE TABLE IF NOT EXISTS schema_pa_backend.job_abort_audits (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    job_id        uuid NOT NULL,
    thread_id     uuid NOT NULL,
    product_id    uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    reason        text NOT NULL,
    aborted_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_abort_audits_org ON schema_pa_backend.job_abort_audits (org_id, aborted_at);
CREATE INDEX IF NOT EXISTS idx_job_abort_audits_thread ON schema_pa_backend.job_abort_audits (thread_id);

-- 无 updated_at 列：审计只追加，不需要「最后修改时间」这种可被改写的信息
DROP TRIGGER IF EXISTS trg_job_abort_audits_updated ON schema_pa_backend.job_abort_audits;

GRANT SELECT, INSERT ON schema_pa_backend.job_abort_audits TO role_pa_backend;

REVOKE UPDATE, DELETE ON schema_pa_backend.job_abort_audits FROM role_pa_backend;

RESET ROLE;
