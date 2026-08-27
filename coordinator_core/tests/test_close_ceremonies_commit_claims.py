"""
coordinator_core.tests.test_close_ceremonies_commit_claims

C5 (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-wrote.md
§ C5): the two REACHABLE close ceremonies — `/quick-wrap`
(`coordinator_core.quick_wrap_assemble.brief`) and `/workstream-complete`'s
in-process tail sequencer (`coordinator_core.ops.ceremony.wsc_tail._handler`)
— were to each fire C4's hardened `commit_session_offer_async` in-process,
deterministically, rather than relying on an agent-executed directive that
may never actually run.

STATUS: `/quick-wrap` ships in this chunk (below). `/workstream-complete` does
NOT — see "wsc_tail: BLOCKED" below; this file covers `/quick-wrap` only.
`/handoff` is explicitly out of scope for this chunk (see the chunk body).

wsc_tail: BLOCKED (2026-08-20, this chunk's own execution). Wiring an
in-process `commit_session_offer_async` call into
`coordinator_core.ops.ceremony.wsc_tail._handler` breaks the PM-ratified
wsc-tail-sub-2s-invoke-budget invariant (module docstring "Push-mode /
result-contract decision" section; DEC-1/DEC-3,
docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md): measured
`coordinator_core/ops/ceremony/tests/test_wsc_tail_parity.py::
test_kpi_wsc_tail_blocking_path_under_2s` at 3.386s (budget <2.0s) with the
safety net wired in synchronously — `commit_session_offer_async`'s
`compute_offer`/`compute_scope` call chain runs several of its own git
spawns, and that test file (out of this chunk's declared `writes:` scope) is
a hard KPI gate, not a soft parity check. The same wiring also breaks that
file's `test_timing_map_covers_every_instrumented_step_with_nonnegative_ms`
(a closed step-name-set contract) by adding an unaccounted-for timing span.
Neither test is editable within this chunk's scope, and reconciling the
budget (detach the call, widen the KPI, or something else) is a product/
architecture decision this chunk's brief did not make. See the C5 dispatch
report for the full BLOCKED writeup; the `wsc_tail.py` edit was reverted in
this chunk rather than shipped red.

This file asserts, for `/quick-wrap` only:
  1. `commit_session_offer_async` is invoked IN-PROCESS by `brief()` (never
     merely emitted as a `safe-commit-offer` directive for an agent to run
     later).
  2. A failure inside that call does not prevent the ceremony from
     completing — no exception escapes `brief()`, the envelope is still
     returned.
  3. AC9: the structured outcome is rendered as a NAMED FACT on the
     ceremony's own close output (`envelope["gates"]["commit_outcome"]`,
     never only a log line/stderr), and its residue is carried on that same
     fact — residue paths stay dirty on disk, so the next session's own
     git-status-backed workstream-start read surfaces them structurally,
     without a second write here.

Spec backlink: state/dispatch-briefs/2026-08-20-the-close-ceremony-commits-
what-the-session-wrote/C5.md
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinator_core import quick_wrap_assemble as qwa
from coordinator_core.test_quick_wrap_assemble import _SID, _stub_facts_all_computed


def _empty_outcome_report(session_id: str, residue: dict | None = None) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "groups": [],
        "excluded": [],
        "failed_groups": [],
        "dropped_groups": [],
        "residue": residue or {},
        "outcome": {
            "status": "empty",
            "detail": "no claimed path(s) to commit this call.",
            "committed_paths": [],
            "conflicted_paths": [],
        },
    }


def _committed_outcome_report(session_id: str, residue: dict) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "groups": [{"paths": ["state/foo.md"], "message": "m", "committed": True,
                     "sha": "deadbeef", "push_state": None, "error": None,
                     "commit_failed": False, "reason": None}],
        "excluded": [],
        "failed_groups": [],
        "dropped_groups": [],
        "residue": residue,
        "outcome": {
            "status": "committed",
            "detail": "1 path(s) committed across 1 group(s).",
            "committed_paths": ["state/foo.md"],
            "conflicted_paths": [],
        },
    }


@pytest.fixture
def qw_repo(tmp_path: Path) -> Path:
    import json

    common = tmp_path / ".git"
    path = common / "coordinator-sessions" / _SID / "session-shape.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "session_id": _SID}), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _qw_no_real_git(monkeypatch):
    def _boom(args, cwd):
        raise AssertionError(f"unstubbed git call: {args}")

    monkeypatch.setattr(qwa, "_git_out", _boom)


def test_quick_wrap_calls_auto_commit_in_process(qw_repo, monkeypatch):
    calls: list[tuple[Any, ...]] = []

    async def _fake(session_id, cwd=None, groups=None, invoker=None):
        calls.append((session_id, cwd, groups, invoker))
        return _empty_outcome_report(session_id)

    _stub_facts_all_computed(monkeypatch, qw_repo)
    monkeypatch.setattr(qwa, "commit_session_offer_async", _fake)

    envelope = qwa.brief()

    assert calls, "quick_wrap_assemble.brief() must call commit_session_offer_async in-process"
    assert calls[0][0] == _SID
    assert calls[0][3] == "attended"
    assert "safe-commit-offer" not in [d["cli"] for d in envelope["directives"]]


def test_quick_wrap_auto_commit_failure_does_not_block_completion(qw_repo, monkeypatch):
    async def _boom(session_id, cwd=None, groups=None, invoker=None):
        raise RuntimeError("boom")

    _stub_facts_all_computed(monkeypatch, qw_repo)
    monkeypatch.setattr(qwa, "commit_session_offer_async", _boom)

    envelope = qwa.brief()  # must not raise

    assert envelope["gates"]["commit_outcome"]["status"] == "error"
    assert "next_move" in envelope


def test_quick_wrap_renders_outcome_and_residue(qw_repo, monkeypatch):
    residue = {"state/leftover": ["state/leftover/file.md"]}

    async def _fake(session_id, cwd=None, groups=None, invoker=None):
        return _committed_outcome_report(session_id, residue)

    _stub_facts_all_computed(monkeypatch, qw_repo)
    monkeypatch.setattr(qwa, "commit_session_offer_async", _fake)

    envelope = qwa.brief()

    outcome = envelope["gates"]["commit_outcome"]
    assert outcome["status"] == "committed"
    assert outcome["committed_paths"] == ["state/foo.md"]
    # AC9 residue: carried on the wire response so it stays visible for the
    # next session's own workstream-start dirty-tree read (structural — the
    # residue paths remain dirty on disk; this pins that this ceremony's own
    # output does not swallow that fact).
    assert outcome["residue"] == residue
