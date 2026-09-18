// 审批详情（移动端）语义用例 —— 手机端最关键的页面，守护三件事。
//
// 为什么必须有（2026-09 的两次真实事故/反馈）:
//   ① 深链是**通知点进来**的路径：票据只做定位、不授予权限。若"票据与单据不匹配"却照常渲染，
//      审核员会以为自己点的是 A 单、实际批的是 B 单；
//   ② 驳回意见是 AI 重写的输入，空意见 = 让 AI 瞎猜（桌面端已强制，移动端不能因为"手机上打字麻烦"就放松）；
//   ③ 带评估命中点放行必须写理由（写 approval_overrides 审计）—— 手机上误触成本更高，更要拦。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "@pa/core/api";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

import ApprovalDetailPage from "../pages/ApprovalDetailPage";
import { typeWhenReady } from "../test/helpers";

/** 可变的 searchParams 替身：用例通过它注入/清除深链票据（`?ticket=…`）。 */
const searchParams = new URLSearchParams();

vi.mock("@pa/core/api", () => ({
  approvalsApi: { deeplink: vi.fn(), get: vi.fn(), approve: vi.fn(), reject: vi.fn(), redrive: vi.fn() },
}));

vi.mock("react-router-dom", () => ({
  useParams: () => ({ approvalId: "a-1" }),
  useSearchParams: () => [searchParams, vi.fn()],
  useNavigate: () => vi.fn(),
}));

/** 只桩审批域（含 deeplink：票据校验走的就是它）。 */
const mockApi = vi.mocked(approvalsApi);

/** 造一张带完整快照的审批单（详情页要渲染评估分/命中点/图文快照）。 */
function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "a-1",
    org_id: "o-1",
    product_id: "p-1",
    thread_id: "t-1",
    status: "pending",
    channel: "web",
    sku_code: "SKU-1",
    product_title: "测试商品",
    product_status: "waiting_approval",
    snapshot_summary: { reason: "high_value", score: 95, violation_count: 0, evaluation_attempts: 1 },
    content_snapshot: {
      reason: "high_value",
      evaluation_result: { score: 95, passed: true, violations: [] },
      evaluation_attempts: 1,
      content: { blocks: [{ type: "text", text: "正文内容" }] },
    },
    notifications: [],
    ...overrides,
  };
}

/** 带评估命中点的单：放行必须写理由。 */
function withViolations(): Approval {
  return approval({
    snapshot_summary: { reason: "quality_exhausted", score: 72, violation_count: 1, evaluation_attempts: 3 },
    content_snapshot: {
      reason: "quality_exhausted",
      evaluation_result: {
        score: 72,
        passed: false,
        violations: [{ keyword: "最便宜", reason: "广告法极限词", severity: "high" }],
      },
      evaluation_attempts: 3,
      content: { blocks: [{ type: "text", text: "全网最便宜" }] },
    },
  });
}

/** 挂载审批详情（独立 QueryClient + 关重试）。 */
function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApprovalDetailPage />
    </QueryClientProvider>,
  );
}

/**
 * 等底部弹层可交互后再输入。
 *
 * 为什么需要: antd-mobile 的 Popup 用 react-spring 做入场动画，动画期间会在内容容器上设
 * `pointer-events: none`（`popup.js` 里 `percent.to(v => v === 0 ? 'unset' : 'none')`）。
 * 在 jsdom 里这个 spring 需要若干帧才收敛，直接 `user.type` 会报
 * "Unable to perform pointer interaction as the element has pointer-events: none"。
 * 这里等它真的可交互 —— 与真人在手机上"等弹层滑出来再打字"是同一件事（实现见 src/test/helpers.ts）。
 */
const typeInto = typeWhenReady;


beforeEach(() => {
  searchParams.delete("ticket");
  vi.clearAllMocks();
  useAuthStore.setState({
    token: "t",
    user: { id: "u-1", org_id: "o-1", username: "reviewer", role: "reviewer" },
  });
  mockApi.get.mockResolvedValue(approval());
});

describe("深链票据校验（只定位，不授权）", () => {
  it("票据与审批单不匹配 → 不渲染该单据，并给出可读原因", async () => {
    searchParams.set("ticket", "t-other");
    mockApi.deeplink.mockResolvedValue({ valid: true, approval_id: "a-OTHER", product_id: "p-9", org_id: "o-1" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/审批详情加载失败/)).toBeInTheDocument();
    });
    expect(screen.getByText(/深链票据与审批单不匹配/)).toBeInTheDocument();
    // 关键：连详情都不去取（避免"照着 B 单渲染，却以为是点进来的 A 单"）
    expect(mockApi.get).not.toHaveBeenCalled();
  });

  it("票据匹配 → 正常渲染单据内容", async () => {
    searchParams.set("ticket", "t-ok");
    mockApi.deeplink.mockResolvedValue({ valid: true, approval_id: "a-1", product_id: "p-1", org_id: "o-1" });

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("测试商品")).toBeInTheDocument();
    });
    expect(mockApi.deeplink).toHaveBeenCalledWith("t-ok");
    expect(mockApi.get).toHaveBeenCalledWith("a-1");
  });
});

describe("审批动作必填校验（与后端同口径）", () => {
  it("带命中点放行：按钮先禁用，填理由后才可提交，并随请求带出理由", async () => {
    const user = userEvent.setup();
    mockApi.get.mockResolvedValue(withViolations());
    mockApi.approve.mockResolvedValue({ approval_id: "a-1", status: "approved", resume_enqueued: true });

    renderPage();
    await waitFor(() => expect(screen.getByText(/评估命中点（1 条）/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "批准" }));
    const confirm = screen.getByRole("button", { name: "确认批准并继续写入" });
    expect(confirm).toBeDisabled();
    // 未填理由时应给出为什么不能提交
    expect(screen.getByText(/放行必须填写理由/)).toBeInTheDocument();

    await typeInto(screen.getByPlaceholderText(/放行理由（必填）/), "已人工核对，价格表述已与法务确认");
    await waitFor(() => expect(confirm).not.toBeDisabled());
    await user.click(confirm);

    await waitFor(() => {
      expect(mockApi.approve).toHaveBeenCalledWith("a-1", "已人工核对，价格表述已与法务确认");
    });
  });

  it("驳回：意见必填（空意见等于让 AI 瞎猜）", async () => {
    const user = userEvent.setup();
    mockApi.reject.mockResolvedValue({ approval_id: "a-1", status: "rejected", resume_enqueued: true });

    renderPage();
    await waitFor(() => expect(screen.getByText("测试商品")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "驳回" }));
    const confirm = screen.getByRole("button", { name: "确认驳回并让 AI 重写" });
    expect(confirm).toBeDisabled();

    await typeInto(screen.getByPlaceholderText(/驳回意见（必填）/), "配图不够科技感，请重出");
    await waitFor(() => expect(confirm).not.toBeDisabled());
    await user.click(confirm);

    await waitFor(() => {
      expect(mockApi.reject).toHaveBeenCalledWith("a-1", "配图不够科技感，请重出");
    });
  });

  it("已定案的单：不给审批按钮，只提示无需处理", async () => {
    mockApi.get.mockResolvedValue(approval({ status: "approved", resolved_at: "2026-09-17T10:00:00" }));
    renderPage();

    // 文案**逐字**断言（不用 /无需再处理/ 这种宽正则）：句子与标签都会提供「已」，
    // 一旦有人再写成「该审批单已{label}」，就会渲染出「已已批准」——正则拦不住这种回潮。
    await waitFor(() =>
      expect(screen.getByText("该审批单已批准，无需再处理")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "批准" })).toBeNull();
    expect(screen.queryByRole("button", { name: "驳回" })).toBeNull();
  });
});
