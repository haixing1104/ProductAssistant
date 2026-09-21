"""``pa:{env}:*`` keyspace 单点（backend 侧唯一出处）。

为什么 backend 要再写一份（而不是 import ai-engine 的实现）:
    仓库红线：backend-api 与 ai-engine 是**可独立替换语言实现**的两个模块，不共享代码
    （模块之间互相解耦，方便更换其他语言实现）。所以 keyspace 只能靠
    「同名同义 + 两侧各自单点」维持一致：本文件是 backend 侧唯一出处，改键名必须同步
    ``ai-engine/src/adapters/redis_eventbus.py::RedisKeys`` —— 否则链路会**静默断掉**
    （投出去没人消费 / 等一个永远不来的结果），这类故障最难排查。

两类键，严格区分:
    ① **契约键**（跨模块，两侧同名同义）：
       job:generate / job:approval / job:product_purge / result:workflow / evt:{tid} / dlq:{domain}
       其中 job:* 由 backend 写、ai-engine 读；result:workflow / evt:* 由 ai-engine 写、backend 读。
    ② **backend 私有键**（ai-engine 完全不感知）：会话 refresh 三件套与登录限流计数。
       私有键也放这里，是为了让「本模块用到的所有 Redis 键」只有一处可查。

红线:
    ``lock:{thread_id}`` 与 ``done:{thread_id}`` 属 worker 内部幂等/互斥语义（ai-engine 侧
    ``acquire_lock`` / ``mark_done``）。**backend 不得读写**：碰了会破坏「谁在处理这条消息」的
    判定（例如误删锁会导致审批被并行 resume）。故本类**不提供**这两个方法。
"""

from __future__ import annotations

__all__ = ["RedisKeys"]


class RedisKeys:
    """keyspace 模板 ``pa:{env}:{domain}:{suffix}`` 的构造器。"""

    def __init__(self, env: str) -> None:
        """初始化。

        参数:
            env: 环境标识（来自 ``PA_ENV``）；必须与 ai-engine 工作进程一致，否则两侧看到的是两套队列。
        """
        self.env = env

    # ---------------------------------------------------------------- 契约键
    def job_generate(self) -> str:
        """生成任务流（backend → ai-engine）：``pa:{env}:job:generate``。"""
        return f"pa:{self.env}:job:generate"

    def job_approval(self) -> str:
        """审批回传流（backend → ai-engine）：``pa:{env}:job:approval``。"""
        return f"pa:{self.env}:job:approval"

    def job_product_purge(self) -> str:
        """商品彻底删除流（backend → ai-engine）：``pa:{env}:job:product_purge``。"""
        return f"pa:{self.env}:job:product_purge"

    def workflow_result(self) -> str:
        """图终态结果流（ai-engine → backend）：``pa:{env}:result:workflow``。"""
        return f"pa:{self.env}:result:workflow"

    def evt(self, thread_id: str) -> str:
        """单线程事件流（ai-engine → backend SSE）：``pa:{env}:evt:{thread_id}``。"""
        return f"pa:{self.env}:evt:{thread_id}"

    def dlq(self, domain: str) -> str:
        """死信流（worker → 运维）：``pa:{env}:dlq:{domain}``（如 ``domain='job:generate'``）。"""
        return f"pa:{self.env}:dlq:{domain}"

    def heartbeat(self, consumer: str) -> str:
        """消费心跳键（worker → 运维）：``pa:{env}:worker:heartbeat:{consumer}``。

        注意：键**过期即表示没有进程在消费**（TTL 默认 30s），运维面据此告警（P6）。
        """
        return f"pa:{self.env}:worker:heartbeat:{consumer}"

    # ------------------------------------------------------- backend 私有键
    def auth_refresh_session(self, token: str) -> str:
        """refresh 会话键（值 = 该会话的 claims JSON）：``pa:{env}:auth:refresh:{token}``。"""
        return f"pa:{self.env}:auth:refresh:{token}"

    def auth_refresh_owner(self, token: str) -> str:
        """refresh → user_id 反查键（会话被删除后仍能识别「重用者」）：``pa:{env}:auth:refresh:owner:{token}``。"""
        return f"pa:{self.env}:auth:refresh:owner:{token}"

    def auth_refresh_user_set(self, user_id: str) -> str:
        """用户 → 其全部 refresh 会话集合（被盗时整租户会话一键吊销）：``pa:{env}:auth:refresh:user:{user_id}``。"""
        return f"pa:{self.env}:auth:refresh:user:{user_id}"

    def auth_login_fail(self, subject: str) -> str:
        """登录失败计数（窗口内累计）：``pa:{env}:auth:loginfail:{subject}``（subject = ``username:ip``）。"""
        return f"pa:{self.env}:auth:loginfail:{subject}"

    def auth_login_lock(self, subject: str) -> str:
        """登录锁定标记（存在即锁定中）：``pa:{self.env}:auth:loginlock:{subject}``。"""
        return f"pa:{self.env}:auth:loginlock:{subject}"

    def approval_redrive(self, approval_id: str) -> str:
        """审批补投节流键：``pa:{env}:approval:redrive:{approval_id}``。

        为什么要节流：补投守护会周期性扫描「已定案但商品仍停在待审批」的单子；
        不加节流就会在每次扫描里重复投递同一条 ``job:approval``（worker 反复 resume、
        日志与线程锁竞争被放大）。键存在 = 最近已补投过，跳过；TTL = 重投频率上限。
        """
        return f"pa:{self.env}:approval:redrive:{approval_id}"

