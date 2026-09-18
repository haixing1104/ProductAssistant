// 商品新建/编辑（移动版）：底部 `Popup` + `Form`。
//
// 为什么是底部弹层而不是整屏页: 只有 4 个字段（SKU/标题/售价/库存），底部弹层能保留
// 列表上下文（用户改完一眼能看到卡片变化），也避免手机键盘弹出后整屏重排。
import { Button, Form, Input, Popup, Selector, Toast } from "antd-mobile";
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { productsApi } from "@pa/core/api";
import { apiErrorMessage } from "@pa/core/services/errors";
import { STOCK_OPTIONS } from "@pa/core/services/productMeta";
import type { Product } from "@pa/core/types/api";

interface Props {
  /** `"new"` = 新建；`Product` = 编辑；`null` = 关闭 */
  target: Product | "new" | null;
  onClose: () => void;
  onSaved: () => void;
}

interface Values {
  sku_code: string;
  title: string;
  base_price: string;
  stock_status: string;
}

/** 商品新建/编辑弹层（`target` 为受控开关：null=关闭，「new」=新建，Product=编辑）。 */
export default function ProductEditorPopup({ target, onClose, onSaved }: Props) {
  const qc = useQueryClient();
  const [form] = Form.useForm<Values>();
  const [stock, setStock] = useState<string>("in_stock");
  const editing = target && target !== "new" ? target : null;
  const visible = target !== null;

  useEffect(() => {
    if (!visible) return;
    if (editing) {
      form.setFieldsValue({
        sku_code: editing.sku_code,
        title: editing.title,
        base_price: String(editing.base_price),
        stock_status: editing.stock_status,
      });
      setStock(editing.stock_status);
    } else {
      form.resetFields();
      setStock("in_stock");
    }
  }, [visible, editing, form]);

  const save = useMutation({
    mutationFn: (values: Values) => {
      const payload = {
        sku_code: values.sku_code.trim(),
        title: values.title.trim(),
        base_price: Number(values.base_price),
        stock_status: stock,
      };
      return editing ? productsApi.update(editing.id, payload) : productsApi.create(payload);
    },
    onSuccess: (saved) => {
      Toast.show({ icon: "success", content: editing ? "已保存" : `已创建 ${saved.sku_code}` });
      qc.invalidateQueries({ queryKey: ["products"] });
      qc.invalidateQueries({ queryKey: ["product", saved.id] });
      onSaved();
      onClose();
    },
    onError: (e) => Toast.show({ icon: "fail", content: apiErrorMessage(e, "保存失败") }),
  });

  return (
    <Popup
      visible={visible}
      onMaskClick={onClose}
      position="bottom"
      bodyStyle={{ maxHeight: "85vh", overflow: "auto", borderTopLeftRadius: 12, borderTopRightRadius: 12 }}
      destroyOnClose
    >
      <div style={{ padding: "12px 0 4px" }}>
        <Form
          form={form}
          mode="card"
          layout="horizontal"
          onFinish={(values) => save.mutate(values)}
          footer={
            <Button block type="submit" color="primary" size="large" loading={save.isPending}>
              {editing ? "保存修改" : "创建商品"}
            </Button>
          }
        >
          <Form.Item name="sku_code" label="SKU" rules={[{ required: true, message: "请输入 SKU" }]}>
            {/* 编辑时 SKU 通常作为业务主键，不允许就地改（改 SKU 等于换商品） */}
            <Input placeholder="如 SKU-001" disabled={Boolean(editing)} />
          </Form.Item>
          <Form.Item name="title" label="标题" rules={[{ required: true, message: "请输入标题" }]}>
            <Input placeholder="商品标题（合规闸门会预检此字段）" />
          </Form.Item>
          <Form.Item name="base_price" label="售价" rules={[{ required: true, message: "请输入售价" }]}>
            <Input placeholder="如 199" type="number" />
          </Form.Item>
          <Form.Item label="库存状态">
            <Selector
              columns={2}
              value={[stock]}
              onChange={(v) => setStock(v[v.length - 1] ?? "in_stock")}
              options={STOCK_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
            />
          </Form.Item>
        </Form>
        <div style={{ padding: "0 12px 12px", fontSize: 12, color: "#999" }}>
          标题里的违禁词（如「最便宜」「国家级」）会被合规预检直接拦下，生成任务不会启动。
        </div>
      </div>
    </Popup>
  );
}
