// backend 契约类型（与 ProductAssistant backend-api 的响应形状一一对应）。
//
// 与 ProductPilot 的差异（照抄 PP 类型会埋雷）：
//   · 错误信封是 `{code, data, message}`（**没有** `detail` 字段）；
//   · 商品详情额外带 `active_job_status` / `active_job_error`；
//   · 商品**没有** `last_reject_*` 字段（PA 不冗余存驳回信息，改由审批历史接口取）；
//   · 触发生成返回 `{thread_id, status, message}`（**不含** stream_url，前端自拼流地址）；
//   · CSV 导入成功是 `{created, skus}`，失败是 `400 + data.row_errors[{line,sku_code,reason}]`；
//   · 审批项额外带 `approver_name` 与 `notifications[]`（PA 在 P7 扩展的字段）。

/** 统一响应封装：`{code, data, message}`。 */
export interface Envelope<T> {
  code: number;
  data: T;
  message: string;
}

/** 登录/续签返回的 access 令牌（**refresh 不在响应体里**：它走 HttpOnly Cookie）。 */
export interface TokenData {
  access_token: string;
  token_type: string;
  expires_in: number;
}

/** 当前登录身份（`GET /auth/me`；前端据此做角色化 UI）。 */
export interface CurrentUser {
  id: string;
  org_id: string;
  username: string;
  role: string;
  /**
   * 平台超管标记（`sys_users.is_superuser`）。
   *
   * 为真时界面显示「组织选择器」，选中的组织会作为 `X-Org-Id` 头随每个请求发出
   * （见 `services/orgScope.ts` 与 backend `core/deps`）。**它只是展示条件**：
   * 服务端每次都重新查库判定，客户端无法伪造。
   */
  is_superuser?: boolean;
}

/** 组织（租户）——`GET /orgs`；超管拿全量，普通 admin 只拿自己那一个。 */
export interface Org {
  id: string;
  name: string;
  /** `active` | `suspended`（已停用的组织不可作为切换目标） */
  status: string;
}

/** 组织成员（`GET /users`；**绝不含** `hashed_password`）。 */
export interface Member {
  id: string;
  username: string;
  role: string;
  status: string;
  last_login_at?: string | null;
}

/** 商品主记录；列表与详情同形状，详情接口**额外**带 `active_job_status` / `active_job_error`。 */
export interface Product {
  id: string;
  org_id: string;
  sku_code: string;
  title: string;
  base_price: number;
  stock_status: string;
  status: string;
  active_thread_id?: string | null;
  /** 运营上传的商品图（OSS 公有 URL 列表；规范形状=字符串数组） */
  raw_images?: string[];
  created_at?: string | null;
  updated_at?: string | null;
  /** 详情接口额外附带：进行中任务状态（generating/waiting_input），无任务为 null */
  active_job_status?: string | null;
  /** 详情接口额外附带：任务失败原因（failed 时非空，供运营判断是数据问题还是系统问题） */
  active_job_error?: string | null;
}

/** 触发生成的返回（**不含**流地址：前端自拼 `/products/{id}/stream` + 短时票据）。 */
export interface GenerateResult {
  thread_id: string;
  status: string;
  message?: string;
}

/** 商品写入载荷（base_price 允许数字或字符串：后端是 Decimal，前端表单常给字符串）。 */
export interface ProductWritePayload {
  sku_code?: string;
  title?: string;
  base_price?: number | string;
  stock_status?: string;
  /** 整体覆盖语义（不是增量合并）：删图就是「写回少了那张的列表」 */
  raw_images?: string[];
}

/** 图文详情的最小单元（text + image 混排；与 ai-engine content_store 的 blocks 一致）。 */
export interface ContentBlock {
  type: string;
  text?: string;
  url?: string;
  alt?: string;
  source?: "uploaded" | "ai_generated" | string;
  width?: number;
  height?: number;
}

/** 图文正文载荷（blocks 数组；与 ai-engine `content_store` 的 `blocks` 同形状）。 */
export interface ContentData {
  blocks?: ContentBlock[];
}

/** 一版已保存的图文内容（**批准后才落库**，因此 `is_approved` 是版本级标记，不是草稿态）。 */
export interface ContentVersion {
  id: string;
  version: number;
  content_data: ContentData;
  is_approved: boolean;
  model_name?: string | null;
  prompt_version?: string | null;
  thread_id: string;
  created_at?: string | null;
}

/** AI 思考轨迹（`schema_pa_ai.evaluation_logs`，backend 只读）。 */
export interface EvaluationLog {
  id: string;
  thread_id: string | null;
  /** rule=确定性规则层（无 LLM 打分）；llm=语义兜底评估 */
  evaluator_type: "rule" | "llm" | string;
  score: number | null;
  errors: Array<{ keyword?: string; reason?: string } | string> | unknown;
  latency_ms?: number | null;
  llm_usage?: unknown;
  rule_id?: string | null;
  created_at?: string | null;
}

/** 审批快照（`hitl_approvals.content_snapshot`；批准前文案不落 product_contents）。 */
export interface ContentSnapshot {
  /** 转人工原因：high_value=高价商品需放行；quality_exhausted=质量多次未达标 */
  reason?: string;
  content?: ContentData | null;
  evaluation_result?: {
    passed?: boolean;
    score?: number | string | null;
    violations?: unknown[];
    facts_checked?: unknown[];
    errors?: unknown[];
  } | null;
  evaluation_attempts?: number | null;
}

/** 审批项（列表与详情同形状；列表不带 `content_snapshot`，只给 `snapshot_summary`）。 */
export interface Approval {
  id: string;
  org_id: string;
  product_id: string;
  thread_id: string;
  status: string;
  channel: string;
  feedback?: string | null;
  approver_id?: string | null;
  /** backend join `sys_users` 补全（P7 扩展）；用户已删除时为 null */
  approver_name?: string | null;
  /** 通知投递状态（P7 扩展；无 outbox 行为 []） */
  notifications?: ApprovalNotification[];
  /** 补投痕迹（无记录为 null；见 `approval_redrive_audits`） */
  redrive?: ApprovalRedrive | null;
  /** 人工放行记录（仅"带命中点仍被批准"的单存在；见 `approval_overrides`） */
  override?: ApprovalOverrideInfo | null;
  snapshot_summary?: {
    reason?: string | null;
    score?: number | string | null;
    violation_count?: number | null;
    evaluation_attempts?: number | null;
  };
  created_at?: string | null;
  resolved_at?: string | null;
  expire_at?: string | null;
  product_title?: string;
  sku_code?: string;
  product_status?: string;
  /** 仅 `with_snapshot=1` 或详情接口返回 */
  content_snapshot?: ContentSnapshot | null;
}

/** 单渠道外发通知的投递状态（`notification_outbox` 只读视图）。 */
export interface ApprovalNotification {
  channel: "email" | "feishu" | "dingtalk" | string;
  status: "pending" | "sent" | "dlq" | string;
  retry_count: number;
  next_retry_at?: string | null;
  provider_msg_id?: string | null;
  /** 最后失败原因（仅失败时非空；来自 payload.last_error，诊断字段） */
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** 补投痕迹（`approval_redrive_audits` 只读汇总；无记录时为 null）。 */
export interface ApprovalRedrive {
  count: number;
  last_at?: string | null;
  /** enqueued=真投递成功；not_needed=引擎已消费（空操作）；throttled=节流窗口内已投过；enqueue_failed=投递失败 */
  last_outcome?: "enqueued" | "not_needed" | "throttled" | "enqueue_failed" | string | null;
}

/** 人工放行记录（`approval_overrides`；仅"带评估命中点仍被批准"的单存在）。 */
export interface ApprovalOverrideInfo {
  reason: string;
  violation_count: number;
  at?: string | null;
}

/** 深链票据解析结果（匿名可访问；**不授予审批权限**）。 */
export interface DeeplinkResult {
  valid: boolean;
  approval_id: string;
  product_id: string;
  org_id: string;
}

/** OSS 预签名直传（`POST /oss/presign`）。 */
export interface PresignResult {
  upload_url: string;
  public_url: string;
  object_key: string;
  expires_in: number;
  content_type: string;
  max_upload_bytes: number;
}

/** CSV 导入结果（**PA 形状**：成功 `{created, skus}`；失败 400 + `data.row_errors`）。 */
export interface CsvImportResult {
  created: number;
  skus: string[];
}

/** CSV 导入的逐行错误（来自 `400 + data.row_errors`；`line` 是原始文件行号便于定位）。 */
export interface CsvRowError {
  line: number;
  sku_code?: string;
  reason: string;
}

// ============================== 合规（P6/P7）=================================

/** 违禁词（**全局配置，无 org_id**：改一个词影响所有组织 → 写操作仅 admin）。 */
export interface ComplianceWord {
  id: string;
  word: string;
  severity: string;
  source?: string | null;
  effective_at?: string | null;
  expires_at?: string | null;
  /** 当前是否生效（已生效且未过期）；前端据此标注「未生效/已过期」 */
  is_active: boolean;
}

/** 合规正则规则（**全局配置，无 org_id**；语法在写入入口即校验，坏正则不会入库）。 */
export interface ComplianceRule {
  id: string;
  pattern: string;
  type: string;
  severity: string;
  suggestion?: string | null;
}

/** 下一次生成会下发的规则快照（与 `job:generate` 载荷里的 `rules` **完全一致**）。 */
export interface ComplianceSnapshot {
  words: Array<{ id?: string; word: string; severity: string; source?: string | null }>;
  rules: Array<{ id?: string; pattern: string; severity: string; suggestion?: string | null }>;
}

/** 快照接口响应（计数用于「规则明明配了却没拦」的快速排查，快照给完整内容）。 */
export interface ComplianceSnapshotResponse {
  words_count: number;
  rules_count: number;
  snapshot: ComplianceSnapshot;
}

/** 一次确定性命中（含 span：这是 backend 预览相对 ai-engine 额外返回的字段，供高亮）。 */
export interface ComplianceHit {
  rule_id?: string | null;
  kind: "word" | "regex" | string;
  keyword: string;
  severity: string;
  reason: string;
  start: number;
  end: number;
  blocking: boolean;
}

/** 预览结果（`score`/`blocked` **只由确定性规则层决定**：预览不调用模型）。 */
export interface CompliancePreviewResult {
  text_length: number;
  words_count: number;
  rules_count: number;
  score: number;
  blocked: boolean;
  hits: ComplianceHit[];
}

// ============================== 运维只读面（P6/P7）=============================

/** 消费侧心跳（`alive=false` 或**一个心跳都没有**都必须按异常看，不能显示成绿色）。 */
export interface WorkerHeartbeat {
  consumer?: string;
  key?: string;
  ttl_seconds?: number;
  alive?: boolean;
  error?: string;
}

/** 一个消费组的状态（`pending`/`lag` 是实现积压判据；`consumers=0` 也要当异常）。 */
export interface StreamGroupInfo {
  name?: string;
  consumers?: number;
  pending?: number;
  last_delivered_id?: string;
  lag?: number;
}

/** 一个 Redis 流的整体状态（Redis 不可用时 `length=null` + `error`，**不抛错**）。 */
export interface StreamOverview {
  name: string;
  key: string;
  length: number | null;
  groups: StreamGroupInfo[];
  error?: string;
}

/** 某域死信队列（DLQ）的长度摘要（**只读**：清理是运维动作，接口不提供一键重投）。 */
export interface DlqSummary {
  domain?: string;
  key?: string;
  length?: number;
  error?: string;
}

/** 运维总览（`GET /ops/overview` 的聚合结果；单项失败只降低该项，不让整页报错）。 */
export interface OpsOverview {
  env: string;
  generated_at: string;
  workers: WorkerHeartbeat[];
  /** 一个心跳都没有 = 消费侧完全停滞（不是健康） */
  stalled: boolean;
  streams: StreamOverview[];
  dlq: DlqSummary[];
  /** 卡住的生成任务（超期未推进，判据与后端 reaper 同源）→ 提供「终止」入口 */
  stuck_jobs: StuckJob[];
}

/**
 * 卡住的生成任务（后端 `/ops/overview` 的 `stuck_jobs`）。
 *
 * 为什么要有这一项：卡在 running 的任务会让商品**永久 409**（详情页按钮也变成不可点），
 * 过去只能人肉改库；有了这份清单，运维可以「看见 → 立刻终止」（写审计）。
 */
export interface StuckJob {
  job_id: string;
  thread_id: string;
  product_id: string;
  sku_code?: string | null;
  product_status?: string | null;
  job_status?: string;
  /** 距今多久没推进（秒） */
  age_seconds?: number | null;
  /** 指标级失败（单个查询失败不影响整页） */
  error?: string;
}

/** 终止任务的结果（`POST /ops/jobs/{id}/abort`）。 */
export interface AbortJobResult {
  job_id: string;
  thread_id: string;
  product_id: string;
  job_status: string;
  product_status?: string | null;
  /** 是否同时把商品放回 draft（False = 商品已被更新的线程接管，只收口了任务） */
  product_released: boolean;
  already_terminal: boolean;
}

/** 一条死信消息（`payload` 为原始载荷，供人工判断根因；坏载荷给 `decode_error`）。 */
export interface DlqEntry {
  seq: string;
  payload?: Record<string, unknown> | null;
  decode_error?: string;
}

/** 某域死信的明细列表（`length` 是全量长度，`entries` 是本页取样）。 */
export interface DlqEntries {
  domain: string;
  key: string;
  length: number;
  entries: DlqEntry[];
}

