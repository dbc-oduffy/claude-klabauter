"""coordinator_core.bash_guards.block_worktree_sentinel_creation --
PreToolUse (Bash) hard-deny guard that makes the git-worktree ban's override
sentinel un-creatable by any agent -- Bash-level, EM included.

WHY THIS EXISTS. The fleet-wide git-worktree ban (DoE-side
`block-worktree-tool.py` / `strip-worktree-isolation.py`) is gated open by a
single repo-root sentinel file, `.coordinator-override-worktree-guard`.
Both of those DoE hooks deliberately omit an env-var override leg -- a
subagent can set an env var on itself before its own tool call runs, which
would defeat a guard that must bind subagents exactly as it binds the main
session (see either hook's own module docstring). But the sentinel FILE
itself was reachable: `Write`-ing it directly, or `touch`-ing it from a
shell, both succeeded, and `EnterWorktree` succeeded immediately afterward.
That is the identical self-grant the missing env-var leg exists to prevent,
reintroduced on a different surface. Found live by attempting the bypass,
after both worktree-ban guards were otherwise green -- the same discovery
shape that led to `block_approval_sentinel_creation.py` for the doctrine-
approval sentinel.

This guard closes the Bash-level leg for the worktree sentinel. The
file-write leg (Write/Edit/MultiEdit/NotebookEdit) is closed separately in
Coordinator-content-repo's `coordinator/hooks/scripts/guard-worktree-sentinel-write.py`.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
`block_approval_sentinel_creation.py` and `block_worktree_creation.py`: the
anti-pattern (self-granting a worktree-ban override) is wrong regardless of
who types it.

NO OVERRIDE -- DELIBERATE, by design, no exceptions. Same reasoning as
`block_approval_sentinel_creation.py`'s own "NO OVERRIDE" section: any
`COORDINATOR_OVERRIDE_*` escape hatch here would be reachable by exactly
the caller class this guard exists to constrain (a subagent, or an EM,
setting its own process env), making this a bypass of a bypass-prevention
guard.

REGISTRATION ORDERING -- MUST run BEFORE `offer-git-c` in
`coordinator_core.bash_guards.dispatch`, for the identical short-circuit
reason documented on `block_approval_sentinel_creation.py` ("REGISTRATION
ORDERING") and `block_worktree_creation.py`: `offer-git-c` rewrites
`cd <dir> && git <sub>`-shaped commands into `git -C <dir> <sub>` and
returns allow-with-updatedInput, which SHORT-CIRCUITS every later guard in
the chain. Registered immediately adjacent to `block_approval_sentinel_
creation` in `dispatch.py`, ahead of `offer-git-c`, for the same reason.

DETECTION SURFACE. Delegates entirely to the shared
`SentinelCreationDetector` in `_sentinel_creation_guard.py` (see that
module's docstring for the full rule set: redirection, `touch`/`cp`/`mv`/
`install`/`ln`/`tee`, `sed -i`, `python -c`, and the `cd <dir> &&`-prefixed
/ `git -C <dir>`-prefixed forms this guard's dispatch-level position
already covers by running ahead of the rewriter). DEFAULT POSTURE ON
AMBIGUITY IS DENY, same asymmetric posture as the doctrine-sentinel guard.

ALLOWED, UNCONDITIONALLY: reads (`cat`, `ls`, `stat`) and removal (`rm`,
`test -f ... && rm ...`) of the sentinel. Removing an override always
re-locks the boundary rather than unlocking it.

Deny message deliberately never names the sentinel file or prints a
workaround -- an eager agent reading its own bypass in a deny message
treats it as sanctioned (precedent: `block-worktree-tool.py`'s own deny
message discipline). Leads with the sanctioned alternative (scoped-parallel
dispatch into the same tree), then names PM permission as the path to
genuine branch-level isolation.

Spec: git-worktree-ban sentinel un-creatable-by-agent guard (coordinator-content-repo
dispatch, 2026-07-28) -- companion to the sibling DoE-side hooks that read
this sentinel to gate the worktree ban's override.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._sentinel_creation_guard import (
    REASON_INDIRECTION,
    SentinelCreationDetector,
)
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _tokenize_full_command,
)
from coordinator_core.conservatism import SafeDirection, declares_safe_direction
from coordinator_core.machine_profile import LEVEL_VERB, apply_guard_level

GUARD_NAME = "block-worktree-sentinel-creation"

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: prefix match -- an unrelated file that merely CONTAINS this string in a
#: longer name is a DIFFERENT file and is not the worktree-ban override
_TARGET_BASENAME = ".coordinator-override-worktree-guard"

_detector = SentinelCreationDetector(_TARGET_BASENAME)

_RISK_DIRECT = (
    "this command creates or modifies the worktree-ban override file, which "
    "only the PM may create; an agent creating it grants itself worktree isolation."
)
_RISK_INDIRECTION = (
    "this command's payload runs through an interpreter, stdin or xargs "
    "wrapper the guard could not read, so it might create the worktree-ban "
    "override file (PM-created only) unseen."
)

#: Heads that cannot write a file (no redirects except to /dev/null are
#: allowed alongside them). `sh`/`bash` qualify only in the `-c <payload>`
#: form, whose payload is checked in turn.
_READ_ONLY_HEADS = frozenset(
    {
        "jq", "cat", "echo", "printf", "head", "tail", "grep", "wc", "sort",
        "uniq", "cut", "tr", "ls", "stat", "test", "[", "true", "false",
        "pwd", "basename", "dirname", "date", "printenv",
    }
)
_SHELL_HEADS = frozenset({"sh", "bash", "dash"})
_OUT_REDIRECT_RE = re.compile(r"^\d*>{1,2}(?P<rest>.*)$")
_CMD_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_PREFIX_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_MAX_READ_ONLY_DEPTH = 3


def _tokens_are_read_only(cmd: str, depth: int) -> bool:
    if depth > _MAX_READ_ONLY_DEPTH:
        return False
    tokens = _tokenize_full_command(cmd)
    if not tokens:
        return False
    segment: List[str] = []
    segments: List[List[str]] = []
    for tok in tokens:
        if tok in ("|", "||", "&&", ";", "&", "\n"):
            if segment:
                segments.append(segment)
            segment = []
        else:
            segment.append(tok)
    if segment:
        segments.append(segment)
    return bool(segments) and all(_segment_is_read_only(seg, depth) for seg in segments)


def _segment_is_read_only(seg: List[str], depth: int) -> bool:
    i = 0
    while i < len(seg) and _PREFIX_ASSIGN_RE.match(seg[i]):
        i += 1
    if i >= len(seg):
        return False
    head = _normalize_executable_basename(seg[i])
    rest = seg[i + 1 :]
    j = 0
    while j < len(rest):
        m = _OUT_REDIRECT_RE.match(rest[j])
        if m:
            target = m.group("rest")
            if not target and "&" not in rest[j]:
                j += 1
                target = rest[j] if j < len(rest) else ""
            if target not in ("/dev/null", "&1", "&2") and not target.startswith("&"):
                return False
        elif "<(" in rest[j] or ">(" in rest[j]:
            return False
        j += 1
    for tok in seg:
        for m in _CMD_SUBST_RE.finditer(tok):
            if not _tokens_are_read_only(m.group(1) or m.group(2) or "", depth + 1):
                return False
    if head in _READ_ONLY_HEADS:
        return True
    if head in _SHELL_HEADS:
        if len(rest) >= 2 and rest[0] == "-c":
            payload = rest[1]
            inner = _CMD_SUBST_RE.fullmatch(payload)
            if inner:
                return True
            return "$(" not in payload and _tokens_are_read_only(payload, depth + 1)
        return False
    return False


def is_read_only_compound(cmd: str) -> bool:
    """True when `cmd` names no override file and every command in it (pipes,
    `sh -c` literals, `$(...)` substitutions) is a non-writing read."""
    if ".coordinator-override-worktree" in cmd:
        return False
    return _tokens_are_read_only(cmd, 0)


def _evaluate(cmd: str, dialect: Optional[Dialect] = None):
    if dialect is None or dialect is Dialect.BASH:
        return _detector.evaluate(cmd)
    return _detector.evaluate_for_dialect(
        cmd, dialect, guard_name="block_worktree_sentinel_creation"
    )


def _deny_reason(cmd: str, reason_kind: str, reason_class: str) -> str:
    # truthful ones: REASON_DIRECT means a rule positively matched the
    # assertion is correct. REASON_INDIRECTION means the payload sits
    # examine, so it denies BY CONSTRUCTION -- not because anything was
    del cmd
    if reason_class == REASON_INDIRECTION:
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the sentinel>")
        return (
            "BLOCKED (override-file guard): payload unreadable (%s); it "
            "might create the PM-only worktree-ban override file unseen.\n\n"
            "Use instead: read-only commands (`jq`, `cat`, `echo`, `grep`, "
            "`head`, `ls`, no file redirect), directly or in `sh -c`; "
            "`./path/to/script.sh` if executable with a shebang.\n\n"
            "Lower this guard: `%s`." % (safe_shape, LEVEL_VERB)
        )
    del reason_kind  # REASON_DIRECT: message below is fixed, not shape-derived.
    return (
        "BLOCKED: creates/modifies a worktree-ban override file. Use "
        "scoped-parallel edits instead; isolation needs EM+PM.\n\n"
        "Lower it: `%s`." % LEVEL_VERB
    )


@declares_safe_direction(
    SafeDirection.RAISE,
    because=(
        "swallowing a detection-engine failure into a silent allow would "
        "let a worktree-ban-override sentinel creation through unexamined "
        "-- the identical self-grant this guard exists to close, on a "
        "different surface; propagating the failure lets the dispatcher's "
        "fail-closed default deny it instead, per module docstring's "
        "'Deliberately no try/except here' note"
    ),
)
def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the worktree-override-sentinel-creation-ban gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM
    (see module docstring "NOT IDENTITY-GATED").
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None
    dialect = dialect_from_tool_name(tool_name)

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    deny, reason_kind, reason_class = _evaluate(cmd, dialect)
    if not deny:
        return None

    if reason_class == REASON_INDIRECTION and is_read_only_compound(cmd):
        return None

    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, reason_kind, reason_class),
        }
    }
    risk = _RISK_INDIRECTION if reason_class == REASON_INDIRECTION else _RISK_DIRECT
    return apply_guard_level(GUARD_NAME, envelope, risk=risk[0].upper() + risk[1:])
