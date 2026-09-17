import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider, Result, Spin } from "antd";
import zhCN from "antd/locale/zh_CN";
import React, { useEffect, useState, type ReactNode } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Outlet, Route, Routes } from "react-router-dom";

import AppLayout from "./components/AppLayout";
import SessionExpiredGate from "./components/SessionExpiredGate";
import ApprovalsPage from "./pages/ApprovalsPage";
import CompliancePage from "./pages/CompliancePage";
import LoginPage from "./pages/LoginPage";
import MembersPage from "./pages/MembersPage";
import OpsPage from "./pages/OpsPage";
import ProductDetailPage from "./pages/ProductDetailPage";
import ProductsPage from "./pages/ProductsPage";
import { restoreSession } from "./services/http";
import { useAuthStore } from "./store/authStore";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

/** 会话守卫：access 只在内存，刷新页面后用 HttpOnly refresh cookie 静默恢复。 */
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
        <Spin tip="恢复会话…" />
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
        <Spin tip="加载身份…" />
      </div>
    );
  }
  if (!roles.includes(user.role)) {
    return (
      <Result
        status="403"
        title="无权访问"
        subTitle={`当前角色（${user.role}）无权访问该页面，请与管理员确认身份`}
      />
    );
  }
  return <>{children}</>;
}

function Root() {
  return (
    <ConfigProvider locale={zhCN}>
      <>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<AuthGuard />}>
            <Route element={<AppLayout />}>
              <Route index element={<ProductsPage />} />
              <Route path="/products/:productId" element={<ProductDetailPage />} />
              <Route
                path="/approvals"
                element={
                  <RoleRoute roles={["admin", "reviewer"]}>
                    <ApprovalsPage />
                  </RoleRoute>
                }
              />
              {/* 深链落地页：通知里的 /approvals/{id}?ticket=…（票据只做定位，不授予审批权限） */}
              <Route
                path="/approvals/:approvalId"
                element={
                  <RoleRoute roles={["admin", "reviewer"]}>
                    <ApprovalsPage />
                  </RoleRoute>
                }
              />
              <Route
                path="/compliance"
                element={
                  <RoleRoute roles={["admin", "reviewer"]}>
                    <CompliancePage />
                  </RoleRoute>
                }
              />
              <Route
                path="/ops"
                element={
                  <RoleRoute roles={["admin"]}>
                    <OpsPage />
                  </RoleRoute>
                }
              />
              <Route
                path="/members"
                element={
                  <RoleRoute roles={["admin"]}>
                    <MembersPage />
                  </RoleRoute>
                }
              />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        <SessionExpiredGate />
      </>
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Root />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
