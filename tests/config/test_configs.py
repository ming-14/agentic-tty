"""各层的配置对象：**字段默认值就是配置常量**，入口 / 调用方可以覆盖实例名与端点名。"""

from dataclasses import FrozenInstanceError

import pytest

from agentic_tty.config import DEFAULT_INSTANCE, ConsumerConfig, DaemonConfig


def test_name_comes_from_the_constant():
    """名字来自常量——两端同一个。"""
    assert DaemonConfig().name == DEFAULT_INSTANCE
    assert ConsumerConfig().name == DEFAULT_INSTANCE


def test_endpoint_defaults_to_derived_from_name():
    """端点默认留空 = 由实例名派生；两端都能显式覆盖（`--listen`）。"""
    assert DaemonConfig().endpoint is None
    assert ConsumerConfig().endpoint is None
    assert DaemonConfig(name="foo", endpoint="my-pipe").endpoint == "my-pipe"
    assert ConsumerConfig(name="foo", endpoint="my-pipe").endpoint == "my-pipe"


def test_daemon_defaults_are_the_assembly_parameters():
    config = DaemonConfig()
    assert config.mount_endpoint is True
    assert config.tick_interval > 0
    assert config.stop_timeout > config.drain_timeout
    assert config.write_log_file is True


def test_configs_are_frozen():
    """配置是显式对象、一次定死——不许运行期改。"""
    with pytest.raises(FrozenInstanceError):
        DaemonConfig().tick_interval = 1.0
