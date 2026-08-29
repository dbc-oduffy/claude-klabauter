"""coordinator_core.bash_guards.guard_repo_setup_claude_home_refusal --
PreToolUse(Bash|PowerShell) hard-deny guard making repo-setup's "never
target ~/.claude" precondition executable rather than prose.

Port of DoE-claude's ``coordinator/hooks/scripts/guard-repo-setup-claude-
home-refusal.py``, one of the four folded PreToolUse(Bash) guards
`preuse-bash-dispatch.py` runs in-process ahead of the engine dispatch (see
`docs/plans/2026-08-28-the-four-folded-bash-guards-get-registered-not-
folded.md`) -- this module ports the LOGIC into the engine's own registered
guard chain (`dispatch.py`) so DoE's in-process fold can be deleted without
the cold path losing coverage. Same predicate, same deny text; only the
transport differs -- DoE's `main()`/stdin/exit-code wrapper is replaced by
this package's `check(payload) -> Optional[dict]` contract.

THE DEFECT THIS CLOSES: ``coordinator/skills/repo-setup/SKILL.md`` line 37
already NAMES ``~/.claude`` (Claude Central, the ``example-doctrine-mirror-repo-v3`` backup
tree) as a non-target root -- but that line is prose the orchestrating agent
reads and can misapply, and ~/.claude carried a stray repo-setup scaffold as
a result. ``coordinator:new-project`` delegates its own onboarding half back
to this same skill, so one guard at this seam covers both entry points
without a second, drifting copy of the same predicate. The specific incident
is single-sourced to DoE's own cross-repo memo prose
(`cross-repo/inbox/2026-08-27-doe-claude-em-rehome-four-bash-guards-onto-
the-guard-chain.md`) and has not been independently verified in THIS repo;
this port's rationale rests only on the general hazard the incident
illustrates -- a prose-only precondition is not an enforced one -- not on
that specific account being established fact here.

SEAM CHOICE. The actual scaffold-writing mechanism
(``coordinator_core.install.scaffold_structure``, invoked via
``python3 -m coordinator_core.install.scaffold_structure``) and the
target-root resolver it depends on
(``"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/
repo-setup-args-and-register" resolve-target-root``) are BOTH engine-plane
code living in THIS repo -- unlike DoE's own copy (authored DoE-resident
because DoE holds no cross-repo commit grant over engine code), this port
lives directly alongside the mechanism it protects, in the SAME registered
guard chain every other confinement-deny guard runs through.

DETECTION STRATEGY. Fires only when the command text names one of the
scaffold-mechanism CLIs (``repo-setup-args-and-register`` or
``coordinator_core.install.scaffold_structure`` / ``scaffold_structure``).
The candidate target root is: an explicit ``--root``/``--target`` flag value
if the command carries one (resolved against the payload's ``cwd`` if
relative), else a leading ``cd``/``Set-Location`` prefix's target (resolved
against ``cwd`` if relative), else the payload's own ``cwd`` (mirrors
SKILL.md's own "$_TARGET_ROOT ... defaults to $(pwd)" resolution rule).
PATH-SHAPE ROBUSTNESS: never a string compare. The candidate and Claude Home
are both resolved to real, canonical, case-normalized paths via
``pathlib.Path.resolve()`` (which follows NTFS junctions on Windows since
Python 3.8, unlike ``os.path.islink()`` -- ``~/.claude/machine-local`` is
one such junction and must never be mistaken for "not a link, so not worth
resolving") before comparison.

NEGATIVE-SPEC -- ``--dry-run`` is exempt. The scaffold CLI's dry run prints
its plan and writes nothing, so there is no write to refuse; the
`coordinator-doctor` P-12 probe reads Claude Home's structure through
exactly that flag. Dropping the flag denies the command again, which is the
whole safety argument -- this is a no-write exemption, not an escape hatch.

CLAUDE HOME RESOLUTION -- never ``os.path.expanduser`` naively, which
ignores a monkeypatched ``HOME`` in a way that has clobbered a real
``.doe-root`` in this repo family's own install history. Resolution order,
explicit and testable via an injected env mapping: ``CLAUDE_CONFIG_DIR``
(if set, IS the Claude Home) -> ``HOME`` (POSIX) -> ``USERPROFILE``
(Windows), each joined with ``.claude`` for the latter two. No fallback to
``Path.home()`` -- an unresolvable Claude Home means this guard has nothing
to compare against, so it fails OPEN (allow) rather than guessing.

ENV RESOLUTION -- per-call ``payload["env"]`` preferred over ambient
``os.environ`` (mirrors ``dispatch_checks._override``'s C14c re-keying):
this guard may run inside a warm, long-lived dispatch server whose own
``os.environ`` is frozen at server start and shared across every session on
the box, so resolving Claude Home from that server's ambient env rather than
the calling session's own would silently misclassify every caller. A
``payload`` absent, not a dict, or carrying no ``env`` mapping falls back to
``os.environ`` unchanged.

LEADING ``cd``/``Set-Location`` HANDLING. When no ``--root``/``--target``
flag is present, a leading ``cd <path> &&``/``cd <path> ;`` (or PowerShell
``Set-Location``/``sl``, optionally with ``-Path``) prefix is treated as the
effective cwd for candidate resolution instead of the payload's own ``cwd``,
mirroring what the shell would actually do before the scaffold command
runs. Handles a quoted or unquoted path, ``~`` and
``$HOME``/``${HOME}``/``%USERPROFILE%``/``$env:USERPROFILE`` shorthand, and
both ``&&`` and ``;`` separators. Only a SINGLE leading ``cd`` is honored
(the command must start with it, modulo leading whitespace) -- a `cd`
appearing later in a `;`-chained command, or a second `cd` after the first,
is out of scope; see NEGATIVE-SPEC below.

NEGATIVE-SPEC -- ``--batch`` mode is deliberately UNHANDLED. Batch mode
reads repo paths from ``~/.claude/working-repos.yaml`` at runtime and loops
the single-repo flow per listed repo; this guard cannot see that list
without executing the very command it is trying to gate, so a
``--batch``-shaped command is never denied by this guard on the strength of
its own command text alone. Closing that gap is a ``working-repos.yaml``
data-hygiene concern, not a command-classification one.

NEGATIVE-SPEC -- only ONE leading ``cd``/``Set-Location`` is honored. A
command chaining a SECOND ``cd`` after the first, or one buried mid-command
rather than at the very start, is not walked -- the candidate resolves
against the FIRST ``cd``'s target only. Closing that fully would mean
simulating shell cwd-tracking across the whole command, which is out of
scope for a cheap pre-execution text classifier.

Contract (matches every other ``check(payload) -> Optional[dict]`` module in
this package): returns ``None`` (allow) or the nested hard-deny envelope.
Deliberately no try/except at the top level of ``check`` itself -- fail-
CLOSED-on-exception for a hard-deny entry is the dispatcher's own job
(``dispatch.py``'s ``guard_chain`` routes an uncaught exception in a
``fail_closed=True`` entry through its crash-deny wrapper); this module's
own internal resolution helpers each fail open on their own narrow
uncertainty (unresolvable Claude Home, unresolvable candidate path) per
their own docstrings, matching DoE's original fail-open posture for those
specific cases.

Spec backlink: state/bug-backlog/2026-08-15-repo-setup-scaffolded-claude-as-
a-projec-7439cdca3aa3.yaml (DoE-claude); cross-repo/inbox/2026-08-27-doe-
claude-em-rehome-four-bash-guards-onto-the-guard-chain.md
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: Identifiers naming the engine-plane scaffold mechanism (see module
#: docstring "SEAM CHOICE"). A bare substring test would deny a command that
#: merely MENTIONS one of these strings (a `grep scaffold_structure ...`, a
#: `git log --grep=...`) without invoking it -- `_names_scaffold_mechanism`
#: below requires the marker to appear in an INVOKED-program-ish position,
#: not merely anywhere in the text.
_SCAFFOLD_MECHANISM_MARKERS = (
    "repo-setup-args-and-register",
    "coordinator_core.install.scaffold_structure",
    "scaffold_structure",
)

#: ``--root <val>`` / ``--target <val>`` (also ``--root=val``), tolerating a
#: single- or double-quoted value. Mirrors SKILL.md's own documented flag
#: pair (``--root``, alias ``--target``).
_ROOT_FLAG_RE = re.compile(r"--(?:root|target)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")

#: ``--dry-run`` is the scaffold CLI's own no-write mode: it prints the
#: ``create``/``skip (exists)`` plan and touches nothing. This guard exists
#: to keep a WRITE off Claude Home, so a dry run has nothing to refuse -- and
#: the `coordinator-doctor` P-12 probe reads Claude Home's structure through
#: exactly that flag. NEGATIVE-SPEC: this is a no-write exemption, never a
#: bypass -- drop the flag and the write is denied again.
_DRY_RUN_RE = re.compile(r"(?:^|\s)--dry-run(?:[=\s]|$)")

#: A leading ``cd <path> &&``/``cd <path> ;`` or PowerShell
#: ``Set-Location``/``sl`` (optionally ``-Path``) prefix -- see module
#: docstring "LEADING cd/Set-Location HANDLING". Must anchor the START of
#: the command (modulo leading whitespace); only ONE such prefix is
#: recognized (see NEGATIVE-SPEC).
_LEADING_CD_RE = re.compile(
    r"""^\s*(?:cd|Set-Location|sl)\s+(?:-Path\s+)?
        ("[^"]*"|'[^']*'|\S+)
        \s*(?:&&|;)""",
    re.IGNORECASE | re.VERBOSE,
)


def _resolve_env(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Per-call env mapping -- payload's own ``env`` preferred over ambient
    ``os.environ``. See module docstring "ENV RESOLUTION"."""
    if isinstance(payload, dict):
        candidate = payload.get("env")
        if isinstance(candidate, dict):
            return candidate
    return dict(os.environ)


def _resolve_claude_home(env: Dict[str, str]) -> Optional[str]:
    """Canonical, resolved path to Claude Home, or ``None`` if unresolvable.

    Never ``os.path.expanduser`` -- see module docstring. Order: an explicit
    ``CLAUDE_CONFIG_DIR`` IS the Claude Home; otherwise ``HOME``/
    ``USERPROFILE`` joined with ``.claude``."""
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        try:
            return str(Path(config_dir).resolve())
        except OSError:
            pass
    for key in ("HOME", "USERPROFILE"):
        val = env.get(key)
        if not val:
            continue
        try:
            return str((Path(val) / ".claude").resolve())
        except OSError:
            continue
    return None


def _expand_home_shorthand(raw: str, env: Dict[str, str]) -> str:
    """Expand a literal leading ``~`` or ``$HOME``/``${HOME}``/``%USERPROFILE%``
    token in ``raw`` against ``env`` -- this guard inspects the command text
    BEFORE any shell ever runs it, so a POSIX shell's own tilde/variable
    expansion has not happened yet by the time ``tool_input.command`` is
    read. Falls back to ``raw`` unchanged when the referenced variable is
    absent from ``env``, or no such shorthand is present."""
    home = env.get("HOME") or env.get("USERPROFILE")
    if raw.startswith("~") and home:
        return home + raw[1:]
    for token in ("${HOME}", "$HOME", "%USERPROFILE%", "$env:USERPROFILE"):
        if raw.startswith(token) and home:
            return home + raw[len(token):]
    return raw


def _names_scaffold_mechanism(cmd: str) -> bool:
    """True iff ``cmd`` invokes (not merely mentions) one of
    ``_SCAFFOLD_MECHANISM_MARKERS``. A marker occurrence counts as "invoked"
    when it sits at the very start of the command, immediately follows a
    ``python3 -m``/``python -m`` module flag, or is preceded by a quote or a
    path separator (i.e. it names a program/module/path being RUN). A marker
    that appears only as plain text elsewhere -- a ``grep`` pattern, a
    ``--grep`` value, a comment -- does not count, so a command that merely
    mentions the marker string is not ruled IN for the (denying) path
    classification below."""
    for marker in _SCAFFOLD_MECHANISM_MARKERS:
        start = 0
        while True:
            idx = cmd.find(marker, start)
            if idx == -1:
                break
            if idx == 0:
                return True
            prefix = cmd[:idx]
            if prefix[-1] in "\"'/\\":
                return True
            if re.search(r"-m\s+$", prefix):
                return True
            start = idx + 1
    return False


def _leading_cd_target(cmd: str, cwd: Optional[str], env: Dict[str, str]) -> Optional[str]:
    """The effective cwd after a leading ``cd``/``Set-Location`` prefix, or
    ``None`` if ``cmd`` doesn't open with one -- see module docstring
    "LEADING cd/Set-Location HANDLING"."""
    match = _LEADING_CD_RE.match(cmd)
    if not match:
        return None
    raw = _expand_home_shorthand(match.group(1).strip("'\""), env)
    candidate = Path(raw)
    if not candidate.is_absolute() and cwd:
        candidate = Path(cwd) / candidate
    return str(candidate)


def _extract_candidate_root(cmd: str, cwd: Optional[str], env: Dict[str, str]) -> Optional[str]:
    """The path this command would resolve ``$_TARGET_ROOT`` to, per
    SKILL.md's own "Target-root resolution" preamble: an explicit
    ``--root``/``--target`` value (resolved against ``cwd`` if relative);
    else a leading ``cd``/``Set-Location`` prefix's target (resolved against
    ``cwd`` if relative -- see ``_leading_cd_target``); else ``cwd`` itself
    when neither is present."""
    match = _ROOT_FLAG_RE.search(cmd)
    if match:
        raw = _expand_home_shorthand(match.group(1).strip("'\""), env)
        candidate = Path(raw)
        if not candidate.is_absolute() and cwd:
            candidate = Path(cwd) / candidate
        return str(candidate)
    cd_target = _leading_cd_target(cmd, cwd, env)
    if cd_target is not None:
        return cd_target
    return cwd


def is_denied_repo_setup_claude_home(
    cmd: str, cwd: Optional[str], env: Dict[str, str]
) -> bool:
    """The whole predicate, isolated from payload plumbing so it is directly
    unit-testable. Returns True (deny) iff ``cmd`` invokes the scaffold
    mechanism AND its resolved candidate target root is Claude Home."""
    if not _names_scaffold_mechanism(cmd):
        return False

    if _DRY_RUN_RE.search(cmd):
        return False  # no-write mode -- nothing to refuse; see _DRY_RUN_RE

    claude_home = _resolve_claude_home(env)
    if not claude_home:
        return False  # cannot resolve what to compare against -- fail open

    candidate = _extract_candidate_root(cmd, cwd, env)
    if not candidate:
        return False  # no cwd and no explicit flag -- nothing to compare

    try:
        resolved_candidate = str(Path(candidate).resolve())
    except OSError:
        return False  # unresolvable candidate path -- fail open

    return resolved_candidate == claude_home


def _deny_reason() -> str:
    return (
        "BLOCKED: repo-setup's scaffold cannot target ~/.claude -- it is not a "
        "working tree. Run repo-setup against the project clone you mean to set "
        "up: /repo-setup --root <path-to-that-clone>."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the repo-setup-Claude-Home-refusal gate against a PreToolUse
    payload. Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller.

    Deliberately no try/except here -- fail-CLOSED-on-exception is the
    dispatcher's job for hard-deny guards; the internal resolution helpers
    each fail open on their own narrow uncertainty per their own docstrings.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    env = _resolve_env(payload)

    if not is_denied_repo_setup_claude_home(cmd, cwd, env):
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(),
        }
    }
