"""Tests for coordinator_core.ops.freshness_commit_delta -- the "freshness.commit_delta"
op (C3, docs/plans/2026-09-10-cartography-churn-producer-and-staleness-registrations.md).

Coverage:
    (a) the three-field payload shape (`doc_commit_delta`, `test_commit_delta`,
        `bug_sweep_commit_delta`), each an int, against a REAL fixture repo -- not a
        mocked `run_git`, because the op's own contract is what the single `git log
        --name-only` read actually parses (message pattern for doc/bug_sweep, changed-
        PATH pattern for test), and a mock would assert the parser against itself.
    (b) the non-degenerate fixture (staff-eng F1 review, C4's own row): a fixture repo
        carrying a REAL test-touching commit inside `_SCAN_DEPTH` must make
        `test_commit_delta` return something other than the `_VERY_STALE` (99)
        sentinel -- the exact falsification the review demanded, proving this op is not
        the "ships a producer that returns 99 forever" failure class it names.
    (c) the single-spawn budget: one `run_git` call per `commit_delta()` invocation,
        regardless of how many of the three fields fire, via a `run_git` seam count.
    (d) registration-quad parity: the same five-surface wiring
        `coordinator_core/ops/tests/test_op_registration.py` exercises for every other
        op in this tree, restated here as a direct import/dispatch smoke so this op's
        own test module does not depend on that file's parametrization surviving a
        future edit.

Spec backlink: docs/plans/2026-09-10-cartography-churn-producer-and-staleness-
registrations.md § C4
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.git.commit_delta import _SCAN_DEPTH, _VERY_STALE
from coordinator_core.ops import freshness_commit_delta as fcd
from coordinator_core.win_portability import no_console_creationflags
import coordinator_core.ipc as ipc

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")


def _commit(root: Path, relpath: str, message: str, content: str = "x") -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", "--", relpath)
    _git(root, "commit", "-q", "-m", message)


def test_commit_delta_returns_three_int_fields_all_stale_on_fresh_repo(tmp_path):
    """A brand-new repo with one unrelated commit qualifies for none of the three
    signals -- all three fields read the `_VERY_STALE` sentinel."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "seed commit, no convention markers")

    result = fcd.commit_delta(repo)

    assert set(result) == {"doc_commit_delta", "test_commit_delta", "bug_sweep_commit_delta"}
    assert all(isinstance(v, int) for v in result.values())
    assert result == {
        "doc_commit_delta": _VERY_STALE,
        "test_commit_delta": _VERY_STALE,
        "bug_sweep_commit_delta": _VERY_STALE,
    }


def test_commit_delta_positions_each_field_independently(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "src/a.py", "bug-sweep pass over src/a.py")
    _commit(repo, "coordinator_core/ops/tests/test_thing.py", "add a test")
    _commit(repo, "docs/readme.md", "update-docs: refresh readme")

    result = fcd.commit_delta(repo)

    assert result["doc_commit_delta"] == 0
    assert result["test_commit_delta"] == 1
    assert result["bug_sweep_commit_delta"] == 2


def test_test_commit_delta_is_non_degenerate_for_a_real_test_touching_commit(tmp_path):
    """The review's own falsifier (C4 row, staff-eng F1): on a fixture repo carrying a
    REAL test-touching commit inside `_SCAN_DEPTH`, `test_commit_delta` must return
    something other than `_VERY_STALE` (99). This is the assertion that would have
    failed had `test_commit_delta` stayed message-pattern-matched (no commit-message
    convention exists for "tests were last touched") -- it only passes because the op
    matches the changed-PATH list instead (`_TEST_PATH_PATTERN`, module docstring)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "unrelated seed, no test/doc/bug-sweep markers")
    _commit(
        repo,
        "coordinator_core/ops/tests/test_freshness_commit_delta_fixture.py",
        "touch a test file, ordinary message with no special convention",
    )

    result = fcd.commit_delta(repo)

    assert result["test_commit_delta"] != _VERY_STALE, (
        "test_commit_delta returned the _VERY_STALE sentinel despite a real "
        "test-touching commit inside _SCAN_DEPTH -- the exact degenerate failure "
        "class the staff-eng F1 review named (a producer that returns 99 forever "
        "and reports itself working)."
    )
    assert result["test_commit_delta"] == 0


@pytest.mark.parametrize(
    "relpath",
    [
        "tests/test_foo.py",
        "coordinator_core/ops/tests/test_bar.py",
        "some/dir/baz_test.py",
        "baz_test.js",
        "test_bare.py",
    ],
)
def test_test_path_pattern_fires_on_every_documented_naming_convention(tmp_path, relpath):
    """The three cross-ecosystem test-naming conventions `_TEST_PATH_PATTERN`'s own
    docstring commits to (module docstring for `_TEST_PATH_PATTERN`)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "seed")
    _commit(repo, relpath, "ordinary message")

    result = fcd.commit_delta(repo)
    assert result["test_commit_delta"] == 0, relpath


def test_test_path_pattern_does_not_fire_on_a_non_test_path(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "seed")
    _commit(repo, "src/testimony.py", "not a test file despite the substring")

    result = fcd.commit_delta(repo)
    assert result["test_commit_delta"] == _VERY_STALE


def test_commit_delta_issues_exactly_one_run_git_call(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "docs/readme.md", "update-docs: refresh")
    _commit(repo, "coordinator_core/ops/tests/test_thing.py", "add a test")
    _commit(repo, "src/a.py", "bug-sweep pass")

    calls = {"n": 0}
    real_run_git = fcd.run_git

    def _counting_run_git(*args, **kwargs):
        calls["n"] += 1
        return real_run_git(*args, **kwargs)

    monkeypatch.setattr(fcd, "run_git", _counting_run_git)

    result = fcd.commit_delta(repo)

    assert calls["n"] == 1, (
        f"commit_delta() issued {calls['n']} run_git call(s), expected exactly 1 -- "
        "a second spawn was added for one of the three fields, breaking the "
        "single-spawn budget every other cadence field in this tree holds to."
    )
    assert result["bug_sweep_commit_delta"] == 0
    assert result["test_commit_delta"] == 1
    assert result["doc_commit_delta"] == 2


def test_commit_delta_never_raises_on_run_git_failure(tmp_path, monkeypatch):
    failure = MagicMock()
    failure.ok = False
    failure.stdout = ""
    monkeypatch.setattr(fcd, "run_git", lambda *a, **k: failure)

    result = fcd.commit_delta(tmp_path / "does-not-exist")

    assert result == {
        "doc_commit_delta": _VERY_STALE,
        "test_commit_delta": _VERY_STALE,
        "bug_sweep_commit_delta": _VERY_STALE,
    }


def test_scan_depth_window_caps_the_read(tmp_path):
    """A qualifying commit outside `_SCAN_DEPTH` must not be found -- the op's window
    is capped the same way `_commits_since_last_batch`'s own read is."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "coordinator_core/ops/tests/test_old.py", "old test commit, outside window")
    for i in range(_SCAN_DEPTH):
        _commit(repo, f"filler-{i}.txt", f"filler commit {i}")

    result = fcd.commit_delta(repo)
    assert result["test_commit_delta"] == _VERY_STALE


def test_op_resolves_through_the_real_dispatch_path():
    """Mirrors test_op_registration.py's (a): resolve through ipc's real dispatch
    path (registry hit, else the lazy-import fallback), never raw _REGISTRY
    membership -- the property that matters is reachability from
    coordinator-invoke, not collection-order luck."""
    handler = ipc._REGISTRY.get("freshness.commit_delta") or ipc._lazy_import_and_lookup(
        "freshness.commit_delta"
    )
    assert handler is not None
    assert callable(handler)


def test_op_is_classified_compute_only():
    assert classify("freshness.commit_delta") is OpClass.COMPUTE_ONLY


def test_op_has_show_top_scope():
    assert ipc.OP_KEY_SCOPE.get("freshness.commit_delta") == "show_top"


def test_freshness_commit_delta_handler_dispatches_with_no_params(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "seed")

    result = fcd.freshness_commit_delta({}, repo_root=repo)
    assert set(result) == {"doc_commit_delta", "test_commit_delta", "bug_sweep_commit_delta"}
