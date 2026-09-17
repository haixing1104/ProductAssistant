import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import { installAntdMobileReact19Shim } from "../services/antdMobileReact19";

// 与线上一致：main.tsx 会装这层 React 19 兼容层。测试环境同样要装 ——
// 否则 Toast/Dialog 走 antd-mobile 的默认渲染路径（依赖 React 18 的 ReactDOM.render），
// 在 React 19 下会静默不渲染，并在卸载时抛 `unmountComponentAtNode is not a function`
//（unhandled rejection，测试文件本身仍"通过"，噪音掩盖真实问题）。
installAntdMobileReact19Shim();

// 每个用例结束后卸载：触发 effect cleanup（abort 流 / 停重连循环），
// 避免残留定时器与流句柄让 vitest run 无法退出（与桌面端同因同解）。
afterEach(() => {
  cleanup();
});

// antd-mobile 的部分组件依赖 matchMedia（jsdom 未实现）：不补会直接抛 ReferenceError
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

// antd-mobile 的 Popup / ImageViewer 走 rc-util，jsdom 无 ResizeObserver（同桌面端）
if (typeof window !== "undefined" && !("ResizeObserver" in window)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(window, "ResizeObserver", { writable: true, value: ResizeObserverStub });
}

// ImageViewer / 图片懒加载路径会读 IntersectionObserver
if (typeof window !== "undefined" && !("IntersectionObserver" in window)) {
  class IntersectionObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): [] {
      return [];
    }
  }
  Object.defineProperty(window, "IntersectionObserver", {
    writable: true,
    value: IntersectionObserverStub,
  });
}

// 真机调试用的滚动 API（jsdom 未实现，Element.scrollTo 在部分组件里会被调用）
if (typeof window !== "undefined" && !window.scrollTo) {
  Object.defineProperty(window, "scrollTo", { writable: true, value: () => undefined });
}
