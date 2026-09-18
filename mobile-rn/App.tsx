// App 根装配（P0 时的占位骨架已替换为真实路由树）。
//
// 组成（与 mobile-h5/src/main.tsx 对照看）:
//   SafeAreaProvider      → 安全区（iOS 刘海 / 底部横条）
//   QueryClientProvider   → React Query（同一份缓存口径：retry 1、不做 focus 自动刷新）
//   RootNavigator         → 路由树 + 深链 + 会话恢复
//   FeedbackHost          → Toast/Dialog 宿主（命令式 API 的渲染层，**必须挂一次**）
//   ForegroundRefresh     → 回前台刷新（移动端特有，见下方注释）
//
// 注意：平台实现（`installRnPlatform()`）在 `index.ts` 入口就已装好 ——
// 共享契约层的 http/sse 在**首次请求/连流**时就要按平台取基址与传输实现。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { AppState } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import RootNavigator from "./src/navigation/RootNavigator";
import { FeedbackHost } from "./src/ui/feedback";

/** React Query 单例（与桌面端/H5 同口径：retry 1、不做 focus 刷新；回前台刷新见 `ForegroundRefresh`）。 */
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

/**
 * 回到前台自动刷新（**移动端特有**）。
 *
 * 为什么必须有：iOS/Android 把 App 切到后台会**冻结** JS（SSE 长连接、定时器、轮询全停），
 * 用户切回来看到的可能是十分钟前的旧状态（生成已完成却还显示「生成中…」）。
 * 这里只在"从后台回到前台"时失效一次查询，代价极小、收益极大（与 H5 的 visibilitychange 同口径）。
 */
function ForegroundRefresh() {
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") void queryClient.invalidateQueries();
    });
    return () => subscription.remove();
  }, []);
  return null;
}

/** App 根：安全区 → React Query → 路由树 + 反馈宿主 + 回前台刷新 + 状态栏。 */
export default function App() {
  return (
    <SafeAreaProvider>
      <QueryClientProvider client={queryClient}>
        <RootNavigator />
        <FeedbackHost />
        <ForegroundRefresh />
        <StatusBar style="auto" />
      </QueryClientProvider>
    </SafeAreaProvider>
  );
}
