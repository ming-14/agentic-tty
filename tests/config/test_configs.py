"""各层的配置对象：**字段默认值就是配置常量**——没有别的地方可配。"""

from dataclasses import FrozenInstanceError

import pytest

from agentic_tty.config import DEFAULT_INSTANCE, ConsumerConfig, DaemonConfig


def test_name_comes_from_the_constant():
    """名字来自常量——两端同一个，且没有别的地方可配。"""
    assert DaemonConfig().name == DEFAULT_INSTANCE
    assert ConsumerConfig().name == DEFAULT_INSTANCE


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
