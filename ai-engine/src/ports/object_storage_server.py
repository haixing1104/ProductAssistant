"""
ObjectStorageServer 端口：对象存储（OSS）读写边界

ai-engine 职责：
  · put_bytes：AI 生图产物/规格化后的图片落库自有 OSS（公有读），返回永久 URL 供 image block / 审批快照直接展示；
  · get_bytes：按 URL 取回本存储受管对象字节（供图片规格化重新编码后再落库）；
    非本存储（host 不匹配 / 越界前缀）一律返回 None，绝不做任意 URL 抓取（防 SSRF）；
  · delete_key：admin 彻底删除联动时清理该商品已生成的 OSS 图片对象（只删本 bucket+前缀白名单）。

真实实现见 adapters/oss.py（工厂 build_oss_storage_from_env()）；未配置 OSS 凭据时工厂返回 None，
由 node_image 节点按「无图降级」处理（不配图、不阻断文案），purge 侧跳过 OSS 清理。

"""

from abc import ABC, abstractmethod

class ObjectStorageServer(ABC):
    """对象存储端口：图片字节上传取 URL、取回受管对象字节，以及按 key/URL 删除（purge 联动）。

    实现见 adapters/oss.py（OSS SigV1）；未配置凭据时工厂返回 None，节点按无图降级。
    """

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, content_type: str = "image/png") -> str:
        """上传 bytes 对象并返回公有访问 URL。

        参数:
            key: 对象键（不含 bucket；由调用方按 image_key_prefix 组织，供 purge 白名单识别）。
            data: 对象字节内容。
            content_type: MIME 类型（**必须与字节真实格式一致**：OSS SigV1 预签名把
                Content-Type 算进签名串，传错会签名不匹配被拒）。
        返回:
            可直接展示/落库的公有访问 URL。
        异常:
            OSSObjectStorageError: 鉴权 / 网络 / HTTP 失败由实现方抛出；生图链路会捕获并降级纯文本。
        """

    def get_bytes(self, url: str) -> bytes | None:
        """按 URL 取回本存储受管对象的字节（规格化上传图用）；越界或失败返回 None。

        契约（实现方必须遵守）:
            · **只接受本存储受管对象**（host 等于本 bucket 且对象键在受管前缀内）；
              其它域名的 URL 直接返回 None，不做任何网络请求 —— 避免 SSRF 与
              「替第三方抓图」的合规风险；
            · best-effort：网络 / HTTP 失败返回 None 而不抛异常，调用方回退「用原 URL 直拼」。

        参数:
            url: 对象 URL（可含 query，如 ?x-oss-process=... 或预签名参数）。
        返回:
            对象字节；非本存储受管对象或取回失败时返回 None。
        注意:
            默认实现恒返回 None（不取字节）；需要「上传图规格化重落」的部署
            由具体 adapter 覆写（见 adapters/oss.py）。
        """
        return None

    @abstractmethod
    def delete_key(self, key: str) -> bool:
        """删除单个对象（幂等：对象不存在同样视为成功）。

        参数:
            key: 对象键（实现方按自身语义决定是否接受完整 URL）。
        返回:
            True 表示删除成功或对象本就不存在；False 表示删除失败。
        异常:
            OSSObjectStorageError: 网络 / HTTP 失败由实现方抛出；调用方按 best-effort 处理。
        """

    def delete_urls(self, urls: list[str]) -> int:
        """按对象 URL 白名单批量删除（只删本存储创建/受管前缀对象），返回删除成功数。

        同一 URL 多处引用天然幂等；
        默认实现逐条转 delete_key；
        子类可按自身 URL 语义做 host/前缀白名单校验。

        参数:
            urls: 待删除的对象 URL 列表（None/空列表返回 0）。
        返回:
            删除成功（或对象本就不存在）的条数；单条异常被吞掉，不影响其余删除。
        注意:
            本方法为 best-effort：任一 URL 删除失败都不抛异常，
            避免一次清理失败把 purge 任务整体打成 failed。
        """
        deleted = 0
        for url in urls or []:
            try:
                if self.delete_key(url):
                    deleted += 1
            except Exception:  # noqa: BLE001 删除失败不阻断整体清理
                pass
        return deleted
