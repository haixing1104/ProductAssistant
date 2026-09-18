// 登录页（RN 版）—— 与 H5 的 `LoginPage` 同口径。
//
// 与桌面端的三点差异（都是手机场景逼出来的，不是风格偏好）:
//   1) **记住组织名**：PA 的用户名只在组织内唯一，跨组织同名要靠 `org_name` 消歧；
//      手机上敲一遍企业名很痛，所以登录成功后记住（AsyncStorage）、下次自动带出（可清除）；
//   2) 失败用 `Toast` 而非 message，并把 429 的 `Retry-After` 秒数说清楚（共享 errors.ts 已翻好文案）；
//   3) 会话过期后重新登录要**回到原页面**：回跳地址由平台端口提供（RN 无 location，用导航层喂的路径）。
import { useEffect, useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";

import Button from "../ui/Button";
import { LabeledInput } from "../ui/Field";
import { NoticeBar } from "../ui/NavBar";
import { Toast } from "../ui/feedback";
import { colors, font, space } from "../ui/theme";
import { authApi } from "@pa/core/api";
import { apiErrorMessage } from "@pa/core/services/errors";
import { resetExpiredFlag, scheduleTokenRefresh } from "@pa/core/services/http";
import { getPlatform } from "@pa/core/services/platform";
import { useAuthStore } from "@pa/core/store/authStore";

import { readRememberedOrg, rememberOrg } from "../services/rememberOrg";

interface Props {
  /** 会话过期而来（登录页顶部提示） */
  expired?: boolean;
  /** 登录成功后去哪：由宿主（导航）决定（回跳地址优先） */
  onSignedIn: (returnTo: string | null) => void;
}

/** 登录/注册同屏（RN 版）：成功后记住组织名；跳转目标由宿主决定（RN 不能自己 location.assign）。 */
export default function LoginScreen({ expired = false, onSignedIn }: Props) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [orgName, setOrgName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<{ orgName?: string; username?: string; password?: string }>({});
  const [loading, setLoading] = useState(false);
  const setSession = useAuthStore((state) => state.setSession);

  // 带出上次记住的组织名（读不到就是空串，不阻塞界面）
  useEffect(() => {
    void readRememberedOrg().then((value) => {
      if (value) setOrgName(value);
    });
  }, []);

  const submit = async () => {
    const next: typeof errors = {};
    if (mode === "register" && !orgName.trim()) next.orgName = "请输入企业名称";
    if (!username.trim()) next.username = "请输入用户名";
    if (!password) next.password = "请输入密码";
    else if (password.length < 8) next.password = "密码至少 8 位";
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    setLoading(true);
    try {
      if (mode === "register") {
        await authApi.register({
          org_name: orgName.trim(),
          username: username.trim(),
          password,
        });
        Toast.show({ icon: "success", content: "租户创建成功，正在登录…" });
      }
      const login = await authApi.login(username.trim(), password, orgName.trim() || undefined);
      // 先落 token 再取身份：http 拦截器从 authStore 读 token，
      // 若在 /auth/me 之后才 setSession，请求会带空 token → 401（登录成功却报登录失败）
      setSession(login.access_token);
      const me = await authApi.me();
      setSession(login.access_token, { id: me.id, org_id: me.org_id, username: me.username, role: me.role });
      scheduleTokenRefresh(); // 到期前主动续签（refresh 走实现层的 cookie 存储）
      resetExpiredFlag(); // 新会话开始后允许再次触发过期提示
      if (orgName.trim()) await rememberOrg(orgName);
      onSignedIn(getPlatform().takeReturnUrl());
    } catch (error) {
      Toast.show({ icon: "fail", content: `登录失败：${apiErrorMessage(error, "无法登录，请稍后重试")}` });
    } finally {
      setLoading(false);
    }
  };

  return (
    <SafeAreaView style={styles.page} edges={["top", "bottom"]}>
      <ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={styles.content}>
        <Text style={styles.brand}>ProductAssistant</Text>
        <Text style={styles.title}>移动审批工作台</Text>
        {expired ? <NoticeBar color="alert" content="登录已过期，请重新登录" /> : null}

        {mode === "register" ? (
          <LabeledInput
            label="企业名称"
            value={orgName}
            onChangeText={setOrgName}
            placeholder="用于开通租户"
            error={errors.orgName}
            autoCapitalize="none"
            testID="pa-login-org"
          />
        ) : (
          <LabeledInput
            label="组织名称（可留空）"
            value={orgName}
            onChangeText={setOrgName}
            placeholder="可留空"
            help="仅当同一用户名在多个组织中存在时才需要填写"
            autoCapitalize="none"
            testID="pa-login-org"
          />
        )}
        <LabeledInput
          label="用户名"
          value={username}
          onChangeText={setUsername}
          placeholder="用户名"
          error={errors.username}
          autoCapitalize="none"
          autoComplete="username"
          testID="pa-login-username"
        />
        <LabeledInput
          label="密码"
          value={password}
          onChangeText={setPassword}
          placeholder="至少 8 位"
          error={errors.password}
          secureTextEntry
          autoCapitalize="none"
          autoComplete="current-password"
          testID="pa-login-password"
        />

        <Button
          block
          variant="primary"
          size="large"
          loading={loading}
          onPress={() => void submit()}
          style={styles.submit}
          testID="pa-login-submit"
        >
          {mode === "login" ? "登录" : "注册并登录"}
        </Button>
        <View style={styles.switchWrap}>
          <Button fill="none" variant="primary" onPress={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "没有账号？注册新企业" : "已有账号？去登录"}
          </Button>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bgCard },
  content: { padding: space.lg },
  brand: { fontSize: font.sm, color: colors.textSecondary, marginBottom: space.xs },
  title: { fontSize: font.xl, fontWeight: "700", color: colors.text, marginBottom: space.lg },
  submit: { marginTop: space.sm },
  switchWrap: { marginTop: space.sm, alignItems: "center" },
});
