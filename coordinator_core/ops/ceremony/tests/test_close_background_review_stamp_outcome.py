"""Every non-ran exit of close_background._review_stamp names a review skip reason or an unminted refusal."""
from __future__ import annotations

import pytest

from coordinator_core.ops import review_stamp as rs
from coordinator_core.ops.ceremony import close_background as cb

SHA = "a" * 40


def _ctx(root, plan_rel="docs/plans/p.md", sha=SHA, inputs=None):
    record = {"inputs": inputs or {}}
    if plan_rel is not None:
        record["plan_rel"] = plan_rel
    return cb.JobContext(root, root / ".git", "run-1", (), None, sha, record)


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(cb, "_not_opted_in", lambda ctx: False)


def test_no_plan_is_skipped(tmp_path):
    got = cb._review_stamp(_ctx(tmp_path, plan_rel=None))
    assert got.status == "not-applicable"
    assert got.detail.startswith("review skipped: no-plan")


def test_no_terminal_sha_is_skipped(tmp_path):
    got = cb._review_stamp(_ctx(tmp_path, sha=None))
    assert got.status == "not-applicable"
    assert got.detail.startswith("review skipped: no-terminal-commit")


def test_not_opted_in_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "_not_opted_in", lambda ctx: True)
    got = cb._review_stamp(_ctx(tmp_path))
    assert got.status == "not-applicable"
    assert got.detail.startswith("review skipped: not-opted-in")
    assert cb._NOT_OPTED_IN in got.detail


def test_spine_without_inventory_record_is_skipped(tmp_path):
    got = cb._review_stamp(_ctx(tmp_path, plan_rel="docs/plans/p.spine.md"))
    assert got.status == "not-applicable"
    assert got.detail.startswith("review skipped: no-inventory-record")


def test_plan_escaping_worktree_is_unminted(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    got = cb._review_stamp(_ctx(root, plan_rel="../outside.md"))
    assert got.status == "failed"
    assert got.detail.startswith("review ran, stamp not minted: plan path escapes the worktree")


def test_unreadable_review_record_is_unminted(tmp_path):
    got = cb._review_stamp(_ctx(tmp_path, inputs={"review_record": "docs/missing-record.md"}))
    assert got.status == "failed"
    assert got.detail.startswith(
        "review ran, stamp not minted: review record docs/missing-record.md is unreadable"
    )


def test_mint_refusal_is_unminted(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise rs.MintRefusal("no review record for the plan")

    monkeypatch.setattr(rs, "mint", refuse)
    got = cb._review_stamp(_ctx(tmp_path))
    assert got.status == "failed"
    assert got.detail.startswith("review ran, stamp not minted: no review record for the plan")
