# vendor —— 不安装的长期依赖

这个目录放**不通过 pip 安装**的长期依赖（原生扩展）。`runtime/vendor.py` 会在
导入宿主之前把它接进 `sys.path`，因此用哪个解释器、从哪个目录起都一样，
调用者不需要配 `PYTHONPATH`。

## pywezterm

`pywezterm` 提供 PTY 引擎（portable-pty / ConPTY）与终端模型（wezterm-term），
是 `pty` 会话的宿主依赖。

**包目录 `vendor/pywezterm/` 不进版本控制**（二进制体积大）。补齐方式：

1. 从上游 Releases 下载对应平台的 wheel（例如 `pywezterm-<版本>-cp38-abi3-win_amd64.whl`）；
2. 把 wheel 里的 `pywezterm/` 包目录解出来放到本目录下：

   ```bash
   python -m zipfile -e pywezterm-<版本>-<平台>.whl .
   ```

3. 确认目录里有 `__init__.py` 与扩展模块（`pywezterm.pyd` / `pywezterm.so`）。

## 缺失时的行为

依赖缺失**不会**静默降级：`runtime.host_factory.check_dependencies()` 会抛
`DependencyMissing`，调用方据此拒绝启动——不会出现"起来了却建不出会话"的进程。
纯 `subprocess` 场景不碰这个扩展，因此没有它也能跑。
