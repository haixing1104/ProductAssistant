"""backend-api 测试包。

为什么需要 ``__init__.py``:
    用例之间要复用夹具与工具函数（如 ``from tests.conftest import _exec``）。
    把 tests/ 变成常规包 + pyproject 里 ``pythonpath = [".", "src"]``，
    导入行为在「本机 venv」与「容器 /work/backend-api」下完全一致，
    不依赖 pytest 隐式往 sys.path 插目录的行为（那种行为随导入模式/配置漂移，很难排查）。
"""
