// 审批详情（RN 版）—— 与 H5 的 `ApprovalDetailPage` 同口径：**通知深链的落地页**，手机端最关键的页面。
//
// 深链契约（与 backend `GET /approvals/deeplink` 一致）:
//   · 通知里的链接是 `…/approvals/{id}?ticket=…`；票据**只做定位，不授予审批权限**
//     （真正的授权在 `GET /approvals/{id}` 与 approve/reject 上，越权会直接 403）；
//   · 票据与审批单不匹配 / 已过期 → 明确告知"链接无效"，**而不是默默显示另一个单**。
// 两条与人直接相关的口径（与后端 422 同口径，别"放宽"）:
//   · **驳回必须写意见**（意见是 AI 重写的输入）；
//   · **带评估命中点放行必须写理由**（写 approval_overrides 审计，事后可追溯"谁在知情下放行"）。
import { useState } from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import BlockView from "../components/BlockView";
import DeliveryNotes from "../components/DeliveryNotes";
import QueryError from "../components/QueryError";
import { ApprovalStatusTag, ScoreTag } from "../components/StatusTag";
import Button from "../ui/Button";
import { LabeledTextArea } from "../ui/Field";
import { ListRow, ListSection, Loading, PreWrapText } from "../ui/List";
import { NavBar, NoticeBar } from "../ui/NavBar";
import { ActionSheet, Sheet } from "../ui/Sheet";
import { colors, font, space } from "../ui/theme";
import Tag from "../ui/Tag";
import { Dialog, Toast } from "../ui/feedback";
import { approvalsApi } from "@pa/core/api";
import { reasonMeta, scoreColor, snapshotBlocks, snapshotText, snapshotViolations } from "@pa/core/services/contentSnapshot";
import { apiErrorMessage } from "@pa/core/services/errors";
import { approvalStatusLabel, productStatusLabel } from "@pa/core/services/productMeta";
import { formatTime, toTagColor } from "@pa/core/services/mobileFormat";
import { isAdmin, useAuthStore } from "@pa/core/store/authStore";
import type { Approval } from "@pa/core/types/api";

interface Props {
  approvalId: string;
  /** 通知深链里的票据（可空：从列表点进来就没有） */
  ticket?: string | null;
  onBack: () => void;
}

/** 审批详情（通知深链落地页）：先校验票据定位单据，再展示复盘并给出「批准/驳回」。 */
export default function ApprovalDetailScreen({ approvalId, ticket, onBack }: Props) {
  const qc = useQueryClient();
  const role = useAuthStore((state) => state.user?.role);
  const [action, setAction] = useState<"approve" | "reject" | null>(null);
  const [feedback, setFeedback] = useState("");
  const [headerActions, setHeaderActions] = useState(false);

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
  const view: Approval | undefined = detail.data;
  const violations = snapshotViolations(view?.content_snapshot);
  const score = view?.content_snapshot?.evaluation_result?.score ?? null;
  const attempts = view?.content_snapshot?.evaluation_attempts ?? null;
  const reasonInfo = reasonMeta(view?.content_snapshot?.reason ?? view?.snapshot_summary?.reason ?? undefined);

  const decide = useMutation({
    mutationFn: (input: { approve: boolean; feedback: string }) =>
      input.approve
        ? approvalsApi.approve(approvalId, input.feedback)
        : approvalsApi.reject(approvalId, input.feedback),
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
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "审批失败") }),
  });

  const redrive = useMutation({
    mutationFn: () => approvalsApi.redrive(approvalId),
    onSuccess: (result) => {
      // 后端判定"是否真的需要补投"：对已跑完的线程 resume 是静默 no-op，假装成功会误导人
      const outcome = (result as { outcome?: string }).outcome;
      if (outcome === "enqueued") Toast.show({ icon: "success", content: "已补投：结论重新投递给引擎" });
      else if (outcome === "not_needed") Toast.show("无需补投：引擎已消费该结论（重复 resume 无副作用）");
      else if (outcome === "throttled") Toast.show("节流窗口内已补投过，未重复投递");
      else Toast.show({ icon: "fail", content: "补投失败：请检查 Redis 投递链路（job:approval）" });
      void detail.refetch();
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "补投失败") }),
  });

  /** 与后端同口径的禁用条件：驳回必填意见；**带命中点放行也必须写理由**。 */
  const needFeedback = action === "reject" || (action === "approve" && violations.length > 0);
  const canSubmit = !needFeedback || feedback.trim().length > 0;

  if (detail.isError) {
    return (
      <View style={styles.page}>
        <NavBar title="审批详情" back={onBack} />
        <QueryError what="审批详情加载" error={detail.error} onRetry={onBack} />
      </View>
    );
  }
  if (!view) {
    return (
      <View style={styles.page}>
        <NavBar title="审批详情" back={onBack} />
        <Loading />
      </View>
    );
  }

  return (
    <View style={styles.page}>
      <NavBar
        title={view.sku_code ?? "审批详情"}
        back={onBack}
        right={
          <View style={styles.navRight}>
            <ApprovalStatusTag status={view.status} />
            {isAdmin(role) ? (
              <Button size="mini" fill="none" onPress={() => setHeaderActions(true)} testID="pa-approval-more">
                更多
              </Button>
            ) : null}
          </View>
        }
      />

      <ScrollView contentContainerStyle={styles.scrollBody} testID="pa-approval-detail">
        <ListSection header="基本信息">
          <ListRow label="商品" extra={<PreWrapText>{view.product_title ?? "-"}</PreWrapText>} />
          <ListRow label="商品状态" extra={productStatusLabel(view.product_status)} />
          <ListRow label="审批状态" extra={approvalStatusLabel(view.status)} />
          <ListRow
            label="转人工原因"
            extra={
              reasonInfo ? (
                <Tag color={toTagColor(reasonInfo.color)}>{reasonInfo.label}</Tag>
              ) : (
                (view.content_snapshot?.reason ?? view.snapshot_summary?.reason ?? "-")
              )
            }
          />
          <ListRow
            label="评估分"
            extra={
              <View style={styles.scoreWrap}>
                <ScoreTag score={score} />
                {attempts ? <Text style={styles.dim}>第 {attempts} 次评估</Text> : null}
              </View>
            }
          />
          <ListRow label="创建时间" extra={formatTime(view.created_at)} />
          <ListRow label="定案时间" extra={view.resolved_at ? formatTime(view.resolved_at) : "-"} />
          <ListRow
            label="审批人"
            extra={view.approver_name ?? (view.approver_id ? "已停用用户" : "-")}
          />
        </ListSection>

        <ListSection header="通知投递状态">
          <ListRow>
            <DeliveryNotes notes={view.notifications} />
          </ListRow>
        </ListSection>

        {violations.length > 0 ? (
          <ListSection header={`评估命中点（${violations.length} 条）`}>
            {violations.map((item, index) => (
              <ListRow
                key={`${item.keyword ?? "hit"}-${index}`}
                extra={
                  item.severity ? (
                    <Tag color={item.severity === "low" ? "default" : "danger"}>
                      {item.severity === "low" ? "低（提示）" : `${item.severity}（阻断）`}
                    </Tag>
                  ) : null
                }
                description={
                  <PreWrapText style={styles.hitText}>
                    {item.keyword ? `${item.keyword}：` : ""}
                    {item.reason ?? JSON.stringify(item)}
                  </PreWrapText>
                }
              />
            ))}
          </ListSection>
        ) : null}


        {view.redrive ? (
          <View style={styles.noticeWrap}>
            <NoticeBar
              color="info"
              content={`补投痕迹：共 ${view.redrive.count} 次（最近 ${formatTime(view.redrive.last_at)}，结果 ${view.redrive.last_outcome ?? "-"}）`}
            />
          </View>
        ) : null}
        {view.override ? (
          <View style={styles.noticeWrap}>
            <NoticeBar
              color="alert"
              content={`人工放行记录：${view.override.violation_count} 条命中点（${formatTime(view.override.at)}）理由：${view.override.reason || "（未填写）"}`}
            />
          </View>
        ) : null}

        <ListSection header="AI 生成详情（待审图文）">
          <ListRow>
            <View style={styles.blockWrap}>
              <BlockView blocks={snapshotBlocks(view.content_snapshot)} />
              {snapshotText(view.content_snapshot).length === 0 ? (
                <Text style={styles.dim}>（该单没有正文快照，可能来自历史数据）</Text>
              ) : null}
            </View>
          </ListRow>
        </ListSection>

        {view.feedback ? (
          <ListSection header="审批意见">
            <ListRow>
              <PreWrapText style={styles.feedbackText}>{view.feedback}</PreWrapText>
            </ListRow>
          </ListSection>
        ) : null}
      </ScrollView>

      {/* 底部固定操作栏：审批动作放在拇指区 */}
      <View style={styles.actionBar}>
        {view.status === "pending" ? (
          <>
            <Button
              variant="primary"
              size="large"
              style={styles.flex}
              onPress={() => {
                setFeedback("");
                setAction("approve");
              }}
              testID="pa-approve"
            >
              批准
            </Button>
            <Button
              variant="danger"
              fill="outline"
              size="large"
              style={styles.flex}
              onPress={() => {
                setFeedback("");
                setAction("reject");
              }}
              testID="pa-reject"
            >
              驳回
            </Button>
          </>
        ) : (
          <Text style={styles.settledNote}>
            该审批单已{approvalStatusLabel(view.status)}，无需再处理
          </Text>
        )}
      </View>

      {/* admin 的补投入口（低频 + 危险 → 收进「更多」）：只在该单"真卡住"时露出 */}
      <ActionSheet
        visible={headerActions}
        onClose={() => setHeaderActions(false)}
        actions={[
          {
            key: "redrive",
            text: `补投${view.redrive?.count ? `（已 ${view.redrive.count} 次）` : ""}`,
            disabled: view.product_status !== "waiting_approval",
          },
        ]}
        onAction={(item) => {
          setHeaderActions(false);
          if (item.key !== "redrive") return;
          void Dialog.confirm({
            title: `补投审批结论：${view.sku_code ?? ""}`,
            content:
              "仅用于「已定案但引擎没收到（商品仍停在待审批）」的情况；引擎已消费时后端会直接回「无需补投」，不会产生副作用。",
            confirmText: "确认补投",
            onConfirm: () => redrive.mutate(),
          });
        }}
      />


      {/* 审批动作 + 意见：底部弹层（手机键盘弹起时不会把正文挤没） */}
      <Sheet visible={action !== null} onClose={() => setAction(null)} testID="pa-decide-sheet">
        <View style={styles.sheetBody}>
          <Text style={styles.sheetTitle}>
            {action === "approve" ? "批准并继续写入" : "驳回并让 AI 重写"}
          </Text>
          <LabeledTextArea
            label={action === "reject" ? "驳回意见（必填）" : needFeedback ? "放行理由（必填）" : "批准意见（可选）"}
            value={feedback}
            onChangeText={setFeedback}
            rows={4}
            placeholder={
              action === "reject"
                ? "请写明具体问题与期望改法，它会作为 AI 重写的输入"
                : violations.length > 0
                  ? "该内容存在评估命中点，理由会写入审批审计"
                  : "可留空"
            }
            error={
              needFeedback && feedback.trim().length === 0
                ? action === "reject"
                  ? "驳回必须填写意见"
                  : `存在 ${violations.length} 条评估命中点，放行必须填写理由`
                : null
            }
            testID="pa-feedback"
          />
          <Button
            block
            variant={action === "reject" ? "danger" : "primary"}
            size="large"
            loading={decide.isPending}
            disabled={!canSubmit}
            onPress={() => {
              if (action) decide.mutate({ approve: action === "approve", feedback: feedback.trim() });
            }}
            testID="pa-decide-submit"
          >
            {action === "approve" ? "确认批准并继续写入" : "确认驳回并让 AI 重写"}
          </Button>
          <Button block fill="none" onPress={() => setAction(null)} style={styles.sheetCancel}>
            取消
          </Button>
        </View>
      </Sheet>
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bg },
  navRight: { flexDirection: "row", alignItems: "center", gap: space.xs },
  scrollBody: { paddingBottom: 96 },
  scoreWrap: { flexDirection: "row", alignItems: "center", gap: space.sm },
  dim: { fontSize: font.xs, color: colors.textSecondary },
  hitText: { fontSize: font.sm, color: colors.text, lineHeight: 20 },
  noticeWrap: { marginHorizontal: space.md },
  blockWrap: { width: "100%" },
  feedbackText: { fontSize: font.md, lineHeight: 22 },
  actionBar: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    backgroundColor: colors.bgCard,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
  },
  flex: { flex: 1 },
  settledNote: { flex: 1, fontSize: font.sm, color: colors.textSecondary },
  sheetBody: { padding: space.md },
  sheetTitle: { fontSize: font.lg, fontWeight: "600", marginBottom: space.sm, color: colors.text },
  sheetCancel: { marginTop: space.sm },
});

