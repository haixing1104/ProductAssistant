"""pa_backend：ProductAssistant 业务 API 网关层（FastAPI）。

分层：main（装配）/ core（配置·DB·安全·依赖）/ middleware / models / repositories /
services / routers / schemas。

红线：不含任何 AI 逻辑；只以 role_pa_backend 连库；与 ai-engine 只经 Redis Streams 通信。
"""
