"""守护进程配置：值表 → 配置对象（按默认值类型转换 + 认不出的键报错）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_tty.config import ConfigError
from agentic_tty.daemon.config import DaemonConfig


def test_defaults_are_the_known_keys():
    assert set(DaemonConfig.defaults()) == set(DaemonConfig.__dataclass_fields__)


def test_values_are_coerced_by_type():
    config = DaemonConfig.from_values(
        {
            "name": "x",
            "runtime_dir": "/tmp/run",
            "tick_interval": "0.01",
            "write_log_file": "false",
        }
    )
    assert config.name == "x"
    assert config.runtime_dir == Path("/tmp/run")
    assert config.tick_interval == 0.01
    assert config.write_log_file is False


def test_empty_string_means_not_given():
    assert DaemonConfig.from_values({"listen": ""}).listen is None


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError):
        DaemonConfig.from_values({"nmae": "typo"})


def test_bad_value_is_reported():
    with pytest.raises(ConfigError):
        DaemonConfig.from_values({"tick_interval": "soon"})


def test_bad_bool_is_reported():
    with pytest.raises(ConfigError):
        DaemonConfig.from_values({"write_log_file": "maybe"})
