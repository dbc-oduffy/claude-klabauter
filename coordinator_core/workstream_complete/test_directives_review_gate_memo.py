"""test_directives_review_gate_memo — regression tests for the C4 gate
verdict memo (docs/plans/2026-08-10-commit-event-5s-cap-and-the-silent-
tail.md, AC6), retry #3.

The first two attempts at this AC recorded the memo INSIDE the directive
builders, at directive-BUILD time — unconditionally, independent of whether
the gate CLI ever dispatched or what verdict it returned. That poisons a
read-only `brief()` preview (which calls the same builders `apply()` does)
before the gate ever ran, and caches a WARN/FAIL result as done. This file
now asserts the FIXED contract:

  - The builder (`build_review_brightline_gate_directive`) is READ-ONLY at
    build time: a `gate_memo_hit` lookup only, never a write, regardless of
    how many times it is called.
  - The write happens exactly once, from `directives_review.
    record_gate_verdict_if_passed` — the function `apply.py::
    _execute_directives` calls after a directive actually dispatched this
    pass, verdict-aware: the brightline gate records only on a
    resolved-range (3-arg) call that exited 0 — never the symbolic-default
    (2-arg) shape, which can go stale without the key changing (see that
    function's own docstring for why).
  - `apply.py::_execute_directives` end-to-end: unchanged inputs after a
    confirmed PASS skip the gate's dispatch entirely on the next pass; a
    changed input re-fires it.

The chain-end coverage gate (`build_chain_coverage_gate_directive`,
`d-coverage-gate` memo recording, `_coverage_directive`) was removed by
K-001 (state/kill-ledger.md) — every test that exercised it here is
retired along with it; see that commit (55e64be13) for the removal
evidence.

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_directives_review_gate_memo.py -q
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest

from coordinator_core.workstream_complete import apply as ws_apply
from coordinator_core.workstream_complete import build_write_trail_directives
from coordinator_core.workstream_complete.directives_review import (
    build_review_brightline_gate_directive,
    gate_memo_hit,
    record_gate_memo,
    record_gate_verdict_if_passed,
)


# ---------------------------------------------------------------------------
# Builders are read-only at build time — never write, regardless of repeats.
# ---------------------------------------------------------------------------


def test_brightline_gate_directive_build_alone_never_writes_a_memo(tmp_path: Path) -> None:
    for _ in range(3):
        directive = build_review_brightline_gate_directive("sid-1", repo_root=tmp_path)
        assert directive["already_satisfied"] is False
    memo_dir = tmp_path / "state" / "ceremony" / "wsc-gate-verdict-memo"
    assert not memo_dir.exists() or list(memo_dir.iterdir()) == []


def test_brightline_gate_directive_build_sees_an_execution_time_hit(tmp_path: Path) -> None:
    args = ["--session-id", "sid-1"]
    assert build_review_brightline_gate_directive("sid-1", repo_root=tmp_path)["already_satisfied"] is False
    record_gate_memo(tmp_path, "d-run-review-brightline-gate", *args)
    directive = build_review_brightline_gate_directive("sid-1", repo_root=tmp_path)
    assert directive["already_satisfied"] is True


def test_brightline_gate_directive_resolved_range_change_misses_even_with_same_session(tmp_path: Path) -> None:
    """A new trail record moving the resolved floor mints a different
    argv -- the memo must key on the RESOLVED range, not session_id alone,
    so a stale-floor memo never masks a real scope change."""

    def is_ancestor(head: str, tip: str) -> bool:
        return True

    records_v1 = [{"sha_range_head": "aaa1111"}]
    first = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=records_v1,
        chain_tip_sha="ttt9999",
        is_ancestor=is_ancestor,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert first["already_satisfied"] is False
    record_gate_memo(tmp_path, first["id"], *first["args"])

    repeat = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=records_v1,
        chain_tip_sha="ttt9999",
        is_ancestor=is_ancestor,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert repeat["already_satisfied"] is True

    records_v2 = [{"sha_range_head": "aaa1111"}, {"sha_range_head": "bbb2222"}]
    moved = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=records_v2,
        chain_tip_sha="ttt9999",
        is_ancestor=is_ancestor,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert moved["already_satisfied"] is False
    assert moved["args"] != first["args"]


# ---------------------------------------------------------------------------
# gate_memo_hit / record_gate_memo — the low-level primitives directly.
# ---------------------------------------------------------------------------


def test_gate_memo_hit_is_false_before_any_record(tmp_path: Path) -> None:
    assert gate_memo_hit(tmp_path, "d-some-gate", "input-a") is False


def test_gate_memo_hit_is_true_after_record_with_identical_parts(tmp_path: Path) -> None:
    record_gate_memo(tmp_path, "d-some-gate", "input-a", "input-b")
    assert gate_memo_hit(tmp_path, "d-some-gate", "input-a", "input-b") is True


def test_gate_memo_hit_is_order_sensitive(tmp_path: Path) -> None:
    record_gate_memo(tmp_path, "d-some-gate", "input-a", "input-b")
    assert gate_memo_hit(tmp_path, "d-some-gate", "input-b", "input-a") is False


def test_gate_memo_hit_distinguishes_gate_ids(tmp_path: Path) -> None:
    record_gate_memo(tmp_path, "d-run-chain-coverage-gate", "same-input")
    assert gate_memo_hit(tmp_path, "d-run-review-brightline-gate", "same-input") is False


# ---------------------------------------------------------------------------
# record_gate_verdict_if_passed — the execution-time, verdict-aware writer.
# ---------------------------------------------------------------------------


def _brightline_directive(args: list[str]) -> dict[str, Any]:
    return {"id": "d-run-review-brightline-gate", "cli": "review-brightline-gate", "args": args}


#: Review-integrator (Finding 1, 2026-08-11): `record_gate_verdict_if_passed`
#: now requires BOTH range halves to be concrete 40-hex-digit object ids
#: (never a bare/abbreviated ref) before it memoizes a brightline range —
#: see that function's own docstring. These fixtures use full-length fake
#: shas so the concreteness check they exercise is the same shape the
#: gate's real argv carries; a short placeholder like `"aaa1111"` would
#: fail the check regardless of which test property is under test.
_FLOOR_SHA = "a1" * 20
_TIP_SHA = "b2" * 20


def test_record_gate_verdict_records_brightline_on_resolved_range_pass(tmp_path: Path) -> None:
    directive = _brightline_directive(["--session-id", "sid-1", f"{_FLOOR_SHA}..{_TIP_SHA}"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, f"range={_FLOOR_SHA}..{_TIP_SHA} VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is True


def test_record_gate_verdict_never_records_brightline_symbolic_default_shape(tmp_path: Path) -> None:
    """The ordinary no-floor-resolved 2-arg shape falls back to the gate's
    OWN symbolic default range (merge-base(origin/main, HEAD)..HEAD), which
    re-resolves at the NEXT call's time — a new commit between two apply
    passes changes the real input without changing this key. Never
    memoized (settles the key-staleness question this stub raised)."""
    directive = _brightline_directive(["--session-id", "sid-1"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, "VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is False


def test_record_gate_verdict_never_records_brightline_when_tip_is_still_symbolic(tmp_path: Path) -> None:
    """Review-integrator (Finding 1, 2026-08-11): the ONLY production caller
    supplying the floor kwargs (`_resolve_review_brightline_floor_kwargs`)
    passes the literal string `"HEAD"` as `chain_tip_sha`, by design — so
    the real mid-chain argv is 3 elements (`floor..HEAD`) but its tip half
    is still a moving symbolic ref, not a frozen object id. This is the
    exact shape that must MISS: `len(args) == 3` alone is not a sound
    concreteness proxy, and recording here would reopen the identical
    stale-key hazard the 2-arg-shape exclusion above exists to prevent."""
    directive = _brightline_directive(["--session-id", "sid-1", f"{_FLOOR_SHA}..HEAD"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, "VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is False


def test_record_gate_verdict_never_records_brightline_when_floor_is_still_symbolic(tmp_path: Path) -> None:
    """The mirror of the tip case above. `_is_concrete_sha` gates both halves
    of the range through one boolean, so this direction cannot currently
    regress independently — but nothing pins that, and a later edit
    optimizing the check to test only the tip (the half the known production
    caller gets wrong) would pass every other test in this file. No
    production caller produces a symbolic floor today; this test exists to
    keep the symmetry honest rather than to describe a live shape."""
    directive = _brightline_directive(["--session-id", "sid-1", f"HEAD..{_TIP_SHA}"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, "VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is False


def test_record_gate_verdict_does_not_record_brightline_on_nonzero_exit(tmp_path: Path) -> None:
    directive = _brightline_directive(["--session-id", "sid-1", f"{_FLOOR_SHA}..{_TIP_SHA}"])
    record_gate_verdict_if_passed(tmp_path, directive, 1, "")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is False


def test_record_gate_verdict_is_a_noop_for_unrelated_directive_ids(tmp_path: Path) -> None:
    directive = {"id": "d-write-trail", "cli": "wsc-coverage-gate-runner", "args": ["write-trail"]}
    record_gate_verdict_if_passed(tmp_path, directive, 0, "")
    memo_dir = tmp_path / "state" / "ceremony" / "wsc-gate-verdict-memo"
    assert not memo_dir.exists() or list(memo_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# apply.py::_execute_directives — end-to-end skip/re-fire/no-poison behavior.
# ---------------------------------------------------------------------------


def _fake_module(main_fn: Callable[..., Any]) -> ModuleType:
    mod = ModuleType("fake_cli")
    mod.main = main_fn
    return mod


def test_execute_directives_unchanged_inputs_after_pass_skip_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review-integrator (Finding 1, 2026-08-11): the floor and tip must both
    # be concrete 40-hex-digit shas for `record_gate_verdict_if_passed` to
    # memoize this pass at all — see `_FLOOR_SHA`/`_TIP_SHA`'s docstring.
    args = ["--session-id", "sid-1", f"{_FLOOR_SHA}..{_TIP_SHA}"]
    dispatch_count = {"n": 0}

    def gate_main(argv: list[str]) -> int:
        dispatch_count["n"] += 1
        print("VERDICT=single-reviewer-ok")
        return 0

    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: _fake_module(gate_main))

    directive = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=[{"sha_range_head": _FLOOR_SHA}],
        chain_tip_sha=_TIP_SHA,
        is_ancestor=lambda head, tip: True,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert directive["args"] == args
    assert directive["already_satisfied"] is False

    exit_code, report = ws_apply._execute_directives([directive], [], {}, repo_root=tmp_path)
    assert report["landed"] == [directive["id"]]
    assert dispatch_count["n"] == 1

    # Second pass: builder now sees the execution-time memo -> already_satisfied.
    directive_2 = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=[{"sha_range_head": _FLOOR_SHA}],
        chain_tip_sha=_TIP_SHA,
        is_ancestor=lambda head, tip: True,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert directive_2["already_satisfied"] is True
    exit_code_2, report_2 = ws_apply._execute_directives([directive_2], [], {}, repo_root=tmp_path)
    assert report_2["landed"] == [directive_2["id"]]
    assert dispatch_count["n"] == 1  # not dispatched again


def test_execute_directives_symbolic_tip_re_dispatches_every_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review-integrator (Finding 1, 2026-08-11): the real production shape
    (`_resolve_review_brightline_floor_kwargs` passing the literal `"HEAD"`
    as `chain_tip_sha`) must NEVER memoize, end to end — even after a
    confirmed exit-0 pass, the next `_execute_directives` call re-dispatches
    because the tip half of the range is still symbolic."""
    dispatch_count = {"n": 0}

    def gate_main(argv: list[str]) -> int:
        dispatch_count["n"] += 1
        print("VERDICT=single-reviewer-ok")
        return 0

    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: _fake_module(gate_main))

    directive = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=[{"sha_range_head": _FLOOR_SHA}],
        chain_tip_sha="HEAD",
        is_ancestor=lambda head, tip: True,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert directive["args"] == ["--session-id", "sid-1", f"{_FLOOR_SHA}..HEAD"]
    assert directive["already_satisfied"] is False

    ws_apply._execute_directives([directive], [], {}, repo_root=tmp_path)
    assert dispatch_count["n"] == 1

    directive_2 = build_review_brightline_gate_directive(
        "sid-1",
        trail_records=[{"sha_range_head": _FLOOR_SHA}],
        chain_tip_sha="HEAD",
        is_ancestor=lambda head, tip: True,
        session_start_sha="sss0000",
        repo_root=tmp_path,
    )
    assert directive_2["already_satisfied"] is False  # never memoized -- symbolic tip
    ws_apply._execute_directives([directive_2], [], {}, repo_root=tmp_path)
    assert dispatch_count["n"] == 2  # re-fired, not skipped


# ---------------------------------------------------------------------------
# C4 (AC7) — the write-trail directive opts into the same memo mechanism.
# `_LIVE_GATE_MEMO_DIRECTIVE_IDS` is a frozenset of exact strings and cannot
# match `d-write-trail-<index>` by membership; `record_gate_verdict_if_
# passed` additionally checks `_is_write_trail_directive_id`, a prefix
# test, alongside it.
# ---------------------------------------------------------------------------

_SINGLE_REVIEW = {
    "sha_range": f"{_FLOOR_SHA}..{_TIP_SHA}",
    "reviewer": "patrik",
    "scope": "chain",
    "verdict": "PASS",
    "diff_loc": 42,
}


def test_write_trail_directive_omits_session_id_and_repo_root_stays_byte_identical(tmp_path: Path) -> None:
    d1 = build_write_trail_directives(_SINGLE_REVIEW)
    d2 = build_write_trail_directives(_SINGLE_REVIEW)
    assert d1 == d2
    assert "_gate_memo_key_parts" not in d1[0]
    assert d1[0]["already_satisfied"] is False


def test_write_trail_directive_build_alone_never_writes_a_memo(tmp_path: Path) -> None:
    for _ in range(3):
        build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-1", repo_root=tmp_path)
    memo_dir = tmp_path / "state" / "ceremony" / "wsc-gate-verdict-memo"
    assert not memo_dir.exists() or list(memo_dir.iterdir()) == []


def test_write_trail_directive_build_sees_an_execution_time_hit(tmp_path: Path) -> None:
    record_gate_memo(tmp_path, "d-write-trail", "sid-1", _SINGLE_REVIEW["sha_range"])
    directive = build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-1", repo_root=tmp_path)[0]
    assert directive["already_satisfied"] is True


def test_write_trail_directive_hit_is_session_and_range_specific(tmp_path: Path) -> None:
    record_gate_memo(tmp_path, "d-write-trail", "sid-1", _SINGLE_REVIEW["sha_range"])
    other_session = build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-2", repo_root=tmp_path)[0]
    assert other_session["already_satisfied"] is False

    other_range = dict(_SINGLE_REVIEW, sha_range=f"{_TIP_SHA}..{_FLOOR_SHA}")
    other_range_directive = build_write_trail_directives(other_range, session_id="sid-1", repo_root=tmp_path)[0]
    assert other_range_directive["already_satisfied"] is False


def test_write_trail_directive_list_shape_indexed_ids_key_on_sha_range_not_index(tmp_path: Path) -> None:
    entry_a = dict(_SINGLE_REVIEW, sha_range=f"{_FLOOR_SHA}..{_TIP_SHA}")
    entry_b = dict(_SINGLE_REVIEW, sha_range=f"{_TIP_SHA}..{_FLOOR_SHA}")
    directives = build_write_trail_directives([entry_a, entry_b], session_id="sid-1", repo_root=tmp_path)
    assert [d["id"] for d in directives] == ["d-write-trail-0", "d-write-trail-1"]
    assert all(d["already_satisfied"] is False for d in directives)

    record_gate_memo(tmp_path, "d-write-trail", "sid-1", entry_b["sha_range"])
    directives_2 = build_write_trail_directives([entry_a, entry_b], session_id="sid-1", repo_root=tmp_path)
    assert directives_2[0]["already_satisfied"] is False  # entry_a's range untouched
    assert directives_2[1]["already_satisfied"] is True  # entry_b's range hit


def test_record_gate_verdict_records_write_trail_single_dict_shape_on_pass(tmp_path: Path) -> None:
    directive = build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-1", repo_root=tmp_path)[0]
    record_gate_verdict_if_passed(tmp_path, directive, 0, "")
    assert gate_memo_hit(tmp_path, "d-write-trail", "sid-1", _SINGLE_REVIEW["sha_range"]) is True


def test_record_gate_verdict_records_write_trail_indexed_shape_on_pass(tmp_path: Path) -> None:
    entry = dict(_SINGLE_REVIEW, sha_range=f"{_FLOOR_SHA}..{_TIP_SHA}")
    directive = build_write_trail_directives([entry], session_id="sid-1", repo_root=tmp_path)[0]
    assert directive["id"] == "d-write-trail-0"
    record_gate_verdict_if_passed(tmp_path, directive, 0, "")
    assert gate_memo_hit(tmp_path, "d-write-trail", "sid-1", entry["sha_range"]) is True


def test_record_gate_verdict_does_not_record_write_trail_on_nonzero_exit(tmp_path: Path) -> None:
    directive = build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-1", repo_root=tmp_path)[0]
    record_gate_verdict_if_passed(tmp_path, directive, 1, "")
    assert gate_memo_hit(tmp_path, "d-write-trail", "sid-1", _SINGLE_REVIEW["sha_range"]) is False


def test_record_gate_verdict_write_trail_without_key_parts_is_a_noop(tmp_path: Path) -> None:
    """A hand-built directive (e.g. an existing pre-C4 test) that never went
    through `build_write_trail_directives`'s opt-in carries no
    `_gate_memo_key_parts` — must not raise and must not write."""
    directive = {"id": "d-write-trail", "cli": "wsc-coverage-gate-runner", "args": ["write-trail"]}
    record_gate_verdict_if_passed(tmp_path, directive, 0, "")
    memo_dir = tmp_path / "state" / "ceremony" / "wsc-gate-verdict-memo"
    assert not memo_dir.exists() or list(memo_dir.iterdir()) == []


def test_execute_directives_write_trail_partial_failure_only_succeeded_entry_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composed case: two indexed `d-write-trail-<n>` directives with
    DIFFERENT `sha_range` values in one apply pass, one dispatching
    successfully and its sibling failing. A memo hit must never cause a
    trail write to be SKIPPED when the record does not exist — so the
    FAILED entry's sha_range must re-fire on a subsequent pass (no memo
    recorded for it) while the SUCCEEDED entry's sha_range short-circuits.
    Proves the partial-failure case cannot over-claim `already_satisfied`."""
    dispatch_count = {"n": 0}

    def write_trail_main(argv: list[str]) -> int:
        dispatch_count["n"] += 1
        # entry_a's range succeeds; entry_b's range fails.
        return 0 if entry_a["sha_range"] in argv else 1

    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: _fake_module(write_trail_main))

    entry_a = dict(_SINGLE_REVIEW, sha_range=f"{_FLOOR_SHA}..{_TIP_SHA}")
    entry_b = dict(_SINGLE_REVIEW, sha_range=f"{_TIP_SHA}..{_FLOOR_SHA}")

    directives = build_write_trail_directives([entry_a, entry_b], session_id="sid-1", repo_root=tmp_path)
    assert [d["id"] for d in directives] == ["d-write-trail-0", "d-write-trail-1"]
    assert all(d["already_satisfied"] is False for d in directives)

    _, report = ws_apply._execute_directives(directives, [], {}, repo_root=tmp_path)
    assert report["landed"] == ["d-write-trail-0"]
    assert [f["id"] for f in report["failed"]] == ["d-write-trail-1"]
    assert dispatch_count["n"] == 2

    # Only entry_a's range got a memo -- entry_b's failed dispatch must not
    # have written one.
    assert gate_memo_hit(tmp_path, "d-write-trail", "sid-1", entry_a["sha_range"]) is True
    assert gate_memo_hit(tmp_path, "d-write-trail", "sid-1", entry_b["sha_range"]) is False

    # Subsequent pass: the succeeded range short-circuits, the failed range
    # re-fires -- no over-claimed `already_satisfied` for the sibling that
    # never actually got a review-trail record written.
    directives_2 = build_write_trail_directives([entry_a, entry_b], session_id="sid-1", repo_root=tmp_path)
    assert directives_2[0]["already_satisfied"] is True
    assert directives_2[1]["already_satisfied"] is False


def test_execute_directives_write_trail_skip_on_second_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch_count = {"n": 0}

    def write_trail_main(argv: list[str]) -> int:
        dispatch_count["n"] += 1
        return 0

    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda cli_name: _fake_module(write_trail_main))

    directive = build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-1", repo_root=tmp_path)[0]
    assert directive["already_satisfied"] is False
    report = ws_apply._execute_directives([directive], [], {}, repo_root=tmp_path)[1]
    assert report["landed"] == [directive["id"]]
    assert dispatch_count["n"] == 1

    directive_2 = build_write_trail_directives(_SINGLE_REVIEW, session_id="sid-1", repo_root=tmp_path)[0]
    assert directive_2["already_satisfied"] is True
    report_2 = ws_apply._execute_directives([directive_2], [], {}, repo_root=tmp_path)[1]
    assert report_2["landed"] == [directive_2["id"]]
    assert dispatch_count["n"] == 1  # not dispatched again
