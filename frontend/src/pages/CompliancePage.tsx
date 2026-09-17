// 合规词库（PA 独有）：违禁词 / 正则规则 CRUD + 快照查看 + 命中预览。
//
// 权限（与 backend 一致，务必分清）:
//   · **读**：admin / reviewer（审批人需要能查「为什么这条被判违规」）；
//   · **写**：仅 admin —— 这两张表是**全局配置（无 org_id）**，改一个词会影响**所有组织**
//     的生成结果。因此写按钮只对 admin 显示（服务端还有一道 403）。
//
// 为什么要有「预览」这个 tab:
//   规则是「写进库 → 下次生成才生效」，写错了要等一次真实生成才发现；而坏正则在 ai-engine 是
//   **静默跳过**的（看起来生效、实际没拦）。预览用**与入队完全一致的快照**跑一遍匹配，
//   把命中的词/位置/原因/分数/是否阻断当场显示出来 —— 上线前验证不用赌。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useState } from "react";

import { complianceApi } from "../api";
import { apiErrorMessage } from "../services/errors";
import { SEVERITY_LABELS, severityColor, severityLabel } from "../services/productMeta";
import { isAdmin, useAuthStore } from "../store/authStore";
import type { ComplianceHit, CompliancePreviewResult, ComplianceRule, ComplianceWord } from "../types/api";

const SEVERITY_OPTIONS = [
  { value: "high", label: SEVERITY_LABELS.high },
  { value: "medium", label: SEVERITY_LABELS.medium },
  { value: "low", label: SEVERITY_LABELS.low },
];

/** 把预览命中的位置画成带高亮的文本（命中区间用【】包起来，前端零依赖）。 */
export function highlightHits(text: string, hits: ComplianceHit[]): string {
  const spans = hitSpans(hits);
  if (spans.length === 0) return text;
  let out = "";
  let cursor = 0;
  for (const [start, end] of spans) {
    if (start < cursor) continue;
    out += text.slice(cursor, start) + "【" + text.slice(start, end) + "】";
    cursor = end;
  }
  return out + text.slice(cursor);
}

/** 命中区间（按起点排序、去重叠）——预览高亮的几何计算，独立出来便于单测。 */
export function hitSpans(hits: ComplianceHit[]): Array<[number, number]> {
  return hits
    .map((hit) => [hit.start, hit.end] as [number, number])
    .filter(([start, end]) => Number.isInteger(start) && Number.isInteger(end) && end > start)
    .sort((a, b) => a[0] - b[0]);
}

export default function CompliancePage() {
  const qc = useQueryClient();
  const admin = isAdmin(useAuthStore((s) => s.user?.role));
  const [tab, setTab] = useState("words");
  const [wordPage, setWordPage] = useState(1);
  const [rulePage, setRulePage] = useState(1);
  const [severityFilter, setSeverityFilter] = useState<string | undefined>();
  const [activeOnly, setActiveOnly] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [wordEditor, setWordEditor] = useState<{ mode: "create" | "edit"; word?: ComplianceWord } | null>(null);
  const [ruleEditor, setRuleEditor] = useState<{ mode: "create" | "edit"; rule?: ComplianceRule } | null>(null);
  const [previewText, setPreviewText] = useState("");
  const [preview, setPreview] = useState<CompliancePreviewResult | null>(null);
  const pageSize = 10;

  const words = useQuery({
    queryKey: ["words", wordPage, severityFilter, activeOnly, keyword],
    queryFn: () =>
      complianceApi.words({
        offset: (wordPage - 1) * pageSize,
        limit: pageSize,
        severity: severityFilter,
        activeOnly,
        keyword: keyword || undefined,
      }),
  });
  const rules = useQuery({
    queryKey: ["rules", rulePage, severityFilter],
    queryFn: () =>
      complianceApi.rules({ offset: (rulePage - 1) * pageSize, limit: pageSize, severity: severityFilter }),
  });
  const snapshot = useQuery({
    queryKey: ["snapshot"],
    queryFn: () => complianceApi.snapshot(),
    enabled: tab === "preview" || tab === "snapshot",
  });

  const saveWord = useMutation({
    mutationFn: async (values: Record<string, unknown> & { id?: string }) => {
      const { id, ...payload } = values;
      return id ? complianceApi.updateWord(id, payload) : complianceApi.createWord(payload as { word: string; severity: string });
    },
    onSuccess: () => {
      message.success("词条已保存（下次生成生效）");
      setWordEditor(null);
      qc.invalidateQueries({ queryKey: ["words"] });
      qc.invalidateQueries({ queryKey: ["snapshot"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "保存失败")),
  });

  const deleteWord = useMutation({
    mutationFn: (id: string) => complianceApi.deleteWord(id),
    onSuccess: () => {
      message.success("词条已删除");
      qc.invalidateQueries({ queryKey: ["words"] });
      qc.invalidateQueries({ queryKey: ["snapshot"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "删除失败")),
  });

  const saveRule = useMutation({
    mutationFn: async (values: Record<string, unknown> & { id?: string }) => {
      const { id, ...payload } = values;
      return id ? complianceApi.updateRule(id, payload) : complianceApi.createRule(payload as { pattern: string; severity: string });
    },
    onSuccess: () => {
      message.success("规则已保存（下次生成生效）");
      setRuleEditor(null);
      qc.invalidateQueries({ queryKey: ["rules"] });
      qc.invalidateQueries({ queryKey: ["snapshot"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "保存失败（正则语法错误会在此被拒）")),
  });

  const deleteRule = useMutation({
    mutationFn: (id: string) => complianceApi.deleteRule(id),
    onSuccess: () => {
      message.success("规则已删除");
      qc.invalidateQueries({ queryKey: ["rules"] });
      qc.invalidateQueries({ queryKey: ["snapshot"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "删除失败")),
  });

  const runPreview = useMutation({
    mutationFn: (text: string) => complianceApi.preview(text),
    onSuccess: (result) => setPreview(result),
    onError: (e) => message.error(apiErrorMessage(e, "预览失败")),
  });

  const severityFilterSelect = (onChange: (v: string | undefined) => void) => (
    <Select
      allowClear
      placeholder="全部严重级"
      style={{ width: 160 }}
      value={severityFilter}
      onChange={onChange}
      options={SEVERITY_OPTIONS}
    />
  );

  return (
    <div>
      <Space style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }} wrap>
        <Typography.Title level={4} style={{ margin: 0 }}>
          合规词库与规则
        </Typography.Title>
        <Typography.Text type="secondary">
          全局配置（对所有组织生效）：{admin ? "你可以修改" : "你只有查看权限（写操作仅管理员）"}
        </Typography.Text>
      </Space>

      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="规则改动在「下一次生成」时通过快照下发（不影响进行中的任务）"
        description="预览用「当前生效快照」计算命中，与真实生成口径一致；时效过滤在入队时完成，过期/未生效的词不会命中。"
      />

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          { key: "words", label: "违禁词" },
          { key: "rules", label: "正则规则" },
          { key: "preview", label: "命中预览" },
          { key: "snapshot", label: "当前快照" },
        ]}
      />

      {tab === "words" ? (
        <>
          <Space style={{ marginBottom: 12 }} wrap>
            <Input
              allowClear
              placeholder="按词面搜索"
              style={{ width: 200 }}
              value={keyword}
              onChange={(e) => {
                setKeyword(e.target.value);
                setWordPage(1);
              }}
            />
            {severityFilterSelect((v) => {
              setSeverityFilter(v);
              setWordPage(1);
            })}
            <Select
              style={{ width: 150 }}
              value={activeOnly ? "active" : "all"}
              onChange={(v) => {
                setActiveOnly(v === "active");
                setWordPage(1);
              }}
              options={[
                { value: "all", label: "全部词条" },
                { value: "active", label: "仅生效中" },
              ]}
            />
            <Button type="primary" disabled={!admin} onClick={() => setWordEditor({ mode: "create" })}>
              新增词条
            </Button>
          </Space>
          <Table<ComplianceWord>
            rowKey="id"
            size="small"
            loading={words.isLoading}
            dataSource={words.data?.items ?? []}
            pagination={{
              current: wordPage,
              pageSize,
              total: words.data?.total ?? 0,
              onChange: setWordPage,
              showSizeChanger: false,
            }}
            columns={[
              { title: "词面", dataIndex: "word" },
              {
                title: "严重级",
                dataIndex: "severity",
                render: (v: string) => <Tag color={severityColor(v)}>{severityLabel(v)}</Tag>,
              },
              { title: "来源", dataIndex: "source", render: (v: string | null) => v ?? "-" },
              {
                title: "生效状态",
                render: (_: unknown, row: ComplianceWord) => (
                  <Tag color={row.is_active ? "green" : "default"}>
                    {row.is_active ? "生效中" : "未生效/已过期"}
                  </Tag>
                ),
              },
              {
                title: "有效期",
                render: (_: unknown, row: ComplianceWord) =>
                  row.effective_at || row.expires_at
                    ? `${row.effective_at ?? "立即"} → ${row.expires_at ?? "长期"}`
                    : "长期有效",
              },
              {
                title: "操作",
                width: 160,
                render: (_: unknown, row: ComplianceWord) => (
                  <Space>
                    <Button size="small" disabled={!admin} onClick={() => setWordEditor({ mode: "edit", word: row })}>
                      编辑
                    </Button>
                    <Popconfirm
                      title="删除该词条？"
                      description="删除后下一次生成即不再拦截该词（物理删除）。"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      disabled={!admin}
                      onConfirm={() => deleteWord.mutate(row.id)}
                    >
                      <Button size="small" danger disabled={!admin}>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        </>
      ) : null}

      {tab === "rules" ? (
        <>
          <Space style={{ marginBottom: 12 }} wrap>
            {severityFilterSelect((v) => {
              setSeverityFilter(v);
              setRulePage(1);
            })}
            <Button type="primary" disabled={!admin} onClick={() => setRuleEditor({ mode: "create" })}>
              新增规则
            </Button>
            <Typography.Text type="secondary">
              正则语法在入库时校验（坏正则会 ai-engine 静默跳过，所以必须在写入前拦住）
            </Typography.Text>
          </Space>
          <Table<ComplianceRule>
            rowKey="id"
            size="small"
            loading={rules.isLoading}
            dataSource={rules.data?.items ?? []}
            pagination={{
              current: rulePage,
              pageSize,
              total: rules.data?.total ?? 0,
              onChange: setRulePage,
              showSizeChanger: false,
            }}
            columns={[
              { title: "正则", dataIndex: "pattern", ellipsis: true },
              {
                title: "严重级",
                dataIndex: "severity",
                width: 140,
                render: (v: string) => <Tag color={severityColor(v)}>{severityLabel(v)}</Tag>,
              },
              { title: "改写建议", dataIndex: "suggestion", render: (v: string | null) => v ?? "-" },
              {
                title: "操作",
                width: 160,
                render: (_: unknown, row: ComplianceRule) => (
                  <Space>
                    <Button size="small" disabled={!admin} onClick={() => setRuleEditor({ mode: "edit", rule: row })}>
                      编辑
                    </Button>
                    <Popconfirm
                      title="删除该规则？"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      disabled={!admin}
                      onConfirm={() => deleteRule.mutate(row.id)}
                    >
                      <Button size="small" danger disabled={!admin}>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        </>
      ) : null}


      {tab === "preview" ? (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Input.TextArea
            rows={4}
            value={previewText}
            onChange={(e) => setPreviewText(e.target.value)}
            placeholder="粘贴待检查的文案（标题/正文/整篇）。用当前生效快照计算，与真实生成口径一致。"
          />
          <Space>
            <Button
              type="primary"
              loading={runPreview.isPending}
              disabled={previewText.trim().length === 0}
              onClick={() => runPreview.mutate(previewText)}
            >
              检查这条文案
            </Button>
            <Typography.Text type="secondary">
              快照：{snapshot.data?.words_count ?? "-"} 个词 / {snapshot.data?.rules_count ?? "-"} 条正则
            </Typography.Text>
          </Space>

          {preview ? (
            <>
              <Alert
                type={preview.blocked ? "error" : preview.hits.length > 0 ? "warning" : "success"}
                showIcon
                message={
                  preview.blocked
                    ? `会被拦截（blocked=true，score=${preview.score}）`
                    : preview.hits.length > 0
                      ? `不会拦截，但有 ${preview.hits.length} 处提示级命中（score=${preview.score}）`
                      : `未命中任何规则（score=${preview.score}）`
                }
                description={
                  preview.blocked
                    ? "命中 high/medium 级规则 → 真实生成会在评估阶段 fail-fast 并让模型重写。"
                    : "low 级命中仅扣分与提示，不阻断。"
                }
              />
              {preview.hits.length > 0 ? (
                <>
                  <Card size="small" title="命中明细">
                    <Table<ComplianceHit>
                      rowKey={(row) => `${row.kind}-${row.start}-${row.keyword}`}
                      size="small"
                      pagination={false}
                      dataSource={preview.hits}
                      columns={[
                        {
                          title: "类型",
                          dataIndex: "kind",
                          width: 90,
                          render: (v: string) => (v === "word" ? "词" : "正则"),
                        },
                        { title: "命中片段", dataIndex: "keyword", width: 160 },
                        {
                          title: "严重级",
                          dataIndex: "severity",
                          width: 130,
                          render: (v: string) => <Tag color={severityColor(v)}>{severityLabel(v)}</Tag>,
                        },
                        {
                          title: "位置",
                          width: 110,
                          render: (_: unknown, row: ComplianceHit) => `${row.start}~${row.end}`,
                        },
                        { title: "原因/建议", dataIndex: "reason" },
                        {
                          title: "规则 ID",
                          dataIndex: "rule_id",
                          width: 200,
                          render: (v: string | null | undefined) => v ?? "-",
                        },
                      ]}
                    />
                  </Card>
                  <Card size="small" title="高亮预览（命中片段用【】标出）">
                    <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
                      {highlightHits(previewText, preview.hits)}
                    </Typography.Paragraph>
                  </Card>
                </>
              ) : null}
            </>
          ) : null}
        </Space>
      ) : null}

      {tab === "snapshot" ? (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message={`下一次生成会下发的快照：${snapshot.data?.words_count ?? 0} 个生效词 + ${snapshot.data?.rules_count ?? 0} 条正则`}
            description="排查「规则配了却没拦」时先看这里：最常见原因是词的时效窗口（未生效/已过期）。"
          />
          <Card size="small" title="生效词">
            <Space wrap>
              {(snapshot.data?.snapshot.words ?? []).map((w) => (
                <Tag key={w.id ?? w.word} color={severityColor(w.severity)}>
                  {w.word}
                </Tag>
              ))}
              {(snapshot.data?.snapshot.words ?? []).length === 0 ? (
                <Typography.Text type="secondary">（当前没有生效词）</Typography.Text>
              ) : null}
            </Space>
          </Card>
          <Card size="small" title="正则规则">
            <Table
              rowKey={(row) => row.id ?? row.pattern}
              size="small"
              pagination={false}
              dataSource={snapshot.data?.snapshot.rules ?? []}
              columns={[
                { title: "正则", dataIndex: "pattern" },
                { title: "建议", dataIndex: "suggestion", render: (v: string | null) => v ?? "-" },
              ]}
            />
          </Card>
        </Space>
      ) : null}

      {/* 词条编辑弹窗 */}
      <Modal
        title={wordEditor?.mode === "edit" ? `编辑词条：${wordEditor.word?.word}` : "新增词条"}
        open={wordEditor !== null}
        onCancel={() => setWordEditor(null)}
        footer={null}
        destroyOnHidden
      >
        <Form
          layout="vertical"
          initialValues={
            wordEditor?.mode === "edit" && wordEditor.word
              ? {
                  word: wordEditor.word.word,
                  severity: wordEditor.word.severity,
                  source: wordEditor.word.source ?? undefined,
                }
              : { severity: "high" }
          }
          onFinish={(values) => saveWord.mutate({ ...values, id: wordEditor?.word?.id })}
        >
          <Form.Item label="词面" name="word" rules={[{ required: true, message: "请输入词面" }]}>
            <Input placeholder="如「国家级」「最便宜」" />
          </Form.Item>
          <Form.Item label="严重级" name="severity" rules={[{ required: true }]}>
            <Select options={SEVERITY_OPTIONS} />
          </Form.Item>
          <Form.Item label="来源/依据" name="source">
            <Input placeholder="如「广告法第九条」" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={saveWord.isPending}>
            保存
          </Button>
          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
            high / medium 命中即阻断（fail-fast 让模型重写）；low 仅扣分提示。词面重复会被拒（409）。
          </Typography.Paragraph>
        </Form>
      </Modal>

      {/* 规则编辑弹窗 */}
      <Modal
        title={ruleEditor?.mode === "edit" ? "编辑正则规则" : "新增正则规则"}
        open={ruleEditor !== null}
        onCancel={() => setRuleEditor(null)}
        footer={null}
        destroyOnHidden
      >
        <Form
          layout="vertical"
          initialValues={
            ruleEditor?.mode === "edit" && ruleEditor.rule
              ? {
                  pattern: ruleEditor.rule.pattern,
                  severity: ruleEditor.rule.severity,
                  suggestion: ruleEditor.rule.suggestion ?? undefined,
                }
              : { severity: "high" }
          }
          onFinish={(values) => saveRule.mutate({ ...values, id: ruleEditor?.rule?.id })}
        >
          <Form.Item
            label="正则表达式"
            name="pattern"
            rules={[{ required: true, message: "请输入正则" }]}
            extra="Python 正则语法（与 ai-engine 使用的 re 模块同语义）；语法错误会在保存时被拒。"
          >
            <Input placeholder="如 (第一|顶级|100%)" />
          </Form.Item>
          <Form.Item label="严重级" name="severity" rules={[{ required: true }]}>
            <Select options={SEVERITY_OPTIONS} />
          </Form.Item>
          <Form.Item label="改写建议" name="suggestion" extra="命中时给模型的改写方向（会显示在审批详情里）">
            <Input placeholder="如「改为客观描述，如销量领先」" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={saveRule.isPending}>
            保存
          </Button>
        </Form>
      </Modal>
    </div>
  );
}


