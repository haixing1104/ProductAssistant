// 审批列表（RN）语义用例 —— 守护三条与桌面端不同的移动端口径（与 H5 的用例逐条对齐）。
//
// 为什么这三条必须钉住:
//   ① **列表不放「批准」按钮**：手机上误触代价是"错误内容直接上架"。动作一律进详情页
//     （那里有底部固定操作栏 + 必填理由校验），列表只负责"看"和"去处理"；
//   ② 已定案但**商品仍停在待审批**时必须给红字提示：这正是「引擎没收到结论」的现场信号；
//   ③ 卡片上必须保留**评估分 + 转人工原因短标签**这两个扫视锚点（长原因文案会挤掉评估分）。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import ApprovalsScreen from "../screens/ApprovalsScreen";
import { approvalsApi } from "@pa/core/api";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

jest.mock("@pa/core/api", () => ({
  approvalsApi: { list: jest.fn(), get: jest.fn(), approve: jest.fn(), reject: jest.fn(), redrive: jest.fn(), deeplink: jest.fn() },
}));

jest.mock("../platform/pickFile", () => ({ pickCsvFile: jest.fn(), pickImageFile: jest.fn() }));

/** 只桩审批域的列表接口（本文件守护的是卡片列表口径）。 */
const mockApi = approvalsApi as unknown as { list: jest.Mock };

/** 造一张最小可信的审批单（默认 pending + 高价值转人工）。 */
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

/** SafeAreaProvider 的初始度量（jsdom 无原生安全区）。 */
const insets = { top: 0, left: 0, right: 0, bottom: 0 };
/** iPhone 14 逻辑分辨率。 */
const frame = { x: 0, y: 0, width: 390, height: 844 };

/**
 * ⚠️ RNTL v14 的 `render` **返回 Promise**（React 19 并发渲染）—— 必须 `await`。
 * 不 await 的报错是 `render function has not been called`（来自 `screen` 代理），
 * 极易被误判成"用例写错了"，实为"渲染还没落地"。
 */
async function renderPage(rows: Approval[]) {
  mockApi.list.mockResolvedValue({ items: rows, total: rows.length });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <SafeAreaProvider initialMetrics={{ frame, insets }}>
      <QueryClientProvider client={qc}>
        <ApprovalsScreen onOpenDetail={jest.fn()} />
      </QueryClientProvider>
    </SafeAreaProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  useAuthStore.setState({ token: "t", user: { username: "u", role: "reviewer" } });
});

describe("审批列表（RN）", () => {
  it("待处理卡片**没有**批准/驳回按钮（防误触上架），只有「去处理」", async () => {
    await renderPage([approval()]);
    await waitFor(() => expect(screen.getByText("去处理")).toBeTruthy());
    expect(screen.queryByText("批准")).toBeNull();
    expect(screen.queryByText("驳回")).toBeNull();
  });

  it("扫视锚点保留：评估分 + 转人工原因短标签 + SKU", async () => {
    await renderPage([approval()]);
    await waitFor(() => expect(screen.getByText("95")).toBeTruthy());
    expect(screen.getByText("高价商品")).toBeTruthy(); // 短标签（长解释另起一行小字）
    expect(screen.getByText("SKU-1")).toBeTruthy();
  });

  it("「商品仍停在待审批」的红字只出现在已定案却卡住的那一行", async () => {
    await renderPage([
      approval({ id: "a-1", sku_code: "SKU-1", status: "approved", product_status: "waiting_approval" }),
      approval({ id: "a-2", sku_code: "SKU-2", status: "approved", product_status: "published" }),
    ]);
    await waitFor(() => expect(screen.getByTestId("pa-approval-a-1")).toBeTruthy());
    const notices = screen.queryAllByText(/商品仍停在待审批/);
    expect(notices).toHaveLength(1);
    // 卡住的那张卡里才有这段红字
    expect(screen.getByTestId("pa-approval-a-1")).toContainElement(notices[0]);
  });

  it("「去处理」把用户带到详情（列表不直接改状态）", async () => {
    const onOpenDetail = jest.fn();
    mockApi.list.mockResolvedValue({ items: [approval()], total: 1 });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = await render(
      <SafeAreaProvider initialMetrics={{ frame, insets }}>
        <QueryClientProvider client={qc}>
          <ApprovalsScreen onOpenDetail={onOpenDetail} />
        </QueryClientProvider>
      </SafeAreaProvider>,
    );
    const button = await view.findByTestId("pa-approval-open-a-1");
    await fireEvent.press(button);
    expect(onOpenDetail).toHaveBeenCalledWith("a-1");
  });
});
