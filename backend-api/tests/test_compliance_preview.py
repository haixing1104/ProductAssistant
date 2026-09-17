"""快照与预览用例（P6）：把「预览 = 生成时实际会用的规则」这条承诺钉住。

为什么重要:
    运营用预览判断「这条文案能不能发」。若预览用的规则集与实际入队快照不同
    （例如忘了时效过滤、或漏了正则），预览就会给出**与生产相反**的结论 —— 比没有预览更糟。

覆盖:
    · ``GET /compliance/snapshot``：与 ``job:generate`` 载荷同形状，且**排除**过期词；
    · ``POST /compliance/preview``：命中明细（kind/keyword/位置/可读原因）、分数、``blocked``；
    · 预览与生成链路的口径一致性（本文件最重要的一条红线）。
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from pa_backend.core.config import Settings
from pa_backend.core.keys import RedisKeys
from pa_backend.core.redis_client import new_sync_redis
from pa_backend.services.event_envelope import decode_fields
from tests.conftest import SeededOrg, _exec

SNAPSHOT_URL = "/api/v1/compliance/snapshot"
PREVIEW_URL = "/api/v1/compliance/preview"


def _seed_word(backend_dsn: str, *, word: str, severity: str = "high", expired: bool = False) -> str:
    """直接入库一条词（用于精确构造时效边界）。"""
    word_id = str(uuid.uuid4())
    if expired:
        _exec(
            backend_dsn,
            "INSERT INTO schema_pa_backend.compliance_words "
            "(id, word, severity, effective_at, expires_at, source) "
            "VALUES (%s, %s, %s, now() - interval '10 day', now() - interval '1 day', 'pytest')",
            (word_id, word, severity),
        )
    else:
        _exec(
            backend_dsn,
            "INSERT INTO schema_pa_backend.compliance_words (id, word, severity, source) "
            "VALUES (%s, %s, %s, 'pytest')",
            (word_id, word, severity),
        )
    return word_id


def _drop_word(backend_dsn: str, word_id: str) -> None:
    """清理用例词。"""
    _exec(backend_dsn, "DELETE FROM schema_pa_backend.compliance_words WHERE id = %s", (word_id,))


async def test_snapshot_excludes_expired_words(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """快照只含「当前生效」的词：已过期词不在内（与入队口径一致）。"""
    expired_word, live_word = f"pytest-exp-{uuid.uuid4().hex[:6]}", f"pytest-live-{uuid.uuid4().hex[:6]}"
    expired_id = _seed_word(backend_dsn, word=expired_word, expired=True)
    live_id = _seed_word(backend_dsn, word=live_word)
    try:
        resp = await client.get(SNAPSHOT_URL, headers=auth_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        words = {item["word"] for item in data["snapshot"]["words"]}
        assert live_word in words
        assert expired_word not in words
        assert {"国家级", "最便宜"} <= words  # 种子词仍在
        assert data["snapshot"]["rules"], "种子正则规则应随快照下发"
        assert data["words_count"] == len(data["snapshot"]["words"])
    finally:
        _drop_word(backend_dsn, expired_id)
        _drop_word(backend_dsn, live_id)


async def test_preview_reports_hits_score_and_blocking(client: AsyncClient, auth_headers: dict):
    """预览：命中词与正则、给出位置与可读原因、按严重级算分并判定是否阻断。"""
    resp = await client.post(
        PREVIEW_URL,
        json={"text": "本店国家级最低价，100%正品，绝对是全网最便宜"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    keywords = {hit["keyword"] for hit in data["hits"]}
    assert "国家级" in keywords  # 词类
    assert "100%" in keywords  # 正则类（种子规则）
    assert data["blocked"] is True  # 含 high/medium 命中 → 会 fail-fast
    assert data["score"] < 100.0
    word_hit = next(hit for hit in data["hits"] if hit["keyword"] == "国家级")
    assert word_hit["kind"] == "word" and word_hit["blocking"] is True
    assert "国家级" in word_hit["reason"]
    assert word_hit["end"] > word_hit["start"]  # 位置用于前端高亮
    assert data["words_count"] >= 3 and data["rules_count"] >= 1


async def test_preview_clean_text_is_not_blocked(client: AsyncClient, auth_headers: dict):
    """干净文本：无命中、满分、不阻断（避免「预览永远报违规」这种反向误报）。"""
    resp = await client.post(
        PREVIEW_URL, json={"text": "食品级不锈钢内胆，容量 500ml，日常售价实惠"}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["hits"] == []
    assert data["score"] == 100.0
    assert data["blocked"] is False


async def test_preview_longest_match_wins(client: AsyncClient, auth_headers: dict, backend_dsn: str):
    """预览遵循「同起点取最长」：更长的词吃掉更短的（与 ai-engine 选择一致）。"""
    short_word = f"pytest-最{uuid.uuid4().hex[:4]}"
    long_word = short_word + "加长"
    short_id = _seed_word(backend_dsn, word=short_word, severity="high")
    long_id = _seed_word(backend_dsn, word=long_word, severity="medium")
    try:
        resp = await client.post(PREVIEW_URL, json={"text": f"xx{long_word}yy"}, headers=auth_headers)
        hits = resp.json()["data"]["hits"]
        assert [hit["keyword"] for hit in hits] == [long_word]
        assert hits[0]["severity"] == "medium"  # 长词是 medium → 「更长」优先于「更严」
        assert hits[0]["start"] == 2
    finally:
        _drop_word(backend_dsn, short_id)
        _drop_word(backend_dsn, long_id)


async def test_preview_rejects_blank_text(client: AsyncClient, auth_headers: dict):
    """空文本 → 400（入参校验：预览必须有内容才有意义）。"""
    resp = await client.post(PREVIEW_URL, json={"text": ""}, headers=auth_headers)
    assert resp.status_code == 400


async def test_preview_matches_what_generation_enqueues(
    client: AsyncClient, auth_headers: dict, backend_dsn: str, seeded_org: SeededOrg, settings: Settings
):
    """**一致性红线**：预览命中的词，必须能在真实 ``job:generate`` 的 rules 快照里找到。

    这是本文件最重要的一条 —— 它把「预览口径 = 入队口径」从注释变成断言。
    做法：先预览（唯一后缀词，必命中），再触发真实生成并读 Redis 流里的载荷，两边比对。
    """
    unique_word = f"pytest-一致{uuid.uuid4().hex[:6]}"
    word_id = _seed_word(backend_dsn, word=unique_word)
    try:
        preview = await client.post(
            PREVIEW_URL, json={"text": f"这款产品{unique_word}，值得购买"}, headers=auth_headers
        )
        assert [hit["keyword"] for hit in preview.json()["data"]["hits"]] == [unique_word]

        redis_client = new_sync_redis(settings)
        try:
            redis_client.delete(RedisKeys(settings.env).job_generate())
        finally:
            redis_client.close()
        generate = await client.post(f"/api/v1/products/{seeded_org.product_id}/generate", headers=auth_headers)
        assert generate.status_code == 200, generate.text

        redis_client = new_sync_redis(settings)
        try:
            entries = redis_client.xrange(RedisKeys(settings.env).job_generate(), min="-", max="+", count=10)
            payload = decode_fields(entries[-1][1])
        finally:
            redis_client.close()
        enqueued_words = {item["word"] for item in payload["rules"]["words"]}
        assert unique_word in enqueued_words  # 预览说会命中 → 入队快照里确实有它
    finally:
        _drop_word(backend_dsn, word_id)
