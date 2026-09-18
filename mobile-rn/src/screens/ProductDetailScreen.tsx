// 商品详情（RN 版）—— 与 H5 的 `ProductDetailPage` 同口径：
// 生成中实时流 + 已保存图文 + 思考轨迹 + 审批复盘 + 商品图素材 + 底部固定操作栏。
//
// **必须照搬的三个教训**（移植时逐条保留，注释里写明原因）:
//   1) `streamNonce` 参与 `StreamingPanel` 的 key → 点生成时**强制重挂载**。连接器一旦 `stopped`
//      就不会自己复活，只 setStreamOn(true) 是 no-op → 按钮永久「生成中…」（2026-09 事故根因）；
//   2) 详情查询**仅在进行中**开 5s 兜底轮询（SSE 断线时也能复位按钮）；
//   3) 复盘查询**必须带 `status=all`** —— backend 缺省是 pending，已定案（驳回/批准）的单会查不到，
//      界面恒显示「还没有审批记录」（「驳回后看不到被驳回的图文」正是这么来的）。
import { useState } from "react";
import { Image, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import BlockView from "../components/BlockView";
import DeliveryNotes from "../components/DeliveryNotes";
import QueryError from "../components/QueryError";
import StreamingPanel from "../components/StreamingPanel";
import { ApprovalStatusTag, ProductStatusTag, ScoreTag } from "../components/StatusTag";
import Button from "../ui/Button";
import Chips from "../ui/Chips";
import { LabeledTextArea } from "../ui/Field";
import { ImageThumbs, ImageViewer } from "../ui/ImageViewer";
import { ListRow, ListSection, Loading, PreWrapText } from "../ui/List";
import { NavBar, NoticeBar } from "../ui/NavBar";
import { ActionSheet, Sheet } from "../ui/Sheet";
import Tag from "../ui/Tag";
import { colors, font, space } from "../ui/theme";
import { Dialog, Toast } from "../ui/feedback";
import { approvalsApi, contentsApi, evaluationLogsApi, ossApi, productsApi } from "@pa/core/api";
import { reasonMeta, reasonShortLabel, snapshotBlocks, snapshotViolations } from "@pa/core/services/contentSnapshot";
import { apiErrorMessage } from "@pa/core/services/errors";
import { traceRows } from "@pa/core/services/evaluationTrace";
import { formatPrice, formatTime, toTagColor } from "@pa/core/services/mobileFormat";
import { stockStatusLabel } from "@pa/core/services/productMeta";
import { canWriteProducts, isAdmin, useAuthStore } from "@pa/core/store/authStore";
import type { Approval, Product } from "@pa/core/types/api";

import { pickImageFile } from "../platform/pickFile";

/**
 * 兜底轮询周期：**仅进行中**才 5s 一次（终态自动停）。
 *
 * 为什么需要: SSE 是主通道，但它可能断（网络抖动 / 后端重启 / 票据重试耗尽 / 服务端 ready 提前关流）。
 * 没有这层兜底时，手机的弱网切换会让按钮一直停在「生成中…」直到手动刷新。
 * 抽成纯函数便于单测（H5 也是这么守护的）。
 */
export function detailRefetchInterval(current?: Product): number | false {
  const inFlight = Boolean(current && (current.status === "generating" || current.active_job_status));
  return inFlight ? 5000 : false;
}

/**
 * 商品详情（RN 版，最重的一屏）：基础信息 / 生成与实时流 / 已保存图文 / 思考轨迹 /
 * 审批复盘 / 商品图素材 + 底部固定操作栏。
 *
 * 两条同源护栏: 重新生成必须换 key 重挂载流（`streamNonce`）；
 * 进行中靠 `detailRefetchInterval` 兜底轮询，避免弱网下按钮永久「生成中…」。
 */
export default function ProductDetailScreen({ productId, onBack }: { productId: string; onBack: () => void }) {
  const qc = useQueryClient();
  const role = useAuthStore((state) => state.user?.role);
  const writable = canWriteProducts(role);

  const [streamOn, setStreamOn] = useState(false);
  const [streamNonce, setStreamNonce] = useState(0);
  const [selectedVersion, setSelectedVersion] = useState("");
  const [uploading, setUploading] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeReason, setPurgeReason] = useState("");
  const [previewImage, setPreviewImage] = useState<string | null>(null);

  const detail = useQuery({
    queryKey: ["product", productId],
    queryFn: () => productsApi.get(productId),
    enabled: productId.length > 0,
    refetchInterval: (query) => detailRefetchInterval(query.state.data as Product | undefined),
  });
  const product = detail.data;

  const contents = useQuery({
    queryKey: ["contents", productId],
    queryFn: () => contentsApi.list(productId),
    enabled: productId.length > 0,
  });
  const traces = useQuery({
    queryKey: ["traces", productId],
    queryFn: () => evaluationLogsApi.list(productId),
    enabled: productId.length > 0,
  });
  const approvals = useQuery({
    queryKey: ["approvals", "product", productId],
    // ⚠️ 必须显式 status=all：缺省是 pending，已定案的单（驳回/批准）会查不到
    queryFn: () => approvalsApi.list({ productId, status: "all", withSnapshot: true, limit: 20 }),
    enabled: productId.length > 0,
  });


  /** 生成/重新生成：**必须在成功后强制重挂载流**（见文件头教训 1）。 */
  const generate = useMutation({
    mutationFn: () => productsApi.generate(productId),
    onSuccess: (result) => {
      Toast.show({ content: `已触发生成（任务 ${result.thread_id.slice(0, 8)}…）` });
      setStreamOn(true);
      setStreamNonce((value) => value + 1); // key 变化 = 重建 SSE 连接
      void detail.refetch();
      void contents.refetch();
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "触发生成失败") }),
  });

  /** 流结束/转人工/ready 之后的统一动作：详情与内容都刷新一次（UI 与后端状态纠偏）。 */
  const refreshAfterStream = () => {
    void detail.refetch();
    qc.invalidateQueries({ queryKey: ["contents", productId] });
    qc.invalidateQueries({ queryKey: ["traces", productId] });
    qc.invalidateQueries({ queryKey: ["approvals", "product", productId] });
  };

  /** 上传商品图：presign → 二进制 PUT（同 Content-Type）→ PATCH raw_images（整体覆盖）。 */
  const uploadImage = async () => {
    try {
      const picked = await pickImageFile();
      if (!picked) return;
      setUploading(true);
      const signed = await ossApi.presign({
        product_id: productId,
        filename: picked.file.name,
        content_type: picked.file.type,
      });
      if (picked.size > signed.max_upload_bytes) {
        Toast.show({
          icon: "fail",
          content: `图片超过上限（${Math.floor(signed.max_upload_bytes / 1024 / 1024)}MB）`,
        });
        return;
      }
      // PUT 必须带**同一个** Content-Type（预签名时签的就是它，不一致会被 OSS 拒绝）
      await ossApi.put(signed.upload_url, picked.file, picked.file.type);
      const next = [...(product?.raw_images ?? []), signed.public_url];
      await productsApi.update(productId, { raw_images: next });
      Toast.show({ icon: "success", content: "图片已上传并写入商品" });
      qc.invalidateQueries({ queryKey: ["product", productId] });
    } catch (error) {
      Toast.show({ icon: "fail", content: apiErrorMessage(error, "图片上传失败") });
    } finally {
      setUploading(false);
    }
  };

  /** 移除一张已上传图（PATCH 整体覆盖：写回新列表，而不是"就地删"）。 */
  const removeImage = useMutation({
    mutationFn: (url: string) =>
      productsApi.update(productId, { raw_images: (product?.raw_images ?? []).filter((item) => item !== url) }),
    onSuccess: () => {
      Toast.show("已移除该图片（OSS 对象会在彻底删除商品时清理）");
      qc.invalidateQueries({ queryKey: ["product", productId] });
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "移除失败") }),
  });

  /** 彻底删除（admin + 原因必填，写审计）。 */
  const purge = useMutation({
    mutationFn: (reason: string) => productsApi.purge(productId, reason),
    onSuccess: (result) => {
      Toast.show({ icon: "success", content: `已删除 ${result.sku_code}（OSS 清理任务已入队）` });
      onBack();
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "彻底删除失败") }),
  });

  const versions = contents.data ?? [];
  const currentVersion =
    versions.find((version) => String(version.version) === selectedVersion) ?? versions[0] ?? null;
  const trace = traceRows(traces.data ?? []);
  const approvalItems: Approval[] = approvals.data?.items ?? [];
  const canGenerate = writable && product?.status !== "generating" && product?.status !== "waiting_approval";
  // 最近一次驳回：复盘卡用它渲染「按驳回意见重新生成」（意见由 backend 随新任务注入下一轮生成）
  const lastRejected = approvalItems.find((item) => item.status === "rejected") ?? null;

  if (detail.isLoading) {
    return (
      <View style={styles.page}>
        <NavBar title="商品详情" back={onBack} />
        <Loading />
      </View>
    );
  }
  if (detail.isError || !product) {
    return (
      <View style={styles.page}>
        <NavBar title="商品详情" back={onBack} />
        <QueryError what="商品详情加载" error={detail.error} onRetry={() => void detail.refetch()} />
      </View>
    );
  }

  const rawImages = product.raw_images ?? [];

  return (
    <View style={styles.page}>
      <NavBar
        title={product.sku_code}
        back={onBack}
        right={<ProductStatusTag status={product.status} />}
      />

      <ScrollView contentContainerStyle={styles.scrollBody} testID="pa-product-detail">
        <ListSection header="商品基础信息">
          <ListRow label="标题" extra={<PreWrapText>{product.title}</PreWrapText>} />
          <ListRow label="售价" extra={formatPrice(product.base_price)} />
          <ListRow label="库存" extra={stockStatusLabel(product.stock_status)} />
          <ListRow label="最近更新" extra={formatTime(product.updated_at)} />
          <ListRow label="进行中任务" extra={product.active_job_status ?? "无"} />
        </ListSection>

        {/* 任务终态后 `active_job_status` 会消失，因此 backend 额外回传 `active_job_error`
            —— 合规闸门拦截/生成失败必须在这里看得见原因 */}
        {product.active_job_error ? (
          <View style={styles.noticeWrap}>
            <NoticeBar color="error" content={`最近一次任务失败原因：${product.active_job_error}`} />
          </View>
        ) : null}
        {product.status === "waiting_approval" ? (
          <View style={styles.noticeWrap}>
            <NoticeBar
              color="alert"
              content="该商品已转人工审批：审批通过后才会恢复写入并上架（可在「审批」页处理）"
            />
          </View>
        ) : null}

        {/* 三路查询的错误必须显式可见（否则接口失败会被当成「没有数据」） */}
        {contents.isError ? (
          <QueryError what="已保存内容加载" error={contents.error} onRetry={() => void contents.refetch()} />
        ) : null}
        {traces.isError ? (
          <QueryError what="AI 思考轨迹加载" error={traces.error} onRetry={() => void traces.refetch()} />
        ) : null}
        {approvals.isError ? (
          <QueryError what="审批与驳回复盘加载" error={approvals.error} onRetry={() => void approvals.refetch()} />
        ) : null}

        {streamOn ? (
          <ListSection header="AI 实时生成">
            <ListRow>
              <StreamingPanel
                // key 里带线程与 nonce：新一轮生成 / 手动重连都会**重挂载**（= 新建 SSE 连接）。
                // 只靠 streamOn 布尔量无法重开 —— 连接器一旦 stopped 就不会自己复活。
                key={`${productId}:${product.active_thread_id ?? "none"}:${streamNonce}`}
                productId={productId}
                height={220}
                onDone={refreshAfterStream}
                onWaiting={refreshAfterStream}
                onTerminal={() => refreshAfterStream()}
                // 服务端回 ready（它认为没有进行中任务）时也刷新一次：UI 与后端状态纠偏的机会
                onNoActiveTask={refreshAfterStream}
              />
            </ListRow>
          </ListSection>
        ) : null}


        <ListSection header="已保存内容">
          {versions.length > 1 ? (
            <ListRow>
              <Chips
                options={versions.map((version) => ({
                  value: String(version.version),
                  label: `v${version.version}${version.is_approved ? "（已上架）" : ""}`,
                }))}
                value={String(currentVersion?.version ?? "")}
                onChange={setSelectedVersion}
                scrollable
                testID="pa-version-picker"
              />
            </ListRow>
          ) : null}
          <ListRow>
            {currentVersion ? (
              <View style={styles.blockWrap}>
                <View style={styles.versionHeader}>
                  <Tag color={currentVersion.is_approved ? "success" : "default"}>
                    v{currentVersion.version} · {currentVersion.is_approved ? "已上架" : "草稿"}
                  </Tag>
                  <Text style={styles.dim}>
                    {formatTime(currentVersion.created_at)} · 模型 {currentVersion.model_name ?? "-"}
                  </Text>
                </View>
                <BlockView blocks={currentVersion.content_data?.blocks} />
              </View>
            ) : (
              <PreWrapText style={styles.dim}>
                还没有已保存的内容（批准后才写入 product_contents；驳回/待审的图文见下方「审批与驳回复盘」）
              </PreWrapText>
            )}
          </ListRow>
        </ListSection>

        <ListSection header="AI 思考轨迹（Trace）">
          {trace.length === 0 ? (
            <ListRow label="暂无评估记录" description="生成一次后可见规则层/LLM 层的每次判据" />
          ) : (
            trace.map((row) => (
              <ListRow
                key={row.key}
                label={
                  <View>
                    <Text style={styles.traceTitle}>
                      第 {row.attempt} 次 · {row.evaluatorLabel}
                    </Text>
                    <Text style={styles.dim}>{formatTime(row.createdAt)}</Text>
                  </View>
                }
                extra={
                  <View style={styles.traceExtra}>
                    <ScoreTag score={row.score} />
                    <Text style={styles.dim}>{row.latencyMs !== null ? `${row.latencyMs}ms` : "-"}</Text>
                  </View>
                }
                description={<PreWrapText style={styles.dim}>命中：{row.violations || "无"}</PreWrapText>}
              />
            ))
          )}
        </ListSection>

        <ListSection header="审批与驳回复盘">
          {/* 驳回后最有用的一步：把这条意见带回生成（backend 会随新任务注入 guidance） */}
          {lastRejected && writable ? (
            <ListRow>
              <Button
                block
                variant="primary"
                fill="outline"
                loading={generate.isPending}
                disabled={!canGenerate}
                onPress={() => generate.mutate()}
              >
                按驳回意见重新生成
              </Button>
              <Text style={styles.dim}>驳回意见会随新任务注入下一轮生成，AI 需逐条解决</Text>
            </ListRow>
          ) : null}
          {approvalItems.length === 0 ? (
            <ListRow label="该商品还没有审批记录" />
          ) : (
            approvalItems.map((item) => {
              const reason = item.content_snapshot?.reason ?? item.snapshot_summary?.reason ?? undefined;
              const meta = reasonMeta(reason);
              const itemViolations = snapshotViolations(item.content_snapshot);
              return (
                <ListRow
                  key={item.id}
                  description={
                    <Text style={styles.dim}>
                      {formatTime(item.resolved_at ?? item.created_at)}
                      {item.approver_name ? ` · 审批人 ${item.approver_name}` : ""}
                    </Text>
                  }
                >
                  <View style={styles.blockWrap}>
                    <View style={styles.tagRow}>
                      <ApprovalStatusTag status={item.status} />
                      {reason ? (
                        <Tag color={meta ? toTagColor(meta.color) : "default"}>{reasonShortLabel(reason)}</Tag>
                      ) : null}
                      <ScoreTag score={item.snapshot_summary?.score ?? null} />
                    </View>
                    {meta ? <Text style={styles.dim}>{meta.label}</Text> : null}
                    {itemViolations.length > 0 ? (
                      <PreWrapText style={styles.hitText}>
                        {`命中 ${itemViolations.length} 点：`}
                        {itemViolations
                          .map(
                            (violation) =>
                              `${violation.keyword ?? ""}${violation.reason ? `（${violation.reason}）` : ""}`,
                          )
                          .filter((text) => text.length > 0)
                          .join("、")}
                      </PreWrapText>
                    ) : null}
                    {item.feedback ? (
                      <View style={styles.feedbackBox}>
                        <PreWrapText>审批意见：{item.feedback}</PreWrapText>
                      </View>
                    ) : null}
                    <DeliveryNotes notes={item.notifications} />
                    {item.content_snapshot ? (
                      <BlockView blocks={snapshotBlocks(item.content_snapshot)} />
                    ) : (
                      <Text style={styles.dim}>（该审批单没有内容快照）</Text>
                    )}
                  </View>
                </ListRow>
              );
            })
          )}
        </ListSection>


        <ListSection header={`商品图素材（${rawImages.length} 张）`}>
          <ListRow>
            <View style={styles.blockWrap}>
              {rawImages.length === 0 ? (
                <Text style={styles.dim}>
                  未上传商品图：AI 将按商品信息生成配图（上传图存在时优先使用上传图）。
                </Text>
              ) : (
                <View style={styles.rawImages}>
                  {rawImages.map((url) => (
                    <View key={url} style={styles.rawImageItem}>
                      <Pressable
                        accessibilityRole="imagebutton"
                        accessibilityLabel="查看商品图"
                        onPress={() => setPreviewImage(url)}
                      >
                        <Image source={{ uri: url }} style={styles.rawImage} resizeMode="cover" />
                      </Pressable>
                      <Button
                        size="mini"
                        variant="danger"
                        fill="none"
                        disabled={!writable}
                        onPress={() => {
                          void Dialog.confirm({
                            title: "移除图片",
                            content: "移除这张图片？（OSS 对象会在彻底删除商品时清理）",
                            confirmText: "移除",
                            danger: true,
                            onConfirm: () => removeImage.mutate(url),
                          });
                        }}
                      >
                        移除
                      </Button>
                    </View>
                  ))}
                </View>
              )}
              <View style={styles.uploadRow}>
                <Button
                  size="small"
                  loading={uploading}
                  disabled={!writable}
                  onPress={() => void uploadImage()}
                  testID="pa-upload-image"
                >
                  上传商品图
                </Button>
              </View>
            </View>
          </ListRow>
        </ListSection>
      </ScrollView>

      {/* 底部固定操作栏：主操作放在拇指区，危险/低频操作收进「更多」 */}
      <View style={styles.actionBar}>
        <Button
          variant="primary"
          size="large"
          style={styles.flex}
          loading={generate.isPending}
          disabled={!canGenerate}
          onPress={() => generate.mutate()}
          testID="pa-generate-submit"
        >
          {product.status === "generating" ? "生成中…" : "开始 / 重新生成"}
        </Button>
        <Button
          size="large"
          disabled={!writable || uploading}
          onPress={() => void uploadImage()}
          testID="pa-upload-inline"
        >
          上传图
        </Button>
        <Button size="large" onPress={() => setActionsOpen(true)} testID="pa-detail-more">
          更多
        </Button>
      </View>
      {!writable ? <Text style={styles.readonlyNote}>当前角色只读（审核员不可写商品）</Text> : null}

      <ActionSheet
        visible={actionsOpen}
        onClose={() => setActionsOpen(false)}
        actions={[
          { key: "stream", text: streamOn ? "断开实时流" : "连接实时流" },
          ...(isAdmin(role) ? [{ key: "purge", text: "彻底删除商品", danger: true }] : []),
        ]}
        onAction={(item) => {
          setActionsOpen(false);
          if (item.key === "stream") setStreamOn((value) => !value);
          if (item.key === "purge") {
            setPurgeReason("");
            setPurgeOpen(true);
          }
        }}
      />

      {/* 彻底删除：原因必填（与后端 422 同口径），空原因时按钮禁用 */}
      <Sheet visible={purgeOpen} onClose={() => setPurgeOpen(false)} testID="pa-purge-sheet">
        <View style={styles.sheetBody}>
          <Text style={styles.sheetTitle}>彻底删除商品</Text>
          <PreWrapText style={styles.dim}>删除后内容与 OSS 对象会一并清理，且不可恢复。</PreWrapText>
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
              purge.mutate(purgeReason.trim());
              setPurgeOpen(false);
            }}
            testID="pa-purge-confirm"
          >
            确认删除
          </Button>
          <Button block fill="none" onPress={() => setPurgeOpen(false)} style={styles.sheetCancel}>
            取消
          </Button>
        </View>
      </Sheet>

      <ImageViewer image={previewImage ?? ""} visible={previewImage !== null} onClose={() => setPreviewImage(null)} />
    </View>
  );
}


const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.bg },
  scrollBody: { paddingBottom: 104 },
  noticeWrap: { marginHorizontal: space.md },
  blockWrap: { width: "100%" },
  versionHeader: { flexDirection: "row", alignItems: "center", gap: space.sm, flexWrap: "wrap", marginBottom: space.sm },
  dim: { fontSize: font.xs, color: colors.textSecondary },
  traceTitle: { fontSize: font.md, color: colors.text },
  traceExtra: { alignItems: "flex-end", gap: space.xs },
  tagRow: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: space.sm, marginBottom: space.xs },
  hitText: { fontSize: font.xs, color: "#d4380d", marginTop: space.xs },
  feedbackBox: {
    marginTop: space.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    backgroundColor: "#f6ffed",
    borderWidth: 1,
    borderColor: "#b7eb8f",
    borderRadius: 6,
  },
  rawImages: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  rawImageItem: { alignItems: "center" },
  rawImage: { width: 88, height: 88, borderRadius: 6, borderWidth: 1, borderColor: colors.border },
  uploadRow: { flexDirection: "row", gap: space.sm, marginTop: space.sm },
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
  readonlyNote: { position: "absolute", bottom: 4, left: space.md, fontSize: font.xs, color: colors.textSecondary },
  sheetBody: { padding: space.md },
  sheetTitle: { fontSize: font.lg, fontWeight: "600", marginBottom: space.sm, color: colors.text },
  sheetCancel: { marginTop: space.sm },
});
