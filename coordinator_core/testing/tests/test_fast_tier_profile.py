from __future__ import annotations


import pluggy
import pytest

from coordinator_core.testing import fast_tier_profile as ftp


class _Config:
    def __init__(self, workerid=None):
        self.pluginmanager = pluggy.PluginManager("pytest")
        if workerid:
            self.workerinput = {"workerid": workerid}


def test_unset_env_registers_nothing():
    config = _Config()
    assert ftp.register(config, {}) is None
    assert ftp.register(config, {ftp.ENV_KEY: ""}) is None
    assert config.pluginmanager.get_plugins() == set()


def test_env_set_registers_plugin_with_worker_id(tmp_path):
    config = _Config("gw3")
    plugin = ftp.register(config, {ftp.ENV_KEY: str(tmp_path / "p.json")})
    assert config.pluginmanager.get_plugin(ftp.PLUGIN_NAME) is plugin
    assert plugin.worker == "gw3"


def test_merge_tail_idle_is_wall_minus_busiest_worker():
    workers = [
        {"worker": "gw0", "tests": 5, "collection_s": 40.0, "session_setup_s": 50.0, "busy_s": 300.0, "tail_s": 2.0},
        {"worker": "gw1", "tests": 4, "collection_s": 42.0, "session_setup_s": 55.0, "busy_s": 200.0, "tail_s": 90.0},
        {"worker": "controller", "tests": 0, "collection_s": 1.0, "session_setup_s": 1.0, "busy_s": 0.0, "tail_s": 0.0},
    ]
    merged = ftp.merge(workers, 500.0)
    assert merged["tail_idle_s"] == 200.0 and merged["max_busy_s"] == 300.0
    assert merged["collection_s"] == 42.0 and merged["session_setup_s"] == 55.0 and merged["worker_tail_s"] == 90.0
    assert merged["workers"] == 2 and merged["sum_busy_s"] == 500.0
