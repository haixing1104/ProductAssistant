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
  Tooltip,
  Typography,
} from "antd";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { approvalsApi } from "../api";
import BlockRenderer from "../components/BlockRenderer";
import DeliverySummary from "../components/DeliverySummary";
import {
  reasonMeta,
  reasonShortLabel,
  scoreColor,
  snapshotBlocks,
  snapshotText,
  snapshotViolations,
} from "../services/contentSnapshot";
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
      // 后端会判定"是否真的需要补投"：不再一律回成功（对已跑完的线程 resume 是静默 no-op，
      // 假装成功会让人以为投出去了）。这里按 outcome 说真话。
      const outcome = (result as { outcome?: string }).outcome;
      if (outcome === "enqueued") message.success("已补投：审批结论重新投递给引擎");
      else if (outcome === "not_needed")
        message.info("无需补投：引擎已消费该结论（重复 resume 是无副作用的空操作）");
      else if (outcome === "throttled") message.warning("节流窗口内已补投过，未重复投递");
      else message.error("补投失败：请检查 Redis 投递链路（job:approval）");
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["product"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "补投失败")),
  });

  /** 补投前的二次确认：这是运维兜底动作，且"是否需要"由后端判定。 */
  const confirmRedrive = (row: Approval) => {
    Modal.confirm({
      title: `补投审批结论：${row.sku_code ?? ""}`,
      content:
        "仅用于「已定案但引擎没收到（商品仍停在待审批）」的情况；引擎已消费时后端会直接回「无需补投」，不会产生副作用。",
      okText: "确认补投",
      cancelText: "取消",
      onOk: () => redrive.mutateAsync(row.id),
    });
  };


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
  // 当前抽屉单据的「评估命中点」（真实快照用 violations；errors 是更早的形状，做读侧兼容）
  const viewViolations = snapshotViolations(view?.content_snapshot);
  const viewScore = view?.content_snapshot?.evaluation_result?.score ?? null;
  const viewAttempts = view?.content_snapshot?.evaluation_attempts ?? null;

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
            width: 120,
            ellipsis: true,
            render: (_: unknown, row: Approval) => {
              const reason = row.snapshot_summary?.reason ?? undefined;
              const meta = reasonMeta(reason);
              if (!meta) return reason ?? "-";
              // 短标签 + Tooltip：长文案（"高价商品（> ¥500），需人工放行"）直接铺在列里会把
              // 「评估分」压得看不见（2026-09 反馈），列表只给结论，解释放悬浮。
              return (
                <Tooltip title={meta.label}>
                  <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                    {reasonShortLabel(reason)}
                  </Tag>
                </Tooltip>
              );
            },
          },
          {
            title: "评估分",
            width: 104,
            render: (_: unknown, row: Approval) => {
              const score = row.snapshot_summary?.score ?? null;
              const attempts = row.snapshot_summary?.evaluation_attempts;
              return (
                <Space size={4}>
                  <Tag color={scoreColor(score)} style={{ marginInlineEnd: 0, minWidth: 44, textAlign: "center" }}>
                    {score ?? "-"}
                  </Tag>
                  {attempts ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{`第${attempts}次`}</Typography.Text> : null}
                </Space>
              );
            },
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
                        // 必须取详情：列表行不带 content_snapshot，而"带评估命中点时放行需写理由"
                        // 的判定依赖快照（否则前端无法预警，只能等后端 422）。
                        void openDetail(row.id);
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
                        void openDetail(row.id);
                        setAction("reject");
                        setFeedback("");
                      }}
                    >
                      驳回
                    </Button>
                  </>
                ) : null}
                {isAdmin(role) && row.product_status === "waiting_approval" ? (
                  // 只在"真卡住"（商品仍停在待审批 = 引擎很可能没收到）时露出补投：
                  // 已批准/已驳回且商品已推进的单，补投必然是空操作（2026-09 实测）。
                  <Button
                    size="small"
                    loading={redrive.isPending}
                    onClick={() => confirmRedrive(row)}
                    title="已定案但引擎未收到（商品仍停在待审批）时的手工补投"
                  >
                    补投
                    {row.redrive?.count ? `（${row.redrive.count}）` : ""}
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
              {/* 与后端同口径：驳回必须写意见；**带评估命中点时放行也必须写理由**（W6，
                  理由会写入 approval_overrides 审计，事后可追溯"谁在知情下放行"） */}
              <Button
                type="primary"
                danger={action === "reject"}
                loading={decide.isPending}
                disabled={(action === "reject" || viewViolations.length > 0) && feedback.trim().length === 0}
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
                <Space size={4}>
                  <Tag color={scoreColor(viewScore)} style={{ marginInlineEnd: 0, minWidth: 44, textAlign: "center" }}>
                    {viewScore ?? "-"}
                  </Tag>
                  {viewAttempts ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>{`第 ${viewAttempts} 次评估`}</Typography.Text>
                  ) : null}
                </Space>
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

            {viewViolations.length > 0 ? (
              <Alert
                type={view.status === "approved" ? "warning" : "error"}
                showIcon
                message={`评估命中点（${viewViolations.length} 条）`}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {viewViolations.map((item, i) => (
                      <li key={i}>
                        {item.keyword ? <Typography.Text strong>{item.keyword}</Typography.Text> : null}
                        {item.keyword ? "：" : ""}
                        {item.reason ?? JSON.stringify(item)}
                        {item.severity ? (
                          <Tag
                            color={item.severity === "low" ? "default" : "red"}
                            style={{ marginInlineStart: 6 }}
                          >
                            {item.severity === "low" ? "低（提示）" : `${item.severity}（阻断）`}
                          </Tag>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                }
              />
            ) : null}

            {view.redrive ? (
              <Alert
                type="info"
                showIcon
                message={`补投痕迹：共 ${view.redrive.count} 次（最近 ${view.redrive.last_at ?? "-"}）`}
                description={`最近结果：${view.redrive.last_outcome ?? "-"}（enqueued=已重新投递；not_needed=引擎已消费；throttled=节流窗口内已投过）`}
              />
            ) : null}

            {view.override ? (
              <Alert
                type="warning"
                showIcon
                message={`人工放行记录：${view.override.violation_count} 条命中点（${view.override.at ?? "-"}）`}
                description={`放行理由：${view.override.reason || "（未填写）"}`}
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
                    : viewViolations.length > 0
                      ? "放行理由（必填）：该内容存在评估命中点，理由会写入审批审计"
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


