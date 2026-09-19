// 商品详情（RN）语义用例 —— 与 H5 的同名用例逐条对齐，守护三条**曾经真的出过事故**的口径。
//
// ① `streamNonce` 参与流组件 key：点「生成」时必须**重建 SSE 连接**。
//    连接器一旦 `stopped` 就不会自己复活 —— 只把 streamOn 置 true 是 no-op，症状是按钮永久「生成中…」（2026-09 事故根因）；
// ② 复盘查询**必须带 `status=all`**：backend 缺省是 pending，已定案（驳回/批准）的单查不到，
//    界面恒显示「还没有审批记录」（"驳回后看不到被驳回的图文"正是这么来的）；
// ③ 详情**仅在进行中**开 5s 兜底轮询（SSE 断线时也能复位按钮），终态必须停。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import ProductDetailScreen, { detailRefetchInterval } from "../screens/ProductDetailScreen";
import { approvalsApi, contentsApi, evaluationLogsApi, productsApi } from "@pa/core/api";
import { connectProductStream } from "@pa/core/services/sse";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Product } from "@pa/core/types/api";

jest.mock("@pa/core/api", () => ({
  approvalsApi: { list: jest.fn(), get: jest.fn(), approve: jest.fn(), reject: jest.fn(), redrive: jest.fn(), deeplink: jest.fn() },
  contentsApi: { list: jest.fn(), latest: jest.fn() },
  evaluationLogsApi: { list: jest.fn() },
  ossApi: { presign: jest.fn(), put: jest.fn() },
  productsApi: {
    list: jest.fn(),
    get: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
    purge: jest.fn(),
    generate: jest.fn(),
    importCsv: jest.fn(),
  },
}));
jest.mock("@pa/core/services/sse", () => ({
  connectProductStream: jest.fn(() => Promise.resolve({ terminals: [], idleReconnects: 0, retries: 0, stopped: true })),
  TERMINAL_EVENT_TYPES: new Set(["done", "rejected", "failed", "hitl.waiting"]),
}));
jest.mock("../platform/pickFile", () => ({ pickCsvFile: jest.fn(), pickImageFile: jest.fn() }));

/** 商品域桩（get/generate：按钮态与刷新依赖它们）。 */
const mockProducts = productsApi as unknown as { get: jest.Mock; generate: jest.Mock };
/** 审批域桩（复盘查询必须带 `status=all` —— 本文件的守护之一）。 */
const mockApprovals = approvalsApi as unknown as { list: jest.Mock };
/** 已保存图文桩（驳回后要看得到内容）。 */
const mockContents = contentsApi as unknown as { list: jest.Mock };
/** 思考轨迹桩。 */
const mockTraces = evaluationLogsApi as unknown as { list: jest.Mock };
/** 流连接器桩：只统计「连了几次」（= 组件是否重挂载）。 */
const mockConnect = connectProductStream as unknown as jest.Mock;

/** SafeAreaProvider 的初始度量（jsdom 无原生安全区）。 */
const insets = { top: 0, left: 0, right: 0, bottom: 0 };
/** iPhone 14 逻辑分辨率。 */
const frame = { x: 0, y: 0, width: 390, height: 844 };

/** 造一个最小可信的商品（默认草稿态；用 overrides 指定进行中/失败等分支）。 */
function product(overrides: Partial<Product> = {}): Product {
  return {
    id: "p-1",
    org_id: "o-1",
    sku_code: "SKU-1",
    title: "测试商品",
    base_price: 199,
    stock_status: "in_stock",
    status: "draft",
    raw_images: [],
    ...overrides,
  };
}

/** 挂载商品详情（独立 QueryClient + 关重试；⚠️ RNTL v14 的 render 必须 await）。 */
async function renderScreen() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <SafeAreaProvider initialMetrics={{ frame, insets }}>
      <QueryClientProvider client={qc}>
        <ProductDetailScreen productId="p-1" onBack={jest.fn()} />
      </QueryClientProvider>
    </SafeAreaProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  useAuthStore.setState({ token: "t", user: { username: "u", role: "operator" } });
  mockProducts.get.mockResolvedValue(product());
  mockProducts.generate.mockResolvedValue({ thread_id: "thread-1", status: "generating" });
  mockApprovals.list.mockResolvedValue({ items: [], total: 0 });
  mockContents.list.mockResolvedValue([]);
  mockTraces.list.mockResolvedValue([]);
});

describe("商品详情（RN）", () => {
  it("复盘查询必须带 status=all（否则驳回后看不到被驳回的图文）", async () => {
    await renderScreen();
    await waitFor(() => expect(mockApprovals.list).toHaveBeenCalled());
    expect(mockApprovals.list).toHaveBeenCalledWith(
      expect.objectContaining({ productId: "p-1", status: "all", withSnapshot: true }),
    );
  });

  it("点「开始/重新生成」后**重建流连接**（连接器 stopped 后不会自己复活）", async () => {
    const view = await renderScreen();
    await waitFor(() => expect(view.getByTestId("pa-generate-submit")).toBeTruthy());

    // 先手动接上实时流（= 第一次连接）
    await fireEvent.press(view.getByTestId("pa-detail-more"));
    await fireEvent.press(view.getByText("连接实时流"));
    await waitFor(() => expect(mockConnect).toHaveBeenCalledTimes(1));

    // 再触发一次生成：必须出现第二次连接（key 里带 nonce → 重挂载）
    await fireEvent.press(view.getByTestId("pa-generate-submit"));
    await waitFor(() => expect(mockProducts.generate).toHaveBeenCalledWith("p-1"));
    await waitFor(() => expect(mockConnect).toHaveBeenCalledTimes(2));
  });

  it("详情轮询只在进行中开启 5s，终态必须停（SSE 断线时的兜底）", () => {
    expect(detailRefetchInterval(product({ status: "generating" }))).toBe(5000);
    expect(detailRefetchInterval(product({ status: "draft", active_job_status: "generating" }))).toBe(5000);
    expect(detailRefetchInterval(product({ status: "waiting_approval" }))).toBe(false);
    expect(detailRefetchInterval(product({ status: "draft" }))).toBe(false);
    expect(detailRefetchInterval(undefined)).toBe(false);
  });

  it("任务失败原因与转人工提示必须可见（合规闸门拦截不能只停在日志里）", async () => {
    mockProducts.get.mockResolvedValue(
      product({ status: "waiting_approval", active_job_error: "命中违禁词「国家级」，已阻断" }),
    );
    const view = await renderScreen();
    await waitFor(() => expect(view.getByText(/命中违禁词「国家级」，已阻断/)).toBeTruthy());
    expect(view.getByText(/该商品已转人工审批/)).toBeTruthy();
    // 转人工期间不允许再触发生成（与后端 409 同口径）
    expect(view.getByTestId("pa-generate-submit").props.accessibilityState.disabled).toBe(true);
  });

  it("流式正文区与外层页面都显式打开嵌套滚动，未溢出时不吃手势（2026-09 真机：滑正文却整页滚走）", async () => {
    const view = await renderScreen();
    await waitFor(() => expect(view.getByTestId("pa-generate-submit")).toBeTruthy());

    // 打开实时流（面板挂进详情页的外层 ScrollView 里）
    await fireEvent.press(view.getByTestId("pa-detail-more"));
    await fireEvent.press(view.getByText("连接实时流"));

    const box = await view.findByTestId("pa-stream-box");
    // ① 内层显式打开嵌套滚动：普通 ScrollView 不写就走平台默认，Android 上内层拿不到手势
    expect(box.props.nestedScrollEnabled).toBe(true);
    // ② 测试环境没有真实布局 → onContentSizeChange 不触发 → 视为未溢出 → 关掉滚动（不白吃一次滑动）
    expect(box.props.scrollEnabled).toBe(false);
    // ③ 外层页面同样要显式打开，否则 Android 父层会抢掉内层手势
    expect(view.getByTestId("pa-product-detail").props.nestedScrollEnabled).toBe(true);
  });
});
