import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// 每个用例结束后卸载组件：触发 StreamingDisplay 等组件的 effect cleanup（abort fetch / 停重连循环），
// 避免残留的定时器与流句柄让 vitest run 无法退出。
afterEach(() => {
  cleanup();
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
