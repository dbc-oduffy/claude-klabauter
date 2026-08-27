"""Regression tests for cross-platform home sandboxing.

Guards the 2026-07-20 incident in which this suite wrote pytest tmpdirs into
the real `~/.claude/.doe-root` on Windows because `monkeypatch.setenv("HOME")`
does not influence `os.path.expanduser("~")` there.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.testing.home_sandbox import sandbox_home


def test_sandbox_home_redirects_expanduser(tmp_path, monkeypatch):
    home = sandbox_home(monkeypatch, tmp_path / "home")

    # The load-bearing assertion: expanduser — not just os.environ["HOME"] —
    # must resolve into the sandbox. This is what fails on Windows under a
    # bare setenv("HOME", ...).
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
    """Every test — including ones that never sandbox anything — resolves `~`
    into a pytest-owned tmpdir, courtesy of the suite-root autouse fixture."""
    resolved = Path(os.path.expanduser("~")).resolve()

    # tmp_path_factory.mktemp suffixes a counter, hence startswith not ==.
    assert resolved.name.startswith("home-quarantine"), resolved


# ---------------------------------------------------------------------------
# `real_home` must not smuggle machine-mutation permission along with it
#
# The marker means "resolve the real home", scoped by its own docstring to
# read-only oracles. It used to be read BEFORE the fixture set
# COORDINATOR_DISABLE_MACHINE_MUTATION, so a marked test got the real home AND
# no kill switch — the pairing behind the live `.doe-root` pollution
# (state/bug-backlog/2026-08-26-a-test-writes-the-live-claude-machine-lo-
# 6cdf6bc87771.yaml). These three pin the three states apart.
# ---------------------------------------------------------------------------

_MUTATION_SWITCH = "COORDINATOR_DISABLE_MACHINE_MUTATION"


def test_ordinary_test_carries_the_machine_mutation_kill_switch():
    assert os.environ.get(_MUTATION_SWITCH) == "1"


@pytest.mark.real_home
def test_real_home_still_carries_the_machine_mutation_kill_switch():
    """The regression pin. Opting out of the HOME quarantine must not also opt
    out of the mutation kill switch: this test HAS the real home, and must still
    be forbidden from mutating real machine state."""
    resolved = Path(os.path.expanduser("~")).resolve()
    assert not resolved.name.startswith("home-quarantine"), (
        f"real_home did not hand back the real home ({resolved}) — this pin is "
        "vacuous unless the opt-out actually fired"
    )

    assert os.environ.get(_MUTATION_SWITCH) == "1"


@pytest.mark.real_machine_mutation
def test_named_marker_is_the_only_way_to_drop_the_kill_switch():
    """The deliberate, plan-authorized live write asks for it by name."""
    assert _MUTATION_SWITCH not in os.environ


@pytest.mark.real_home
@pytest.mark.real_machine_mutation
def test_the_paired_escape_hatch_is_what_the_markers_exist_to_gate():
    """The combination, not just its two halves independently.

    `real_home` alone gives the real home with the switch still on;
    `real_machine_mutation` alone drops the switch inside a quarantined home.
    Neither is the case the marker system exists for — that is a test which
    genuinely must mutate REAL machine state, which needs both at once. Pinning
    only the halves would let the pairing regress unnoticed.
    """
    resolved = Path(os.path.expanduser("~")).resolve()
    assert not resolved.name.startswith("home-quarantine"), (
        f"real_home did not hand back the real home ({resolved})"
    )
    assert _MUTATION_SWITCH not in os.environ
