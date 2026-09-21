// 商品列表（RN 版）—— 与 H5 的 `ProductsPage` 同口径：卡片列表 + 状态筛选 + 下拉刷新 + 触底加载。
//
// 表格 → 卡片的落法（与 H5 端口径完全一致）:
//   桌面列 `SKU / 标题 / 价格 / 库存 / 状态 / 操作` →
//   卡片头 = 标题 + 状态 Tag（扫视锚点）；卡身 = SKU/价格/库存；
//   卡底 = 主操作（生成/编辑），低频危险操作（彻底删除）收进 ActionSheet。
// 分页: 桌面是表格分页；移动端换 `useInfiniteQuery` 触底加载，并保留「共 N 条」（滚动位置感比页码更重要）。
import { useState } from "react";
import { FlatList, RefreshControl, StyleSheet, Text, View } from "react-native";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import Button from "../ui/Button";
import Card from "../ui/Card";
import Chips from "../ui/Chips";
import { EmptyState, ListRow, ListSection, Loading, PreWrapText } from "../ui/List";
import { NavBar, NoticeBar } from "../ui/NavBar";
import Screen from "../ui/Screen";
import { ActionSheet, Sheet } from "../ui/Sheet";
import { LabeledTextArea } from "../ui/Field";
import { colors, font, space } from "../ui/theme";
import { Dialog, Toast } from "../ui/feedback";
import QueryError from "../components/QueryError";
import ProductEditorSheet from "../components/ProductEditorSheet";
import { ProductStatusTag } from "../components/StatusTag";
import { productsApi } from "@pa/core/api";
import { apiErrorMessage, csvRowErrors } from "@pa/core/services/errors";
import { PRODUCT_STATUS_FILTERS, stockStatusLabel } from "@pa/core/services/productMeta";
import { formatPrice, formatTime } from "@pa/core/services/mobileFormat";
import { canWriteProducts, isAdmin, useAuthStore } from "@pa/core/store/authStore";
import type { CsvRowError } from "@pa/core/services/errors";
import type { Product } from "@pa/core/types/api";

import { pickCsvFile } from "../platform/pickFile";

/** 触底加载每页条数（与 H5/桌面端同口径）。 */
const PAGE_SIZE = 10;
/** CSV 模板表头（导入入口下载空模板用）。 */
const CSV_TEMPLATE = "sku_code,title,base_price,stock_status,raw_images";

/** 状态筛选选项（「全部」+ 共享层的状态枚举，避免两处各写一份）。 */
const STATUS_FILTERS = [{ value: "", label: "全部" }, ...PRODUCT_STATUS_FILTERS];

/** 商品列表（RN 版）：卡片列表 + 状态筛选 + 下拉刷新 + 触底加载 + 新建编辑 + CSV 导入。 */
export default function ProductsScreen({ onOpenDetail }: { onOpenDetail: (productId: string) => void }) {
  const qc = useQueryClient();
  const role = useAuthStore((state) => state.user?.role);
  const writable = canWriteProducts(role);

  const [statusFilter, setStatusFilter] = useState("");
  const [editorTarget, setEditorTarget] = useState<Product | "new" | null>(null);
  const [moreTarget, setMoreTarget] = useState<Product | null>(null);
  const [pageActionsOpen, setPageActionsOpen] = useState(false);
  const [templateOpen, setTemplateOpen] = useState(false);
  const [purgeTarget, setPurgeTarget] = useState<Product | null>(null);
  const [purgeReason, setPurgeReason] = useState("");
  const [csvErrors, setCsvErrors] = useState<CsvRowError[]>([]);
  /**
   * 下拉刷新指示器必须**受控**（只反映"用户下拉"这一次）。
   *
   * 为什么不能用 `list.isRefetching`：purge/保存/生成之后我们会 `invalidateQueries`，
   * 那些**后台** refetch 也会让 `isRefetching` 变 true → 指示器凭空出现并把内容顶下去，
   * 看起来就是"列表顶部留了一块占位"（2026-09 真机反馈的现场之一）。
   */
  const [refreshing, setRefreshing] = useState(false);

  const list = useInfiniteQuery({
    queryKey: ["products", statusFilter],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      productsApi.list({ offset: pageParam, limit: PAGE_SIZE, status: statusFilter || undefined }),
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  const items: Product[] = list.data?.pages.flatMap((page) => page.items) ?? [];
  const total = list.data?.pages[0]?.total ?? 0;

  const generate = useMutation({
    mutationFn: (id: string) => productsApi.generate(id),
    onSuccess: (result, id) => {
      Toast.show({ content: `已触发生成（任务 ${result.thread_id.slice(0, 8)}…）` });
      qc.invalidateQueries({ queryKey: ["products"] });
      // 直接进详情看打字机：列表没有实时流，这是确认"生成真的跑起来了"最快的路径
      onOpenDetail(id);
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "触发生成失败") }),
  });

  const purge = useMutation({
    mutationFn: (target: Product) => productsApi.purge(target.id, purgeReason.trim()),
    onSuccess: (result) => {
      Toast.show({ icon: "success", content: `已删除 ${result.sku_code}（OSS 清理任务已入队）` });
      qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "彻底删除失败") }),
  });

  /**
   * CSV 导入：成功是 `{created, skus}`；**逐行错误只在 400 的 `data.row_errors`** —— 必须逐行展示，
   * 否则运营只知道"导入失败"，不知道哪一行、为什么（H5 同口径）。
   */
  const importCsv = async () => {
    try {
      const file = await pickCsvFile();
      if (!file) return;
      const result = await productsApi.importCsv(file);
      setCsvErrors([]);
      Toast.show({
        icon: "success",
        content: `已导入 ${result.created} 个商品${result.skus.length ? `（${result.skus.slice(0, 3).join("、")}）` : ""}`,
      });
      qc.invalidateQueries({ queryKey: ["products"] });
    } catch (error) {
      const rows = csvRowErrors(error);
      if (rows.length > 0) {
        setCsvErrors(rows);
        return;
      }
      Toast.show({ icon: "fail", content: apiErrorMessage(error, "导入失败") });
    }
  };

  /** 导入模板：RN 没有"下载文件"，改为系统分享一段 CSV 文本（够用且零依赖）。 */
  const shareTemplate = async () => {
    try {
      const { Share } = await import("react-native");
      await Share.share({ message: `${CSV_TEMPLATE}\n` });
    } catch {
      // 用户取消分享不算失败
    }
  };

  /** 用户下拉刷新（受控 refreshing：结束后必须复位，否则指示器永远转）。 */
  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await list.refetch();
    } finally {
      setRefreshing(false);
    }
  };

  const renderItem = (item: Product) => (
    <Card
      title={item.title}
      extra={<ProductStatusTag status={item.status} />}
      onPress={() => onOpenDetail(item.id)}
      style={styles.card}
      testID={`pa-product-${item.sku_code}`}
    >
      <ListRow label="SKU" extra={item.sku_code} />
      <ListRow label="售价" extra={formatPrice(item.base_price)} />
      <ListRow label="库存" extra={stockStatusLabel(item.stock_status)} />
      <ListRow label="最近更新" extra={formatTime(item.updated_at)} />
      <View style={styles.cardActions}>
        <Button
          size="small"
          variant="primary"
          fill="outline"
          disabled={!writable || generate.isPending}
          onPress={() => generate.mutate(item.id)}
          testID={`pa-generate-${item.sku_code}`}
        >
          生成
        </Button>
        <Button size="small" disabled={!writable} onPress={() => setEditorTarget(item)}>
          编辑
        </Button>
        <Button size="small" onPress={() => setMoreTarget(item)} testID={`pa-more-${item.sku_code}`}>
          更多
        </Button>
      </View>
      {/* 失败原因直接铺在卡片上：手机上"进详情才知道为什么失败"会让人白跑一趟 */}
      {item.active_job_error ? (
        <View style={styles.cardNotice}>
          <NoticeBar color="error" content={`最近失败原因：${item.active_job_error}`} />
        </View>
      ) : null}
    </Card>
  );


  return (
    <View style={styles.page}>
      <NavBar
        title="商品"
        back={null}
        right={
          <Button size="mini" onPress={() => setPageActionsOpen(true)} testID="pa-products-actions">
            导入
          </Button>
        }
      />
      <Chips
        options={STATUS_FILTERS}
        value={statusFilter}
        onChange={setStatusFilter}
        scrollable
        testID="pa-status-filter"
      />

      {list.isLoading ? (
        <Loading />
      ) : list.isError ? (
        <QueryError what="商品列表加载" error={list.error} onRetry={() => void list.refetch()} />
      ) : (
        /**
         * ⚠️ Android 上 FlatList 的 `removeClippedSubviews` **默认是 true**
         * （RN 0.86 `Libraries/Lists/FlatList.js`: "The default value is true for Android"）——
         * 数据变短（如彻底删除一行）后旧 cell 的视图剥离不干净，表现为
         * 「被删的那行留下空占位、下面几行不往上顶」（2026-09 真机反馈）。
         * 本列表每页 ≤10 张卡，关掉它没有任何性能代价。
         */
        <FlatList
          data={items}
          keyExtractor={(item) => item.id}
          renderItem={({ item }) => renderItem(item)}
          contentContainerStyle={styles.listContent}
          removeClippedSubviews={false}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => void onRefresh()} />}
          onEndReachedThreshold={0.4}
          onEndReached={() => {
            if (list.hasNextPage && !list.isFetchingNextPage) void list.fetchNextPage();
          }}
          ListEmptyComponent={
            <EmptyState title="还没有商品" description="点右下角「＋」新建，或用右上角「导入」批量导入 CSV" />
          }
          ListFooterComponent={
            <Text style={styles.footer}>{list.isFetchingNextPage ? "加载中…" : `共 ${total} 条`}</Text>
          }
          testID="pa-product-list"
        />
      )}

      {/* 右下角新建（替代 antd-mobile 的 FloatingBubble：绝对定位按钮即可，不必多引依赖） */}
      {writable ? (
        <Button
          variant="primary"
          size="large"
          onPress={() => setEditorTarget("new")}
          style={styles.fab}
          testID="pa-product-create"
        >
          ＋
        </Button>
      ) : null}

      <ProductEditorSheet
        target={editorTarget}
        onClose={() => setEditorTarget(null)}
        onSaved={() => qc.invalidateQueries({ queryKey: ["products"] })}
      />

      <ActionSheet
        visible={pageActionsOpen}
        onClose={() => setPageActionsOpen(false)}
        actions={[
          { key: "import", text: "导入 CSV" },
          { key: "template", text: "分享导入模板" },
        ]}
        onAction={(action) => {
          setPageActionsOpen(false);
          if (action.key === "import") void importCsv();
          if (action.key === "template") void shareTemplate();
        }}
      />

      <ActionSheet
        visible={moreTarget !== null}
        onClose={() => setMoreTarget(null)}
        actions={[
          { key: "detail", text: "查看详情" },
          ...(isAdmin(role) ? [{ key: "purge", text: "彻底删除", danger: true }] : []),
        ]}
        onAction={(action) => {
          const target = moreTarget;
          setMoreTarget(null);
          if (!target) return;
          if (action.key === "detail") onOpenDetail(target.id);
          if (action.key === "purge") {
            setPurgeTarget(target);
            setPurgeReason("");
          }
        }}
      />

      {/* 彻底删除（admin + 原因必填，写审计）：软删端点已下线，删除只有这一条路径 */}
      <Sheet visible={purgeTarget !== null} onClose={() => setPurgeTarget(null)} testID="pa-purge-sheet">
        <View style={styles.sheetBody}>
          <Text style={styles.sheetTitle}>彻底删除 {purgeTarget?.sku_code ?? ""}</Text>
          <PreWrapText style={styles.sheetHint}>删除后内容与 OSS 对象会一并清理，且不可恢复。</PreWrapText>
          <LabeledTextArea
            label="删除原因（必填，会写入审计）"
            value={purgeReason}
            onChangeText={setPurgeReason}
            placeholder="请填写删除原因"
            rows={3}
            testID="pa-purge-reason"
          />
          <Button
            block
            variant="danger"
            size="large"
            loading={purge.isPending}
            disabled={purgeReason.trim().length === 0}
            onPress={() => {
              if (purgeTarget) purge.mutate(purgeTarget);
              setPurgeTarget(null);
            }}
            testID="pa-purge-confirm"
          >
            确认删除
          </Button>
          <Button block fill="none" onPress={() => setPurgeTarget(null)} style={styles.sheetCancel}>
            取消
          </Button>
        </View>
      </Sheet>

      {/* 逐行错误（只有 400 的 data.row_errors 才有）：哪一行、哪个 SKU、为什么 */}
      <Sheet visible={csvErrors.length > 0} onClose={() => setCsvErrors([])} testID="pa-csv-errors">
        <View style={styles.sheetBody}>
          <Text style={styles.sheetTitle}>导入失败：{csvErrors.length} 行有误</Text>
          <ListSection>
            {csvErrors.slice(0, 20).map((row, index) => (
              <ListRow
                key={`${row.line}-${index}`}
                label={`第 ${row.line} 行`}
                description={
                  <PreWrapText style={styles.csvReason}>
                    {row.sku_code ? `${row.sku_code}：` : ""}
                    {row.reason}
                  </PreWrapText>
                }
              />
            ))}
          </ListSection>
          {csvErrors.length > 20 ? <Text style={styles.sheetHint}>仅显示前 20 条</Text> : null}
          <Button block variant="primary" size="large" onPress={() => setCsvErrors([])}>
            知道了
          </Button>
        </View>
      </Sheet>
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bg },
  listContent: { paddingBottom: space.xl, paddingTop: space.sm },
  card: { marginHorizontal: space.md, marginBottom: space.sm, borderWidth: 1, borderColor: colors.border },
  cardActions: { flexDirection: "row", gap: space.sm, paddingHorizontal: space.md, marginTop: space.sm },
  cardNotice: { paddingHorizontal: space.md, marginTop: space.xs },
  footer: { textAlign: "center", color: colors.textSecondary, fontSize: font.sm, paddingVertical: space.md },
  fab: {
    position: "absolute",
    right: space.lg,
    bottom: space.xl + 56,
    width: 52,
    height: 52,
    borderRadius: 26,
    paddingHorizontal: 0,
  },
  sheetBody: { padding: space.md },
  sheetTitle: { fontSize: font.lg, fontWeight: "600", marginBottom: space.sm, color: colors.text },
  sheetHint: { fontSize: font.sm, color: colors.textSecondary, marginBottom: space.sm },
  sheetCancel: { marginTop: space.sm },
  csvReason: { fontSize: font.sm, color: colors.danger },
});


