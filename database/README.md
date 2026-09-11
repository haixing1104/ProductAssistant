# database模块介绍

## 原则
- 项目架构分为backend(后端)、frontend（前端）、ai-engine（AI Langgraph编排层）、database（数据库层）、infra（容器配置层） 5个模块。模块直接互相解耦，方便更换其他语言实现。
- frontend只与backend交互，绝不跨层对接ai-engine；backend与ai-engine也互相隔离，不能让ai-engine去直接操作backend的数据库表
- 项目名称为ProductAssdistant 产品上线助手，简称pa, 本项目中出现的pa开头命名的，如无特殊说明即代表项目名
- database目录只放初始化SQL、迁移脚本与集合初始化脚本，绝不存放业务代码
- database包含2个SCHEMA和4个ROLE以及多个TABLE
  - SCHEMA（确保AI层和Backend互相隔离）
    - pa_backend 核心业务命名空间（给backend使用）
        - organizations 
        - sys_users 
        - products 
        - hitl_approvals
        - compliance_words
        - compliance_rules
        - generation_jobs
        - notification_outbox
        - delete-audits
    - pa_ai AI相关命名空间（给ai-engine使用）
        - product_contents 
        - evaluation_logs
        - langgraph_checkpoints(postgresSaver创建)
  - ROLE
    - pa_admin 管理员角色，拥有所有权限 (DDL)
    - pa_backend 负责管理backend (DML)
    - pa_ai 负责管理ai-engine (DML)
    - pa_ai_setup LangGraph建表使用（CREATE）

## 目录结构

```
database/
├── sql/
│   ├── 0001_schema.sql        # 角色 + schema + 全量表 DDL
│   ├── 0002_roles_grants.sql  # 四账号最小权限矩阵落地
│   ├── 0003_seed.sql          # demo 种子（租户/用户/商品/合规词库）
│   ├── 0004_delete_audit.sql  # admin 彻底删除审计表（
│   ├── 0005_align_product_constraints.sql  # 商品状态/库存约束对齐
│   └── 0006_hitl_approval_content_snapshot.sql  # hitl_approvals.content_snapshot（审批“AI 生成详情”）
├── milvus/
│   └── init_collections.py    # Milvus 集合初始化（pp_{env}_listing_vec）
└── tests/
    ├── conftest.py                # Testcontainers/本地 DSN 夹具
    ├── test_permission_matrix.py  # 权限矩阵实测
    └── test_migrations.py         # 幂等重放 + 约束/GRANT 语义
```

