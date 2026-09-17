#!/usr/bin/env python3
"""
ProductPilot | database/milvus/init_collections.py

用途     : 初始化 Milvus 向量集合（商品文案语义向量库，RAG Few-Shot 召回用）。
依赖     : pymilvus 2.6.x（与 Milvus 服务端 2.6.x 配套，见 infra/docker-compose.yml 的 milvusdb/milvus:v2.6.1）。

集合命名 : pa_listing_vec（固定名，不带环境前缀）
           ⚠ 与 ai-engine/src/adapters/milvus_rag_store.py 的 LISTING_VEC_COLLECTION 必须保持一致：
             改一处必须同步另一处，否则会出现「写入 A 集合、检索 B 集合」，
             表现为 retrieve() 恒返回 []（静默降级），属于极难排查的问题。

Schema   : 5 个标量字段 + 1 个向量字段（向量用于语义相似度检索）
    - id         VARCHAR(64)    主键，由业务侧提供（doc_id），是 upsert 幂等覆盖的前提
    - thread_id  VARCHAR(64)    关联 generation_jobs / product_contents，作为检索回溯锚点
    - product_id VARCHAR(64)    支撑按商品删除（admin 彻底删除 purge 联动）与「同商品优先」召回
    - org_id     VARCHAR(64)    多租户逻辑隔离，检索时必须按租户过滤
    - text       VARCHAR(8192)  被召回的历史高转化文案片段（Few-Shot 上下文，随向量一同存储）
    - embedding  FLOAT_VECTOR   文案向量，维度由 --dim 指定（默认 1024）

索引策略 : HNSW + COSINE（M=16 / efConstruction=200）
    - COSINE：智谱 embedding-3 输出为归一化向量，余弦距离即语义相似度，与检索侧口径一致
    - M / efConstruction：建索引时「内存占用 ↔ 召回率」的取舍，16 / 200 为单机 standalone 的常用平衡点

幂等性   : 集合已存在则直接跳过（只判断存在性，不校验也不迁移 schema）；
           如需变更 schema 或向量维度，必须手工删除集合后重建（Milvus 不支持原地改字段）。

环境变量 : MILVUS_URI（默认 http://localhost:19530）

用法     :
    python init_collections.py                             # 默认地址 + 默认维度 1024
    python init_collections.py --uri http://milvus:19530   # 容器内按服务名连接
    python init_collections.py --dim 1024                  # 显式指定向量维度
"""

from __future__ import annotations

import argparse
import logging
import os

from pymilvus import MilvusClient, DataType  # pymilvus 2.x API

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Milvus 服务地址默认值：与 infra/.env 的 MILVUS_URI 保持一致（本机 docker compose 暴露 19530）。
DEFAULT_MILVUS_URI = "http://localhost:19530"

# 集合名：固定字面量，不带环境前缀。
# ⚠ 与 ai-engine/src/adapters/milvus_rag_store.py::LISTING_VEC_COLLECTION 必须一致。
LISTING_VEC_COLLECTION = "pa_listing_vec"

# 向量维度：已锁定智谱 embedding-3 = 1024。
# ⚠ 必须与 infra/.env 的 EMBEDDING_DIM / MILVUS_DIM 对齐，否则写入向量维度不匹配会被 Milvus 拒绝。
DEFAULT_DIM = 1024


def main() -> None:
    """CLI 入口：解析参数 → 连接 Milvus → 幂等创建集合（集合已存在则直接跳过）。"""
    parser = argparse.ArgumentParser(description="创建 ProductPilot 商品文案向量集合")
    parser.add_argument("--uri", default=os.getenv("MILVUS_URI", DEFAULT_MILVUS_URI), help="Milvus 服务地址")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="embedding 维度（已锁定智谱 embedding-3 = 1024）")
    args = parser.parse_args()

    # MilvusClient 按 URI 直连（standalone 单机模式）；连接失败会抛异常，由进程退出码暴露
    client = MilvusClient(uri=args.uri)

    # 幂等：集合已存在直接返回，不重复建索引、也不改动既有 schema
    if client.has_collection(LISTING_VEC_COLLECTION):
        logger.info("集合已存在，跳过创建: %s", LISTING_VEC_COLLECTION)
        return

    # auto_id=False：主键由业务侧 doc_id 提供（upsert 幂等覆盖的前提）
    # enable_dynamic_field=True：允许写入未声明的附加字段，后续扩展字段无需重建集合
    schema = client.create_schema(auto_id=False, enable_dynamic_field=True)
    # id：主键 = 业务 doc_id（VARCHAR(64) 对齐 UUID 长度），重复写入同一 id 视为覆盖
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64)
    # thread_id：关联 generation_jobs / product_contents，作为检索回溯锚点
    schema.add_field(field_name="thread_id", datatype=DataType.VARCHAR, max_length=64)
    # product_id：支撑按商品删除（purge 联动）与「同商品优先」的检索过滤
    schema.add_field(field_name="product_id", datatype=DataType.VARCHAR, max_length=64)
    # org_id：多租户逻辑隔离，检索时必须按租户过滤
    schema.add_field(field_name="org_id", datatype=DataType.VARCHAR, max_length=64)
    # text：被召回的历史高转化文案片段（Few-Shot 上下文，随向量一同存储）
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=8192)
    # embedding：待检索的向量字段，维度须与 EMBEDDING_DIM / MILVUS_DIM 一致
    schema.add_field(field_name="embedding", datatype=DataType.FLOAT_VECTOR, dim=args.dim)

    # 向量索引：HNSW（图索引，检索快、召回率高）+ COSINE（与归一化 embedding 的口径一致）
    # M=16：每个节点邻居数（越大召回率越高、内存越多）；efConstruction=200：建索引时候选队列长度
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="HNSW",
        metric_type="COSINE",
        params={"M": 16, "efConstruction": 200},
    )

    # 建集合（schema + 向量索引一次到位）；标量字段按查询需要建索引，此处不额外声明
    client.create_collection(collection_name=LISTING_VEC_COLLECTION, schema=schema, index_params=index_params)
    logger.info("已创建集合: %s (dim=%s)", LISTING_VEC_COLLECTION, args.dim)


if __name__ == "__main__":
    main()
