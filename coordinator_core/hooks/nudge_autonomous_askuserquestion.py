"""
coordinator_core.hooks.nudge_autonomous_askuserquestion — PreToolUse(AskUserQuestion)
advisory hook op.

Purpose: warm-door counterpart of DoE-claude's
`coordinator/hooks/scripts/nudge-autonomous-askuserquestion.py` — the FIRST hot-path
reconstructable unit built against `docs/reference/warm-hook-migration.md` (C1's
classification table), and the worked example this plan's C3 replicates across the
remaining candidates. Fires the standing ask-bar advisory ("break-class is yours to
decide and report, not to ask") whenever the resolved posture is "default"/
"substrate-free", or an autonomous-run sentinel is present for the calling session.
Advisory only — always "allow", never blocks.

Two independent obligations this op discharges (per its dispatch brief, staff-eng
finding 6): ROUTABILITY is satisfied by registering under the `hooks.` prefix alone
(`warm.hook_http.op_for_path` short-circuits on that prefix and never calls
`_is_compute_only` for it); AUTHZ CLASSIFICATION is a second, independent obligation
for the op's own dispatch-time authorization — added to
`coordinator_core/authz/classification.py` deliberately, not because routing needs it.

Every input is read from `params["payload"]` — the shape
`warm/hook_http.py :: payload_from_event` builds from the fired event — and NEVER
from this process's own `os.environ`: the resident engine serves ~50 concurrent
sessions, and its own environment belongs to none of them (see `hook_http`'s own
module docstring, obligation 2, and `ops/warm_guard_evaluate.py`'s docstring for the
established precedent this op follows). In particular the source script's
`COORDINATOR_AUTONOMOUS_ASK_OK=1` bypass is read here from `payload["env"]`, not
`os.environ.get(...)` — `hook_http.FORWARDED_ENV_PREFIXES` does not yet carry this
var (only `COORDINATOR_ALLOW_/OVERRIDE_/PROBE_/SCOPE_`), so an http-flipped
registration will not thread it end-to-end until that list is widened — a
same-shaped gap to the one C1's table already named for `plan-persistence-check.py`
and `stop-dispatch.py`'s env reads. Not fixed here (outside this chunk's `writes:`);
named so C4's env-threading step inherits it rather than re-discovering it.

Posture resolution is a deliberately narrower port than the source script's
`_posture.py`: that module walks upward from a resolved consuming-repo root for
`coordinator.local.md`, then falls back to `~/.claude/coordinator-identity.yaml`.
This op reads `payload["cwd"]` (a caller-supplied fact already on the event, never
an ambient walk from this process's own cwd) for the first rung and the same
identity file for the second, both fail-open to "precision" on any read failure —
the DIRECTION that matters (module docstring's "fail-open direction is
load-bearing"). It does not perform the `.git`-upward walk `_engine_root
._session_repo_root` does; a `cwd` that is not itself the repo root degrades this
rung to a miss (falls through to the identity-file rung, then to "precision"),
never to a wrong non-"precision" answer.

Spec backlink: docs/plans/2026-08-31-the-hook-category-stops-paying-an-interpreter-start.md
(chunk C2); docs/reference/warm-hook-migration.md (candidate-selection input).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Mapping, Optional

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import allow_advisory, no_advisory

_VALID_POSTURES = frozenset({"precision", "default", "substrate-free"})
_FAIL_OPEN_POSTURE = "precision"

_NUDGE_ANCHOR = (
    "coordinator/docs/wiki/coordinator-tripwires/"
    "an-em-asking-the-pm-a-break-class-question.md"
)

_ADVISORY_PREFIX_KEY = "engagement_posture"


def _extract_key_from_lines(lines, key: str) -> Optional[str]:
    """Scan flat `key: value` lines for `key`'s first value, tolerating a leading
    frontmatter fence and trailing inline comments. Not general YAML parsing —
    mirrors `_posture.py::_extract_key_from_lines` exactly, on the same class of
    input (a flat frontmatter/mapping block)."""
    prefix = key + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if value:
                return value
    return None


def _read_key_from_file(path: str, key: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return None
    return _extract_key_from_lines(lines, key)


def _resolve_posture(cwd: str) -> str:
    """Fail-open posture resolution: `<cwd>/coordinator.local.md` frontmatter,
    then `~/.claude/coordinator-identity.yaml`, then "precision".

    `cwd` is the caller-supplied fact off the payload — never this process's own
    working directory. `os.path.expanduser("~")` below resolves the ENGINE HOST's
    home directory (a fixed machine fact, not per-session state), matching the
    source script's own second rung; it is not a caller input this op is
    withholding by reading it from params instead.
    """
    if cwd:
        value = _read_key_from_file(
            os.path.join(cwd, "coordinator.local.md"), _ADVISORY_PREFIX_KEY
        )
        if value in _VALID_POSTURES:
            return value

    identity_path = os.path.join(
        os.path.expanduser("~"), ".claude", "coordinator-identity.yaml"
    )
    value = _read_key_from_file(identity_path, _ADVISORY_PREFIX_KEY)
    if value in _VALID_POSTURES:
        return value

    return _FAIL_OPEN_POSTURE


def _compose_advisory(posture: str) -> str:
    """The single unconditional advisory emitted at every firing posture — renders
    no verdict on the question at hand and blocks nothing. Verbatim port of the
    source script's `_compose_advisory`."""
    return (
        f"[first-officer posture: engagement_posture={posture}]\n"
        "Approach, structure, naming, sequencing, and break-class fixes are yours to\n"
        "decide and report, not to ask. Direction-class asks -- scope, product\n"
        "direction, prioritization, an irreversible or external action -- are\n"
        "correct to ask.\n"
        "This advisory renders no verdict on THIS question and blocks nothing.\n\n"
        f"See {_NUDGE_ANCHOR}."
    )


@register_op("hooks.nudge_autonomous_askuserquestion")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(AskUserQuestion) advisory: nudge the EM off AskUserQuestion for
    break-class/engineering-approach decisions at a firing posture.

    `params["payload"]` is the dict `warm/hook_http.py :: payload_from_event` builds
    from the fired event. Every input this handler reads — `agent_id`, `env`,
    `session_id`, `cwd` — comes from that payload, never from `os.environ` or this
    process's own `cwd`.

    Suppression conditions (verbatim order from the source script):
      1. `agent_id` present — a delegated worker's ask is not the EM's own halting
         decision (subagent fire).
      2. `payload["env"]["COORDINATOR_AUTONOMOUS_ASK_OK"] == "1"` — a pre-flight
         declaration of a legitimate irreversible ask (see module docstring for the
         env-threading gap this depends on).
      3. `session_id` absent/unresolvable — fail-open, inert.
      4. Neither the autonomous-run sentinel for this session nor a
         "default"/"substrate-free" resolved posture — inert.

    Returns `no_advisory()` (empty dict — D2 shape c) on any suppression; otherwise
    `allow_advisory("PreToolUse", <advisory text>)` (D2 shape a) — the same
    hookSpecificOutput shape the source script prints to stdout.
    """
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}

    if payload.get("agent_id"):
        return no_advisory()

    env = payload.get("env")
    if not isinstance(env, Mapping):
        env = {}
    if env.get("COORDINATOR_AUTONOMOUS_ASK_OK") == "1":
        return no_advisory()

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()

    sentinel_path = os.path.join(
        tempfile.gettempdir(), f"autonomous-run-{session_id}"
    )
    try:
        sentinel_present = os.path.isfile(sentinel_path)
    except Exception:
        sentinel_present = False

    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""
    try:
        posture = _resolve_posture(cwd)
    except Exception:
        posture = _FAIL_OPEN_POSTURE

    if not sentinel_present and posture not in ("default", "substrate-free"):
        return no_advisory()

    return allow_advisory("PreToolUse", _compose_advisory(posture))
