"""Tests for coordinator_core.ops.check_import_budget_staleness.

Cadence-tier only (see AC6) — this module's cost (a `git log` shell-out per
entrypoint, plus a throwaway-git-repo fixture for the STALE arm) does not
belong on the commit hot path.

Both predicate arms are exercised (AC5):

    - FRESH: the real repo checkout, real manifest, `measured_at` == today
      (AC4 — a just-measured manifest must never cry stale).
    - STALE: a fixture manifest with an old `measured_at` plus a commit that
      actually touches one of the entrypoint's `measured_paths`, built in a
      throwaway git repo under `tmp_path` (not simulated away — the git leg
      is the one most likely to break silently).

AC2 is discharged by `test_main_exits_nonzero_on_stale_manifest`: a stale
verdict must make `main()`'s exit code nonzero, a genuinely failing signal,
not merely a printed line.

Spec backlink: docs/plans/2026-08-13-import-budget-manifest-staleness-tell.md § C3
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops import check_import_budget_staleness as cibs
from coordinator_core.win_portability import no_console_creationflags

# Real-git spawn is load-bearing: STALE arm builds a throwaway repo and drives
# `git log`/`git commit` to prove the staleness predicate reads actual commit
# history, not a mock. Per-test isolation via tmp_path fixtures, not hoisted.
# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    return repo


def _commit_all(repo: Path, message: str, *, commit_date: str | None = None) -> None:
    _run_git(repo, "add", "-A")
    if commit_date is None:
        _run_git(repo, "commit", "-q", "-m", message)
        return
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_DATE": commit_date,
            "GIT_COMMITTER_DATE": commit_date,
        },
        **no_console_creationflags(),
    )


# ---------------------------------------------------------------------------
# compute_entrypoint_staleness — pure(ish) predicate, both arms
# ---------------------------------------------------------------------------


def test_stale_requires_both_age_and_graph_movement(tmp_path):
    """STALE arm, constructed: old measured_at AND a commit touching a
    measured_path, in a real (throwaway) git repo."""
    repo = _init_repo(tmp_path)
    watched = repo / "watched.py"
    watched.write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "initial")

    old_date = "2026-08-01"

    watched.write_text("x = 2\n", encoding="utf-8")
    _commit_all(repo, "touch watched path")

    entry = {"measured_at": old_date, "measured_paths": ["watched.py"]}
    result = cibs.compute_entrypoint_staleness(
        repo, "some.entrypoint", entry, today=date(2026, 8, 13)
    )
    assert result["verdict"] == "STALE"
    assert result["age_stale"] is True
    assert result["graph_moved"] is True


def test_fresh_when_recently_measured_even_if_graph_moved(tmp_path):
    """Age leg absent: a just-measured manifest is FRESH even if the graph
    happened to move (AND semantics — recency excuses a quiet-recent edit)."""
    repo = _init_repo(tmp_path)
    watched = repo / "watched.py"
    watched.write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "initial")

    today = date(2026, 8, 13)
    recent_date = today.isoformat()

    watched.write_text("x = 2\n", encoding="utf-8")
    _commit_all(repo, "touch watched path")

    entry = {"measured_at": recent_date, "measured_paths": ["watched.py"]}
    result = cibs.compute_entrypoint_staleness(repo, "some.entrypoint", entry, today=today)
    assert result["verdict"] == "FRESH"
    assert result["graph_moved"] is True
    assert result["age_stale"] is False


def test_fresh_when_old_but_graph_unmoved(tmp_path):
    """Graph-movement leg absent: an old measured_at with no touch to the
    measured paths stays FRESH — a quiet repo must not nag on the calendar."""
    repo = _init_repo(tmp_path)
    watched = repo / "watched.py"
    watched.write_text("x = 1\n", encoding="utf-8")
    other = repo / "other.py"
    other.write_text("y = 1\n", encoding="utf-8")
    _commit_all(repo, "initial", commit_date="2026-07-01T00:00:00")

    old_date = "2026-08-01"

    other.write_text("y = 2\n", encoding="utf-8")
    _commit_all(repo, "touch unrelated path")

    entry = {"measured_at": old_date, "measured_paths": ["watched.py"]}
    result = cibs.compute_entrypoint_staleness(
        repo, "some.entrypoint", entry, today=date(2026, 8, 13)
    )
    assert result["verdict"] == "FRESH"
    assert result["age_stale"] is True
    assert result["graph_moved"] is False


def test_unknown_on_missing_measured_paths(tmp_path):
    repo = _init_repo(tmp_path)
    entry = {"measured_at": "2026-08-01"}
    result = cibs.compute_entrypoint_staleness(repo, "ep", entry, today=date(2026, 8, 13))
    assert result["verdict"] == "UNKNOWN"


def test_unknown_on_missing_measured_at(tmp_path):
    repo = _init_repo(tmp_path)
    entry = {"measured_paths": ["watched.py"]}
    result = cibs.compute_entrypoint_staleness(repo, "ep", entry, today=date(2026, 8, 13))
    assert result["verdict"] == "UNKNOWN"


def test_unknown_not_fresh_when_git_log_query_fails(tmp_path, monkeypatch):
    """A failed git query (missing binary, non-zero exit, shallow-clone
    history gap) must surface as UNKNOWN, never fold into FRESH — the
    checker cannot confirm 'no movement' if it can't ask git at all.

    Mocked leg, kept alongside the real-failure leg below (AC5): proves
    compute_entrypoint_staleness maps a None return to UNKNOWN regardless
    of why _git_log_since returned it."""
    repo = _init_repo(tmp_path)

    monkeypatch.setattr(cibs, "_git_log_since", lambda repo_root, since_date, paths: None)

    entry = {"measured_at": "2026-08-01", "measured_paths": ["watched.py"]}
    result = cibs.compute_entrypoint_staleness(repo, "ep", entry, today=date(2026, 8, 13))
    assert result["verdict"] == "UNKNOWN"
    assert result["verdict"] != "FRESH"


def test_unknown_not_fresh_when_git_log_fails_for_real(tmp_path):
    """Real end-to-end git failure (Review: coordinator:code-reviewer — the
    mocked test above only proves the None->UNKNOWN mapping, never exercises
    _git_log_since's own except-OSError/non-zero-exit branches). Running
    against a directory that is not a git repository at all makes `git log`
    itself fail with a non-zero exit, which must surface as UNKNOWN."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    entry = {"measured_at": "2026-08-01", "measured_paths": ["watched.py"]}
    result = cibs.compute_entrypoint_staleness(not_a_repo, "ep", entry, today=date(2026, 8, 13))
    assert result["verdict"] == "UNKNOWN"
    assert result["verdict"] != "FRESH"


def test_unknown_not_fresh_when_measured_paths_is_a_string(tmp_path):
    """Review: coordinator:code-reviewer (F1) — a truthiness-only check on
    measured_paths passes a bare string (a plausible hand-edit slip in
    place of a one-element list). git log then unpacks the string into one
    pathspec per character, which can silently report FRESH regardless of
    actual staleness. Reproduced against the live checker before the fix
    (a string with no path-matching characters, e.g. 'zzzz', returned
    FRESH); must now report UNKNOWN instead."""
    repo = _init_repo(tmp_path)
    watched = repo / "watched.py"
    watched.write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "initial")

    entry = {"measured_at": "2026-08-01", "measured_paths": "watched.py"}
    result = cibs.compute_entrypoint_staleness(repo, "ep", entry, today=date(2026, 8, 13))
    assert result["verdict"] == "UNKNOWN"
    assert result["verdict"] != "FRESH"


# ---------------------------------------------------------------------------
# AC4 — the real manifest, as it stands today, is FRESH
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_real_manifest_is_fresh_today():
    """AC4: running the check against the manifest AS IT STANDS TODAY
    (re-measured 2026-08-13) must return FRESH for every entrypoint. A tell
    that cries stale on a just-measured manifest gets muted and is worse
    than none."""
    repo_root = _repo_root()
    manifest_path = repo_root / cibs.MANIFEST_RELATIVE_PATH
    with manifest_path.open("r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    results = cibs.compute_manifest_staleness(repo_root, manifest, today=date(2026, 8, 13))
    assert results, "manifest must declare at least one entrypoint"
    for name, result in results.items():
        assert result["verdict"] == "FRESH", (name, result)


# ---------------------------------------------------------------------------
# AC2 — the verdict is a FAILING signal, not only a printed line
# ---------------------------------------------------------------------------


def test_main_exits_nonzero_on_stale_manifest(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    watched = repo / "watched.py"
    watched.write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo, "initial")

    old_date = "2026-08-01"
    watched.write_text("x = 2\n", encoding="utf-8")
    _commit_all(repo, "touch watched path")

    manifest = {
        "entrypoints": {
            "fixture.entrypoint": {
                "measured_at": old_date,
                "measured_paths": ["watched.py"],
            }
        }
    }
    manifest_dir = repo / "coordinator_core" / "benchmarks"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "import-budget-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _commit_all(repo, "add fixture manifest")

    monkeypatch.setattr(cibs, "_git_root", lambda cwd=None: str(repo))

    original_compute = cibs.compute_manifest_staleness

    def _pinned_compute(repo_root, manifest, *, today=None):
        return original_compute(repo_root, manifest, today=date(2026, 8, 13))

    monkeypatch.setattr(cibs, "compute_manifest_staleness", _pinned_compute)

    exit_code = cibs.main([])
    out = capsys.readouterr().out
    assert "STALE" in out
    assert exit_code != 0, "a stale verdict must fail the check, not merely print"
