"""LLMImageGenGateway:LLM 生图端口（只定接口，不含具体供应商实现）。

与 LLMGateway（文本生文/评估）分离，图文生成解耦；产物以字节返回，
由调用方经 ObjectStorageServer 转存阿里云 OSS。
"""

from abc import ABC, abstractmethod


class LLMImageGenGateway(ABC):
    """LLM 生图网关接口：按商品信息生成一张配图，返回图片字节（png/jpeg）。

    协议约定：同步阻塞返回；网络 / 协议错误由实现方抛自身异常类型。
    失败由 node_image 节点捕获并按「无图降级」处理，绝不阻断文案落库。
    """

    @abstractmethod
    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        """生成配图并返回图片字节流。

        参数:
            prompt: 生图提示词（商品信息 + 风格约束）。
            size: 图片尺寸，形如 "1024x1024"。
        返回:
            图片字节（png/jpeg）。
        异常:
            Exception: 网络 / 协议失败由实现方抛出，调用方按「无图降级」处理。
        """
