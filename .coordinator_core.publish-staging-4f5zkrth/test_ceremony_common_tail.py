"""
coordinator_core.test_ceremony_common_tail — co-located pytest for
coordinator_core.ceremony_common.tail's shared close-tail builder, and the
regression net for the workday_complete/workweek_complete brief() consumers
that replaced their hand-maintained tail copies with it.

Covers, per assembler (`workday_complete.brief`, `workweek_complete.brief`):
  - brief() calls resolve_operator_config() and returns a directives[] list
    (never raises) with the ceremony-close directive present, at the
    expected step id, with the correct cli/args/depends_on shape.
  - the tail directive's `cli` value is a member of that module's own
    CONSUMES_MANIFEST (AC15c: no phantom verbs — the shared builder must not
    let either consumer drift off its manifest).
  - the hard_block invariant the refactor could silently break: workweek's
    tail directive carries `hard_block: False` (workweek's own uniform
    post-build pass stamps `hard_block` onto every directive it builds,
    tail included), while workday's carries NO `hard_block` key at all
    (workday never had that concept — the shared tail builder itself does
    not know about `hard_block`, per its own negative-spec).

The emission-cadence half of this tail was removed with the emission
artifact itself (2026-08-22 CUT); what remains is the post-command hook.

Spec backlink: DoE-claude DoE-claude:pln-b1-ceremony-complete-computed--9ffa54,
chunk C5, AC9

Run: cd /Users/oduffy/X/project-makima && python3 -m pytest coordinator_core/test_ceremony_common_tail.py -q
"""
from __future__ import annotations

from typing import Any

import coordinator_core.workday_complete.brief as workday_brief
import coordinator_core.workweek_complete.brief as workweek_brief
from coordinator_core.ceremony_common.tail import build_ceremony_close_tail
import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _find(directives: list[dict[str, Any]], directive_id: str) -> dict[str, Any]:
    for entry in directives:
        if entry["id"] == directive_id:
            return entry
    raise AssertionError(f"directive {directive_id!r} not found in {directives!r}")


def test_build_ceremony_close_tail_shape():
    tail = build_ceremony_close_tail(
        post_command_hook_id="d_step_x_post_command_hook",
        ceremony_name="workday-complete",
    )
    assert [entry["id"] for entry in tail] == ["d_step_x_post_command_hook"]
    (hook,) = tail
    assert hook["cli"] == "coordinator-ceremony-hook"
    # 2026-07-26 arg-mismatch audit, class (c): the hook reads its one
    # positional (argv[0]) as the ceremony name — `args=[]` always resolved
    # an empty ceremony and silently no-opped regardless of caller.
    assert hook["args"] == ["workday-complete"]
    assert hook["depends_on"] is None
    assert hook["already_satisfied"] is False
    assert "hard_block" not in hook

    # The post-command-hook step is NOT best-effort — it never carried the
    # key. The one entry that did was the emission-cadence directive, gone
    # with the artifact (2026-08-22 CUT).
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

    # workday's assembler never stamps hard_block onto anything — the
    # invariant a silent regression in the refactor could break.
    assert "hard_block" not in hook


def test_workweek_complete_tail_directives():
    directives = workweek_brief._build_directives()

    hook = _find(directives, "d_step13_5_post_command_hook")
    assert hook["cli"] == "coordinator-ceremony-hook"
    assert hook["args"] == ["workweek-complete"]
    assert hook["depends_on"] is None
    assert hook["cli"] in workweek_brief.CONSUMES_MANIFEST

    # workweek's uniform post-build pass stamps hard_block onto EVERY
    # directive it builds, tail included — the tail directive is not a
    # hard-block gate, so it must read False, never missing.
    assert hook["hard_block"] is False



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


def test_workweek_brief_envelope_contains_tail(monkeypatch):
    monkeypatch.setattr(workweek_brief, "resolve_operator_config", lambda **_: {})
    exit_code, envelope = workweek_brief.brief()
    assert exit_code == int(workweek_brief.WorkweekExitCode.SUCCESS)
    directive_ids = [entry["id"] for entry in envelope["directives"]]
    assert "d_step13_5_post_command_hook" in directive_ids
