"""coordinator_core.bash_guards.tests.test_subagent_cohort_gate_identity_shapes --
pins the cold-vs-warm identity-shape fix for
state/audits/2026-08-29-unverified-parity-findings-measured.md FINDING A.

Both `guard_host_subagent_bash_ban` and `guard_host_subagent_bash_spawn_shapes`
previously gated their subagent cohort on
`_resolve_subagent_identity(raw_agent_id, session_id)` returning a
non-empty canonical id -- a DIFFERENT question from cold's
`isinstance(agent_id, str) and agent_id.strip()` raw non-empty-string test.
Three measured shapes (named teammate + short session_id, uppercase hex,
dashed UUID) were cold-DENY / warm-allow under the resolver-gated cohort
test. This module pins the fix: the cohort gate now asks cold's question
directly, with no canonical-id resolver in the loop.

Negative-spec:
  - Does NOT re-test `_resolve_subagent_identity` itself (the resolver is no
    longer imported by either guard under test) -- these cases assert the
    GUARDS' cohort-membership behavior end to end via `check()`.
  - Does NOT exercise the spawn-shapes guard's decline predicate
    (`_declines_for_inprocess_answer`) -- `test_spawn_shapes_decline_
    predicate.py` owns that; the fixture command here (`for f in *.md; do
    wc -l "$f"; done`) is a FOR_LOOP shape, never GREP_VIA_BASH.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.bash_guards import guard_host_subagent_bash_ban as _ban_mod
from coordinator_core.bash_guards import (
    guard_host_subagent_bash_spawn_shapes as _shapes_mod,
)

# Measured (state/audits/2026-08-29-unverified-parity-findings-measured.md
# FINDING A) cold-DENY / warm-allow shapes, plus the bare-hex control both
# sides already agreed on.
_DENY_SHAPES = [
    pytest.param("aexecutor-0123456789abcdef", "short12", id="named_teammate_short_session"),
    pytest.param("ABCDEF0123456789", "sess12345678", id="uppercase_hex"),
    pytest.param(
        "12345678-90ab-cdef-1234-567890abcdef", "sess12345678", id="dashed_uuid"
    ),
    pytest.param("abcdef1234567890", "sess12345678", id="bare_hex_control"),
]

_FOR_LOOP_CMD = 'for f in *.md; do wc -l "$f"; done'


def _write_ban_config(tmp_path: Path) -> None:
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\n", encoding="utf-8"
    )


def _write_shapes_config(tmp_path: Path) -> None:
    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_spawn_shapes: deny\n---\n", encoding="utf-8"
    )


def _ban_payload(tmp_path: Path, agent_id: str, session_id: str) -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "agent_id": agent_id,
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_input": {"command": _FOR_LOOP_CMD},
    }


def _shapes_payload(tmp_path: Path, agent_id: str, session_id: str) -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "agent_id": agent_id,
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_input": {"command": _FOR_LOOP_CMD},
    }


@pytest.mark.parametrize("agent_id,session_id", [(p.values[0], p.values[1]) for p in _DENY_SHAPES], ids=[p.id for p in _DENY_SHAPES])
def test_bash_ban_denies_all_measured_shapes(tmp_path: Path, agent_id: str, session_id: str) -> None:
    _write_ban_config(tmp_path)
    result = _ban_mod.check(_ban_payload(tmp_path, agent_id, session_id))
    assert result is not None, f"expected deny for agent_id={agent_id!r}"


@pytest.mark.parametrize("agent_id,session_id", [(p.values[0], p.values[1]) for p in _DENY_SHAPES], ids=[p.id for p in _DENY_SHAPES])
def test_spawn_shapes_denies_all_measured_shapes(tmp_path: Path, agent_id: str, session_id: str) -> None:
    _write_shapes_config(tmp_path)
    result = _shapes_mod.check(_shapes_payload(tmp_path, agent_id, session_id))
    assert result is not None, f"expected deny for agent_id={agent_id!r}"


def test_bash_ban_allows_no_agent_id(tmp_path: Path) -> None:
    _write_ban_config(tmp_path)
    payload = _ban_payload(tmp_path, "", "sess12345678")
    payload.pop("agent_id")
    assert _ban_mod.check(payload) is None


def test_bash_ban_allows_whitespace_agent_id(tmp_path: Path) -> None:
    _write_ban_config(tmp_path)
    result = _ban_mod.check(_ban_payload(tmp_path, "   ", "sess12345678"))
    assert result is None


def test_spawn_shapes_allows_no_agent_id(tmp_path: Path) -> None:
    _write_shapes_config(tmp_path)
    payload = _shapes_payload(tmp_path, "", "sess12345678")
    payload.pop("agent_id")
    assert _shapes_mod.check(payload) is None


def test_spawn_shapes_allows_whitespace_agent_id(tmp_path: Path) -> None:
    _write_shapes_config(tmp_path)
    result = _shapes_mod.check(_shapes_payload(tmp_path, "   ", "sess12345678"))
    assert result is None
