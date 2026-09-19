import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// 每个用例结束后卸载组件：避免残留的定时器/订阅让 vitest run 收不了尾
// （本沙箱 Node 24 + vitest 5 确实有「用例全绿但进程不退出」的现象，卸载是必要的卫生习惯）。
afterEach(() => {
  cleanup();
});
