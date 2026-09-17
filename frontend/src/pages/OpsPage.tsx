// 运维面板（PA 独有，仅 admin）：worker 心跳 / 契约流与消费组 PEL / 死信队列（DLQ）回看。
//
// 为什么页面上**没有**「重投」按钮（刻意的设计取舍）:
//   DLQ 里的消息是「重试到上限仍失败」的 —— 根因多半是商品数据异常或外部依赖不可用。
//   一键重投只会再造一条毒消息、并掩盖根因。因此这里**只呈现事实**，处置走 backend-api/README
//   里的 SOP（判根因 → 修数据/等依赖 → 重投或重新触发生成）。
//
// 三个必看的判据（放在最显眼的位置）:
//   1) `stalled`（一个心跳都没有）= 消费侧完全停滞 —— 不是「健康」，必须红；
//   2) 流长度 + 消费组 `pending`：pending 持续增长 = 消费能力不足（积压）；
//   3) DLQ 非空 = 有消息被打成毒消息，需要人工介入。
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { opsApi } from "../api";
import { apiErrorMessage } from "../services/errors";
import type { DlqEntry, DlqSummary, StreamOverview, WorkerHeartbeat } from "../types/api";

/** 心跳 TTL 分级：ai-engine 默认每 30s 刷新一次，接近到期就要预警（避免「看着活着其实刚断」）。 */
export function heartbeatMeta(worker: WorkerHeartbeat): { label: string; color: string } {
  if (worker.error) return { label: "扫描失败", color: "default" };
  if (!worker.alive) return { label: "已停止（键已过期）", color: "red" };
  const ttl = worker.ttl_seconds ?? 0;
  if (ttl <= 10) return { label: `即将过期（${ttl}s）`, color: "orange" };
  return { label: `存活（TTL ${ttl}s）`, color: "green" };
}

export default function OpsPage() {
  const [domain, setDomain] = useState<string | null>(null);
  const overview = useQuery({ queryKey: ["ops", "overview"], queryFn: () => opsApi.overview() });
  const dlq = useQuery({
    queryKey: ["ops", "dlq", domain],
    queryFn: () => opsApi.dlq(domain as string, 20),
    enabled: Boolean(domain),
  });
  const data = overview.data;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space style={{ width: "100%", justifyContent: "space-between" }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          运维面板（只读）
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

