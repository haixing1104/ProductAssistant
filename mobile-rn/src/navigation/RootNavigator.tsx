// 路由树 + 深链 + 会话恢复 —— RN 版的 `main.tsx`（H5）等价物。
//
// 结构（与 mobile-h5/src/main.tsx 一一对应）:
//   NavigationContainer(linking 深链)
//     ├─ 未登录 → Login
//     └─ 已登录 → Tabs(商品/审批/我的) + 整屏详情（ProductDetail / ApprovalDetail）
//   + SessionGate（会话过期提示，不静默硬跳）
//   + FeedbackHost（Toast/Dialog 宿主，必须在根挂一次）
//   + ForegroundRefresh（回前台刷新，见 App.tsx）
//
// 关键差异（照抄 H5 会错）:
//   · H5 用 `<Route>` 条件渲染 + `Navigate`；RN 用**条件化的 Stack.Screen**（re-navigation 更稳）；
//   · H5 登录后 `navigate(returnTo)` 直接给路径；RN 必须用 `pathToRoute()` 把路径翻成 route+params
//     （尤其是深链里的 `ticket`，丢了会被后端视为无票据）。
import { createNavigationContainerRef, NavigationContainer } from "@react-navigation/native";
import { createNativeStackNavigator } from "@react-navigation/native-stack";
import { useEffect, useState } from "react";
import { View } from "react-native";

import SessionGate from "../components/SessionGate";
import ApprovalDetailScreen from "../screens/ApprovalDetailScreen";
import LoginScreen from "../screens/LoginScreen";
import ProductDetailScreen from "../screens/ProductDetailScreen";
import { Loading } from "../ui/List";
import { colors } from "../ui/theme";
import { restoreSession } from "@pa/core/services/http";
import { getPlatform } from "@pa/core/services/platform";
import { useAuthStore } from "@pa/core/store/authStore";

import { resolveDesktopBaseUrl } from "../platform/env";
import { pathToRoute, routeToPath, type RouteTarget } from "./paths";
import TabShell from "./TabShell";

/** 根栈参数表（深链解析出来的目标最终落到这里；`ticket` 只做定位）。 */
export type RootStackParamList = {
  Login: { expired?: boolean } | undefined;
  Tabs: undefined;
  ProductDetail: { productId: string };
  ApprovalDetail: { approvalId: string; ticket?: string };
};

/** 导航引用：**只在导航容器就绪后**可用（会话过期回跳要据此判断能否立刻跳转）。 */
export const navigationRef = createNavigationContainerRef<RootStackParamList>();

/** 原生栈导航器实例（header 全部自绘：用 `ui/NavBar`，两端标题条才能长得一样）。 */
const Stack = createNativeStackNavigator<RootStackParamList>();

/**
 * 深链前缀：
 *   · 自定义 scheme（开发期用 `npx uri-scheme open productassistant://approvals/xxx?ticket=yyy`）；
 *   · 桌面端域名（生产环境真正的"点通知直接进 App"要靠 Universal Link / App Link 关联文件，
 *     那属于 infra 与域名配置 —— 本轮只保证路径能解析）。
 */
const linking = {
  prefixes: ["productassistant://", `${resolveDesktopBaseUrl()}/`],
  config: {
    screens: {
      Login: "login",
      Tabs: {
        screens: { Products: "", Approvals: "approvals", Me: "me" },
      },
      ProductDetail: "products/:productId",
      ApprovalDetail: "approvals/:approvalId",
    },
  },
};

/** 根导航：会话恢复守卫 + 深链 + 会话过期回跳（与 H5 的 AuthGuard + 路由树同口径）。 */
export default function RootNavigator() {
  const token = useAuthStore((state) => state.token);
  // 刷新后 token 在内存里丢了、但实现层的 refresh 存储仍有效 → 静默恢复一次（与 H5 的 AuthGuard 同口径）
  const [pending, setPending] = useState(!token);
  const [expiredNotice, setExpiredNotice] = useState(false);
  const [returnTarget, setReturnTarget] = useState<RouteTarget | null>(null);

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
  }, []);

  // 登录成功后回跳到"会话过期前所在的页面"（回跳地址由平台端口提供）
  useEffect(() => {
    if (!token || !returnTarget || !navigationRef.isReady()) return;
    const target = returnTarget;
    setReturnTarget(null);
    if (target.name === "Tabs") {
      navigationRef.navigate("Tabs", target.params as never);
      return;
    }
    navigationRef.navigate(target.name as "ProductDetail", target.params as never);
  }, [token, returnTarget]);

  /** 让平台端口知道"现在在哪"（RN 没有 window.location，只能由导航层喂）。 */
  const syncCurrentPath = () => {
    if (!navigationRef.isReady()) return;
    getPlatform().setCurrentPath(routeToPath(navigationRef.getCurrentRoute()));
  };

  if (pending) {
    return (
      <View style={{ flex: 1, justifyContent: "center", backgroundColor: colors.bg }}>
        <Loading />
      </View>
    );
  }

  return (
    <>
      <NavigationContainer
        ref={navigationRef}
        linking={linking}
        onReady={syncCurrentPath}
        onStateChange={syncCurrentPath}
      >
        <Stack.Navigator screenOptions={{ headerShown: false }}>
          {token ? (
            <>
              <Stack.Screen name="Tabs">
                {() => (
                  <TabShell
                    onOpenProduct={(productId) => navigationRef.navigate("ProductDetail", { productId })}
                    onOpenApproval={(approvalId) => navigationRef.navigate("ApprovalDetail", { approvalId })}
                    onSignedOut={() => setExpiredNotice(false)}
                  />
                )}
              </Stack.Screen>
              <Stack.Screen name="ProductDetail">
                {({ route }) => (
                  <ProductDetailScreen
                    productId={route.params.productId}
                    onBack={() => navigationRef.canGoBack() && navigationRef.goBack()}
                  />
                )}
              </Stack.Screen>
              <Stack.Screen name="ApprovalDetail">
                {({ route }) => (
                  <ApprovalDetailScreen
                    approvalId={route.params.approvalId}
                    ticket={route.params.ticket ?? null}
                    onBack={() => navigationRef.canGoBack() && navigationRef.goBack()}
                  />
                )}
              </Stack.Screen>
            </>
          ) : (
            <Stack.Screen name="Login">
              {() => (
                <LoginScreen
                  expired={expiredNotice}
                  onSignedIn={(returnTo) => {
                    setExpiredNotice(false);
                    setReturnTarget(returnTo ? pathToRoute(returnTo) : null);
                  }}
                />
              )}
            </Stack.Screen>
          )}
        </Stack.Navigator>
      </NavigationContainer>
      <SessionGate
        onRelogin={() => {
          // 平台端口已经把登录态清空 → 上面的条件渲染会自动切回 Login；这里只负责"说明原因"
          setExpiredNotice(true);
        }}
      />
    </>
  );
}
