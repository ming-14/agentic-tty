# vendor —— 不安装的长期依赖

这个目录放**不通过 pip 安装**的长期依赖与平台原语。`core/runtime/vendor.py` 会在
导入宿主之前把它接进 `sys.path`，因此用哪个解释器、从哪个目录起都一样，
调用者不需要配 `PYTHONPATH`。

两类内容，版本控制待遇不同：

- **第三方二进制产物**（`pywezterm/`、`condrv/OpenConsole.exe`、`winsandbox/`）：
  不进版本控制，按下面各节的方式补齐；
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

## winsandbox

Windows 沙箱运行时（受限令牌 + 能力 SID 写白名单 + 作业），`sandbox_pty` 模式的依赖。
in-process 的 nanobind 扩展，**不走 pip 安装**。

**整个包目录 `vendor/winsandbox/` 不进版本控制**（含 `_native/*.pyd`——扩展按构建机的
CPython 版本编，本仓库用的是 `cp311`）。补齐方式：把 win-sandbox 发行包的
`dist/win_sandbox/` 整个拷成
`vendor/winsandbox/`——**目录名即导入名**，所以 `import winsandbox` 找得到它；包内的
`win_sandbox_native` 由它自己把 `_native/` 接进 `sys.path`，与目录名无关。

覆盖的语义：子进程的可写区只有**它的工作区**（工作区根与子进程 cwd 是同一个值）与
**每次 spawn 私有的 temp**（`TMP`/`TEMP` 指向它，进程退出即回收），**其余位置一律
只读**。`workspace_write=False` 只把工作区也关掉，私有 temp 照旧——宿主 temp 根目录在
写白名单之外，拿不到可写 temp 的进程（DLL 初始化、解释器）根本起不来。

工作区是**常驻授权**：可写档跑过一次，那条 ACE 就留在那棵目录树上（下次同一个工作区
直接复用），而**别把它落在你没打算给写的目录上**——尤其是调用方没给 cwd 时退回进程
当前目录那种情形（服务从仓库根起的，工作区就是整个仓库）。

每次 spawn 会给**宿主进程**的 DACL 加两条 Deny（logon SID + Everyone，含
`PROCESS_TERMINATE`）：跑过沙箱会话的进程因此不能再被任务管理器 / `taskkill` 强杀，
只能自己退出。这是它的 confinement 设计，不是错误。

## openpty

Linux 的伪终端。纯标准库（`os.forkpty` / `fcntl` / `termios`），**没有外部产物**，
不需要补齐什么。

`os.forkpty()` 一次把 fork、`setsid`、开 slave、`login_tty`（取控制终端）与 dup2 都
做完，子进程只剩「设尺寸 → chdir → exec」。exec 成败经一条 CLOEXEC 管道同步回报，
所以命令不存在时 `spawn` 直接抛错，而不是留下一个随即死掉的会话。

## 缺失时的行为

依赖缺失**不会**静默降级：宿主的惰性导入会抛 `DependencyMissing`，那一次
`create_session` 明确失败并回报调用方。依赖是**按模式**算的（`pty` 要 pywezterm、
`localpty` 要平台原语、`sandbox_pty` 要 winsandbox），所以缺一个不影响另一个——守护
进程照常服务、别的模式照常可用；
纯 `subprocess` 场景不碰这些扩展，没有它们也能跑。
