// 应用装配（移动端 H5，与桌面端 `frontend/src/main.tsx` 对照看）。
//
// 与桌面端的差异只有“装配件”，契约层是**同一份**（`@pa/core/*` → ../frontend/src/*）:
//   TabShell       → 底部 TabBar（桌面是左侧 Menu）
//   SessionGate    → 会话过期弹窗（共享层事件；桌面叫 SessionExpiredGate）
//   ForegroundRefresh → 回前台刷新（**移动端特有**，见下方注释）
//
// ⚠️ `installAntdMobileReact19Shim()` 必须在**任何 antd-mobile 命令式 API 使用之前**调用。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider, Result, SpinLoading } from "antd-mobile";
import zhCN from "antd-mobile/es/locales/zh-CN";
import { useEffect, useState, type ReactNode } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";

import SessionGate from "./components/SessionGate";
import TabShell from "./components/TabShell";
import ApprovalDetailPage from "./pages/ApprovalDetailPage";
import ApprovalsPage from "./pages/ApprovalsPage";
import LoginPage from "./pages/LoginPage";
import MePage from "./pages/MePage";
import ProductDetailPage from "./pages/ProductDetailPage";
import ProductsPage from "./pages/ProductsPage";
import { restoreSession } from "@pa/core/services/http";
import { useAuthStore } from "@pa/core/store/authStore";

import { installAntdMobileReact19Shim } from "./services/antdMobileReact19";
import "./styles/global.css";

// 必须在**任何 antd-mobile 命令式 API（Toast/Dialog）使用前**安装：
// React 19 移除了 ReactDOM.render / unmountComponentAtNode，antd-mobile v5 默认实现会静默不渲染。
installAntdMobileReact19Shim();

/** React Query 单例：与桌面端同口径（retry 1、不做 focus 刷新；移动端另由 ForegroundRefresh 兜底）。 */
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

/** 会话守卫：access 只在内存，刷新页面后用 HttpOnly refresh cookie 静默恢复（与桌面端同一份 http.ts）。 */
function AuthGuard() {
  const token = useAuthStore((s) => s.token);
  const [pending, setPending] = useState<boolean>(!token);

  useEffect(() => {
    let active = true;
    if (!useAuthStore.getState().token) {
      void restoreSession().finally(() => {
        if (active) setPending(false);
      });
    } else {
      setPending(false);
    }
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (pending) {
    return (
      <div style={{ display: "flex", justifyContent: "center", paddingTop: 120 }}>
        <SpinLoading style={{ "--size": "32px" }} />
      </div>
    );
  }
  if (!useAuthStore.getState().token) return <Navigate to="/login" replace />;
  return <Outlet />;
}

/** 角色守卫：与 backend RBAC 对齐（前端隐藏只是体验，真正的拒绝在服务端）。 */
function RoleRoute({ roles, children }: { roles: string[]; children: ReactNode }) {
  const user = useAuthStore((s) => s.user);
  if (!user?.role) {
    return (
      <div style={{ display: "flex", justifyContent: "center", paddingTop: 80 }}>
        <SpinLoading style={{ "--size": "28px" }} />
      </div>
    );
  }
  if (!roles.includes(user.role)) {
    return <Result status="warning" title="无权访问" description={`当前角色（${user.role}）无权访问该页面`} />;
  }
  return <>{children}</>;
}

/**
 * 回到前台自动刷新（**移动端特有**）。
 *
 * 为什么必须有：iOS/Android 把浏览器切到后台会**冻结** JS（SSE 长连接、定时器、轮询全停），
 * 用户切回来看到的可能是十分钟前的旧状态（生成已完成却还显示「生成中…」）。
 * 这里只在"从后台回到前台"时失效一次查询，代价极小、收益极大。
 */
function ForegroundRefresh() {
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void queryClient.invalidateQueries();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);
  return null;
}

/** 路由树：列表类页面走 TabShell（带底部 TabBar），详情类页面整屏（移动端返回手势更自然）。 */
function Root() {
  const location = useLocation();
  return (
    <ConfigProvider locale={zhCN}>
      <>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<AuthGuard />}>
            {/* 列表类页面：底部 TabBar（商品 / 审批 / 我的） */}
            <Route element={<TabShell />}>
              <Route index element={<ProductsPage />} />
              <Route
                path="/approvals"
                element={
                  <RoleRoute roles={["admin", "reviewer"]}>
                    <ApprovalsPage />
                  </RoleRoute>
                }
              />
              <Route path="/me" element={<MePage />} />
            </Route>
            {/* 详情类页面：整屏（无 TabBar），回到列表用 NavBar 返回 —— 手机返回手势/按钮行为更自然 */}
            <Route path="/products/:productId" element={<ProductDetailPage />} />
            <Route
              path="/approvals/:approvalId"
              element={
                <RoleRoute roles={["admin", "reviewer"]}>
                  <ApprovalDetailPage />
                </RoleRoute>
              }
            />
          </Route>
          {/* 深链兜底：通知里的链接落在 /approvals/{id}?ticket=…，未知路径回首页 */}
          <Route path="*" element={<Navigate to="/" replace state={{ from: location.pathname }} />} />
        </Routes>
        <SessionGate />
        <ForegroundRefresh />
      </>
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    <BrowserRouter>
      <Root />
    </BrowserRouter>
  </QueryClientProvider>,
);
