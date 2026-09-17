"""ContentStore 端口：
通过AI生成内容后，持久化存入 pgsql/productassistant/schema_pa_ai.product_contents；
并提供 admin 彻底删除商品时的物理清理能力（purge_by_product / list_image_urls）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ContentRecord:
    """一次生成结果的落库持久化 product_contents。"""

    org_id: str
    product_id: str
    thread_id: str
    content_data: dict = field(default_factory=dict)  # 结构化 blocks [{type:text|image,...}]
    prompt_version: str | None = None
    model_name: str | None = None
    version: int = 1  # 仅作调用方入参/展示参考；适配器忽略该字段，权威版本由 DB 按 MAX(version)+1 生成
    is_approved: bool = False


class ContentStore(ABC):
    """AI 生成内容写库端口（ai-engine 独占写）。"""

    @abstractmethod
    def save(self, record: ContentRecord) -> ContentRecord:
        """持久化一条生成内容并返回**传入的记录**（id/version 由适配器/DB 生成，不回填）。

        参数:
            record: 待落库记录（ContentRecord）。
        返回:
            传入的 record 本身；DB 生成的 id 与 version 不回填，record.version 不具权威性，
            权威版本由 DB 按该 (org_id, product_id) 的 MAX(version)+1 生成。
        注意:
            并发落同一 (org_id, product_id) 时版本号可能冲突，实现方应保证重试收敛，
            或按自身异常类型抛出（见 adapters/pg_store.py）。
        """

    @abstractmethod
    def purge_by_product(self, *, org_id: str, product_id: str) -> int:
        """物理清理某商品全部生成内容（admin 彻底删除用；ai-engine 独占写）。

        参数:
            org_id: 组织 ID。
            product_id: 商品 ID。
        返回:
            实际删除的行数；0 表示该商品无已落库内容。
        """

    def list_image_urls(self, *, org_id: str, product_id: str) -> list[str]:
        """收集某商品全部已落库内容中的 image block URL（purge 联动清 OSS 用）。

        默认实现返回空列表：不落 image block 语义的实现方无需覆盖；
        真实实现见 adapters/pg_store.py。

        参数:
            org_id: 组织 ID。
            product_id: 商品 ID。
        返回:
            image block 的 url 列表；同 URL 多次引用由调用方去重。
        注意:
            必须在 purge_by_product 之前调用：行删除后 URL 无法再取回，
            OSS 侧会残留孤儿对象。
        """
        return []

