"""worker 可观测性单测：配图结果/降级原因必须落进程日志。

为什么要有这组用例（2026-09 实测）:
    `OSS_BUCKET` 指向不存在的桶时，每次生成的形态是「生图成功 → OSS 上传 404 → 降级纯文本」，
    但原因只写进图内 State：任务状态是 succeeded、日志只有「开始生成 / 待处理读数」、
    `result:workflow` 载荷也不含图像字段 —— 前端表现为「永远没有配图」，排查时无迹可循。
    这行日志是那次的直接教训，用用例锁住"降级必须留痕"。
"""

from __future__ import annotations

from src.service.worker import image_outcome_line


def test_degraded_line_carries_reason() -> None:
    """降级 → 明确写 image_attached=False 与原因（可 grep、可直接定位 OSS/生图）。"""
    line = image_outcome_line(
        "9436fc3c-590a-4d29-b24e-e997f7cda150",
        {"image_attached": False, "image_error": "AI 配图失败，已按纯文本落库: OSSObjectStorageError('404')"},
    )

    assert "配图降级为纯文本" in line
    assert "image_attached=False" in line
    assert "OSSObjectStorageError" in line
    assert "thread_id=9436fc3c" in line  # 与其他日志行同口径：线程 ID 只取前 8 位


def test_success_line_reports_attached() -> None:
    """成功 → 一行说明 image_attached=True（与降级行同一 grep 入口）。"""
    line = image_outcome_line("thread-1", {"image_attached": True, "image_error": None})

    assert "image_attached=True" in line
    assert "降级" not in line


def test_line_tolerates_missing_fields() -> None:
    """字段缺失（老 checkpoint / 未走 image 节点）→ 不抛异常，按未配图如实记录。"""
    line = image_outcome_line("", {})

    assert "image_attached=None" in line
