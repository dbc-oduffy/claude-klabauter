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
from coordinator_core.workstream_complete.directives_review import (
    build_review_brightline_gate_directive,
    gate_memo_hit,
    record_gate_memo,
    record_gate_verdict_if_passed,
)


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


def _brightline_directive(args: list[str]) -> dict[str, Any]:
    return {"id": "d-run-review-brightline-gate", "cli": "review-brightline-gate", "args": args}


_FLOOR_SHA = "a1" * 20
_TIP_SHA = "b2" * 20


def test_record_gate_verdict_records_brightline_on_resolved_range_pass(tmp_path: Path) -> None:
    directive = _brightline_directive(["--session-id", "sid-1", f"{_FLOOR_SHA}..{_TIP_SHA}"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, f"range={_FLOOR_SHA}..{_TIP_SHA} VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is True


def test_record_gate_verdict_never_records_brightline_symbolic_default_shape(tmp_path: Path) -> None:
    directive = _brightline_directive(["--session-id", "sid-1"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, "VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is False


def test_record_gate_verdict_never_records_brightline_when_tip_is_still_symbolic(tmp_path: Path) -> None:
    directive = _brightline_directive(["--session-id", "sid-1", f"{_FLOOR_SHA}..HEAD"])
    record_gate_verdict_if_passed(tmp_path, directive, 0, "VERDICT=single-reviewer-ok")
    assert gate_memo_hit(tmp_path, directive["id"], *directive["args"]) is False


def test_record_gate_verdict_never_records_brightline_when_floor_is_still_symbolic(tmp_path: Path) -> None:
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


def _fake_module(main_fn: Callable[..., Any]) -> ModuleType:
    mod = ModuleType("fake_cli")
    mod.main = main_fn
    return mod


def test_execute_directives_unchanged_inputs_after_pass_skip_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert dispatch_count["n"] == 1


def test_execute_directives_symbolic_tip_re_dispatches_every_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert directive_2["already_satisfied"] is False
    ws_apply._execute_directives([directive_2], [], {}, repo_root=tmp_path)
    assert dispatch_count["n"] == 2


# C4 (AC7)'s `_SINGLE_REVIEW`/`build_write_trail_directives` gate-memo tests
