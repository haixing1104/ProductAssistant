// 底部 Tab 外壳（商品 / 审批 / 我的）—— 与 H5 的 `TabShell` 同口径。
//
// 与桌面 `AppLayout` 的对应关系: 桌面用左侧 Menu + 顶部身份条；移动端用底部 TabBar，
// 身份与退出登录收进「我的」（手机顶部空间要留给页面标题与主操作）。
// 角色显隐与后端 RBAC 一一对应（复用共享层 `canApprove`，避免与后端漂移）。
//
// 用 `createBottomTabNavigator`（成熟库）而不是自研 tab：
//   底部 Tab 涉及安全区、Android 返回栈、屏幕外缓存这些**平台耦合**细节，属于"该用库"的一类；
//   自研薄 UI 只覆盖样式型组件（Button/Tag/Card 等）。
import { createBottomTabNavigator } from "@react-navigation/bottom-tabs";
import { StyleSheet, Text } from "react-native";

import { colors, font, TAB_BAR_HEIGHT } from "../ui/theme";
import { canApprove, useAuthStore } from "@pa/core/store/authStore";

import ApprovalsScreen from "../screens/ApprovalsScreen";
import MeScreen from "../screens/MeScreen";
import ProductsScreen from "../screens/ProductsScreen";

export type TabParamList = {
  Products: undefined;
  Approvals: undefined;
  Me: undefined;
};

/** 无图标库：用字符字形做 Tab 图标（零依赖；语义一眼可辨即可）。 */
function TabGlyph({ glyph, color }: { glyph: string; color: string }) {
  return <Text style={[styles.glyph, { color }]}>{glyph}</Text>;
}

interface Props {
  onOpenProduct: (productId: string) => void;
  onOpenApproval: (approvalId: string) => void;
  onSignedOut: () => void;
}

export default function TabShell({ onOpenProduct, onOpenApproval, onSignedOut }: Props) {
  const Tab = createBottomTabNavigator<TabParamList>();
  const role = useAuthStore((state) => state.user?.role);

  return (
    <Tab.Navigator
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textSecondary,
        tabBarStyle: { height: TAB_BAR_HEIGHT + 16 },
        tabBarLabelStyle: { fontSize: font.xs },
      }}
    >
      <Tab.Screen
        name="Products"
        options={{
          title: "商品",
          tabBarIcon: ({ color }) => <TabGlyph glyph="▤" color={color} />,
        }}
      >
        {() => <ProductsScreen onOpenDetail={onOpenProduct} />}
      </Tab.Screen>

      {/* 审批 Tab 只对 admin/reviewer 显示（与后端 RBAC 一致；隐藏只是体验，真正的拒绝在服务端） */}
      {canApprove(role) ? (
        <Tab.Screen
          name="Approvals"
          options={{
            title: "审批",
            tabBarIcon: ({ color }) => <TabGlyph glyph="☑" color={color} />,
          }}
        >
          {() => <ApprovalsScreen onOpenDetail={onOpenApproval} />}
        </Tab.Screen>
      ) : null}

      <Tab.Screen
        name="Me"
        options={{ title: "我的", tabBarIcon: ({ color }) => <TabGlyph glyph="☺" color={color} /> }}
      >
        {() => <MeScreen onSignedOut={onSignedOut} />}
      </Tab.Screen>
    </Tab.Navigator>
  );
}

const styles = StyleSheet.create({
  glyph: { fontSize: 18 },
});
