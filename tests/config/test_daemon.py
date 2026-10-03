"""各层的配置对象：值表 → 对象（按默认值类型转换 + 认不出的键报错）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_tty.config import ConfigError, ConsumerConfig, DaemonConfig


def test_defaults_are_the_known_keys():
    assert set(DaemonConfig.defaults()) == set(DaemonConfig.__dataclass_fields__)
    assert set(ConsumerConfig.defaults()) == set(ConsumerConfig.__dataclass_fields__)


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


def test_consumer_config_only_knows_who_to_connect_to():
    """消费者要配的只有"连谁"——守护进程那些字段它一概不认。"""
    assert ConsumerConfig.from_values({"name": "daemon-test"}).name == "daemon-test"
    assert ConsumerConfig().name == DaemonConfig().name  # 默认实例名是两端共用的约定
    with pytest.raises(ConfigError):
        ConsumerConfig.from_values({"tick_interval": "0.01"})
