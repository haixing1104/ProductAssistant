// 应用入口（与 Expo SDK 57 官方模板同口径：package.json 的 `main` = index.ts + registerRootComponent）。
// 真实的路由树、Provider、会话恢复在 App.tsx 里装配。
import { registerRootComponent } from "expo";

import App from "./App";
import { installRnPlatform } from "./src/platform/rnPlatform";

// ⚠️ **必须在 App 之前**：共享契约层（@pa/core/services/http|sse）在首次请求/连流时
// 会按当前平台取「接口基址」与「传输实现」。装晚了会先走 Web 默认实现（相对路径 + 浏览器读流），
// 表现是 RN 上所有请求都发不出去（相对 URL 在原生端无效）。
installRnPlatform();

registerRootComponent(App);
