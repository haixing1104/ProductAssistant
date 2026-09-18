// 审批中心用例（W3/W4/W6）：列表视觉层级、补投语义与按钮条件、带命中点的放行必须写理由。
//
// 为什么要有这组用例（2026-09 实测）:
//   ① 列表把「高价商品（> ¥500），需人工放行」整串塞进有色 Tag，把「评估分」压得看不见；
//   ② 补投按钮对**已闭环**的单也露出，点了却"什么都没发生"（对已跑完的线程 resume 是静默 no-op）
//      —— 现在按钮只在"商品仍停在待审批"时出现，结果按 needed/outcome 说真话；
//   ③ 合规命中转人工后，审批人一点「批准」即可上架且不留痕 —— 现在必须写理由（写审计）。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "../api";
import ApprovalsPage from "../pages/ApprovalsPage";
import { useAuthStore } from "../store/authStore";
import type { Approval } from "../types/api";

vi.mock("../api", () => ({
  approvalsApi: { list: vi.fn(), get: vi.fn(), approve: vi.fn(), reject: vi.fn(), redrive: vi.fn(), deeplink: vi.fn() },
}));

vi.mock("react-router-dom", () => ({
  useParams: () => ({}),
  useNavigate: () => vi.fn(),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
  Link: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

/** 本文件只桩 `approvalsApi`：其它域不应被这条用例触达（触达即说明页面越权取数）。 */
const mockApi = vi.mocked(approvalsApi);

/** 造一张最小可信的审批单（默认 pending + 高价值转人工，覆盖最常见的展示分支）。 */
function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "a-1",
    org_id: "o-1",
    product_id: "p-1",
    thread_id: "t-1",
    status: "pending",
    channel: "web",
    snapshot_summary: { reason: "high_value", score: 95, violation_count: 0, evaluation_attempts: 1 },
    product_status: "waiting_approval",
    sku_code: "SKU-1",
    product_title: "测试商品",
    ...overrides,
  };
}

/** 挂载审批中心（每个用例独立 QueryClient：`retry:false` 让失败立刻暴露，不等重试）。 */
function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApprovalsPage />
    </QueryClientProvider>,
  );
}

describe("ApprovalsPage（视觉层级 / 补投 / 放行留痕）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ token: "t", user: { id: "u-1", role: "admin" } });
  });

  it("转人工原因用短标签（长文案进 Tooltip），评估分单独成列不再被压", async () => {
    mockApi.list.mockResolvedValue({ items: [approval()], total: 1 } as never);
    renderPage();

    // 短标签在列表里；长文案不再直接铺在列里（只在 Tooltip 内，悬浮才出现）
    expect(await screen.findByText("高价商品", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText(/需人工放行/)).not.toBeInTheDocument();
    // 评估分渲染为独立 Tag
    expect(screen.getByText("95")).toBeInTheDocument();
  });

  it("补投按钮只在「商品仍停在待审批」的行出现（已闭环的单不露）", async () => {
    mockApi.list.mockResolvedValue({
      items: [
        approval({ id: "a-approved", status: "approved", product_status: "published", sku_code: "SKU-DONE" }),
        approval({ id: "a-stuck", status: "approved", product_status: "waiting_approval", sku_code: "SKU-STUCK" }),
      ],
      total: 2,
    } as never);
    renderPage();

    await screen.findByText("SKU-STUCK", {}, { timeout: 5000 });
    // antd 会给两字按钮插空格（「补 投」），用正则匹配
    expect(screen.getAllByRole("button", { name: /补\s*投/ })).toHaveLength(1);
  });

  it("补投要二次确认；not_needed 时如实提示「无需补投」", async () => {
    const user = userEvent.setup();
    mockApi.list.mockResolvedValue({
      items: [approval({ id: "a-stuck", status: "rejected", product_status: "waiting_approval" })],
      total: 1,
    } as never);
    mockApi.redrive.mockResolvedValue({
      approval_id: "a-stuck",
      needed: false,
      outcome: "not_needed",
      resume_enqueued: false,
    } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: /补\s*投/ }, { timeout: 5000 }));
    // 二次确认弹窗（运维动作）
    const confirm = await screen.findByRole("button", { name: "确认补投", hidden: false }, { timeout: 5000 });
    await user.click(confirm);

    await waitFor(() => expect(mockApi.redrive).toHaveBeenCalledWith("a-stuck"));
    expect(await screen.findByText(/无需补投/, {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it("带评估命中点的单：放行必须写理由（未填时确认按钮禁用）", async () => {
    const user = userEvent.setup();
    mockApi.list.mockResolvedValue({ items: [approval()], total: 1 } as never);
    mockApi.get.mockResolvedValue(
      approval({
        content_snapshot: {
          reason: "quality_exhausted",
          content: { blocks: [{ type: "text", text: "全网最便宜的水杯" }] },
          evaluation_result: {
            passed: false,
            score: 60,
            violations: [{ keyword: "最便宜", reason: "命中违禁词「最便宜」（广告法种子）", severity: "high" }],
          },
          evaluation_attempts: 3,
        },
      }) as never,
    );
    renderPage();

    await user.click(await screen.findByRole("button", { name: /批\s*准/ }, { timeout: 5000 }));

    // 抽屉里能看到命中点
    expect(await screen.findByText(/评估命中点（1 条）/, {}, { timeout: 5000 })).toBeInTheDocument();
    const confirm = await screen.findByRole("button", { name: "确认批准并继续写入" }, { timeout: 5000 });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/放行理由（必填）/), "已核对：标题将同步整改");
    await waitFor(() => expect(confirm).not.toBeDisabled());
  });
});
