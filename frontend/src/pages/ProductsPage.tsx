// 商品管理：列表 + 新建/编辑 + CSV 批量导入 + 图片上传（OSS 直传）+ 彻底删除（admin）。
//
// 与 backend 契约对齐的三处关键点（与 ProductPilot 版不同）：
//   1) **删除只有彻底删除一条路径**：软删端点已下线（`DELETE /products/{id}` → 405）。
//      因此列表不再有「软删」按钮；彻底删除要求 admin + 填写原因（审计留痕），
//      并且要回显 `purge_enqueued`：行已删但 AI 域清理消息可能没投出去（需运维补投）。
//   2) **OSS 预签名必须带 product_id**：即图片上传只能在「商品已存在」时进行
//      （新建弹窗里没有上传控件，建完商品再进编辑上传）。
//   3) **CSV 结果是 PA 形状**：成功 `{created, skus}`；失败 400 + `data.row_errors[{line,sku_code,reason}]`
//      （逐行原因在 data 里，不在 message 里）。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  List,
  message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ossApi, productsApi } from "../api";
import { apiErrorMessage, csvRowErrors, type CsvRowError } from "../services/errors";
import {
  PRODUCT_STATUS_FILTERS,
  STOCK_OPTIONS,
  productStatusColor,
  productStatusLabel,
  stockStatusLabel,
} from "../services/productMeta";
import { isAdmin, canWriteProducts, useAuthStore } from "../store/authStore";
import type { CsvImportResult, Product } from "../types/api";

interface ProductFormValues {
  sku_code?: string;
  title: string;
  base_price: number;
  stock_status: string;
}

/** 允许的图片类型（与 backend `services/oss.ALLOWED_CONTENT_TYPES` 对齐）。 */
const IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
/** 单图上限（与 backend `BACKEND_OSS_MAX_UPLOAD_BYTES` 默认 10MB 对齐；以接口返回的为准）。 */
const FALLBACK_MAX_BYTES = 10 * 1024 * 1024;
/** 前端预检的 CSV 行数上限。比 backend `csv_import.CSV_MAX_ROWS`（2000）**更严**：
 *  客户端先拦住大文件，避免用户上传后白等一轮才被服务端拒。 */
const CSV_MAX_ROWS = 500;

/** 商品列表 + 新建/编辑/CSV 导入/触发生成（写操作按角色禁用，真正的拒绝在服务端）。 */
export default function ProductsPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const role = useAuthStore((s) => s.user?.role);
  const writable = canWriteProducts(role);
  const admin = isAdmin(role);

  const [editor, setEditor] = useState<{ mode: "create" | "edit"; product?: Product } | null>(null);
  const [csvResult, setCsvResult] = useState<CsvImportResult | null>(null);
  const [csvErrors, setCsvErrors] = useState<CsvRowError[]>([]);
  const [purgeTarget, setPurgeTarget] = useState<{ id: string; sku_code: string } | null>(null);
  const [purgeReason, setPurgeReason] = useState("");
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [page, setPage] = useState(1);
  const pageSize = 10;

  const { data, isLoading } = useQuery({
    queryKey: ["products", page, statusFilter],
    queryFn: () => productsApi.list({ offset: (page - 1) * pageSize, limit: pageSize, status: statusFilter }),
  });
  const products = data?.items ?? [];
  const total = data?.total ?? 0;
  useEffect(() => {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    if (page > pages) setPage(pages);
  }, [total, page]);

  const save = useMutation({
    mutationFn: (values: ProductFormValues & { id?: string }) => {
      const payload = {
        title: values.title,
        base_price: String(values.base_price),
        stock_status: values.stock_status,
      };
      return values.id
        ? productsApi.update(values.id, payload)
        : productsApi.create({ ...payload, sku_code: values.sku_code });
    },
    onSuccess: (p) => {
      message.success(editor?.mode === "edit" ? "商品已更新" : `商品已创建：${p.sku_code}`);
      setEditor(null);
      qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "保存失败，请检查后重试")),
  });

  const purge = useMutation({
    mutationFn: (v: { id: string; reason: string }) => productsApi.purge(v.id, v.reason),
    onSuccess: (result) => {
      // purge_enqueued=false 时行已删、AI 域清理未投出 —— 必须显式告诉用户/运维
      if (result.purge_enqueued) {
        message.success("商品已彻底删除（含 AI 内容、审批记录与上传图对象）");
      } else {
        message.warning("商品已彻底删除，但 AI 域清理消息投递失败：请运维补投或人工清理 AI 侧数据");
      }
      setPurgeTarget(null);
      setPurgeReason("");
      qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "彻底删除失败")),
  });

  // ---------- CSV 导入 ----------
  const importCsv = useMutation({
    mutationFn: (file: File) => productsApi.importCsv(file),
    onSuccess: (result) => {
      setCsvResult(result);
      setCsvErrors([]);
      message.success(`导入成功：新增 ${result.created} 个商品`);
      qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: (e) => {
      const rows = csvRowErrors(e);
      setCsvResult(null);
      setCsvErrors(rows);
      message.error(apiErrorMessage(e, "导入失败"));
    },
  });

  /** 上传前预检：类型/大小 + 表头（不合格直接拦在本地，省一次往返）。 */
  const beforeUpload = async (file: File): Promise<boolean> => {
    if (!file.name.toLowerCase().endsWith(".csv")) {
      message.error("请选择 .csv 文件");
      return false;
    }
    const text = await file.text().catch(() => "");
    const headerLine = text.split(/\r?\n/)[0] ?? "";
    const headers = headerLine
      .split(",")
      .map((h) => h.trim().replace(/^"|"$/g, "").toLowerCase())
      .filter(Boolean);
    const missing = ["sku_code", "title"].filter((h) => !headers.includes(h));
    if (missing.length > 0) {
      message.error(`表头缺少必需列：${missing.join(" / ")}（当前：${headers.join(" / ")}）`);
      return false;
    }
    const dataRows = text.split(/\r?\n/).filter((line) => line.trim().length > 0).length - 1;
    if (dataRows > CSV_MAX_ROWS) {
      message.error(`单次最多导入 ${CSV_MAX_ROWS} 行（当前 ${dataRows} 行）`);
      return false;
    }
    importCsv.mutate(file);
    return false; // 关闭 antd 默认上传，由后端接口处理
  };

  /** 下载导入模板（表头与 backend csv_import.REQUIRED_HEADERS 一致）。 */
  const downloadTemplate = () => {
    const lines = [
      "sku_code,title,base_price,stock_status,raw_images",
      'SKU-001,示例商品 A,199.00,in_stock,',
      'SKU-002,示例商品 B,99.50,preorder,https://example.com/a.png|https://example.com/b.png',
    ];
    const blob = new Blob([String.fromCharCode(0xfeff) + lines.join(String.fromCharCode(10))], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "商品批量导入模板.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const columns = [
    { title: "SKU", dataIndex: "sku_code" },
    { title: "标题", dataIndex: "title", ellipsis: true },
    { title: "价格", dataIndex: "base_price", render: (v: number) => `¥ ${v}` },
    { title: "库存", dataIndex: "stock_status", render: (v: string) => stockStatusLabel(v) },
    {
      title: "状态",
      dataIndex: "status",
      render: (v: string) => <Tag color={productStatusColor(v)}>{productStatusLabel(v)}</Tag>,
    },
    {
      title: "操作",
      fixed: "right" as const,
      render: (_: unknown, p: Product) => (
        <Space>
          <Button size="small" onClick={() => navigate(`/products/${p.id}`)}>
            详情
          </Button>
          <Button
            size="small"
            disabled={!writable || p.status === "deleted"}
            onClick={() => setEditor({ mode: "edit", product: p })}
          >
            编辑
          </Button>
          {admin ? (
            <Button
              size="small"
              danger
              onClick={() => {
                setPurgeTarget({ id: p.id, sku_code: p.sku_code });
                setPurgeReason("");
              }}
            >
              彻底删除
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12, width: "100%", justifyContent: "space-between" }} wrap>
        <Space wrap>
          <Typography.Title level={4} style={{ margin: 0 }}>
            商品管理
          </Typography.Title>
          <Select
            allowClear
            placeholder="全部状态"
            style={{ width: 160 }}
            value={statusFilter}
            onChange={(v) => {
              setStatusFilter(v);
              setPage(1);
            }}
            options={[
              ...PRODUCT_STATUS_FILTERS,
              // 历史已删行（软删功能已下线，仅历史数据可能出现）：只给 admin 一个只读入口
              ...(admin ? [{ value: "deleted", label: "已删除（历史）" }] : []),
            ]}
          />
        </Space>
        <Space wrap>
          <Upload beforeUpload={beforeUpload} showUploadList={false} accept=".csv" disabled={!writable}>
            <Button loading={importCsv.isPending} disabled={!writable}>
              CSV 导入
            </Button>
          </Upload>
          <Button onClick={downloadTemplate}>下载模板</Button>
          <Button type="primary" disabled={!writable} onClick={() => setEditor({ mode: "create" })}>
            新建商品
          </Button>
        </Space>
      </Space>

      {!writable ? (
        <Alert
          type="info"
          showIcon
          message="当前角色为只读（审核员）"
          description="审核员可以查看商品与内容，但不能新建/编辑/导入/触发生成。"
          style={{ marginBottom: 12 }}
        />
      ) : null}

      {csvResult ? (
        <Alert
          type="success"
          showIcon
          closable
          onClose={() => setCsvResult(null)}
          message={`CSV 导入成功：新增 ${csvResult.created} 个商品`}
          description={<Typography.Text type="secondary">SKU：{csvResult.skus.join("、")}</Typography.Text>}
          style={{ marginBottom: 12 }}
        />
      ) : null}

      {csvErrors.length > 0 ? (
        <Alert
          type="error"
          showIcon
          closable
          onClose={() => setCsvErrors([])}
          message="CSV 导入失败：逐行原因如下（修正后重新上传，本次未导入任何数据）"
          style={{ marginBottom: 12 }}
          description={
            <List
              size="small"
              dataSource={csvErrors}
              renderItem={(row) => (
                <List.Item>
                  第 {row.line} 行{row.sku_code ? `（${row.sku_code}）` : ""}：{row.reason}
                </List.Item>
              )}
            />
          }
        />
      ) : null}

      <Table<Product>
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={products}
        scroll={{ x: 900 }}
        pagination={{ current: page, pageSize, total, onChange: setPage, showSizeChanger: false }}
      />

      {/* 新建/编辑弹窗（SKU 不可改：它是组织内唯一键） */}
      <Modal
        title={editor?.mode === "edit" ? `编辑商品：${editor.product?.sku_code}` : "新建商品"}
        open={editor !== null}
        onCancel={() => setEditor(null)}
        footer={null}
        destroyOnHidden
      >
        <Form<ProductFormValues>
          layout="vertical"
          onFinish={(values) => save.mutate({ ...values, id: editor?.product?.id })}
          initialValues={
            editor?.mode === "edit" && editor.product
              ? {
                  title: editor.product.title,
                  base_price: editor.product.base_price,
                  stock_status: editor.product.stock_status,
                }
              : { base_price: 0, stock_status: "in_stock" }
          }
        >
          {editor?.mode === "create" ? (
            <Form.Item label="SKU 编码" name="sku_code" rules={[{ required: true, message: "请输入 SKU" }]}>
              <Input placeholder="组织内唯一，如 SKU-001" />
            </Form.Item>
          ) : null}
          <Form.Item label="标题" name="title" rules={[{ required: true, message: "请输入标题" }]}>
            <Input placeholder="商品标题" />
          </Form.Item>
          <Form.Item label="售价（元）" name="base_price" rules={[{ required: true, message: "请输入售价" }]}>
            <InputNumber min={0} step={1} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item label="库存状态" name="stock_status" rules={[{ required: true }]}>
            <Select options={STOCK_OPTIONS} />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={save.isPending}>
            保存
          </Button>
          <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
            {editor?.mode === "create"
              ? "商品图上传需在商品创建后进行（OSS 预签名接口要求 product_id）：保存后进入「编辑」即可上传。"
              : "商品图上传在详情页/编辑页的上传控件中进行：先换预签名 URL，浏览器直传 OSS，再把公有 URL 写回商品。"}
          </Typography.Paragraph>
        </Form>
      </Modal>

      {/* 彻底删除（唯一删除路径）：原因必填 —— 审计留痕由 backend 写 delete_audits */}
      <Modal
        title={purgeTarget ? `彻底删除商品：${purgeTarget.sku_code}` : "彻底删除商品"}
        open={purgeTarget !== null}
        onCancel={() => setPurgeTarget(null)}
        okText="确认彻底删除"
        okButtonProps={{ danger: true, disabled: purgeReason.trim().length === 0 }}
        confirmLoading={purge.isPending}
        onOk={() => purgeTarget && purge.mutate({ id: purgeTarget.id, reason: purgeReason })}
      >
        <Alert
          type="error"
          showIcon
          message="这是物理删除，不可恢复"
          description="商品行、生成任务、审批记录、上传图对象与 AI 域数据都会被清理（审计记录会保留）。"
          style={{ marginBottom: 12 }}
        />
        <Input.TextArea
          rows={3}
          value={purgeReason}
          onChange={(e) => setPurgeReason(e.target.value)}
          placeholder="删除原因（必填，将写入审计表：如「重复录入」「用户要求删除」）"
        />
      </Modal>
    </div>
  );
}


