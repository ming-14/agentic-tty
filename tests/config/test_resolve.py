"""配置合并：**命令行 > 环境变量 > 文件 > 默认值**；认不出的键报错。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_tty.config import ConfigError, env_values, file_values, resolve

_DEFAULTS = {"name": "default", "listen": None, "tick_interval": 0.005}


def test_precedence_is_argv_then_env_then_file(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('name = "from-file"\nlisten = "from-file"\n', encoding="utf-8")
    merged = resolve(
        _DEFAULTS,
        argv={"name": "from-argv"},
        environ={"AGENTIC_TTY_LISTEN": "from-env"},
        file=path,
    )
    assert merged == {"name": "from-argv", "listen": "from-env", "tick_interval": 0.005}


def test_defaults_win_when_nothing_is_given():
    assert resolve(_DEFAULTS) == _DEFAULTS


def test_unknown_key_is_rejected():
    """拼错一个键静默失效最坑人——直接报错。"""
    with pytest.raises(ConfigError):
        resolve(_DEFAULTS, argv={"nmae": "typo"})


def test_env_prefix_is_stripped_and_lowered():
    assert env_values({"AGENTIC_TTY_TICK_INTERVAL": "1", "OTHER": "x"}) == {
        "tick_interval": "1"
    }


def test_missing_file_is_reported(tmp_path: Path):
    with pytest.raises(ConfigError):
        file_values(tmp_path / "nope.toml")


def test_bad_toml_is_reported(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text("name = \n", encoding="utf-8")
    with pytest.raises(ConfigError):
        file_values(path)


def test_dashes_in_file_keys_become_underscores(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('tick-interval = 0.01\n', encoding="utf-8")
    assert file_values(path) == {"tick_interval": 0.01}
