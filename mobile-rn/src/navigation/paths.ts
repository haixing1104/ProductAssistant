// 深链路径 ↔ 导航参数的纯函数（**单测守护**：这条链路出错的表现是"点通知打开了错误的单"）。
//
// 为什么必须显式解析:
//   · 通知里的链接形如 `…/approvals/{id}?ticket=…`，**票据不能丢**（丢了会被后端当成越权/无票据）；
//   · RN 不能像浏览器那样 `location.assign`，会话过期后要"回到原页面"只能靠 route 名 + 参数重建。
import type { NavigationState } from "@react-navigation/native";

/** 导航目标（与 `../navigation/paths` 的解析结果同形状）。 */
export interface RouteTarget {
  name: string;
  params?: Record<string, unknown>;
}

/** URL path → 导航目标（未知路径一律回商品列表，绝不白屏）。 */
export function pathToRoute(path: string | null | undefined): RouteTarget {
  if (!path) return { name: "Tabs", params: { screen: "Products" } };

  const product = /^\/products\/([^/?#]+)/.exec(path);
  if (product) return { name: "ProductDetail", params: { productId: product[1] } };

  const approval = /^\/approvals\/([^/?#]+)/.exec(path);
  if (approval) {
    const ticket = /[?&]ticket=([^&#]+)/.exec(path);
    return {
      name: "ApprovalDetail",
      params: {
        approvalId: approval[1],
        // 票据要解码：它本身是 base64url，但可能被再编码一次
        ticket: ticket ? decodeURIComponent(ticket[1]) : undefined,
      },
    };
  }
  if (path.startsWith("/approvals")) return { name: "Tabs", params: { screen: "Approvals" } };
  if (path.startsWith("/me")) return { name: "Tabs", params: { screen: "Me" } };
  return { name: "Tabs", params: { screen: "Products" } };
}

/** 当前导航状态 → URL path（喂给平台端口的 `setCurrentPath`，供会话过期后回跳）。 */
export function routeToPath(route: NavigationState["routes"][number] | undefined): string {
  if (!route) return "/";
  const params = (route.params ?? {}) as Record<string, unknown>;
  if (route.name === "ProductDetail" && typeof params.productId === "string") {
    return `/products/${params.productId}`;
  }
  if (route.name === "ApprovalDetail" && typeof params.approvalId === "string") {
    return `/approvals/${params.approvalId}`;
  }
  if (route.name === "Approvals") return "/approvals";
  if (route.name === "Me") return "/me";
  return "/";
}
