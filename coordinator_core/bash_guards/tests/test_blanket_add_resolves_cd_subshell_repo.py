"""check_blanket_git_add probes the repo at the command's effective cwd
(git -C, a leading or subshell-paren cd, a cd chained after non-cd segments),
not the guard process's cwd. Fixture repos are tmp_path ``git init``s; the
hazard discriminator is pointed at the one repo standing in for the shared tree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _dc_git_shared, dispatch_checks
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _init(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(path),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )
    (path / "coordinator_core").mkdir()
    (path / "coordinator_core" / "f.py").write_text("x", encoding="utf-8")
    return path


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hazard = _init(tmp_path / "shared")
    scratch = _init(tmp_path / "scratch")
    hazard_real = os.path.realpath(hazard)
    monkeypatch.setattr(
        _dc_git_shared,
        "_is_hazard_repo",
        lambda root: os.path.realpath(root) == hazard_real,
    )
    monkeypatch.chdir(hazard)
    return hazard, scratch


def _denied(cmd: str) -> bool:
    out = envelope_of(dispatch_checks.check_blanket_git_add(cmd, "sess-cd-subshell"))
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ac1_subshell_cd_init_and_blanket_add_silent(repos) -> None:
    _, scratch = repos
    assert not _denied(f"(cd {scratch} && git init -q && git add .)")


def test_ac2_subshell_cd_pathspec_add_silent(repos) -> None:
    _, scratch = repos
    assert not _denied(f"(cd {scratch} && git add -- coordinator_core)")


@pytest.mark.parametrize(
    "tmpl",
    [
        "cd {s} && git add .",
        "cd {s}; git add -A",
        "git -C {s} add .",
        "git -C '{s}' add .",
        "mkdir -p x && cd {s} && git add .",
        "(cd {s} && git add .)",
    ],
)
def test_ac3_cd_prefix_and_dash_c_forms_silent(repos, tmpl: str) -> None:
    _, scratch = repos
    assert not _denied(tmpl.format(s=scratch))


def test_ac3_relative_dash_c_resolves_against_cd(repos) -> None:
    _, scratch = repos
    assert not _denied(f"cd {scratch.parent} && git -C scratch add .")


@pytest.mark.parametrize(
    "tmpl",
    [
        "git add .",
        "git add -A",
        "cd {h} && git add .",
        "(cd {h} && git add .)",
        "git -C {h} add .",
        "cd {s} && git add x; cd {h} && git add .",
        "(cd {s} && git add x) ; git add .",
    ],
)
def test_ac4_shared_tree_blanket_add_still_denied(repos, tmpl: str) -> None:
    hazard, scratch = repos
    assert _denied(tmpl.format(h=hazard, s=scratch))


@pytest.mark.parametrize(
    "tmpl",
    [
        "cd {s}/* && git add .",
        "cd $SCRATCH && git add .",
        "cd {s} && cd $X && git add .",
    ],
)
def test_ac5_unresolvable_cd_target_falls_back_to_session_cwd(repos, tmpl: str) -> None:
    _, scratch = repos
    assert _denied(tmpl.format(s=scratch))


def test_blanket_add_exempts_scratch_clone_even_when_registered(tmp_path, monkeypatch):
    live = _init(tmp_path / "live")
    clone = live / "scratch" / "fixture clone"
    clone.parent.mkdir()
    subprocess.run(
        ["git", "clone", "--local", "-q", str(live), str(clone)],
        cwd=live, check=True, capture_output=True, **no_console_creationflags(),
    )
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda root: True)
    monkeypatch.chdir(live)

    assert _denied("git add -A")
    assert _denied("cd scratch && git add -A")
    assert not _denied(f'git -C "{clone}" add -A')
    assert not _denied(f'cd "{clone}" && git add -A')
    assert not _denied('git -C scratch -C "fixture clone" add -A')
    assert _denied(f'git -C "{clone}" --git-dir "{live / ".git"}" add -A')
    assert _denied(f'git -C "{clone}" add -A; git add -A')
    monkeypatch.chdir(clone)
    assert _denied(f'git -C "{live}" add -A')
