// 审批中心（RN 版）—— 与 H5 的 `ApprovalsPage` 同口径：待处理 / 已批准 / 已驳回 + 卡片列表 + 触底加载。
//
// 表格 → 卡片的落法（10 列的审批表在手机上必须"结论前置"）:
//   卡片头 = 商品名 + 审批状态；卡身第一行 = **评估分（着色）+ 转人工原因短标签**（扫视锚点）；
//   第二行 = SKU / 商品状态 / 通知投递（点按展开）；卡底 = 时间与审批人。
// 两条与桌面端一致的关键口径（**不要"顺手"改掉**）:
//   1) 待处理卡片**不放「批准」按钮** —— 手机上误触代价极高（批准=直接上架），动作全部进详情页；
//   2) 已定案但**商品仍停在待审批**时给红字提示：这正是"引擎没收到结论"的现场信号（admin 可进详情补投）。
import { useState } from "react";
import { FlatList, RefreshControl, StyleSheet, Text, View } from "react-native";
import { useInfiniteQuery } from "@tanstack/react-query";

import Button from "../ui/Button";
import Card from "../ui/Card";
import { EmptyState, Loading, PreWrapText } from "../ui/List";
import { NavBar } from "../ui/NavBar";
import Tag from "../ui/Tag";
import Tabs from "../ui/Tabs";
import { colors, font, space } from "../ui/theme";
import DeliveryNotes from "../components/DeliveryNotes";
import QueryError from "../components/QueryError";
import { ApprovalStatusTag, ScoreTag } from "../components/StatusTag";
import { approvalsApi } from "@pa/core/api";
import { reasonMeta, reasonShortLabel } from "@pa/core/services/contentSnapshot";
import { formatTime, toTagColor } from "@pa/core/services/mobileFormat";
import { productStatusLabel } from "@pa/core/services/productMeta";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

type Tab = "pending" | "approved" | "rejected";
const PAGE_SIZE = 10;

export default function ApprovalsScreen({ onOpenDetail }: { onOpenDetail: (approvalId: string) => void }) {
  const role = useAuthStore((state) => state.user?.role);
  const [tab, setTab] = useState<Tab>("pending");

  const list = useInfiniteQuery({
    queryKey: ["approvals", tab],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => approvalsApi.list({ status: tab, offset: pageParam, limit: PAGE_SIZE }),
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, page) => sum + page.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  const items: Approval[] = list.data?.pages.flatMap((page) => page.items) ?? [];
  const total = list.data?.pages[0]?.total ?? 0;

  /** 列表里"卡住"的信号：已定案但商品仍停在待审批（引擎很可能没收到结论） */
  const looksStuck = (row: Approval) => row.status !== "pending" && row.product_status === "waiting_approval";

  const renderItem = (row: Approval) => {
    const reason = row.snapshot_summary?.reason ?? undefined;
    const meta = reasonMeta(reason);
    return (
      <Card
        title={row.product_title ?? row.sku_code ?? "（无标题）"}
        extra={<ApprovalStatusTag status={row.status} />}
        onPress={() => onOpenDetail(row.id)}
        style={styles.card}
        testID={`pa-approval-${row.id}`}
      >
        {/* 扫视锚点：评估分（着色）+ 转人工原因（短标签；长解释另起一行小字） */}
        <View style={styles.tagRow}>
          <ScoreTag score={row.snapshot_summary?.score ?? null} />
          {reason ? <Tag color={meta ? toTagColor(meta.color) : "default"}>{reasonShortLabel(reason)}</Tag> : null}
          <Tag>{row.sku_code ?? "-"}</Tag>
          <Tag>{productStatusLabel(row.product_status)}</Tag>
        </View>
        {meta ? <Text style={styles.dim}>{meta.label}</Text> : null}
        {row.snapshot_summary?.evaluation_attempts ? (
          <Text style={styles.dim}>第 {row.snapshot_summary.evaluation_attempts} 次评估</Text>
        ) : null}
        <View style={styles.notes}>
          <DeliveryNotes notes={row.notifications} compact />
        </View>
        <Text style={styles.dim}>
          {formatTime(row.created_at)}
          {row.approver_name ? ` · 审批人 ${row.approver_name}` : ""}
          {row.redrive?.count ? ` · 已补投 ${row.redrive.count} 次` : ""}
        </Text>
        {looksStuck(row) ? (
          <PreWrapText style={styles.stuck}>商品仍停在待审批：可能是引擎未收到结论，进详情可「补投」</PreWrapText>
        ) : null}
        <View style={styles.actions}>
          <Button
            size="small"
            variant={row.status === "pending" ? "primary" : "default"}
            fill="outline"
            onPress={() => onOpenDetail(row.id)}
            testID={`pa-approval-open-${row.id}`}
          >
            {row.status === "pending" ? "去处理" : "查看详情"}
          </Button>
        </View>
      </Card>
    );
  };

  return (
    <View style={styles.page}>
      <NavBar
        title="审批中心"
        back={null}
        right={<Text style={styles.role}>{role === "admin" ? "管理员" : "审核员"}</Text>}
      />
      <Tabs
        activeKey={tab}
        onChange={(key) => setTab(key as Tab)}
        items={[
          { key: "pending", title: tab === "pending" ? `待我处理（${total}）` : "待我处理" },
          { key: "approved", title: "已批准" },
          { key: "rejected", title: "已驳回" },
        ]}
        testID="pa-approval-tabs"
      />

      {list.isLoading ? (
        <Loading />
      ) : list.isError ? (
        <QueryError what="审批列表加载" error={list.error} onRetry={() => void list.refetch()} />
      ) : (
        <FlatList
          data={items}
          keyExtractor={(row) => row.id}
          renderItem={({ item }) => renderItem(item)}
          contentContainerStyle={styles.listContent}
          refreshControl={<RefreshControl refreshing={list.isRefetching} onRefresh={() => void list.refetch()} />}
          onEndReachedThreshold={0.4}
          onEndReached={() => {
            if (list.hasNextPage && !list.isFetchingNextPage) void list.fetchNextPage();
          }}
          ListEmptyComponent={<EmptyState title={tab === "pending" ? "没有待办审批 🎉" : "没有历史记录"} />}
          ListFooterComponent={
            <Text style={styles.footer}>{list.isFetchingNextPage ? "加载中…" : `共 ${total} 条`}</Text>
          }
          testID="pa-approval-list"
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bg },
  role: { fontSize: font.sm, color: colors.textSecondary },
  listContent: { paddingBottom: space.xl, paddingTop: space.sm },
  card: { marginHorizontal: space.md, marginBottom: space.sm, borderWidth: 1, borderColor: colors.border },
  tagRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    alignItems: "center",
    gap: space.sm,
    paddingHorizontal: space.md,
  },
  dim: { fontSize: font.xs, color: colors.textSecondary, marginTop: space.xs, paddingHorizontal: space.md },
  notes: { paddingHorizontal: space.md, marginTop: space.sm },
  stuck: { fontSize: font.xs, color: colors.danger, marginTop: space.xs, paddingHorizontal: space.md },
  actions: { flexDirection: "row", paddingHorizontal: space.md, marginTop: space.sm },
  footer: { textAlign: "center", color: colors.textSecondary, fontSize: font.sm, paddingVertical: space.md },
});

