"""coordinator_core.write_guards.guard_doctrine_surface_edits — deny edits to
always-loaded doctrine surfaces unless a PM-created approval sentinel is
present and unexpired.

Ported from DoE-claude
`coordinator/hooks/scripts/guard-doctrine-surface-edits.py` (faithful port —
see `write_guards/INTERFACE.md`). CLASS is hard-deny and fail-closed; this
is the one guard in the DoE source tree that deliberately differs from every
sibling guard's fail-open posture — see "Fail-closed is DELIBERATE" below,
carried over verbatim from the source docstring.

Why this exists
----------------
Two distinct classes of file are protected here, for two distinct reasons.
Both reduce to the same structural property — an EM editing one of them
unilaterally is self-approval of a change nobody else gets to see — so both
take the same sentinel, but conflating their rationales has already cost a
sibling repo a round trip (cross-repo/inbox/2026-08-08-example-store-repo-em-…),
and the deny message is per-class for that reason.

CLASS 1 — always-loaded instruction prose. The global CLAUDE.md, the
EM-only operating-doctrine channel, and the repo-root project CLAUDE.md are
injected into every session and every dispatched agent's context. An edit
here silently changes everyone's instructions, fleet-wide.

CLASS 2 — privileged configuration. coordinator.local.md is NOT
always-loaded prose, and calling it doctrine is a category error. It is
protected because its frontmatter is the repo's privileged-execution and
authority surface, which is a stronger reason to gate it, not a weaker one:
  - `fast_test_cmd` / `full_test_cmd` and the `*_post_command` keys are
    command strings the ceremony machinery EXECUTES
    (coordinator/bin/coordinator-ceremony-hook.py, argv-only).
  - `fast_tier_unscoped_reason` / `fast_tier_shape` DISCHARGE the Tier-U
    caller-authority check for this repo's literal resolved `fast_test_cmd`
    (coordinator_core/session/tier_u_gate.py, DR-088 R6 / DR-235).
An EM free to edit these keys could narrow the gate command and, in the same
edit, keep the declaration that authorizes running it — self-approving a
gutted cadence gate. The file's prose body is the part closest to ordinary
documentation; the frontmatter is the part that must not move unwitnessed.

Protected surfaces (resolved, symlink-free, absolute real paths; matched
exactly — never by substring or basename, so a nested CLAUDE.md deeper in
the tree, e.g. coordinator/tests/CLAUDE.md, is NOT protected):
  - <repo-root>/global-doctrine/CLAUDE.md                 (class 1)
  - $HOME/.claude/CLAUDE.md              (class 1, the derived live copy)
  - <repo-root>/coordinator/snippets/em-operating-doctrine.md   (class 1)
  - <repo-root>/CLAUDE.md          (class 1, repo-root project instructions)
  - <repo-root>/coordinator.local.md                      (class 2)

Negative spec — do NOT "split" this guard by gating coordinator.local.md's
prose body while ungating its frontmatter keys. That is the intuitive split
and it is exactly inverted: the frontmatter is the executed/authority half.

Approval mechanism
-------------------
Repo-root sentinel file `.coordinator-doctrine-edit-approved` (name
deliberately NOT printed in the deny message — see Deny message below):
  - absent -> DENY
  - present, mtime older than APPROVAL_WINDOW_SECONDS (30 minutes) -> DENY
    (treated as expired)
  - present, mtime within the window -> ALLOW (silent, no output). One
    approval covers a multi-edit change, which is the normal shape of a
    doctrine edit (several Edit calls against the same file in one pass).

Fail-closed is DELIBERATE and is the one place this guard differs from
every sibling guard in this tree. Every other guard fails OPEN on its own
resolution failures (git root unresolvable, home unresolvable, subprocess
timeout) because each of those guards protects against a mistake, and
refusing to protect against a mistake you can't currently detect is the
safer failure mode. This guard protects a PM approval BOUNDARY, not a
mistake — a boundary that fails open under an unreadable sentinel or an
unresolvable repo root is not a boundary at all, it is a suggestion. So: if
the git root cannot be resolved, this guard falls back to checking ONLY the
$HOME/.claude/CLAUDE.md protection (the one protected path that does not
require a repo root), and treats the approval sentinel as ABSENT (deny)
rather than skipping the check.

The one place this guard still fails OPEN, unconditionally, is an
unparseable or non-matching payload: if the guard cannot tell what file is
being edited (missing tool_input, no path key, tool not in the guarded set,
or the resolved path is not one of the five protected surfaces), it has no
basis to block anything and allows silently. The distinction that matters:
unparseable payload -> allow; parseable payload that DOES target a
protected file but whose approval state cannot be resolved -> deny.

**HARD CONSTRAINT carried from the porting brief — do NOT wrap `check()` in
a blanket try/except.** Every risky call below (`_git_root`, `_norm`,
`_protected_paths`, `_sentinel_state`) is individually try/excepted to a
definite value exactly as in the source, so the fail-closed posture holds
without needing a function-level catch-all; adding one would convert this
boundary to fail-open, which is the one identified way to put a hole in it.

Deny message
-------------
Design-as-offers: leads with why THIS file matters — per class, because a
class-1 reason attached to coordinator.local.md reads as false to anyone who
has looked at the file, and a deny message a reader can falsify is a deny
message they route around — and follows with what to do about it (describe
the proposed change to the PM and ask for approval).

AUDIENCE-GATED (2026-08-13, C4a; plan
docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md;
docs/wiki/guard-messaging.md § Register, B6). The prior render named the
sentinel filename and printed its creation command verbatim, paired with an
explicit "relay this, do NOT run it, you cannot" — the exact BYPASS-in-the-
denial shape B6 forbids: showing a confined reader the key and forbidding
its use in the same breath is the disclosure, not a mitigation, because it
is what makes a well-meaning subagent's rationalisation through the gate
available ("my EM told me to..., and this guard shows the button, so I
press it"). Per B6's audience split:
  - **Dispatched subagent** — no statement that an unlock exists survives in
    any shape: no filename, no command, no "you cannot" line. The REFUSAL
    (describe the change to the PM) is the entire message.
  - **Positively resolved EM** (`resolves_em_audience`,
    `coordinator_core.session.identity`) — the message additionally routes
    to `docs/reference/guard-override-keys.md` (which documents this exact
    sentinel, `.coordinator-doctrine-edit-approved`, under its own entry)
    and nothing else: no key, no path, no command.
  - **Unresolved audience** — degrades to the terse (subagent) form, never
    the mechanism, per B6's unresolved-audience degradation rule.
`_deny_reason` therefore takes `payload` (the PreToolUse envelope) to
resolve audience at render time; `check()` threads it through unchanged.

Fail-open paths (all return None, in order):
  - tool_name not in the guarded set.
  - no target path in tool_input.
  - resolved target path is not one of the five protected surfaces.

C4 addendum — advisory repo-identity recording
------------------------------------------------
`check()` also calls C1's `coordinator_core.pickup_assemble.compute_repo_identity_gate`
and logs its verdict (MATCH/MISMATCH/UNRESOLVED) via
`_write_repo_identity_advisory_log`, best-effort, to
`<repo-root>/.git/coordinator-sessions/<session_id>/repo-identity-gate.log`.
**This is READ-ONLY and ADVISORY ONLY — see DR-277
(`docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md`).** This
site clears no DR-277 carve-out, so the verdict is recorded and never
consulted by the allow/deny decision below, on MISMATCH or any other
verdict. Do not "finish the job" by wiring a refusal off it.

Spec backlink: DoE-claude
  coordinator/hooks/scripts/guard-doctrine-surface-edits.py
Spec backlink (C4 addendum): pln-a-ceremony-must-not-be-able-to-5e9421
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import resolve_override_keys_doc_display
from coordinator_core.repo_identity_gate import compute_repo_identity_gate
from coordinator_core.session.identity import resolves_em_audience
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards._sentinel_write_guard import (
    extract_target_path,
    sentinel_write_denial,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 127

#: Generator-provenance declaration (coordinator_core/ops/generator_provenance.py).
#: This module's only write is _write_repo_identity_advisory_log()'s best-effort
#: append to <repo_root>/.git/coordinator-sessions/<session_id>/repo-identity-gate.log
#: -- inside .git/, never a tracked repo artifact (see DR-277, read-only/advisory).
GENERATES = []

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_SENTINEL_NAME = ".coordinator-doctrine-edit-approved"
_APPROVAL_WINDOW_SECONDS = 30 * 60


def _norm(path: str) -> str:
    """Resolve to an absolute, symlink-free real path."""
    return os.path.realpath(os.path.abspath(path))


def _git_root() -> "str | None":
    """Repo root via the shared memoized resolver
    (`coordinator_core.write_guards._repo_root.resolve_repo_root`), which
    delegates to `coordinator_core.git.repo_root.show_toplevel` -- see that
    module for the walk-first/spawn-fallback resolution and per-process memo
    policy.

    Any failure (not a git repo, git missing, timeout) returns None; the
    caller falls back to the HOME-only protected-path check and treats the
    approval sentinel as absent (deny), per this guard's fail-closed
    posture.
    """
    try:
        return resolve_repo_root() or None
    except Exception:
        return None


def _home_claude_md() -> "str | None":
    try:
        home = os.path.expanduser("~")
        if not home or home == "~":
            return None
        return _norm(os.path.join(home, ".claude", "CLAUDE.md"))
    except Exception:
        return None


def _protected_paths(repo_root: "str | None") -> "list[str]":
    paths: "list[str]" = []
    home_claude_md = _home_claude_md()
    if home_claude_md:
        paths.append(home_claude_md)
    if repo_root:
        try:
            paths.append(_norm(os.path.join(repo_root, "global-doctrine", "CLAUDE.md")))
            paths.append(
                _norm(
                    os.path.join(
                        repo_root, "coordinator", "snippets", "em-operating-doctrine.md"
                    )
                )
            )
            paths.append(_norm(os.path.join(repo_root, "CLAUDE.md")))
            paths.append(_norm(os.path.join(repo_root, "coordinator.local.md")))
        except Exception:
            pass
    return paths


def _sentinel_state(repo_root: "str | None") -> str:
    """Returns "allow", "deny-absent", or "deny-expired".

    No repo root resolvable -> "deny-absent" (fail closed on the guard's
    own resolution failure, per this guard's deliberate fail-closed
    posture -- see module docstring).

    A DIRECTORY at the sentinel path is treated identically to an ABSENT
    sentinel (2026-07-30 forge-closure fix, ported from the DoE-side
    source). `os.path.getmtime` succeeds on a directory exactly as it does
    on a regular file, so `mkdir <sentinel>` used to read as a real,
    honoured approval -- this guard's read side never actually checked
    that the path was a *file*. `os.path.isfile()` follows symlinks (a
    symlink to a regular file the PM created still approves,
    deliberately) but returns False for a directory, fifo, socket, or
    device, all of which now fall into the same "deny-absent" bucket a
    missing sentinel does.
    """
    if not repo_root:
        return "deny-absent"
    sentinel_path = os.path.join(repo_root, _SENTINEL_NAME)
    try:
        if not os.path.isfile(sentinel_path):
            return "deny-absent"
        mtime = os.path.getmtime(sentinel_path)
    except OSError:
        return "deny-absent"
    except Exception:
        return "deny-absent"
    age = time.time() - mtime
    if age > _APPROVAL_WINDOW_SECONDS:
        return "deny-expired"
    return "allow"


def _local_config_path(repo_root: "str | None") -> "str | None":
    if not repo_root:
        return None
    try:
        return _norm(os.path.join(repo_root, "coordinator.local.md"))
    except Exception:
        return None


def _why_protected(is_local_config: bool) -> str:
    """The class-specific first sentence of the deny message.

    Split per the class-1/class-2 distinction in the module docstring: the
    generic always-loaded-doctrine wording is FALSE of coordinator.local.md
    and reads as such to anyone who has opened the file.
    """
    if is_local_config:
        return (
            "is this repo's privileged configuration — its frontmatter holds "
            "the command strings the ceremony machinery executes "
            "(fast_test_cmd, full_test_cmd, *_post_command) and the "
            "declarations that discharge the Tier-U authority check for them "
            "(fast_tier_unscoped_reason, fast_tier_shape). An EM editing "
            "these could narrow a cadence gate and, in the same edit, keep "
            "the declaration authorizing the narrowed command — which is "
            "self-approval, not configuration"
        )
    return (
        "is always-loaded doctrine — it reaches every session and every "
        "dispatched agent in the fleet, so a change here is not a unilateral "
        "edit the EM can make on its own"
    )


def _deny_reason(
    target: str,
    is_local_config: bool = False,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Audience-gated render — see module docstring "Deny message".

    Subagent/unresolved audience: REFUSAL only, no unlock statement in any
    shape. Positively-resolved EM (`resolves_em_audience`): the REFUSAL plus
    a doc pointer, and nothing else — no key, no path, no command.
    """
    base = (
        "[doctrine-surface guard] BLOCKED: "
        f"{target} {_why_protected(is_local_config)}. Describe the proposed "
        "change to the PM and ask them to approve it before editing this "
        "file. Consider whether the content belongs in a wiki page or a "
        "skill surface instead — those need no approval."
    )
    if resolves_em_audience(payload, _git_root()):
        base += (
            f" See {resolve_override_keys_doc_display()} for how "
            "approval works."
        )
    return base


def _write_repo_identity_advisory_log(
    repo_root: "str | None", session_id: str, gate_result: Dict[str, Any]
) -> None:
    """Best-effort, non-blocking record of C1's `compute_repo_identity_gate`
    verdict (plan `docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md`
    § C4).

    **DR-277 (`docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md`):
    this guard is advisory-by-default and clears no hard-deny carve-out for
    the repo-identity gate, so the verdict computed here is recorded ONLY —
    it is NEVER consulted by `check()`'s allow/deny decision, on MISMATCH or
    on any other verdict.** Do not "finish the job" by adding a refusal path
    off `gate_result["verdict"]`; that would harden this advisory guard into
    exactly the kind of unratified block DR-277 forbids. If a hard block on
    repo identity is ever wanted here, it needs its own named DR-277
    carve-out, not a quiet upgrade of this log call.

    Wrapped so any failure (including no resolvable repo root or session id)
    can NEVER flip the ALLOW/DENY decision — mirrors
    `block_subagent_plan_body_write._write_block_log`'s `|| true` posture.
    """
    if not repo_root or not session_id:
        return
    try:
        from datetime import datetime, timezone

        log_dir = Path(repo_root) / ".git" / "coordinator-sessions" / session_id
        # NEVER mkdir here. This used to be `mkdir(parents=True,
        # exist_ok=True)`, which let an ADVISORY log line MINT a session
        # directory for whatever `session_id` it was handed — including test
        # fixture ids exercising this guard against the real repo root. Nine
        # such dirs (`sess-1`, `sess-abc`, `test-session-abc123`, the
        # `sess-msys-*` dispatcher slugs) had accumulated in this repo's real
        # hub by 2026-08-19, and `liveness.live_session_ids` enumerates every
        # non-denylisted child as a SESSION — so an advisory write was
        # manufacturing phantom sessions into the corpus that claim
        # attribution and scope computation both read.
        #
        # A real session's directory is created by `core.init`. If it does not
        # exist, there is no session here to annotate and the correct action
        # is to drop the line — an advisory log has no business creating
        # session state. Pinned by
        # `test_advisory_log_never_creates_a_session_dir`.
        if not log_dir.is_dir():
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_dir / "repo-identity-gate.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                f"{ts} | gates.repo_identity | verdict={gate_result['verdict']} | "
                f"{gate_result['message']}\n"
            )
    except OSError as exc:
        print(
            "guard_doctrine_surface_edits: repo-identity advisory-log write "
            f"failed (decision unaffected): {exc}",
            file=sys.stderr,
        )


def _sentinel_write_deny_reason() -> str:
    return (
        "[doctrine-surface guard] BLOCKED: this file is the PM's approval to "
        "edit doctrine; you creating it would be self-approval. Ask the PM to "
        "approve and create it. Deleting it (re-locking) is still allowed."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the doctrine-surface guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Mirrors the
    source `main()` control flow exactly, minus the stdin/stdout plumbing
    the engine already handles.
    """
    if payload.get("tool_name", "") not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return None

    try:
        target = _norm(target_raw)
    except Exception:
        return None

    repo_root = _git_root()

    # C4 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md):
    # record C1's repo-identity gate verdict as an advisory
    # `gates.repo_identity` fact. Read-only, ADVISORY ONLY — see the
    # DR-277 note in `_write_repo_identity_advisory_log`'s docstring. The
    # verdict is NEVER read again below and never participates in the
    # allow/deny decision this function returns.
    session_id = payload.get("session_id") or ""
    if repo_root:
        repo_identity_gate = compute_repo_identity_gate(Path(repo_root), session_id or None)
        _write_repo_identity_advisory_log(repo_root, session_id, repo_identity_gate)

    # The sentinel itself is unwritable through the file-write tools, and this
    # check runs BEFORE the approval lookup below -- deliberately, because
    # consulting approval here would let a valid approval authorize extending
    # itself. Removal stays available via `rm`; only creation is the
    # boundary. Delegated to the shared _sentinel_write_guard helper -- see
    # that module's docstring for the ordering contract this call site
    # relies on.
    denial = sentinel_write_denial(
        target, _SENTINEL_NAME, _sentinel_write_deny_reason(), payload=payload
    )
    if denial is not None:
        return denial

    protected = _protected_paths(repo_root)
    if target not in protected:
        return None

    state = _sentinel_state(repo_root)
    if state == "allow":
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(
                target_raw,
                is_local_config=(target == _local_config_path(repo_root)),
                payload=payload,
            ),
        }
    }
