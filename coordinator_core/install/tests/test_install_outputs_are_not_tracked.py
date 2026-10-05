"""Installer outputs live outside the tracked tree or are ignored, so an install leaves the clone clean."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO_ROOT = Path(__file__).resolve().parents[3]

GENERATED = [
    "coordinator_core/warm/door/door.engine-root.txt",
    "coordinator_core/warm/door/door",
    "coordinator_core/warm/door/door.provenance.json",
    "coordinator_core.egg-info/PKG-INFO",
    "coordinator_core.egg-info/SOURCES.txt",
]


def _git(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *argv], capture_output=True, text=True, check=False, **no_console_creationflags())


@pytest.fixture(scope="module", autouse=True)
def _in_git_checkout():
    if _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout")


@pytest.mark.parametrize("rel", GENERATED)
def test_generated_output_is_ignored(rel):
    assert _git("check-ignore", "-q", rel).returncode == 0, f"{rel} is not gitignored"


@pytest.mark.parametrize("rel", GENERATED)
def test_generated_output_is_not_tracked(rel):
    assert _git("ls-files", "--", rel).stdout.strip() == ""


def test_no_egg_info_is_tracked():
    assert _git("ls-files", "--", "*.egg-info/*").stdout.strip() == ""


def test_installed_door_lands_in_settings_home_bin_not_the_tree():
    from coordinator_core.install import door_install

    src = Path(door_install.__file__).read_text(encoding="utf-8")
    assert "build_or_advise(engine_root, output=dest_exe)" in src
