// 商品详情（移动版）：生成中实时流 + 已保存图文 + 思考轨迹 + 审批复盘 + 图片素材 + 底部操作栏。
//
// **必须照搬的三个桌面端教训**（移植时逐条保留，注释里写明原因）:
//   1) `streamNonce` 参与 `StreamingPanel` 的 key → 点生成时**强制重挂载**。连接器一旦
//      `stopped` 就不会自己复活，只 setStreamOn(true) 是 no-op → 按钮永久「生成中…」（2026-09 事故根因）；
//   2) 详情查询**仅在进行中**开 5s 兜底轮询（SSE 断线时也能复位按钮）；
//   3) 复盘查询**必须带 `status=all`** —— backend 缺省是 pending，已定案（驳回/批准）的单会查不到，
//      界面恒显示「还没有审批记录」（「驳回后看不到被驳回的图文」正是这么来的）。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ActionSheet,
  Button,
  Dialog,
  Divider,
  Image,
  ImageViewer,
  List,
  NavBar,
  NoticeBar,
  SafeArea,
  Selector,
  Tag,
  TextArea,
  Toast,
} from "antd-mobile";
import { MoreOutline } from "antd-mobile-icons";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { contentsApi, evaluationLogsApi, ossApi, productsApi, approvalsApi } from "@pa/core/api";
import {
  reasonMeta,
  reasonShortLabel,
  scoreColor,
  snapshotBlocks,
  snapshotViolations,
} from "@pa/core/services/contentSnapshot";
import { apiErrorMessage } from "@pa/core/services/errors";
import { traceRows } from "@pa/core/services/evaluationTrace";
import { stockStatusLabel } from "@pa/core/services/productMeta";
import { canWriteProducts, isAdmin, useAuthStore } from "@pa/core/store/authStore";
import type { Approval, Product } from "@pa/core/types/api";

import BlockView from "../components/BlockView";
import DeliveryNotes from "../components/DeliveryNotes";
import QueryError from "../components/QueryError";
import StreamingPanel from "../components/StreamingPanel";
import { ApprovalStatusTag, ProductStatusTag, ScoreTag } from "../components/StatusTag";
import { formatPrice, formatTime, toTagColor } from "@pa/core/services/mobileFormat";

const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

/**
 * 兜底轮询周期：**仅进行中**才 5s 一次（终态自动停）。
 *
 * 为什么需要: SSE 是主通道，但它可能断（网络抖动 / 后端重启 / 票据重试耗尽 / 服务端 ready 提前关流）。
 * 没有这层兜底时，手机的弱网切换会让按钮一直停在「生成中…」直到手动刷新。
 */
export function detailRefetchInterval(current?: Product): number | false {
  const inFlight = Boolean(current && (current.status === "generating" || current.active_job_status));
  return inFlight ? 5000 : false;
}

export default function ProductDetailPage() {
  const { productId = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const role = useAuthStore((s) => s.user?.role);
  const writable = canWriteProducts(role);

  const [streamOn, setStreamOn] = useState(false);
  const [streamNonce, setStreamNonce] = useState(0);
  const [selectedVersion, setSelectedVersion] = useState<string[]>([]);
  const [uploading, setUploading] = useState(false);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

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
  // 复盘：必须显式 status=all（缺省 pending 会漏掉已定案的单）
  const approvals = useQuery({
    queryKey: ["approvals", "product", productId],
    queryFn: () => approvalsApi.list({ productId, status: "all", withSnapshot: true, limit: 20 }),
    enabled: productId.length > 0,
  });

  const activeJob = Boolean(product?.active_job_status);
  useEffect(() => {
    if (activeJob) setStreamOn(true);
  }, [activeJob]);

  /** 生成过程结束后刷新：状态、内容、轨迹、审批四者都会变。 */
  const refreshAfterStream = () => {
    qc.invalidateQueries({ queryKey: ["product", productId] });
    qc.invalidateQueries({ queryKey: ["contents", productId] });
    qc.invalidateQueries({ queryKey: ["traces", productId] });
    qc.invalidateQueries({ queryKey: ["approvals", "product", productId] });
  };

  const generate = useMutation({
    mutationFn: () => productsApi.generate(productId),
    onSuccess: (result) => {
      Toast.show({ content: `已触发生成（任务 ${result.thread_id.slice(0, 8)}…）` });
      // 关键：**必须让流重开**。streamOn 很可能已是 true（进页面时有活跃任务）——
      // 此时 setStreamOn(true) 是 no-op，组件不重挂载，而连接器早已 stopped：
      // 新任务的 done 事件无人接收 → 按钮永久「生成中…」。所以额外 bump nonce。
      setStreamOn(true);
      setStreamNonce((n) => n + 1);
      qc.invalidateQueries({ queryKey: ["product", productId] });
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "触发生成失败") }),
  });
  /** 上传一张商品图：换预签名 URL → 浏览器直传 → PATCH 覆盖 raw_images（追加）。 */
  const uploadImage = async (file: File) => {
    if (!IMAGE_TYPES.has(file.type)) {
      Toast.show({ icon: "fail", content: "只支持 JPEG / PNG / WebP 图片" });
      return;
    }
    setUploading(true);
    try {
      const signed = await ossApi.presign({
        product_id: productId,
        filename: file.name,
        content_type: file.type,
      });
      if (file.size > signed.max_upload_bytes) {
        Toast.show({
          icon: "fail",
          content: `图片超过上限（${Math.floor(signed.max_upload_bytes / 1024 / 1024)}MB）`,
        });
        return;
      }
      // PUT 必须带**同一个** Content-Type（预签名时签的就是它，不一致会被 OSS 拒绝）
      await ossApi.put(signed.upload_url, file, file.type);
      const next = [...(product?.raw_images ?? []), signed.public_url];
      await productsApi.update(productId, { raw_images: next });
      Toast.show({ icon: "success", content: "图片已上传并写入商品" });
      qc.invalidateQueries({ queryKey: ["product", productId] });
    } catch (e) {
      Toast.show({ icon: "fail", content: apiErrorMessage(e, "图片上传失败") });
    } finally {
      setUploading(false);
    }
  };

  /** 删除一张已上传图（PATCH 整体覆盖：写回新列表，而不是「就地删」）。 */
  const removeImage = useMutation({
    mutationFn: (url: string) =>
      productsApi.update(productId, { raw_images: (product?.raw_images ?? []).filter((u) => u !== url) }),
    onSuccess: () => {
      Toast.show({ content: "已移除该图片（OSS 对象会在彻底删除商品时清理）" });
      qc.invalidateQueries({ queryKey: ["product", productId] });
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "移除失败") }),
  });

  /** 彻底删除（admin + 原因必填，写审计）：软删端点已下线，删除只有这一条路径。 */
  const purge = useMutation({
    mutationFn: (reason: string) => productsApi.purge(productId, reason),
    onSuccess: (result) => {
      Toast.show({ icon: "success", content: `已删除 ${result.sku_code}（OSS 清理任务已入队）` });
      navigate("/");
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "彻底删除失败") }),
  });
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeReason, setPurgeReason] = useState("");

  const versions = contents.data ?? [];
  const currentVersion =
    versions.find((v) => String(v.version) === (selectedVersion[0] ?? "")) ?? versions[0] ?? null;
  const trace = traceRows(traces.data ?? []);
  const approvalItems: Approval[] = approvals.data?.items ?? [];
  const canGenerate = writable && product?.status !== "generating" && product?.status !== "waiting_approval";
  // 最近一次驳回：复盘卡用它渲染「按驳回意见重新生成」（意见由 backend 随新任务注入下一轮生成）
  const lastRejected = approvalItems.find((item) => item.status === "rejected") ?? null;

  if (detail.isLoading) {
    return (
      <div>
        <NavBar onBack={() => navigate(-1)}>商品详情</NavBar>
        <List>
          <List.Item>加载中…</List.Item>
        </List>
      </div>
    );
  }
  if (detail.isError || !product) {
    return (
      <div>
        <NavBar onBack={() => navigate(-1)}>商品详情</NavBar>
        <QueryError what="商品详情加载" error={detail.error} onRetry={() => void detail.refetch()} />
      </div>
    );
  }
  return (
    <div className="pa-page pa-page--actions" style={{ background: "#f5f6f8" }}>
      <NavBar onBack={() => navigate("/")} right={<ProductStatusTag status={product.status} />}>
        {product.sku_code}
      </NavBar>

      <List header="商品基础信息" mode="card">
        <List.Item extra={<span className="pa-pre-wrap">{product.title}</span>}>标题</List.Item>
        <List.Item extra={formatPrice(product.base_price)}>售价</List.Item>
        <List.Item extra={stockStatusLabel(product.stock_status)}>库存</List.Item>
        <List.Item extra={formatTime(product.updated_at)}>最近更新</List.Item>
        <List.Item extra={product.active_job_status ?? "无"}>进行中任务</List.Item>
      </List>

      {/* 最近一次任务失败原因：任务终态后 `active_job_status` 会消失，因此 backend 额外回传
          `active_job_error`（来自最近一条任务）—— 合规闸门拦截/生成失败必须在这里看得见原因。 */}
      {product.active_job_error ? (
        <div style={{ margin: "8px 12px" }}>
          <NoticeBar color="error" content={`最近一次任务失败原因：${product.active_job_error}`} />
        </div>
      ) : null}
      {product.status === "waiting_approval" ? (
        <div style={{ margin: "8px 12px" }}>
          <NoticeBar
            color="alert"
            content="该商品已转人工审批：审批通过后才会恢复写入并上架（可在「审批」页处理）"
          />
        </div>
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
        <List header="AI 实时生成" mode="card">
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
            //（例如任务其实早已结束，只是本页的流断在了之前）
            onNoActiveTask={refreshAfterStream}
          />
        </List>
      ) : null}

      <List header="已保存内容" mode="card">
        {versions.length > 1 ? (
          <List.Item>
            <Selector
              columns={2}
              value={selectedVersion.length ? selectedVersion : currentVersion ? [String(currentVersion.version)] : []}
              onChange={(v) => setSelectedVersion(v.slice(-1))}
              options={versions.map((v) => ({
                value: String(v.version),
                label: `v${v.version}${v.is_approved ? "（已上架）" : ""}`,
              }))}
            />
          </List.Item>
        ) : null}
        {currentVersion ? (
          <List.Item>
            <div style={{ width: "100%" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                <Tag color={currentVersion.is_approved ? "success" : "default"}>
                  v{currentVersion.version} · {currentVersion.is_approved ? "已上架" : "草稿"}
                </Tag>
                <span style={{ fontSize: 12, color: "#999" }}>
                  {formatTime(currentVersion.created_at)} · 模型 {currentVersion.model_name ?? "-"}
                </span>
              </div>
              <BlockView blocks={currentVersion.content_data?.blocks} />
            </div>
          </List.Item>
        ) : (
          <List.Item>
            还没有已保存的内容（批准后才写入 product_contents；驳回/待审的图文见下方「审批与驳回复盘」）
          </List.Item>
        )}
      </List>

      <List header="AI 思考轨迹（Trace）" mode="card">
        {trace.length === 0 ? (
          <List.Item>暂无评估记录（生成一次后可见规则层/LLM 层的每次判据）</List.Item>
        ) : (
          trace.map((row) => (
            <List.Item
              key={row.key}
              description={<span className="pa-pre-wrap">命中：{row.violations || "无"}</span>}
              extra={
                <div style={{ textAlign: "right" }}>
                  <ScoreTag score={row.score} />
                  <div style={{ fontSize: 12, color: "#999", marginTop: 2 }}>
                    {row.latencyMs !== null ? `${row.latencyMs}ms` : "-"}
                  </div>
                </div>
              }
            >
              <div>
                第 {row.attempt} 次 · {row.evaluatorLabel}
              </div>
              <div style={{ fontSize: 12, color: "#999" }}>{formatTime(row.createdAt)}</div>
            </List.Item>
          ))
        )}
      </List>

      <List header="审批与驳回复盘" mode="card">
        {/* 驳回后最有用的一步：把这条意见带回生成（backend 会随新任务注入 guidance） */}
        {lastRejected && writable ? (
          <List.Item>
            <Button
              block
              color="primary"
              fill="outline"
              loading={generate.isPending}
              disabled={!canGenerate}
              onClick={() => generate.mutate()}
            >
              按驳回意见重新生成
            </Button>
            <div style={{ fontSize: 12, color: "#999", marginTop: 6 }}>
              驳回意见会随新任务注入下一轮生成，AI 需逐条解决
            </div>
          </List.Item>
        ) : null}
        {approvalItems.length === 0 ? (
          <List.Item>该商品还没有审批记录</List.Item>
        ) : (
          approvalItems.map((item) => {
            const reason = item.content_snapshot?.reason ?? item.snapshot_summary?.reason ?? undefined;
            const meta = reasonMeta(reason);
            const violations = snapshotViolations(item.content_snapshot);
            return (
              <List.Item
                key={item.id}
                description={
                  <div style={{ fontSize: 12, color: "#999" }}>
                    {formatTime(item.resolved_at ?? item.created_at)}
                    {item.approver_name ? ` · 审批人 ${item.approver_name}` : ""}
                  </div>
                }
              >
                <div style={{ width: "100%" }}>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                    <ApprovalStatusTag status={item.status} />
                    {reason ? (
                      <Tag color={meta ? toTagColor(meta.color) : "default"} fill="outline">
                        {reasonShortLabel(reason)}
                      </Tag>
                    ) : null}
                    <ScoreTag score={item.snapshot_summary?.score ?? null} />
                  </div>
                  {/* 长解释（如「高价商品（> ¥500），需人工放行」）在手机上用整行小字给出，
                      而不是塞进 Tag —— 否则会把评估分挤没（W3 的教训）。 */}
                  {meta ? <div style={{ fontSize: 12, color: "#999", marginTop: 4 }}>{meta.label}</div> : null}
                  {violations.length > 0 ? (
                    <div style={{ fontSize: 12, color: "#d4380d", marginTop: 4 }} className="pa-pre-wrap">
                      命中 {violations.length} 点：
                      {violations
                        .map((v) => `${v.keyword ?? ""}${v.reason ? `（${v.reason}）` : ""}`)
                        .filter((s) => s.length > 0)
                        .join("、")}
                    </div>
                  ) : null}
                  {item.feedback ? (
                    <div
                      style={{
                        marginTop: 8,
                        padding: "8px 10px",
                        background: "#f6ffed",
                        border: "1px solid #b7eb8f",
                        borderRadius: 6,
                        fontSize: 13,
                      }}
                      className="pa-pre-wrap"
                    >
                      审批意见：{item.feedback}
                    </div>
                  ) : null}
                  <Divider style={{ margin: "10px 0" }} />
                  <DeliveryNotes notes={item.notifications} />
                  {item.content_snapshot ? (
                    <BlockView blocks={snapshotBlocks(item.content_snapshot)} />
                  ) : (
                    <div style={{ fontSize: 12, color: "#999" }}>（该审批单没有内容快照）</div>
                  )}
                </div>
              </List.Item>
            );
          })
        )}
      </List>

      <List header={`商品图素材（${(product.raw_images ?? []).length} 张）`} mode="card">
        <List.Item>
          <div style={{ width: "100%" }}>
            {(product.raw_images ?? []).length === 0 ? (
              <div style={{ fontSize: 13, color: "#999", marginBottom: 8 }}>
                未上传商品图：AI 将按商品信息生成配图（上传图存在时优先使用上传图）。
              </div>
            ) : (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
                {(product.raw_images ?? []).map((url) => (
                  <div key={url} style={{ textAlign: "center" }}>
                    <Image
                      src={url}
                      width={88}
                      height={88}
                      fit="cover"
                      onClick={() => setPreview(url)}
                      style={{ borderRadius: 6, border: "1px solid #f0f0f0" }}
                    />
                    <Button
                      size="mini"
                      color="danger"
                      fill="none"
                      disabled={!writable}
                      onClick={() => {
                        void Dialog.confirm({
                          content: "移除这张图片？（OSS 对象会在彻底删除商品时清理）",
                          onConfirm: () => removeImage.mutate(url),
                        });
                      }}
                    >
                      移除
                    </Button>
                  </div>
                ))}
              </div>
            )}
            <Button size="small" loading={uploading} disabled={!writable} onClick={() => fileRef.current?.click()}>
              上传商品图
            </Button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void uploadImage(file);
                e.target.value = ""; // 允许连续上传同一张图
              }}
            />
          </div>
        </List.Item>
      </List>

      {/* 底部固定操作栏：主操作放在拇指区，危险/低频操作收进「更多」 */}
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
          <Button
            color="primary"
            style={{ flex: 1 }}
            loading={generate.isPending}
            disabled={!canGenerate}
            onClick={() => generate.mutate()}
          >
            {product.status === "generating" ? "生成中…" : "开始 / 重新生成"}
          </Button>
          <Button disabled={!writable || uploading} onClick={() => fileRef.current?.click()}>
            上传图
          </Button>
          <Button onClick={() => setActionsOpen(true)}>
            <MoreOutline />
          </Button>
        </div>
        {!writable ? (
          <div style={{ fontSize: 12, color: "#999", marginTop: 4 }}>当前角色只读（审核员不可写商品）</div>
        ) : null}
        <SafeArea position="bottom" />
      </div>

      <Dialog
        visible={purgeOpen}
        title="彻底删除商品"
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
          if (action.key === "ok") {
            purge.mutate(purgeReason.trim());
          } else {
            setPurgeOpen(false);
          }
        }}
        closeOnAction
      />
      <ActionSheet
        visible={actionsOpen}
        actions={[
          { key: "stream", text: streamOn ? "断开实时流" : "连接实时流" },
          ...(isAdmin(role)
            ? [{ key: "purge", text: "彻底删除商品", danger: true, disabled: purge.isPending }]
            : []),
        ]}
        onAction={(action) => {
          setActionsOpen(false);
          if (action.key === "stream") setStreamOn((v) => !v);
          if (action.key === "purge") {
            setPurgeReason("");
            setPurgeOpen(true);
          }
        }}
        cancelText="取消"
      />
      <ImageViewer image={preview ?? ""} visible={preview !== null} onClose={() => setPreview(null)} />
    </div>
  );
}

