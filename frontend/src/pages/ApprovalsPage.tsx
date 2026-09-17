// 审批中心：待我处理 / 已处理 两个 tab；详情抽屉（图文快照 + 评估判据 + 通知状态）；批准/驳回；admin 补投。
//
// 与 backend 契约对齐的要点:
//   · 列表走 `GET /approvals?status=…`（PA 在 P7 扩展）：**只回 pending 的话，审批人点完按钮
//     那张单就从界面上消失了** —— 批过什么、驳回原因是什么全都查不到。已处理 tab 就是补这个缺口；
//   · 深层链接 `/approvals/:approvalId?ticket=…`：通知里的链接落到这里。
//     票据先经 `GET /approvals/deeplink` 校验（**只做定位，不授予审批权限**），
//     未登录时由 AuthGuard 先引导登录，登录后回到本页；
//   · 每项都带 `notifications`（渠道/状态/重试次数/失败原因）：通知没发出去是审批卡住的头号原因，
//     必须能在界面上直接看出来；
//   · 驳回时**意见必填**（意见会作为 ai-engine Reflection 重写的输入，空意见等于让 AI 瞎猜）。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Descriptions,
  Divider,
  Empty,
  Input,
  message,
  Modal,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { approvalsApi } from "../api";
import BlockRenderer from "../components/BlockRenderer";
import DeliverySummary from "../components/DeliverySummary";
import { reasonMeta, snapshotBlocks, snapshotText } from "../services/contentSnapshot";
import { apiErrorMessage } from "../services/errors";
import { approvalStatusLabel, productStatusLabel } from "../services/productMeta";
import { isAdmin, useAuthStore } from "../store/authStore";
import type { Approval } from "../types/api";

type Tab = "pending" | "approved" | "rejected";

export default function ApprovalsPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { approvalId } = useParams();
  const [params, setParams] = useSearchParams();
  const role = useAuthStore((s) => s.user?.role);
  const [tab, setTab] = useState<Tab>("pending");
  const [page, setPage] = useState(1);
  const [view, setView] = useState<Approval | null>(null);
  const [feedback, setFeedback] = useState("");
  const [action, setAction] = useState<"approve" | "reject" | null>(null);
  const [deeplinkState, setDeeplinkState] = useState<"idle" | "checking" | "ok" | "bad">("idle");
  const ticket = params.get("ticket");
  const pageSize = 10;

  const list = useQuery({
    queryKey: ["approvals", tab, page],
    queryFn: () => approvalsApi.list({ status: tab, offset: (page - 1) * pageSize, limit: pageSize }),
  });

  // 深链：先校验票据（只定位），再看单
  useEffect(() => {
    if (!approvalId) {
      setDeeplinkState("idle");
      return;
    }
    let active = true;
    const load = async () => {
      setDeeplinkState("checking");
      try {
        if (ticket) {
          const resolved = await approvalsApi.deeplink(ticket);
          if (!active) return;
          if (resolved.approval_id !== approvalId) {
            setDeeplinkState("bad");
            return;
          }
        }
        const detail = await approvalsApi.get(approvalId);
        if (!active) return;
        setView(detail);
        setDeeplinkState("ok");
      } catch (e) {
        if (!active) return;
        setDeeplinkState("bad");
        message.error(apiErrorMessage(e, "审批链接无效或已过期"));
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [approvalId, ticket]);

  const closeDeeplink = () => {
    setView(null);
    setDeeplinkState("idle");
    params.delete("ticket");
    setParams(params, { replace: true });
    navigate("/approvals", { replace: true });
  };

  const decide = useMutation({
    mutationFn: (v: { id: string; approve: boolean; feedback: string }) =>
      v.approve ? approvalsApi.approve(v.id, v.feedback) : approvalsApi.reject(v.id, v.feedback),
    onSuccess: (result, variables) => {
      if (result.resume_enqueued) {
        message.success(variables.approve ? "已批准：已通知 AI 继续写入" : "已驳回：AI 将按意见重写");
      } else {
        message.warning("审批已定案，但恢复消息投递失败：管理员可在本页「补投」或等待补投守护重试");
      }
      setAction(null);
      setFeedback("");
      setView(null);
      closeDeeplink();
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["product"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "审批失败")),
  });

  const redrive = useMutation({
    mutationFn: (id: string) => approvalsApi.redrive(id),
    onSuccess: (result) => {
      if (result.resume_enqueued) message.success("补投成功：AI 已收到该审批结论");
      else message.warning("补投仍失败：请检查 Redis 投递链路（job:approval）");
    },
    onError: (e) => message.error(apiErrorMessage(e, "补投失败")),
  });

  const openDetail = async (id: string) => {
    try {
      setView(await approvalsApi.get(id));
    } catch (e) {
      message.error(apiErrorMessage(e, "无法加载审批详情"));
    }
  };

  const items: Approval[] = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const pendingCount = tab === "pending" ? total : undefined;

  return (
    <div>
      <Space style={{ marginBottom: 12, justifyContent: "space-between", width: "100%" }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          审批中心
        </Typography.Title>
        <Typography.Text type="secondary">
          生成与把关分离：operator 不能审批；驳回意见会作为 AI 重写的输入，请写具体
        </Typography.Text>
      </Space>

      {deeplinkState === "bad" ? (
        <Alert
          type="error"
          showIcon
          closable
          onClose={closeDeeplink}
          message="审批链接无效或已过期"
          description="请从审批列表中打开该单（票据有效期默认 7 天，过期后通知里的链接会失效）。"
          style={{ marginBottom: 12 }}
        />
      ) : null}

      <Tabs
        activeKey={tab}
        onChange={(key) => {
          setTab(key as Tab);
          setPage(1);
        }}
        items={[
          { key: "pending", label: pendingCount === undefined ? "待我处理" : `待我处理（${pendingCount}）` },
          { key: "approved", label: "已批准" },
          { key: "rejected", label: "已驳回" },
        ]}
      />

      <Table<Approval>
        rowKey="id"
        loading={list.isLoading}
        dataSource={items}
        locale={{ emptyText: <Empty description={tab === "pending" ? "没有待办审批" : "没有历史记录"} /> }}
        pagination={{ current: page, pageSize, total, onChange: setPage, showSizeChanger: false }}
        columns={[
          { title: "商品", dataIndex: "product_title", ellipsis: true },
          { title: "SKU", dataIndex: "sku_code" },
          {
            title: "商品状态",
            dataIndex: "product_status",
            render: (v: string) => productStatusLabel(v),
          },
          {
            title: "转人工原因",
            render: (_: unknown, row: Approval) => {
              const meta = reasonMeta(row.snapshot_summary?.reason ?? undefined);
              return meta ? <Tag color={meta.color}>{meta.label}</Tag> : (row.snapshot_summary?.reason ?? "-");
            },
          },
          {
            title: "评估分",
            render: (_: unknown, row: Approval) => row.snapshot_summary?.score ?? "-",
          },
          {
            title: "状态",
            dataIndex: "status",
            render: (v: string) => (
              <Tag color={v === "approved" ? "green" : v === "rejected" ? "red" : "gold"}>
                {approvalStatusLabel(v)}
              </Tag>
            ),
          },
          {
            title: "通知",
            render: (_: unknown, row: Approval) => <DeliverySummary notes={row.notifications} compact />,
          },
          {
            title: "审批人",
            dataIndex: "approver_name",
            render: (v: string | null | undefined, row: Approval) =>
              v ?? (row.approver_id ? "已停用用户" : "-"),
          },
          {
            title: "创建时间",
            dataIndex: "created_at",
            render: (v: string | null) => v ?? "-",
          },
          {
            title: "操作",
            fixed: "right" as const,
            render: (_: unknown, row: Approval) => (
              <Space>
                <Button size="small" onClick={() => void openDetail(row.id)}>
                  详情
                </Button>
                {row.status === "pending" ? (
                  <>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() => {
                        setView(row);
                        setAction("approve");
                        setFeedback("");
                      }}
                    >
                      批准
                    </Button>
                    <Button
                      size="small"
                      danger
                      onClick={() => {
                        setView(row);
                        setAction("reject");
                        setFeedback("");
                      }}
                    >
                      驳回
                    </Button>
                  </>
                ) : null}
                {isAdmin(role) && row.status !== "pending" ? (
                  <Button
                    size="small"
                    loading={redrive.isPending}
                    onClick={() => redrive.mutate(row.id)}
                    title="已定案但 AI 未收到时的手工补投"
                  >
                    补投
                  </Button>
                ) : null}
              </Space>
            ),
          },
        ]}
      />

      <Modal
        open={view !== null}
        title={view ? `审批详情：${view.sku_code ?? ""}` : "审批详情"}
        width={860}
        onCancel={() => {
          setView(null);
          setAction(null);
        }}
        footer={
          view?.status === "pending" && action ? (
            <Space>
              <Button
                onClick={() => {
                  setView(null);
                  setAction(null);
                }}
              >
                取消
              </Button>
              <Button
                type="primary"
                danger={action === "reject"}
                loading={decide.isPending}
                disabled={action === "reject" && feedback.trim().length === 0}
                onClick={() => decide.mutate({ id: view.id, approve: action === "approve", feedback })}
              >
                {action === "approve" ? "确认批准并继续写入" : "确认驳回并让 AI 重写"}
              </Button>
            </Space>
          ) : null
        }
        destroyOnHidden
      >
        {view ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions column={2} size="small">
              <Descriptions.Item label="商品">{view.product_title ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="SKU">{view.sku_code ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="商品状态">{productStatusLabel(view.product_status)}</Descriptions.Item>
              <Descriptions.Item label="审批状态">{approvalStatusLabel(view.status)}</Descriptions.Item>
              <Descriptions.Item label="转人工原因">
                {reasonMeta(view.content_snapshot?.reason)?.label ?? view.content_snapshot?.reason ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label="评估分">
                {view.content_snapshot?.evaluation_result?.score ?? "-"}
                {view.content_snapshot?.evaluation_attempts
                  ? `（第 ${view.content_snapshot.evaluation_attempts} 次评估）`
                  : ""}
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">{view.created_at ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="定案时间">{view.resolved_at ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="审批人">{view.approver_name ?? (view.approver_id ? "已停用用户" : "-")}</Descriptions.Item>
              <Descriptions.Item label="深链有效期">{view.expire_at ?? "-"}</Descriptions.Item>
            </Descriptions>

            <div>
              <Typography.Text strong>通知投递状态</Typography.Text>
              <div style={{ marginTop: 6 }}>
                <DeliverySummary notes={view.notifications} />
              </div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                投递失败（红）多为 webhook/凭据配置问题或对端不可用；`dlq` 是终态，需人工修配置后由补投守护或运维处理。
              </Typography.Text>
            </div>

            {view.content_snapshot?.evaluation_result?.errors &&
            Array.isArray(view.content_snapshot.evaluation_result.errors) &&
            view.content_snapshot.evaluation_result.errors.length > 0 ? (
              <Alert
                type="warning"
                showIcon
                message="评估命中的问题点（AI 自检判据）"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {(view.content_snapshot.evaluation_result.errors as unknown[]).map((item, i) => (
                      <li key={i}>{typeof item === "string" ? item : JSON.stringify(item)}</li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            <Divider style={{ margin: "4px 0" }} />
            <div>
              <Typography.Text strong>AI 生成详情（待审图文）</Typography.Text>
              <div style={{ marginTop: 6 }}>
                <BlockRenderer blocks={snapshotBlocks(view.content_snapshot)} maxHeight={420} bordered />
              </div>
              {snapshotText(view.content_snapshot).length === 0 ? (
                <Typography.Text type="secondary">（该单没有正文快照，可能来自历史数据）</Typography.Text>
              ) : null}
            </div>

            {view.feedback ? <Alert type="info" showIcon message={`审批意见：${view.feedback}`} /> : null}

            {action ? (
              <Input.TextArea
                rows={3}
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                placeholder={
                  action === "reject"
                    ? "驳回意见（必填）：请写明具体问题与期望改法，它会作为 AI 重写的输入"
                    : "批准意见（可选）"
                }
              />
            ) : null}
          </Space>
        ) : null}
      </Modal>
    </div>
  );
}


