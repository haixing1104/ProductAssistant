// 商品详情：实时生成流（打字机+阶段+配图）/ 上传图管理 / 最新内容图文 / AI 思考轨迹 / 审批与驳回复盘。
//
// 与 backend 契约对齐的要点（PA 专属，照抄 PP 会错的地方）:
//   · 详情接口带 `active_job_status` / `active_job_error` → 「生成中」按钮态与失败原因据此判断，
//     不必靠猜（PP 无此字段）；
//   · **PA 没有 `last_reject_*` 字段**：驳回复盘走 `GET /approvals?product_id=…`（历史审批单，
//     带完整 `content_snapshot` 与 `feedback`）—— 这是 PA 的等价能力（见 backend README §2.6）；
//   · 图片上传：`POST /oss/presign` 需要 product_id → 三步（换 URL → 浏览器 PUT → PATCH 整体覆盖 raw_images）；
//   · 流可重连：服务端空闲关流（注释帧）→ 连接器自动带 Last-Event-ID 续连；
//     `hitl.waiting` 是**终态**（等待审批可能数小时）→ 停止，审批后回来点「连接实时流」即可。
//   · **重开流必须换 key**（历史事故，勿改回）：连接器命中终态/ready 后即 `stopped` 且**不会自己复活**，
//     而 `streamOn` 常常在进页面时就已经是 true（有活跃任务 / 用户点过「连接实时流」）
//     → 此时 `setStreamOn(true)` 是 no-op：不重渲染、不重挂载、不会再建连接。
//     新任务的 `done` 事件因此永远无人接收 → 按钮永久停在「生成中…」（本次修的 bug）。
//     解法：`streamNonce` 参与 `StreamingDisplay` 的 key 强制重挂载；另有「仅在生成中轮询」兜底。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Image,
  List,
  message,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { approvalsApi, contentsApi, evaluationLogsApi, ossApi, productsApi } from "../api";
import BlockRenderer from "../components/BlockRenderer";
import DeliverySummary from "../components/DeliverySummary";
import StreamingDisplay from "../components/StreamingDisplay";
import { reasonMeta, snapshotBlocks } from "../services/contentSnapshot";
import { apiErrorMessage } from "../services/errors";
import { traceRows } from "../services/evaluationTrace";
import {
  approvalStatusLabel,
  productStatusColor,
  productStatusLabel,
  stockStatusLabel,
} from "../services/productMeta";
import { canWriteProducts, useAuthStore } from "../store/authStore";
import type { Approval, ContentVersion, Product } from "../types/api";

/** 允许的图片类型（与 backend `services/oss.ALLOWED_CONTENT_TYPES` 对齐；此处用于选图时预检）。 */
const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

/**
 * 查询失败提示（可重试）。
 *
 * 为什么必须有（2026-09 实测）: 详情页此前只处理「商品」这一路查询的错误，
 * 内容版本 / 思考轨迹 / 审批复盘三路查询一旦失败（403/500/网络）会被静默当成"没有数据"，
 * 渲染成 Empty 文案 —— 「驳回后看不到内容」的观感一半来自这里：用户看到的是"没有记录"，
 * 而真相是"接口没取到"。
 */
function QueryError({
  title,
  error,
  onRetry,
}: {
  title: string;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <Alert
      type="warning"
      showIcon
      message={title}
      description={apiErrorMessage(error, "加载失败，请重试")}
      action={
        <Button size="small" onClick={onRetry}>
          重试
        </Button>
      }
    />
  );
}

/**
 * 详情查询的兜底轮询策略（导出便于单测，与 ``pages/OpsPage.heartbeatMeta`` 同一做法）。
 *
 * 为什么需要它：SSE 是状态推进的主通道，但**它可能断**（网络抖动 / 后端重启 / 票据重试耗尽 /
 * 服务端因 ready 提前关流）。没有兜底时按钮会永远停在「生成中…」直到手动刷新页面
 * （2026-09 事故的另一半成因）。只在「进行中」时轮询，终态后立即停（返回 false）。
 *
 * 参数:
 *   current: 详情接口返回的商品（可能还没加载出来）。
 * 返回:
 *   5000（进行中：``generating`` 或仍有 ``active_job_status``）或 false（不再轮询）。
 */
export function detailRefetchInterval(current?: Product): number | false {
  const inFlight = Boolean(
    current && (current.status === "generating" || current.active_job_status),
  );
  return inFlight ? 5000 : false;
}

/**
 * 商品详情页（本项目最复杂的一屏）：基础信息 / 生成与实时流 / 已保存图文 / 思考轨迹 /
 * 审批与驳回复盘 / 商品图素材。
 *
 * 两处历史事故的护栏请勿移除:
 *   ① `streamNonce`：命中终态后连接器不会自动复活，重新生成必须换 key 重挂载；
 *   ② `detailRefetchInterval`：SSE 可能断，进行中要轮询兜底，否则按钮永久「生成中…」。
 */
export default function ProductDetailPage() {
  const { productId = "" } = useParams();
  const qc = useQueryClient();
  const role = useAuthStore((s) => s.user?.role);
  const writable = canWriteProducts(role);
  const [streamOn, setStreamOn] = useState(false);
  // 「重开流」的强制开关：连接器一旦 stopped 就不会自己复活，而 streamOn 很可能一直是 true
  // → 用自增 nonce 参与 StreamingDisplay 的 key（nonce 变 = 组件重挂载 = 新建 SSE 连接）。
  const [streamNonce, setStreamNonce] = useState(0);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [uploading, setUploading] = useState(false);

  const detail = useQuery({
    queryKey: ["product", productId],
    queryFn: () => productsApi.get(productId),
    enabled: productId.length > 0,
    // 兜底轮询（**仅进行中**，5s 一次）：SSE 是主通道，但它可能断（网络抖动 / 后端重启 /
    // 票据重试耗尽 / 服务端 ready 提前关流）。没有这层兜底时，按钮会一直停在「生成中…」
    // 直到手动刷新页面 —— 正常终态后这里会自动停（status 不再是 generating 且无 active_job）。
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
  // 审批与驳回复盘：按 product_id 查历史（**必须显式 status=all**；带完整快照）
  // 历史事故（2026-09）：这里只传 product_id + with_snapshot，而 backend 的缺省语义是
  // status=pending → 已定案（批准/驳回）的单永远查不到 → 卡片恒显示「还没有审批记录」，
  // 「驳回后看不到被驳回的图文」正是这么来的。契约见 backend README §2.6。
  const approvals = useQuery({
    queryKey: ["approvals", "product", productId],
    queryFn: () =>
      approvalsApi.list({ productId, status: "all", withSnapshot: true, limit: 20 }),
    enabled: productId.length > 0,
  });

  // 有活跃任务就自动连流（生成中/待审批）；任务结束由流自身终止
  const activeJob = Boolean(product?.active_job_status);
  useEffect(() => {
    if (activeJob) setStreamOn(true);
  }, [activeJob]);

  const versions: ContentVersion[] = contents.data ?? [];
  const currentVersion = versions.find((v) => v.version === selectedVersion) ?? versions[0] ?? null;

  const generate = useMutation({
    mutationFn: () => productsApi.generate(productId),
    onSuccess: (result) => {
      message.success(`已触发生成（任务 ${result.thread_id.slice(0, 8)}…），可实时查看过程`);
      // 关键：**必须让流重开**。streamOn 很可能已经是 true（进页面时有活跃任务，或用户先点过
      // 「连接实时流」）——此时 setStreamOn(true) 是 no-op，组件不重挂载，而它的连接器早已因
      // 服务端 ready/终态而 stopped：新任务的 done 事件无人接收 → 按钮永久「生成中…」。
      // 因此这里额外 bump nonce（参与 StreamingDisplay 的 key）强制重挂载。
      setStreamOn(true);
      setStreamNonce((n) => n + 1);
      qc.invalidateQueries({ queryKey: ["product", productId] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "触发生成失败")),
  });

  /** 生成过程结束后刷新：状态、内容、轨迹、审批都要重新拉（四者都会变）。 */
  const refreshAfterStream = () => {
    qc.invalidateQueries({ queryKey: ["product", productId] });
    qc.invalidateQueries({ queryKey: ["contents", productId] });
    qc.invalidateQueries({ queryKey: ["traces", productId] });
    qc.invalidateQueries({ queryKey: ["approvals", "product", productId] });
  };

  /** 上传一张商品图：换预签名 URL → 浏览器直传 → PATCH 覆盖 raw_images（追加）。 */
  const uploadImage = async (file: File): Promise<boolean> => {
    if (!IMAGE_TYPES.has(file.type)) {
      message.error("只支持 JPEG / PNG / WebP 图片");
      return false;
    }
    setUploading(true);
    try {
      const signed = await ossApi.presign({
        product_id: productId,
        filename: file.name,
        content_type: file.type,
      });
      if (file.size > signed.max_upload_bytes) {
        message.error(`图片超过上限（${Math.floor(signed.max_upload_bytes / 1024 / 1024)}MB）`);
        return false;
      }
      await ossApi.put(signed.upload_url, file, file.type);
      const next = [...(product?.raw_images ?? []), signed.public_url];
      await productsApi.update(productId, { raw_images: next });
      message.success("图片已上传并写入商品");
      qc.invalidateQueries({ queryKey: ["product", productId] });
    } catch (e) {
      message.error(apiErrorMessage(e, "图片上传失败"));
    } finally {
      setUploading(false);
    }
    return false;
  };

  /** 删除一张已上传图（PATCH 整体覆盖：写回新列表而不是「就地删」）。 */
  const removeImage = useMutation({
    mutationFn: (url: string) =>
      productsApi.update(productId, { raw_images: (product?.raw_images ?? []).filter((u) => u !== url) }),
    onSuccess: () => {
      message.success("已移除该图片（OSS 对象会在彻底删除商品时清理）");
      qc.invalidateQueries({ queryKey: ["product", productId] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "移除失败")),
  });

  if (detail.isLoading) return <Card loading />;
  if (detail.isError || !product) {
    return <Alert type="error" showIcon message="商品不存在或不属于当前租户（越权与不存在同响应）" />;
  }

  const trace = traceRows(traces.data ?? []);
  const approvalItems: Approval[] = approvals.data?.items ?? [];
  const canGenerate = writable && product.status !== "generating" && product.status !== "waiting_approval";
  // 最近一次驳回（复盘卡用它渲染「按该意见重新生成」；意见由 backend 随新任务注入下一轮生成）
  const lastRejected = approvalItems.find((item) => item.status === "rejected") ?? null;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card title="商品基础信息" extra={<Link to="/">← 返回列表</Link>}>
        <Descriptions column={2} size="small">
          <Descriptions.Item label="SKU">{product.sku_code}</Descriptions.Item>
          <Descriptions.Item label="标题">{product.title}</Descriptions.Item>
          <Descriptions.Item label="售价">¥ {product.base_price}</Descriptions.Item>
          <Descriptions.Item label="库存">{stockStatusLabel(product.stock_status)}</Descriptions.Item>
          <Descriptions.Item label="状态">
            <Tag color={productStatusColor(product.status)}>{productStatusLabel(product.status)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label="进行中任务">
            {product.active_job_status ?? <Typography.Text type="secondary">无</Typography.Text>}
          </Descriptions.Item>
        </Descriptions>
        {product.active_job_error ? (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 8 }}
            message="最近一次任务失败原因"
            description={product.active_job_error}
          />
        ) : null}
        {product.status === "waiting_approval" ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 8 }}
            message="该商品已转人工审批，等待审批人处理"
            description="审批通过后会恢复写入并上架；审批中心可查看 AI 生成详情与通知投递状态。"
          />
        ) : null}
        <Divider style={{ margin: "12px 0" }} />
        <Space wrap>
          <Button
            type="primary"
            loading={generate.isPending}
            disabled={!canGenerate}
            onClick={() => generate.mutate()}
          >
            {product.status === "generating" ? "生成中…" : "开始 / 重新生成"}
          </Button>
          <Button onClick={() => setStreamOn((v) => !v)}>{streamOn ? "断开实时流" : "连接实时流"}</Button>
          <Upload
            beforeUpload={uploadImage}
            showUploadList={false}
            accept="image/*"
            disabled={!writable || uploading}
          >
            <Button loading={uploading} disabled={!writable}>
              上传商品图
            </Button>
          </Upload>
          {!writable ? <Typography.Text type="secondary">当前角色只读（审核员）</Typography.Text> : null}
        </Space>
        {(product.raw_images ?? []).length > 0 ? (
          <div style={{ marginTop: 12 }}>
            <Typography.Text type="secondary">
              已上传 {product.raw_images?.length} 张（AI 生成时作为素材参考）
            </Typography.Text>
            <Space wrap style={{ marginTop: 8 }}>
              <Image.PreviewGroup>
                {(product.raw_images ?? []).map((url) => (
                  <Space key={url} direction="vertical" size={2} align="center">
                    <Image src={url} width={96} height={96} style={{ objectFit: "cover", borderRadius: 6 }} />
                    <Button size="small" danger disabled={!writable} onClick={() => removeImage.mutate(url)}>
                      移除
                    </Button>
                  </Space>
                ))}
              </Image.PreviewGroup>
            </Space>
          </div>
        ) : (
          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
            未上传商品图：AI 将按商品信息生成配图（上传图存在时优先使用上传图）。
          </Typography.Paragraph>
        )}
      </Card>

      {/* 三路查询的错误必须显式可见（否则接口失败会被当成"没有数据"，见 QueryError 注释） */}
      {contents.isError ? (
        <QueryError
          title="已保存内容加载失败"
          error={contents.error}
          onRetry={() => void contents.refetch()}
        />
      ) : null}
      {traces.isError ? (
        <QueryError title="AI 思考轨迹加载失败" error={traces.error} onRetry={() => void traces.refetch()} />
      ) : null}
      {approvals.isError ? (
        <QueryError
          title="审批与驳回复盘加载失败"
          error={approvals.error}
          onRetry={() => void approvals.refetch()}
        />
      ) : null}

      {streamOn ? (
        <Card
          title="AI 实时生成"
          extra={
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              打字机正文来自 content.chunk；阶段行来自 stage.* / agent.* / evaluate.result
            </Typography.Text>
          }
        >
          <StreamingDisplay
            // key 里带线程与 nonce：新一轮生成 / 手动重连都会**重挂载**（= 新建 SSE 连接）。
            // 只靠 streamOn 布尔量无法重开 —— 连接器一旦 stopped 就不会自己复活。
            key={`${productId}:${product.active_thread_id ?? "none"}:${streamNonce}`}
            productId={productId}
            onDone={refreshAfterStream}
            onWaiting={refreshAfterStream}
            onTerminal={() => refreshAfterStream()}
            // 服务端回 ready（它认为没有进行中任务）时也刷新一次：这是 UI 与后端状态纠偏的机会
            // （例如任务其实早已结束，只是本页的流断了）。
            onNoActiveTask={refreshAfterStream}
          />
        </Card>
      ) : null}

      {/* 审批与驳回复盘：PA 用「按产品查审批历史」替代 PP 的 last_reject_* 冗余列。
          必须带 status=all —— 缺省 pending 会让已定案（驳回/批准）的单查不到。 */}
      <Card
        title="审批与驳回复盘"
        extra={
          <Space>
            {lastRejected && writable ? (
              <Tooltip title="驳回意见会由 backend 随新任务注入下一轮生成（AI 需逐条解决）">
                <Button
                  size="small"
                  type="primary"
                  loading={generate.isPending}
                  disabled={!canGenerate}
                  onClick={() => generate.mutate()}
                >
                  按驳回意见重新生成
                </Button>
              </Tooltip>
            ) : null}
            <Link to="/approvals">去审批中心</Link>
          </Space>
        }
      >
        {approvalItems.length === 0 ? (
          <Empty description="该商品还没有审批记录" />
        ) : (
          <List
            dataSource={approvalItems}
            renderItem={(item) => {
              const meta = reasonMeta(item.content_snapshot?.reason ?? item.snapshot_summary?.reason ?? undefined);
              return (
                <List.Item key={item.id}>
                  <Space direction="vertical" size={6} style={{ width: "100%" }}>
                    <Space wrap>
                      <Tag color={item.status === "approved" ? "green" : item.status === "rejected" ? "red" : "gold"}>
                        {approvalStatusLabel(item.status)}
                      </Tag>
                      {meta ? <Tag color={meta.color}>{meta.label}</Tag> : null}
                      <Typography.Text type="secondary">
                        {item.resolved_at ?? item.created_at ?? "-"}
                        {item.approver_name ? ` · 审批人 ${item.approver_name}` : ""}
                      </Typography.Text>
                      <DeliverySummary notes={item.notifications} compact />
                    </Space>
                    {item.feedback ? (
                      <Alert type="info" showIcon message={`审批意见：${item.feedback}`} />
                    ) : null}
                    {item.content_snapshot ? (
                      <BlockRenderer blocks={snapshotBlocks(item.content_snapshot)} maxHeight={280} bordered />
                    ) : (
                      <Typography.Text type="secondary">（该审批单没有内容快照）</Typography.Text>
                    )}
                  </Space>
                </List.Item>
              );
            }}
          />
        )}
      </Card>

      <Card
        title="已保存内容"
        extra={
          versions.length > 1 ? (
            <Select
              size="small"
              style={{ width: 160 }}
              value={currentVersion?.version}
              onChange={(v) => setSelectedVersion(v)}
              options={versions.map((v) => ({
                value: v.version,
                label: `v${v.version}${v.is_approved ? "（已上架）" : ""}`,
              }))}
            />
          ) : null
        }
      >
        {currentVersion ? (
          <>
            <Space style={{ marginBottom: 8 }} wrap>
              <Tag color={currentVersion.is_approved ? "green" : "default"}>
                v{currentVersion.version} · {currentVersion.is_approved ? "已上架" : "草稿"}
              </Tag>
              <Typography.Text type="secondary">
                {currentVersion.created_at ?? ""} · 模型 {currentVersion.model_name ?? "-"} · 线程{" "}
                {currentVersion.thread_id.slice(0, 8)}…
              </Typography.Text>
            </Space>
            <BlockRenderer blocks={currentVersion.content_data?.blocks} bordered />
          </>
        ) : (
          <Empty description="还没有已保存的内容（批准后才写入 product_contents；驳回/待审的图文见上方「审批与驳回复盘」）" />
        )}
      </Card>

      <Card
        title="AI 思考轨迹（Trace）"
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            只读展示每次自检/重试的判据（来自 evaluation_logs）：复盘「为什么被拦 / 为什么重试」
          </Typography.Text>
        }
      >
        {trace.length === 0 ? (
          <Empty description="暂无评估记录" />
        ) : (
          <Table
            rowKey="key"
            size="small"
            pagination={false}
            dataSource={trace}
            columns={[
              { title: "第几次", dataIndex: "attempt", width: 80 },
              { title: "评估层", dataIndex: "evaluatorLabel", width: 180 },
              { title: "得分", dataIndex: "score", width: 90, render: (v: number | null) => v ?? "-" },
              { title: "违规/命中点", dataIndex: "violations", render: (v: string) => v || "-" },
              {
                title: "耗时(ms)",
                dataIndex: "latencyMs",
                width: 100,
                render: (v: number | null) => v ?? "-",
              },
              { title: "时间", dataIndex: "createdAt", width: 200, render: (v: string | null) => v ?? "-" },
            ]}
          />
        )}
      </Card>
    </Space>
  );
}


