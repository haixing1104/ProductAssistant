// antd-mobile v5 + React 19 的兼容层（**不装它，Toast/Dialog 在浏览器里会静默不渲染**）。
//
// 成因（读源码 + 实测）:
//   · antd-mobile 的 `Toast.show` / `Dialog.alert|confirm` 走命令式渲染
//     `renderImperatively → renderToBody → unstableSetRender()(element, container)`；
//   · 默认实现里 `render/unmount` 依赖 `ReactDOM.render` + `unmountComponentAtNode`
//     —— 这两个 API **在 React 19 已被移除**，而 react-dom 19 的入口也不再导出 `createRoot`，
//     于是它退化成"什么都不做"（渲染静默失败、卸载抛 `unmountComponentAtNode is not a function`）；
//   · antd-mobile 自己的告警也点明了这件事：
//     `[Compatible] antd-mobile v5 support React is 16 ~ 18. see .../guide/v5-for-19`。
// 解法：注入基于 `react-dom/client` 的 createRoot 实现（antd-mobile 官方 v5-for-19 的口径）。
import { createRoot, type Root } from "react-dom/client";
import { unstableSetRender } from "antd-mobile";

type RootHost = HTMLElement & { __paReactRoot?: Root };

let installed = false;

/** 幂等安装（重复调用无副作用；在 main.tsx 与测试 setup 里都可安全调用）。 */
export function installAntdMobileReact19Shim(): void {
  if (installed) return;
  installed = true;
  unstableSetRender((node, container) => {
    const host = container as RootHost;
    host.__paReactRoot ??= createRoot(container);
    const root = host.__paReactRoot;
    root.render(node);
    return async () => {
      // 等一帧再卸载：给弹层的退场动画/回调留出时间，避免 "unmount 后仍 setState" 的告警
      await new Promise((resolve) => setTimeout(resolve, 0));
      root.unmount();
      delete host.__paReactRoot;
    };
  });
}
