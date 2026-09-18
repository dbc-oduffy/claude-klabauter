"""The tool-surface out-of-repo write bump (C7) stands down where its premise
is false.

WHY THIS FILE EXISTS. The environment stand-down was authored in C4
(`bash_guards/bump_foreign_repo_write.py`) and never ported. On a managed
remote container -- one session, a closed set of repos granted to it
together, no peer team, no memo reader -- C4 therefore ALLOWED a cross-repo
`git commit` while this guard HARD-DENIED the identical `Edit`. This surface
is the one a well-meaning agent reaches for first and `CLASS = "hard-deny"`
makes its refusal the least passable of the three, so the asymmetry landed
its whole cost here. That asymmetry, not the stand-down, was the defect.

WHAT MUST NOT DRIFT:

  1. `fleet_present` false -> `check()` returns `None`, prints an
     after-the-fact advisory, and leaves a durable audit line under the
     SESSION's own repo, never the target's. `None`, not an envelope: the
     engine's hard-deny phase takes the first non-`None` verdict, so any
     value here claims the slot and skips every guard after this one.
  2. `fleet_present` true -> the hard deny is exactly what it was. A
     stand-down that leaks onto a fleet workstation is a deleted guard
     wearing a warning's clothes.
  3. The capability layer raising -> deny, as before the stand-down existed.
  4. `CLASS` stays `"hard-deny"`. The stand-down is an environment-
     conditional decline at ONE call site, not a class change -- the guard's
     own docstring forbids reverting `CLASS` to `"advisory"`, and this fix
     does not. `test_guard_classification.py` carries that ruling; this file
     asserts the two coexist.

A REAL SESSION-START ANCHOR RECORD IS LOAD-BEARING HERE. Every non-fail-open
test below writes one (`write_session_start_record`), because without it
`resolve_launch_anchor` returns `None` and `check()` allows for a reason that
has nothing to do with the stand-down -- a green that proves nothing. See the
guard's own docstring, "VERIFYING THIS GUARD BY HAND", and the
`test_em_repro_payload_*` pair in this package's main suite.

The package `conftest.py` pins `fleet_present` on for every test here; the
`cloud_container` fixture below overrides that pin, and runs after it.
"""

from __future__ import annotations

import pytest

from coordinator_core import environment
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.write_guards import bump_out_of_repo_tool_write as guard
from coordinator_core.write_guards.tests.test_bump_out_of_repo_tool_write import (
    _init_repo,
    _isolate_home,
    _payload,
)

# `_init_repo` builds real git repos; declared, not inherited silently.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture()
def cloud_container(monkeypatch):
    """A managed remote container: the package conftest's capability pin is
    removed and the venue markers a real one sets are put back."""
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")


@pytest.fixture()
def exploding_capability(monkeypatch):
    """The capability layer itself unhappy -- the fail-open leg. Patched at
    `coordinator_core.environment.capability`, the attribute
    `_write_bump_stand_down` imports inside its own function body, so the
    raise lands where the guard actually consults it."""

    def boom(*args, **kwargs):
        raise RuntimeError("capability layer unavailable")

    monkeypatch.setattr(environment, "capability", boom)


def _anchored_foreign_edit(tmp_path, monkeypatch, session_id: str):
    """A payload that this guard, absent any stand-down, denies: an `Edit`
    into a real foreign git repo from a session with a real anchor record in
    its own real repo. Returns `(payload, own_repo)`."""
    own = _init_repo(tmp_path, "own-repo-%s" % session_id)
    foreign = _init_repo(tmp_path, "foreign-repo-%s" % session_id)
    _isolate_home(monkeypatch, tmp_path / ("claude-home-%s" % session_id))
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    payload = _payload("Edit", str(foreign / "coordinator.local.md"), session_id, str(own))
    return payload, own


def test_stands_down_on_a_host_with_no_fleet(tmp_path, monkeypatch, capsys):
    """Property 1. The hard deny becomes an after-the-fact advisory, and the
    `Edit` proceeds -- matching what the Bash surface already did for the
    equivalent `git commit`."""
    payload, _own = _anchored_foreign_edit(tmp_path, monkeypatch, "sess-c7-sd-cloud")
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")

    result = guard.check(payload)

    assert result is None, (
        "a stood-down bump must return None -- the engine's hard-deny phase "
        "takes the first non-None verdict, so any value here skips every "
        "later guard"
    )
    assert "out-of-repo-tool-write" in capsys.readouterr().err


def test_stand_down_leaves_a_durable_audit_line_in_the_session_repo(tmp_path, monkeypatch):
    """Property 1's other half, plus WHERE. Without the line, "permissive and
    warn" degrades to "permissive"; and the line must land in the session's
    own repo, never in the repo the bump was steering the write away from."""
    payload, own = _anchored_foreign_edit(tmp_path, monkeypatch, "sess-c7-sd-audit")
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")

    assert guard.check(payload) is None

    logs = list(own.rglob("overrides.log"))
    assert logs, "the stand-down left no trace in the session's own repo"
    body = "\n".join(p.read_text(encoding="utf-8") for p in logs)
    assert "STAND-DOWN-OUT-OF-REPO-TOOL-WRITE" in body, (
        "the audit token is per-surface on purpose -- three guards standing "
        "down must leave three distinguishable records"
    )


def test_still_hard_denies_where_the_premise_holds(tmp_path, monkeypatch):
    """Property 2, resting on the package pin rather than restating it."""
    payload, _own = _anchored_foreign_edit(tmp_path, monkeypatch, "sess-c7-sd-fleet")

    result = guard.check(payload)

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_when_the_capability_layer_raises(
    tmp_path, monkeypatch, cloud_container, exploding_capability
):
    """Property 3. The venue markers say "stand down" and the capability
    layer cannot answer -- the guard behaves as it did before the stand-down
    existed rather than guessing."""
    payload, _own = _anchored_foreign_edit(tmp_path, monkeypatch, "sess-c7-sd-broken-cap")

    result = guard.check(payload)

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_stand_down_did_not_soften_the_class(cloud_container):
    """Property 4. The decline is environment-conditional and lives at one
    call site; reverting `CLASS` would soften this guard on every host,
    including the fleet workstations where its premise holds."""
    assert guard.CLASS == "hard-deny"
    assert guard.MATCHERS == ["Write", "Edit", "MultiEdit", "NotebookEdit"]
    assert guard.PRIORITY == 135
