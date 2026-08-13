"""
coordinator_core.test_ceremony_common_tail — co-located pytest for
coordinator_core.ceremony_common.tail's shared close-tail builder, and the
regression net for the workday_complete/workweek_complete brief() consumers
that replaced their hand-maintained tail copies with it.

Covers, per assembler (`workday_complete.brief`, `workweek_complete.brief`):
  - brief() calls resolve_operator_config() and returns a directives[] list
    (never raises) with the two ceremony-close directives present, at the
    expected step ids, with the correct cli/args/depends_on shape.
  - both tail directives' `cli` values are members of that module's own
    CONSUMES_MANIFEST (AC15c: no phantom verbs — the shared builder must not
    let either consumer drift off its manifest).
  - the hard_block invariant the refactor could silently break: workweek's
    tail directives carry `hard_block: False` (workweek's own uniform
    post-build pass stamps `hard_block` onto every directive it builds,
    tail included), while workday's tail directives carry NO `hard_block`
    key at all (workday never had that concept — the shared tail builder
    itself does not know about `hard_block`, per its own negative-spec).

Spec backlink: coordinator-claude docs/plans/2026-07-24-b1-ceremony-complete-computed-conversion.md,
chunk C5, AC9

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_ceremony_common_tail.py -q
"""
from __future__ import annotations

from typing import Any

import coordinator_core.workday_complete.brief as workday_brief
import coordinator_core.workweek_complete.brief as workweek_brief
from coordinator_core.ceremony_common.tail import build_ceremony_close_tail
from coordinator_core.workstream_complete.directives_commit_tail import (
    build_emit_cadence_directive,
)


def _find(directives: list[dict[str, Any]], directive_id: str) -> dict[str, Any]:
    for entry in directives:
        if entry["id"] == directive_id:
            return entry
    raise AssertionError(f"directive {directive_id!r} not found in {directives!r}")


def test_build_ceremony_close_tail_shape():
    tail = build_ceremony_close_tail(
        post_command_hook_id="d_step_x_post_command_hook",
        emit_cadence_id="d_step_x_emit_cadence",
        ceremony_name="workday-complete",
    )
    assert [entry["id"] for entry in tail] == [
        "d_step_x_post_command_hook",
        "d_step_x_emit_cadence",
    ]
    hook, cadence = tail
    assert hook["cli"] == "coordinator-ceremony-hook"
    # 2026-07-26 arg-mismatch audit, class (c): the hook reads its one
    # positional (argv[0]) as the ceremony name — `args=[]` always resolved
    # an empty ceremony and silently no-opped regardless of caller.
    assert hook["args"] == ["workday-complete"]
    assert hook["depends_on"] is None
    assert hook["already_satisfied"] is False
    assert "hard_block" not in hook

    assert cadence["cli"] == "emit-cadence"
    assert cadence["args"] == []
    assert cadence["depends_on"] is None
    assert cadence["already_satisfied"] is False
    assert "hard_block" not in cadence

    # AC8: the emit-cadence entry declares itself best-effort so a
    # non-zero exit lands in the ceremony runner's `degraded` bucket
    # rather than `failed` — the post-command-hook entry does NOT get
    # the key, since that step is not documented as best-effort.
    assert cadence["best_effort"] is True
    assert "best_effort" not in hook


def test_workday_complete_tail_directives():
    directives = workday_brief._build_directives(
        {}, {"today": [], "stale": []}, {"ambiguous": False}
    )

    hook = _find(directives, "d_step10_5_post_command_hook")
    assert hook["cli"] == "coordinator-ceremony-hook"
    assert hook["args"] == ["workday-complete"]
    assert hook["depends_on"] is None
    assert hook["cli"] in workday_brief.CONSUMES_MANIFEST

    cadence = _find(directives, "d_step10_6_emit_cadence")
    assert cadence["cli"] == "emit-cadence"
    assert cadence["args"] == []
    assert cadence["depends_on"] is None
    assert cadence["cli"] in workday_brief.CONSUMES_MANIFEST

    # workday's assembler never stamps hard_block onto anything — the
    # invariant a silent regression in the refactor could break.
    assert "hard_block" not in hook
    assert "hard_block" not in cadence


def test_workweek_complete_tail_directives():
    directives = workweek_brief._build_directives()

    hook = _find(directives, "d_step13_5_post_command_hook")
    assert hook["cli"] == "coordinator-ceremony-hook"
    assert hook["args"] == ["workweek-complete"]
    assert hook["depends_on"] is None
    assert hook["cli"] in workweek_brief.CONSUMES_MANIFEST

    cadence = _find(directives, "d_step13_6_emit_cadence")
    assert cadence["cli"] == "emit-cadence"
    assert cadence["args"] == []
    assert cadence["depends_on"] is None
    assert cadence["cli"] in workweek_brief.CONSUMES_MANIFEST

    # workweek's uniform post-build pass stamps hard_block onto EVERY
    # directive it builds, tail included — neither tail directive is a
    # hard-block gate, so both must read False, never missing.
    assert hook["hard_block"] is False
    assert cadence["hard_block"] is False


def test_workstream_complete_emit_cadence_directive_is_best_effort():
    # AC8: workstream_complete takes only build_ceremony_close_tail's
    # emit-cadence element (it has no post-command-hook step of its own)
    # and must inherit best_effort: True from that shared factor rather
    # than losing it across the re-point of depends_on/args.
    cadence = build_emit_cadence_directive()
    assert cadence["cli"] == "emit-cadence"
    assert cadence["best_effort"] is True


def test_workday_brief_envelope_contains_tail(monkeypatch):
    # Suite-root autouse fixture quarantines HOME/USERPROFILE per test (see
    # coordinator_core/conftest.py); resolve_operator_config() legitimately
    # fails against that quarantine. Stub it so brief() proceeds — mirrors
    # test_baton_assemble.py's own resolve_operator_config spy pattern.
    monkeypatch.setattr(workday_brief, "resolve_operator_config", lambda **_: {})
    exit_code, envelope = workday_brief.brief()
    assert exit_code == int(workday_brief.WorkdayExitCode.SUCCESS)
    directive_ids = [entry["id"] for entry in envelope["directives"]]
    assert "d_step10_5_post_command_hook" in directive_ids
    assert "d_step10_6_emit_cadence" in directive_ids


def test_workweek_brief_envelope_contains_tail(monkeypatch):
    monkeypatch.setattr(workweek_brief, "resolve_operator_config", lambda **_: {})
    exit_code, envelope = workweek_brief.brief()
    assert exit_code == int(workweek_brief.WorkweekExitCode.SUCCESS)
    directive_ids = [entry["id"] for entry in envelope["directives"]]
    assert "d_step13_5_post_command_hook" in directive_ids
    assert "d_step13_6_emit_cadence" in directive_ids
