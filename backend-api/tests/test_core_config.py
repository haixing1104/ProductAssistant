"""配置派生单测（需要 monkeypatch 环境变量，属纯函数验证）。"""

from __future__ import annotations

import pytest

from pa_backend.core.config import Settings, _derive_backend_dsn


def test_dsn_derived_from_postgres_env_with_encoded_password(monkeypatch):
    """``BACKEND_PG_DSN`` 缺省时按 POSTGRES_* + 角色密码派生，且口令做 URL 编码。"""
    monkeypatch.delenv("BACKEND_PG_DSN", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "productassistant")
    monkeypatch.setenv("ROLE_PA_BACKEND_PWD", "p@ss/word:1")
    dsn = _derive_backend_dsn()
    assert dsn.startswith("postgresql+psycopg://role_pa_backend:")
    assert "db.internal:5433/productassistant" in dsn
    assert "p%40ss%2Fword%3A1" in dsn  # @ / : 必须编码，否则 DSN 会被解析成另一个主机


def test_explicit_dsn_wins_over_derivation(monkeypatch):
    """显式 ``BACKEND_PG_DSN`` 优先（.env.template 里的可选覆盖项）。"""
    monkeypatch.setenv("BACKEND_PG_DSN", "postgresql+psycopg://role_pa_backend:x@explicit-host:5432/db")
    assert Settings().database_url.endswith("@explicit-host:5432/db")


def test_missing_role_password_fails_loudly(monkeypatch):
    """既无显式 DSN 又无角色密码时必须**响亮失败**（不静默降级为空口令）。"""
    monkeypatch.delenv("BACKEND_PG_DSN", raising=False)
    monkeypatch.delenv("ROLE_PA_BACKEND_PWD", raising=False)
    with pytest.raises(RuntimeError, match="ROLE_PA_BACKEND_PWD"):
        Settings()


def test_cors_origin_list_parsing(monkeypatch):
    """CORS 白名单按逗号切分并去空白；空串 = 不启用跨域。"""
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", " http://a.test , http://b.test ,")
    assert Settings().cors_origin_list == ["http://a.test", "http://b.test"]
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", "")
    assert Settings().cors_origin_list == []
