"""guard-doctrine-surface-bash-write names the Edit route for every single non-derived governed target outside the CLAUDE.md class."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as doctrine
from coordinator_core.guard_messages.alternative_record import (
    AdaptableOffer,
    Audience,
)
from coordinator_core.guard_messages.render import render
from coordinator_core.write_guards.tests.test_guard_doctrine_surface_edits_gem_doctrine import (  # noqa: F401
    box,
)
from coordinator_core.write_guards import guard_doctrine_surface_edits as edits

_PREFIX = "BLOCKED: writes a governed doctrine surface."
_NO_TARGET = "Name one non-derived target file and use the Edit tool on it."


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)
    monkeypatch.setattr(doctrine, "apply_guard_level", lambda name, envelope, **kw: envelope)
    monkeypatch.setattr(machine_profile, "out_of_repo_write_blocked", lambda: False)


_SURFACE = "coordinator/snippets/em-operating-doctrine.md"


def _run(box, command, surfaces=(_SURFACE,)):
    payload = box.payload(_SURFACE, tool="Bash")
    payload["tool_input"] = {"command": command}
    record = doctrine.check(payload, list(surfaces))
    assert record is not None
    reason = render(record, Audience.EM)["hookSpecificOutput"]["permissionDecisionReason"]
    return payload, record, reason


def _offers_edit(record):
    return any(isinstance(m, AdaptableOffer) for m in record.members)


def test_live_gem_is_told_to_edit_the_file_and_edit_is_admitted(box):
    target = (box.doe / _SURFACE).as_posix()
    payload, record, reason = _run(box, f"cat > {target}")
    assert reason.startswith(_PREFIX)
    assert f"Edit {target}" in reason
    assert _offers_edit(record)
    assert edits.edit_admitted(payload, target) is True


def test_non_gem_is_told_to_edit_the_file_with_no_human_route(box):
    box.state = "vacant"
    target = (box.doe / _SURFACE).as_posix()
    payload, record, reason = _run(box, f"cat > {target}")
    assert reason.startswith(_PREFIX)
    assert f"Edit {target}" in reason
    assert "sentinel" not in reason
    assert "PM" not in reason
    assert _offers_edit(record)
    assert edits.edit_admitted(payload, target) is True


def test_class_write_to_the_derived_copy_is_denied_with_no_edit_offer(box):
    target = (box.cfg / "CLAUDE.md").as_posix()
    _, record, reason = _run(box, f"cat > {target}", surfaces=("CLAUDE.md",))
    assert record.verdict_kind == "deny"
    assert ".coordinator-doctrine-edit-approved" in reason
    assert not _offers_edit(record)


def test_unresolvable_interpreter_payload_gets_the_name_one_target_prose(box):
    _, record, reason = _run(box, f"python3 -c \"import os; open(os.environ['T'], 'w').write('x')\" {_SURFACE}")
    assert reason.startswith(_PREFIX)
    assert _NO_TARGET in reason
    assert not _offers_edit(record)


def test_two_targets_get_the_name_one_target_prose(box):
    a = (box.doe / _SURFACE).as_posix()
    b = (box.repo / _SURFACE).as_posix()
    _, _, reason = _run(box, f"cat > {a}; cat > {b}")
    assert _NO_TARGET in reason


def test_verdict_stays_deny_for_the_admitted_caller(box):
    target = (box.doe / _SURFACE).as_posix()
    _, record, _ = _run(box, f"cat > {target}")
    assert record.verdict_kind == "deny"


def test_unpatched_policy_point_keeps_the_class_floor_and_demotes_the_rest(box, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)
    monkeypatch.setattr(doctrine, "sentinel_present", lambda cwd: False)
    cls = box.payload("global-doctrine/CLAUDE.md", tool="Bash")
    cls["tool_input"] = {"command": f"cat > {(box.doe / 'global-doctrine' / 'CLAUDE.md').as_posix()}"}
    assert doctrine.check(cls, ["CLAUDE.md"]).verdict_kind == "deny"
    other = box.payload(_SURFACE, tool="Bash")
    other["tool_input"] = {"command": f"cat > {(box.doe / _SURFACE).as_posix()}"}
    result = doctrine.check(other, [_SURFACE])
    assert isinstance(result, dict)
    assert result["hookSpecificOutput"].get("permissionDecision") != "deny"
