"""分层强制：扫 AST 检查依赖方向，不靠自觉。"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agentic_tty"
VENDOR = Path(__file__).resolve().parents[1] / "vendor"

# 允许的依赖：包 → 它可以依赖的顶层包
_ALLOWED: dict[str, frozenset[str]] = {
    "foundation": frozenset({"foundation"}),
    # 线协议是两端共享的契约，只压在 foundation 上
    # 配置：本机实例的命名 ＋ 配置值加载；只压在 foundation 上（transport 够不着它）
    "config": frozenset({"foundation", "config"}),
    "protocol": frozenset({"foundation", "protocol"}),
    # 核心层（能力体）：core 内部可以直接用 core.runtime 的宿主实现
    "core": frozenset({"foundation", "core"}),
    # 沙箱（能力实现）：只压在核心层上——模式由装配处合并注册，core 反过来不认识它
    "sandbox": frozenset({"foundation", "core", "sandbox"}),
    # 只搬字节，不认识帧（帧在 protocol，缝合靠装配方注入）
    "transport": frozenset({"foundation", "transport"}),
    # 守护进程（那个进程）：机制不碰 core，只有 kernel / assembly / __main__ 碰
    "daemon": frozenset(
        {"foundation", "config", "protocol", "transport", "core", "sandbox", "daemon"}
    ),
    # 示例层是各层的占位：直连核心层当测试驱动，也演示守护进程与客户端这一对
    "example": frozenset(
        {
            "foundation",
            "config",
            "protocol",
            "core",
            "sandbox",
            "transport",
            "daemon",
            "example",
        }
    ),
}

# 受限第三方 → 只允许出现在这些目录前缀下
_ALLOWED_THIRD_PARTY: dict[str, frozenset[str]] = {
    # 原生扩展只允许出现在这些格子里——纯子进程场景因此不拖进 pywezterm
    "pywezterm": frozenset({"core/runtime/pywezterm_pty"}),
    "resvg_py": frozenset({"core/runtime/local_pty", "example/ui"}),
    # 沙箱运行时同样只在它自己那一格
    "winsandbox": frozenset({"sandbox/windows"}),
    # web 框架只允许出现在 web 层
    "fastapi": frozenset({"web"}),
    "starlette": frozenset({"web"}),
    "uvicorn": frozenset({"web"}),
}


def _package_of(path: Path) -> str:
    return path.relative_to(SRC).parts[0]


def _module_path(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _module_of(path: Path, node: ast.ImportFrom) -> str | None:
    """把相对导入解析成完整模块路径（`ui/screen.py` 里的 `.common` → `example.ui.common`）。"""
    parts = list(path.relative_to(SRC).parts[:-1])
    up = node.level - 1
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if node.module:
        base += node.module.split(".")
    return ".".join(base) if base else None


def _resolve_relative(path: Path, node: ast.ImportFrom) -> str | None:
    module = _module_of(path, node)
    return module.split(".")[0] if module else None


def _import_targets(path: Path, node: ast.AST) -> list[str]:
    """把一条 import 语句解析成完整模块路径列表。

    相对导入（`level > 0`）按文件位置解析；绝对导入原样取 `module`——`_module_of`
    只认相对导入，拿它解绝对导入会把 `import dataclasses` 错解成包内模块。
    """
    if isinstance(node, ast.ImportFrom):
        if node.level:
            module = _module_of(path, node)
            return [module] if module else []
        return [node.module] if node.module else []
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return []


def _example_peers(path: Path) -> set[str]:
    """该文件引用到的 `example.<格>`：包内相对引用与绝对引用都算。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                modules.append(_module_of(path, node))
            elif node.module:
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    peers: set[str] = set()
    for module in modules:
        parts = module.split(".") if module else []
        if parts[:1] == ["agentic_tty"]:
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "example":
            peers.add(parts[1])
    return peers


def _deps(path: Path) -> tuple[set[str], set[str]]:
    """返回 (项目内依赖, 受限第三方依赖)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    deps: set[str] = set()
    third: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                top = node.module.split(".")[0] if node.module else ""
                if top == "agentic_tty" and node.module and node.module.count(".") >= 1:
                    deps.add(node.module.split(".")[1])
                elif top in _ALLOWED_THIRD_PARTY:
                    third.add(top)
            else:
                resolved = _resolve_relative(path, node)
                if resolved:
                    deps.add(resolved)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top == "agentic_tty" and "." in alias.name:
                    deps.add(alias.name.split(".")[1])
                elif top in _ALLOWED_THIRD_PARTY:
                    third.add(top)
    return deps, third


def test_dependency_direction():
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        package = _package_of(path)
        allowed = _ALLOWED.get(package)
        if allowed is None:
            continue
        deps, _ = _deps(path)
        for dep in sorted(deps - allowed):
            violations.append(f"{path.relative_to(SRC)}: {package} → {dep}")
    assert not violations, "分层依赖违规:\n" + "\n".join(violations)


def test_restricted_third_party_stays_in_its_slot():
    """原生扩展与 web 框架只能出现在各自的格子里。"""
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        _, third = _deps(path)
        where = _module_path(path)
        for name in sorted(third):
            slots = _ALLOWED_THIRD_PARTY[name]
            if not any(where.startswith(slot + "/") for slot in slots):
                allowed = ", ".join(sorted(slots))
                violations.append(f"{where} 不允许 import {name}（只允许 {allowed}）")
    assert not violations, "受限依赖越界:\n" + "\n".join(violations)


def test_nothing_depends_on_example():
    """example 是纯消费者：只依赖别人，不被任何层依赖。"""
    violations = [
        _module_path(path)
        for path in sorted(SRC.rglob("*.py"))
        if _package_of(path) != "example" and "example" in _deps(path)[0]
    ]
    assert not violations, "有层反向依赖 example:\n" + "\n".join(violations)


def test_example_client_stays_a_pure_client():
    """客户端那一格只许依赖公共层，也不许伸手进别的格——包级规则管不住它，单列一条。

    `core_test_console/` 跳过：它是**进程内**的台，直连核心层是它的职责，import core 是本职。
    `daemon_test_console/` **不跳过**——它是**跨进程**的纯消费者，这条断言真的管得住它。
    `ui/` 是**共用格**（纯 Tk，不认识任何层），台子引用它不算跨格违规。
    """
    for package in sorted((SRC / "example").iterdir()):
        if not package.is_dir() or not (package / "__init__.py").exists():
            continue
        if package.name == "core_test_console":  # 进程内的台：直连核心层是它的职责
            continue
        for path in sorted(package.rglob("*.py")):
            deps, _ = _deps(path)
            # "example" 是包内引用的自指，格与格之间由下面那条单独管
            extra = sorted(deps - {"foundation", "config", "protocol", "transport", "example"})
            assert not extra, f"{path.relative_to(SRC)} 越出客户端链: {extra}"
            peers = _example_peers(path) - {package.name, "ui"}
            assert not peers, f"{path.relative_to(SRC)} 伸手进了别的格: {sorted(peers)}"


_DAEMON_CORE_FILES = frozenset({"kernel.py", "assembly.py", "__main__.py"})
"""`daemon/` 里**允许** import core 的文件——其余是"机制"，必须与 core 无关。"""


def test_only_the_named_daemon_files_may_import_core():
    """`daemon/` 的机制不认 core：换掉请求处理层，机制一行不改。

    白名单而不是黑名单——以后往 `daemon/` 加新的机制文件，自动被管住。
    """
    violations: list[str] = []
    for path in sorted((SRC / "daemon").rglob("*.py")):
        if path.name in _DAEMON_CORE_FILES:
            continue
        deps, _ = _deps(path)
        if "core" in deps:
            violations.append(f"{path.relative_to(SRC)} → core")
    assert not violations, "daemon 的机制文件 import 了 core:\n" + "\n".join(violations)


_SPAWN_MODULES = frozenset({"subprocess", "multiprocessing"})
_SPAWN_CALLS = ("subprocess.", "os.system", "os.popen", "os.spawn", "os.exec", "os.fork")
"""启动进程的调用——消费者格一个都不许有。"""


def _called_name(node: ast.expr) -> str | None:
    """把调用目标还原成点号链：`subprocess.Popen(...)` → `"subprocess.Popen"`。"""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def test_consumer_packages_never_spawn_processes():
    """**守护进程只由用户启动**：消费者格（含验证台）不得拉子进程。

    它们只被"告知"要连哪个名字，然后自己算地址去连。`tests/` 里起守护进程是为了测它本身、
    用的是它自己的接口，所以这条只管 `example/`。
    """
    violations: list[str] = []
    for path in sorted((SRC / "example").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _SPAWN_MODULES:
                        violations.append(f"{path.relative_to(SRC)} → import {alias.name}")
            elif isinstance(node, ast.Call):
                name = _called_name(node.func)
                if name is not None and name.startswith(_SPAWN_CALLS):
                    violations.append(f"{path.relative_to(SRC)} → {name}()")
    assert not violations, "消费者格启动了进程:\n" + "\n".join(violations)


def test_protocol_base_does_not_import_contracts():
    """通用底座（帧 / 信封 / 响应 / 错误）不认识任何边界。"""
    base = ("__init__.py", "frame.py", "envelope.py", "response.py", "errors.py")
    violations: list[str] = []
    for rel in base:
        path = SRC / "protocol" / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for target in _import_targets(path, node):
                if target.startswith("protocol.contracts"):
                    violations.append(f"{path.relative_to(SRC)} → {target}")
    assert not violations, "protocol 底座反向依赖了分区:\n" + "\n".join(violations)


def test_protocol_partitions_are_independent():
    """`protocol/contracts/` 的分区互不依赖、互不共享类型——改一条边界不波及另一条。

    分区只许压在通用底座上；哪怕命令名相同，也各留一份。
    """
    contracts = SRC / "protocol" / "contracts"
    partitions = sorted(p.stem for p in contracts.glob("*.py") if p.stem != "__init__")
    assert len(partitions) >= 2, "protocol/contracts 下应当有两条以上分区"
    violations: list[str] = []
    for name in partitions:
        path = contracts / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for target in _import_targets(path, node):
                if target.startswith("protocol.contracts.") and target != (
                    f"protocol.contracts.{name}"
                ):
                    violations.append(f"{path.relative_to(SRC)} → {target}")
    assert not violations, "protocol 分区互相依赖:\n" + "\n".join(violations)


def test_daemon_only_uses_the_local_pipe():
    """接入点是本机 IPC——daemon 不许碰 transport 的网络部分与 scheme 分派。"""
    forbidden = {"transport.tcp", "transport.registry"}
    violations: list[str] = []
    for path in sorted((SRC / "daemon").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for target in _import_targets(path, node):
                if target in forbidden:
                    violations.append(f"{path.relative_to(SRC)} → {target}")
    assert not violations, "daemon 用了 transport 的网络部分:\n" + "\n".join(violations)


_VENDORED_MODULES = ("condrv", "openpty", "winsandbox")
"""`vendor/` 里的模块——它们与第三方依赖放在同一处，所以方向得自己守住。

`winsandbox` 是发行方的包装层（原地不动），一起管住：哪天要改它，反向依赖 `agentic_tty`
一样会把层次倒过来。"""


def _top_level_imports(path: Path) -> list[str]:
    """只取绝对 import 的顶层名（`vendor/` 不在 `SRC` 下，解不了相对导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def test_vendored_modules_never_import_the_app():
    """`vendor/` 里的模块是被依赖方，反向 import `agentic_tty` 就把层次倒过来了。

    分层测试只扫 `src/agentic_tty`，够不到这里，所以单列一条。
    """
    violations: list[str] = []
    for name in _VENDORED_MODULES:
        package = VENDOR / name
        if not package.is_dir():
            continue
        for path in sorted(package.rglob("*.py")):
            for target in _top_level_imports(path):
                if target.split(".")[0] == "agentic_tty":
                    violations.append(f"vendor/{name}/{path.name} → {target}")
    assert not violations, "vendor 模块反向依赖了 agentic_tty:\n" + "\n".join(violations)
