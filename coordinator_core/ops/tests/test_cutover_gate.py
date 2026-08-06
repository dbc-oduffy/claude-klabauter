"""
coordinator_core.ops.tests.test_cutover_gate — C6's standing regression
coverage for the gate's red case: a derived consumer absent from
confirmed_consumers must REFUSE, and the allow path (a fully-covered record)
must PASS. This is deliberately a STANDING pytest assertion — unlike C6's
OTHER deliverable (the live DR-084 refusal, recorded as a one-shot artifact
in the closed-reason-terminal.md cutover record's own derivation_history,
never as a pytest assertion here — Review: the Director of Engineering-cutover-review F9, see
docs/plans/2026-07-25-cutover-state-machine.md § C6).

Does NOT re-cover the handler's other agreement legs (narrowing,
foreign-repo fail-closed, signal-2 re-verification dispatch) — those are
C4b's test_cutover_gate_handler.py, which this file does not duplicate or
collide with; nor derive()'s own derivation-mode correctness (C4a's
test_cutover_gate_derivation.py).

Assertion style follows house convention (test_block_reviewer_bash_outside_
allowlist.py:143-149): assert on the verdict AND on load-bearing substrings
of the refusal reason, not the verdict alone.

Spec backlink: docs/plans/2026-07-25-cutover-state-machine.md § C6 (Review:
The Director of Engineering-cutover-review F9).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from coordinator_core.ops.cutover_gate import _cutover_gate


def _write_record(
    tmp_path: Path,
    *,
    confirmed_consumers: list | None = None,
) -> Path:
    fm: dict = {
        "surface": "test surface — red case regression",
        "phase": "dual-write",
        "confirmed_consumers": confirmed_consumers or [],
        "gate_source": {
            "kind": "value-vocabulary",
            "pattern": "TARGET_TOKEN",
            "paths": ["sub"],
            "repos": [{"repo": "example-doctrine-repo", "foreign": False}],
        },
    }
    fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)
    record_path = tmp_path / "record.md"
    record_path.write_text(f"---\n{fm_text}---\n\n# Test record\n", encoding="utf-8")
    return record_path


def _write_consumer_writer(tmp_path: Path, filename: str) -> None:
    sub = tmp_path / "sub"
    sub.mkdir(exist_ok=True)
    (sub / filename).write_text('TOKEN = "TARGET_TOKEN"\n', encoding="utf-8")


def _gate(params: dict, repo_root) -> dict:
    return asyncio.run(_cutover_gate(params, repo_root=repo_root))


def test_derived_consumer_absent_from_confirmed_refuses(tmp_path: Path) -> None:
    """The red case: derive() finds a live consumer of the vocabulary that
    confirmed_consumers never named. The gate must REFUSE — not PASS, not
    silently skip the unnamed consumer — and the reason must name the
    specific unconfirmed id, per house convention."""
    _write_consumer_writer(tmp_path, "unconfirmed_writer.py")
    record_path = _write_record(tmp_path, confirmed_consumers=[])

    result = _gate({"record": str(record_path)}, tmp_path / ".git")

    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    refusal_notes = [n for n in result["notes"] if "no matching" in n.lower()]
    assert refusal_notes, result["notes"]
    assert "example-doctrine-repo:sub/unconfirmed_writer.py" in refusal_notes[0]
    assert "confirmed_consumers entry" in refusal_notes[0]


def test_derived_consumer_absent_from_confirmed_refuses_even_with_other_confirmed(
    tmp_path: Path,
) -> None:
    """The red case survives a partially-confirmed record: one consumer
    correctly confirmed does not exempt a SECOND, unconfirmed consumer the
    same derivation finds — the gate must still REFUSE on the gap, not PASS
    because *some* coverage exists."""
    _write_consumer_writer(tmp_path, "confirmed_writer.py")
    _write_consumer_writer(tmp_path, "unconfirmed_writer.py")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "example-doctrine-repo:sub/confirmed_writer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )

    result = _gate({"record": str(record_path)}, tmp_path / ".git")

    assert result["exit_code"] == 2
    assert "VERDICT=REFUSE" in result["verdict_line"]
    refusal_notes = [n for n in result["notes"] if "no matching" in n.lower()]
    assert refusal_notes, result["notes"]
    assert "example-doctrine-repo:sub/unconfirmed_writer.py" in refusal_notes[0]


def test_fully_covered_record_passes(tmp_path: Path) -> None:
    """The allow path: every consumer the derivation finds has a matching
    confirmed_consumers entry — two-way agreement holds and the gate PASSES."""
    _write_consumer_writer(tmp_path, "writer.py")
    record_path = _write_record(
        tmp_path,
        confirmed_consumers=[
            {
                "id": "example-doctrine-repo:sub/writer.py",
                "verified_by": {"kind": "probe-op-key", "ref": "ping"},
                "verified_at": "2026-07-25",
            }
        ],
    )

    result = _gate({"record": str(record_path)}, tmp_path / ".git")

    assert result["exit_code"] == 0
    assert "VERDICT=PASS" in result["verdict_line"]
    assert result["derivation_history_entry"]["derived_count"] == 1
