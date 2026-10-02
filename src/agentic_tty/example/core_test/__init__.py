"""核心层的测试：连到 core，专门测它的能力。

里面分两类东西：

- `runtime_fakehost/` —— 给 core 提供宿主能力的假实现（与 `runtime/` 同生态位）。
- 其余 —— 装配（`sessions.py`）、假程序脚本（`programs.py`）、
  直连核心层的 Tk 测试台（`gui.py` + `__main__.py`）。

`python -m agentic_tty.example.core_test` 打开测试台。
"""
