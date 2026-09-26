"""Regression cover for state/bug-backlog/2026-08-19-doctrine-surface-guard-
on-coordinator-lo-9720031728bf.yaml.

THE GAP THIS PINS. `guard_doctrine_surface_edits.py` (the Write/Edit/
MultiEdit admission gate) protects `coordinator.local.md` unconditionally --
it is this repo's CLASS-2 privileged-configuration surface (the ceremony-
executed test-command strings and the Tier-U authority declarations that
discharge them). `guard-doctrine-surface-bash-write`, the Bash/PowerShell
mirror registered in `dispatch.py`, only ever denied on the identifier set
DoE's own `governed-authoring-surfaces.json` manifest resolves --
CLASS-1 always-loaded doctrine, never `coordinator.local.md`. A Bash-
mediated write (a shell redirect, or a `python3 -c` payload) therefore
reached no guard at all: an `Edit` of `coordinator.local.md` was BLOCKED,
the identical content written through Bash landed silently.

`dispatch._with_local_config_surface` closes this by composing this repo's
own CLASS-2 identifier onto whatever the DoE manifest resolves, UNCON-
DITIONALLY -- see that function's own docstring for why this lives in the
caller rather than inside `resolve_governed_authoring_surfaces` itself
(whose own return-value contract `test_governed_surfaces_manifest_miss.py`
pins unchanged).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards import dispatch


class TestWithLocalConfigSurface:

    def test_appends_when_absent(self) -> None:
        assert dispatch._with_local_config_surface(["CLAUDE.md"]) == [
            "CLAUDE.md",
            "coordinator.local.md",
        ]

    def test_idempotent_when_already_present(self) -> None:
        surfaces = ["CLAUDE.md", "coordinator.local.md"]
        assert dispatch._with_local_config_surface(surfaces) == surfaces

    def test_appends_on_manifest_miss(self) -> None:
        assert dispatch._with_local_config_surface(None) == ["coordinator.local.md"]

    def test_appends_on_explicit_empty_manifest(self) -> None:
        assert dispatch._with_local_config_surface([]) == ["coordinator.local.md"]


def _fire_registered_doctrine_surface_bash_write(cmd: str) -> Optional[Dict[str, Any]]:
    """Invoke the REGISTERED `guard-doctrine-surface-bash-write` entry's own
    closure, exactly as `evaluate_payload_json` would -- not the bare
    predicate with a hand-assembled identifier list, so this exercises the
    real wiring `_doctrine_surface_bash_write_entry` composes in `dispatch.py`.

    `cwd="/tmp"` (outside this repo, no `.git` ancestor) so
    `guard_advisory_counter.record_advisory_fire`'s own `resolve_git_root_
    cheap` miss degrades every advisory-bookkeeping call here to a silent
    no-op -- this test writes nothing to disk."""
    payload: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "local-config-surface-bash-write-probe",
    }
    chain: List[dispatch.GuardEntry] = dispatch._build_guard_chain(
        cmd=cmd,
        session_id="local-config-surface-bash-write-probe",
        cwd="/tmp",
        payload=payload,
        policy_file=None,
        host_is_windows=None,
    )
    matches = [entry for entry in chain if entry.name == "guard-doctrine-surface-bash-write"]
    assert len(matches) == 1, "guard-doctrine-surface-bash-write missing from the registered chain"
    return matches[0].fn()


class TestBashMediatedLocalConfigWriteIsDenied:
    def test_python_via_bash_write_is_denied(self) -> None:
        result = _fire_registered_doctrine_surface_bash_write(
            "python3 -c \"open('coordinator.local.md', 'a').write('x')\""
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_shell_redirect_write_is_denied(self) -> None:
        result = _fire_registered_doctrine_surface_bash_write(
            "echo corrupted > coordinator.local.md"
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_read_only_command_still_allows(self) -> None:
        assert _fire_registered_doctrine_surface_bash_write("cat coordinator.local.md") is None
