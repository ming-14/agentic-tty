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
    # 开发接口：用例编排，不认识传输与适配器
    "service": frozenset({"foundation", "protocol", "core", "runtime", "service"}),
    # 外网兼容层：只搬字节与帧，不认识业务（客户端链也止于这里）
    "transport": frozenset({"foundation", "protocol", "transport"}),
    # 接入层：客户端形态只用 transport + protocol，进程内挂载形态才用得上 service
    "adapters": frozenset({"foundation", "protocol", "service", "transport", "adapters"}),
    # 装配层：摸得到它要装配的一切，但不该认识适配器
    "daemon": frozenset(
        {"foundation", "protocol", "core", "runtime", "service", "transport", "daemon"}
    ),
    # 示例层是核心层的测试驱动与各层的占位：经 transport 演示客户端，但不走 service
    "example": frozenset({"foundation", "protocol", "core", "runtime", "transport", "example"}),
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
