"""
coordinator_core.ops.tests.test_crossrepo_closure_status

Tests for the crossrepo.closure_status COMPUTE_ONLY op (memo-corpus x
commitment-ledger join, DR-208 § 5 read-only classification).

Coverage:
  (a) input-count == output-count invariant (AC7) — a fixture spanning zero,
      one, and multiple referencing commitments per memo asserts every memo
      enumerated on the left gets exactly one output row (join can't silently
      drop a row).
  (b) closed/open literal-vocabulary reuse — ONLY an exact "closed" status
      closes a memo; "fulfilled", "open", and an unknown value all count as
      open (mirrors delete_guard.check_commitment_closure's inherited quirk).
  (c) realizing-artifact resolution — an inline sentinel realized_by resolves;
      a nonexistent path does not; a commitment with no realized_by resolves
      to None.
  (d) unparseable/unreadable ledger entries are INCLUDED in the join (status
      None, never silently dropped from the commitment_records set).
  (e) handler smoke — end-to-end over a fixture worktree, no file written
      (pure read + compute + return).

Spec backlink: coordinator_core/ops/crossrepo_closure_status.py
Plan: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C15 / AC7
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from coordinator_core.ops.crossrepo_closure_status import (
    _handler,
    collect_commitment_entries,
    collect_memo_records,
    compute_closure_status,
)


def _run(coro):
    return asyncio.run(coro)


def _memo(worktree_root: Path, rel_dir: str, memo_id: str) -> None:
    d = worktree_root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{memo_id}.md").write_text(
        f"---\ntitle: \"{memo_id}\"\nfrom: \"sibling-em\"\nto: \"claude-klabauter-em\"\n"
        f"status: actioned\n---\n\n## {memo_id}\n\nBody.\n",
        encoding="utf-8",
    )


def _entry(commitments_dir: Path, entry_id: str, *, memo_ref: str, status: str, realized_by=None) -> None:
    commitments_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"created: 2026-07-23\n"
        f"title: fixture entry {entry_id}\n"
        f"status: {status}\n"
        f"committed_by: sibling-em\n"
        f"memo: {memo_ref}\n"
        f"commitment: fixture commitment text\n"
    )
    if realized_by is not None:
        body += f"realized_by: {realized_by}\n"
    (commitments_dir / f"{entry_id}.yaml").write_text(body, encoding="utf-8")


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def test_join_input_count_equals_output_count_no_commitments(tmp_path: Path) -> None:
    _memo(tmp_path, "cross-repo/archive", "memo-a")
    _memo(tmp_path, "cross-repo/archive", "memo-b")
    _memo(tmp_path, "cross-repo/inbox", "memo-c")

    memo_records, memo_degraded = collect_memo_records(
        [tmp_path / "cross-repo" / "inbox", tmp_path / "cross-repo" / "archive"], tmp_path
    )
    commitment_records, commit_degraded = collect_commitment_entries(
        tmp_path / "state" / "cross-repo-commitments"
    )
    assert not memo_degraded and not commit_degraded
    assert len(memo_records) == 3

    outcome = compute_closure_status(memo_records, commitment_records, repo_root=tmp_path)
    assert len(outcome["memos"]) == len(memo_records)
    assert outcome["counts"]["total_memos"] == 3
    assert all(m["closure"] == "none" for m in outcome["memos"])
    assert all(m["commitments"] == [] for m in outcome["memos"])


def test_closed_open_literal_vocabulary(tmp_path: Path) -> None:
    _memo(tmp_path, "cross-repo/archive", "memo-closed")
    _memo(tmp_path, "cross-repo/archive", "memo-fulfilled")
    _memo(tmp_path, "cross-repo/archive", "memo-open")
    _memo(tmp_path, "cross-repo/archive", "memo-mixed")

    commitments_dir = tmp_path / "state" / "cross-repo-commitments"
    _entry(commitments_dir, "c1", memo_ref="cross-repo/archive/memo-closed.md", status="closed")
    _entry(commitments_dir, "c2", memo_ref="cross-repo/archive/memo-fulfilled.md", status="fulfilled")
    _entry(commitments_dir, "c3", memo_ref="cross-repo/archive/memo-open.md", status="open")
    _entry(commitments_dir, "c4", memo_ref="cross-repo/archive/memo-mixed.md", status="closed")
    _entry(commitments_dir, "c5", memo_ref="cross-repo/archive/memo-mixed.md", status="open")

    memo_records, _ = collect_memo_records(
        [tmp_path / "cross-repo" / "inbox", tmp_path / "cross-repo" / "archive"], tmp_path
    )
    commitment_records, _ = collect_commitment_entries(commitments_dir)
    outcome = compute_closure_status(memo_records, commitment_records, repo_root=tmp_path)

    by_id = {m["memo_id"]: m for m in outcome["memos"]}
    assert by_id["memo-closed"]["closure"] == "closed"
    # "fulfilled" is NOT the literal "closed" string -> counts as open (inherited
    # check_commitment_closure quirk, deliberately not patched by this op).
    assert by_id["memo-fulfilled"]["closure"] == "open"
    assert by_id["memo-open"]["closure"] == "open"
    # One closed + one open commitment on the same memo -> overall open (any-open wins).
    assert by_id["memo-mixed"]["closure"] == "open"
    assert len(by_id["memo-mixed"]["commitments"]) == 2


def test_realizing_artifact_resolution(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _memo(tmp_path, "cross-repo/archive", "memo-inline")
    _memo(tmp_path, "cross-repo/archive", "memo-missing-path")
    _memo(tmp_path, "cross-repo/archive", "memo-no-realized")

    commitments_dir = tmp_path / "state" / "cross-repo-commitments"
    _entry(
        commitments_dir, "c1",
        memo_ref="cross-repo/archive/memo-inline.md", status="closed", realized_by="inline",
    )
    _entry(
        commitments_dir, "c2",
        memo_ref="cross-repo/archive/memo-missing-path.md", status="closed",
        realized_by="does/not/exist.md",
    )
    _entry(
        commitments_dir, "c3",
        memo_ref="cross-repo/archive/memo-no-realized.md", status="open",
    )

    memo_records, _ = collect_memo_records(
        [tmp_path / "cross-repo" / "inbox", tmp_path / "cross-repo" / "archive"], tmp_path
    )
    commitment_records, _ = collect_commitment_entries(commitments_dir)
    outcome = compute_closure_status(memo_records, commitment_records, repo_root=tmp_path)

    by_id = {m["memo_id"]: m for m in outcome["memos"]}
    assert by_id["memo-inline"]["commitments"][0]["realizing"] == "inline"
    assert by_id["memo-missing-path"]["commitments"][0]["realizing"] is None
    assert by_id["memo-no-realized"]["commitments"][0]["realizing"] is None


def test_unparseable_ledger_entry_included_not_dropped(tmp_path: Path) -> None:
    commitments_dir = tmp_path / "state" / "cross-repo-commitments"
    commitments_dir.mkdir(parents=True, exist_ok=True)
    (commitments_dir / "bad.yaml").write_text("[this is not a mapping]\n", encoding="utf-8")
    (commitments_dir / "unmapped.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")

    records, degraded = collect_commitment_entries(commitments_dir)
    assert not degraded
    assert len(records) == 2
    names = {r["entry"] for r in records}
    assert names == {"bad.yaml", "unmapped.yaml"}
    assert all(r["status"] is None for r in records)
    assert all(r["parse_error"] for r in records)


def test_handler_smoke_no_disk_write(tmp_path: Path, monkeypatch) -> None:
    _init_git_repo(tmp_path)
    _memo(tmp_path, "cross-repo/archive", "memo-1")
    commitments_dir = tmp_path / "state" / "cross-repo-commitments"
    _entry(commitments_dir, "c1", memo_ref="cross-repo/archive/memo-1.md", status="closed", realized_by="inline")

    monkeypatch.setattr(
        "coordinator_core.ops.fleet._common.main_worktree_root",
        lambda common_dir: tmp_path,
    )

    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    result = _run(_handler({}, repo_root=tmp_path / ".git"))
    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after  # pure read + compute; no artifact written

    assert result["counts"]["total_memos"] == 1
    assert result["memos"][0]["memo_id"] == "memo-1"
    assert result["memos"][0]["closure"] == "closed"
    assert result["memos"][0]["commitments"][0]["realizing"] == "inline"


def test_handler_requires_repo_root() -> None:
    try:
        _run(_handler({}, repo_root=None))
    except ValueError as exc:
        assert "repo_root is None" in str(exc)
    else:
        raise AssertionError("expected ValueError for repo_root=None")
