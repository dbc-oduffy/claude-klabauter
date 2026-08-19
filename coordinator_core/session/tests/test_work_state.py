"""
coordinator_core.session.tests.test_work_state

Purpose: pins C1b of docs/plans/2026-08-19-fleet-work-state-who-holds-
which-baton.md — `build_work_state`'s held/unclaimed partition, the
readiness-is-consumed-not-derived contract, and the four-bucket
`basis` handling.

AC13 (import-cycle standalone) is already pinned by the sibling C1a file
`test_work_state_imports.py`, in its own subprocess — not repeated here in
full; a light corroborating check is included below since the C1b brief
names it explicitly.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.session.work_state as ws
from coordinator_core.win_portability import no_console_creationflags

#: Repo root derived from this file's own location (never a hardcoded
#: drive/host path) — coordinator_core/session/tests/test_work_state.py is
#: three levels below the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _code_lines(func) -> list[str]:
    """Source lines of `func`'s BODY, docstring stripped — a plain substring
    scan over `inspect.getsource` false-positives on prose inside the
    function's own docstring (e.g. this module's docstrings quote
    `pickup_ready`/`archive/handoffs/` while explaining why the CODE never
    reads them); this operates on executable statements only."""
    import ast
    import textwrap

    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    fn = tree.body[0]
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]  # drop the docstring statement
    lines: list[str] = []
    for stmt in body:
        segment = ast.get_source_segment(source, stmt) or ""
        lines.extend(segment.splitlines())
    return lines


def _write_handoff(tmp_path: Path, name: str, body: str, *, archived: bool = False) -> Path:
    subdir = tmp_path / ("archive/handoffs" if archived else "state/handoffs")
    subdir.mkdir(parents=True, exist_ok=True)
    p = subdir / name
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# AC1 — held/unclaimed partition, frame-free row shape
# ---------------------------------------------------------------------------

def test_held_and_unclaimed_partition(tmp_path):
    _write_handoff(
        tmp_path,
        "held.md",
        """---
id: hnd-held-fixture-000001
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
claimed_by: sess-fake-holder-1234
---
Body.
""",
    )
    _write_handoff(
        tmp_path,
        "unclaimed.md",
        """---
id: hnd-unclaimed-fixture-000002
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)

    assert len(result["held"]) == 1
    assert len(result["unclaimed"]) == 1
    assert result["held"][0]["claimed_by"] == "sess-fake-holder-1234"
    assert result["unclaimed"][0]["path"].endswith("unclaimed.md")


def test_held_row_shape_is_frame_free(tmp_path):
    """AC1: HELD rows carry exactly the listed keys — no verdict, no
    disposition, no scope_overlap, no recent_paths (the THIS-ARTIFACT frame
    is absent by construction, not merely unused)."""
    _write_handoff(
        tmp_path,
        "held.md",
        """---
id: hnd-held-fixture-000003
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
claimed_by: sess-fake-holder-5678
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)
    assert len(result["held"]) == 1
    row = result["held"][0]

    expected_keys = {
        "path",
        "claimed_by",
        "holder_live",
        "liveness_basis",
        "last_activity_age_sec",
        "send_message_address",
        "send_message_address_unavailable_reason",
        "send_message_address_resolved_at",
    }
    assert set(row.keys()) == expected_keys
    for forbidden in ("verdict", "disposition", "scope_overlap", "recent_paths"):
        assert forbidden not in row


# ---------------------------------------------------------------------------
# AC3 — archival expressed by scan root, not a filter
# ---------------------------------------------------------------------------

def test_archived_record_is_invisible_never_scanned(tmp_path):
    _write_handoff(
        tmp_path,
        "archived.md",
        """---
id: hnd-archived-fixture-000004
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
---
Body.
""",
        archived=True,
    )

    result = ws.build_work_state(tmp_path)
    assert result == {"held": [], "unclaimed": []}


def test_build_work_state_scan_root_is_single_glob_no_second_filter():
    """Source-level corroboration of AC3's one-condition shape: the only
    place this module's row-emission set is built is off
    `collect_live_handoff_paths` (state/handoffs/ only) — never a second
    'skip if under archive/' predicate applied after the fact."""
    lines = _code_lines(ws.build_work_state)
    joined = "\n".join(lines)
    assert "collect_live_handoff_paths" in joined
    assert "archive" not in joined.lower()


# ---------------------------------------------------------------------------
# AC3b — gate_notes verbatim shape, blocking_notes never gates readiness
# ---------------------------------------------------------------------------

def test_blocking_notes_present_with_empty_blocked_by_still_lands_in_unclaimed(tmp_path):
    _write_handoff(
        tmp_path,
        "notes.md",
        """---
id: hnd-notes-fixture-000005
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
blocking_notes: "Windows machine required for AC7 verification"
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)
    assert len(result["unclaimed"]) == 1
    row = result["unclaimed"][0]
    assert row["gate_notes"] == {
        "present": True,
        "text": "Windows machine required for AC7 verification",
        "passed": None,
    }


def test_gate_notes_absent_shape():
    from coordinator_core.session.work_state import _gate_notes

    assert _gate_notes({}) == {"present": False, "text": None, "passed": None}


# ---------------------------------------------------------------------------
# AC3c + mandatory string-coercion test — VALUE not truthiness
# ---------------------------------------------------------------------------

def test_stamp_disagrees_and_blocked_by_empty_list_read_by_value_not_truthiness(tmp_path):
    """`blocked_by: []` must parse to an actual empty list (vacuously freed,
    landing in `unclaimed`) — `bool("false") is True` is the defect class:
    a text-scalar parser handing back the raw string "[]" would read as
    non-empty/truthy and misclassify every candidate as blocked. `pickup_ready:
    false` must likewise resolve to the real Python `False`, not a truthy
    string, for the `stamp_disagrees` comparison to mean anything."""
    _write_handoff(
        tmp_path,
        "coerce.md",
        """---
id: hnd-coerce-fixture-000006
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
pickup_ready: false
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)
    assert len(result["unclaimed"]) == 1, (
        "blocked_by: [] must resolve to a real empty list (vacuously freed), "
        "not a truthy raw-text scalar"
    )
    row = result["unclaimed"][0]
    assert row.get("stamp_disagrees") is True


def test_stamp_agreeing_with_computed_state_has_no_marker(tmp_path):
    _write_handoff(
        tmp_path,
        "agree.md",
        """---
id: hnd-agree-fixture-000007
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
pickup_ready: true
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)
    assert len(result["unclaimed"]) == 1
    assert "stamp_disagrees" not in result["unclaimed"][0]


# ---------------------------------------------------------------------------
# AC3 — readiness is consumed, never derived; four buckets over `basis`
# ---------------------------------------------------------------------------

def test_still_blocked_never_lands_in_unclaimed(tmp_path):
    _write_handoff(
        tmp_path,
        "blocked.md",
        """---
id: hnd-blocked-fixture-000008
kind: session-handoff
deployment_state: awaiting_gate
blocked_by:
  - hnd-nonexistent-000000
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)
    assert result == {"held": [], "unclaimed": []}


def test_off_gate_axis_never_reaches_readiness_axis(tmp_path):
    _write_handoff(
        tmp_path,
        "in_flight.md",
        """---
id: hnd-inflight-fixture-000009
kind: session-handoff
deployment_state: in_flight
---
Body.
""",
    )

    result = ws.build_work_state(tmp_path)
    assert result == {"held": [], "unclaimed": []}


def test_review_due_basis_excluded_from_both_buckets(tmp_path, monkeypatch):
    """`basis="review-due"` is its own bucket by omission — never `unclaimed`,
    never blocked. Stubbing the producer isolates this module's OWN
    dispatch-on-`basis` contract from `gate_eval`'s own review-due trigger
    conditions (deadline-elapsed `gate_evidence` legs), which this module
    never constructs on its own."""
    _write_handoff(
        tmp_path,
        "review.md",
        """---
id: hnd-review-fixture-000010
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
---
Body.
""",
    )

    def _fake_derive_readiness_batch(handoffs, all_handoffs, *, scan_incomplete=False):
        return [
            {"deployment_state": None, "pickup_ready": None, "basis": "review-due"}
            for _ in handoffs
        ]

    monkeypatch.setattr(ws, "derive_readiness_batch", _fake_derive_readiness_batch)

    result = ws.build_work_state(tmp_path)
    assert result == {"held": [], "unclaimed": []}


# ---------------------------------------------------------------------------
# AC3 — pickup_ready frontmatter is never a readiness INPUT (only ever
# compared against the producer's own computed verdict for stamp_disagrees)
# ---------------------------------------------------------------------------

def test_pickup_ready_never_drives_eligibility_only_stamp_comparison():
    """Every raw-frontmatter `pickup_ready` read in this module's source is
    the ONE stamp-comparison line feeding `stamp_disagrees` — eligibility
    for `unclaimed` is decided exclusively off `ready.get("pickup_ready")`,
    the PRODUCER's own computed verdict key, never a second frontmatter
    read standing in for it (the second-gate-evaluator shape this module's
    docstring forbids)."""
    lines = [l for l in _code_lines(ws.build_work_state) if "pickup_ready" in l]
    assert lines, "expected at least the producer-key + stamp-comparison lines"
    for line in lines:
        stripped = line.strip()
        allowed = (
            'ready.get("pickup_ready")' in stripped
            or 'stamped_pickup_ready = handoff.get("pickup_ready")' in stripped
            or "stamped_pickup_ready" in stripped
        )
        assert allowed, f"unexpected pickup_ready read/use: {stripped!r}"


def test_derive_readiness_batch_called_exactly_once(tmp_path, monkeypatch):
    calls = []
    real = ws.derive_readiness_batch

    def _spy(handoffs, all_handoffs, **kwargs):
        calls.append(1)
        return real(handoffs, all_handoffs, **kwargs)

    _write_handoff(
        tmp_path,
        "a.md",
        """---
id: hnd-spy-a-000011
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
---
Body.
""",
    )
    _write_handoff(
        tmp_path,
        "b.md",
        """---
id: hnd-spy-b-000012
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
---
Body.
""",
    )

    monkeypatch.setattr(ws, "derive_readiness_batch", _spy)
    ws.build_work_state(tmp_path)
    assert calls == [1]


# ---------------------------------------------------------------------------
# AC8 — send_message_address "" vs None+reason distinction
# ---------------------------------------------------------------------------

def test_send_message_address_none_with_reason_when_messaging_unavailable(tmp_path, monkeypatch):
    _write_handoff(
        tmp_path,
        "held.md",
        """---
id: hnd-held-fixture-000013
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
claimed_by: sess-fake-holder-9999
---
Body.
""",
    )

    def _fake_resolve(sids):
        return {}, False  # messaging box-wide unavailable

    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        _fake_resolve,
    )

    result = ws.build_work_state(tmp_path)
    assert len(result["held"]) == 1
    row = result["held"][0]
    assert row["send_message_address"] is None
    assert row["send_message_address_unavailable_reason"] == "peer-messaging-unavailable"


def test_send_message_address_empty_string_when_peer_unresolvable_but_messaging_available(
    tmp_path, monkeypatch
):
    _write_handoff(
        tmp_path,
        "held.md",
        """---
id: hnd-held-fixture-000014
kind: session-handoff
deployment_state: awaiting_gate
blocked_by: []
claimed_by: sess-fake-holder-1111
---
Body.
""",
    )

    def _fake_resolve(sids):
        return {}, True  # messaging available box-wide, this peer just unresolved

    monkeypatch.setattr(
        "coordinator_core.session.reachability.resolve_addresses_bulk_with_availability",
        _fake_resolve,
    )

    result = ws.build_work_state(tmp_path)
    assert len(result["held"]) == 1
    row = result["held"][0]
    assert row["send_message_address"] == ""
    assert row["send_message_address_unavailable_reason"] is None


# ---------------------------------------------------------------------------
# AC13 — light corroborating standalone-import check (full subprocess
# assertion lives in the sibling C1a file, test_work_state_imports.py)
# ---------------------------------------------------------------------------

def test_session_work_state_imports_standalone_without_ops_light_check():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "assert 'coordinator_core.ops' not in sys.modules\n"
            "import coordinator_core.session.work_state\n"
            "print('OK')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        **no_console_creationflags(),
    )
    assert "OK" in result.stdout
