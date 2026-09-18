"""一次性运维脚本（不参与请求链路）。

与 ``services`` / ``repositories`` 的区别:
    这些脚本由运维**手工执行**（部署/初始化时），因此:
      · 不进 FastAPI 依赖注入，不引入任何运行期状态；
      · 允许直接读 ``os.getenv``（读取的键仍须在 ``infra/.env.template`` 登记，
        ``infra/scripts/env-check.sh`` 以源码扫描为准，不会被漏掉）；
      · 一律**幂等**：重复执行不应产生重复数据，也不应覆盖已轮换的凭据（除非显式要求）。

当前内容:
    · ``seed_super_admin`` —— 引导平台超管（组织 + admin 账号 + ``is_superuser`` 标记）。
"""
