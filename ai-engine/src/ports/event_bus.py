"""EventBus 端口：事件发布边界（真实实现为 Redis Streams XADD）。

职责:
    定义「图执行过程事件」的出站发布边界：worker / 节点只依赖本抽象投递阶段、内容、
    终态事件；实现方负责落盘（实现 = adapters/redis_eventbus.RedisEventBus，写入
    `evt:{thread_id}` 事件流；测试可注入内存 mock 实现）。
    本端口是**纯发布面**（只有 publish，无读接口）：消费、回放、ack 由 backend 侧
    SSE 读流完成。

约束:
    · payload 必须是 JSON 可序列化的 dict（实现方按 JSON 信封落盘）；
    · topic 命名约定 `evt:{thread_id}`——它是前端 SSE 实时过程（打字机、阶段提示、
      图片就绪、终态信号）与断线回放的数据源；
    · 发布失败必须上抛（连接错 / 序列化错），不得静默吞：事件缺失在前端只会表现为
      「莫名卡住」，属最难排查的一类故障；
    · 同一 topic 内消息顺序 = publish 调用顺序（由实现方保证）。

说明:
    · 版本纪律以本模块为单一事实源（出站消息信封统一携带 schema_version），消费端需与之对齐；
    · 消费端（backend-api/）尚未落地，`schema_version` 的容忍/降级逻辑目前只有生产端
      一处实现，升版本时需同步补消费端。

事件协议版本：
  · 所有跨进程消息（`evt:*` / `job:*` / `result:workflow`）在信封里统一携带 `schema_version`；
  · **加字段 = 向后兼容**（老消费端忽略未知字段，无需升版本）；**改语义/删字段 = 必须升版本号**，
    消费端据版本号决定降级或告警，避免「静默错读」，
  · 版本号由生产端单一常量注入（本模块），避免各节点各写一套导致漂移。
"""

from abc import ABC, abstractmethod
from typing import Any

#: 当前事件信封版本。升版本 = 语义变更（不是加字段），须同步消费端容忍逻辑。
EVENT_SCHEMA_VERSION = 1


def with_schema_version(payload: dict[str, Any], version: int = EVENT_SCHEMA_VERSION) -> dict[str, Any]:
    """给消息信封补 `schema_version`。

    参数:
        payload: 事件载荷（JSON 可序列化 dict）；本函数不修改该对象。
        version: 注入的版本号，默认取 EVENT_SCHEMA_VERSION；仅供回放/测试定向注入。
    返回:
        含 `schema_version` 的**新 dict**（浅拷贝）；若入参已显式携带 `schema_version`，
        则**原样返回同一对象**（不覆盖）——回放历史消息/测试定向注入依赖这一行为。
    注意:
        新键插在字典最前，便于人肉查看与日志比对；语义上键顺序无要求，消费端不得依赖。
    """
    if "schema_version" in payload:
        return payload
    return {"schema_version": version, **payload}


class EventBus(ABC):
    """事件发布端口（出站）：把图过程事件写进事件流供前端 SSE 消费。

    实现契约:
        · publish 正常返回即代表事件已落盘（如 Redis XADD 已被服务端确认）；
        · 不得吞异常：连接不可用、载荷不可序列化时必须向上抛，由调用方决定重试或降级；
        · 同一 topic 内保持调用顺序；
        · `schema_version` 由实现方注入（调用方无需自填，见 with_schema_version）。
    注意:
        本端口不含读接口与消费组语义：消费顺序、ack、重投策略属于消费端（backend）职责。
    """

    @abstractmethod
    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """向指定 keyspace（如 `evt:{thread_id}`）发布一条事件。

        参数:
            topic: 目标 topic，约定为 `evt:{thread_id}`；`evt:` 前缀由实现方归一化，
                也兼容直接传裸 thread_id。
            payload: 事件载荷（JSON 可序列化 dict）；实现方会补 `schema_version`。
        返回:
            None。消息 ID 有意不外露：若调用方需要游标（断点回放/对账）应先扩展本端口契约。
        异常:
            Exception: 连接失败、payload 不可 JSON 序列化等实现方透出的异常（不得静默吞）。
        注意:
            发布是「追加上」语义：同一条逻辑事件重复 publish 会产生两条消息，
            去重责任在消费端（按 stream ID 或业务幂等键），引擎侧不做去重。
        """
