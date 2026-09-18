// 商品新建/编辑（RN 版）—— 与 H5 的 `ProductEditorPopup` 同口径（底部弹层 + 4 个字段）。
//
// 为什么是底部弹层而不是整屏页: 只有 SKU/标题/售价/库存四个字段，弹层能保留列表上下文
//（改完一眼能看到卡片变化），也避免键盘弹出后整屏重排。
import { useEffect, useState } from "react";
import { ScrollView, StyleSheet, View } from "react-native";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import Button from "../ui/Button";
import Chips from "../ui/Chips";
import { LabeledInput } from "../ui/Field";
import { Sheet } from "../ui/Sheet";
import { space } from "../ui/theme";
import { Toast } from "../ui/feedback";
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

/** 空表单初值（新建时用；编辑时由 `useEffect` 灌入目标商品）。 */
const EMPTY: Values = { sku_code: "", title: "", base_price: "", stock_status: "in_stock" };

/** 商品新建/编辑底部弹层（`target` 为受控开关：null=关闭，「new」=新建，Product=编辑）。 */
export default function ProductEditorSheet({ target, onClose, onSaved }: Props) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Values>(EMPTY);
  const [errors, setErrors] = useState<Partial<Record<keyof Values, string>>>({});
  const editing = target && target !== "new" ? target : null;
  const visible = target !== null;

  useEffect(() => {
    if (!visible) return;
    if (editing) {
      setValues({
        sku_code: editing.sku_code,
        title: editing.title,
        base_price: String(editing.base_price),
        stock_status: editing.stock_status,
      });
    } else {
      setValues(EMPTY);
    }
    setErrors({});
  }, [visible, editing]);

  const save = useMutation({
    mutationFn: (payload: Values) => {
      const body = {
        sku_code: payload.sku_code.trim(),
        title: payload.title.trim(),
        base_price: Number(payload.base_price),
        stock_status: payload.stock_status,
      };
      return editing ? productsApi.update(editing.id, body) : productsApi.create(body);
    },
    onSuccess: (saved) => {
      Toast.show({ icon: "success", content: editing ? "已保存" : `已创建 ${saved.sku_code}` });
      qc.invalidateQueries({ queryKey: ["products"] });
      qc.invalidateQueries({ queryKey: ["product", saved.id] });
      onSaved();
      onClose();
    },
    onError: (error) => Toast.show({ icon: "fail", content: apiErrorMessage(error, "保存失败") }),
  });

  /** 本地校验（与 H5 的 Form rules 同口径）：缺字段不发请求，错误就地显示。 */
  const submit = () => {
    const next: Partial<Record<keyof Values, string>> = {};
    if (!values.sku_code.trim()) next.sku_code = "请输入 SKU";
    if (!values.title.trim()) next.title = "请输入标题";
    if (!values.base_price.trim()) next.base_price = "请输入售价";
    else if (Number.isNaN(Number(values.base_price))) next.base_price = "售价必须是数字";
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    save.mutate(values);
  };

  return (
    <Sheet visible={visible} onClose={onClose} testID="pa-product-editor">
      <ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={styles.body}>
        <LabeledInput
          label="SKU"
          value={values.sku_code}
          onChangeText={(text) => setValues((current) => ({ ...current, sku_code: text }))}
          placeholder="如 SKU-001"
          error={errors.sku_code}
          // 编辑时 SKU 是业务主键，不允许就地改（改 SKU 等于换商品）
          editable={!editing}
          autoCapitalize="characters"
          testID="pa-editor-sku"
        />
        <LabeledInput
          label="标题"
          value={values.title}
          onChangeText={(text) => setValues((current) => ({ ...current, title: text }))}
          placeholder="商品标题（合规闸门会预检此字段）"
          error={errors.title}
          testID="pa-editor-title"
        />
        <LabeledInput
          label="售价"
          value={values.base_price}
          onChangeText={(text) => setValues((current) => ({ ...current, base_price: text }))}
          placeholder="如 199"
          error={errors.base_price}
          keyboardType="numeric"
          testID="pa-editor-price"
        />
        <View>
          <Chips
            options={STOCK_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
            value={values.stock_status}
            onChange={(value) => setValues((current) => ({ ...current, stock_status: value }))}
            testID="pa-editor-stock"
          />
        </View>
        <Button block variant="primary" size="large" loading={save.isPending} onPress={submit} testID="pa-editor-save">
          {editing ? "保存修改" : "创建商品"}
        </Button>
        <Button block fill="none" onPress={onClose} style={styles.cancel}>
          取消
        </Button>
      </ScrollView>
    </Sheet>
  );
}

const styles = StyleSheet.create({
  body: { padding: space.md, paddingBottom: space.lg },
  cancel: { marginTop: space.sm },
});
