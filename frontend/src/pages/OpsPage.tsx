// 运维面板（PA 独有，仅 admin）：worker 心跳 / 契约流与消费组 PEL / 死信队列（DLQ）回看 /
// 卡住任务（**唯一**变更入口：终止 → 写审计）。
//
// 为什么读面**没有**「重投」按钮（刻意的设计取舍）:
//   DLQ 里的消息是「重试到上限仍失败」的 —— 根因多半是商品数据异常或外部依赖不可用。
//   一键重投只会再造一条毒消息、并掩盖根因。因此读面**只呈现事实**，处置走 backend-api/README
//   里的 SOP（判根因 → 修数据/等依赖 → 重投或重新触发生成）。
//
// 唯一的例外是「卡住任务」的**终止**（P8）:
//   卡在 running 的任务会让商品**永久 409**（详情页按钮也变成不可点），过去只能人肉改库且不留痕。
//   终止必须填原因 → 写入 job_abort_audits（只增不改）；后台 reaper 也会自动回收，
//   本入口用于「不等阈值、立刻处置」。
//
// 四个必看的判据（放在最显眼的位置）:
//   1) `stalled`（一个心跳都没有）= 消费侧完全停滞 —— 不是「健康」，必须红；
//   2) 流长度 + 消费组 `pending`：pending 持续增长 = 消费能力不足（积压）；
//   3) DLQ 非空 = 有消息被打成毒消息，需要人工介入；
//   4) 卡住任务非空 = 有商品正卡在「生成中」且用户点不动 —— 要么等 reaper，要么在这里终止。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { useState } from "react";

import { opsApi } from "../api";
import { apiErrorMessage } from "../services/errors";
import { productStatusLabel } from "../services/productMeta";
import type {
  DlqEntry,
  DlqSummary,
  StreamOverview,
  StuckJob,
  WorkerHeartbeat,
} from "../types/api";

/** 心跳 TTL 分级：ai-engine 默认每 30s 刷新一次，接近到期就要预警（避免「看着活着其实刚断」）。 */
export function heartbeatMeta(worker: WorkerHeartbeat): { label: string; color: string } {
  if (worker.error) return { label: "扫描失败", color: "default" };
  if (!worker.alive) return { label: "已停止（键已过期）", color: "red" };
  const ttl = worker.ttl_seconds ?? 0;
  if (ttl <= 10) return { label: `即将过期（${ttl}s）`, color: "orange" };
  return { label: `存活（TTL ${ttl}s）`, color: "green" };
}

/** 卡住任务的「多久没推进」文案（人看得懂比精确更重要）。 */
export function stuckAgeLabel(ageSeconds?: number | null): string {
  if (ageSeconds === null || ageSeconds === undefined) return "-";
  if (ageSeconds < 60) return `${ageSeconds}s`;
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}min`;
  return `${(ageSeconds / 3600).toFixed(1)}h`;
}

/** 运维面板（只读）：心跳 / 流与消费组 / DLQ 明细 / 卡住任务（可带原因终止）。 */
export default function OpsPage() {
  const qc = useQueryClient();
  const [domain, setDomain] = useState<string | null>(null);
  // 「终止」需要必填原因（审计要求）：用一个受控的待终止任务 + 原因输入
  const [pendingAbort, setPendingAbort] = useState<StuckJob | null>(null);
  const [abortReason, setAbortReason] = useState("");
  const overview = useQuery({ queryKey: ["ops", "overview"], queryFn: () => opsApi.overview() });
  const dlq = useQuery({
    queryKey: ["ops", "dlq", domain],
    queryFn: () => opsApi.dlq(domain as string, 20),
    enabled: Boolean(domain),
  });
  const abort = useMutation({
    mutationFn: ({ jobId, reason }: { jobId: string; reason: string }) =>
      opsApi.abortJob(jobId, reason),
    onSuccess: (result) => {
      message.success(
        result.product_released
          ? "任务已终止，商品已回到草稿（可重新生成）"
          : "任务已终止（商品已被更新的任务接管，未改动商品状态）",
      );
      setPendingAbort(null);
      setAbortReason("");
      qc.invalidateQueries({ queryKey: ["ops", "overview"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "终止任务失败")),
  });
  const data = overview.data;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space style={{ width: "100%", justifyContent: "space-between" }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          运维面板（读面只读 · 唯一变更：终止卡死任务）
        </Typography.Title>
        <Space>
          <Typography.Text type="secondary">
            环境 {data?.env ?? "-"} · 采集于 {data?.generated_at ?? "-"}
          </Typography.Text>
          <Button loading={overview.isFetching} onClick={() => overview.refetch()}>
            刷新
          </Button>
        </Space>
      </Space>

      {overview.isError ? (
        <Alert type="error" showIcon message={apiErrorMessage(overview.error, "无法获取运维数据")} />
      ) : null}

      {data?.stalled ? (
        <Alert
          type="error"
          showIcon
          message="消费侧停滞：没有任何 worker 心跳"
          description="所有心跳键都已过期 —— 说明 ai-engine 的 worker 没有在跑（或已崩溃）：新任务不会被消费、结果也不会回写。"
        />
      ) : (
        <Alert
          type="success"
          showIcon
          message={`消费侧存活：${(data?.workers ?? []).filter((w) => w.alive).length} 个 worker 心跳`}
        />
      )}

      <Card size="small" title="Worker 心跳">
        {(data?.workers ?? []).length === 0 ? (
          <Empty description="没有心跳键（消费侧未运行）" />
        ) : (
          <Table<WorkerHeartbeat>
            rowKey={(row) => row.consumer ?? row.key ?? "unknown"}
            size="small"
            pagination={false}
            dataSource={data?.workers ?? []}
            columns={[
              { title: "消费者", dataIndex: "consumer", render: (v: string | undefined) => v ?? "-" },
              { title: "心跳键", dataIndex: "key", ellipsis: true },
              {
                title: "状态",
                render: (_: unknown, row: WorkerHeartbeat) => {
                  const meta = heartbeatMeta(row);
                  return <Tag color={meta.color}>{meta.label}</Tag>;
                },
              },
              {
                title: "TTL(秒)",
                dataIndex: "ttl_seconds",
                width: 110,
                render: (v: number | undefined) => v ?? "-",
              },
            ]}
          />
        )}
      </Card>

      <Card size="small" title="契约流与消费组（pending 持续增长 = 消费能力不足）">
        <Table<StreamOverview>
          rowKey="name"
          size="small"
          pagination={false}
          dataSource={data?.streams ?? []}
          columns={[
            { title: "流", dataIndex: "name" },
            { title: "键", dataIndex: "key", ellipsis: true },
            {
              title: "长度",
              dataIndex: "length",
              width: 100,
              render: (v: number | null) => (v === null ? <Tag color="red">读取失败</Tag> : v),
            },
            {
              title: "消费组",
              render: (_: unknown, row: StreamOverview) =>
                row.groups.length === 0 ? (
                  <Typography.Text type="secondary">未建组（尚未消费过）</Typography.Text>
                ) : (
                  <Space direction="vertical" size={2}>
                    {row.groups.map((g, i) => (
                      <Typography.Text key={g.name ?? `g${i}`}>
                        <Tag>{g.name}</Tag> 消费者 {g.consumers} ·{" "}
                        <Tag color={(g.pending ?? 0) > 0 ? "orange" : "green"}>待处理 {g.pending}</Tag> · 最后投递{" "}
                        {g.last_delivered_id ?? "-"}
                      </Typography.Text>
                    ))}
                  </Space>
                ),
            },
          ]}
        />
      </Card>

      <Card
        size="small"
        title="卡住任务（超期未推进 → 会让商品永久「生成中」且按钮点不动）"
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            判据与后端回收器（reaper）同源；后台会自动回收，这里用于**立刻**处置（写审计）
          </Typography.Text>
        }
      >
        {(data?.stuck_jobs ?? []).length === 0 ? (
          <Empty description="没有卡住的任务（健康）" />
        ) : (
          <Table<StuckJob>
            rowKey="job_id"
            size="small"
            pagination={false}
            dataSource={data?.stuck_jobs ?? []}
            columns={[
              { title: "SKU", dataIndex: "sku_code", render: (v: string | null) => v ?? "-" },
              {
                title: "线程",
                dataIndex: "thread_id",
                width: 120,
                render: (v: string) => `${v.slice(0, 8)}…`,
              },
              {
                title: "任务状态",
                dataIndex: "job_status",
                width: 130,
                render: (v: string | undefined) => <Tag color="orange">{v ?? "-"}</Tag>,
              },
              {
                title: "商品状态",
                dataIndex: "product_status",
                width: 130,
                render: (v: string | null) => (v ? <Tag>{productStatusLabel(v)}</Tag> : "-"),
              },
              {
                title: "停滞时长",
                dataIndex: "age_seconds",
                width: 110,
                render: (v: number | null | undefined) => stuckAgeLabel(v),
              },
              {
                title: "操作",
                width: 110,
                render: (_: unknown, row: StuckJob) => (
                  <Button
                    size="small"
                    danger
                    onClick={() => {
                      setPendingAbort(row);
                      setAbortReason("");
                    }}
                  >
                    终止
                  </Button>
                ),
              },
            ]}
          />
        )}
      </Card>

      <Modal
        open={Boolean(pendingAbort)}
        title="终止卡住的生成任务"
        okText="终止并写审计"
        okButtonProps={{ danger: true, disabled: abortReason.trim().length === 0 }}
        confirmLoading={abort.isPending}
        onOk={() => {
          if (pendingAbort) {
            abort.mutate({ jobId: pendingAbort.job_id, reason: abortReason.trim() });
          }
        }}
        onCancel={() => {
          setPendingAbort(null);
          setAbortReason("");
        }}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Typography.Text>
            线程 <Typography.Text code>{pendingAbort?.thread_id.slice(0, 8)}…</Typography.Text> ·
            商品 <Typography.Text code>{pendingAbort?.sku_code ?? "-"}</Typography.Text> · 停滞{" "}
            {stuckAgeLabel(pendingAbort?.age_seconds)}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            终止后：任务置为 failed、商品回到草稿（可重新生成）。原因会写入 job_abort_audits
            审计表（只增不改），请写清「为什么判定它已死」。
          </Typography.Text>
          <Input.TextArea
            rows={3}
            maxLength={200}
            showCount
            value={abortReason}
            placeholder="例如：worker 已被 kill，Redis 中无该线程消息，人工确认可终止"
            onChange={(e) => setAbortReason(e.target.value)}
          />
        </Space>
      </Modal>

      <Card
        size="small"
        title="死信队列（DLQ）"
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            只读：处置 SOP 见 backend-api/README（判根因 → 修数据/等依赖 → 重投或重新触发生成）
          </Typography.Text>
        }
      >
        {(data?.dlq ?? []).length === 0 ? (
          <Empty description="没有死信（健康）" />
        ) : (
          <Table<DlqSummary>
            rowKey={(row) => row.domain ?? row.key ?? "unknown"}
            size="small"
            pagination={false}
            dataSource={data?.dlq ?? []}
            columns={[
              { title: "域", dataIndex: "domain", render: (v: string | undefined) => v ?? "-" },
              { title: "键", dataIndex: "key", ellipsis: true },
              {
                title: "消息数",
                dataIndex: "length",
                width: 160,
                render: (v: number | undefined, row: DlqSummary) =>
                  row.error ? <Tag color="red">{row.error}</Tag> : <Tag color="red">{v}</Tag>,
              },
              {
                title: "操作",
                width: 120,
                render: (_: unknown, row: DlqSummary) =>
                  row.domain ? (
                    <Button size="small" onClick={() => setDomain(row.domain as string)}>
                      回看消息
                    </Button>
                  ) : null,
              },
            ]}
          />
        )}

        {domain ? (
          <Card size="small" type="inner" title={`${domain} 最新消息`} style={{ marginTop: 12 }}>
            <Descriptions size="small" column={2}>
              <Descriptions.Item label="键">{dlq.data?.key ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="总条数">{dlq.data?.length ?? "-"}</Descriptions.Item>
            </Descriptions>
            <Table<DlqEntry>
              rowKey="seq"
              size="small"
              loading={dlq.isLoading}
              dataSource={dlq.data?.entries ?? []}
              pagination={false}
              columns={[
                { title: "消息 ID", dataIndex: "seq", width: 200 },
                {
                  title: "载荷",
                  render: (_: unknown, row: DlqEntry) =>
                    row.payload ? (
                      <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap", fontSize: 12 }}>
                        {JSON.stringify(row.payload, null, 2)}
                      </Typography.Paragraph>
                    ) : (
                      <Tag color="orange">解码失败：{row.decode_error}</Tag>
                    ),
                },
              ]}
            />
          </Card>
        ) : null}
      </Card>
    </Space>
  );
}

