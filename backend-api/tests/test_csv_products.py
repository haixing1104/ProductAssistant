"""CSV 导入用例：全批语义 + 逐行报告 + 表头校验。

语义红线（改动前先读 ``services/csv_import.py`` 模块 docstring）:
    **要么全进要么全不进** —— 任一行不合格即整批拒绝。这样才能保证「导入后库里有多少条」
    始终可解释；部分导入会让运营陷入「不知道补哪几行」的中间态。

覆盖:
    · 正常导入（含注释行/空行/带 BOM 的 UTF-8）；
    · 缺必需列、行内容非法（价格/库存枚举）、文件内 SKU 重复 → 400 且带逐行错误；
    · 与库中已有 SKU 冲突 → 409 且**不导入任何数据**（用条数断言钉住）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

IMPORT_URL = "/api/v1/products/import-csv"
PRODUCTS_URL = "/api/v1/products"

HEADER = "sku_code,title,base_price,stock_status\n"


def _csv(*rows: str, header: str = HEADER) -> bytes:
    """拼一个 CSV 文件字节串（UTF-8）。"""
    return (header + "\n".join(rows) + "\n").encode("utf-8")


async def _count_products(client: AsyncClient, headers: dict) -> int:
    """当前商品总数（读 ``X-Total-Count``）。"""
    resp = await client.get(PRODUCTS_URL, params={"limit": 1}, headers=headers)
    return int(resp.headers["X-Total-Count"])


async def test_import_creates_all_rows(client: AsyncClient, auth_headers: dict):
    """正常导入：3 行全部入库，返回 SKU 清单。"""
    suffix = uuid.uuid4().hex[:6]
    body = _csv(
        f"SKU-CSV-{suffix}-1,保温杯 A,19.90,in_stock",
        f"SKU-CSV-{suffix}-2,保温杯 B,29.90,low_stock",
        f"SKU-CSV-{suffix}-3,保温杯 C,39.90,preorder",  # preorder 是 PA 独有枚举
    )
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["created"] == 3
    assert len(data["skus"]) == 3


async def test_import_ignores_blank_and_comment_lines(client: AsyncClient, auth_headers: dict):
    """空行与 ``#`` 注释行被忽略（不参与校验，也不入库）。"""
    suffix = uuid.uuid4().hex[:6]
    body = _csv(
        f"SKU-CSV-{suffix}-c1,注释测试,9.90,in_stock",
        "",
        "# 这一行是注释,不该被解析,0,in_stock",
    )
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["created"] == 1


async def test_import_accepts_utf8_bom(client: AsyncClient, auth_headers: dict):
    """带 BOM 的 UTF-8（Excel 另存为常见产物）必须能解析 —— 否则第一列表头会被污染。"""
    suffix = uuid.uuid4().hex[:6]
    body = ("\ufeff" + HEADER + f"SKU-CSV-{suffix}-bom,带 BOM,1.00,in_stock\n").encode("utf-8")
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 200, resp.text


async def test_missing_required_header_is_rejected(client: AsyncClient, auth_headers: dict):
    """缺必需列 → 400（错误信息里点出缺了哪一列）。"""
    body = _csv("SKU-X,只有三列,19.90", header="sku_code,title,base_price\n")
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 400
    assert "stock_status" in resp.json()["message"]


async def test_row_level_errors_are_reported_and_nothing_is_imported(
    client: AsyncClient, auth_headers: dict
):
    """行内容非法（价格/库存枚举）：返回 400 + 逐行明细，且**一条都不入库**。"""
    before = await _count_products(client, auth_headers)
    suffix = uuid.uuid4().hex[:6]
    body = _csv(
        f"SKU-CSV-{suffix}-ok,合格行,10.00,in_stock",
        f"SKU-CSV-{suffix}-badprice,价格非法,abc,in_stock",
        f"SKU-CSV-{suffix}-badstock,库存非法,10.00,有货",
    )
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 400
    payload = resp.json()
    assert payload["code"] == 400
    errors = payload["data"]["row_errors"]
    assert {item["line"] for item in errors} == {3, 4}  # 表头占第 1 行 → 第 2 行才是第一个数据行
    assert any("base_price" in item["reason"] for item in errors)
    assert any("stock_status" in item["reason"] for item in errors)
    assert await _count_products(client, auth_headers) == before  # 全批回滚


async def test_duplicate_sku_inside_file_is_rejected(client: AsyncClient, auth_headers: dict):
    """文件内 SKU 重复 → 400，并指出首次出现行号。"""
    suffix = uuid.uuid4().hex[:6]
    body = _csv(f"SKU-CSV-{suffix}-dup,第一行,1.00,in_stock", f"SKU-CSV-{suffix}-dup,重复行,2.00,in_stock")
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 400
    errors = resp.json()["data"]["row_errors"]
    assert any("重复" in item["reason"] for item in errors)


async def test_existing_sku_conflict_rejects_whole_batch(client: AsyncClient, auth_headers: dict):
    """与库中已有 SKU 冲突 → 409，且整批不导入。"""
    suffix = uuid.uuid4().hex[:6]
    existing_sku = f"SKU-CSV-{suffix}-exists"
    created = await client.post(
        PRODUCTS_URL, json={"sku_code": existing_sku, "title": "已存在"}, headers=auth_headers
    )
    assert created.status_code == 200
    before = await _count_products(client, auth_headers)

    body = _csv(f"SKU-CSV-{suffix}-new,新行,1.00,in_stock", f"{existing_sku},撞车行,2.00,in_stock")
    resp = await client.post(IMPORT_URL, files={"file": ("p.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 409
    assert existing_sku in resp.json()["message"]
    assert await _count_products(client, auth_headers) == before


async def test_oversized_file_is_rejected(client: AsyncClient, auth_headers: dict):
    """超过体积上限 → 400（同步接口不允许被大文件拖住）。"""
    from pa_backend.services.csv_import import CSV_MAX_BYTES

    body = HEADER.encode("utf-8") + b"x" * (CSV_MAX_BYTES + 1)
    resp = await client.post(IMPORT_URL, files={"file": ("big.csv", body, "text/csv")}, headers=auth_headers)
    assert resp.status_code == 400
    assert "过大" in resp.json()["message"]


async def test_import_requires_write_role(client: AsyncClient, auth_headers: dict):
    """reviewer 不能导入（403）。"""
    from tests.test_members_rbac import _create_member, _login

    member = await _create_member(client, auth_headers, "reviewer")
    token = await _login(client, member["username"], "Pa-Member-Passw0rd!")
    resp = await client.post(
        IMPORT_URL,
        files={"file": ("p.csv", _csv("SKU-RV-1,只读角色,1.00,in_stock"), "text/csv")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
