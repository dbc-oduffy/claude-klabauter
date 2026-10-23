"""halted_run_landed hands the close job the run's review record, or settles review-stamp as no-review-outcome."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit import halted_run_landed as op

RID = "A"
UUID = "123e4567-e89b-12d3-a456-426614174000"
OTHER = "223e4567-e89b-12d3-a456-426614174000"
SHA = "a" * 40
MARKER = {"status": "halted", "plan": "docs/plans/p.md", "blocking": [], "run_base_sha": "c" * 40}
SKIPPED = {"status": "not-applicable", "detail": "review skipped: no-review-outcome"}


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


def _outcome(root: Path, **inline_review) -> None:
    ask_contract.write_run_outcome(root, RID, {"inline_review": inline_review})


def _sidecar(root: Path, session: str, stem: str) -> str:
    path = root / ".coordinator-local" / "subagent-share" / session / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("review", encoding="utf-8")
    return path.relative_to(root).as_posix()


def _close(root: Path, session_id=UUID) -> dict:
    out = op._queue_close(root, RID, SHA, MARKER, session_id)
    assert out["close"]["launched"] is True, out
    return json.loads((root / ".coordinator-local" / "close-jobs" / f"{RID}.json").read_text(encoding="utf-8"))


def _assert_unresolved(root: Path, job: dict) -> None:
    assert "review-stamp" not in job["legs"]
    assert job["settled"]["review-stamp"] == SKIPPED
    assert "review_record" not in job["inputs"]


def test_stem_under_calling_session_resolves_and_queues_review_stamp(tmp_path):
    rel = _sidecar(tmp_path, UUID, "wave-review")
    _outcome(tmp_path, integration_stem="wave-review")
    assert op._review_record(tmp_path, RID, UUID) == rel
    job = _close(tmp_path)
    assert job["inputs"]["review_record"] == rel
    assert set(job["legs"]) == set(op.CLOSE_LEGS)
    assert job["settled"] == {}


def test_integration_sidecar_alone_resolves_without_a_session(tmp_path):
    rel = _sidecar(tmp_path, OTHER, "wave-review")
    _outcome(tmp_path, integration={"sidecar": rel})
    assert op._review_record(tmp_path, RID, None) == rel
    job = _close(tmp_path, session_id=None)
    assert job["inputs"]["review_record"] == rel and "review-stamp" in job["legs"]


def test_no_run_outcome_settles_no_review_outcome(tmp_path):
    job = _close(tmp_path)
    _assert_unresolved(tmp_path, job)
    assert set(job["legs"]) == set(op.CLOSE_LEGS) - {"review-stamp"}


def test_stem_under_a_different_session_dir_is_unresolved(tmp_path):
    _sidecar(tmp_path, OTHER, "wave-review")
    _outcome(tmp_path, integration_stem="wave-review")
    assert op._review_record(tmp_path, RID, UUID) is None
    _assert_unresolved(tmp_path, _close(tmp_path))


@pytest.mark.parametrize("stem", ["sub/wave-review", "sub\\wave-review", "..", ""])
def test_non_bare_stem_is_unresolved(tmp_path, stem):
    _sidecar(tmp_path, UUID, "wave-review")
    (tmp_path / ".coordinator-local" / "subagent-share" / UUID / "sub").mkdir(exist_ok=True)
    (tmp_path / ".coordinator-local" / "subagent-share" / UUID / "sub" / "wave-review.md").write_text("x", encoding="utf-8")
    _outcome(tmp_path, integration_stem=stem)
    assert op._review_record(tmp_path, RID, UUID) is None
    _assert_unresolved(tmp_path, _close(tmp_path))


def test_escaping_or_absolute_integration_sidecar_is_ignored(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("x", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    try:
        for bad in (f"../{outside.name}", outside.as_posix(), "a/../../" + outside.name, "a\\b.md"):
            _outcome(root, integration={"sidecar": bad})
            assert op._review_record(root, RID, UUID) is None, bad
    finally:
        outside.unlink()
    _assert_unresolved(root, _close(root))
