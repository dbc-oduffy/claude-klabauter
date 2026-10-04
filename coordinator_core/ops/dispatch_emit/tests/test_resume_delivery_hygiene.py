"""Reverify settles the test-runner sidecar; prep diffs are keyed per plan and run base; rows
coded before a resume's base are credited with one batched git pass."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import delivery_credit
from coordinator_core.ops.dispatch_emit.delivery_credit import rows_backed_before_base
from coordinator_core.ops.dispatch_emit.reverify_delivery import settle_tests_sidecar
from coordinator_core.ops.review_mint.execute_review import prep_slice_id_for


def test_settle_writes_test_verdict_and_leaves_lifecycle_status(tmp_path):
    side = tmp_path / "runner.md"
    side.write_text("---\nstatus: open\nagent_type: t\n---\nbody\n", encoding="utf-8", newline="")
    assert settle_tests_sidecar(tmp_path, {"status": "pass", "run": 124, "failed": 0, "sidecar": "runner.md"})
    text = side.read_text(encoding="utf-8")
    assert "status: open" in text and "test_verdict: pass" in text
    assert "run: 124" in text and "failed: 0" in text
    assert not settle_tests_sidecar(tmp_path, {"status": "fail", "sidecar": "runner.md"})
    assert "test_verdict: pass" in side.read_text(encoding="utf-8")


def test_settle_maps_error_and_skips_missing(tmp_path):
    side = tmp_path / "r.md"
    side.write_text("---\nstatus: open\n---\n", encoding="utf-8", newline="")
    assert settle_tests_sidecar(tmp_path, {"status": "error", "sidecar": "r.md"})
    assert "test_verdict: errored" in side.read_text(encoding="utf-8")
    assert not settle_tests_sidecar(tmp_path, {"status": "pass", "sidecar": "nope.md"})


def test_prep_slice_id_differs_per_plan_and_base():
    a = prep_slice_id_for("docs/plans/a.md", "1" * 40)
    assert a == prep_slice_id_for("docs/plans/a.md", "1" * 40)
    assert a != prep_slice_id_for("docs/plans/b.md", "1" * 40)
    assert a != prep_slice_id_for("docs/plans/a.md", "2" * 40)
    assert a.endswith("-prep") and "/" not in a


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _plan(rows: str) -> str:
    return (
        "---\nstatus: draft\n---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n"
    )


@pytest.mark.spawns_process
def test_rows_backed_before_base_uses_one_batch(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("1")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-qm", "one")
    old = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "f").write_text("2")
    _git(tmp_path, "commit", "-qam", "two")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "f").write_text("3")
    _git(tmp_path, "commit", "-qam", "three")
    newer = _git(tmp_path, "rev-parse", "HEAD")
    rows = "".join(
        f"- id: {i}\n  disposition: coded\n  disposition_ref: '{ref}'\n"
        for i, ref in (("r-old", old[:10]), ("r-new", newer), ("r-bogus", "deadbeef" * 5))
    ) + "- id: r-open\n  disposition: open\n"
    plan = tmp_path / "plan.md"
    plan.write_text(_plan(rows), encoding="utf-8")

    calls = []
    real = git_run.run_git
    monkeypatch.setattr(
        delivery_credit, "run_git", lambda args, **kw: (calls.append(args[0]), real(args, **kw))[1]
    )
    got = rows_backed_before_base(tmp_path, str(plan), base)
    assert got == ["r-old"]
    assert len(calls) <= 2
