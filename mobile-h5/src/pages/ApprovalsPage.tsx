// 审批中心（移动版）：待处理 / 已批准 / 已驳回 + 卡片列表 + 触底加载。
//
// 表格 → 卡片的落法（10 列的审批表在手机上必须"结论前置"）:
//   卡片头 = 商品名 + 审批状态；卡身第一行 = **评估分（着色）+ 转人工原因短标签**（扫视锚点）；
//   第二行 = SKU / 商品状态 / 通知投递（点按看失败原因）；卡底 = 时间与审批人。
// 两条与桌面端一致的关键口径:
//   1) 待处理卡片**不放「批准」按钮** —— 手机上误触代价极高（批准=直接上架），
//      动作全部进详情页（底部固定操作栏 + 必填理由校验）；
//   2) 已定案但**商品仍停在待审批**时给红色提示：这正是「引擎没收到结论」的现场信号，
//      管理员进详情页可「补投」（桌面放在列表是因为有鼠标精度；手机上改为提示 + 进详情）。
import { useInfiniteQuery } from "@tanstack/react-query";
import { Button, Card, InfiniteScroll, List, NavBar, PullToRefresh, Tabs, Tag } from "antd-mobile";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { approvalsApi } from "@pa/core/api";
import { reasonMeta, reasonShortLabel } from "@pa/core/services/contentSnapshot";
import { productStatusLabel } from "@pa/core/services/productMeta";
import { useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

import DeliveryNotes from "../components/DeliveryNotes";
import QueryError from "../components/QueryError";
import { ApprovalStatusTag, ScoreTag } from "../components/StatusTag";
import { formatTime, toTagColor } from "@pa/core/services/mobileFormat";

type Tab = "pending" | "approved" | "rejected";
const PAGE_SIZE = 10;

export default function ApprovalsPage() {
  const navigate = useNavigate();
  const role = useAuthStore((s) => s.user?.role);
  const [tab, setTab] = useState<Tab>("pending");

  const list = useInfiniteQuery({
    queryKey: ["approvals", tab],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => approvalsApi.list({ status: tab, offset: pageParam, limit: PAGE_SIZE }),
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, p) => n + p.items.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  const items: Approval[] = list.data?.pages.flatMap((p) => p.items) ?? [];
  const total = list.data?.pages[0]?.total ?? 0;
  const pendingCount = tab === "pending" ? total : undefined;

  /** 列表里"卡住"的信号：已定案但商品仍停在待审批（引擎很可能没收到结论） */
  const looksStuck = (row: Approval) => row.status !== "pending" && row.product_status === "waiting_approval";

  return (
    <div>
      <NavBar
        back={null}
        right={<span style={{ fontSize: 13, color: "#999" }}>{role === "admin" ? "管理员" : "审核员"}</span>}
      >
        审批中心
      </NavBar>
      <Tabs activeKey={tab} onChange={(key) => setTab(key as Tab)}>
        <Tabs.Tab title={`待我处理${pendingCount === undefined ? "" : `（${pendingCount}）`}`} key="pending" />
        <Tabs.Tab title="已批准" key="approved" />
        <Tabs.Tab title="已驳回" key="rejected" />
      </Tabs>

      <PullToRefresh
        onRefresh={async () => {
          await list.refetch();
        }}
      >
        {list.isLoading ? (
          <List mode="card">
            <List.Item>加载中…</List.Item>
          </List>
        ) : list.isError ? (
          <QueryError what="审批列表加载" error={list.error} onRetry={() => void list.refetch()} />
        ) : items.length === 0 ? (
          <List mode="card">
            <List.Item>{tab === "pending" ? "没有待办审批 🎉" : "没有历史记录"}</List.Item>
          </List>
        ) : (
          <>
            {items.map((row) => {
              const reason = row.snapshot_summary?.reason ?? undefined;
              const meta = reasonMeta(reason);
              return (
                <Card
                  key={row.id}
                  title={row.product_title ?? row.sku_code ?? "（无标题）"}
                  extra={<ApprovalStatusTag status={row.status} />}
                  style={{ margin: "8px 12px", borderRadius: 8 }}
                  onClick={() => navigate(`/approvals/${row.id}`)}
                >
                  {/* 扫视锚点：评估分（着色）+ 转人工原因（短标签；长解释另起一行小字） */}
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <ScoreTag score={row.snapshot_summary?.score ?? null} />
                    {reason ? (
                      <Tag color={meta ? toTagColor(meta.color) : "default"} fill="outline">
                        {reasonShortLabel(reason)}
                      </Tag>
                    ) : null}
                    <Tag fill="outline">{row.sku_code ?? "-"}</Tag>
                    <Tag fill="outline">{productStatusLabel(row.product_status)}</Tag>
                  </div>
                  {meta ? <div style={{ fontSize: 12, color: "#999", marginTop: 4 }}>{meta.label}</div> : null}
                  {row.snapshot_summary?.evaluation_attempts ? (
                    <div style={{ fontSize: 12, color: "#999", marginTop: 4 }}>
                      第 {row.snapshot_summary.evaluation_attempts} 次评估
                    </div>
                  ) : null}
                  <div style={{ marginTop: 8 }}>
                    <DeliveryNotes notes={row.notifications} compact />
                  </div>
                  <div style={{ fontSize: 12, color: "#999", marginTop: 6 }}>
                    {formatTime(row.created_at)}
                    {row.approver_name ? ` · 审批人 ${row.approver_name}` : ""}
                    {row.redrive?.count ? ` · 已补投 ${row.redrive.count} 次` : ""}
                  </div>
                  {looksStuck(row) ? (
                    <div style={{ fontSize: 12, color: "#ff3141", marginTop: 6 }}>
                      商品仍停在待审批：可能是引擎未收到结论，进详情可「补投」
                    </div>
                  ) : null}
                  <div style={{ marginTop: 10 }} onClick={(e) => e.stopPropagation()}>
                    <Button
                      size="mini"
                      color={row.status === "pending" ? "primary" : "default"}
                      fill="outline"
                      onClick={() => navigate(`/approvals/${row.id}`)}
                    >
                      {row.status === "pending" ? "去处理" : "查看详情"}
                    </Button>
                  </div>
                </Card>
              );
            })}
            <InfiniteScroll
              loadMore={async () => {
                await list.fetchNextPage();
              }}
              hasMore={Boolean(list.hasNextPage)}
            />
          </>
        )}
      </PullToRefresh>
    </div>
  );
}
