"""分层强制：扫 AST 检查依赖方向，不靠自觉。"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agentic_tty"

# 允许的依赖：包 → 它可以依赖的顶层包
_ALLOWED: dict[str, frozenset[str]] = {
    "foundation": frozenset({"foundation"}),
    # 线协议是两端共享的契约，只压在 foundation 上
    "protocol": frozenset({"foundation", "protocol"}),
    "core": frozenset({"foundation", "core"}),
    # 运行时层是唯一可以碰原生扩展与平台 API 的层
    "runtime": frozenset({"foundation", "core", "runtime"}),
    # 只搬字节，不认识帧（帧在 protocol，缝合靠装配方注入）
    "transport": frozenset({"foundation", "transport"}),
    # 只认注入的请求处理接缝：不 import core，也不 import protocol / transport
    "daemon": frozenset({"foundation", "runtime", "daemon"}),
    # 示例层是各层的占位：既在进程内直连核心层当测试驱动，也演示守护进程与客户端这一对
    "example": frozenset(
        {"foundation", "protocol", "core", "runtime", "transport", "daemon", "example"}
    ),
}

# 核心链路不允许触碰的第三方（原生扩展 / web 框架）
_RESTRICTED_THIRD_PARTY = frozenset({"pywezterm", "fastapi", "starlette", "uvicorn"})
# 只有运行时层可以碰原生扩展
_ALLOWED_FOR_THIRD_PARTY = frozenset({"runtime"})


def _package_of(path: Path) -> str:
    return path.relative_to(SRC).parts[0]


def _resolve_relative(path: Path, node: ast.ImportFrom) -> str | None:
    parts = list(path.relative_to(SRC).parts[:-1])
    up = node.level - 1
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    if base:
        return base[0]
    if node.module:
        return node.module.split(".")[0]
    return None


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
                elif top in _RESTRICTED_THIRD_PARTY:
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
                elif top in _RESTRICTED_THIRD_PARTY:
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


def test_core_does_not_touch_restricted_third_party():
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        package = _package_of(path)
        if package not in _ALLOWED or package in _ALLOWED_FOR_THIRD_PARTY:
            continue
        _, third = _deps(path)
        for name in sorted(third):
            violations.append(f"{path.relative_to(SRC)}: {package} 不允许 import {name}")
    assert not violations, "核心链路触碰了受限依赖:\n" + "\n".join(violations)


def test_example_client_stays_a_pure_client():
    """客户端那一格只许依赖公共层——包级规则管不住它，所以单列一条。

    `example` 的允许集里有 `core` / `runtime` / `daemon`（验证台要直接接核心层），
    而客户端那一格不许碰它们。当前 `example/` 下没有客户端包，这条直接跳过；
    等客户端那一格回来（它依赖 `foundation + protocol + transport`），断言自动生效。
    """
    for package in sorted((SRC / "example").iterdir()):
        if not package.is_dir() or not (package / "__init__.py").exists():
            continue
        if package.name in {"core_test", "daemon_test"}:  # 验证台：直连核心层是它的职责
            continue
        for path in sorted(package.rglob("*.py")):
            deps, _ = _deps(path)
            allowed = {"foundation", "protocol", "transport"}
            extra = sorted(deps - allowed)
            assert not extra, f"{path.relative_to(SRC)} 越出客户端链: {extra}"
