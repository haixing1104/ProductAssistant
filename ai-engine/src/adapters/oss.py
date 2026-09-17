"""阿里云 OSS 对象存储真实 adapter（ai-engine 侧）。

流程:
    1. put_bytes → SigV1 预签名 PUT + urllib 直传 → 返回永久公有 URL；
    2. delete_key → SigV1 预签名 DELETE 清理单个对象（幂等：对象不存在也算成功）；
    3. delete_urls → URL 去重 + host/前缀白名单校验 → 逐条转 delete_key，返回成功数。

安全红线:
    删除只作用于 host=本 bucket 且对象键以 img/pa/ 开头的对象
    （上传图 / AI 配图 / 删除白名单同源），防误删同 bucket 其它目录或外链图。

环境变量:
    - OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET：AK/SK，必填；缺失则工厂返回 None。
    - OSS_ENDPOINT：Region 域名（如 oss-cn-hangzhou.aliyuncs.com）。
    - OSS_BUCKET：Bucket 名。

依赖:
    仅标准库（hmac/hashlib/base64/urllib），不引入 oss2 等第三方 SDK；
    传输复用 llm_zhipu._urlopen（DNS 预检 + 超时包装），域名解析挂起不再冻结 worker。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate

from ..ports import ObjectStorageServer
from . import llm_zhipu

logger = logging.getLogger(__name__)

# 网络超时（秒）：上传/删除不允许无限阻塞 worker 线程。
_HTTP_TIMEOUT = 30.0

# 预签名 URL 有效期（秒）：覆盖大图上传耗时；过期后 URL 失效，需重新签名。
_PRESIGN_TTL_SECONDS = 3600

# 启动自检（probe_oss_bucket）超时（秒）：比业务上传短得多 —— 自检只回 200/4xx，
# 不该拖慢 worker 启动；慢就是坏（网络/DNS 问题），按不可用报出来。
_PROBE_TIMEOUT = 5.0

class OSSObjectStorageError(RuntimeError):
    """OSS 对象存储调用异常（HTTP / 网络 / 响应异常）。"""

def _raise_for_oss_error(exc: urllib.error.URLError, action: str, key: str) -> None:
    """统一 HTTP/网络错误映射为 OSSObjectStorageError。

    HTTPError 是 URLError 子类：优先按 HTTP 状态报告并附带响应体片段，其余按网络原因报告。

    参数:
        exc: urllib 抛出的异常。
        action: 动作名（如 put），用于定位失败环节。
        key: 对象键，用于定位失败对象。
    异常:
        OSSObjectStorageError: 总是抛出。
    """
    if isinstance(exc, urllib.error.HTTPError):
        # 只读取 200 字节（足够携带错误信息），非 UTF-8 字节以 replace 兜底。
        detail = exc.read(200).decode("utf-8", "replace")
        raise OSSObjectStorageError(f"OSS {action} HTTP {exc.code} key={key}: {detail}") from exc
    raise OSSObjectStorageError(f"OSS {action} 网络错误 key={key}: {exc.reason}") from exc

def build_oss_storage_from_env() -> OSSObjectStorage | None:
    """按环境变量构建 OSS 存储；四项配置齐备才构造，否则返回 None。

    返回 None 时由调用方降级：node_image 生成纯文本（不配图）、purge 跳过 OSS 清理。

    参数:
        （无；全部读取环境变量 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET /
        OSS_ENDPOINT / OSS_BUCKET）
    返回:
        OSSObjectStorage 实例；None 表示未配置。
    """
    key_id = os.getenv("OSS_ACCESS_KEY_ID")
    key_secret = os.getenv("OSS_ACCESS_KEY_SECRET")
    endpoint = os.getenv("OSS_ENDPOINT")
    bucket_name = os.getenv("OSS_BUCKET")
    if not (key_id and key_secret and endpoint and bucket_name):
        return None
    return OSSObjectStorage(
        access_key_id=key_id,
        access_key_secret=key_secret,
        endpoint=endpoint,
        bucket_name=bucket_name,
    )

class OSSObjectStorage(ObjectStorageServer):
    """OSS 对象存储实现：SigV1 预签名直传 + img/pa/ 前缀白名单幂等删除。

    仅依赖标准库；put_bytes 返回永久公有读 URL（供 image block / 审批快照展示），
    delete_key / delete_urls 只作用于本 bucket + img/pa/ 白名单对象且失败不抛错。
    """
    def __init__(self, *, access_key_id: str, access_key_secret: str,
                 endpoint: str, bucket_name: str):
        """初始化 OSS 客户端（不做网络探测、不建桶）。

        参数:
            access_key_id: 阿里云 AccessKeyId。
            access_key_secret: 阿里云 AccessKeySecret（仅本地签名用，不外发）。
            endpoint: Region 域名（如 oss-cn-hangzhou.aliyuncs.com）。
            bucket_name: Bucket 名。
        异常:
            ValueError: 任一配置为空。
        """
        if not (access_key_id and access_key_secret and endpoint and bucket_name):
            raise ValueError("OSSObjectStorage 需要完整配置 endpoint/bucket/ak/sk")
        self._endpoint = endpoint
        self._bucket_name = bucket_name
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret

    def _prefix(self) -> str:
        """受管对象前缀：上传图、AI 配图与删除白名单同源，均为 img/pa/（不含环境段）。"""
        return "img/pa/"

    @property
    def bucket_name(self) -> str:
        """本实例实际使用（并由本实例签名）的 bucket 名；供启动自检/日志展示。"""
        return self._bucket_name

    def _public_url(self, key: str) -> str:
        """对象公有读直链（bucket 需配置公共读），供 image block / 审批快照展示。

        参数:
            key: 对象键（未编码）。
        返回:
            https://{bucket}.{endpoint}/{URL 编码对象键}。
        """
        path = urllib.parse.quote(key, safe="/-_.~")
        return f"https://{self._bucket_name}.{self._endpoint}/{path}"

    def _presign(self, method: str, key: str, content_type: str | None = None) -> str:
        r"""生成 OSS SigV1 预签名 URL（PUT / DELETE 通用）。
        SigV1 签名串生成 + URL 拼装
        阿里云 OSS 的签名算法（HMAC-SHA1），把 AK/SK 在本地算出 Signature 拼到 URL query 上，服务端无需客户端再带密钥。

        签名串固定为 "{method}\n{Content-MD5}\n{Content-Type}\n{Expires}\n/{bucket}/{key}"，
        本项目未启用 Content-MD5，故第 2 段恒为空。
        按 SigV1 规范：CanonicalizedResource 用未编码对象键、请求路径用 URL 编码键
        （此处 resource 用裸 key、_public_url 内部 quote，两种形态不可合并）。

        参数:
            method: HTTP 方法（PUT / DELETE）。
            key: 对象键（未编码）。
            content_type: PUT 须与请求头一致，否则 OSS 报签名不匹配；DELETE 传 None。
        返回:
            含 OSSAccessKeyId / Expires / Signature 的预签名 URL（有效期 _PRESIGN_TTL_SECONDS 秒）。
        """
        resource = f"/{self._bucket_name}/{key}"
        expires = int(time.time()) + _PRESIGN_TTL_SECONDS
        string_to_sign = f"{method}\n\n{content_type or ''}\n{expires}\n{resource}"
        digest = hmac.new(self._access_key_secret.encode("utf-8"),
                          string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        query = urllib.parse.urlencode(
            {"OSSAccessKeyId": self._access_key_id, "Expires": str(expires), "Signature": signature}
        )
        return f"{self._public_url(key)}?{query}"

    def put_bytes(self, key: str, data: bytes, content_type: str = "image/png") -> str:
        """预签名 PUT 上传对象，返回永久公有 URL。

        写侧不校验前缀：对象键由调用方决定（约定收拢在 img/pa/ 下），
        删除侧才启用白名单（见 _key_from_url）。

        参数:
            key: 对象键（约定以 img/pa/ 开头）。
            data: 图片字节。
            content_type: Content-Type；须与签名时一致，否则 OSS 报签名不匹配。
        返回:
            公有读 URL（不带签名 query）。
        异常:
            OSSObjectStorageError: 网络故障或 OSS 返回非 2xx。
        """
        # 预签名后的url
        url = self._presign("PUT", key, content_type=content_type)
        # 拼接HTTP请求
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers={"Content-Type": content_type})
        try:
            # 预签名 PUT 直传：发起HTTP请求，成功返回 2xx；非 2xx 由 urllib 抛 HTTPError，统一映射为域异常。
            with llm_zhipu._urlopen(req, timeout=_HTTP_TIMEOUT):
                pass
        except urllib.error.URLError as exc:
            _raise_for_oss_error(exc, "put", key)
        return self._public_url(key)

    def _key_from_url(self, url: str) -> str | None:
        """URL → 对象键；host 不匹配或非 img/pa/ 前缀时返回 None（不删）。
        从完整的URL  提取末尾的image key.
        如：完整的URL "https://bucket.endpoint/img/pa/a.png"  末尾的key: "img/pa/a.png"

        对 URL path 做 URL 解码，与 _public_url 的 quote(safe="/-_.~") 互为逆运算；
        否则编码串会被二次编码，导致签名不匹配、对象删不掉。

        参数:
            url: 完整 OSS URL（可含预签名 query）。
        返回:
            解码后的对象键；越界返回 None。
        """
        if not url:
            return None
        # 解析URL
        parsed = urllib.parse.urlparse(url)
        # 白名单校验：netloc必须等于本bucket的域名。拒绝其他外联域名。
        if parsed.netloc != f"{self._bucket_name}.{self._endpoint}":
            return None
        # 解码
        key = urllib.parse.unquote(parsed.path.lstrip("/"))
        # 前缀匹配校验
        return key if key.startswith(self._prefix()) else None

    def get_bytes(self, url: str) -> bytes | None:
        """按 URL 取回本 bucket 受管对象的字节（供上传图规格化后重落）。

        只接受 host 等于本 bucket 且对象键在 img/pa/ 白名单内的 URL（复用 _key_from_url）；
        第三方域名一律返回 None 且**不发起任何请求** —— 既防 SSRF，也不做别人图床的代理。

        参数:
            url: 对象 URL（可含 query，如预签名参数或图片处理参数；解析只看 path）。
        返回:
            对象字节；越界对象或取回失败时返回 None（best-effort，不抛异常，
            调用方回退「用原 URL 直拼」）。
        """
        key = self._key_from_url(url)
        if key is None:
            return None
        try:
            req = urllib.request.Request(self._public_url(key), method="GET")
            with llm_zhipu._urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 取回失败不抛错（best-effort）
            logger.warning("OSS GET 失败 key=%s: %r", key, exc)
            return None

    def delete_key(self, key: str) -> bool:
        """删除单个对象（幂等）：裸对象键或完整 URL 均可传入。

        先按完整 URL 解析（host + img/pa/ 白名单校验），失败再按裸对象键判前缀；
        越界对象直接返回 False，不发起任何请求。

        参数:
            key: 裸对象键（img/pa/...）或完整 OSS URL。
        返回:
            True 表示删除成功；False 表示越界或删除失败（仅告警，不抛错）。
        """
        real = self._key_from_url(key)
        if real is None and key.startswith(self._prefix()):
            real = key
        if real is None:
            return False
         # 预签名后的url
        url = self._presign("DELETE", real)
        # 拼接HTTP请求
        req = urllib.request.Request(url, method="DELETE")
        try:
            # 发起HTTP请求，OSS 删除成功返回 204；对象不存在同样视为成功（天然幂等）。
            with llm_zhipu._urlopen(req, timeout=_HTTP_TIMEOUT):
                pass
            return True
        except Exception as exc:  # noqa: BLE001 删除失败不阻断整体清理（端口契约：返回 False 而非抛出）
            logger.warning("OSS DELETE 失败 key=%s: %r", real, exc)
            return False

    def delete_urls(self, urls: list[str]) -> int:
        """按 URL 白名单批量删除（覆盖端口默认实现：去重 + host/前缀校验）。

        同一 URL 多处引用只删一次；单条失败由 delete_key 吞错返回 False，不阻断其余清理。

        参数:
            urls: 待清理的 OSS URL 列表（可为 None / 空）。
        返回:
            实际删除成功的对象数。
        """
        deleted = 0
        seen: set[str] = set()
        for url in urls or []:
            key = self._key_from_url(url)
            if key is None or key in seen:
                continue
            seen.add(key)
            if self.delete_key(key):
                deleted += 1
        return deleted

def probe_oss_bucket(storage: OSSObjectStorage, *, timeout: float = _PROBE_TIMEOUT) -> tuple[bool, str]:
    """启动自检：本 bucket 是否真的可用（只读 HEAD，不写任何对象、不建桶）。

    为什么需要（2026-09 实测事故）:
        `OSS_BUCKET` 指向一个**从未创建**的 bucket 时，`put_bytes` 每次都 404 NoSuchBucket，
        而 node_image 会把原因吞进 State 的 `image_error`（只降级纯文本）→ 现象是
        「页面永远没有配图」，日志里却看不到任何线索。启动时喊一声，把静默降级变成显性告警。

    签名口径与 `_presign` 同源（SigV1 + `Date` 头），resource 取桶根 `/{bucket}/`；
    HEAD 是无 body 的 HeadBucket 语义：200 = 存在且有权；403 = 存在但 AK 无权；
    404 = 桶不存在（或 endpoint 地域与桶不符）。

    参数:
        storage: 已构建的 OSS 存储实例（携带 AK/SK/endpoint/bucket）。
        timeout: 探针超时（秒）；默认 `_PROBE_TIMEOUT`（慢即视为不可用）。
    返回:
        (ok, detail)：ok=True 表示桶可用；detail 为可供人直接读的原因（失败时含排查线索）。
        本函数**不抛异常** —— 自检永远不该让 worker 起不来。
    """
    bucket = storage.bucket_name
    date = formatdate(usegmt=True)
    resource = f"/{bucket}/"
    string_to_sign = f"HEAD\n\n\n{date}\n{resource}"
    digest = hmac.new(
        storage._access_key_secret.encode("utf-8"),  # noqa: SLF001 同模块内的自检，复用签名材料
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    auth = f"OSS {storage._access_key_id}:{base64.b64encode(digest).decode('utf-8')}"  # noqa: SLF001
    req = urllib.request.Request(
        f"https://{bucket}.{storage._endpoint}/",
        method="HEAD",
        headers={"Date": date, "Authorization": auth},
    )
    try:
        with llm_zhipu._urlopen(req, timeout=timeout):
            return True, f"bucket={bucket} 可用（HEAD 200）"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, (
                f"bucket={bucket} 不存在（HTTP 404 NoSuchBucket）：请确认 OSS_BUCKET 是否已创建、"
                f"AK 是否有权访问该桶、endpoint={storage._endpoint} 是否与桶地域一致"  # noqa: SLF001
            )
        if exc.code in (401, 403):
            return False, (
                f"bucket={bucket} 存在但当前 AK 无权限（HTTP {exc.code}）：检查 RAM 授权与桶读写策略"
            )
        return False, f"bucket={bucket} 探针返回 HTTP {exc.code}（既非可用也非缺失，请人工确认）"
    except urllib.error.URLError as exc:
        return False, f"bucket={bucket} 网络不可达：{exc.reason}"
    except Exception as exc:  # noqa: BLE001 自检绝不冒泡：任何异常都降级为「不可用 + 原因」
        return False, f"bucket={bucket} 探针异常：{type(exc).__name__}: {exc}"


__all__ = ["OSSObjectStorage", "OSSObjectStorageError", "build_oss_storage_from_env", "probe_oss_bucket"]
