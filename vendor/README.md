# vendor —— 不安装的长期依赖

这个目录放**不通过 pip 安装**的长期依赖与平台原语。`core/runtime/vendor.py` 会在
导入宿主之前把它接进 `sys.path`，因此用哪个解释器、从哪个目录起都一样，
调用者不需要配 `PYTHONPATH`。

两类内容，版本控制待遇不同：

- **第三方二进制产物**（`pywezterm/`、`condrv/OpenConsole.exe`）：不进版本控制，
  按下面各节的方式补齐；
- **自研模块**（`condrv/`、`openpty/` 里的 `.py`）：进版本控制。它们是被依赖方，
  **不得 import `agentic_tty`**（`tests/test_layering.py` 强制）。

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

## openpty

Linux 的伪终端。纯标准库（`os.forkpty` / `fcntl` / `termios`），**没有外部产物**，
不需要补齐什么。

`os.forkpty()` 一次把 fork、`setsid`、开 slave、`login_tty`（取控制终端）与 dup2 都
做完，子进程只剩「设尺寸 → chdir → exec」。exec 成败经一条 CLOEXEC 管道同步回报，
所以命令不存在时 `spawn` 直接抛错，而不是留下一个随即死掉的会话。

## 缺失时的行为

依赖缺失**不会**静默降级：宿主的惰性导入会抛 `DependencyMissing`，那一次
`create_session` 明确失败并回报调用方。依赖是**按模式**算的（`pty` 要 pywezterm、
`localpty` 要平台原语），所以缺一个不影响另一个——守护进程照常服务、别的模式照常可用；
纯 `subprocess` 场景不碰这些扩展，没有它们也能跑。
