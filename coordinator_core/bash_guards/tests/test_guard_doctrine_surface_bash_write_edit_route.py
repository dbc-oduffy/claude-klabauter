"""guard-doctrine-surface-bash-write names an Edit route only when Edit is admitted for the caller."""

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


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setattr(doctrine, "feature_enabled", lambda name: True)
    monkeypatch.setattr(doctrine, "_is_onboarding_write", lambda *a, **k: False)
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "strict")
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


def test_non_gem_gets_the_no_route_prose_and_no_edit_offer(box):
    box.state = "vacant"
    target = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    _, record, reason = _run(box, f"cat > {target}")
    assert reason.startswith(_PREFIX)
    assert "No Edit route is open" in reason
    assert "approval sentinel" in reason
    assert "Edit " + target not in reason
    assert not _offers_edit(record)


def test_live_gem_is_never_offered_the_derived_copy(box):
    target = (box.cfg / "CLAUDE.md").as_posix()
    _, record, reason = _run(box, f"cat > {target}")
    assert "No Edit route is open" in reason
    assert not _offers_edit(record)


def test_unresolvable_interpreter_payload_gets_the_no_route_prose(box):
    _, record, reason = _run(box, "python3 -c \"import os; open(os.environ['T'], 'w').write('x')\" CLAUDE.md")
    assert reason.startswith(_PREFIX)
    assert "No Edit route is open" in reason
    assert not _offers_edit(record)


def test_two_targets_get_the_no_route_prose(box):
    a = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    b = (box.repo / "CLAUDE.md").as_posix()
    _, _, reason = _run(box, f"cat > {a}; cat > {b}")
    assert "No Edit route is open" in reason


def test_verdict_stays_deny_for_the_admitted_caller(box):
    target = (box.doe / "global-doctrine" / "CLAUDE.md").as_posix()
    _, record, _ = _run(box, f"cat > {target}")
    assert record.verdict_kind == "deny"
