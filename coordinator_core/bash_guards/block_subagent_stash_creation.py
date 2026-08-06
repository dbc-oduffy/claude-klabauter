"""coordinator_core.bash_guards.block_subagent_stash_creation -- PreToolUse
(Bash) hard-deny guard closing the CREATE-side half of the stash-stack gap
`block_stash_destruction.py` only closes on the UNDO side.

THE HOLE THIS CLOSES. `dispatch.py`'s guard chain already denies a resolved
subagent `git stash pop`/`apply` (`block_subagent_destructive_action.py`'s
`_STASH_DROP_CLEAR_RE`-adjacent branch), and `block_stash_destruction.py`
denies `drop`/`clear` for EVERY caller including the EM. Neither of those
guards -- nor anything else in the chain -- ever denied `git stash push`
(or the bare `git stash` form, which is push) for a subagent. The asymmetry:
a dispatched subagent could always CREATE a stash it was structurally
incapable of undoing, because the only guard standing between it and `pop`
(its own restore path) is a hard deny, and `git stash push` sailed through
untouched.

Confirmed live 2026-08-01 14:08:32: a `coordinator:review-integrator`
subagent ran `git stash push` while reporting a "scoped push on 3 files" and
instead created `stash@{0}` as a bare `WIP on <branch>` stash holding 60
files -- a shared working tree carries every concurrent session's
uncommitted work, and an unqualified `git stash push` (or one whose
pathspec silently failed to scope, as here) captures ALL of it, not just
the caller's own. Its own `git stash pop` was then denied by the sibling
identity-gated guard (correctly -- that guard exists for exactly this
reason), and it recovered by hand via `git show stash@{0}:<path> > <path>`
for only the three paths it knew about; the other 57 files survived by
luck, not by any guard.

WHY THIS IS A SEPARATE MODULE, NOT AN EXTRA BRANCH ON
`block_subagent_destructive_action.py`. Same registration-adjacency
argument `block_stash_destruction.py`'s own docstring already makes for its
own existence: this module's classification (a single second-level `git
stash` subcommand test) is small, self-contained, and independent of that
guard's Layer-1/Layer-2 machinery -- folding it in would grow an already
large module for a change that does not touch its logic.

IDENTITY-GATE POSTURE -- DELIBERATELY THE OPPOSITE OF `block_stash_
destruction.py`'s (fail-open, EM exempt AND non-identity-gated at all) and
MODELED ON `block_subagent_commit.py`'s instead, not on `block_subagent_
destructive_action.py`'s type-routed one. Three distinct postures exist in
this package and picking the wrong one here would either re-open the hole
or block the EM:

  1. `block_subagent_destructive_action.py` (UNDO side, `pop`/`apply`) --
     resolves agent_id AND resolves a subagent KIND, and fails OPEN (allow)
     both when no `agent_id` is present at all AND when a present
     `agent_id` fails to resolve a kind. Correct there because that guard
     routes to per-kind messaging and forward-paths.
  2. `block_stash_destruction.py` (`drop`/`clear`) -- not identity-gated at
     all; fires for every caller, EM included, because the EM is the MORE
     dangerous caller for an irreversible drop (see that module's own "WHY
     THE EM IS THE MORE DANGEROUS CALLER HERE"). This is `drop`/`clear`
     ONLY -- the EM-side coverage for `push`/bare `stash` (this module's own
     CREATE shape), plus `reset --hard`/`checkout .`/`restore .`, is a
     DIFFERENT pair of non-identity-gated guards, `dispatch_checks.check_destructive_git_revert`
     (hard deny) and `dispatch_checks.check_destructive_git_revert_advisory`
     (split 2026-08-05, Review: staff-eng Finding 0, into separate
     CONFINEMENT_DENY and ADVISORY_REWRITE chain positions so an advisory
     can never shadow a hard deny): the hard-deny leg denies a sweep
     touching a load-bearing or peer-claimed path, and separately denies
     outright (no path classification) whenever its `git status` oracle
     call times out; the advisory leg fires a non-blocking nudge otherwise.
  3. THIS MODULE (`push`/bare, i.e. CREATE) -- identity-gated, EM exempt,
     but unlike (1) does NOT fail open on a present-but-unresolvable
     `agent_id`. The EM/subagent discriminant is the RAW presence of the
     `agent_id` field in the payload alone (harness-supplied, not settable
     by the caller being constrained) -- exactly `block_subagent_commit.
     py`'s own discriminator ("presence of the RAW field... is the
     EM/subagent discriminator: 'is this a subagent at all' cannot fail the
     way 'what KIND of subagent' can"). No `agent_type`/`subagent_type`
     resolution is needed here at all: every subagent kind is denied
     equally (there is no per-kind allowlist the way `block_subagent_
     commit.py` reserves one for a future named persona), so there is
     nothing left to resolve once `agent_id` is known present. `agent_id`
     ABSENT is the sole, harness-supplied allow signal -- the main-loop EM
     is the only caller that shape describes. This is the correct posture
     for a CREATE-side guard specifically because failing open here (like
     (1) does for the undo side) would silently re-admit the exact hole
     this module exists to close: an `agent_id` present but unresolvable to
     a kind is still, unambiguously, "not the EM".

DESIGN AS AN OFFER, NOT A NAG. Per the project's agent-facing-tooling
convention, the deny message below LEADS with the two non-shared-tree
alternatives (`git archive <sha> | tar -x -C <tmpdir>` for a whole-tree
baseline, `git show <sha>:<path>` for a single file) before explaining the
refusal -- a subagent almost never actually wants a stash, it wants a
diffable/measurable baseline, and both alternatives get it one without
touching the shared worktree at all.

SCOPE: `push` (explicit) and bare `git stash` / the flag-only implicit-push
form (`git stash -u`), matching the sibling guard's own "bare `git stash`
is an implicit push" framing. Every other second-level subcommand (`list`,
`show`, `pop`, `apply`, `drop`, `clear`, `branch`, `create`, `store`,
`save`) is OUT OF SCOPE for this guard -- `pop`/`apply` are `block_
subagent_destructive_action.py`'s to deny, `drop`/`clear` are `block_stash_
destruction.py`'s, and the rest never create or destroy a stack entry this
guard's own incident is about.

Tokenization, shell-shape handling (env assignments, passthrough wrappers,
`sh -c` unwrapping, `;`/`&&`/`|` chaining, heredoc-body stripping,
command-position discipline) is REUSED verbatim from `block_stash_
destruction.py` (`_skip_leading_env_assignments`, `_STASH_WORD_RE`) and, one
level further, from `block_subagent_destructive_action.py` (`_tokenize_
full_command`, `_segments_from_tokens`, `_real_git_subcommand`, `_strip_
heredoc_bodies`, ...) -- never re-implemented. See `block_stash_destruction.
py`'s own module docstring for the bypass history each of those closes.

OVERRIDE -- NONE at this layer, matching `block_stash_destruction.py`'s own
"OVERRIDE -- NONE at this layer, by design": any `COORDINATOR_OVERRIDE_*`
escape hatch is settable by the very subagent this guard constrains, and
the forward path (the two alternatives above) already serves every
legitimate need.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, Optional

from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _BUNDLED_C_FLAG_RE,
    _normalize_executable_basename,
    _real_git_subcommand,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards.block_stash_destruction import (
    _skip_leading_env_assignments,
    _STASH_WORD_RE,
)

CLASS = "hard-deny"
MATCHERS = ["Bash"]
#: `dispatch.py` hardcodes chain ordering explicitly, so this value governs
#: nothing at runtime; matches the sibling identity-gated hard-denies
#: (`block_subagent_commit`, `block_subagent_destructive_action`) it is
#: registered near.
PRIORITY = 40

#: Shell interpreters whose `-c <string>` argument is executed, not inert
#: text -- reused verbatim from `block_stash_destruction.py`'s own constant
#: (not re-exported there, so re-declared identically here; keep in sync if
#: that module's own set ever changes).
_C_FLAG_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

#: The single plain word immediately following (whitespace-separated) a
#: `stash` match position -- used only by the narrow legacy fallback below.
_NEXT_WORD_AFTER_RE = re.compile(r"\s+(\S+)")


def _classify_stash_subcommand(second: Optional[str]) -> Optional[str]:
    """Return a deny_kind label for the second-level `git stash` subcommand
    `second` (the token immediately following `stash`), or `None` (allow).

    `second is None` (bare `git stash`) and a leading-flag-only shape
    (`git stash -u`) are both the IMPLICIT push form -- git creates a stash
    entry either way. `second == "push"` is the explicit form. Every other
    recognized or unrecognized token is OUT OF SCOPE for this guard (see
    module docstring "SCOPE") and allows here.
    """
    if second is None:
        return "git stash"
    if second.startswith("-"):
        return "git stash " + second
    if second == "push":
        return "git stash push"
    return None


def _evaluate_legacy(text: str) -> Optional[str]:
    """Narrow free-text fallback, mirroring `block_stash_destruction.py`'s
    own `_evaluate_legacy` -- only ever called on text already known (via
    the cheap `_STASH_WORD_RE` pre-filter in `check()`) to contain a `stash`
    word, and only on a segment the shared tokenizer could not parse or
    that `_real_git_subcommand` reported as globally ambiguous.
    """
    for m in _STASH_WORD_RE.finditer(text):
        nxt = _NEXT_WORD_AFTER_RE.match(text, m.end())
        second = nxt.group(1).rstrip(";&|") if nxt else None
        verdict = _classify_stash_subcommand(second)
        if verdict is not None:
            return verdict
    return None


def _evaluate(cmd: str) -> Optional[str]:
    """Primary classification -- identical segment-walking shape to
    `block_stash_destruction.py`'s own `_evaluate`, differing only in which
    second-level subcommand set is denied (see module docstring "SCOPE").
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return _evaluate_legacy(cmd)

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue

        working = _strip_leading_subshell_and_env(seg_tokens)
        working = _skip_leading_env_assignments(working)
        if not working:
            continue

        head_base = _normalize_executable_basename(working[0])
        if head_base in _C_FLAG_SHELL_INTERPRETERS:
            c_flag_positions = [
                i for i in range(1, len(working)) if _BUNDLED_C_FLAG_RE.match(working[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(working):
                    verdict = _evaluate(working[idx + 1])
                    if verdict is not None:
                        return verdict
                    continue

        if head_base != "git":
            continue

        subcmd, ambiguous, remaining = _real_git_subcommand(working[1:])
        if ambiguous:
            seg_text = " ".join(shlex.quote(t) for t in seg_tokens)
            verdict = _evaluate_legacy(seg_text)
            if verdict is not None:
                return verdict
            continue

        if subcmd != "stash":
            continue

        second = remaining[0] if remaining else None
        verdict = _classify_stash_subcommand(second)
        if verdict is not None:
            return verdict

    return None


def _deny_reason(cmd: str, deny_kind: str) -> str:
    cmd_safe = cmd if len(cmd) <= 200 else cmd[:200] + "..."
    return (
        "Did you mean one of these instead? Neither touches the shared "
        "worktree:\n"
        "  - Whole-tree baseline:  git archive <sha> | tar -x -C <tmpdir>\n"
        "  - Single file:          git show <sha>:<path>\n\n"
        "BLOCKED: `%s` creates a stash entry, and on a shared working tree "
        "a stash is GLOBAL -- it sweeps every concurrent session's "
        "uncommitted work, not just yours, and you have no sanctioned way "
        "to undo it (`git stash pop`/`git stash apply` are denied for you).\n\n"
        "  Command:  %s\n\n"
        "Confirmed live: a subagent ran what it reported as a scoped push "
        "on 3 files and instead created a bare 60-file stash holding a "
        "concurrent peer session's review-trail records, sidecars, and "
        "lessons -- its own `git stash pop` was then denied, and only 3 of the 60 "
        "files were recovered by hand.\n\n"
        "A subagent almost never actually needs a stash -- it needs a "
        "clean baseline to diff or measure against, which the two "
        "alternatives above give you without ever touching the shared "
        "tree."
    ) % (deny_kind, cmd_safe)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the stash-CREATION gate against a PreToolUse payload.

    Identity-gated to subagents only -- see module docstring "IDENTITY-GATE
    POSTURE" for why this guard's discriminator (raw `agent_id` presence
    alone) deliberately does NOT fail open on an `agent_id` present but
    unresolvable to a kind, unlike its undo-side sibling.
    """
    # Deliberately no try/except -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards.
    if (payload.get("tool_name") or "") != "Bash":
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    cmd_for_classification = _strip_heredoc_bodies(cmd)

    if not _STASH_WORD_RE.search(cmd_for_classification):
        return None

    # EM/subagent discriminator -- raw presence of `agent_id`, not whether
    # it canonicalizes (see module docstring "IDENTITY-GATE POSTURE"). No
    # further kind-resolution is needed: every subagent kind is denied
    # equally, so an `agent_id` present but unresolvable to a kind still
    # denies here rather than falling open.
    raw_agent_id = payload.get("agent_id")
    if not raw_agent_id:
        return None

    deny_kind = _evaluate(cmd_for_classification)
    if deny_kind is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(cmd, deny_kind),
        }
    }
