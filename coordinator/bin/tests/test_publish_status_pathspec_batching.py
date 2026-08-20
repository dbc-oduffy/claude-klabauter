"""`_git_status_porcelain` must batch a long pathspec instead of dying on it.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md — found
while publishing the warm engine's operator stop hatch, 2026-08-19.

THE INCIDENT THIS PINS. Windows caps a `CreateProcess` command line at 32767
characters. The `claude-klabauter-coordinator-bin` row's allowlist names every
tracked top-level entry under `coordinator/bin` — 948 of them, ~35k characters
once each carries a `:(literal)` prefix. Python raised `WinError 206` before
git ran; `_git_status_porcelain` mapped that `OSError` to `(1, "")`; and
`check_dirty_tree`'s explicit-pathspec leg is fail-CLOSED, so the row REFUSED
and did not publish while the round reported PASS for the other eight rows.
The visible symptom named neither length nor Windows.

`git status` does NOT accept `--pathspec-from-file` (only the commit/add
family does), so the fix is batching. Batching is only sound because every
entry reaching this function is an inclusion: `_publish_relevant_allowlist_leg`
resolves `!`-prefixed entries by subtraction upstream and never hands git a
literal `!...` path. The union-equality case below is what would break first
if that ever changed.

NEGATIVE-SPEC:
  - Does NOT assert a batch count or a specific budget. Both are tuning; the
    contract is that the union equals the single-call result.
  - Does NOT cover `check_dirty_tree`'s own legs (warn-and-proceed, override,
    fail-open pathspec-less default) — those are unchanged by this fix.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_status_pathspec_batching_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "status-batching-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Status Batching Test")
    # Names long enough that a few hundred of them blow past any argv budget.
    for i in range(400):
        (root / f"entry-with-a-deliberately-long-name-{i:04d}.py").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


def _all_entries(repo: Path) -> list[str]:
    return sorted(f":(literal){p.name}" for p in repo.iterdir() if p.name != ".git")


def test_a_pathspec_far_past_the_argv_cap_still_answers(repo: Path, monkeypatch):
    """The original failure: this raised WinError 206 and surfaced as
    'refusing to treat this as clean'."""
    monkeypatch.setattr(publish, "_PATHSPEC_ARGV_BUDGET", 2000)
    spec = _all_entries(repo)
    assert sum(len(s) + 1 for s in spec) > 2000 * 3, "fixture must exceed several batches"

    (repo / "entry-with-a-deliberately-long-name-0007.py").write_text("dirty\n", encoding="utf-8")

    rc, out = publish._git_status_porcelain(repo, spec)

    assert rc == 0
    assert "entry-with-a-deliberately-long-name-0007.py" in out


def test_batched_union_equals_the_single_call_result(repo: Path, monkeypatch):
    for i in (3, 11, 250, 399):
        (repo / f"entry-with-a-deliberately-long-name-{i:04d}.py").write_text("dirty\n", encoding="utf-8")
    spec = _all_entries(repo)

    monkeypatch.setattr(publish, "_PATHSPEC_ARGV_BUDGET", 10**9)
    rc_single, out_single = publish._git_status_porcelain(repo, spec)
    monkeypatch.setattr(publish, "_PATHSPEC_ARGV_BUDGET", 1500)
    rc_batched, out_batched = publish._git_status_porcelain(repo, spec)

    assert rc_single == 0 and rc_batched == 0
    assert sorted(out_single.splitlines()) == sorted(out_batched.splitlines())
    assert len(out_single.splitlines()) == 4


def test_a_failing_batch_returns_no_partial_output(repo: Path, monkeypatch):
    """A fail-closed caller must not receive a partial answer that looks whole."""
    calls = {"n": 0}
    real_run = subprocess.run

    def flaky_run(args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            class _R:
                returncode = 128
                stdout = "partial\n"
                stderr = ""

            return _R()
        return real_run(args, **kwargs)

    monkeypatch.setattr(publish, "_PATHSPEC_ARGV_BUDGET", 1500)
    monkeypatch.setattr(publish.subprocess, "run", flaky_run)

    rc, out = publish._git_status_porcelain(repo, _all_entries(repo))

    assert rc == 128
    assert out == ""
