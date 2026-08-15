"""test_repair_empty_review_trail_ranges.py — tests for
repair-empty-review-trail-ranges.py (C4 of docs/plans/2026-08-10-caret-fix-
on-the-wrong-launcher.md).

Builds a real throwaway git repo (tmp_path) with a root commit, a linear
commit, and a merge commit, then seeds review-trail record files whose
`sha_range` has the identical-endpoint (caret-strip) signature against each
of those three commit shapes, plus one record pointing at a SHA absent from
the repo entirely.

Coverage (AC7/AC8):
  - a linear commit's identical-endpoint record is repaired to
    first-parent..sha, byte-preserving every other field/key/order.
  - a root commit's record is reported (SKIP_ROOT_COMMIT), left untouched.
  - a merge commit's record is repaired to first-parent..sha AND the report
    line names the merge-commit choice explicitly (not silent).
  - an unresolvable SHA's record is reported (SKIP_UNRESOLVED_SHA), left
    untouched — never guessed at.
  - dry-run (the default — no --apply) writes nothing to any record file.

Spec backlink: pln-the-caret-fix-went-to-the-laun-aff9e5 § C4
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


# ---------------------------------------------------------------------------
# Path setup — locate the CLI relative to this test file
# test file: coordinator/bin/tests/test_repair_empty_review_trail_ranges.py
# CLI:       coordinator/bin/repair-empty-review-trail-ranges.py
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "repair-empty-review-trail-ranges.py"


def _load_module(path: Path, module_name: str):
    """Load the hyphenated CLI as a Python module for direct-function testing."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_repair = _load_module(_CLI_PATH, "repair_empty_review_trail_ranges")


# ---------------------------------------------------------------------------
# Git repo fixture — root commit, linear commit, merge commit.
# ---------------------------------------------------------------------------

def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _commit(repo: Path, rel_path: str, content: str, message: str) -> str:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-q", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.example"], root)
    _git(["config", "user.name", "t"], root)
    return root


@pytest.fixture()
def shaped_repo(repo: Path) -> dict:
    """A repo with a root commit, a linear child commit, and a merge commit
    (two divergent branches joined back into main), returning the SHAs the
    tests key off of.
    """
    root_sha = _commit(repo, "a.txt", "a\n", "root commit")
    linear_sha = _commit(repo, "b.txt", "b\n", "linear commit")

    _git(["checkout", "-q", "-b", "side"], repo)
    side_sha = _commit(repo, "c.txt", "c\n", "side-branch commit")
    _git(["checkout", "-q", "-"], repo)  # back to main-line branch
    _git(["checkout", "-q", "main"], repo) if _has_branch(repo, "main") else None

    merge_msg = "merge side into main"
    _git(["merge", "--no-ff", "-q", "-m", merge_msg, "side"], repo)
    merge_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    return {
        "root_sha": root_sha,
        "linear_sha": linear_sha,
        "side_sha": side_sha,
        "merge_sha": merge_sha,
    }


def _has_branch(repo: Path, name: str) -> bool:
    result = _git(["branch", "--list", name], repo)
    return bool(result.stdout.strip())


# ---------------------------------------------------------------------------
# Review-trail record seeding — mirrors _build_json_record's hand-built,
# compact, fixed-key-order serialization (coordinator_core/ops/
# review_trail_write.py) so byte-preservation assertions are meaningful.
# ---------------------------------------------------------------------------

def _seed_record(trail_dir: Path, filename: str, sha_range: str) -> Path:
    trail_dir.mkdir(parents=True, exist_ok=True)
    record = (
        f'{{"sha_range":"{sha_range}",'
        f'"reviewer":"code-reviewer",'
        f'"scope":"chain",'
        f'"scope_kind":"diff",'
        f'"verdict":"pass",'
        f'"diff_loc":12,'
        f'"session_id":"deadbeef-0000-0000-0000-000000000000",'
        f'"workstream":null,'
        f'"reviewed_paths":null}}'
    )
    path = trail_dir / filename
    path.write_text(record, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _split_identical_endpoint_range — unit coverage of the pure detector.
# ---------------------------------------------------------------------------

def test_identical_endpoints_detected() -> None:
    assert _repair._split_identical_endpoint_range("abc123..abc123") == "abc123"


def test_distinct_endpoints_not_flagged() -> None:
    assert _repair._split_identical_endpoint_range("abc123..def456") is None


def test_three_dot_range_not_misread() -> None:
    # "..." takes precedence over ".." per _resolve_symbolic_range parity —
    # a legitimate three-dot range must never be misread as a two-dot split.
    assert _repair._split_identical_endpoint_range("abc123...abc123") == "abc123"


def test_no_separator_not_flagged() -> None:
    assert _repair._split_identical_endpoint_range("abc123") is None


# ---------------------------------------------------------------------------
# Linear commit — repaired to first-parent..sha, byte-preserving other fields.
# ---------------------------------------------------------------------------

def test_linear_commit_repaired_and_byte_preserved(shaped_repo, repo) -> None:
    linear_sha = shaped_repo["linear_sha"]
    root_sha = shaped_repo["root_sha"]
    trail_dir = repo / "state" / "review-trail"
    original_range = f"{linear_sha}..{linear_sha}"
    path = _seed_record(trail_dir, "linear.json", original_range)
    original_text = path.read_text(encoding="utf-8")

    rc = _repair.main(["--repo-root", str(repo), "--apply"])
    assert rc == 0

    new_text = path.read_text(encoding="utf-8")
    expected_range = f"{root_sha}..{linear_sha}"
    assert new_text == original_text.replace(
        f'"sha_range":"{original_range}"', f'"sha_range":"{expected_range}"',
    )
    record = json.loads(new_text)
    assert record["sha_range"] == expected_range
    # Every other field byte-identical to the seeded record.
    assert record["reviewer"] == "code-reviewer"
    assert record["diff_loc"] == 12
    assert record["workstream"] is None


# ---------------------------------------------------------------------------
# Root commit — reported, left untouched (not repairable).
# ---------------------------------------------------------------------------

def test_root_commit_reported_and_untouched(shaped_repo, repo, capsys) -> None:
    root_sha = shaped_repo["root_sha"]
    trail_dir = repo / "state" / "review-trail"
    original_range = f"{root_sha}..{root_sha}"
    path = _seed_record(trail_dir, "root.json", original_range)
    original_text = path.read_text(encoding="utf-8")

    rc = _repair.main(["--repo-root", str(repo), "--apply"])
    assert rc == 0

    assert path.read_text(encoding="utf-8") == original_text
    out = capsys.readouterr().out
    assert "SKIP_ROOT_COMMIT" in out
    assert "root.json" in out


# ---------------------------------------------------------------------------
# Merge commit — repaired to first-parent..sha, report states the choice.
# ---------------------------------------------------------------------------

def test_merge_commit_repaired_with_explicit_report(shaped_repo, repo, capsys) -> None:
    merge_sha = shaped_repo["merge_sha"]
    linear_sha = shaped_repo["linear_sha"]
    trail_dir = repo / "state" / "review-trail"
    original_range = f"{merge_sha}..{merge_sha}"
    path = _seed_record(trail_dir, "merge.json", original_range)

    rc = _repair.main(["--repo-root", str(repo), "--apply"])
    assert rc == 0

    record = json.loads(path.read_text(encoding="utf-8"))
    # The merge commit's first parent is the branch-side (main-line) parent
    # it was merged into — here, the linear commit.
    assert record["sha_range"] == f"{linear_sha}..{merge_sha}"

    out = capsys.readouterr().out
    assert "REPAIRED_MERGE" in out
    assert "MERGE COMMIT" in out
    assert "merge.json" in out


# ---------------------------------------------------------------------------
# Three-dot identical-endpoint record — repaired range keeps the three-dot
# separator, never silently narrowed to a two-dot ancestry range.
# ---------------------------------------------------------------------------

def test_three_dot_record_repaired_preserves_separator(shaped_repo, repo) -> None:
    linear_sha = shaped_repo["linear_sha"]
    root_sha = shaped_repo["root_sha"]
    trail_dir = repo / "state" / "review-trail"
    original_range = f"{linear_sha}...{linear_sha}"
    path = _seed_record(trail_dir, "three-dot.json", original_range)

    rc = _repair.main(["--repo-root", str(repo), "--apply"])
    assert rc == 0

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["sha_range"] == f"{root_sha}...{linear_sha}"


# ---------------------------------------------------------------------------
# Unresolvable SHA — reported, left untouched, never guessed at.
# ---------------------------------------------------------------------------

def test_unresolvable_sha_reported_and_untouched(repo, capsys) -> None:
    _commit(repo, "seed.txt", "seed\n", "seed commit so the repo is non-empty")
    trail_dir = repo / "state" / "review-trail"
    bogus_sha = "0" * 40
    original_range = f"{bogus_sha}..{bogus_sha}"
    path = _seed_record(trail_dir, "unresolved.json", original_range)
    original_text = path.read_text(encoding="utf-8")

    rc = _repair.main(["--repo-root", str(repo), "--apply"])
    assert rc == 0

    assert path.read_text(encoding="utf-8") == original_text
    out = capsys.readouterr().out
    assert "SKIP_UNRESOLVED_SHA" in out
    assert "unresolved.json" in out


# ---------------------------------------------------------------------------
# Dry-run (default) — writes nothing to any record file.
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(shaped_repo, repo, capsys) -> None:
    linear_sha = shaped_repo["linear_sha"]
    merge_sha = shaped_repo["merge_sha"]
    root_sha = shaped_repo["root_sha"]
    trail_dir = repo / "state" / "review-trail"

    paths_and_texts = []
    for name, sha in (
        ("linear.json", linear_sha),
        ("merge.json", merge_sha),
        ("root.json", root_sha),
    ):
        p = _seed_record(trail_dir, name, f"{sha}..{sha}")
        paths_and_texts.append((p, p.read_text(encoding="utf-8")))

    # No --apply: dry-run is the default.
    rc = _repair.main(["--repo-root", str(repo)])
    assert rc == 0

    for p, original_text in paths_and_texts:
        assert p.read_text(encoding="utf-8") == original_text, (
            f"{p} was written to during a dry run"
        )

    out = capsys.readouterr().out
    assert "WOULD_REPAIRED" in out
    assert "WOULD_REPAIRED_MERGE" in out
    assert "SKIP_ROOT_COMMIT" in out
    assert "--apply" in out
