// 商品详情页回归用例 —— 守护「点生成后按钮永远停在生成中」那次事故的三道防线。
//
// 事故现场（2026-09-17，有后台访问日志为证）:
//   GET /stream-ticket → GET /stream（**在 POST /generate 之前**，此时商品非 generating
//   → 服务端状态门只发一条 ready 并关流）→ POST /generate → 此后**再也没有 /stream 请求**。
//   成因：连接器命中 ready/终态即 stopped 且不会自己复活，而页面只做 setStreamOn(true)
//   （已为 true 时是 no-op）→ 组件不重挂载 → 新任务的 done 事件无人接收 → 按钮永久「生成中…」，
//   而 DB 里其实早就 published 了。
//
// 三道防线（本文件逐条断言）:
//   ① 点生成后**必须重新建立流连接**（key 里带 nonce → 重挂载）—— 修复前这里永远是 1 次连接；
//   ② 终态（done）/ ready 都要触发一次详情重拉（UI 与后端状态纠偏）；
//   ③ 进行中时详情有 5s 兜底轮询（流断了也能复位），终态后立即停止轮询。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { approvalsApi, contentsApi, evaluationLogsApi, productsApi } from "../api";
import ProductDetailPage, { detailRefetchInterval } from "../pages/ProductDetailPage";
import { useAuthStore } from "../store/authStore";
import type { Product } from "../types/api";

// ---------- 替身 ----------
const connectSpy = vi.fn();

vi.mock("../services/sse", async () => {
  const actual = await vi.importActual<typeof import("../services/sse")>("../services/sse");
  return {
    ...actual,
    connectProductStream: (options: unknown) => {
      connectSpy(options);
      return Promise.resolve({ terminals: [], idleReconnects: 0, retries: 0, stopped: false });
    },
  };
});

vi.mock("../api", () => ({
  productsApi: { get: vi.fn(), generate: vi.fn(), update: vi.fn(), purge: vi.fn() },
  contentsApi: { list: vi.fn(), latest: vi.fn() },
  evaluationLogsApi: { list: vi.fn() },
  approvalsApi: { list: vi.fn(), get: vi.fn() },
  ossApi: { presign: vi.fn(), put: vi.fn() },
}));

vi.mock("react-router-dom", () => ({
  useParams: () => ({ productId: "p-1" }),
  Link: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
}));

const mockProducts = vi.mocked(productsApi);
const PRODUCT_ID = "p-1";

function product(overrides: Partial<Product> = {}): Product {
  return {
    id: PRODUCT_ID,
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

/** 当前用例的 QueryClient（afterEach 里 clear，取消兜底轮询定时器）。 */
let queryClient: QueryClient | null = null;

function renderPage() {
  // 注意：页面在「进行中」会开 5s 兜底轮询（detailRefetchInterval）→ 定时器会在用例结束后
  // 继续排程，导致并行跑全量时 worker 无法进入空闲（表现为该文件迟迟不回报）。
  // 因此把 QueryClient 提到外层，在 afterEach 里 clear() 掉（清缓存 + 取消定时器）。
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProductDetailPage />
    </QueryClientProvider>,
  );
}

/** 取最近一次流连接的回调集合（用来模拟服务端推帧 / 关流）。 */
function lastStreamOptions() {
  const call = connectSpy.mock.calls.at(-1);
  expect(call, "应当已经建立过流连接").toBeTruthy();
  return call?.[0] as {
    onFrame: (frame: { type?: string; comment?: string; data?: unknown }) => void;
    onReady?: () => void;
    onTerminal?: (type: string) => void;
  };
}

describe("ProductDetailPage（生成按钮态与流重连）", () => {
  beforeEach(() => {
    connectSpy.mockClear();
    mockProducts.get.mockResolvedValue(product());
    mockProducts.generate.mockResolvedValue({
      thread_id: "thread-1234-abcd",
      status: "running",
      message: "ok",
    });
    vi.mocked(contentsApi.list).mockResolvedValue([]);
    vi.mocked(evaluationLogsApi.list).mockResolvedValue([]);
    vi.mocked(approvalsApi.list).mockResolvedValue({ items: [], total: 0 } as never);
    // 可写角色（否则按钮 disabled，点不动）：与 backend WRITE_ROLES 对齐
    useAuthStore.setState({ token: "t", user: { id: "u-1", role: "operator" } });
  });

  afterEach(() => {
    // 清 QueryClient：取消兜底轮询定时器（否则并行跑全量时 worker 无法进入空闲）
    queryClient?.clear();
    queryClient = null;
    useAuthStore.getState().clear();
  });

  it("流已连过（streamOn 已是 true）时点生成，仍必须重新建立流连接 —— 本次事故的回归点", async () => {
    const user = userEvent.setup();
    renderPage();

    // ① 先点「连接实时流」：复刻现场时序（/stream 请求先于 /generate），
    //    此时商品是 draft → 服务端会回 ready 并关流（连接器 stopped）。
    await user.click(await screen.findByRole("button", { name: "连接实时流" }));
    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(1));

    // ② 点生成：后端 200，详情重拉后状态变 generating
    mockProducts.get.mockResolvedValue(
      product({ status: "generating", active_job_status: "running", active_thread_id: "thread-1" }),
    );
    await user.click(screen.getByRole("button", { name: "开始 / 重新生成" }));

    // ③ **必须发起第二次流连接**：修复前 streamOn 已为 true → setStreamOn(true) 是 no-op
    //    → 组件不重挂载 → 永远只有 1 次连接，done 事件无人接收。
    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "断开实时流" })).toBeInTheDocument();
  });

  it("生成中收到 done 终态 → 详情被重拉（按钮据此复位）", async () => {
    mockProducts.get.mockResolvedValue(
      product({ status: "generating", active_job_status: "running", active_thread_id: "thread-1" }),
    );
    renderPage();

    // 进页面就有活跃任务 → 自动连流（activeJob effect），按钮显示「生成中…」
    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "生成中…" })).toBeDisabled());

    mockProducts.get.mockClear();
    mockProducts.get.mockResolvedValue(product({ status: "published" }));

    const opts = lastStreamOptions();
    act(() => opts.onFrame({ type: "done", data: {} }));

    await waitFor(() => expect(mockProducts.get).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "开始 / 重新生成" })).not.toBeDisabled(),
    );
  });

  it("服务端回 ready（它认为没有进行中任务）也要重拉一次详情做纠偏", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "连接实时流" }));
    await waitFor(() => expect(connectSpy).toHaveBeenCalledTimes(1));

    mockProducts.get.mockClear();
    const opts = lastStreamOptions();
    act(() => opts.onReady?.());

    await waitFor(() => expect(mockProducts.get).toHaveBeenCalled());
  });
});

describe("detailRefetchInterval（流断了也能复位的兜底轮询）", () => {
  it("generating / 有活跃任务 → 5s 轮询", () => {
    expect(detailRefetchInterval(product({ status: "generating" }))).toBe(5000);
    expect(detailRefetchInterval(product({ active_job_status: "waiting_input" }))).toBe(5000);
  });

  it("终态（published/draft/waiting_approval）或还没加载 → 不轮询", () => {
    expect(detailRefetchInterval(product({ status: "published" }))).toBe(false);
    expect(detailRefetchInterval(product({ status: "draft" }))).toBe(false);
    expect(detailRefetchInterval(product({ status: "waiting_approval" }))).toBe(false);
    expect(detailRefetchInterval(undefined)).toBe(false);
  });
});
describe("ProductDetailPage（审批与驳回复盘：必须能看到已定案单）", () => {
  beforeEach(() => {
    connectSpy.mockClear();
    mockProducts.get.mockResolvedValue(product());
    vi.mocked(contentsApi.list).mockResolvedValue([]);
    vi.mocked(evaluationLogsApi.list).mockResolvedValue([]);
    useAuthStore.setState({ token: "t", user: { id: "u-1", role: "operator" } });
  });

  afterEach(() => {
    queryClient?.clear();
    queryClient = null;
    useAuthStore.getState().clear();
  });

  it("复盘查询显式带 status=all（否则已定案的驳回单会被 backend 缺省 pending 过滤掉）", async () => {
    vi.mocked(approvalsApi.list).mockResolvedValue({ items: [], total: 0 } as never);
    renderPage();

    await waitFor(() => expect(vi.mocked(approvalsApi.list)).toHaveBeenCalled());
    expect(vi.mocked(approvalsApi.list).mock.calls.at(-1)?.[0]).toMatchObject({
      productId: PRODUCT_ID,
      status: "all",
      withSnapshot: true,
    });
  });

  it("已驳回的单会渲染出审批意见与被驳回的图文快照", async () => {
    vi.mocked(approvalsApi.list).mockResolvedValue({
      items: [
        {
          id: "a-1",
          org_id: "o-1",
          product_id: PRODUCT_ID,
          thread_id: "t-1",
          status: "rejected",
          channel: "web",
          feedback: "配图不符，请重做",
          resolved_at: "2026-09-17T20:42:18+08:00",
          content_snapshot: {
            reason: "high_value",
            content: {
              blocks: [
                { type: "text", text: "被驳回的文案正文" },
                { type: "image", url: "https://oss.example/img.jpg", alt: "AI 配图", source: "ai_generated" },
              ],
            },
            evaluation_result: { passed: true, score: 95 },
            evaluation_attempts: 1,
          },
        },
      ],
      total: 1,
    } as never);
    renderPage();

    expect(await screen.findByText("被驳回的文案正文")).toBeInTheDocument();
    expect(screen.getByText("审批意见：配图不符，请重做")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "AI 配图" })).toBeInTheDocument();
  });
});

