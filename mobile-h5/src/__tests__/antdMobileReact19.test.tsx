// React 19 兼容层冒烟用例 —— 守护「Toast/Dialog 静默不渲染」这个**浏览器里很难察觉**的坑。
//
// 事故性质（2026-09 实测）: antd-mobile v5 的命令式 API（Toast/Dialog）默认依赖 React 18 的
// `ReactDOM.render` / `unmountComponentAtNode`；React 19 已移除它们，且 react-dom 19 入口不再导出
// `createRoot` → 默认实现**静默什么都不渲染**（不报错、不显示），只在某些路径抛
// `unmountComponentAtNode is not a function`（unhandled rejection）。
// 本文件先断言"装了兼容层能渲染"，再由 `main.tsx` 保证线上一定装了。
import { Dialog, Toast } from "antd-mobile";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { installAntdMobileReact19Shim } from "../services/antdMobileReact19";
import { clickWhenReady } from "../test/helpers";

beforeEach(() => {
  installAntdMobileReact19Shim();
});

describe("antd-mobile 命令式 API 在 React 19 下可用", () => {
  it("Toast.show 真的渲染到 DOM（不装兼容层时这里是空的）", async () => {
    Toast.show({ content: "已触发生成（任务 ab12cd34…）" });

    await waitFor(() => {
      expect(screen.getByText("已触发生成（任务 ab12cd34…）")).toBeInTheDocument();
    });
    Toast.clear();
  });

  it("Dialog.alert 能渲染按钮并响应关闭（弹层可用，不只是挂了个空 div）", async () => {
    let confirmed = false;
    Dialog.alert({
      title: "彻底删除商品",
      content: "删除后不可恢复",
      confirmText: "知道了",
      onConfirm: () => {
        confirmed = true;
      },
    });

    await waitFor(() => expect(screen.getByText("彻底删除商品")).toBeInTheDocument());
    await clickWhenReady(screen.getByText("知道了"));
    await waitFor(() => expect(confirmed).toBe(true));
  });

  it("不渲染任何组件的普通 render 不受兼容层影响（回归：shim 不污染正常渲染）", () => {
    render(<div data-testid="plain">普通节点</div>);
    expect(screen.getByTestId("plain")).toHaveTextContent("普通节点");
  });
});
