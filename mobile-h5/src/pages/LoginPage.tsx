// 登录页（移动版）。
//
// 与桌面端 LoginPage 的两点差异（都是手机场景逼出来的，不是风格偏好）:
//   1) **记住组织名**：PA 的用户名只在组织内唯一，跨组织同名要靠 `org_name` 消歧；
//      手机上敲一遍企业名很痛，所以登录成功后在 localStorage 记住、下次自动带出（可清空）；
//   2) 失败用 `Toast` 而非 `message`，并把 429 的 `Retry-After` 秒数说清楚（共享 errors.ts 已翻好文案）。
import { Button, Form, Input, NavBar, NoticeBar, Toast } from "antd-mobile";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { authApi } from "@pa/core/api";
import { apiErrorMessage } from "@pa/core/services/errors";
import { resetExpiredFlag, scheduleTokenRefresh } from "@pa/core/services/http";
import { getPlatform } from "@pa/core/services/platform";
import { useAuthStore } from "@pa/core/store/authStore";

import { readRememberedOrg, rememberOrg } from "../services/rememberOrg";

interface FormValues {
  orgName?: string;
  username: string;
  password: string;
}

/** 登录/注册同页（移动版）：登录成功后记住组织名，`?reason=expired` 时提示会话过期。 */
export default function LoginPage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [loading, setLoading] = useState(false);
  const [params] = useSearchParams();
  const expired = params.get("reason") === "expired";
  const navigate = useNavigate();
  const setSession = useAuthStore((s) => s.setSession);

  const onFinish = async (values: FormValues) => {
    setLoading(true);
    try {
      if (mode === "register") {
        await authApi.register({
          org_name: values.orgName ?? "",
          username: values.username,
          password: values.password,
        });
        Toast.show({ icon: "success", content: "租户创建成功，正在登录…" });
      }
      const login = await authApi.login(values.username, values.password, values.orgName);
      // 先落 token 再取身份：http 拦截器从 authStore 读 token，
      // 若在 /auth/me 之后才 setSession，请求会带空 token → 401（登录成功却报登录失败）
      setSession(login.access_token);
      const me = await authApi.me();
      setSession(login.access_token, { id: me.id, org_id: me.org_id, username: me.username, role: me.role });
      scheduleTokenRefresh(); // 到期前主动续签（HttpOnly refresh cookie）
      resetExpiredFlag(); // 新会话开始后允许再次触发过期提示
      if (values.orgName) rememberOrg(values.orgName);
      // 回跳地址由平台端口提供（Web = sessionStorage；RN = AsyncStorage）—— 不再直接读 sessionStorage
      const returnTo = getPlatform().takeReturnUrl() || "/";
      navigate(returnTo);
    } catch (e) {
      Toast.show({ icon: "fail", content: `登录失败：${apiErrorMessage(e, "无法登录，请稍后重试")}` });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", background: "#fff" }}>
      <NavBar back={null} style={{ "--border-bottom": "none" }}>
        ProductAssistant
      </NavBar>
      <div style={{ padding: "0 16px" }}>
        <h2 style={{ margin: "8px 0 16px", fontSize: 22 }}>移动审批工作台</h2>
        {expired ? <NoticeBar color="alert" content="登录已过期，请重新登录" /> : null}
        <Form
          layout="horizontal"
          mode="card"
          initialValues={{ orgName: readRememberedOrg() }}
          onFinish={onFinish}
          footer={
            <Button block type="submit" color="primary" size="large" loading={loading}>
              {mode === "login" ? "登录" : "注册并登录"}
            </Button>
          }
        >
          {mode === "register" ? (
            <Form.Item name="orgName" label="企业名称" rules={[{ required: true, message: "请输入企业名称" }]}>
              <Input placeholder="用于开通租户" autoComplete="organization" />
            </Form.Item>
          ) : (
            <Form.Item
              name="orgName"
              label="组织名称"
              help="仅当同一用户名在多个组织中存在时才需要填写"
            >
              <Input placeholder="可留空" autoComplete="organization" />
            </Form.Item>
          )}
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input placeholder="用户名" autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true, message: "请输入密码" },
              { min: 8, message: "密码至少 8 位" },
            ]}
          >
            <Input placeholder="至少 8 位" type="password" autoComplete="current-password" />
          </Form.Item>
        </Form>
        <Button
          block
          fill="none"
          color="primary"
          onClick={() => setMode(mode === "login" ? "register" : "login")}
        >
          {mode === "login" ? "没有账号？注册新企业" : "已有账号？去登录"}
        </Button>
      </div>
    </div>
  );
}
