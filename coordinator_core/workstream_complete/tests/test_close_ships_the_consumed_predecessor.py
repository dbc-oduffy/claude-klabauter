"""test_close_ships_the_consumed_predecessor — C2 of
docs/plans/2026-09-11-the-memo-lifecycle-closes-its-own-handoffs.md.

Covers the OTHER ship seam from `test_close_ships_its_batons.py` (that file
is C3 of docs/plans/2026-08-30-the-close-ships-the-baton-it-closed.md and
covers this session's own held batons via `directives_commit_tail`). This
file covers C1's `d-ship-consumed-handoff:<basename>` directive — the
PREDECESSOR baton a `predecessor-consumed` close ships, front-ended by
`directives_memo_lifecycle.build_consumed_handoff_ship_directives` and gated
at `coordinator_core.workstream_complete.__init__`'s `_consumed_handoff_ship_
paths`/`build_directives`/`brief` call sites. Do not extend
`test_close_ships_its_batons.py` with these cases — the filenames are the
only signal telling a reader which ship path is under test.

Negative-spec (mirrors the plan's own § Problem / AC4 restatement): none of
these tests drive a real `archive-stamp-cli` run or a real git commit — the
observable under test is the ORCHESTRATION (which directive gets emitted,
with what shape, in what order, and how the completeness judgment point
wires to it), never `handoff.archive_transition`'s already-tested internals.
AC4 is checked at the directive level (assertion 1: the `d-ship-consumed-
handoff:*` directive exists, with no `--sha`/`--archive`/`best_effort`), not
by asserting a real on-disk `deployment_state` flip.

GATE WIRING (assertion 4) is checked against `brief()`'s output for BOTH
ends, never `build_directives()` alone: the `_gated_directive_id` append
loop and `build_consumed_handoff_completeness_judgment_point` both live
inside `brief()` (`__init__.py`), operating on the directives list
`build_directives` already returned — a `build_directives`-only call cannot
see this wiring at all (plan body's own F3 review note: an executor writing
assertion 4 entirely against `build_directives` output gets a red test with
no defect behind it).

Run: python -m pytest coordinator_core/workstream_complete/tests/test_close_ships_the_consumed_predecessor.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.ops.ceremony.wsc_disposition import PREDECESSOR_CONSUMED, SINGLE_SESSION
from coordinator_core.workstream_complete import directives_commit_tail


def _gate(
    disposition: str,
    consumed_handoff: str = "",
    consumed_handoff_paths: tuple[str, ...] | None = None,
) -> wsc.SessionShapeGate:
    if consumed_handoff_paths is None:
        consumed_handoff_paths = (consumed_handoff,) if consumed_handoff else ()
    return wsc.SessionShapeGate(
        sid="testsid-ship-consumed-predecessor",
        disposition=disposition,
        consumed_handoff=consumed_handoff,
        diagnostics=[],
        consumed_handoff_paths=consumed_handoff_paths,
        detection={},
    )


def _patch_gate(monkeypatch: pytest.MonkeyPatch, gate: wsc.SessionShapeGate) -> None:
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: gate)


def _write_ac_handoff(tmp_path: Path, rel_path: str, body: str) -> None:
    handoff_path = tmp_path / rel_path
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(f"---\nstatus: open\n---\n\n{body}\n", encoding="utf-8")


def _ship_directives(directives: list[dict]) -> list[dict]:
    return [d for d in directives if d["id"].startswith("d-ship-consumed-handoff:")]


# ---------------------------------------------------------------------------
# 1. POSITIVE
# ---------------------------------------------------------------------------


def test_resolvable_predecessor_consumed_handoff_emits_the_ship_directive(tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, {}, tmp_path)

    ship = _ship_directives(directives)
    assert len(ship) == 1
    entry = ship[0]
    assert entry["id"] == "d-ship-consumed-handoff:foo.md"
    assert entry["cli"] == "archive-stamp-cli"
    assert entry["args"] == ["ship-handoff", "state/handoffs/foo.md"]


# ---------------------------------------------------------------------------
# 2. NEGATIVE, DISPOSITION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disposition",
    [SINGLE_SESSION, "memo-predecessor"],
)
def test_every_other_disposition_emits_no_ship_directive(tmp_path, disposition):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    gate = _gate(disposition, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, {}, tmp_path)

    assert _ship_directives(directives) == []


def test_predecessor_consumed_with_unresolvable_path_emits_no_ship_directive(tmp_path):
    # No file written under tmp_path at all -- the absent-file case catches
    # an emit predicate written against the gate field alone rather than
    # against resolution, which is the case a `disposition`-only predicate
    # cannot distinguish from assertion 1's positive fixture.
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/does-not-exist.md",))

    directives = wsc.build_directives(gate, {}, tmp_path)

    assert _ship_directives(directives) == []


# ---------------------------------------------------------------------------
# 3. AC2 -- no truthy best_effort key
# ---------------------------------------------------------------------------


def test_ship_directive_carries_no_truthy_best_effort_key(tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, {}, tmp_path)

    entry = _ship_directives(directives)[0]
    assert not entry.get("best_effort")


# ---------------------------------------------------------------------------
# 4. GATE, BOTH ENDS -- checked against `brief()`'s output only (see module
# docstring). Both legs need the completeness gate to BLOCK (an unticked
# acceptance criterion) since the `_gated_directive_id` append loop inside
# `brief()` only runs when `consumed_handoff_completeness_gate.blocks` is
# True.
# ---------------------------------------------------------------------------


def test_ship_directive_depends_on_the_completeness_judgment_point(tmp_path, monkeypatch):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [ ] one\n")
    gate = _gate(
        PREDECESSOR_CONSUMED,
        consumed_handoff="state/handoffs/foo.md",
        consumed_handoff_paths=("state/handoffs/foo.md",),
    )
    _patch_gate(monkeypatch, gate)

    directives = wsc.brief(decisions={}, repo_root=tmp_path)["directives"]

    entry = _ship_directives(directives)[0]
    assert "jp-consumed-handoff-completeness" in (entry.get("depends_on") or [])


def test_completeness_judgment_point_resolves_names_the_ship_id_on_every_arm(tmp_path, monkeypatch):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [ ] one\n")
    gate = _gate(
        PREDECESSOR_CONSUMED,
        consumed_handoff="state/handoffs/foo.md",
        consumed_handoff_paths=("state/handoffs/foo.md",),
    )
    _patch_gate(monkeypatch, gate)

    envelope = wsc.brief(decisions={}, repo_root=tmp_path)

    jp = next(
        jp for jp in envelope["judgment_points"] if jp["id"] == "jp-consumed-handoff-completeness"
    )
    ship_id = "d-ship-consumed-handoff:foo.md"
    arms = {d["value"]: d for d in jp["dispositions"]}
    assert "verified-complete-proceed" in arms
    assert "override-known-in-flight" in arms
    for arm_value in ("verified-complete-proceed", "override-known-in-flight"):
        assert ship_id in arms[arm_value]["resolves"], (
            f"arm {arm_value!r} must name {ship_id!r} in resolves, or an EM reaching "
            "the gate by that arm can never ship the consumed baton"
        )


# ---------------------------------------------------------------------------
# 5. ORDERING
# ---------------------------------------------------------------------------


def test_ship_directive_precedes_the_terminal_handoff_sweep(tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, {}, tmp_path)

    ids = [d["id"] for d in directives]
    ship_idx = ids.index("d-ship-consumed-handoff:foo.md")
    sweep_idx = ids.index("d-sweep-terminal-handoffs")
    assert ship_idx < sweep_idx


# ---------------------------------------------------------------------------
# 6. PLURALITY
# ---------------------------------------------------------------------------


def test_two_consumed_handoff_paths_emit_two_distinct_ship_ids(tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    _write_ac_handoff(tmp_path, "state/handoffs/bar.md", "## Acceptance criteria\n\n- [x] one\n")
    gate = _gate(
        PREDECESSOR_CONSUMED,
        consumed_handoff="state/handoffs/foo.md",
        consumed_handoff_paths=("state/handoffs/foo.md", "state/handoffs/bar.md"),
    )

    directives = wsc.build_directives(gate, {}, tmp_path)

    ship_ids = {d["id"] for d in _ship_directives(directives)}
    assert ship_ids == {"d-ship-consumed-handoff:foo.md", "d-ship-consumed-handoff:bar.md"}


# ---------------------------------------------------------------------------
# 7. NON-OVERLAP
# ---------------------------------------------------------------------------


def test_no_ship_directive_when_the_commit_tail_already_stamps_it(tmp_path, monkeypatch):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {
        "handoff_dispositions": {
            "foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}
        }
    }
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, decisions, tmp_path)

    assert _ship_directives(directives) == []


def test_ship_directive_still_emitted_when_shipped_disposition_carries_no_shipped_in(
    tmp_path, monkeypatch
):
    # AMENDMENT (C1's F2 review note, docs/plans/2026-09-11-the-memo-
    # lifecycle-closes-its-own-handoffs.md § C1 body): the suppression must
    # call `directives_commit_tail.resolve_ship_stamp_candidates` itself
    # rather than re-deriving a one-conjunct ("disposition == shipped")
    # approximation. That function also requires a TRUTHY `shipped_in`
    # before it will treat a basename as a commit-tail candidate -- a
    # `disposition: shipped` entry with no `shipped_in` is NOT suppressed
    # here, so this consumed predecessor is still this close's to ship. A
    # one-conjunct emit predicate (disposition alone) would wrongly suppress
    # this case and leave the baton stamped by nobody.
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {
        "handoff_dispositions": {
            "foo.md": {"disposition": "shipped", "shipped_in": ""}
        }
    }
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, decisions, tmp_path)

    ship = _ship_directives(directives)
    assert len(ship) == 1
    assert ship[0]["id"] == "d-ship-consumed-handoff:foo.md"


# ---------------------------------------------------------------------------
# 8. PORTABILITY
# ---------------------------------------------------------------------------


def test_emitted_path_argument_contains_no_backslash(tmp_path):
    _write_ac_handoff(tmp_path, "state/handoffs/foo.md", "## Acceptance criteria\n\n- [x] one\n")
    gate = _gate(PREDECESSOR_CONSUMED, consumed_handoff_paths=("state/handoffs/foo.md",))

    directives = wsc.build_directives(gate, {}, tmp_path)

    entry = _ship_directives(directives)[0]
    assert "\\" not in entry["args"][-1]
