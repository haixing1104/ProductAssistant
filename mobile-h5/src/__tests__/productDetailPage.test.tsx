// 商品详情（移动端）语义用例 —— 守护桌面端那次事故的三道防线在本端同样成立。
//
// 事故现场（2026-09-17，桌面端，有后台访问日志为证）:
//   GET /stream-ticket → GET /stream（**在 POST /generate 之前**，商品非 generating → 服务端只发一条
//   ready 就关流）→ POST /generate → 此后**再也没有 /stream 请求**。成因：连接器命中 ready/终态即
//   stopped 且不会自己复活，而页面只做 setStreamOn(true)（已为 true 时是 no-op）→ 组件不重挂载
//   → 新任务的 done 事件无人接收 → 按钮永久「生成中…」，而 DB 里其实早就 published 了。
// 移动端在弱网/切后台场景下更容易撞上（iOS 会冻结 JS），所以这三道防线是**必须**的。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi, contentsApi, evaluationLogsApi, productsApi } from "@pa/core/api";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Product } from "@pa/core/types/api";

import ProductDetailPage, { detailRefetchInterval } from "../pages/ProductDetailPage";

const connectSpy = vi.fn();

vi.mock("@pa/core/services/sse", async () => {
  const actual = await vi.importActual<typeof import("@pa/core/services/sse")>("@pa/core/services/sse");
  return {
    ...actual,
    // 只关心"流被发起了几次"（= 组件是否重挂载），不真的连
    connectProductStream: (options: unknown) => {
      connectSpy(options);
      return Promise.resolve({ terminals: [], idleReconnects: 0, retries: 0, stopped: false });
    },
  };
});

vi.mock("@pa/core/api", () => ({
  productsApi: { get: vi.fn(), generate: vi.fn(), update: vi.fn(), purge: vi.fn() },
  contentsApi: { list: vi.fn() },
  evaluationLogsApi: { list: vi.fn() },
  approvalsApi: { list: vi.fn(), get: vi.fn() },
  ossApi: { presign: vi.fn(), put: vi.fn() },
}));

const navigateSpy = vi.fn();
vi.mock("react-router-dom", () => ({
  useParams: () => ({ productId: "p-1" }),
  useNavigate: () => navigateSpy,
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

const mockProducts = vi.mocked(productsApi);
const mockApprovals = vi.mocked(approvalsApi);

function product(overrides: Partial<Product> = {}): Product {
  return {
    id: "p-1",
    org_id: "o-1",
    sku_code: "SKU-1",
    title: "测试商品",
    base_price: 100,
    stock_status: "in_stock",
    status: "draft",
    active_thread_id: null,
    raw_images: [],
    active_job_status: null,
    active_job_error: null,
    ...overrides,
  };
}

let queryClient: QueryClient | null = null;

function renderPage() {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProductDetailPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({
    token: "t",
    user: { id: "u-1", org_id: "o-1", username: "operator", role: "operator" },
  });
  mockProducts.get.mockResolvedValue(product());
  vi.mocked(contentsApi.list).mockResolvedValue([]);
  vi.mocked(evaluationLogsApi.list).mockResolvedValue([]);
  mockApprovals.list.mockResolvedValue({ items: [], total: 0 });
});

afterEach(() => {
  // 取消"仅进行中"的 5s 兜底轮询定时器，避免污染下个用例 / 进程不退出
  queryClient?.clear();
});

describe("① 兜底轮询口径（仅在『进行中』才 5s 一次）", () => {
  it("generating / 有活跃任务 → 5000ms", () => {
    expect(detailRefetchInterval(product({ status: "generating" }))).toBe(5000);
    expect(detailRefetchInterval(product({ active_job_status: "running" }))).toBe(5000);
  });

  it("终态（draft/published/waiting_approval 且无活跃任务）→ 不轮询", () => {
    expect(detailRefetchInterval(product({ status: "draft" }))).toBe(false);
    expect(detailRefetchInterval(product({ status: "published" }))).toBe(false);
    expect(detailRefetchInterval(product({ status: "waiting_approval" }))).toBe(false);
    expect(detailRefetchInterval(undefined)).toBe(false);
  });
});

describe("② 点生成必须重开流连接（否则按钮永久『生成中…』）", () => {
  it("已有活跃任务时点生成：连接次数 1 → 2（key 里的 nonce 逼出重挂载）", async () => {
    // 场景对齐桌面端事故：商品状态还是 draft（按钮可点），但已有活跃任务 → 流已经在跑；
    // 此时只 setStreamOn(true) 是 no-op，必须靠 nonce 重挂载才会有第二条连接。
    mockProducts.get.mockResolvedValue(product({ status: "draft", active_job_status: "running" }));
    mockProducts.generate.mockResolvedValue({ thread_id: "t-new-1", status: "queued" });

    renderPage();
    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: /开始 \/ 重新生成/ }));

    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(2));
    expect(mockProducts.generate).toHaveBeenCalledWith("p-1");
  });

  it("终态商品点生成也会开流（不依赖 active_job_status 的旧值）", async () => {
    mockProducts.generate.mockResolvedValue({ thread_id: "t-new-2", status: "queued" });

    renderPage();
    const button = await screen.findByRole("button", { name: /开始 \/ 重新生成/ });
    expect(connectSpy).not.toHaveBeenCalled(); // 无活跃任务时不该自己连流

    fireEvent.click(button);

    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(1));
  });
});

describe("③ 审批复盘查询必须带 status=all（否则驳回后看不到被驳回的图文）", () => {
  it("复盘查询显式传 status=all + with_snapshot（backend 缺省是 pending）", async () => {
    renderPage();

    await waitFor(() => expect(mockApprovals.list).toHaveBeenCalled());
    expect(mockApprovals.list).toHaveBeenCalledWith({
      productId: "p-1",
      status: "all",
      withSnapshot: true,
      limit: 20,
    });
  });

  it("驳回单的意见与图文快照要真的渲染出来（不只是发对请求）", async () => {
    mockApprovals.list.mockResolvedValue({
      items: [
        {
          id: "a-9",
          org_id: "o-1",
          product_id: "p-1",
          thread_id: "t-1",
          status: "rejected",
          channel: "web",
          sku_code: "SKU-1",
          product_title: "测试商品",
          product_status: "draft",
          feedback: "配图不够科技感，要体现科技感。",
          snapshot_summary: { reason: "quality_exhausted", score: 72, evaluation_attempts: 3 },
          content_snapshot: {
            reason: "quality_exhausted",
            evaluation_result: {
              score: 72,
              passed: false,
              violations: [{ keyword: "最便宜", reason: "广告法极限词", severity: "high" }],
            },
            content: { blocks: [{ type: "text", text: "全网最便宜的水杯" }] },
          },
          notifications: [],
        },
      ],
      total: 1,
    });

    renderPage();

    await waitFor(() => expect(screen.getByText(/审批意见：配图不够科技感/)).toBeInTheDocument());
    expect(screen.getByText("全网最便宜的水杯")).toBeInTheDocument();
    expect(screen.getByText(/命中 1 点/)).toBeInTheDocument();
    // 有驳回单时给出「按驳回意见重新生成」入口（意见会随新任务注入下一轮生成）
    expect(screen.getByRole("button", { name: "按驳回意见重新生成" })).toBeInTheDocument();
  });
});

