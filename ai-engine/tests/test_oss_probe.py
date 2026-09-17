"""OSS 启动自检探针单测（monkeypatch 传输层；不需要真实 HTTP / AK）。

覆盖:
  · 200 → 可用；
  · 404 → 判定「桶不存在」并给出排查线索（OSS_BUCKET 未创建 / AK 无权 / 地域不符）；
  · 403 → 判定「桶存在但 AK 无权」（与 404 是两种完全不同的处置动作，不能混为一谈）；
  · 网络不可达 / 未预期异常 → 一律降级为「不可用 + 原因」，**绝不抛异常**（自检不该让 worker 起不来）。

为什么要有这组用例（2026-09 实测）:
    `OSS_BUCKET` 指向一个从未创建的桶时，`put_bytes` 每次都 404 NoSuchBucket，而 node_image
    把原因吞进 State → 「页面永远没有配图、任务却成功」。崩溃/留痕之前先要能**在启动时判定**，
    否则同一个坑会以「静默降级」的形态反复出现。
"""

from __future__ import annotations

import urllib.error

from src.adapters import llm_zhipu
from src.adapters.oss import OSSObjectStorage, probe_oss_bucket


class _FakeResponse:
    """最小 HTTP 响应替身（探针只用到上下文管理器）。"""

    def __enter__(self) -> "_FakeResponse":
        """进入 with 块。"""
        return self

    def __exit__(self, *exc) -> bool:
        """退出 with 块（不吞异常）。"""
        return False


def _storage() -> OSSObjectStorage:
    """构造一个配置齐全的存储实例（不发任何请求）。"""
    return OSSObjectStorage(
        access_key_id="ak-test",
        access_key_secret="sk-test",
        endpoint="oss-cn-shanghai.aliyuncs.com",
        bucket_name="pa-probe-test",
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    """造一个带状态码的 HTTPError（HEAD 无 body）。"""
    return urllib.error.HTTPError("https://pa-probe-test.example/", code, "boom", None, None)


def test_probe_ok_when_bucket_reachable(monkeypatch) -> None:
    """桶存在且有权 → (True, 含 bucket 名的说明)。"""
    monkeypatch.setattr(llm_zhipu, "_urlopen", lambda request, timeout=None: _FakeResponse())

    ok, detail = probe_oss_bucket(_storage())

    assert ok is True
    assert "pa-probe-test" in detail


def test_probe_reports_missing_bucket_with_hints(monkeypatch) -> None:
    """404 → 判定桶不存在，且把三类线索（是否已建 / AK 权限 / 地域）写进原因。"""

    def boom(request, timeout=None):
        raise _http_error(404)

    monkeypatch.setattr(llm_zhipu, "_urlopen", boom)

    ok, detail = probe_oss_bucket(_storage())

    assert ok is False
    assert "不存在" in detail and "NoSuchBucket" in detail
    assert "OSS_BUCKET" in detail and "地域" in detail


def test_probe_reports_permission_denied_distinctly(monkeypatch) -> None:
    """403 → 「桶存在但无权」必须与「桶不存在」区分（处置动作完全不同）。"""

    def boom(request, timeout=None):
        raise _http_error(403)

    monkeypatch.setattr(llm_zhipu, "_urlopen", boom)

    ok, detail = probe_oss_bucket(_storage())

    assert ok is False
    assert "无权限" in detail and "404" not in detail


def test_probe_never_raises_on_network_error(monkeypatch) -> None:
    """网络不可达 → 返回 (False, 原因)，不抛异常。"""

    def boom(request, timeout=None):
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(llm_zhipu, "_urlopen", boom)

    ok, detail = probe_oss_bucket(_storage())

    assert ok is False
    assert "网络不可达" in detail


def test_probe_never_raises_on_unexpected_error(monkeypatch) -> None:
    """未预期异常（如替身被换坏）→ 同样收敛为 (False, 原因)，自检绝不冒泡。"""

    def boom(request, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_zhipu, "_urlopen", boom)

    ok, detail = probe_oss_bucket(_storage())

    assert ok is False
    assert "RuntimeError" in detail


def test_probe_signs_bucket_root_with_date_header(monkeypatch) -> None:
    """请求契约：HEAD 桶根 + Date/SigV1 头（与 _presign 同源签名，不能退化成匿名请求）。"""
    seen: dict = {}

    def capture(request, timeout=None):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        seen["date"] = request.get_header("Date")
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse()

    monkeypatch.setattr(llm_zhipu, "_urlopen", capture)

    probe_oss_bucket(_storage())

    assert seen["method"] == "HEAD"
    assert seen["url"] == "https://pa-probe-test.oss-cn-shanghai.aliyuncs.com/"
    assert seen["date"] and seen["auth"].startswith("OSS ak-test:")
