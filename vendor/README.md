# vendor —— 不安装的长期依赖

这个目录放**不通过 pip 安装**的长期依赖与平台原语。`core/runtime/vendor.py` 会在
导入宿主之前把它接进 `sys.path`，因此用哪个解释器、从哪个目录起都一样，
调用者不需要配 `PYTHONPATH`。

两类内容，版本控制待遇不同：

- **第三方二进制产物**（`pywezterm/`、`condrv/OpenConsole.exe`）：不进版本控制，
  按下面各节的方式补齐；
- **自研模块**（`condrv/` 里的 `.py`）：进版本控制。它们是被依赖方，**不得 import
  `agentic_tty`**（`tests/test_layering.py` 强制）。

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

## condrv

Windows 的 ConDrv 直连伪终端。不走 `CreatePseudoConsole` API，而是直接打开
`\Device\ConDrv\Server` 并自己拼 conhost 命令行——这样 conhost 可以是自带的宿主，
而那个 API 只会起系统 `conhost.exe`。

**只有 conhost 宿主是外部产物**：把 Windows Terminal 发行包里的 `OpenConsole.exe`
放到 `vendor/condrv/` 即可。该文件不进版本控制。

缺失时的行为：`condrv.find_conhost()` 抛 `FileNotFoundError`，**不退回系统
`conhost.exe`**——走 ConDrv 直连的意义正在于换掉它。

## 缺失时的行为

依赖缺失**不会**静默降级：`core.runtime.host_factory.check_dependencies()` 会抛
`DependencyMissing`，调用方据此拒绝启动——不会出现"起来了却建不出会话"的进程。
纯 `subprocess` 场景不碰这些扩展，因此没有它们也能跑。
