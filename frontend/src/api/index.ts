// API 服务层：按域拆分，统一拆信封 `{code,data,message}`（PA 口径）。
//
// 分页约定：响应体是数组，总数在 **`X-Total-Count`** 响应头（`unwrapPage` 负责拼装）。
import { http } from "../services/http";
import { getPlatform, type PaUploadFile } from "../services/platform";
import type {
  Approval,
  AbortJobResult,
  CompliancePreviewResult,
  ComplianceRule,
  ComplianceSnapshotResponse,
  ComplianceWord,
  ContentVersion,
  CsvImportResult,
  CurrentUser,
  DeeplinkResult,
  DlqEntries,
  Envelope,
  EvaluationLog,
  GenerateResult,
  Member,
  OpsOverview,
  PresignResult,
  Product,
  ProductWritePayload,
  TokenData,
} from "../types/api";

/** 拆信封：`{code,data,message}` → `data`（失败时 axios 已 reject，不会走到这里）。 */
async function unwrap<T>(promise: Promise<{ data: Envelope<T> }>): Promise<T> {
  const resp = await promise;
  return resp.data.data;
}

/** 分页结果（**契约**：接口返回数组 + `X-Total-Count` 头，由 `unwrapPage` 拼成这个形状）。 */
export interface PageResult<T> {
  items: T[];
  total: number;
}

/** 分页入参（offset/limit 口径；页面各 API 在此基础上追加自己的筛选字段）。 */
export interface ListParams {
  offset?: number;
  limit?: number;
}

/** 服务端分页：读 X-Total-Count（缺失时退化为当前页长度，不至于把总数算成 0）。 */
async function unwrapPage<T>(promise: Promise<{ data: Envelope<T[]>; headers: unknown }>): Promise<PageResult<T>> {
  const resp = await promise;
  const headers = resp.headers as { "x-total-count"?: string };
  const total = Number(headers["x-total-count"] ?? resp.data.data.length);
  return { items: resp.data.data, total };
}

/** 认证域（注册 / 登录 / 身份 / 登出；续签由 `services/http.ts` 的拦截器负责，不在这里）。 */
export const authApi = {
  register: (payload: { org_name: string; username: string; password: string }) =>
    unwrap<{ user_id: string; org_id: string; role: string }>(http.post("/auth/register", payload)),
  /** `orgName` 仅在「同名账号跨组织」时需要（PA 的用户名只在组织内唯一）。 */
  login: (username: string, password: string, orgName?: string) =>
    unwrap<TokenData>(
      http.post("/auth/login", { username, password, ...(orgName ? { org_name: orgName } : {}) }),
    ),
  me: () => unwrap<CurrentUser>(http.get("/auth/me")),
  logout: () => unwrap<Record<string, never>>(http.post("/auth/logout")),
};

/** 成员管理域（admin 专属；可创建的角色只有 reviewer/operator，见 backend `ASSIGNABLE_ROLES`）。 */
export const membersApi = {
  list: (params?: ListParams) => unwrapPage<Member>(http.get("/users", { params })),
  create: (payload: { username: string; password: string; role: "reviewer" | "operator" }) =>
    unwrap<Member>(http.post("/users", payload)),
  disable: (id: string) => unwrap<Member>(http.post(`/users/${id}/disable`)),
};

/** 商品域（列表 / 详情 / 增改 / 彻底删除 / 触发生成 / CSV 导入）。 */
export const productsApi = {
  list: (params?: ListParams & { status?: string }) => unwrapPage<Product>(http.get("/products", { params })),
  get: (id: string) => unwrap<Product>(http.get(`/products/${id}`)),
  create: (payload: ProductWritePayload) => unwrap<Product>(http.post("/products", payload)),
  update: (id: string, payload: ProductWritePayload) => unwrap<Product>(http.patch(`/products/${id}`, payload)),
  /** 只有彻底删除一条路径（软删端点已下线 → 405）。 */
  purge: (id: string, reason: string) =>
    unwrap<{ product_id: string; sku_code: string; purge_enqueued: boolean; ai_purge_job?: string }>(
      http.delete(`/products/${id}/purge`, { data: { reason } }),
    ),
  generate: (id: string) => unwrap<GenerateResult>(http.post(`/products/${id}/generate`)),
  importCsv: async (file: PaUploadFile) => {
    const form = new FormData();
    // Web 传原生 File；RN 传 {uri, name, type}（RN 的 FormData 原生接受后者）—— 类型上统一向上转一次
    form.append("file", file as unknown as Blob);
    return unwrap<CsvImportResult>(
      http.post("/products/import-csv", form, { headers: { "Content-Type": "multipart/form-data" } }),
    );
  },
};

/** OSS 图片直传域（两步：换预签名 URL → 客户端直传，字节不经过 backend）。 */
export const ossApi = {
  /** 第一步：换预签名 PUT URL（登录态；需 product_id + content_type）。 */
  presign: (payload: { product_id: string; filename: string; content_type: string }) =>
    unwrap<PresignResult>(http.post("/oss/presign", payload)),
  /**
   * 第二步：直传 OSS（**必须带同一个 Content-Type**，它参与签名）。
   * 传输交给平台端口：Web = `fetch(PUT, File)`；RN = `expo-file-system` 的 BINARY_CONTENT PUT
   *（RN 没有 `File`/`Blob` 直传能力，这是原生端与浏览器最容易踩空的一处）。
   */
  put: (uploadUrl: string, file: PaUploadFile, contentType: string): Promise<void> =>
    getPlatform().putBinary(uploadUrl, contentType, file),
};

/** 已保存图文域（版本列表与最新版；空列表 = 还没批准过任何版本）。 */
export const contentsApi = {
  list: (productId: string) => unwrap<ContentVersion[]>(http.get(`/products/${productId}/contents`)),
  latest: (productId: string) =>
    unwrap<ContentVersion | null>(http.get(`/products/${productId}/contents/latest`)),
};

/** AI 思考轨迹域（backend 只读 `schema_pa_ai.evaluation_logs`，正序返回）。 */
export const evaluationLogsApi = {
  list: (productId: string) => unwrap<EvaluationLog[]>(http.get(`/products/${productId}/evaluation-logs`)),
};

/** 审批域（列表 / 详情 / 批准 / 驳回 / 补投 / 深链解析）。 */
export const approvalsApi = {
  /**
   * 列表：`status` 缺省 = pending（backend 兼容约定）；
   * 商品详情页的「审批与驳回复盘」必须显式传 `"all"`，否则已定案（批准/驳回）的单查不到。
   */
  list: (params?: ListParams & { status?: string; productId?: string; withSnapshot?: boolean }) =>
    unwrapPage<Approval>(
      http.get("/approvals", {
        params: {
          ...(params?.status ? { status: params.status } : {}),
          ...(params?.productId ? { product_id: params.productId } : {}),
          ...(params?.withSnapshot ? { with_snapshot: true } : {}),
          offset: params?.offset,
          limit: params?.limit,
        },
      }),
    ),
  get: (id: string) => unwrap<Approval>(http.get(`/approvals/${id}`)),
  approve: (id: string, feedback?: string) =>
    unwrap<{ approval_id: string; status: string; resume_enqueued: boolean }>(
      http.post(`/approvals/${id}/approve`, { feedback: feedback ?? null }),
    ),
  reject: (id: string, feedback?: string) =>
    unwrap<{ approval_id: string; status: string; resume_enqueued: boolean }>(
      http.post(`/approvals/${id}/reject`, { feedback: feedback ?? null }),
    ),
  /** 补投：已定案但 ai-engine 未收到（运维兜底，仅 admin）。 */
  redrive: (id: string) =>
    unwrap<{ approval_id: string; resume_enqueued: boolean }>(http.post(`/approvals/${id}/redrive`)),
  /** 深链票据解析（匿名可访问；只给「跳到哪张单」，不授予审批权限）。 */
  deeplink: (ticket: string) => unwrap<DeeplinkResult>(http.get("/approvals/deeplink", { params: { ticket } })),
};

/** 合规词库 / 规则域（**读对 reviewer 开放，写仅 admin**：全局配置影响所有组织）。 */
export const complianceApi = {
  words: (params?: ListParams & { severity?: string; activeOnly?: boolean; keyword?: string }) =>
    unwrapPage<ComplianceWord>(
      http.get("/compliance/words", {
        params: {
          ...(params?.severity ? { severity: params.severity } : {}),
          ...(params?.activeOnly ? { active_only: true } : {}),
          ...(params?.keyword ? { keyword: params.keyword } : {}),
          offset: params?.offset,
          limit: params?.limit,
        },
      }),
    ),
  createWord: (payload: {
    word: string;
    severity: string;
    source?: string | null;
    effective_at?: string | null;
    expires_at?: string | null;
  }) => unwrap<ComplianceWord>(http.post("/compliance/words", payload)),
  updateWord: (id: string, payload: Record<string, unknown>) =>
    unwrap<ComplianceWord>(http.patch(`/compliance/words/${id}`, payload)),
  deleteWord: (id: string) => unwrap<{ id: string; deleted: boolean }>(http.delete(`/compliance/words/${id}`)),
  rules: (params?: ListParams & { severity?: string }) =>
    unwrapPage<ComplianceRule>(
      http.get("/compliance/rules", {
        params: { ...(params?.severity ? { severity: params.severity } : {}), offset: params?.offset, limit: params?.limit },
      }),
    ),
  createRule: (payload: { pattern: string; severity: string; suggestion?: string | null }) =>
    unwrap<ComplianceRule>(http.post("/compliance/rules", payload)),
  updateRule: (id: string, payload: Record<string, unknown>) =>
    unwrap<ComplianceRule>(http.patch(`/compliance/rules/${id}`, payload)),
  deleteRule: (id: string) => unwrap<{ id: string; deleted: boolean }>(http.delete(`/compliance/rules/${id}`)),
  /** 下一次生成会下发的规则快照（排查「配了却没拦」）。 */
  snapshot: () => unwrap<ComplianceSnapshotResponse>(http.get("/compliance/snapshot")),
  /** 命中预览（用当前快照跑，与入队口径一致）。 */
  preview: (text: string) => unwrap<CompliancePreviewResult>(http.post("/compliance/preview", { text })),
};

/** 运维面域（**只读**：`overview`/`dlq` 不改任何状态；唯一写操作是带审计的「终止卡住任务」）。 */
export const opsApi = {
  overview: () => unwrap<OpsOverview>(http.get("/ops/overview")),
  dlq: (domain: string, limit = 20) => unwrap<DlqEntries>(http.get("/ops/dlq", { params: { domain, limit } })),
  /**
   * 终止卡死的生成任务（admin 独占；**必填原因**，写 job_abort_audits 审计）。
   *
   * 用途：解开「商品永久 409 / 详情页按钮点不动」的死锁（过去只能人肉改库）。
   */
  abortJob: (jobId: string, reason: string) =>
    unwrap<AbortJobResult>(http.post(`/ops/jobs/${jobId}/abort`, { reason })),
};

