import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// 每个用例结束后卸载组件：触发 StreamingDisplay 等组件的 effect cleanup（abort fetch / 停重连循环），
// 避免残留的定时器与流句柄让 vitest run 无法退出。
// ⚠️ 必须是 **async + 让出一次事件循环**（2026-09-20 实测修复）：
//   React 19 的 scheduler 用 setImmediate 排队（performWorkUntilDeadline）。cleanup() 只保证
//   卸载，**不保证**已入队的即时任务已经跑完 —— 在单核/慢 runner 上它们会在 jsdom 环境被拆掉
//   **之后**才执行，于是抛 `ReferenceError: window is not defined`，vitest 记 1 个 unhandled
//   error 并 **exit 1**（用例本身 92/92 全过，CI 只显示 `Process completed with exit code 1`，
//   没有细节 annotation，极难定位；本机 8 核不出现，`taskset -c 0 npx vitest run` 必现）。
//   让出一轮事件循环 = 给这些任务一个在环境内的执行机会，之后才允许销毁环境。
afterEach(async () => {
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

// antd 组件依赖 window.matchMedia（jsdom 未实现）
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

// antd 的 Modal / TextArea 走 @rc-component/resize-observer，jsdom 未实现 ResizeObserver：
// 不补会直接抛 `ReferenceError: ResizeObserver is not defined`（弹窗一渲染就炸）。
if (typeof window !== "undefined" && !("ResizeObserver" in window)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(window, "ResizeObserver", {
    writable: true,
    value: ResizeObserverStub,
  });
}
