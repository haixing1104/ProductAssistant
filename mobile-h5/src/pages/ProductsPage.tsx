// 商品列表（移动版）：卡片列表 + 状态筛选 + 下拉刷新 + 触底加载。
//
// 表格 → 卡片的落法（移动端口径：RN 端必须完全一致）:
//   桌面列 `SKU / 标题 / 价格 / 库存 / 状态 / 操作` →
//   卡片头 = 标题 + 状态 Tag（扫视锚点）；卡身 = SKU/价格/库存；
//   卡底 = 主操作（生成/编辑），低频危险操作（彻底删除）收进 ActionSheet。
// 分页口径: 桌面是 `Table pagination` + `pageSize`；移动端换 `useInfiniteQuery` 触底加载，
// 并保留「共 N 条」（滚动位置感比页码更重要）。
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ActionSheet,
  Button,
  CapsuleTabs,
  Card,
  Dialog,
  FloatingBubble,
  InfiniteScroll,
  List,
  NavBar,
  PullToRefresh,
  Tag,
  TextArea,
  Toast,
} from "antd-mobile";
import { AddOutline, MoreOutline } from "antd-mobile-icons";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { productsApi } from "@pa/core/api";
import { apiErrorMessage, csvRowErrors } from "@pa/core/services/errors";
import { PRODUCT_STATUS_FILTERS, stockStatusLabel } from "@pa/core/services/productMeta";
import { canWriteProducts, isAdmin, useAuthStore } from "@pa/core/store/authStore";
import type { Product } from "@pa/core/types/api";

import ProductEditorPopup from "../components/ProductEditorPopup";
import QueryError from "../components/QueryError";
import { ProductStatusTag } from "../components/StatusTag";
import { formatPrice } from "@pa/core/services/mobileFormat";

/** 触底加载的每页条数（与桌面端 pageSize 同口径，便于两端结果条数对齐）。 */
const PAGE_SIZE = 10;
/** CSV 模板表头（导入入口给用户下载空模板用；字段顺序与服务端解析一致）。 */
const CSV_TEMPLATE = "sku_code,title,base_price,stock_status,raw_images";

/** 商品列表页（卡片列表 + 筛选/下拉刷新/触底加载 + 新建编辑 + CSV 导入 + 触发生成）。 */
export default function ProductsPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const role = useAuthStore((s) => s.user?.role);
  const writable = canWriteProducts(role);

  const [statusFilter, setStatusFilter] = useState<string>("");
  const [statusKey, setStatusKey] = useState(0);
  const [editorTarget, setEditorTarget] = useState<Product | "new" | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [moreTarget, setMoreTarget] = useState<Product | null>(null);
  const [pageActionsOpen, setPageActionsOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeTarget, setPurgeTarget] = useState<Product | null>(null);
  const [purgeReason, setPurgeReason] = useState("");
  const csvRef = useRef<HTMLInputElement | null>(null);

  const list = useInfiniteQuery({
    queryKey: ["products", statusFilter, statusKey],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      productsApi.list({ offset: pageParam, limit: PAGE_SIZE, status: statusFilter || undefined }),
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, p) => n + p.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  const items: Product[] = list.data?.pages.flatMap((p) => p.items) ?? [];
  const total = list.data?.pages[0]?.total ?? 0;

  const generate = useMutation({
    mutationFn: (id: string) => productsApi.generate(id),
    onSuccess: (result, id) => {
      Toast.show({ content: `已触发生成（任务 ${result.thread_id.slice(0, 8)}…）` });
      qc.invalidateQueries({ queryKey: ["products"] });
      // 直接进详情看打字机：列表没有实时流，这是确认"生成真的跑起来了"最快的路径
      navigate(`/products/${id}`);
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "触发生成失败") }),
  });

  /** CSV 导入：成功是 `{created, skus}`；**逐行错误只在 400 的 `data.row_errors`** —— 必须逐行展示。 */
  const importCsv = async (file: File) => {
    try {
      const result = await productsApi.importCsv(file);
      Toast.show({ icon: "success", content: `导入成功 ${result.created ?? 0} 条` });
      qc.invalidateQueries({ queryKey: ["products"] });
    } catch (e) {
      const rows = csvRowErrors(e);
      if (rows.length > 0) {
        void Dialog.alert({
          title: `导入失败：${rows.length} 行有问题`,
          content: (
            <div style={{ maxHeight: 240, overflow: "auto", fontSize: 12 }} className="pa-pre-wrap">
              {rows.map((row, i) => (
                <div key={`${row.line}-${row.sku_code ?? i}`}>
                  第 {row.line} 行{row.sku_code ? `（${row.sku_code}）` : ""}：{row.reason}
                </div>
              ))}
            </div>
          ),
        });
      } else {
        Toast.show({ icon: "fail", content: apiErrorMessage(e, "导入失败") });
      }
    }
  };

  /** 彻底删除（admin + 原因必填）：软删端点已下线，删除只有这一条路径。 */
  const purge = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => productsApi.purge(id, reason),
    onSuccess: (result) => {
      Toast.show({ icon: "success", content: `已删除 ${result.sku_code}` });
      setPurgeOpen(false);
      setPurgeReason("");
      qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "彻底删除失败") }),
  });

  const refresh = async () => {
    await list.refetch();
  };

  return (
    <div>
      <NavBar
        back={null}
        right={
          <span
            style={{ fontSize: 20, color: "#666" }}
            onClick={() => setPageActionsOpen(true)}
            role="button"
            aria-label="更多操作"
          >
            <MoreOutline />
          </span>
        }
      >
        商品管理
      </NavBar>
      <CapsuleTabs
        activeKey={statusFilter}
        onChange={(key) => {
          setStatusFilter(key);
          // 切筛选 = 换 queryKey：列表从头拉（并重算 hasNextPage）
          setStatusKey((n) => n + 1);
        }}
      >
        <CapsuleTabs.Tab title="全部" key="" />
        {PRODUCT_STATUS_FILTERS.map((f) => (
          <CapsuleTabs.Tab title={f.label} key={f.value} />
        ))}
      </CapsuleTabs>
      <div style={{ fontSize: 12, color: "#999", padding: "6px 14px" }}>
        共 {total} 条{statusFilter ? "（已按状态筛选）" : ""}
      </div>

      <PullToRefresh onRefresh={refresh}>
        {list.isLoading ? (
          <List mode="card">
            <List.Item>加载中…</List.Item>
          </List>
        ) : list.isError ? (
          <QueryError what="商品列表加载" error={list.error} onRetry={() => void list.refetch()} />
        ) : items.length === 0 ? (
          <List mode="card">
            <List.Item>
              {statusFilter ? "该状态下暂无商品" : "还没有商品，点右下角「+」新建（或右上角导入 CSV）"}
            </List.Item>
          </List>
        ) : (
          <>
            {items.map((p) => (
              <Card
                key={p.id}
                title={p.title}
                extra={<ProductStatusTag status={p.status} />}
                onClick={() => navigate(`/products/${p.id}`)}
                style={{ margin: "8px 12px", borderRadius: 8 }}
              >
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", fontSize: 14 }}>
                  <Tag fill="outline">{p.sku_code}</Tag>
                  <span style={{ color: "#ff6a00", fontWeight: 600 }}>{formatPrice(p.base_price)}</span>
                  <span style={{ color: "#999" }}>{stockStatusLabel(p.stock_status)}</span>
                </div>
                {p.active_job_error ? (
                  <div style={{ marginTop: 6, fontSize: 12, color: "#ff3141" }} className="pa-pre-wrap">
                    最近失败：{p.active_job_error}
                  </div>
                ) : null}
                <div style={{ display: "flex", gap: 8, marginTop: 10 }} onClick={(e) => e.stopPropagation()}>
                  <Button
                    size="mini"
                    color="primary"
                    fill="outline"
                    loading={generate.isPending && generate.variables === p.id}
                    disabled={!writable || p.status === "generating" || p.status === "waiting_approval"}
                    onClick={() => generate.mutate(p.id)}
                  >
                    生成
                  </Button>
                  <Button size="mini" disabled={!writable} onClick={() => setEditorTarget(p)}>
                    编辑
                  </Button>
                  <Button
                    size="mini"
                    fill="none"
                    onClick={() => {
                      setMoreTarget(p);
                      setMoreOpen(true);
                    }}
                  >
                    更多
                  </Button>
                </div>
              </Card>
            ))}
            <InfiniteScroll
              loadMore={async () => {
                await list.fetchNextPage();
              }}
              hasMore={Boolean(list.hasNextPage)}
            />
          </>
        )}
      </PullToRefresh>

      {writable ? (
        <FloatingBubble
          style={{
            "--initial-position-bottom": "76px",
            "--initial-position-right": "20px",
            "--edge-distance": "20px",
          }}
          onClick={() => setEditorTarget("new")}
        >
          <AddOutline fontSize={22} />
        </FloatingBubble>
      ) : null}

      <ProductEditorPopup
        target={editorTarget}
        onClose={() => setEditorTarget(null)}
        onSaved={() => qc.invalidateQueries({ queryKey: ["products"] })}
      />

      <ActionSheet
        visible={pageActionsOpen}
        actions={[
          { key: "import", text: "导入 CSV" },
          { key: "template", text: "下载导入模板" },
        ]}
        onAction={(action) => {
          setPageActionsOpen(false);
          if (action.key === "import") csvRef.current?.click();
          if (action.key === "template") {
            const blob = new Blob([`${CSV_TEMPLATE}\n`], { type: "text/csv;charset=utf-8" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = "products-template.csv";
            a.click();
            URL.revokeObjectURL(url);
          }
        }}
        cancelText="取消"
      />
      <input
        ref={csvRef}
        type="file"
        accept=".csv,text/csv"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void importCsv(file);
          e.target.value = "";
        }}
      />

      <ActionSheet
        visible={moreOpen}
        actions={[
          { key: "detail", text: "查看详情" },
          ...(isAdmin(role) ? [{ key: "purge", text: "彻底删除", danger: true }] : []),
        ]}
        onAction={(action) => {
          setMoreOpen(false);
          if (!moreTarget) return;
          if (action.key === "detail") navigate(`/products/${moreTarget.id}`);
          if (action.key === "purge") {
            setPurgeTarget(moreTarget);
            setPurgeReason("");
            setPurgeOpen(true);
          }
        }}
        cancelText="取消"
      />

      <Dialog
        visible={purgeOpen}
        title={`彻底删除 ${purgeTarget?.sku_code ?? ""}`}
        content={
          <div>
            <p style={{ marginTop: 0 }}>删除后内容与 OSS 对象会一并清理，且不可恢复。</p>
            <TextArea
              value={purgeReason}
              onChange={setPurgeReason}
              placeholder="请填写删除原因（必填，会写入审计）"
              rows={3}
            />
          </div>
        }
        actions={[
          [
            { key: "cancel", text: "取消" },
            { key: "ok", text: "确认删除", danger: true, disabled: purgeReason.trim().length === 0 },
          ],
        ]}
        onAction={(action) => {
          if (action.key === "ok" && purgeTarget) {
            purge.mutate({ id: purgeTarget.id, reason: purgeReason.trim() });
          } else {
            setPurgeOpen(false);
          }
        }}
        closeOnAction
      />

    </div>
  );
}
