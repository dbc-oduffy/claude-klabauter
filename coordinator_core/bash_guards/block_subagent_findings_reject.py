"""coordinator_core.bash_guards.block_subagent_findings_reject -- PreToolUse
(Bash) hard-deny guard closing the EM-only surface of the reviewer-applies-
own-findings ledger op: a dispatched subagent invoking
`coordinator_core.ops.review_findings_ledger`'s `reject` or `targets`
subcommand (over the `coordinator/bin/review-findings-ledger.py` trampoline,
`python -m`/`python3 -m`, or a direct script invocation) to undo an EM's own
rejection call, or to widen the write-target set a confined reviewer may
touch.

SPEC: DoE-claude `docs/plans/2026-09-26-retire-review-integrator.md`, row M3
(§ Contract's `reject`/`targets` subcommand text; § Design decision 1 for why
`targets` widens what `write_guards.block_confined_agent_write` (M1) admits).

THE HOLE THIS CLOSES. `review_findings_ledger.py` (M2) is a bare CLI: nothing
in the op itself refuses a caller by identity, only by the shape of the
sidecar/finding it is pointed at. `verify` is safe for any reviewer to run on
its own sidecar (it is the reviewer's own self-check before returning); but
`reject` reverses an already-applied finding (restoring `before` over
`after` in the reviewed artifact) and `targets --add` widens the path set
`block_confined_agent_write` (M1) lets a confined findings-agent write to --
both are EM-only per the plan's Contract ("The EM is the only actor that may
reject a finding, and it must give a reason" / "The EM runs it before
dispatching a confined reviewer"). Without this guard, a confined reviewer
could self-serve either: undo a peer's finding to erase evidence its own
ledger disagrees with, or register itself as a target for a file the EM
never intended it to write.

IDENTITY-GATE POSTURE -- fail CLOSED, modeled directly on
`block_subagent_grant_acquisition.py`'s posture (itself modeled on
`block_subagent_stash_creation.py`'s): the discriminator is the RAW
presence of the harness-supplied `agent_id` field alone, never whether it
resolves further. `agent_id` present but unresolvable is still,
unambiguously, "not the EM", so it denies. This guard has exactly one
allowed caller (the EM -- no `agent_id` on the payload at all) and exactly
two gated subcommands (`reject`, `targets`); there is no per-kind branching
a resolution failure could legitimately need to fall through to, so falling
open on an unresolvable `agent_id` would silently re-open the exact hole
this module exists to close.

RECOGNIZED SHAPES (classification only):

  1. `python`/`python3` + `-m` + module `coordinator_core.ops.
     review_findings_ledger` + a `reject`/`targets` subcommand token
     immediately following the module path. `verify` is explicitly NOT
     gated -- a reviewer self-checking its own ledger before returning is
     the ordinary, encouraged path (M3's own body: "Add `review-findings-
     ledger verify` to the reviewer Bash allowlist").
  2. A direct script invocation of the `review-findings-ledger` CLI
     trampoline (`coordinator/bin/review-findings-ledger.py`, optionally
     `python`/`python3`-prefixed, optionally path-prefixed, optionally
     missing its `.py` suffix on a shim/`.cmd` install) whose first
     argument after the script path is `reject`/`targets`. Matched by
     basename suffix, the same shape `block_reviewer_bash_outside_
     allowlist.py`'s Tier B scaffolder match uses for `coordinator-doc-new`.

HEURISTIC-NOT-EXHAUSTIVE -- stated plainly, not left implicit, per the same
posture `block_subagent_grant_acquisition.py`'s docstring states for its own
token-shape classifier. A subagent that reaches either subcommand through a
wrapper, an alternate interpreter, or an argv shape this walk does not
tokenize for is not caught here. This is not framed as the only illegitimate
route -- only as the one this guard's detection surface covers.

OVERRIDE -- none at this layer, matching the sibling identity guards this
module is modeled on: there is no documented rare-use escape hatch for
either gated subcommand, and none is added here.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _segments_from_tokens,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

_LEDGER_MODULE = "coordinator_core.ops.review_findings_ledger"

#: `verify` is deliberately absent -- a reviewer's own self-check is the
#: encouraged path and must never be denied here (M3 body, § Contract).
_GATED_SUBCOMMANDS = frozenset({"reject", "targets"})

_PYTHON_BASENAME_RE = re.compile(r"^python[0-9.]*$")

_DASH_M_BUNDLED_RE = re.compile(r"^-m(.+)$")

#: Basename of the CLI trampoline this op ships as (`coordinator/bin/
#: review-findings-ledger.py`) -- matched by suffix so a path prefix, a
#: missing `.py` (a shim/`.cmd` install), or a hyphen/underscore spelling
#: variant of the on-disk name is still recognised, mirroring `block_
#: reviewer_bash_outside_allowlist.py`'s own `coordinator-doc-new` match.
_TRAMPOLINE_BASENAME = "review-findings-ledger"


def _extract_dash_m_module_and_rest(
    argv_after_interpreter: List[str],
) -> Optional[Tuple[str, List[str]]]:
    n = len(argv_after_interpreter)
    for i, tok in enumerate(argv_after_interpreter):
        if tok == "-m":
            if i + 1 >= n:
                return None
            return argv_after_interpreter[i + 1], argv_after_interpreter[i + 2:]
        m = _DASH_M_BUNDLED_RE.match(tok)
        if m:
            return m.group(1), argv_after_interpreter[i + 1:]
    return None


def _trampoline_basename_match(token: str) -> bool:
    base = _normalize_executable_basename(token)
    base = base[:-3] if base.endswith(".py") else base
    return base == _TRAMPOLINE_BASENAME


def _classify_dash_m_invocation(working: List[str]) -> Optional[str]:
    found = _extract_dash_m_module_and_rest(working[1:])
    if found is None:
        return None
    module, rest = found
    if module != _LEDGER_MODULE:
        return None
    subcmd = rest[0] if rest else None
    if subcmd not in _GATED_SUBCOMMANDS:
        return None
    return f"python -m {_LEDGER_MODULE} {subcmd}"


def _classify_direct_script_invocation(working: List[str]) -> Optional[str]:
    """Classify `working` for the direct-script shape (2): either the head
    token itself is the trampoline (a bare/path-prefixed executable
    invocation) or the head is a python interpreter and the NEXT token is
    the trampoline (`python3 <path>/review-findings-ledger.py reject ...`).
    """
    head_base = _normalize_executable_basename(working[0])
    if _trampoline_basename_match(working[0]):
        rest = working[1:]
    elif _PYTHON_BASENAME_RE.match(head_base) and len(working) > 1 and _trampoline_basename_match(working[1]):
        rest = working[2:]
    else:
        return None
    subcmd = rest[0] if rest else None
    if subcmd not in _GATED_SUBCOMMANDS:
        return None
    return f"{_TRAMPOLINE_BASENAME} {subcmd}"


def _evaluate(cmd: str) -> Optional[str]:
    """Segment-walk `cmd`'s tokenized shape and classify each segment
    against both the `-m` module form and the direct-script trampoline
    form. Returns the first deny_kind label found, or `None` if nothing in
    `cmd` matches either recognized shape.
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        record_silent(
            "block-subagent-findings-reject",
            "_tokenize_full_command returned None (unparsed PowerShell "
            "here-string or backtick-continuation shape)",
        )
        return None

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        working = _strip_leading_subshell_and_env(seg_tokens)
        if not working:
            continue

        verdict = _classify_dash_m_invocation(working)
        if verdict is not None:
            return verdict

        verdict = _classify_direct_script_invocation(working)
        if verdict is not None:
            return verdict

    return None


def _deny_reason(cmd: str, deny_kind: str) -> str:
    """Design-as-offer deny text -- only ever rendered for a dispatched
    subagent (see module docstring "IDENTITY-GATE POSTURE"). Mirrors
    `block_subagent_grant_acquisition._deny_reason`'s register: states the
    structural reason a subagent cannot itself clear this gate, and hands
    it what its BLOCKED report needs, never a command it could run itself.
    """
    del deny_kind, cmd
    return (
        "BLOCKED: `review-findings-ledger reject`/`targets` are EM-only. "
        "Report BLOCKED to your EM: for a contested finding, name it in "
        "your findings ledger and let the EM run `reject` with its reason; "
        "for a write target you need, ask your EM to run `targets --add` "
        "before re-dispatching you. `verify` on your own sidecar stays "
        "available."
    )


def check(payload: dict) -> Optional[dict]:
    """Evaluate the `reject`/`targets` EM-only gate against a PreToolUse
    payload. Identity-gated to subagents only -- fail CLOSED on a
    present-but-unresolvable `agent_id`, same posture as
    `block_subagent_grant_acquisition.check`.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    raw_agent_id = payload.get("agent_id")
    if not raw_agent_id:
        return None

    deny_kind = _evaluate(cmd)
    if deny_kind is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, deny_kind),
        }
    }
