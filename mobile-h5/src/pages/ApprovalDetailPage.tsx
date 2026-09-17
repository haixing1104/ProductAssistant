// 审批详情（移动版）：**通知深链的落地页**，手机端最关键的页面。
//
// 深链契约（与 backend `GET /approvals/deeplink` 一致）:
//   · 通知里的链接是 `…/approvals/{id}?ticket=…`；票据**只做定位，不授予审批权限**
//     （真正的授权在 `GET /approvals/{id}` 与 approve/reject 上，越权会直接 403）；
//   · 票据与审批单不匹配 / 已过期 → 明确告知"链接无效"，而不是默默显示另一个单。
// 两条与人直接相关的口径:
//   · **驳回必须写意见**（意见是 AI 重写的输入）；
//   · **带评估命中点放行必须写理由**（写 approval_overrides 审计，事后可追溯"谁在知情下放行"）。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Dialog,
  Divider,
  List,
  NavBar,
  NoticeBar,
  Popup,
  SafeArea,
  Tag,
  TextArea,
  Toast,
} from "antd-mobile";
import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { approvalsApi } from "@pa/core/api";
import {
  reasonMeta,
  scoreColor,
  snapshotBlocks,
  snapshotText,
  snapshotViolations,
} from "@pa/core/services/contentSnapshot";
import { apiErrorMessage } from "@pa/core/services/errors";
import { approvalStatusLabel, productStatusLabel } from "@pa/core/services/productMeta";
import { isAdmin, useAuthStore } from "@pa/core/store/authStore";

import BlockView from "../components/BlockView";
import DeliveryNotes from "../components/DeliveryNotes";
import QueryError from "../components/QueryError";
import { ApprovalStatusTag, ScoreTag } from "../components/StatusTag";
import { formatTime, toTagColor } from "../services/format";

export default function ApprovalDetailPage() {
  const { approvalId = "" } = useParams();
  const [params] = useSearchParams();
  const ticket = params.get("ticket");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const role = useAuthStore((s) => s.user?.role);
  const [action, setAction] = useState<"approve" | "reject" | null>(null);
  const [feedback, setFeedback] = useState("");

  const detail = useQuery({
    queryKey: ["approval", approvalId, ticket],
    enabled: approvalId.length > 0,
    retry: false,
    queryFn: async () => {
      if (ticket) {
        // 先校验票据（只定位）：不匹配就当场失败，避免"点通知跳到别的单"
        const resolved = await approvalsApi.deeplink(ticket);
        if (resolved.approval_id !== approvalId) {
          throw new Error("深链票据与审批单不匹配（链接可能已过期或被改写）");
        }
      }
      return approvalsApi.get(approvalId);
    },
  });
  const view = detail.data;
  const violations = snapshotViolations(view?.content_snapshot);
  const score = view?.content_snapshot?.evaluation_result?.score ?? null;
  const attempts = view?.content_snapshot?.evaluation_attempts ?? null;
  const reasonMetaInfo = reasonMeta(view?.content_snapshot?.reason ?? view?.snapshot_summary?.reason ?? undefined);

  const decide = useMutation({
    mutationFn: (v: { approve: boolean; feedback: string }) =>
      v.approve ? approvalsApi.approve(approvalId, v.feedback) : approvalsApi.reject(approvalId, v.feedback),
    onSuccess: (result, variables) => {
      if (result.resume_enqueued) {
        Toast.show({
          icon: "success",
          content: variables.approve ? "已批准：AI 将继续写入并上架" : "已驳回：AI 将按你的意见重写",
        });
      } else {
        // 定案了但恢复消息没投出去 → 必须让用户知道（管理员可在本页「补投」）
        Toast.show({ icon: "fail", content: "审批已定案，但恢复投递失败：请让管理员「补投」" });
      }
      setAction(null);
      setFeedback("");
      qc.invalidateQueries({ queryKey: ["approvals"] });
      qc.invalidateQueries({ queryKey: ["product"] });
      void detail.refetch();
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "审批失败") }),
  });

  const redrive = useMutation({
    mutationFn: () => approvalsApi.redrive(approvalId),
    onSuccess: (result) => {
      // 后端判定"是否真的需要补投"：对已跑完的线程 resume 是静默 no-op，假装成功会误导人
      const outcome = (result as { outcome?: string }).outcome;
      if (outcome === "enqueued") Toast.show({ icon: "success", content: "已补投：结论重新投递给引擎" });
      else if (outcome === "not_needed")
        Toast.show({ content: "无需补投：引擎已消费该结论（重复 resume 无副作用）" });
      else if (outcome === "throttled") Toast.show({ content: "节流窗口内已补投过，未重复投递" });
      else Toast.show({ icon: "fail", content: "补投失败：请检查 Redis 投递链路（job:approval）" });
      void detail.refetch();
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "补投失败") }),
  });

  /** 与后端同口径的禁用条件：驳回必填意见；**带命中点放行也必须写理由**。 */
  const needFeedback = action === "reject" || (action === "approve" && violations.length > 0);
  const canSubmit = !needFeedback || feedback.trim().length > 0;

  if (detail.isError) {
    return (
      <div>
        <NavBar onBack={() => navigate("/approvals")}>审批详情</NavBar>
        <QueryError
          what="审批详情加载"
          error={detail.error}
          onRetry={() => {
            // 深链失败时「重试」= 回列表（票据多半已过期，重试没意义）——文案已说明原因
            if (ticket) navigate("/approvals", { replace: true });
            else void detail.refetch();
          }}
        />
      </div>
    );
  }
  if (!view) {
    return (
      <div>
        <NavBar onBack={() => navigate("/approvals")}>审批详情</NavBar>
        <List mode="card">
          <List.Item>加载中…</List.Item>
        </List>
      </div>
    );
  }

  return (
    <div className="pa-page pa-page--actions">
      <NavBar
        onBack={() => navigate("/approvals")}
        right={<ApprovalStatusTag status={view.status} />}
      >
        {view.sku_code ?? "审批详情"}
      </NavBar>

      <List header="基本信息" mode="card">
        <List.Item extra={<span className="pa-pre-wrap">{view.product_title ?? "-"}</span>}>商品</List.Item>
        <List.Item extra={productStatusLabel(view.product_status)}>商品状态</List.Item>
        <List.Item extra={approvalStatusLabel(view.status)}>审批状态</List.Item>
        <List.Item
          extra={
            reasonMetaInfo ? (
              <Tag color={toTagColor(reasonMetaInfo.color)} fill="outline">
                {reasonMetaInfo.label}
              </Tag>
            ) : (
              (view.content_snapshot?.reason ?? view.snapshot_summary?.reason ?? "-")
            )
          }
        >
          转人工原因
        </List.Item>
        <List.Item
          extra={
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <ScoreTag score={score} />
              {attempts ? <span style={{ fontSize: 12, color: "#999" }}>第 {attempts} 次评估</span> : null}
            </div>
          }
        >
          评估分
        </List.Item>
        <List.Item extra={formatTime(view.created_at)}>创建时间</List.Item>
        <List.Item extra={view.resolved_at ? formatTime(view.resolved_at) : "-"}>定案时间</List.Item>
        <List.Item extra={view.approver_name ?? (view.approver_id ? "已停用用户" : "-")}>审批人</List.Item>
      </List>

      <List header="通知投递状态" mode="card">
        <List.Item>
          <div style={{ width: "100%" }}>
            <DeliveryNotes notes={view.notifications} />
            <div style={{ fontSize: 12, color: "#999", marginTop: 6 }}>
              投递失败（红）多为 webhook/凭据问题或对端不可用；`dlq` 是终态，需人工修配置后由补投守护或运维处理。
            </div>
          </div>
        </List.Item>
      </List>

      {violations.length > 0 ? (
        <div style={{ margin: "8px 12px" }}>
          <NoticeBar color={view.status === "approved" ? "default" : "error"} content={`评估命中点（${violations.length} 条）`} />
          <List mode="card" style={{ marginTop: 4 }}>
            {violations.map((item, i) => (
              <List.Item
                key={`${item.keyword ?? "hit"}-${i}`}
                extra={
                  item.severity ? (
                    <Tag color={item.severity === "low" ? "default" : "danger"} fill="outline">
                      {item.severity === "low" ? "低（提示）" : `${item.severity}（阻断）`}
                    </Tag>
                  ) : null
                }
              >
                <div className="pa-pre-wrap" style={{ fontSize: 13 }}>
                  {item.keyword ? <b>{item.keyword}</b> : null}
                  {item.keyword ? "：" : ""}
                  {item.reason ?? JSON.stringify(item)}
                </div>
              </List.Item>
            ))}
          </List>
        </div>
      ) : null}

      {view.redrive ? (
        <div style={{ margin: "8px 12px" }}>
          <NoticeBar
            color="info"
            content={`补投痕迹：共 ${view.redrive.count} 次（最近 ${formatTime(view.redrive.last_at)}，结果 ${view.redrive.last_outcome ?? "-"}）`}
          />
        </div>
      ) : null}
      {view.override ? (
        <div style={{ margin: "8px 12px" }}>
          <NoticeBar
            color="alert"
            content={`人工放行记录：${view.override.violation_count} 条命中点（${formatTime(view.override.at)}）理由：${view.override.reason || "（未填写）"}`}
          />
        </div>
      ) : null}

      <List header="AI 生成详情（待审图文）" mode="card">
        <List.Item>
          <div style={{ width: "100%" }}>
            <BlockView blocks={snapshotBlocks(view.content_snapshot)} />
            {snapshotText(view.content_snapshot).length === 0 ? (
              <div style={{ fontSize: 12, color: "#999", marginTop: 6 }}>
                （该单没有正文快照，可能来自历史数据）
              </div>
            ) : null}
          </div>
        </List.Item>
      </List>
      {view.feedback ? (
        <List header="审批意见" mode="card">
          <List.Item>
            <span className="pa-pre-wrap">{view.feedback}</span>
          </List.Item>
        </List>
      ) : null}

      {/* 底部固定操作栏：审批动作放在拇指区；危险动作（驳回）二次确认 */}
      <div
        style={{
          position: "fixed",
          bottom: 0,
          left: 0,
          right: 0,
          background: "#fff",
          padding: "8px 12px 0",
          boxShadow: "0 -1px 6px rgba(0,0,0,.06)",
          zIndex: 100,
        }}
      >
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {view.status === "pending" ? (
            <>
              <Button
                color="primary"
                style={{ flex: 1 }}
                onClick={() => {
                  setFeedback("");
                  setAction("approve");
                }}
              >
                批准
              </Button>
              <Button
                color="danger"
                fill="outline"
                style={{ flex: 1 }}
                onClick={() => {
                  setFeedback("");
                  setAction("reject");
                }}
              >
                驳回
              </Button>
            </>
          ) : (
            <div style={{ flex: 1, fontSize: 13, color: "#999" }}>
              该审批单已{approvalStatusLabel(view.status)}，无需再处理
            </div>
          )}
          {/* 补投：仅 admin，且只在该单"真卡住"（商品仍停在待审批）时露出 */}
          {isAdmin(role) && view.product_status === "waiting_approval" ? (
            <Button
              loading={redrive.isPending}
              onClick={() => {
                void Dialog.confirm({
                  title: `补投审批结论：${view.sku_code ?? ""}`,
                  content:
                    "仅用于「已定案但引擎没收到（商品仍停在待审批）」的情况；引擎已消费时后端会直接回「无需补投」，不会产生副作用。",
                  confirmText: "确认补投",
                  onConfirm: async () => {
                    await redrive.mutateAsync();
                  },
                });
              }}
            >
              补投{view.redrive?.count ? `（${view.redrive.count}）` : ""}
            </Button>
          ) : null}
        </div>
        <SafeArea position="bottom" />
      </div>

      {/* 审批动作 + 意见：底部弹层（手机键盘弹起时不会把正文挤没） */}
      <Popup
        visible={action !== null}
        onMaskClick={() => setAction(null)}
        position="bottom"
        bodyStyle={{ borderTopLeftRadius: 12, borderTopRightRadius: 12, padding: "12px 12px 0" }}
        destroyOnClose
      >
        <div style={{ paddingBottom: 12 }}>
          <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 8 }}>
            {action === "approve" ? "批准并继续写入" : "驳回并让 AI 重写"}
          </div>
          <TextArea
            rows={4}
            value={feedback}
            onChange={setFeedback}
            placeholder={
              action === "reject"
                ? "驳回意见（必填）：请写明具体问题与期望改法，它会作为 AI 重写的输入"
                : violations.length > 0
                  ? "放行理由（必填）：该内容存在评估命中点，理由会写入审批审计"
                  : "批准意见（可选）"
            }
          />
          {needFeedback && feedback.trim().length === 0 ? (
            <div style={{ fontSize: 12, color: "#ff3141", marginTop: 6 }}>
              {action === "reject" ? "驳回必须填写意见" : `存在 ${violations.length} 条评估命中点，放行必须填写理由`}
            </div>
          ) : null}
          <Divider style={{ margin: "12px 0 8px" }} />
          <Button
            block
            color={action === "reject" ? "danger" : "primary"}
            size="large"
            loading={decide.isPending}
            disabled={!canSubmit}
            onClick={() => {
              if (action) decide.mutate({ approve: action === "approve", feedback: feedback.trim() });
            }}
          >
            {action === "approve" ? "确认批准并继续写入" : "确认驳回并让 AI 重写"}
          </Button>
          <Button block fill="none" style={{ marginTop: 8 }} onClick={() => setAction(null)}>
            取消
          </Button>
          <SafeArea position="bottom" />
        </div>
      </Popup>

    </div>
  );
}
