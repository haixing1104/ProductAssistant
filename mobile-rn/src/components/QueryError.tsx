// 查询失败态 —— 与 H5 的 `components/QueryError.tsx` 同口径。
//
// 为什么单独抽一个组件: 移动端最常见的三种失败（弱网 / 会话过期 / 依赖 503）必须给出**可执行动作**（重试），
// 而不是把「没有数据」和「请求失败」混成同一个空列表 —— 后者会让用户以为"这里本来就没内容"。
import Button from "../ui/Button";
import { EmptyState } from "../ui/List";
import { apiErrorMessage } from "@pa/core/services/errors";

interface Props {
  error: unknown;
  onRetry?: () => void;
  /** 失败语义（"审批详情加载"），会拼进标题 */
  what: string;
}

export default function QueryError({ error, onRetry, what }: Props) {
  return (
    <EmptyState
      tone="error"
      title={`${what}失败`}
      description={apiErrorMessage(error, "网络异常或服务暂不可用")}
      action={onRetry ? <Button variant="primary" fill="outline" onPress={onRetry}>重新加载</Button> : undefined}
      testID="pa-query-error"
    />
  );
}
