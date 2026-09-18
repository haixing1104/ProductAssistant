// 深链路径解析用例 —— 守护"点通知打开了错误的单"这一类事故。
//
// 为什么必须有: 通知链接是 `…/approvals/{id}?ticket=…`，**票据丢了会被后端当成无票据/越权**；
// 而 RN 又不允许像浏览器那样直接 `location.assign`，会话过期后的"回跳"也要靠这套解析。
import { pathToRoute, routeToPath } from "../navigation/paths";

describe("pathToRoute（URL → 导航目标）", () => {
  it("审批深链：把 id 与 ticket 一起带出（票据不能丢）", () => {
    expect(pathToRoute("/approvals/a-1?ticket=abc.def-ghi")).toEqual({
      name: "ApprovalDetail",
      params: { approvalId: "a-1", ticket: "abc.def-ghi" },
    });
  });

  it("审批深链：ticket 被二次编码时能解回来", () => {
    const target = pathToRoute("/approvals/a-1?ticket=ab%2Bcd%2Fef");
    expect(target.params?.ticket).toBe("ab+cd/ef");
  });

  it("无票据的审批路径（从列表点进来）→ ticket 为 undefined", () => {
    expect(pathToRoute("/approvals/a-1")).toEqual({
      name: "ApprovalDetail",
      params: { approvalId: "a-1", ticket: undefined },
    });
  });

  it("商品路径与 Tab 路径", () => {
    expect(pathToRoute("/products/p-9")).toEqual({ name: "ProductDetail", params: { productId: "p-9" } });
    expect(pathToRoute("/approvals")).toEqual({ name: "Tabs", params: { screen: "Approvals" } });
    expect(pathToRoute("/me")).toEqual({ name: "Tabs", params: { screen: "Me" } });
  });

  it("未知/空路径一律回商品列表（绝不白屏）", () => {
    expect(pathToRoute(null)).toEqual({ name: "Tabs", params: { screen: "Products" } });
    expect(pathToRoute("/whatever")).toEqual({ name: "Tabs", params: { screen: "Products" } });
  });
});

describe("routeToPath（导航状态 → URL path，供会话过期后回跳）", () => {
  it("详情页带出参数", () => {
    expect(routeToPath({ key: "a", name: "ProductDetail", params: { productId: "p-1" } })).toBe("/products/p-1");
    expect(routeToPath({ key: "b", name: "ApprovalDetail", params: { approvalId: "a-1" } })).toBe("/approvals/a-1");
  });

  it("Tab 页与未知页", () => {
    expect(routeToPath({ key: "c", name: "Approvals" })).toBe("/approvals");
    expect(routeToPath({ key: "d", name: "Me" })).toBe("/me");
    expect(routeToPath(undefined)).toBe("/");
  });
});
