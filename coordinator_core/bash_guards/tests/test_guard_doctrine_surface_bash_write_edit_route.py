"""guard-doctrine-surface-bash-write names the Edit route for every single non-derived target."""

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


def _run(box, command):
    payload = box.payload("global-doctrine/CLAUDE.md", tool="Bash")
    payload["tool_input"] = {"command": command}
    record = doctrine.check(payload, ["CLAUDE.md"])
    assert record is not None
    reason = render(record, Audience.EM)["hookSpecificOutput"]["permissionDecisionReason"]
    return payload, record, reason


def _offers_edit(record):
    return any(isinstance(m, AdaptableOffer) for m in record.members)


def test_live_gem_is_told_to_edit_the_file_and_edit_is_admitted(box):
    target = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    payload, record, reason = _run(box, f"cat > {target}")
    assert reason.startswith(_PREFIX)
    assert f"Edit {target}" in reason
    assert _offers_edit(record)
    assert edits.edit_admitted(payload, target) is True


def test_non_gem_is_told_to_edit_the_file_with_no_human_route(box):
    box.state = "vacant"
    target = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    payload, record, reason = _run(box, f"cat > {target}")
    assert reason.startswith(_PREFIX)
    assert f"Edit {target}" in reason
    assert "sentinel" not in reason
    assert "PM" not in reason
    assert _offers_edit(record)
    assert edits.edit_admitted(payload, target) is True


def test_live_gem_is_never_offered_the_derived_copy(box):
    target = (box.cfg / "CLAUDE.md").as_posix()
    _, record, reason = _run(box, f"cat > {target}")
    assert _NO_TARGET in reason
    assert not _offers_edit(record)


def test_unresolvable_interpreter_payload_gets_the_name_one_target_prose(box):
    _, record, reason = _run(box, "python3 -c \"import os; open(os.environ['T'], 'w').write('x')\" CLAUDE.md")
    assert reason.startswith(_PREFIX)
    assert _NO_TARGET in reason
    assert not _offers_edit(record)


def test_two_targets_get_the_name_one_target_prose(box):
    a = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    b = (box.repo / "CLAUDE.md").as_posix()
    _, _, reason = _run(box, f"cat > {a}; cat > {b}")
    assert _NO_TARGET in reason


def test_verdict_stays_deny_for_the_admitted_caller(box):
    target = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    _, record, _ = _run(box, f"cat > {target}")
    assert record.verdict_kind == "deny"


def test_unpatched_policy_point_demotes_the_deny_to_advisory(box, monkeypatch):
    # guard-doctrine-surface-bash-write is ADVISORY_BY_RULING (DR-475, 307ea22403; classified b17ec35251).
    monkeypatch.undo()
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)
    target = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    payload = box.payload("global-doctrine/CLAUDE.md", tool="Bash")
    payload["tool_input"] = {"command": f"cat > {target}"}
    result = doctrine.check(payload, ["CLAUDE.md"])
    assert isinstance(result, dict)
    assert result["hookSpecificOutput"].get("permissionDecision") != "deny"
