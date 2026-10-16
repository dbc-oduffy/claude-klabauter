"""The terminal commit replaces a lossy relayed review prep with the reply review.prep stored."""

from __future__ import annotations

from types import SimpleNamespace

import yaml

from coordinator_core.completion_receipts import verdict
from coordinator_core.ops.dispatch_emit.terminal_commit import _reconcile_prep

_SESSION = "11111111-2222-3333-4444-555555555555"
_RUN = "ask-20261016T101500-abc123"
_BASE = "a" * 40
_FILES = ["pkg/a.py", "pkg/b.py"]


def _store(root, run=_RUN, base=_BASE, files=_FILES):
    share = root / ".coordinator-local" / "subagent-share" / _SESSION
    share.mkdir(parents=True, exist_ok=True)
    name = f"manifest-{run}-{base[:12]}-prep.review-prep.md"
    fm = {
        "agent_type": "engine:prep-freeze", "plan_id": "p", "share_dir": "x", "run_base_sha": base,
        "verdict": "OK", "basis": "range", "whole_diff_path": "d", "product_files": len(files),
        "foreign_claims": [], "slices": [{"id": "s", "files": list(files)}], "whole_diff_sidecars": {},
        "sidecar_path": "ignored", "head": "b" * 40, "paths": list(files),
    }
    (share / name).write_text(f"---\n{yaml.safe_dump(fm)}---\n\nbody\n", encoding="utf-8")
    return f".coordinator-local/subagent-share/{_SESSION}/{name}"


def _request():
    return SimpleNamespace(chunks=[SimpleNamespace(paths=list(_FILES))])


def _review():
    return {
        "prep": {"run_base_sha": None, "product_files": None, "slice_files": []},
        "prep_sidecar": None,
        "criterion": {"status": "not_met"},
    }


def _record_base(root):
    run = root / "scratch" / "warp" / _RUN
    run.mkdir(parents=True, exist_ok=True)
    (run / "run-base").write_text(_BASE + "\n", encoding="utf-8")


def test_lossy_relay_is_replaced_and_mint_no_longer_sees_zero_files(tmp_path):
    stored = _store(tmp_path)
    _record_base(tmp_path)
    review = _review()
    integration = {"delivery": {"verdict": "PASS"}, "criterion": {"status": "not_met"}}

    def refusal():
        return verdict.mint_refusal(
            integration, review["prep"], {"test_verdict": "pass"}, criterion_open_ok=True,
        )

    assert "zero files" in (refusal() or "")
    _reconcile_prep(tmp_path, _SESSION, _RUN, _request(), review)
    assert review["prep"]["product_files"] == 2
    assert review["prep"]["slice_files"] == _FILES
    assert review["prep_sidecar"] == stored
    assert "zero files" not in (refusal() or "")


def test_relayed_base_is_used_when_it_is_a_sha(tmp_path):
    _store(tmp_path)
    review = _review()
    review["prep"]["run_base_sha"] = _BASE
    _reconcile_prep(tmp_path, _SESSION, _RUN, _request(), review)
    assert review["prep"]["product_files"] == 2


def test_no_stored_sidecar_leaves_the_relay(tmp_path):
    _record_base(tmp_path)
    review = _review()
    _reconcile_prep(tmp_path, _SESSION, _RUN, _request(), review)
    assert review == _review()


def test_another_runs_sidecar_leaves_the_relay(tmp_path):
    _store(tmp_path, run="ask-20261016T090000-other1")
    _record_base(tmp_path)
    review = _review()
    _reconcile_prep(tmp_path, _SESSION, _RUN, _request(), review)
    assert review == _review()


def test_no_base_or_no_session_leaves_the_relay(tmp_path):
    _store(tmp_path)
    review = _review()
    _reconcile_prep(tmp_path, _SESSION, _RUN, _request(), review)
    _reconcile_prep(tmp_path, None, _RUN, _request(), review)
    assert review == _review()
