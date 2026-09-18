// 查询失败态（替代桌面端的 `<Alert type="error">` + 表格空态）。
//
// 为什么单独抽一个组件: 移动端最常见的三种失败（弱网 / 会话过期 / 依赖 503）必须给出**可执行动作**
// （重试），而不是把「没有数据」和「请求失败」混成同一个空列表 —— 后者会让用户以为"这里本来就没内容"。
import { Button, ErrorBlock } from "antd-mobile";

import { apiErrorMessage } from "@pa/core/services/errors";

interface Props {
  error: unknown;
  onRetry?: () => void;
  /** 失败语义（"审批详情加载失败"），会拼进描述里 */
  what: string;
}

/** 查询失败态：把「请求失败」与「没有数据」区分开，并给出可执行的重试动作。 */
export default function QueryError({ error, onRetry, what }: Props) {
  return (
    <ErrorBlock
      status="disconnected"
      title={`${what}失败`}
      description={apiErrorMessage(error, "网络异常或服务暂不可用")}
      style={{ margin: "24px 0" }}
    >
      {onRetry ? (
        <Button color="primary" fill="outline" size="small" onClick={onRetry}>
          重新加载
        </Button>
      ) : null}
    </ErrorBlock>
  );
}
