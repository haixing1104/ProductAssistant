#!/usr/bin/env bash
# =============================================================================
# ProductAssistant | infra/scripts/redis-verify.sh
# 用途    : 在**临时 Redis 容器**上验收可靠性加固（PEL 回收 / 死信 / 流保留 / 幂等标记 / 心跳）
# 用法    : ./infra/scripts/redis-verify.sh [宿主端口]      # 默认 6399
# 隔离    : 临时容器 `--rm` + 独立容器名；键空间用 PA_ENV=verify 且 DB=15，
#           与 dev/prod 的 `pa:{env}:*` 完全隔离（脚本结束容器即删，跑完还会 flushdb 兜底）。
# 依赖    : docker（daemon 可用）+ ai-engine/.venv（缺任一项即给出提示并以非 0 退出）。
# 覆盖的断言（任一失败即打印 FAIL，最终以非 0 退出）:
#   ① 精确 maxlen 裁剪 = 期望值；近似 maxlen 裁剪确实生效；TTL 已设置；
#   ② XAUTOCLAIM 能接管「已投递未 ack」的滞留消息，且 delivery count 递增；
#   ③ move_to_dlq 把消息落到死信流并清空原流 PEL（含 _original_id/_reason 留证）；
#   ④ 脏消息 read_next 抛 MessageDecodeError（带 msg_id），可被定位并清理；
#   ⑤ done 幂等标记的 SET NX 语义（二次写入被拒）与 TTL；
#   ⑥ 心跳键存在且带 TTL；线程锁互斥（二次抢锁失败）。
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AI_ENGINE_DIR="${ROOT_DIR}/ai-engine"
PY="${AI_ENGINE_DIR}/.venv/bin/python"
PORT="${1:-6399}"
CONTAINER="pa-redis-verify-$$"

command -v docker >/dev/null 2>&1 || { echo "!! 未找到 docker（本脚本需要真实 Redis 容器）" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "!! docker daemon 不可用" >&2; exit 1; }
[ -x "$PY" ] || { echo "!! 缺少 $PY（先建 ai-engine 虚拟环境并安装 requirements）" >&2; exit 1; }

echo "==> 启动临时 Redis 容器 $CONTAINER（127.0.0.1:${PORT} → 6379）"
docker run -d --rm --name "$CONTAINER" -p "127.0.0.1:${PORT}:6379" redis:7.4-alpine >/dev/null
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 40); do
  docker exec "$CONTAINER" redis-cli ping >/dev/null 2>&1 && break
  sleep 0.5
done
docker exec "$CONTAINER" redis-cli ping >/dev/null 2>&1 || { echo "!! 临时 Redis 未就绪" >&2; exit 1; }
echo "==> Redis 就绪，开始断言"

cd "$AI_ENGINE_DIR"
REDIS_URL="redis://127.0.0.1:${PORT}/15" PYTHONPATH="." "$PY" - <<'PY'
"""在真实 Redis 上验收可靠性原语（DB=15 + PA_ENV=verify，容器外零副作用）。"""
import json
import os

import redis

from src.adapters.redis_eventbus import MessageDecodeError, RedisEventBus, RedisKeys, RedisStreams, _xadd

ENV = "verify"
keys = RedisKeys(ENV)
client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
client.flushdb()  # 专用 DB（15）+ 临时容器：清空只为让断言确定

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """打印并记录一条断言结果。"""
    print(f"    {'OK  ' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        failures.append(name)


# ---------------------------------------------------------------- ① 流保留
for i in range(30):
    _xadd(client, "pa:verify:exact", {"type": "t", "i": i}, maxlen=10, approximate=False)
check(
    "精确 maxlen 裁剪 = 期望值",
    client.xlen("pa:verify:exact") == 10,
    f"XLEN={client.xlen('pa:verify:exact')}",
)

bus = RedisEventBus(client, keys, maxlen=50, ttl_seconds=120)
for i in range(300):
    bus.publish("evt:verify-1", {"type": "content.chunk", "i": i})
evt_len = client.xlen(keys.evt("verify-1"))
check("近似 maxlen 裁剪生效", 0 < evt_len < 300, f"XLEN={evt_len}（MAXLEN ~50）")
check("evt TTL 已设置", client.ttl(keys.evt("verify-1")) > 0, f"TTL={client.ttl(keys.evt('verify-1'))}")

# ------------------------------------------------------------- ② PEL 回收
a = RedisStreams(client, keys, consumer="verify-a")
b = RedisStreams(client, keys, consumer="verify-b")
a.xadd(keys.job_generate(), {"thread_id": "t-1", "product_id": "p-1", "org_id": "o-1"})
check("首次投递成功", a.read_next(keys.job_generate(), group="ai-engine", block_ms=200) is not None)
check("未 ack → PEL 有滞留", client.xpending(keys.job_generate(), "ai-engine")["pending"] == 1)

claimed = b.claim_stale(keys.job_generate(), "ai-engine", min_idle_ms=0, count=10)
check("XAUTOCLAIM 接管滞留消息", len(claimed) == 1, f"claimed={len(claimed)}")
if claimed:
    msg_id, payload, _raw = claimed[0]
    check(
        "接管后载荷可解析（含信封 schema_version）",
        payload == {"schema_version": 1, "thread_id": "t-1", "product_id": "p-1", "org_id": "o-1"},
        f"payload={payload}",
    )
    deliveries = b.deliveries_of(keys.job_generate(), msg_id, group="ai-engine")
    check("delivery count 递增", deliveries >= 2, f"deliveries={deliveries}")
    b.ack(keys.job_generate(), msg_id, group="ai-engine")
check("ack 后 PEL 归零", client.xpending(keys.job_generate(), "ai-engine")["pending"] == 0)

# ---------------------------------------------------------------- ③ 死信
a.xadd(keys.job_generate(), {"thread_id": "t-2", "product_id": "p-2", "org_id": "o-2"})
a.read_next(keys.job_generate(), group="ai-engine", block_ms=200)  # 读走不 ack（模拟崩溃）
claimed = b.claim_stale(keys.job_generate(), "ai-engine", min_idle_ms=0, count=10)
msg_id, payload, _raw = claimed[0]
dlq_stream = keys.dlq("job:generate")
b.move_to_dlq(
    keys.job_generate(),
    msg_id,
    dlq_stream=dlq_stream,
    group="ai-engine",
    deliveries=5,
    reason="verify",
    payload=payload,
    maxlen=100,
)
body = json.loads(client.xrange(dlq_stream)[0][1]["data"])
check("死信流收到消息", client.xlen(dlq_stream) == 1, f"XLEN={client.xlen(dlq_stream)}")
check(
    "死信留证字段完整",
    body.get("_original_id") == str(msg_id)
    and body.get("_reason") == "verify"
    and body.get("_deliveries") == 5
    and body.get("_payload") == payload
    and "schema_version" in body,
)
check("转死信后原流 PEL 归零", client.xpending(keys.job_generate(), "ai-engine")["pending"] == 0)

# --------------------------------------------------------------- ④ 脏数据
client.xadd(keys.job_generate(), {"data": "{坏数据"})
try:
    a.read_next(keys.job_generate(), group="ai-engine", block_ms=200)
    check("脏数据抛 MessageDecodeError", False, "未抛异常")
except MessageDecodeError as exc:
    check("脏数据抛 MessageDecodeError", True, f"msg_id={exc.msg_id}")
    a.move_to_dlq(
        exc.stream,
        exc.msg_id,
        dlq_stream=dlq_stream,
        group="ai-engine",
        reason="json_decode_error",
        raw=exc.raw,
        maxlen=100,
    )
check("脏数据可被清理（PEL 归零）", client.xpending(keys.job_generate(), "ai-engine")["pending"] == 0)
check(
    "脏数据带原文留证",
    json.loads(client.xrange(dlq_stream)[-1][1]["data"]).get("_raw") == "{坏数据",
)

# ------------------------------------------- ⑤ 幂等标记 / ⑥ 心跳与锁
check("done 标记首次写入成功", a.mark_done("t-9", ttl_seconds=300) is True)
check("done 标记二次写入被拒（NX 语义）", a.mark_done("t-9", ttl_seconds=300) is False)
check("is_done 命中", a.is_done("t-9") is True)
check("done 标记带 TTL", client.ttl(keys.done("t-9")) > 0, f"TTL={client.ttl(keys.done('t-9'))}")

a.heartbeat(ttl_seconds=30)
check(
    "心跳键存在且带 TTL",
    client.exists(keys.heartbeat("verify-a")) == 1 and client.ttl(keys.heartbeat("verify-a")) > 0,
)

token = a.acquire_lock("t-9", ttl_seconds=60)
check("首次抢锁成功", token is not None)
check("二次抢锁失败（互斥）", b.acquire_lock("t-9", ttl_seconds=60) is None)
check("lock_exists 探测为真", b.lock_exists("t-9") is True)
check("CAS 释放成功", a.release_lock("t-9", token) is True)
check("释放后锁不存在", a.lock_exists("t-9") is False)

client.flushdb()
print()
if failures:
    print(f"!! redis-verify 失败：{len(failures)} 项 → {failures}")
    raise SystemExit(1)
print("==> redis-verify 全部断言通过")
PY
