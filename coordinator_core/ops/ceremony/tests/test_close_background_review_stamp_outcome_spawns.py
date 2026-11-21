"""Every non-ran exit of close_background._review_stamp names a review skip reason or an unminted refusal."""
from __future__ import annotations

import pytest

from coordinator_core.ops import review_stamp as rs
from coordinator_core.ops.ceremony import close_background as cb
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SHA = "a" * 40


def _ctx(root, plan_rel: str | None = "docs/plans/p.md", sha=SHA, inputs=None):
    record = {"inputs": inputs or {}}
    if plan_rel is not None:
        record["plan_rel"] = plan_rel
    return cb.JobContext(root, root / ".git", "run-1", (), None, sha, record)


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cb, "_not_opted_in", lambda ctx: False)


def _xs_spine(root) -> None:
    (root / "state/warp/r1").mkdir(parents=True)
    (root / "state/warp/r1/p.md").write_text("---\nrun_id: r1\nreview_stamp: x\n---\n", encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.parametrize("ignored", [True, False])
def test_mirror_follows_a_real_gitignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignored) -> None:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    (tmp_path / ".gitignore").write_text("state/warp/\n" if ignored else "unrelated/\n", encoding="utf-8")
    _xs_spine(tmp_path)
    monkeypatch.setattr(rs, "mint_receipt", lambda *a, **k: {})
    monkeypatch.setattr(cb, "_queue_review", lambda ctx, rel: None)
    ctx = _ctx(tmp_path, plan_rel="state/warp/r1/p.spine.md")
    got = cb._review_stamp(ctx)
    assert got.status == "ran"
    want = "state/review-receipts/r1-p.md" if ignored else "state/warp/r1/p.md"
    assert want in got.detail
    assert want in ctx.written
    assert (tmp_path / "state/review-receipts/r1-p.md").exists() is ignored
