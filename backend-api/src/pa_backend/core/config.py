"""backend-api 配置（pydantic-settings）：全部经环境变量注入，禁止硬编码密钥。

为什么每个字段都显式写 ``validation_alias``（= 对应环境变量名）:
    ① **契约可见**：代码里能一眼看到「本模块读哪些键」，与 ``infra/.env.template`` 逐条对账；
    ② **可自检**：``infra/scripts/env-check.sh`` 以 ``validation_alias`` 为扫描锚点，
       新增键若忘了在模板登记，自检会以 WARN 报出来 —— 而不是等上线才发现读不到值
       （pydantic-settings 默认按字段名隐式映射，自检脚本看不见，等于契约裸奔）。

DSN 派生口径:
    ``BACKEND_PG_DSN`` 在 ``infra/.env.template`` 里是**已注释的可选覆盖项**；
    默认由 ``POSTGRES_*`` + ``ROLE_PA_BACKEND_PWD`` 拼装，避免同一份连接信息在 .env 里写两遍
    （改一处漏一处是运维事故的常见源头）。与 ai-engine ``__main__._derive_role_dsn`` 同一思路。

角色红线:
    backend-api **只以 role_pa_backend 连库**（schema_pa_backend 全 DML；schema_pa_ai 仅 SELECT）。
    派生函数把角色名写死，正是为了让「换角色」必须显式改代码/改 DSN，不能靠环境变量悄悄漂移。
"""

from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import quote

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 运行期数据库角色（与 database/sql/0001_schema.sql 的 CREATE ROLE 一致，勿改）
RUNTIME_DB_ROLE = "role_pa_backend"


def _derive_backend_dsn() -> str:
    """由 ``POSTGRES_*`` + ``ROLE_PA_BACKEND_PWD`` 拼装运行期 DSN。

    返回:
        SQLAlchemy async 口径的 DSN（``postgresql+psycopg://role_pa_backend:…@host:port/db``）。
    异常:
        RuntimeError: 未配置 ``ROLE_PA_BACKEND_PWD`` 且未显式给出 ``BACKEND_PG_DSN``。
            这里**必须响亮失败**：静默降级成空密码会在第一次查询时才报「认证失败」，
            排障要从 Web 层一路查到这里，代价高得多。
    注意:
        密码做 URL 编码（含 ``@ : / ? #`` 等字符的强口令不编码会把 DSN 解析成另一个主机）。
    """
    password = os.getenv("ROLE_PA_BACKEND_PWD")
    if not password:
        raise RuntimeError(
            "缺少 ROLE_PA_BACKEND_PWD（或显式设置 BACKEND_PG_DSN）；"
            "请先在 infra/.env 填好角色密码，再跑 ./infra/scripts/env-check.sh 自检"
        )
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    name = os.getenv("POSTGRES_DB", "productassistant")
    return (
        f"postgresql+psycopg://{RUNTIME_DB_ROLE}:{quote(password, safe='')}"
        f"@{host}:{port}/{name}"
    )


class Settings(BaseSettings):
    """环境变量 → 应用配置（键名一律与 infra/.env.template 登记的一致）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- 运行形态 ---
    env: str = Field(default="dev", validation_alias="PA_ENV")  # 参与 Redis 键前缀 pa:{env}:…
    port: int = Field(default=8000, validation_alias="BACKEND_PORT")
    cors_origins: str = Field(default="http://localhost:5173", validation_alias="BACKEND_CORS_ORIGINS")

    # --- 连接 ---
    database_url: str = Field(default="", validation_alias="BACKEND_PG_DSN")
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")

    # --- 会话（access 短时效 + refresh HttpOnly Cookie）---
    jwt_secret: str = Field(default="dev-only-change-me", validation_alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", validation_alias="BACKEND_JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=15, validation_alias="BACKEND_ACCESS_TOKEN_MINUTES")
    refresh_token_expire_days: int = Field(default=7, validation_alias="BACKEND_REFRESH_DAYS")
    refresh_cookie_name: str = Field(default="pa_refresh", validation_alias="BACKEND_REFRESH_COOKIE_NAME")
    # 生产必须 1（Cookie 仅 HTTPS 回传）；本地 http 开发必须 0，否则浏览器不落 Cookie
    cookie_secure: bool = Field(default=False, validation_alias="BACKEND_COOKIE_SECURE")

    # --- 自助注册（注册 = 新开租户 + 该租户首个 admin）---
    # 默认**关闭**：公网可自助开租户等于把多租户数据面对外开放；现阶段租户/账号由运维开通
    # （backend-api tools/seed_super_admin.py）。关闭时 POST /auth/register 直接 403。
    # 注意：前端入口隐藏只是体验层（frontend/src/services/features.ts），**授权以本开关为准**——
    # 只藏 UI 等于没关（curl 依然能开租户），所以两处必须同步。
    allow_registration: bool = Field(default=False, validation_alias="BACKEND_ALLOW_REGISTRATION")

    # --- SSE（evt:{thread_id} 回放/尾随）---
    stream_ticket_ttl_seconds: int = Field(default=120, validation_alias="BACKEND_STREAM_TICKET_TTL_SECONDS")
    stream_idle_seconds: int = Field(default=120, validation_alias="BACKEND_STREAM_IDLE_SECONDS")

    # --- 登录失败限流（维度 = 用户名 + 客户端 IP）---
    login_max_failures: int = Field(default=5, validation_alias="BACKEND_LOGIN_MAX_FAILURES")
    login_failure_window_seconds: int = Field(default=300, validation_alias="BACKEND_LOGIN_FAILURE_WINDOW_SECONDS")
    login_lockout_seconds: int = Field(default=900, validation_alias="BACKEND_LOGIN_LOCKOUT_SECONDS")

    # --- OSS 预签名直传 ---
    oss_endpoint: str = Field(default="", validation_alias="OSS_ENDPOINT")
    oss_bucket: str = Field(default="", validation_alias="OSS_BUCKET")
    oss_access_key_id: str = Field(default="", validation_alias="OSS_ACCESS_KEY_ID")
    oss_access_key_secret: str = Field(default="", validation_alias="OSS_ACCESS_KEY_SECRET")
    oss_presign_expire_seconds: int = Field(default=600, validation_alias="BACKEND_OSS_PRESIGN_EXPIRE_SECONDS")
    oss_max_upload_bytes: int = Field(default=10 * 1024 * 1024, validation_alias="BACKEND_OSS_MAX_UPLOAD_BYTES")

    # --- 审批通知（console = 仅打印；live = 真实投递，只对「配了凭据」的渠道生效）---
    notify_mode: str = Field(default="console", validation_alias="NOTIFY_MODE")
    notify_dingtalk_webhook_url: str = Field(default="", validation_alias="NOTIFY_DINGTALK_WEBHOOK_URL")
    approval_ticket_ttl_days: int = Field(default=7, validation_alias="BACKEND_APPROVAL_TICKET_TTL_DAYS")
    frontend_public_base_url: str = Field(
        default="http://localhost:5173", validation_alias="BACKEND_FRONTEND_PUBLIC_BASE_URL"
    )

    # --- 生成任务僵死回收（reaper）---
    # 卡在 running 的任务会永久 409（前端按钮也不可点）→ 超期即判死并放行商品（见 services.generation_job_reaper）
    job_stale_minutes: int = Field(default=15, validation_alias="BACKEND_JOB_STALE_MINUTES")
    job_reaper_interval_seconds: int = Field(default=60, validation_alias="BACKEND_JOB_REAPER_INTERVAL_SECONDS")

    # --- 日志（访问日志/业务日志的级别；修复「root logger 无 handler → INFO 被丢弃」）---
    log_level: str = Field(default="INFO", validation_alias="BACKEND_LOG_LEVEL")

    @model_validator(mode="after")
    def _fill_database_url(self) -> "Settings":
        """``BACKEND_PG_DSN`` 缺省时按 POSTGRES_* + 角色密码派生（见模块 docstring）。"""
        if not self.database_url:
            self.database_url = _derive_backend_dsn()
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS 白名单（逗号分隔 → 去空白列表；空串 = 不启用跨域）。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """进程级单例配置（测试可用 ``create_app(Settings(...))`` 覆盖）。"""
    return Settings()
