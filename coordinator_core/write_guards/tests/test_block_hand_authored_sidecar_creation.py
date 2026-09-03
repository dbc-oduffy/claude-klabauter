"""Behavioral tests for
coordinator_core.write_guards.block_hand_authored_sidecar_creation -- the
hand-authored run-report sidecar creation hard-deny guard (see the module's
own docstring for the incident this closes:
state/handoffs/2026-08-16-sidecar-provisioning-is-the-engines-job.md).

Driven through `guard.check()`, the guard's real operator entrypoint, exactly
as `write_guards/engine.py` invokes it -- not a bare call on an internal
helper.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import block_hand_authored_sidecar_creation as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_HAND_SIDECAR_WRITE"

_HAND_AUTHORED_NO_FRONTMATTER = """## Findings

- Applied: F1, F2
"""

_HAND_AUTHORED_MISSING_AGENT_TYPE = """---
status: open
divergence:
  diverged: false
commits: []
---

## Run notes
"""

_PROVISIONED_SHAPE = """---
status: open
agent_type: coordinator:review-integrator
spawned_at: 2026-08-16T00:00:00+00:00
lead_session_id: abc123
divergence:
  diverged: false
commits: []
---

## Findings
"""


def _payload(
    tmp_path, *, filename="2026-08-16-repaired-sidecar.md", content,
    existing=False, root="state",
):
    sidecar_dir = tmp_path / root / "subagent-share" / "sess-1"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    target = sidecar_dir / filename
    if existing:
        target.write_text(_PROVISIONED_SHAPE, encoding="utf-8")
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": content},
        "cwd": str(tmp_path),
    }


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


def test_denies_new_sidecar_with_no_frontmatter(tmp_path):
    payload = _payload(tmp_path, content=_HAND_AUTHORED_NO_FRONTMATTER)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "agent_type" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_denies_new_sidecar_missing_agent_type(tmp_path):
    payload = _payload(tmp_path, content=_HAND_AUTHORED_MISSING_AGENT_TYPE)
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allows_new_sidecar_carrying_real_agent_type(tmp_path):
    payload = _payload(tmp_path, content=_PROVISIONED_SHAPE)
    assert guard.check(payload) is None


def test_allows_edit_of_existing_sidecar_missing_agent_type(tmp_path):
    # Not matched at all -- MATCHERS is Write-only, and this simulates an
    # in-flight body edit to an already-existing (however malformed) file.
    payload = _payload(tmp_path, content=_HAND_AUTHORED_MISSING_AGENT_TYPE, existing=True)
    payload["tool_name"] = "Edit"
    payload["tool_input"] = {
        "file_path": payload["tool_input"]["file_path"],
        "old_string": "## Run notes",
        "new_string": "## Run notes\n\nDid the thing.",
    }
    assert guard.check(payload) is None


def test_allows_overwrite_of_already_existing_file(tmp_path):
    # New-file gate: this path already has a file on disk, so this Write is
    # not the CREATION event -- out of this guard's scope.
    payload = _payload(tmp_path, content=_HAND_AUTHORED_MISSING_AGENT_TYPE, existing=True)
    assert guard.check(payload) is None


def test_allows_path_outside_subagent_share(tmp_path):
    other = tmp_path / "docs" / "plans" / "some-plan.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(other), "content": _HAND_AUTHORED_MISSING_AGENT_TYPE},
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_override_env_bypasses_deny(tmp_path, monkeypatch):
    monkeypatch.setenv(_OVERRIDE_ENV, "1")
    payload = _payload(tmp_path, content=_HAND_AUTHORED_MISSING_AGENT_TYPE)
    assert guard.check(payload) is None


def test_non_write_tool_not_matched(tmp_path):
    payload = _payload(tmp_path, content=_HAND_AUTHORED_MISSING_AGENT_TYPE)
    payload["tool_name"] = "Read"
    assert guard.check(payload) is None


def test_allows_overwrite_when_file_path_relative_to_payload_cwd(tmp_path):
    # Review: reviewer -- os.path.exists(file_path) must resolve a relative
    # Write file_path against payload['cwd'] (mirrors
    # block_fleet_delegation_write._resolve_candidate), not the guard
    # process's own cwd, or a legitimate overwrite of an already-provisioned
    # sidecar is wrongly denied as a CREATE.
    sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-1"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    target = sidecar_dir / "2026-08-16-repaired-sidecar.md"
    target.write_text(_PROVISIONED_SHAPE, encoding="utf-8")

    relative_file_path = "state/subagent-share/sess-1/2026-08-16-repaired-sidecar.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": relative_file_path,
            "content": _HAND_AUTHORED_MISSING_AGENT_TYPE,
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


@pytest.mark.parametrize("root", ["state", ".coordinator-local"])
def test_denies_hand_authored_under_live_sidecar_root(tmp_path, root):
    """Both sidecar roots reach the deny path.

    Every other fixture here spells `state/`, the PRE-relocation root.
    Provisioning writes under `.coordinator-local/subagent-share/` now, so
    this guard's path gate matched none of the writes it exists to police
    and denied nothing, with the suite green throughout. The
    `.coordinator-local` case fails against the old single-root regex.
    """
    payload = _payload(tmp_path, content=_HAND_AUTHORED_NO_FRONTMATTER, root=root)
    result = guard.check(payload)
    assert result is not None, f"a hand-authored sidecar under {root}/ must be denied"
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("root", ["state", ".coordinator-local"])
def test_provisioned_shape_allowed_under_both_roots(tmp_path, root):
    """Widening the gate must not deny a properly provisioned sidecar.

    A NEW-file write deliberately (no `existing=True`): an existing file is
    allowed by the new-file gate before the path gate is consulted at all,
    so that variant would pass identically against a dead guard and pin
    nothing. This one reaches the path gate and then the frontmatter check.
    """
    payload = _payload(tmp_path, content=_PROVISIONED_SHAPE, root=root)
    assert guard.check(payload) is None


def test_uppercase_md_leaf_is_not_a_way_past_the_gate(tmp_path):
    """`.MD` names the same file on NTFS and default APFS.

    The prior `\\.md$` regex was case-sensitive, so this fell through to
    ALLOW. The sibling guard already casefolds this test on a code-reviewer
    finding; this pins the pair in line.
    """
    payload = _payload(
        tmp_path,
        filename="2026-08-16-repaired-sidecar.MD",
        content=_HAND_AUTHORED_NO_FRONTMATTER,
        root=".coordinator-local",
    )
    result = guard.check(payload)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
