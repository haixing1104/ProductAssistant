-- ============================================================================
-- ProductAssistant | database/sql/0006_approval_audits.sql
--   ① approval_redrive_audits —— 「审批补投」审计
--   ② approval_overrides      —— 「人工放行带合规命中点」审计
-- 授权： role_pa_backend SELECT/INSERT ;  role_pa_ai 无权限
-- 幂等： IF NOT EXISTS / DROP TRIGGER IF EXISTS，可重复执行（pgsql-setup.sh 逐文件灌入）
--
-- 为什么需要它们（2026-09 实测）:
--   ① 审批中心的「补投」按钮此前**没有任何痕迹**：点了之后既看不到"投了几次、结果如何"，
--      也分不清「引擎已收到（无需补投）」与「投出去了但没被消费」——行为上表现为"没真的补投"。
--      实测语义：对已跑完的线程再 resume 是**静默 no-op**（不报错、不重复落库、不翻转决策），
--      因此"是否需要补投"必须由后端判定并留痕，而不是让按钮假装成功。
--   ② 合规命中 → 转人工 → 审批人一点「批准」即可上架，既无强制说明也无留痕：
--      事后无法回答"谁在知情下放行了哪条命中点"。本表把放行理由与命中点快照固化下来。
--   两表与 0004/0005 同一形态：**只增不改**（REVOKE UPDATE/DELETE）。
-- ============================================================================

SET ROLE role_pa_admin;

CREATE TABLE IF NOT EXISTS schema_pa_backend.approval_redrive_audits (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    approval_id   uuid NOT NULL,
    thread_id     uuid NOT NULL,
    -- 可为 NULL：补投守护（后台扫描）触发的补投没有人类操作者
    actor_user_id uuid,
    -- enqueued / not_needed / throttled / enqueue_failed（见 services/approval_service.redrive）
    outcome       text NOT NULL,
    reason        text,
    redriven_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_redrive_audits_approval
    ON schema_pa_backend.approval_redrive_audits (org_id, approval_id, redriven_at);

-- 无 updated_at 列：审计只追加，不存在"最后修改时间"这种可被改写的信息
DROP TRIGGER IF EXISTS trg_approval_redrive_audits_updated ON schema_pa_backend.approval_redrive_audits;

GRANT SELECT, INSERT ON schema_pa_backend.approval_redrive_audits TO role_pa_backend;
REVOKE UPDATE, DELETE ON schema_pa_backend.approval_redrive_audits FROM role_pa_backend;

CREATE TABLE IF NOT EXISTS schema_pa_backend.approval_overrides (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL,
    approval_id     uuid NOT NULL,
    thread_id       uuid NOT NULL,
    actor_user_id   uuid NOT NULL,
    violation_count integer NOT NULL,
    -- 命中点快照（content_snapshot.evaluation_result.violations）：回查"当时到底命中了什么"
    violations      jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason          text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_approval_overrides_approval
    ON schema_pa_backend.approval_overrides (org_id, approval_id, created_at);

DROP TRIGGER IF EXISTS trg_approval_overrides_updated ON schema_pa_backend.approval_overrides;

GRANT SELECT, INSERT ON schema_pa_backend.approval_overrides TO role_pa_backend;
REVOKE UPDATE, DELETE ON schema_pa_backend.approval_overrides FROM role_pa_backend;

RESET ROLE;
