"""OSS 预签名与清理白名单用例。

两类断言，价值不同但都必要:
    ① **接口层**：未配置 → 503（明确失败），已配置 → 返回可直传的预签名 URL，且对象键落在
       ``img/pa/`` 白名单前缀下（否则商品彻底删除时对象不会被清理）；
    ② **纯函数层**：``key_from_url`` 的 host/前缀白名单。这是「只删自己的对象」的机器化表达 ——
       一旦放宽，清理入口就可能删掉第三方外链或同 bucket 的无关目录。

注意:
    OSS 相关环境变量必须在 ``settings`` 夹具构造**之前**设置，故用夹具（``oss_env``）而不是
    在测试函数体里改环境（夹具顺序陷阱见 ``test_login_rate_limit`` 的模块 docstring）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from pa_backend.services.oss import MANAGED_PREFIX, OssStorage

PRESIGN_URL = "/api/v1/oss/presign"

TEST_ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"
TEST_BUCKET = "pa-test-bucket"


@pytest.fixture
def oss_env(monkeypatch) -> None:
    """配置好一套假 OSS 凭据（不联网：预签名是纯本地计算）。"""
    monkeypatch.setenv("OSS_ENDPOINT", TEST_ENDPOINT)
    monkeypatch.setenv("OSS_BUCKET", TEST_BUCKET)
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "fake-ak-id")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "fake-ak-secret")
    monkeypatch.setenv("BACKEND_OSS_PRESIGN_EXPIRE_SECONDS", "600")


async def test_presign_returns_503_when_oss_not_configured(
    client: AsyncClient, auth_headers: dict, seeded_org
):
    """未配置 OSS：明确 503 + 原因（比返回假 URL 好得多）。"""
    resp = await client.post(
        PRESIGN_URL,
        json={"product_id": seeded_org.product_id, "content_type": "image/png", "filename": "a.png"},
        headers=auth_headers,
    )
    assert resp.status_code == 503
    assert "OSS 未配置" in resp.json()["message"]


async def test_presign_returns_direct_upload_url(
    oss_env, client: AsyncClient, auth_headers: dict, seeded_org, settings
):
    """已配置：返回带签名的直传 URL，对象键在 ``img/pa/{env}/{org}/{product}/raw/`` 下。"""
    resp = await client.post(
        PRESIGN_URL,
        json={"product_id": seeded_org.product_id, "content_type": "image/jpeg", "filename": "封面.jpg"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["object_key"].startswith(f"{MANAGED_PREFIX}{settings.env}/{seeded_org.org_id}/")
    assert data["object_key"].endswith(".jpg")
    assert data["public_url"].startswith(f"https://{TEST_BUCKET}.{TEST_ENDPOINT}/")
    for field in ("OSSAccessKeyId=fake-ak-id", "Expires=", "Signature="):
        assert field in data["upload_url"]
    assert data["content_type"] == "image/jpeg"
    assert data["max_upload_bytes"] == settings.oss_max_upload_bytes


async def test_presign_rejects_non_image_content_type(
    oss_env, client: AsyncClient, auth_headers: dict, seeded_org
):
    """白名单外的类型在入口被拒（400）——不把图床当任意文件中转站。"""
    resp = await client.post(
        PRESIGN_URL,
        json={"product_id": seeded_org.product_id, "content_type": "application/pdf"},
        headers=auth_headers,
    )
    assert resp.status_code == 400


async def test_presign_hides_other_tenant_product(oss_env, client: AsyncClient, auth_headers: dict):
    """商品不属于本租户 → 404（同越权响应）。"""
    import uuid

    resp = await client.post(
        PRESIGN_URL,
        json={"product_id": str(uuid.uuid4()), "content_type": "image/png"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_presign_requires_upload_role(oss_env, client: AsyncClient, auth_headers: dict, seeded_org):
    """reviewer 不能上传（403）。"""
    from tests.test_members_rbac import _create_member, _login

    member = await _create_member(client, auth_headers, "reviewer")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    resp = await client.post(
        PRESIGN_URL,
        json={"product_id": seeded_org.product_id, "content_type": "image/png"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- 纯函数层


def _storage() -> OssStorage:
    """构造一个已配置的存储实例（不联网；仅用于签名与键计算）。"""
    return OssStorage(
        endpoint=TEST_ENDPOINT,
        bucket=TEST_BUCKET,
        access_key_id="fake-ak-id",
        access_key_secret="fake-ak-secret",
        presign_ttl=600,
    )


def test_upload_key_prefix_and_extension_mapping():
    """对象键必须落在受管前缀下，且扩展名随 MIME 类型（jpg/png/webp）。"""
    storage = _storage()
    jpg = storage.build_upload_key(env="dev", org_id="o1", product_id="p1", content_type="image/jpeg")
    png = storage.build_upload_key(env="dev", org_id="o1", product_id="p1", content_type="image/png")
    assert jpg.startswith(f"{MANAGED_PREFIX}dev/o1/p1/raw/") and jpg.endswith(".jpg")
    assert png.endswith(".png")


def test_key_from_url_enforces_host_and_prefix_whitelist():
    """只有本 bucket + ``img/pa/`` 前缀的 URL 才能被转成可删对象键。"""
    storage = _storage()
    own = f"https://{TEST_BUCKET}.{TEST_ENDPOINT}/{MANAGED_PREFIX}dev/o1/p1/raw/a.jpg"
    assert storage.key_from_url(own) == f"{MANAGED_PREFIX}dev/o1/p1/raw/a.jpg"
    assert storage.key_from_url("https://other-bucket.oss-cn-hangzhou.aliyuncs.com/img/pa/a.jpg") is None
    assert storage.key_from_url("https://cdn.example.com/img/pa/a.jpg") is None
    assert storage.key_from_url(f"https://{TEST_BUCKET}.{TEST_ENDPOINT}/backups/db.sql") is None


def test_delete_key_refuses_unmanaged_key_without_network():
    """非受管前缀直接拒绝（连请求都不发）——白名单的第一道闸。"""
    assert _storage().delete_key("backups/db.sql") is False


def test_delete_urls_only_counts_whitelisted(monkeypatch):
    """批量删除只处理白名单内的 URL（用替身统计实际下发的删除请求）。"""
    storage = _storage()
    calls: list[str] = []
    monkeypatch.setattr(storage, "delete_key", lambda key: calls.append(key) or True)
    own = f"https://{TEST_BUCKET}.{TEST_ENDPOINT}/{MANAGED_PREFIX}dev/a.jpg"
    removed = storage.delete_urls([own, own, "https://cdn.example.com/x.jpg"])
    assert removed == 1
    assert calls == [f"{MANAGED_PREFIX}dev/a.jpg"]  # 去重 + 白名单过滤都生效


def test_presigned_signature_depends_on_content_type():
    """``Content-Type`` 参与 SigV1 签名：类型不同 → 签名不同（前端必须原样回传该头）。

    注意签名在 URL 里是 **URL 编码**过的（base64 的 ``+ / =`` 会被转义），
    因此断言长度前要先 ``unquote``；base64(SHA1) 解码后固定 28 字符。
    """
    import urllib.parse

    storage = _storage()
    jpeg_url, _, _ = storage.presign_put(f"{MANAGED_PREFIX}dev/a.jpg", content_type="image/jpeg")
    png_url, _, _ = storage.presign_put(f"{MANAGED_PREFIX}dev/a.jpg", content_type="image/png")
    assert jpeg_url != png_url
    signature = urllib.parse.unquote(jpeg_url.split("Signature=")[1])
    assert len(signature) == 28


def test_build_oss_storage_returns_none_when_unconfigured():
    """缺任一必填项都视为未配置（避免半配置状态发出无效签名）。"""
    from pa_backend.services.oss import build_oss_storage

    class _PartialSettings:
        """只有 AK/SK，没有 endpoint/bucket（典型「配了一半」）。"""

        oss_endpoint = ""
        oss_bucket = ""
        oss_access_key_id = "ak"
        oss_access_key_secret = "sk"
        oss_presign_expire_seconds = 600

    assert build_oss_storage(_PartialSettings()) is None
