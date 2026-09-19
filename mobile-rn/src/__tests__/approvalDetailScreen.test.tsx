// 审批详情（RN）语义用例 —— 与 H5 的同名用例逐条对齐，守护两条与"人"直接相关的口径。
//
// ① **深链票据与单据不匹配时必须当场失败**（连详情都不取）：
//    票据只做定位、不授予审批权限；若忽略不匹配，用户点开 A 单的通知却看到 B 单内容，
//    会做出错误判断（而 backend 侧 approve/reject 只校验权限，不会发现这层错位）。
// ② **带评估命中点放行必须写理由**（写 approval_overrides 审计），与后端 422 同口径。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import axios from "axios";
import { SafeAreaProvider } from "react-native-safe-area-context";

import ApprovalDetailScreen from "../screens/ApprovalDetailScreen";
import { approvalsApi } from "@pa/core/api";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

jest.mock("@pa/core/api", () => ({
  approvalsApi: { list: jest.fn(), get: jest.fn(), approve: jest.fn(), reject: jest.fn(), redrive: jest.fn(), deeplink: jest.fn() },
}));
jest.mock("../platform/pickFile", () => ({ pickCsvFile: jest.fn(), pickImageFile: jest.fn() }));

/** 只桩审批域（含 deeplink：票据校验走的就是它）。 */
const mockApi = approvalsApi as unknown as {
  get: jest.Mock;
  approve: jest.Mock;
  reject: jest.Mock;
  deeplink: jest.Mock;
};

/** SafeAreaProvider 的初始度量（jsdom 无原生安全区：给 0 让布局与真机解耦）。 */
const insets = { top: 0, left: 0, right: 0, bottom: 0 };
/** iPhone 14 逻辑分辨率（给安全区 provider 一个稳定的测量环境）。 */
const frame = { x: 0, y: 0, width: 390, height: 844 };

/** 造一张带完整快照与命中点的审批单（详情页要渲染评估分/命中点/图文快照）。 */
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
    snapshot_summary: { reason: "high_value", score: 88 },
    notifications: [],
    content_snapshot: {
      reason: "high_value",
      content: { blocks: [{ type: "text", text: "待审文案" }] },
      evaluation_result: { passed: false, score: 78, violations: [{ keyword: "国家级", reason: "极限词", severity: "high" }] },
    },
    ...overrides,
  };
}

/** 挂载审批详情（⚠️ RNTL v14 的 render 返回 Promise，必须 await）。 */
async function renderScreen(props: { approvalId: string; ticket?: string | null }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <SafeAreaProvider initialMetrics={{ frame, insets }}>
      <QueryClientProvider client={qc}>
        <ApprovalDetailScreen approvalId={props.approvalId} ticket={props.ticket ?? null} onBack={jest.fn()} />
      </QueryClientProvider>
    </SafeAreaProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  useAuthStore.setState({ token: "t", user: { username: "u", role: "reviewer" } });
});

describe("审批详情（RN）", () => {
  it("深链票据与审批单不匹配：**不取详情**并给出可读原因", async () => {
    mockApi.deeplink.mockResolvedValue({ valid: true, approval_id: "another-id", product_id: "p-9", org_id: "o-1" });
    const view = await renderScreen({ approvalId: "a-1", ticket: "ticket-1" });

    await waitFor(() => expect(view.getByTestId("pa-query-error")).toBeTruthy());
    expect(mockApi.get).not.toHaveBeenCalled(); // 关键：连详情都不取
    expect(view.getByText(/深链票据与审批单不匹配/)).toBeTruthy();
  });

  it("票据匹配时才继续取详情", async () => {
    mockApi.deeplink.mockResolvedValue({ valid: true, approval_id: "a-1", product_id: "p-1", org_id: "o-1" });
    mockApi.get.mockResolvedValue(approval());
    const view = await renderScreen({ approvalId: "a-1", ticket: "ticket-1" });

    await waitFor(() => expect(mockApi.get).toHaveBeenCalledWith("a-1"));
    // 用 findByText（等待渲染落地）而不是 getByText：查询已发出 ≠ 界面已刷新，
    // 直接断言会偶发失败（实测抖动过一次）—— 这类"时序断言"必须等渲染。
    expect(await view.findByText("SKU-1")).toBeTruthy();
  });

  it("带命中点放行：必须先填理由才能提交，且请求体带出理由", async () => {
    mockApi.get.mockResolvedValue(approval());
    mockApi.approve.mockResolvedValue({ approval_id: "a-1", status: "approved", resume_enqueued: true });
    const view = await renderScreen({ approvalId: "a-1" });

    // 底部操作栏 → 批准 → 弹层
    await fireEvent.press(await view.findByTestId("pa-approve"));
    const submit = await view.findByTestId("pa-decide-submit");
    // 有命中点 → 未填理由时禁用（按钮 disabled 时不触发 onPress）
    await fireEvent.press(submit);
    expect(mockApi.approve).not.toHaveBeenCalled();

    await fireEvent.changeText(view.getByTestId("pa-feedback"), " 已人工确认文案，命中点为描述性用语 ");
    await fireEvent.press(view.getByTestId("pa-decide-submit"));
    await waitFor(() => expect(mockApi.approve).toHaveBeenCalledWith("a-1", "已人工确认文案，命中点为描述性用语"));
  });

  it("驳回：意见必填（空意见等于让 AI 瞎猜），填了才发请求", async () => {
    mockApi.get.mockResolvedValue(approval());
    mockApi.reject.mockResolvedValue({ approval_id: "a-1", status: "rejected", resume_enqueued: true });
    const view = await renderScreen({ approvalId: "a-1" });

    await fireEvent.press(await view.findByTestId("pa-reject"));
    await fireEvent.press(await view.findByTestId("pa-decide-submit"));
    expect(mockApi.reject).not.toHaveBeenCalled();
    // 文案必须说清"为什么禁用"（而不是让人对着灰按钮猜）
    expect(view.getByText("驳回必须填写意见")).toBeTruthy();

    await fireEvent.changeText(view.getByTestId("pa-feedback"), "标题过长且含极限词，请重写");
    await fireEvent.press(view.getByTestId("pa-decide-submit"));
    await waitFor(() => expect(mockApi.reject).toHaveBeenCalledWith("a-1", "标题过长且含极限词，请重写"));
  });

  it("「根本没到后端」的失败自动重试一次（2026-09 真机：隧道边缘 503，用户被迫手动再点）", async () => {
    mockApi.get.mockResolvedValue(approval());
    // 第一次：网络层失败（无响应 = 请求没到后端）；第二次：成功
    const networkError = new axios.AxiosError("Network Error");
    mockApi.approve
      .mockRejectedValueOnce(networkError)
      .mockResolvedValueOnce({ approval_id: "a-1", status: "approved", resume_enqueued: true });

    const view = await renderScreen({ approvalId: "a-1" });
    await fireEvent.press(await view.findByTestId("pa-approve"));
    await fireEvent.changeText(await view.findByTestId("pa-feedback"), "已人工确认，命中点为描述性用语");
    await fireEvent.press(view.getByTestId("pa-decide-submit"));

    // 定案是 CAS（重复只会 409，不会重复上架）→ 重试一次是安全的；这里固化的就是"会自动重试"
    await waitFor(() => expect(mockApi.approve).toHaveBeenCalledTimes(2), { timeout: 4000 });
    expect(mockApi.approve).toHaveBeenLastCalledWith("a-1", "已人工确认，命中点为描述性用语");
  });

  it("后端已给出结论的失败（4xx 信封）**不重试**：重试只会把 409/403 再撞一遍", async () => {
    mockApi.get.mockResolvedValue(approval());
    const conflict = new axios.AxiosError("Conflict");
    // @ts-expect-error 测试替身：只填被测代码会读到的字段（PA 信封的 409）
    conflict.response = { status: 409, data: { code: 409, data: null, message: "该审批单已被他人定案" } };
    mockApi.approve.mockRejectedValue(conflict);

    const view = await renderScreen({ approvalId: "a-1" });
    await fireEvent.press(await view.findByTestId("pa-approve"));
    await fireEvent.changeText(await view.findByTestId("pa-feedback"), "已人工确认，命中点为描述性用语");
    await fireEvent.press(view.getByTestId("pa-decide-submit"));

    await waitFor(() => expect(mockApi.approve).toHaveBeenCalledTimes(1));
    // 等过重试窗口（retryDelay 800ms）后仍是 1 次：4xx 是"后端已给出结论"，重试没有意义
    await new Promise((resolve) => setTimeout(resolve, 1000));
    expect(mockApi.approve).toHaveBeenCalledTimes(1);
  });
});
