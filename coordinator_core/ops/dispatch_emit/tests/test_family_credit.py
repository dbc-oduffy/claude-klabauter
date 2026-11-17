"""A family-stage row's declared writes are credited at checkpoint from its all-OK return, only where the
path changed since the row started.

A family stage (agy, codex, grok) writes through its vendor CLI inside the runner's Bash call, so the
Write/Edit print hook never prints those writes and `checkpoint_own_blobs` withheld every one
(run ask-20261114T133823-7a3cdc: evidence.md withheld, nothing landed).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.dispatch_emit import family_credit, row_returns
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

pytestmark = pytest.mark.complete

RUN = "ask-1"
EVIDENCE = "state/run-evidence/ask-1/evidence.md"
OTHER = "pkg/mod.py"
RUNNER = "coordinator:family-stage-runner"


def _ok_return(row: str, verdicts: tuple[str, ...] = ("OK",)) -> str:
    rec = {"stage_role": "write", "attempts": [{"family": "agy", "verdict": v} for v in verdicts], "reported_family": "agy"}
    return f"DONE: {row} via agy {json.dumps(rec)}"


def _run(root: Path, writes: list[str]) -> Path:
    rec = record_dir(root, RUN)
    rec.mkdir(parents=True, exist_ok=True)
    rows = [{"id": "LR1", "agent_type": RUNNER, "writes": writes}, {"id": "X1", "agent_type": "coordinator:executor", "writes": [OTHER]}]
    (rec / "manifest.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return rec


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def test_a_path_the_stage_wrote_is_credited(tmp_path: Path) -> None:
    rec = _run(tmp_path, [EVIDENCE, OTHER])
    _write(tmp_path, OTHER, "v1\n")
    family_credit.capture(tmp_path, RUN, "LR1")
    _write(tmp_path, EVIDENCE, "agy wrote this\n")
    row_returns.write(rec, {"LR1": _ok_return("LR1")})
    credited, _ = family_credit.credit(tmp_path, RUN, [EVIDENCE], "")
    assert credited == {EVIDENCE}


def test_a_foreign_edit_to_a_declared_path_the_stage_did_not_touch_is_withheld_and_named(tmp_path: Path) -> None:
    rec = _run(tmp_path, [EVIDENCE, OTHER])
    _write(tmp_path, OTHER, "a peer's edit, before the row started\n")
    family_credit.capture(tmp_path, RUN, "LR1")
    _write(tmp_path, EVIDENCE, "agy wrote this\n")
    row_returns.write(rec, {"LR1": _ok_return("LR1")})
    credited, why = family_credit.credit(tmp_path, RUN, [EVIDENCE, OTHER], "")
    assert credited == {EVIDENCE}
    assert "LR1" in why[OTHER] and "unchanged since the row started" in why[OTHER]


def test_a_fresh_fire_credits_from_the_checkpoint_family_ok_trailer(tmp_path: Path) -> None:
    _run(tmp_path, [EVIDENCE])
    family_credit.capture(tmp_path, RUN, "LR1")
    _write(tmp_path, EVIDENCE, "agy wrote this\n")
    message = f"checkpoint(wave 1): 1 rows — LR1\n\nCheckpoint-Plan: p.md\n{family_credit.FAMILY_OK_TRAILER}: LR1"
    credited, _ = family_credit.credit(tmp_path, RUN, [EVIDENCE], message)
    assert credited == {EVIDENCE}


@pytest.mark.parametrize("text", [
    _ok_return("LR1", ("FAILED", "OK")),
    _ok_return("LR1", ()),
    "BLOCKED: LR1 via agy FAILED",
    "DONE: LR1 via claude fallback after agy FAILED {}",
])
def test_no_all_ok_return_credits_nothing(tmp_path: Path, text: str) -> None:
    rec = _run(tmp_path, [EVIDENCE])
    family_credit.capture(tmp_path, RUN, "LR1")
    _write(tmp_path, EVIDENCE, "written\n")
    row_returns.write(rec, {"LR1": text})
    credited, _ = family_credit.credit(tmp_path, RUN, [EVIDENCE], "")
    assert credited == frozenset()


def test_no_row_start_snapshot_credits_nothing_and_says_so(tmp_path: Path) -> None:
    rec = _run(tmp_path, [EVIDENCE])
    _write(tmp_path, EVIDENCE, "written\n")
    row_returns.write(rec, {"LR1": _ok_return("LR1")})
    credited, why = family_credit.credit(tmp_path, RUN, [EVIDENCE], "")
    assert credited == frozenset()
    assert "no start snapshot" in why[EVIDENCE]


def test_a_non_family_row_is_never_credited(tmp_path: Path) -> None:
    rec = _run(tmp_path, [EVIDENCE])
    family_credit.capture(tmp_path, RUN, "X1")
    _write(tmp_path, OTHER, "written\n")
    row_returns.write(rec, {"X1": _ok_return("X1")})
    credited, _ = family_credit.credit(tmp_path, RUN, [OTHER], "")
    assert credited == frozenset()


def test_checkpoint_own_blobs_names_the_unchanged_declared_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkpoint carries the family credit's reason, not the generic no-print one."""
    from coordinator_core.ops.dispatch_emit import own_hunk_stage, run_credit, run_write_prints

    class _Diff:
        hunks = ["h"]
        head_bytes = b""

    monkeypatch.setattr(own_hunk_stage, "read_diff", lambda root, paths: {p: _Diff() for p in paths})
    monkeypatch.setattr(run_write_prints, "read", lambda root, run_id: {})
    monkeypatch.setattr(run_credit, "credited_writes", lambda root, run_id: frozenset())
    monkeypatch.setattr(family_credit, "credit", lambda root, run_id, paths, message: (frozenset(), {OTHER: "named by family credit"}))
    why: dict = {}
    _, withheld = checkpoint_hunks.checkpoint_own_blobs(tmp_path, RUN, [OTHER], "", why)
    assert withheld == [OTHER] and why[OTHER] == "named by family credit"
