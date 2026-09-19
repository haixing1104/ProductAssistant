// 商品列表（RN）用例 —— 守护两件真机上**真的出过问题**的口径（2026-09 反馈）。
//
// ① 彻底删除后卡片必须从列表消失、下面几行往上顶；
// ② 但 Android 上 FlatList 的 `removeClippedSubviews` **默认是 true**
//    （RN 0.86 `Libraries/Lists/FlatList.js`: "The default value is true for Android"）——
//    数据变短后旧 cell 的视图剥离不干净，正是"删了还留占位、下方不重排"的来源，
//    因此本列表**显式关掉**（每页 ≤10 张卡，无性能代价）。
//
// ⚠️ RNTL v14 的 `render` 返回 Promise（React 19 并发渲染）—— 必须 await。
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor } from "@testing-library/react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import ProductsScreen from "../screens/ProductsScreen";
import { productsApi } from "@pa/core/api";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Product } from "@pa/core/types/api";

jest.mock("@pa/core/api", () => ({
  productsApi: {
    list: jest.fn(),
    get: jest.fn(),
    create: jest.fn(),
    update: jest.fn(),
    purge: jest.fn(),
    generate: jest.fn(),
    importCsv: jest.fn(),
  },
}));
jest.mock("../platform/pickFile", () => ({ pickCsvFile: jest.fn(), pickImageFile: jest.fn() }));

/** 商品域桩（列表分页 + 彻底删除）。 */
const mockProducts = productsApi as unknown as { list: jest.Mock; purge: jest.Mock };

/** 造一个最小可信的商品。 */
function product(overrides: Partial<Product> = {}): Product {
  return {
    id: "p-1",
    org_id: "o-1",
    sku_code: "SKU-1",
    title: "测试商品",
    base_price: 199,
    stock_status: "in_stock",
    status: "draft",
    raw_images: [],
    ...overrides,
  };
}

/** SafeAreaProvider 的初始度量（jest 环境没有原生安全区）。 */
const insets = { top: 0, left: 0, right: 0, bottom: 0 };
/** iPhone 14 逻辑分辨率。 */
const frame = { x: 0, y: 0, width: 390, height: 844 };

/** 挂载商品列表（独立 QueryClient + 关重试）。 */
async function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <SafeAreaProvider initialMetrics={{ frame, insets }}>
      <QueryClientProvider client={qc}>
        <ProductsScreen onOpenDetail={jest.fn()} />
      </QueryClientProvider>
    </SafeAreaProvider>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  // admin 才看得到「彻底删除」（与后端仅 admin 的删除权限同口径）
  useAuthStore.setState({ token: "t", user: { username: "u", role: "admin" } });
});

describe("商品列表（RN）", () => {
  it("彻底删除后卡片从列表消失：下面几行必须重新顶上（不留占位）", async () => {
    mockProducts.list
      .mockResolvedValueOnce({
        items: [
          product({ id: "p-1", sku_code: "SKU-1" }),
          product({ id: "p-2", sku_code: "SKU-2" }),
        ],
        total: 2,
      })
      // purge 之后的 invalidate 会重新拉列表：已删的那条不再返回
      .mockResolvedValue({ items: [product({ id: "p-2", sku_code: "SKU-2" })], total: 1 });
    mockProducts.purge.mockResolvedValue({ product_id: "p-1", sku_code: "SKU-1", purge_enqueued: true });

    const view = await renderPage();
    expect(await view.findByTestId("pa-product-SKU-1")).toBeTruthy();

    // 卡片「更多」→「彻底删除」→ 填原因 → 确认
    await fireEvent.press(view.getByTestId("pa-more-SKU-1"));
    await fireEvent.press(await view.findByText("彻底删除"));
    await fireEvent.changeText(await view.findByTestId("pa-purge-reason"), "重复商品");
    await fireEvent.press(view.getByTestId("pa-purge-confirm"));

    await waitFor(() => expect(mockProducts.purge).toHaveBeenCalledWith("p-1", "重复商品"));
    // 关键断言：被删的卡片从列表里**消失**（数据层），剩下的那张还在
    await waitFor(() => expect(view.queryByTestId("pa-product-SKU-1")).toBeNull());
    expect(view.getByTestId("pa-product-SKU-2")).toBeTruthy();
  });

  it("FlatList 显式关闭 removeClippedSubviews（Android 默认 true → 删行残留空占位）", async () => {
    mockProducts.list.mockResolvedValue({ items: [product()], total: 1 });
    const view = await renderPage();
    await waitFor(() => expect(view.getByTestId("pa-product-list")).toBeTruthy());

    // FlatList 的该 prop 会透传到宿主 ScrollView 上（RN ScrollView render 里 `{...props}`）
    expect(view.getByTestId("pa-product-list").props.removeClippedSubviews).toBe(false);
  });
});
