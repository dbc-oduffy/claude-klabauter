"""Characterization test backing DR-387 (`docs/decisions/DR-387-the-op-
route-runs-no-git-hooks-by-construction.md`): `commit_paths` writes commit
objects directly and CAS-swaps the ref, so it spawns no process and runs no
git hook, for any caller, on every call.

WHY BEHAVIOURAL RATHER THAN SOURCE-TEXT. The sibling precedent
(`coordinator_core/ops/ceremony/tests/test_commit_v2_pre_commit_gates.py`)
pins its two omitted gates with `inspect.getsource` asserts, appropriate
there because the omission is a static fact about `commit_v2.py`'s source.
This test installs a REAL hook script into a temp repo's `.git/hooks/` and
observes its actual side effect instead, because a source-text assert would
share the very assumption it exists to test -- it would keep passing the day
the route starts spawning `git commit` through a call site whose text it
does not happen to match (`state/lessons/2026-08-30-an-instrument-that-
shares-its-subjects-assumption-cannot-falsify-it.yaml`, scope: universal).

THE POSITIVE CONTROL IS NOT OPTIONAL. `commit_paths` spawns no process by
construction, so the negative assertion alone is structurally unfalsifiable:
a broken hook fixture (wrong filename, missing exec bit, or, on Windows, a
script git cannot execute without a shell) and a genuinely-hookless route
are indistinguishable from the negative leg by itself. `test_hook_fixture_
control_fires_on_real_commit` proves the SAME installed hook, in the SAME
temp repo, DOES fire for an ordinary `git commit` before the characterization
test's negative leg is trusted as evidence of anything.

This test spawns real `git` processes as its oracle (repo setup, the control
commit, and post-hoc verification) -- never inside the `commit_paths` call
under test, which is the whole property being characterized.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git import commit as gcommit

# Spawns real external `git` processes as its oracle/fixture setup (never
# inside the commit_paths call under test); runs at cadence, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

# Windows git resolves a hook by trying, in order: the exact filename, then
# (via its own shebang-less .exe/.cmd/.bat/.sh handling through msys sh) a
# script it can actually execute. A python-scripted hook with a `.py`
# extension is not one git will exec directly on any platform, so the hook
# body is a tiny shell script invoked via the extensionless conventional
# name, which git's hook runner executes through `sh` on every platform
# (Windows git ships its own `sh.exe` for exactly this).
_HOOK_BODY = "#!/bin/sh\ntouch \"$(dirname \"$0\")/../../hook-fired.sentinel\"\n"


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo_with_hook(tmp_path: Path, hook_name: str) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")

    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / hook_name
    hook_path.write_text(_HOOK_BODY, encoding="utf-8", newline="\n")
    hook_path.chmod(0o755)
    return repo


def _sentinel(repo: Path) -> Path:
    # matches `../../hook-fired.sentinel` relative to `.git/hooks/<name>`
    return repo / "hook-fired.sentinel"


@pytest.mark.parametrize("hook_name", ["pre-commit", "commit-msg", "prepare-commit-msg"])
def test_hook_fixture_control_fires_on_real_commit(tmp_path, hook_name):
    """POSITIVE CONTROL, required before the negative leg counts as evidence
    (staff-eng review Finding 5): the SAME installed hook, in the SAME repo,
    DOES write the sentinel for an ordinary subprocess `git commit`. If this
    fails on a given platform, the fixture itself is broken there and the
    negative test below proves nothing -- it must be fixed, not skipped."""
    repo = _repo_with_hook(tmp_path, hook_name)
    sentinel = _sentinel(repo)
    assert not sentinel.exists()

    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "trip the hook")

    assert sentinel.exists(), (
        f"hook fixture never fired for {hook_name!r} via a real `git commit` -- "
        "the fixture itself is broken (platform: "
        f"{sys.platform}), not evidence commit_paths runs no hooks"
    )


@pytest.mark.parametrize("hook_name", ["pre-commit", "commit-msg", "prepare-commit-msg"])
def test_commit_paths_runs_no_hooks(tmp_path, hook_name):
    """NEGATIVE LEG / characterization: `commit_paths` must not trip the same
    hook the control above proves fires for a real `git commit`. Goes red
    the day someone makes the route spawn `git commit` -- the change DR-387
    exists to make deliberate rather than accidental."""
    repo = _repo_with_hook(tmp_path, hook_name)
    sentinel = _sentinel(repo)

    # Establish the control first, in this same repo/hook pairing, then
    # clear the sentinel so the commit_paths leg below starts clean.
    (repo / "control.txt").write_text("control\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "control commit")
    assert sentinel.exists(), (
        f"control leg did not fire for {hook_name!r} -- fixture is broken, "
        "fix it rather than trusting the negative leg below"
    )
    sentinel.unlink()

    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    outcome = gcommit.commit_paths(repo, ["new.txt"], "no hooks here")

    assert not sentinel.exists(), (
        f"{hook_name!r} fired for a commit_paths() call -- DR-387's pinned "
        "property (the op route spawns no git hooks) has been broken"
    )
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "no hooks here"
    assert outcome.sha
