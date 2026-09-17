// 审批列表（移动端）语义用例 —— 守护三条与桌面端不同的移动端口径。
//
// 为什么这三条必须钉住:
//   ① **列表不放「批准」按钮**：手机上误触代价是"错误内容直接上架"。动作一律进详情页（那里有
//      底部固定操作栏 + 必填理由校验），列表只负责"看"和"去处理"；
//   ② 已定案但**商品仍停在待审批**时必须给红字提示：这正是「引擎没收到结论」的现场信号，
//      管理员据此进详情页补投（桌面端把补投按钮放在列表，是因为有鼠标精度）；
//   ③ 卡片上必须保留**评估分 + 转人工原因短标签**这两个扫视锚点（W3 的教训：长原因文案挤掉评估分）。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi } from "@pa/core/api";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

import ApprovalsPage from "../pages/ApprovalsPage";

vi.mock("@pa/core/api", () => ({
  approvalsApi: { list: vi.fn(), get: vi.fn(), approve: vi.fn(), reject: vi.fn(), redrive: vi.fn(), deeplink: vi.fn() },
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useParams: () => ({}),
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

const mockApi = vi.mocked(approvalsApi);

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
    notifications: [],
    ...overrides,
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApprovalsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({
    token: "t",
    user: { id: "u-1", org_id: "o-1", username: "reviewer", role: "reviewer" },
  });
});

describe("列表口径（手机只做『看』与『去处理』）", () => {
  it("待处理卡片不给「批准/驳回」按钮，只给「去处理」（防误触上架）", async () => {
    mockApi.list.mockResolvedValue({ items: [approval()], total: 1 });

    renderPage();

    await waitFor(() => expect(screen.getByText("测试商品")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "批准" })).toBeNull();
    expect(screen.queryByRole("button", { name: "驳回" })).toBeNull();
    expect(screen.getByRole("button", { name: "去处理" })).toBeInTheDocument();
  });

  it("扫视锚点：评估分 + 转人工原因短标签 + 原因全文（长解释不挤掉分数）", async () => {
    mockApi.list.mockResolvedValue({
      items: [approval({ snapshot_summary: { reason: "quality_exhausted", score: 72, evaluation_attempts: 3 } })],
      total: 1,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText("72")).toBeInTheDocument());
    expect(screen.getByText("质量未达标")).toBeInTheDocument(); // 短标签
    expect(screen.getByText("AI 内容多次未达标，转人工")).toBeInTheDocument(); // 全文另起一行
    expect(screen.getByText(/第 3 次评估/)).toBeInTheDocument();
  });

  it("已定案但商品仍停在待审批 → 红字提示可补投；已闭环的单不给这个提示", async () => {
    mockApi.list.mockResolvedValue({
      items: [
        approval({ id: "a-stuck", status: "approved", product_status: "waiting_approval" }),
        approval({ id: "a-done", status: "approved", product_status: "published", sku_code: "SKU-2" }),
      ],
      total: 2,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText(/可能是引擎未收到结论/)).toBeInTheDocument());
    // 只有"真卡住"那一行有提示（已闭环的 published 单不该出现）
    expect(screen.getAllByText(/可能是引擎未收到结论/)).toHaveLength(1);
    // 两张单都已定案 → 按钮都是"查看详情"（待处理那张才是"去处理"）
    expect(screen.getAllByText("查看详情")).toHaveLength(2);
  });

  it("补投次数与通知投递状态在卡片上可见（运维排查的第一手信息）", async () => {
    mockApi.list.mockResolvedValue({
      items: [
        approval({
          redrive: { count: 2, last_at: "2026-09-17T10:00:00", last_outcome: "not_needed" },
          notifications: [{ channel: "dingtalk", status: "dlq", retry_count: 3, error: "webhook 404" }],
        }),
      ],
      total: 1,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText(/已补投 2 次/)).toBeInTheDocument());
    expect(screen.getByText(/钉钉 投递失败/)).toBeInTheDocument();
  });
});
