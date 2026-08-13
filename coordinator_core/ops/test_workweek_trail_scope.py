"""
test_workweek_trail_scope.py — pytest unit tests for
coordinator_core.ops.workweek_trail_scope.

Port of: workweek-trail-scope.test.sh (coordinator-claude 3a561713, 2026-07-22), bash-oracle
regression net — reauthored here as direct in-process calls against the ported
Python module (`main(argv)`, cwd-relative, same fixture shape: a git repo with a
local bare "origin" remote so `origin/main..HEAD` resolves).

coordinator_core.ops.review_coverage_core.collect_segments is monkeypatched
(not the real git-backed segment builder — that module has its own
independent test suite, coordinator_core/ops/test_review_coverage_core.py)
via monkeypatching `workweek_trail_scope.collect_segments`. The fakes below
return exactly the segment shape ({sha_range, shas, files} dicts) the real
function returns, exercising the seam-detection / staff_eng-scope / shard-write
logic this module owns.

Spec backlink: docs/plans/2026-06-23-chain-end-review-coverage-gate.md § C2
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import workweek_trail_scope
from coordinator_core.ops.workweek_trail_scope import main

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Fixture helpers — mirror the bash oracle's _make_fixture / _add_commit.
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_fixture(base: Path):
    """Build origin.git (bare) + repo (working clone) under `base`; returns repo Path."""
    origin_dir = base / "origin.git"
    origin_dir.mkdir(parents=True)
    _git("init", "-q", "--bare", cwd=origin_dir)

    seed_dir = base / "seed"
    subprocess.run(
        ["git", "clone", "-q", str(origin_dir), str(seed_dir)],
        check=True, capture_output=True, text=True,
    )
    _git("config", "user.email", "test@test.local", cwd=seed_dir)
    _git("config", "user.name", "Test", cwd=seed_dir)
    _git("config", "commit.gpgsign", "false", cwd=seed_dir)
    subprocess.run(["git", "-C", str(seed_dir), "checkout", "-q", "-b", "main"],
                    capture_output=True, text=True)
    (seed_dir / ".gitkeep").write_text("")
    _git("add", "--", ".gitkeep", cwd=seed_dir)
    _git("commit", "-q", "-m", "root", cwd=seed_dir)
    _git("push", "-q", "origin", "main", cwd=seed_dir)

    repo_dir = base / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin_dir), str(repo_dir)],
        check=True, capture_output=True, text=True,
    )
    _git("config", "user.email", "test@test.local", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)
    _git("config", "commit.gpgsign", "false", cwd=repo_dir)
    subprocess.run(["git", "-C", str(repo_dir), "checkout", "-q", "main"],
                    capture_output=True, text=True)

    (repo_dir / "state" / "review-trail").mkdir(parents=True)
    return repo_dir


def _add_commit(repo: Path, *files: str) -> str:
    for f in files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(f"content of {f}\n")
        _git("add", "--", f, cwd=repo)
    _git("commit", "-q", "-m", f"touch {' '.join(files)}", cwd=repo)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _write_header(repo: Path, week_start: str) -> Path:
    header = repo / "state" / "week-changelog" / "HEADER.md"
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text(f"**Week starting:** {week_start}\n")
    return header


def _fake_collect_segments(segments):
    """Return a stand-in for review_coverage_core.collect_segments that
    ignores its args and returns the given segments list."""
    def _fake(*_args, **_kwargs):
        return segments
    return _fake


def _fake_collect_segments_raises(exc):
    def _fake(*_args, **_kwargs):
        raise exc
    return _fake


def _scope_shards(repo: Path):
    d = repo / "state" / "review-trail"
    if not d.is_dir():
        return []
    return sorted(d.glob(".weekly-reviewer-scopes-*.json"))


def _run(repo: Path, header_file: Path, monkeypatch, segments,
          session_id: str = "test-session-abcd1234"):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HEADER_FILE", str(header_file))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(workweek_trail_scope, "collect_segments", _fake_collect_segments(segments))
    return main([])


# ---------------------------------------------------------------------------
# Basic argv / preflight behavior
# ---------------------------------------------------------------------------


def test_help_flag(capsys):
    rc = main(["--help"])
    assert rc == 0
    assert "Usage: workweek-trail-scope" in capsys.readouterr().out


def test_missing_header_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HEADER_FILE", str(tmp_path / "nope.md"))
    rc = main([])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_unparseable_header(tmp_path, monkeypatch, capsys):
    header = tmp_path / "HEADER.md"
    header.write_text("no week-starting line here\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HEADER_FILE", str(header))
    rc = main([])
    assert rc == 1
    assert "cannot parse" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Happy-path / seam-detection / staff_eng-scope tests (T2/T5 parity)
# ---------------------------------------------------------------------------


def test_reviewed_commits_excluded_from_staff_eng(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path / "t2")
    sha_c1 = _add_commit(repo, "src/alpha.py")
    sha_c2 = _add_commit(repo, "src/beta.py")
    origin_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    header = _write_header(repo, "2000-01-01")
    segments = [{"sha_range": f"{origin_sha}..{sha_c2}", "shas": [sha_c1, sha_c2], "files": ["src/alpha.py", "src/beta.py"]}]

    rc = _run(repo, header, monkeypatch, segments)
    assert rc == 0

    shards = _scope_shards(repo)
    assert len(shards) == 1
    obj = json.loads(shards[0].read_text())
    assert sha_c1 not in obj["staff_eng"]
    assert sha_c2 not in obj["staff_eng"]
    assert obj["mechanical_workers"] == "full"


def test_unreviewed_commit_appears_in_staff_eng(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path / "t_unreviewed")
    sha = _add_commit(repo, "src/only.py")
    header = _write_header(repo, "2000-01-01")

    rc = _run(repo, header, monkeypatch, [])
    assert rc == 0

    obj = json.loads(_scope_shards(repo)[0].read_text())
    assert sha in obj["staff_eng"]


def test_cross_segment_seam_detection(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path / "t5")
    sha_a1 = _add_commit(repo, "src/common.py", "src/afile.py")
    sha_a2 = _add_commit(repo, "src/afile.py")
    origin_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    range_a = f"{origin_sha}..{sha_a2}"
    sha_b1 = _add_commit(repo, "src/common.py", "src/bfile.py")
    sha_b2 = _add_commit(repo, "src/bfile.py")
    range_b = f"{sha_a2}..{sha_b2}"

    header = _write_header(repo, "2000-01-01")
    segments = [
        {"sha_range": range_a, "shas": [sha_a1, sha_a2], "files": ["src/common.py", "src/afile.py"]},
        {"sha_range": range_b, "shas": [sha_b1, sha_b2], "files": ["src/common.py", "src/bfile.py"]},
    ]

    rc = _run(repo, header, monkeypatch, segments)
    assert rc == 0

    obj = json.loads(_scope_shards(repo)[0].read_text())
    assert "src/common.py" in obj["staff_eng_seam_files"]
    # Both segments' reviewed commits are pulled back into staff_eng_scope
    # because their file-sets intersect at the seam file.
    assert sha_a1 in obj["staff_eng"] and sha_a2 in obj["staff_eng"]
    assert sha_b1 in obj["staff_eng"] and sha_b2 in obj["staff_eng"]


# ---------------------------------------------------------------------------
# review_coverage_core.collect_segments failure propagation (T7 parity)
# ---------------------------------------------------------------------------


def test_coverage_core_failure_propagates_business_fail(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path / "t7")
    _add_commit(repo, "src/t7.py")
    header = _write_header(repo, "2000-01-01")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("HEADER_FILE", str(header))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session-abcd1234")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        workweek_trail_scope,
        "collect_segments",
        _fake_collect_segments_raises(workweek_trail_scope._CoverageFatalError("GARBAGE not json")),
    )
    rc = main([])
    assert rc == workweek_trail_scope.EXIT_BUSINESS_FAIL
    assert _scope_shards(repo) == []
    assert "GARBAGE not json" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Session-id resolution
# ---------------------------------------------------------------------------


def test_session_id_unresolvable(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path / "t_no_sid")
    _add_commit(repo, "src/x.py")
    header = _write_header(repo, "2000-01-01")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("HEADER_FILE", str(header))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(workweek_trail_scope, "collect_segments", _fake_collect_segments([]))
    rc = main([])
    assert rc == 1
    assert "Could not resolve session-id" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Concurrency / uniqueness — T8/T9 parity for the native timestamp reimpl.
# (addendum rule 6: the platform-gap fix — native microsecond timestamp
# instead of shelling to `date` — needs its own collision-avoidance test.)
# ---------------------------------------------------------------------------


def test_distinct_sessions_produce_distinct_shards(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path / "t8")
    _add_commit(repo, "src/t8.py")
    header = _write_header(repo, "2000-01-01")

    rc_a = _run(repo, header, monkeypatch, [], session_id="test-session-aaaaaaaa")
    rc_b = _run(repo, header, monkeypatch, [], session_id="test-session-bbbbbbbb")
    assert rc_a == 0 and rc_b == 0
    assert len(_scope_shards(repo)) == 2


def test_same_session_rapid_reinvocation_produces_distinct_shards(tmp_path, monkeypatch):
    """Two back-to-back invocations with the SAME session id must not clobber
    each other — the microsecond-precision native timestamp is the
    collision-avoidance mechanism (mirrors bash oracle T9's %N-fallback
    disambiguator, made unconditional here since Python has no BSD-date gap)."""
    repo = _make_fixture(tmp_path / "t9")
    _add_commit(repo, "src/t9.py")
    header = _write_header(repo, "2000-01-01")

    rc_a = _run(repo, header, monkeypatch, [], session_id="test-session-cccccccc")
    rc_b = _run(repo, header, monkeypatch, [], session_id="test-session-cccccccc")
    assert rc_a == 0 and rc_b == 0
    assert len(_scope_shards(repo)) == 2
