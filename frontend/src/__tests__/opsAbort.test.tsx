// 运维面板「终止卡住任务」用例（P8）：读面只读，唯一的变更入口必须**必填原因**并写审计。
//
// 为什么要单测这条路径:
//   ① 终止是不可逆的运维动作 —— 按钮必须落在「有原因」的前提下（审计的价值全在这条原因上）；
//   ② 面板最容易出的错是「看见卡住任务却点不动/点错」，所以断言：按钮出现 → 弹窗 → 无原因时禁用 →
//      填了才发出请求，且请求体带 job_id + reason。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { opsApi } from "../api";
import OpsPage, { stuckAgeLabel } from "../pages/OpsPage";

vi.mock("../api", () => ({
  opsApi: { overview: vi.fn(), dlq: vi.fn(), abortJob: vi.fn() },
}));

/** 只桩运维面：本文件守护的是「终止卡住任务」的交互与请求体，不关心其它域。 */
const mockOps = vi.mocked(opsApi);

/** 运维总览样本：1 个健康心跳 + 1 个已卡 30 分钟的任务（列表与终止入口的最小前提）。 */
const OVERVIEW = {
  env: "test",
  generated_at: "2026-09-17T12:00:00+00:00",
  workers: [{ consumer: "w-1", key: "pa:test:worker:heartbeat:w-1", ttl_seconds: 25, alive: true }],
  stalled: false,
  streams: [],
  dlq: [],
  stuck_jobs: [
    {
      job_id: "job-1",
      thread_id: "abcdef12-3456-4789-8abc-def012345678",
      product_id: "p-1",
      sku_code: "SKU-STUCK",
      product_status: "generating",
      job_status: "running",
      age_seconds: 1800,
    },
  ],
};

/** 挂载运维面板（独立 QueryClient + 关闭重试：请求体断言依赖「只发一次」）。 */
function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OpsPage />
    </QueryClientProvider>,
  );
}

describe("OpsPage 终止卡住任务", () => {
  beforeEach(() => {
    mockOps.overview.mockClear();
    mockOps.abortJob.mockClear();
    mockOps.overview.mockResolvedValue(OVERVIEW as never);
    mockOps.abortJob.mockResolvedValue({
      job_id: "job-1",
      thread_id: "abcdef12-3456-4789-8abc-def012345678",
      product_id: "p-1",
      job_status: "failed",
      product_status: "draft",
      product_released: true,
      already_terminal: false,
    } as never);
  });

  it("列出卡住任务：SKU / 停滞时长可读", async () => {
    renderPage();
    expect(await screen.findByText("SKU-STUCK")).toBeInTheDocument();
    expect(screen.getByText("30min")).toBeInTheDocument(); // 1800s → 30min（stuckAgeLabel）
  });

  it("终止必须填原因：没填时确定按钮禁用，填了才发出带 job_id + reason 的请求", async () => {
    const user = userEvent.setup();
    renderPage();
    // 先等数据渲染出来（jsdom + antd 首屏较慢，显式给足超时，避免 1s 默认超时误判）
    await screen.findByText("SKU-STUCK", {}, { timeout: 5000 });
    // 注意：antd 会给「恰好两个中文字」的按钮插入空格（DOM 里是「终 止」，与「刷 新」同理），
    // 所以这里用正则匹配，不能用精确名。
    await user.click(
      await screen.findByRole("button", { name: /终\s*止/ }, { timeout: 5000 }),
    );
    const okButton = await screen.findByRole("button", { name: "终止并写审计" }, { timeout: 5000 });
    expect(okButton).toBeDisabled(); // 原因必填（审计要求）
    expect(mockOps.abortJob).not.toHaveBeenCalled();

    await user.type(
      screen.getByPlaceholderText(/为什么判定它已死|worker 已被 kill/),
      "worker 已崩溃",
    );
    await waitFor(() => expect(okButton).not.toBeDisabled());
    await user.click(okButton);

    await waitFor(() => expect(mockOps.abortJob).toHaveBeenCalledWith("job-1", "worker 已崩溃"));
    // 成功后刷新面板（终止后该任务应从「卡住」清单消失）
    await waitFor(() => expect(mockOps.overview.mock.calls.length).toBeGreaterThanOrEqual(2));
  }, 20000);
});

describe("stuckAgeLabel", () => {
  it("秒 / 分 / 小时三档，缺值显示 -", () => {
    expect(stuckAgeLabel(45)).toBe("45s");
    expect(stuckAgeLabel(1800)).toBe("30min");
    expect(stuckAgeLabel(7200)).toBe("2.0h");
    expect(stuckAgeLabel(null)).toBe("-");
    expect(stuckAgeLabel(undefined)).toBe("-");
  });
});
