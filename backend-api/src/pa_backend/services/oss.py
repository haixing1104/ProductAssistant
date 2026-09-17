"""OSS 对象存储（backend 侧）：预签名直传 + 自有对象清理。

为什么浏览器直传（而不是经 backend 转发字节）:
    10MB 级图片经 backend 中转会占用应用进程的内存与带宽，且与「一次请求一次响应」的
    超时口径冲突。预签名把「上传」这一重活交给 OSS，backend 只发一个带签名的 URL。

签名算法与 ai-engine **逐字一致**（SigV1 / HMAC-SHA1）:
    两侧必须能互操作：backend 预签名的对象，ai-engine 要能 ``get_bytes`` 取回并规格化；
    ai-engine 转存的规格化图，backend 要能展示。算法错一点就是 403 SignatureDoesNotMatch。

对象键前缀红线（``img/pa/``）:
    预签名对象键**必须**落在 ``img/pa/`` 下 —— 这是 ai-engine purge 的删除白名单前缀
    （``adapters/oss.py::_prefix``）。写到别处功能上能用，但**商品彻底删除时不会被清掉**，
    属于静默的资源泄漏。

两套清理的分工（重要）:
    · ai-engine 清 ``product_contents`` 里的 image block（AI 图 + 规格化后的上传图）；
    · **backend 清 ``products.raw_images`` 里的原始上传件** —— 那些 URL 只存在于 backend 域的表里，
      ai-engine 的 purge 看不到它们（它只从 content 里收集 URL）。
      这就是本模块需要 ``delete_urls`` 的原因。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ..core.config import Settings

__all__ = ["ALLOWED_CONTENT_TYPES", "MANAGED_PREFIX", "OssError", "OssStorage", "build_oss_storage"]

logger = logging.getLogger(__name__)

#: 受管对象前缀（与 ai-engine ``adapters/oss.py::_prefix`` 同源；purge 白名单同用）
MANAGED_PREFIX = "img/pa/"

#: 允许上传的图片类型 → 扩展名（白名单：杜绝把任意文件塞进图床）
ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

#: 删除请求超时（秒）：清理是 best-effort，不允许拖住请求
_DELETE_TIMEOUT_SECONDS = 10.0


class OssError(RuntimeError):
    """OSS 调用异常（网络/HTTP/配置缺失）。"""


class OssStorage:
    """SigV1 预签名（PUT/DELETE）与公有 URL 构造。"""

    def __init__(
        self, *, endpoint: str, bucket: str, access_key_id: str, access_key_secret: str, presign_ttl: int
    ) -> None:
        """初始化。

        参数:
            endpoint: Region 域名（如 ``oss-cn-hangzhou.aliyuncs.com``，不含协议）。
            bucket: Bucket 名。
            access_key_id: AK。
            access_key_secret: SK。
            presign_ttl: 预签名有效期（秒）。
        """
        self._endpoint = endpoint.strip().removeprefix("https://").removeprefix("http://").strip("/")
        self._bucket = bucket.strip()
        self._access_key_id = access_key_id.strip()
        self._access_key_secret = access_key_secret
        self._presign_ttl = presign_ttl

    @property
    def configured(self) -> bool:
        """配置是否完整（缺任一必填项视为未配置，调用方按降级处理）。"""
        return all([self._endpoint, self._bucket, self._access_key_id, self._access_key_secret])

    def public_url(self, key: str) -> str:
        """对象公有读直链（bucket 需配置公共读），供 image block / 审批快照展示。"""
        return f"https://{self._bucket}.{self._endpoint}/{urllib.parse.quote(key, safe='/-_.~')}"

    @staticmethod
    def build_upload_key(*, env: str, org_id: str, product_id: str, content_type: str) -> str:
        """生成上传对象键（``img/pa/{env}/{org}/raw/{uuid}{ext}``）。

        参数:
            env: 环境段（``PA_ENV``，与 ai-engine 的对象布局一致）。
            org_id: 组织 ID（多租户隔离的第一段）。
            product_id: 商品 ID。
            content_type: MIME 类型（决定扩展名；不在白名单内由调用方先拒）。
        返回:
            对象键（未编码，前缀为 ``img/pa/``）。
        注意:
            ``raw/`` 段刻意与 ai-engine 的 ``{thread_id}/upload-{n}.jpg`` 区分：
            前者是运营上传的**原件**（backend 域），后者是规格化后的产物（ai-engine 域），
            两者归属不同、清理责任也不同，混在同一路径下会难以区分。
        """
        ext = ALLOWED_CONTENT_TYPES[content_type]
        return f"{MANAGED_PREFIX}{env}/{org_id}/{product_id}/raw/{uuid.uuid4().hex}{ext}"

    def _signature(self, method: str, key: str, expires: int, content_type: str | None) -> str:
        """SigV1 签名（HMAC-SHA1）。

        签名串固定为 ``"{method}\\n{Content-MD5}\\n{Content-Type}\\n{Expires}\\n/{bucket}/{key}"``；
        本项目未启用 Content-MD5，故第 2 段恒为空。CanonicalizedResource 用**未编码**对象键。
        """
        resource = f"/{self._bucket}/{key}"
        string_to_sign = f"{method}\n\n{content_type or ''}\n{expires}\n{resource}"
        digest = hmac.new(
            self._access_key_secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def presign_put(self, key: str, *, content_type: str) -> tuple[str, str, int]:
        """预签名 PUT（浏览器直传用）。

        参数:
            key: 对象键（应来自 ``build_upload_key``）。
            content_type: 与浏览器将发送的 ``Content-Type`` **完全一致**的字符串。
        返回:
            ``(upload_url, public_url, expires_in)``。
        异常:
            OssError: 未配置凭据。
        注意:
            ``Content-Type`` 参与签名：浏览器必须原样带上它，否则 OSS 报签名不匹配
            （直传最常见的坑；P7 前端实现时务必与请求体类型一致）。
        """
        if not self.configured:
            raise OssError("OSS 未配置（需要 OSS_ENDPOINT/OSS_BUCKET/OSS_ACCESS_KEY_ID/OSS_ACCESS_KEY_SECRET）")
        expires = int(time.time()) + self._presign_ttl
        signature = self._signature("PUT", key, expires, content_type)
        query = urllib.parse.urlencode(
            {"OSSAccessKeyId": self._access_key_id, "Expires": str(expires), "Signature": signature}
        )
        return f"{self.public_url(key)}?{query}", self.public_url(key), self._presign_ttl

    def key_from_url(self, url: str) -> str | None:
        """URL → 对象键；host 不匹配或不在 ``img/pa/`` 白名单内返回 None（**不删**）。

        为什么必须校验 host 与前缀:
            清理入口接收的是数据库里的 URL；一旦有第三方外链（或历史脏数据），
            盲删会把别人的对象删掉。白名单是「只动自己的东西」的机器化表达。
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc != f"{self._bucket}.{self._endpoint}":
            return None
        key = urllib.parse.unquote(parsed.path.lstrip("/"))
        return key if key.startswith(MANAGED_PREFIX) else None

    def delete_key(self, key: str) -> bool:
        """删除单个对象（幂等：对象不存在也算成功）。

        参数:
            key: 对象键（必须落在 ``img/pa/`` 白名单内）。
        返回:
            是否删除成功（网络/HTTP 失败返回 False，不抛异常 —— 清理是 best-effort）。
        """
        if not self.configured or not key.startswith(MANAGED_PREFIX):
            return False
        expires = int(time.time()) + 60
        signature = self._signature("DELETE", key, expires, None)
        request = urllib.request.Request(
            f"{self.public_url(key)}?OSSAccessKeyId={urllib.parse.quote(self._access_key_id)}"
            f"&Expires={expires}&Signature={urllib.parse.quote(signature)}",
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=_DELETE_TIMEOUT_SECONDS) as response:
                return 200 <= response.status < 300
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # 幂等：目标不存在即视为已清理
                return True
            logger.warning("OSS 删除失败 key=%s http=%s", key, exc.code)
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("OSS 删除异常 key=%s err=%r", key, exc)
            return False

    def delete_urls(self, urls: list[str]) -> int:
        """按 URL 批量删除（去重 + 白名单过滤），返回成功数。"""
        deleted = 0
        for url in dict.fromkeys(urls or []):
            key = self.key_from_url(url)
            if key and self.delete_key(key):
                deleted += 1
        return deleted


def build_oss_storage(settings: Settings) -> OssStorage | None:
    """由环境变量构造 OSS 客户端；未配置返回 None（调用方降级：预签名接口返回 503）。

    参数:
        settings: 应用配置。
    返回:
        ``OssStorage`` 或 None。
    """
    storage = OssStorage(
        endpoint=settings.oss_endpoint,
        bucket=settings.oss_bucket,
        access_key_id=settings.oss_access_key_id,
        access_key_secret=settings.oss_access_key_secret,
        presign_ttl=settings.oss_presign_expire_seconds,
    )
    return storage if storage.configured else None
