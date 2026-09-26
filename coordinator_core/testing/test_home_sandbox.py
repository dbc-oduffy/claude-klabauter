
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.testing.home_sandbox import sandbox_home


def test_sandbox_home_redirects_expanduser(tmp_path, monkeypatch):
    home = sandbox_home(monkeypatch, tmp_path / "home")

    assert Path(os.path.expanduser("~")) == home


def test_sandbox_home_creates_the_directory(tmp_path, monkeypatch):
    home = sandbox_home(monkeypatch, tmp_path / "not-yet-there")

    assert home.is_dir()


def test_sandbox_home_clears_windows_second_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEDRIVE", "C:")
    monkeypatch.setenv("HOMEPATH", r"\Users\someone-else")

    sandbox_home(monkeypatch, tmp_path / "home")

    # HOMEDRIVE+HOMEPATH is expanduser's fallback tier on Windows; leaving it
    # populated would readmit the real profile if USERPROFILE were unset.
    assert "HOMEDRIVE" not in os.environ
    assert "HOMEPATH" not in os.environ


def test_suite_conftest_quarantines_real_home_by_default():
    resolved = Path(os.path.expanduser("~")).resolve()

    assert resolved.name.startswith("home-quarantine"), resolved


# COORDINATOR_DISABLE_MACHINE_MUTATION, so a marked test got the real home AND

_MUTATION_SWITCH = "COORDINATOR_DISABLE_MACHINE_MUTATION"


def test_ordinary_test_carries_the_machine_mutation_kill_switch():
    assert os.environ.get(_MUTATION_SWITCH) == "1"


@pytest.mark.real_home
def test_real_home_still_carries_the_machine_mutation_kill_switch():
    resolved = Path(os.path.expanduser("~")).resolve()
    assert not resolved.name.startswith("home-quarantine"), (
        f"real_home did not hand back the real home ({resolved}) — this pin is "
        "vacuous unless the opt-out actually fired"
    )

    assert os.environ.get(_MUTATION_SWITCH) == "1"


@pytest.mark.real_machine_mutation
def test_named_marker_is_the_only_way_to_drop_the_kill_switch():
    assert _MUTATION_SWITCH not in os.environ


@pytest.mark.real_home
@pytest.mark.real_machine_mutation
def test_the_paired_escape_hatch_is_what_the_markers_exist_to_gate():
    resolved = Path(os.path.expanduser("~")).resolve()
    assert not resolved.name.startswith("home-quarantine"), (
        f"real_home did not hand back the real home ({resolved})"
    )
    assert _MUTATION_SWITCH not in os.environ
