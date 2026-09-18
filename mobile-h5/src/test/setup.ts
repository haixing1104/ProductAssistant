// 测试环境公共桩（vitest setup）。
//
// 定位与桌面端 `frontend/src/test/setup.ts` 一致：**只补 jsdom 缺的能力**，不改被测语义。
// 两件事必做：① 装 antd-mobile 的 React 19 兼容层（与线上 main.tsx 同款，否则命令式 API 静默不渲染）；
// ② 每个用例后 cleanup（触发 effect cleanup：abort 流、停重连循环，避免 vitest 卡在 teardown）。
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
  /** ResizeObserver 的最小替身：Popup/ImageViewer 只用到这三个方法（空实现即可）。 */
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(window, "ResizeObserver", { writable: true, value: ResizeObserverStub });
}

// ImageViewer / 图片懒加载路径会读 IntersectionObserver
if (typeof window !== "undefined" && !("IntersectionObserver" in window)) {
  /** IntersectionObserver 的最小替身（多一个 `takeRecords`：懒加载路径会读它）。 */
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
