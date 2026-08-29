"""coordinator_core.bash_guards.block_fleet_delegation_creation -- PreToolUse
(Bash) hard-deny guard over Bash-level creation/overwrite of the fleet-
delegation grant record, EM included.

WHAT THIS GUARD ACTUALLY BUYS, AND WHAT IT DOES NOT. Same posture, same
caveats, and the same class of gap as
`block_approval_sentinel_creation.py` (read that module's own docstring for
the full accounting) -- this is a LEXICAL classifier over command TEXT, it
never executes, evaluates, or simulates the shell. It closes the shapes it
was shown -- `touch`, a redirect, `cp`/`mv`/`install`/`ln`/`tee`, `sed -i`,
`python -c`, `dd of=`, and the tainted/transitive-variable-dereference forms
of each -- and is not a barrier against a deliberate evasion (dynamic string
assembly, a glob near-miss, a runtime-computed basename; see the shared
`_sentinel_creation_guard` module's own "KNOWN OPEN GAP" entries). What DOES
still hold regardless of this gap: the Write/Edit tool-call surface is
separately guarded by `block_fleet_delegation_write.py` (a different code
path, not a text classifier over Bash strings), and `fleet_delegation.
write_fleet_delegation` (C2) independently refuses to persist a record whose
`authorship` verdict is not HUMAN -- so even a forged file that slipped past
every guard here still fails the read-side `check_fleet_delegation` gate
unless it also forges a HUMAN authorship verdict, which only C1's harness-
ancestor walk can produce truthfully for an actual human-typed write.

WHY THIS EXISTS. `coordinator_core/session/fleet_delegation.py` (C2) persists
the fleet-delegation grant record at `settings_home() /
"fleet-delegation.json"`, gated on `write_fleet_delegation` observing a
HUMAN authorship verdict (C1) at call time. If any agent could fabricate
that file directly via Bash -- `touch`, a redirect, `cp` from a stashed
copy, `python3 -c` -- the human-authorship gate the writer function itself
enforces would be reachable from outside the writer entirely, exactly the
class of hole `block_approval_sentinel_creation.py` exists to close for its
own sentinel. This guard closes the identical class of Bash-level bypass for
the fleet-delegation grant record.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
`block_approval_sentinel_creation.py`: the anti-pattern (an agent forging
its own delegation grant) is wrong regardless of who types it. An EM-only
gate would defeat the guard's own purpose, since the EM is exactly who a
delegation grant would otherwise let bypass the "ask the PM" step.

NO OVERRIDE -- DELIBERATE, by design, no exceptions. Same reasoning as
`block_approval_sentinel_creation.py`'s own "NO OVERRIDE" section: a
subagent (or an EM) can set its own process env, so any
`COORDINATOR_OVERRIDE_*` escape hatch here would be reachable by exactly the
caller class this guard exists to constrain. There is no legitimate reason
for an agent to ever create this specific file -- the PM's own act of
granting delegation IS the approval, so a programmatic path to create it is
never sanctioned, not even conditionally. Do not add one.

REGISTRATION ORDERING -- same requirement as `block_approval_sentinel_
creation.py`'s own "REGISTRATION ORDERING" section: this guard MUST run
BEFORE `offer-git-c` in `coordinator_core.bash_guards.dispatch`, for the
identical short-circuit reason (`cd <dir> && git <sub>` rewriting bypasses
every later guard). This module does not itself edit `dispatch.py` --
registration is a separate seam (see the dispatch brief's REGISTRATION SEAM
note); this docstring records the ordering requirement so whoever wires the
registration entry does not have to re-derive it.

DETECTION SURFACE. Reuses the shared shell-shape tokenizer/segmenter from
`block_subagent_destructive_action.py` (`_tokenize_full_command`,
`_segments_from_tokens`, `_normalize_executable_basename`) and the shared
`_sentinel_creation_guard.SentinelCreationDetector` engine, via the SAME
default-deny subclass shape `block_approval_sentinel_creation.py` already
built and hardened over three forge-closure rounds
(`_ApprovalSentinelDetector` there, `_FleetDelegationDetector` here) --
rather than authoring a new classifier. Within each shell segment, this
guard denies when it finds either:

  1. A shell redirection (`>`, `>>`, optionally fd-prefixed like `2>`, in
     either bare-operator ("> file") or attached ("`>file`") form) whose
     target's basename is the grant-record filename.
  2. An invocation of a command capable of creating/overwriting a named
     file by argument -- `touch`, `cp`, `mv`, `install`, `ln`, `tee` -- where
     any argument's basename is the grant-record filename (source OR
     destination position; default-deny posture, see below).
  3. `sed -i` (any `-i`-prefixed in-place flag spelling) where an argument's
     basename is the grant-record filename.
  4. `python`/`python2`/`python3`(`.NN`) invoked with `-c <code>` (bare or
     attached, `-ccode`) where the code payload contains the grant-record
     filename as a substring.
  5. `dd of=<target>` where the target's basename is the grant-record
     filename.

Plus the same variable-taint (direct and transitive) tracking and
indirection-wrapper (`bash -c`, `env`, `xargs`, heredoc-fed interpreter)
unwrap pass the shared engine and the approval-sentinel subclass already
provide -- see `_sentinel_creation_guard.py`'s own docstring and
`block_approval_sentinel_creation._ApprovalSentinelDetector`'s docstring for
the full history of what each closes.

DEFAULT POSTURE ON AMBIGUITY IS DENY, DELIBERATELY ASYMMETRIC -- same
reasoning as the approval-sentinel guard: a false negative here is a
structural failure of the human-authorship boundary `fleet_delegation.py`
depends on, while a false positive costs only a rephrase.

ALLOWED, UNCONDITIONALLY: reads (`cat`, `ls`, `stat`, `test`, `head`,
`tail`, `wc`, `file`, `grep`) and removal (`rm`) of the grant record, plus
the same narrow read-only `git` subcommand allowlist
(`status`/`diff`/`log`/`show`/`ls-files`/`rev-parse`/`describe`/
`check-ignore`/`check-attr`) -- removing a grant record is always safe (it
re-locks the boundary rather than unlocking it), and reading it does not
create or modify anything.

KNOWN OPEN GAP -- DYNAMIC STRING CONSTRUCTION AND GLOB-SHAPED NEAR-MISSES.
Identical class of gap to `block_approval_sentinel_creation.py`'s own
"KNOWN OPEN GAP" section (documented there, not re-derived here) -- a
payload that never spells the grant-record basename as a contiguous
substring in the command text defeats this guard's mention-based rules.
Not closed here for the same reason it is not closed there: doing so would
require actually interpreting the shell, a different guard shape entirely.

Spec: state/dispatch-briefs/2026-08-28-the-ask-the-pm-step-gets-an-artifact-
to-check/C4.md -- companion to `block_fleet_delegation_write.py` (C3, the
Write/Edit-tool leg) and `coordinator_core/session/fleet_delegation.py`
(C2, the writer this guard's boundary protects).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import re

from coordinator_core.bash_guards._sentinel_creation_guard import (
    REASON_INDIRECTION,
    SentinelCreationDetector,
    _REDIR_PREFIX_RE,
)
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _strip_heredoc_bodies,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
#: Full command tool-name universe, same as `block_approval_sentinel_
#: creation.py`'s own widened declaration (C4e) -- a direct reference to the
#: shared universe, never a copy or re-wrap.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: The exact basename this guard protects -- the grant record C2
#: (`coordinator_core/session/fleet_delegation.py`) writes at
#: `settings_home() / "fleet-delegation.json"`. Never relaxed to a
#: substring/prefix match -- an unrelated file that merely CONTAINS this
#: string in a longer name is a DIFFERENT file and is not the grant record
#: this guard is chartered to protect.
_TARGET_BASENAME = "fleet-delegation.json"

#: A `VAR=value` assignment token (bare, or the `VAR=value` half of an
#: `export VAR=value` pair).
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

#: A `$VAR` or `${VAR}` dereference, anywhere inside a token.
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


class _FleetDelegationDetector(SentinelCreationDetector):
    """Default-deny variant of the shared detector, scoped to THIS guard
    only -- a direct model of `block_approval_sentinel_creation.
    _ApprovalSentinelDetector`, parameterized on the fleet-delegation grant
    record's basename instead of the doctrine-approval sentinel's.

    WHY A SUBCLASS, NOT AN EDIT TO THE SHARED ENGINE. Same reasoning as the
    approval-sentinel guard's own subclass: `SentinelCreationDetector` is
    shared by multiple concrete guards on the base (allowlist) posture;
    overriding only `_segment_denies`/`_segment_mentions_target`/
    `_redirect_target_denies` on a subclass keeps every other guard on the
    shared engine untouched while this guard gets the inverted (default-
    deny) posture -- a segment that MENTIONS the grant-record basename
    anywhere in its tokens DENIES unless its head command is one of a
    narrow, explicitly-enumerated set of provably harmless operations.

    Variable taint (direct and transitive) is reused verbatim from the
    approval-sentinel subclass's own implementation -- see that class's
    docstring "VARIABLE TAINT" / "TRANSITIVE TAINT" for the full history;
    the mechanism is copied here (not imported) because it is bound to
    THIS subclass's own `_tainted_vars` instance state and mention regex,
    exactly as the approval-sentinel subclass's own copy is bound to its.
    """

    #: Commands that can never create/modify the grant record through their
    #: own normal operation (absent a redirect, checked separately and
    #: first). Removal is always sanctioned; the rest are pure reads.
    _SAFE_ARGV0 = frozenset(
        {"rm", "cat", "ls", "stat", "test", "head", "tail", "wc", "file", "grep"}
    )

    #: `git` subcommands that only read repo state -- identical allowlist to
    #: `block_approval_sentinel_creation.py`'s own, for the identical reason
    #: (verifying the grant record's ignore/attr status must not itself be
    #: denied as a write).
    _SAFE_GIT_SUBCOMMANDS = frozenset(
        {
            "status", "diff", "log", "show", "ls-files", "rev-parse", "describe",
            "check-ignore", "check-attr",
        }
    )

    def _segment_is_safe(self, seg_tokens: "list[str]", argv0_idx: int) -> bool:
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base in self._SAFE_ARGV0:
            return True
        if base == "git" and argv0_idx + 1 < len(seg_tokens):
            sub = seg_tokens[argv0_idx + 1]
            if sub in self._SAFE_GIT_SUBCOMMANDS:
                return True
        return False

    def __init__(self, target_basename: str) -> None:
        super().__init__(target_basename)
        #: Variable names, tainted for the CURRENT `evaluate()` call only --
        #: recomputed at the top of `evaluate()` from that call's own
        #: command string, never carried over between calls.
        self._tainted_vars: "set[str]" = set()

    def _collect_tainted_vars(self, tokens: "list[str]") -> "set[str]":
        """Scan every token of the (whole, not-yet-segmented) command for a
        `VAR=value` assignment and return the set of tainted variable
        names, iterated to a FIXED POINT -- direct mention, or dereference
        of an already-tainted variable, same as
        `block_approval_sentinel_creation._ApprovalSentinelDetector`'s own
        copy of this method."""
        assignments: "list[tuple[str, str]]" = []
        for tok in tokens:
            m = _ASSIGN_RE.match(tok)
            if m:
                assignments.append((m.group(1), m.group(2)))

        tainted: "set[str]" = set()
        changed = True
        while changed:
            changed = False
            for var, value in assignments:
                if var in tainted:
                    continue
                if self._mention_re.search(value):
                    tainted.add(var)
                    changed = True
                    continue
                if any(
                    vm.group(1) in tainted for vm in _VAR_REF_RE.finditer(value)
                ):
                    tainted.add(var)
                    changed = True
        return tainted

    def _token_dereferences_tainted_var(self, token: str) -> bool:
        return any(
            m.group(1) in self._tainted_vars for m in _VAR_REF_RE.finditer(token)
        )

    def _segment_mentions_target(self, seg_tokens: "list[str]") -> bool:
        for tok in seg_tokens:
            if self._mention_re.search(tok):
                return True
            if self._token_dereferences_tainted_var(tok):
                return True
        return False

    def _redirect_target_denies(self, seg_tokens: "list[str]") -> bool:
        """OVERRIDE, same reason as the approval-sentinel guard's own
        override: the parent's version only checks the literal-basename
        compare against the redirect's target token, missing a tainted-
        variable-dereferenced redirect target (`S=<grant-file>; cat x > $S`)."""
        n = len(seg_tokens)
        for i, tok in enumerate(seg_tokens):
            m = _REDIR_PREFIX_RE.match(tok)
            if not m:
                continue
            remainder = tok[m.end() :]
            if remainder:
                candidate = remainder
            elif i + 1 < n:
                candidate = seg_tokens[i + 1]
            else:
                continue
            if self._is_target(candidate):
                return True
            if self._token_dereferences_tainted_var(candidate):
                return True
        return False

    def evaluate(self, cmd: str):
        """OVERRIDE: recompute `self._tainted_vars` from THIS call's command
        string before delegating to the parent's `evaluate()`."""
        cmd_norm = _strip_heredoc_bodies(cmd)
        tokens = _tokenize_full_command(cmd_norm)
        self._tainted_vars = self._collect_tainted_vars(tokens) if tokens else set()
        return super().evaluate(cmd)

    def _segment_denies(self, seg_tokens: "list[str]") -> bool:  # noqa: D401
        """Default-deny override: a redirect into the grant record always
        denies; otherwise a segment whose head command is NOT in the safe
        set denies as soon as ANY of its tokens mentions the grant-record
        basename -- subsumes the parent's enumerated-command rules 2-5."""
        if not seg_tokens:
            return False
        if self._redirect_target_denies(seg_tokens):
            return True
        argv0_idx = self._env_skip_index(seg_tokens)
        if argv0_idx >= len(seg_tokens):
            return False
        if self._segment_is_safe(seg_tokens, argv0_idx):
            return False
        return self._segment_mentions_target(seg_tokens)


#: Detection engine for this guard specifically -- see `_FleetDelegationDetector`
#: docstring above for why this is a dedicated subclass rather than a shared-
#: engine edit.
_detector = _FleetDelegationDetector(_TARGET_BASENAME)


def _evaluate(cmd: str, dialect: Optional[Dialect] = None):
    """`dialect=None`/`Dialect.BASH` calls `_detector.evaluate(cmd)` directly
    -- only a genuinely recognized non-bash dialect routes through the
    dialect-aware entry point, same call shape as
    `block_approval_sentinel_creation._evaluate`."""
    if dialect is None or dialect is Dialect.BASH:
        return _detector.evaluate(cmd)
    return _detector.evaluate_for_dialect(
        cmd, dialect, guard_name="block_fleet_delegation_creation"
    )


def _deny_reason(cmd: str, reason_kind: str, reason_class: str) -> str:
    # Deliberately does NOT echo `cmd` back into the message and does NOT
    # name the target basename in the indirection branch -- both would
    # print the exact bypass an eager agent could copy-paste. `cmd` stays
    # accepted for call-site symmetry with the sibling guard, but is
    # intentionally unused here.
    del cmd
    if reason_class == REASON_INDIRECTION:
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the delegation grant>")
        return (
            "BLOCKED (fleet-delegation guard): this command was denied "
            "because its payload is delivered through an interpreter, "
            "stdin, or command-assembly indirection this guard cannot "
            "examine -- NOT because the payload was found to touch the "
            "fleet-delegation grant record.\n\n"
            "Detected shape: %s\n\n"
            "If this command genuinely does not touch the grant record: "
            "run its underlying steps directly (not through an "
            "interpreter/stdin/xargs wrapper) so this guard can see them, "
            "or ask the EM/PM to run it.\n\n"
            "Reading or removing an existing grant record remains available "
            "as a DIRECT command -- `cat`, `ls`, `stat`, `rm` -- but not "
            "through a wrapper like this one: inside an interpreter payload "
            "this guard cannot tell a read from a write, so it denies "
            "either way. Removal only re-locks the boundary." % safe_shape
        )
    del reason_kind  # REASON_DIRECT: message below is fixed, not shape-derived.
    return (
        "BLOCKED: creates/modifies the fleet-delegation grant record; "
        "agents cannot self-grant delegation. Ask the PM to run "
        "`coordinator-delegation grant ...`.\n\n"
        "Use instead: `cat`, `ls`, `stat`, `test`, `head`, `tail`, `wc`, "
        "`file`, `grep`, `rm`, `git status`, `git diff`, `git log`, "
        "`git show`, `git ls-files`, `git rev-parse`, `git describe`, "
        "`git check-ignore`, `git check-attr`. Removal re-locks the boundary."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the fleet-delegation-grant-record-creation-ban gate against a
    PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM.
    """
    # Deliberately no try/except here -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards.
    tool_name = payload.get("tool_name") or ""
    if tool_name not in MATCHERS:
        return None
    dialect = dialect_from_tool_name(tool_name)

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    # NOTE: deliberately no raw-text pre-filter gate here, same reason as
    # `block_approval_sentinel_creation.check`'s own note -- a partially-
    # quoted spelling of the basename only reconstructs after tokenization.
    deny, reason_kind, reason_class = _evaluate(cmd, dialect)
    if not deny:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, reason_kind, reason_class),
        }
    }
