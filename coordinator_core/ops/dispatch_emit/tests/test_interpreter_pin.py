"""interpreter_pin.remedy_for: the remedy names the absolute config file of the repo whose pin is read."""
from __future__ import annotations

import os

from coordinator_core.ops.dispatch_emit import interpreter_pin as ti


def test_config_path_is_absolute_and_per_repo_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert ti.config_path(a) == str(a / ti.CONFIG_FILE) and os.path.isabs(ti.config_path(a))
    assert ti.config_path(a) != ti.config_path(b)


def test_config_path_resolves_a_relative_root_against_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ti.config_path(".") == str(tmp_path / ti.CONFIG_FILE)


def test_remedy_for_carries_the_remedy_and_the_absolute_path(tmp_path):
    text = ti.remedy_for(tmp_path)
    assert text.startswith(ti.REMEDY) and str(tmp_path / ti.CONFIG_FILE) in text
    assert str(tmp_path / ti.CONFIG_FILE) not in ti.REMEDY
