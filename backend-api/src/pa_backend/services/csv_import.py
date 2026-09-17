"""CSV 商品导入（同步 MVP）。

导入语义：**要么全进要么全不进**（单事务）
    先在内存里校验全部行，任一行不合格 → 整批拒绝并返回逐行错误报告（HTTP 400）。
    取舍说明：部分导入会留下「不知道导进去多少」的中间态，运营很难判断下一步该重传还是手工补；
    全批拒绝的代价只是让用户修完再传一次，但状态始终可解释。
    （大批量异步导入是后续演进方向：需新的 job 类型与进度查询，不属本期。）

CSV 契约（表头必须齐全，顺序不限）
    ``sku_code,title,base_price,stock_status``（可选 ``raw_images``：``;`` 分隔的 URL）
    编码 UTF-8（带 BOM 也接受）；空行忽略；``#`` 开头的行视为注释。

限制（防呆 + 防滥用）
    · 文件 ≤ 2MB、数据行 ≤ 2000：超限直接 400（避免同步请求被大文件拖死）；
    · ``base_price`` 必须是非负数；``stock_status`` 必须是 DB CHECK 允许的枚举（PA 有 4 个值，
      比 ProductPilot 多 ``preorder``）。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

__all__ = [
    "ALLOWED_STOCK_STATUS",
    "CSV_MAX_BYTES",
    "CSV_MAX_ROWS",
    "CsvImportError",
    "ParsedProduct",
    "parse_products_csv",
]

#: 文件大小上限（字节）
CSV_MAX_BYTES = 2 * 1024 * 1024
#: 数据行上限
CSV_MAX_ROWS = 2000

#: 与 ``schema_pa_backend.products.stock_status`` 的 CHECK 一致（PA 比 PP 多 preorder）
ALLOWED_STOCK_STATUS = ("in_stock", "low_stock", "out_of_stock", "preorder")

REQUIRED_HEADERS = ("sku_code", "title", "base_price", "stock_status")


class CsvImportError(Exception):
    """CSV 校验失败。

    属性:
        row_errors: ``[{"line": 行号, "sku_code": ?, "reason": 原因}]``（1 基行号，含表头偏移）。
    """

    def __init__(self, message: str, row_errors: list[dict] | None = None) -> None:
        """初始化。

        参数:
            message: 总体原因（面向用户）。
            row_errors: 逐行错误明细。
        返回:
            无返回值。
        """
        super().__init__(message)
        self.message = message
        self.row_errors = row_errors or []


@dataclass
class ParsedProduct:
    """一行合格的导入数据。"""

    line: int
    sku_code: str
    title: str
    base_price: Decimal
    stock_status: str
    raw_images: list[str] = field(default_factory=list)


def _decode(raw: bytes) -> str:
    """解码为文本（容忍 UTF-8 BOM；其余编码直接报错，避免乱码入库）。"""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("文件编码不是 UTF-8（请另存为 UTF-8 后重传）") from exc


def _parse_images(cell: str) -> list[str]:
    """解析可选 ``raw_images`` 单元格（``;`` 分隔的 URL 列表；空 = 无图）。"""
    if not cell or not cell.strip():
        return []
    return [part.strip() for part in cell.split(";") if part.strip()]


def parse_products_csv(raw: bytes) -> list[ParsedProduct]:
    """解析并校验 CSV，返回合格行（任一行不合格即抛 ``CsvImportError``）。

    参数:
        raw: 上传文件字节。
    返回:
        ``ParsedProduct`` 列表。
    异常:
        CsvImportError: 体积/行数超限、表头缺失、行内容不合格、文件内 SKU 重复。
    """
    if len(raw) > CSV_MAX_BYTES:
        raise CsvImportError(f"文件过大（上限 {CSV_MAX_BYTES // 1024 // 1024}MB）")
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    headers = [name.strip() for name in (reader.fieldnames or []) if name]
    missing = [name for name in REQUIRED_HEADERS if name not in headers]
    if missing:
        raise CsvImportError(f"缺少必需列：{', '.join(missing)}（需要 {', '.join(REQUIRED_HEADERS)}）")

    parsed: list[ParsedProduct] = []
    row_errors: list[dict] = []
    seen_sku: dict[str, int] = {}
    for index, row in enumerate(reader, start=2):  # 表头占第 1 行 → 数据从第 2 行开始
        if not any((value or "").strip() for value in row.values()):
            continue  # 空行
        if (row.get("sku_code") or "").strip().startswith("#"):
            continue  # 注释行
        if len(parsed) >= CSV_MAX_ROWS:
            raise CsvImportError(f"数据行超过上限（{CSV_MAX_ROWS} 行）")
        sku_code = (row.get("sku_code") or "").strip()
        title = (row.get("title") or "").strip()
        price_cell = (row.get("base_price") or "").strip()
        stock_status = (row.get("stock_status") or "").strip()
        images = _parse_images(row.get("raw_images") or "")

        if not sku_code or not title:
            row_errors.append({"line": index, "sku_code": sku_code, "reason": "sku_code 与 title 不能为空"})
            continue
        if sku_code in seen_sku:
            row_errors.append(
                {"line": index, "sku_code": sku_code, "reason": f"文件内 SKU 重复（首次出现在第 {seen_sku[sku_code]} 行）"}
            )
            continue
        try:
            base_price = Decimal(price_cell)
            if base_price < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            row_errors.append({"line": index, "sku_code": sku_code, "reason": f"base_price 非法：{price_cell!r}"})
            continue
        if stock_status not in ALLOWED_STOCK_STATUS:
            row_errors.append(
                {
                    "line": index,
                    "sku_code": sku_code,
                    "reason": f"stock_status 非法：{stock_status!r}（允许 {'/'.join(ALLOWED_STOCK_STATUS)}）",
                }
            )
            continue
        seen_sku[sku_code] = index
        parsed.append(
            ParsedProduct(
                line=index,
                sku_code=sku_code,
                title=title,
                base_price=base_price,
                stock_status=stock_status,
                raw_images=images,
            )
        )

    if row_errors:
        raise CsvImportError(f"{len(row_errors)} 行数据不合格（本次未导入任何数据）", row_errors=row_errors)
    if not parsed:
        raise CsvImportError("文件中没有可导入的数据行")
    return parsed
