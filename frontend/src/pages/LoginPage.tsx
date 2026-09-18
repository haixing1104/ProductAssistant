// 登录页：登录（注册入口已按 `SELF_REGISTRATION_ENABLED` 关闭，见 services/features.ts）。
//
// PA 相对 PP 的两点差异：
//   · **组织名可选**：PA 的用户名只在组织内唯一（`uq_sys_users_org_username`），
//     跨组织同名账号必须用 `org_name` 消歧 —— 因此登录表单提供「组织名称（可选）」，
//     并在后端提示需要消歧时高亮该字段，而不是直接报「用户名或密码错误」让人摸不着头脑；
//   · **登录限流提示**：429 带 `Retry-After`，errors.ts 会把它翻成「请 N 秒后再试」。
import { Alert, Button, Card, Form, Input, message, Typography } from "antd";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { authApi } from "../api";
import { apiErrorMessage } from "../services/errors";
import { SELF_REGISTRATION_ENABLED } from "../services/features";
import { resetExpiredFlag, scheduleTokenRefresh } from "../services/http";
import { getPlatform } from "../services/platform";
import { authUserFromMe, useAuthStore } from "../store/authStore";

interface FormValues {
  orgName?: string;
  username: string;
  password: string;
}

/** 登录页（注册入口由 `SELF_REGISTRATION_ENABLED` 控制，当前关闭）。 */
export default function LoginPage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const expired = params.get("reason") === "expired";
  const setSession = useAuthStore((s) => s.setSession);

  const onFinish = async (values: FormValues) => {
    setLoading(true);
    try {
      if (mode === "register") {
        await authApi.register({
          org_name: values.orgName!,
          username: values.username,
          password: values.password,
        });
        message.success("租户创建成功，正在登录…");
      }
      const login = await authApi.login(values.username, values.password, values.orgName);
      // 先落 token 再取身份：http 拦截器从 authStore 读 token，
      // 若在 /auth/me 之后才 setSession，请求会带空 token → 401（登录成功却报登录失败）
      setSession(login.access_token);
      const me = await authApi.me();
      setSession(login.access_token, authUserFromMe(me));
      scheduleTokenRefresh(); // 到期前主动续签（HttpOnly refresh cookie）
      resetExpiredFlag(); // 新会话开始后允许再次触发过期提示
      const returnTo = getPlatform().takeReturnUrl() || "/";
      navigate(returnTo);
    } catch (e) {
      message.error(`登录失败：${apiErrorMessage(e, "无法登录，请稍后重试")}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        minHeight: "100vh",
        background: "#f0f2f5",
      }}
    >
      <Card style={{ width: 400 }}>
        <Typography.Title level={3} style={{ textAlign: "center" }}>
          ProductAssistant
        </Typography.Title>
        {expired ? (
          <Alert type="warning" showIcon message="登录已过期，请重新登录" style={{ marginBottom: 12 }} />
        ) : null}
        <Form<FormValues> layout="vertical" onFinish={onFinish}>
          {mode === "register" ? (
            <Form.Item label="企业名称" name="orgName" rules={[{ required: true, message: "请输入企业名称" }]}>
              <Input placeholder="企业名称（用于开通租户）" autoComplete="organization" />
            </Form.Item>
          ) : (
            <Form.Item
              label="组织名称"
              name="orgName"
              tooltip="仅当同一用户名在多个组织中存在时才需要填写（用于区分登录到哪个组织）"
            >
              <Input placeholder="组织名称（可选）" autoComplete="organization" />
            </Form.Item>
          )}
          <Form.Item label="用户名" name="username" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input placeholder="用户名" autoComplete="username" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: "请输入密码" }, { min: 8, message: "密码至少 8 位" }]}
          >
            <Input.Password placeholder="密码（至少 8 位）" autoComplete="current-password" />
          </Form.Item>
          <Form.Item style={{ marginBottom: 8 }}>
            <Button type="primary" htmlType="submit" block loading={loading}>
              {mode === "login" ? "登录" : "注册并登录"}
            </Button>
          </Form.Item>
          {SELF_REGISTRATION_ENABLED ? (
            <Button type="link" block onClick={() => setMode(mode === "login" ? "register" : "login")}>
              {mode === "login" ? "没有账号？注册新企业" : "已有账号？去登录"}
            </Button>
          ) : null}
        </Form>
      </Card>
    </div>
  );
}
