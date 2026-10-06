"""close-out names a same-subject successor for a rewritten-away disposition_ref."""
from __future__ import annotations

import pytest

from coordinator_core.execute_plan_assemble import close_out_and_stamp as coas
from coordinator_core.execute_plan_assemble.tests.test_close_out_and_stamp_remedy_message import (
    _DLV_VALID_SPINE,
    _FIXTURE_VALID_SPINE,
    _commit_chunk,
    _init_repo,
    _resolve_chunk_coded,
    _run_close_out,
    _run_git,
    _seed_plan,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _build(root, *, rewrite: bool, same_subject: bool = True):
    _init_repo(root)
    _seed_plan(root, _FIXTURE_VALID_SPINE)
    _commit_chunk(root, "plan.md", "C1", deliverable_id=_DLV_VALID_SPINE)
    _resolve_chunk_coded(root, "plan.md", "C1")
    old = _run_git(["rev-parse", "HEAD~1"], root).stdout.strip()
    if rewrite:
        plan_text = (root / "plan.md").read_text(encoding="utf-8")
        _run_git(["reset", "-q", "--hard", "HEAD~2"], root)
        subject = "C1: land chunk" if same_subject else "C1: something else"
        _run_git(["commit", "-q", "--allow-empty", "-m", subject], root)
        (root / "plan.md").write_text(plan_text, encoding="utf-8")
        _run_git(["add", "plan.md"], root)
        _run_git(["commit", "-q", "-m", "resolve C1 again"], root)
    return old


def test_rewritten_ref_names_successor_and_command(tmp_path, monkeypatch):
    old = _build(tmp_path, rewrite=True)
    new = _run_git(["rev-parse", "HEAD~1"], tmp_path).stdout.strip()
    _code, result = _run_close_out(monkeypatch, tmp_path, "plan.md")
    msg = result["message"]
    assert result["disposition_ref_rejections"]["C1"] == coas.DISPOSITION_REF_NOT_ANCESTOR
    assert (
        f"plan-tasks-resolve --plan plan.md --id C1 --coded {new} "
        f'--disposition-detail "history rewrite: {old[:9]} -> {new[:9]}"'
    ) in msg


def test_no_successor_keeps_plain_refusal(tmp_path, monkeypatch):
    _build(tmp_path, rewrite=True, same_subject=False)
    _code, result = _run_close_out(monkeypatch, tmp_path, "plan.md")
    msg = result["message"]
    assert "C1 (non-ancestor)" in msg
    assert "history rewrite" not in msg


def test_successor_lookup_is_two_spawns_regardless_of_rows(tmp_path, monkeypatch):
    old = _build(tmp_path, rewrite=True)
    calls = []
    real = coas._run_git
    monkeypatch.setattr(
        coas, "_run_git", lambda args, root, *a, **k: (calls.append(args), real(args, root, *a, **k))[1]
    )
    refs = {f"R{i}": old for i in range(10)}
    out = coas._rewrite_successors(refs, tmp_path)
    assert len(out) == 10
    assert len(calls) == 2
