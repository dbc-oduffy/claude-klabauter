"""test_publish_scoped_engine_stamp_sha — direct unit coverage for
`_scoped_engine_stamp_sha`, the fix for the warm-engine churn defect
measured 2026-08-21: the engine build stamp previously carried the round's
raw, unscoped pinned HEAD (`_round_pin_source_sha`'s return), which moves on
every commit to the shared branch — docs, `state/` artifacts, any of
50-70 concurrent sessions' unrelated work, not only engine code. Since
`coordinator_core.warm.skew.compute_client_token` hashes the stamp bytes and
the token is embedded in the warm server's pipe name, every publish round
rotated the generation regardless of content: 55%/33% of warm generations
exited `skew`/`superseded` at medians of ~7min/~2.5min against a 15min idle
deadline, tracking the ~9min publish cadence
(docs/decisions/DR-335-publish-lag-is-surfaced-not-shortened.md) almost
exactly.

`_scoped_engine_stamp_sha` scopes the stamp's value to the last commit at or
before the round's pin that touches `_ENGINE_TOUCHING_PATHS`
(`coordinator_core/`, `coordinator/`) — the same pair `skew.py`'s own
`publish_lag()` already scopes its unpublished-commit check to
(`test_publish_engine_stamp.py::test_publisher_and_skew_agree_on_engine_touching_paths`
pins the two definitions equal). These tests pin the soundness property that
matters most: a round whose pin advances past ONLY non-engine commits must
write the IDENTICAL stamp as the round before it, and a round whose pin
advances past a real engine-touching commit must NOT.

Run: python -m pytest coordinator/bin/tests/test_publish_scoped_engine_stamp_sha.py -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

# Spawns real git subprocesses; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_scoped_engine_stamp_sha_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_all(path: Path, message: str) -> str:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def test_scoped_sha_equals_pin_when_pin_itself_touches_engine_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "coordinator_core").mkdir()
    (repo / "coordinator_core" / "f.py").write_text("v1", encoding="utf-8")
    head = _commit_all(repo, "engine change")

    out = io.StringIO()
    scoped = publish._scoped_engine_stamp_sha(repo / "coordinator_core", head, out=out)
    assert scoped == head
    # No "scoped to" announcement when the scoped value equals the pin —
    # nothing to distinguish it from the unscoped behaviour in this case.
    assert "scoped to" not in out.getvalue()


def test_scoped_sha_ignores_trailing_non_engine_commits(tmp_path):
    """THE CORE REGRESSION PIN: a round pinned past commits that touch
    neither `coordinator_core/` nor `coordinator/` must write the SAME
    stamp as the round before it — this is what stops the token from
    rotating on a docs-only / state-only publish."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "coordinator_core").mkdir()
    (repo / "coordinator_core" / "f.py").write_text("v1", encoding="utf-8")
    engine_head = _commit_all(repo, "engine change")

    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("unrelated", encoding="utf-8")
    _commit_all(repo, "docs-only commit 1")
    (repo / "docs" / "notes.md").write_text("unrelated again", encoding="utf-8")
    later_pin = _commit_all(repo, "docs-only commit 2")

    assert later_pin != engine_head  # sanity: the pin genuinely advanced

    scoped = publish._scoped_engine_stamp_sha(repo / "coordinator_core", later_pin, out=io.StringIO())
    assert scoped == engine_head, (
        "a round pinned past two docs-only commits must resolve to the SAME "
        "engine-touching ancestor as the round before it, or the stamp still "
        "rotates on content-free publishes"
    )


def test_scoped_sha_rotates_on_a_genuine_engine_change(tmp_path):
    """The soundness half: once a NEW commit touches `coordinator_core/`,
    the scoped sha must move to it — scoping must never suppress a real
    rotation."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "coordinator_core").mkdir()
    (repo / "coordinator_core" / "f.py").write_text("v1", encoding="utf-8")
    first_engine_head = _commit_all(repo, "engine change 1")

    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("unrelated", encoding="utf-8")
    _commit_all(repo, "docs-only commit")

    (repo / "coordinator_core" / "f.py").write_text("v2", encoding="utf-8")
    second_pin = _commit_all(repo, "engine change 2")

    scoped = publish._scoped_engine_stamp_sha(repo / "coordinator_core", second_pin, out=io.StringIO())
    assert scoped == second_pin
    assert scoped != first_engine_head


def test_scoped_sha_covers_the_coordinator_bin_prefix_too(tmp_path):
    """`_ENGINE_TOUCHING_PATHS` names BOTH `coordinator_core/` and
    `coordinator/` — a commit touching only the latter must still count as
    engine-touching, matching `publish_lag()`'s own scope."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "coordinator_core").mkdir()
    (repo / "coordinator_core" / "f.py").write_text("v1", encoding="utf-8")
    _commit_all(repo, "engine change")

    (repo / "coordinator").mkdir()
    (repo / "coordinator" / "bin.py").write_text("v1", encoding="utf-8")
    bin_head = _commit_all(repo, "coordinator/ change")

    scoped = publish._scoped_engine_stamp_sha(repo / "coordinator_core", bin_head, out=io.StringIO())
    assert scoped == bin_head


def test_scoped_sha_falls_back_to_the_pin_outside_a_work_tree(tmp_path):
    """Never a silent no-stamp: a root that is not a git work tree at all
    (e.g. a materialization/ordering bug upstream) must fall back to the
    unscoped pin rather than raise or write nothing."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    scoped = publish._scoped_engine_stamp_sha(not_a_repo, "deadbeef" * 5, out=io.StringIO())
    assert scoped == "deadbeef" * 5


def test_scoped_sha_falls_back_to_the_pin_when_no_ancestor_touches_engine_paths(tmp_path):
    """Defensive case that should not arise for the real `coordinator_core`
    row (the row IS that directory) but must degrade safely regardless:
    no commit in the pin's ancestry touches `_ENGINE_TOUCHING_PATHS` ->
    fall back to the unscoped pin, never an empty/None stamp value."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "unrelated.txt").write_text("v1", encoding="utf-8")
    head = _commit_all(repo, "no engine paths at all")

    scoped = publish._scoped_engine_stamp_sha(repo, head, out=io.StringIO())
    assert scoped == head
