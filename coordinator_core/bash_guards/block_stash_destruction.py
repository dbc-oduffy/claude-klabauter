"""coordinator_core.bash_guards.block_stash_destruction -- PreToolUse(Bash)
hard-deny guard closing the `git stash drop`/`git stash clear` main-loop gap.

Why this guard exists alongside `block_subagent_destructive_action.py`: that
guard already classifies `git stash drop`/`clear` as a deny (its
`_STASH_DROP_CLEAR_RE` branch, with a fully-written forward path in its
deny-reason table) -- but it is IDENTITY-GATED to resolved subagents only.
Its Layer-2 gate resolves `agent_id`/back-pointer `subagent_type` and fails
OPEN -- allows -- when no subagent identity resolves, so the main-loop EM is
explicitly exempt: an EM-typed `git stash drop` sails through untouched.
This module is the non-identity-gated leg, same shape and same rationale as
the sibling `block_worktree_creation.py` ("the ban needs a SECOND guard with
no identity gate at all") and `check_raw_pid_liveness.py` ("the anti-pattern
is wrong regardless of who types it").

WHY THE EM IS THE MORE DANGEROUS CALLER HERE, NOT THE LESS. `git stash drop`
acts on a STACK POSITION (`stash@{0}` by default), and on a shared working
tree the entry at position 0 belongs to whichever session pushed last -- not
necessarily the session about to drop it. A subagent at least stashes inside
one narrow dispatch; the EM runs long, interleaves with peer sessions on the
same branch, and accumulates the most stack drift between its own push and
its own drop. Confirmed live 2026-07-30: an EM dropped `stash@{0}` believing
it to be its own path-scoped stash, then discovered the entry described work
from a PEER session's commit. Recovery was possible only because the dangling
commit had not yet been gc'd.

WHY DROP/CLEAR AND NOT POP/APPLY. `drop`/`clear` are the IRRECOVERABLE pair
and the only shapes this guard denies. Unlike a deleted branch, a dropped
stash leaves no reflog entry to walk back to -- `git stash` maintains no
per-entry reflog of its own deletions, so the content survives ONLY as a
dangling commit until the next gc, findable by `git fsck --unreachable` and
by nothing else. `pop`/`apply` are a different (and lesser) failure mode: they
are stack-position-ambiguous too, but a conflicted pop leaves the entry in
place, and `apply` never removes it at all. They remain denied for subagents
by the sibling guard and remain ALLOWED for the EM here -- deliberately, and
not an oversight to "tighten" later: `pop` is the EM's own restore path, and
denying it would leave a stash pushed by the EM with no sanctioned way back.
Note that a SUCCESSFUL `pop` does implicitly drop the entry it applied; that
is accepted, because the EM has just seen the content land in its tree and is
therefore no longer guessing about whose entry it was. The deny message below
names `pop` for exactly this reason.

DELIBERATE ALLOW-LIST. Everything that is not `drop`/`clear`: `list`, `show`,
`push`, `save`, `branch`, `create`, `store`, `apply`, `pop`, and bare `git
stash`. This guard is NOT default-deny on an unrecognized second-level token,
which is the one place it diverges from `block_worktree_creation.py`'s
posture -- and the divergence is required, not stylistic. `git worktree`
always takes a second-level subcommand, so an unknown token there is genuinely
anomalous. `git stash` does not: bare `git stash` and the flag-only implicit-
push form (`git stash -u`) are its most common shapes, and both would deny
under a default-deny rule. Denying the EM's ability to stash at all is not
what this guard is for, and the unscoped-stash concern that DOES apply to
those shapes belongs to `dispatch_checks.check_destructive_git_revert` (hard
deny) and its sibling `dispatch_checks.check_destructive_git_revert_advisory`
(split 2026-08-05, Review: staff-eng Finding 0, into separate CONFINEMENT_DENY
and ADVISORY_REWRITE chain positions so an advisory can never shadow a hard
deny), not this guard's -- the non-identity-gated, EM-side legs (unlike this
module's own sibling `block_subagent_destructive_action.py`, which is
identity-gated to resolved subagents): the hard-deny leg denies a sweep
touching a load-bearing or peer-claimed path, and separately denies outright
(no path classification) whenever its `git status` oracle call times out; the
advisory leg fires a non-blocking nudge (allow + additionalContext, never a
deny) otherwise.

CLASSIFICATION IS `remaining[0]`, NOT A FLAG-SKIPPING SCAN. `git stash`
requires its subcommand in first position -- `git stash -q drop` is not valid
git -- so the token immediately after `stash` is the only one that can be the
verb. A scan that skipped leading flags to find the "real" subcommand would
buy nothing and would false-deny on a flag VALUE that happens to read as a
verb: `git stash push -m drop -- <paths>` is a legitimate scoped push whose
first non-flag token is the literal word `drop`. Positional matching never
sees it.

`remaining[0]` IS STRIPPED OF A LEADING REDIRECTION FIRST (2026-08-23 fix, one
shared helper: `block_subagent_destructive_action._strip_leading_redirection_
tokens`, imported here rather than copied). `shlex`/the PowerShell tokenizer
has no concept of shell redirection -- `git stash 2>&1 drop` tokenizes to
`["2>&1", "drop"]` after `stash`, and reading `remaining[0]` unstripped hands
`_classify_stash_subcommand` `"2>&1"` instead of `"drop"`, which this module's
own DELIBERATE ALLOW-LIST then allows as unrecognized -- exactly the
irrecoverable case this guard exists to deny. This was the identical shape
already fixed in the two sibling create-side guards; this file's own two
`remaining[0]` sites had not been patched.

NOT a `stash`-substring ban, and specifically not the sibling module's
`_STASH_DROP_CLEAR_RE` (`\bstash\b.*\b(?:drop|clear)\b`). That regex is
correct in its own module, where it runs only as a free-text fallback on a
segment already resolved to a git invocation; used as a primary classifier it
matches any command line containing both words in order -- `git stash push --
src/drop_handler.py`, or a message mentioning a dropped stash. This guard
REUSES the anchored-classifier approach from `block_subagent_destructive_
action.py` (its `_real_git_subcommand`/`_tokenize_full_command`/`_segments_
from_tokens`/`_normalize_executable_basename`, imported directly rather than
re-implemented) so the git subcommand is resolved from argv POSITION.

Shell-shape handling (leading env-var assignments, passthrough wrappers and
their own argv, `sh -c` payload unwrapping, `&&`/`;`/`|` chaining, POSIX
quoting) reuses the SAME helpers as `block_worktree_creation.py` rather than
inventing a second, weaker parser -- see that module's docstring for the
bypass history (BX-13/BX-14) each of those skips closes.

FAIL-CLOSED FALLBACK, narrowly scoped. If the shared tokenizer cannot parse a
segment (unbalanced quoting), or `_real_git_subcommand` reports an
unrecognized-global-option ambiguity for a segment that DOES contain the word
`stash`, this guard falls back to `_evaluate_legacy` -- a free-text search for
a stand-alone `stash` word whose immediately-following plain word is `drop` or
`clear` -- rather than silently allowing. Deliberately narrow: only ever
invoked on text already known (via the cheap `_STASH_WORD_RE` pre-filter in
`check()`) to contain a `stash` word.

HEREDOC BODIES are stdin DATA, never shell command text, and are stripped
before classification via `_strip_heredoc_bodies` (imported from
`block_subagent_destructive_action.py` -- the same helper
`block_worktree_creation.py` and `_sentinel_creation_guard.py` already reuse,
not a new copy). Without this, a document being persisted through a heredoc
whose prose quotes `git stash drop` -- an incident writeup, or this module's
own docstring -- can mis-tokenize into a segment whose head resolves to `git`,
and the guard reads prose as a live invocation. That exact false-deny was
observed on the worktree guard on 2026-07-29; it is pre-empted here rather
than rediscovered. Anti-bypass: an interpreter FED by a heredoc (`bash <<'EOF'
... EOF`) is untouched by the strip, so a real invocation outside the body
still denies.

OVERRIDE -- NONE at this layer, by design. Same reasoning as the sibling
non-identity-gated guards: any `COORDINATOR_OVERRIDE_*` escape hatch is
settable by the very caller being constrained, and a stash entry is cheap to
leave orphaned, so there is no legitimate need that the forward path in the
deny message does not already serve.

APPLY ADVISORY LEG (`check_apply_advisory`, appended 2026-08-30). A SEPARATE
registered leg, ADVISORY_REWRITE band (allow + `additionalContext`), for
`git stash apply` -- not a change to `check`'s own drop/clear hard-deny
above. Confirmed live: a peer session verified "no data loss" after a
stash by running `git stash apply "stash@{0}"`, got a clean no-op, and
filed that as the finding. It was wrong: a clean `apply` proves only that
the OVERLAPPING files matched; it says nothing about content unique to
the stash when the stash's base is behind HEAD, which is exactly the
shape a peer's stashed-then-superseded work takes. The correct read-only
check, `git stash show --name-only stash@{N}`, was reached for by hand
after the fact -- this leg is the artifact that puts it in front of the
caller before the wrong check is typed, per this project's own north
star: name the artifact, not "the operator remembers."

DR-277 CLASS: ADVISORY, deliberately not a third hard-deny alongside
`check`'s drop/clear. `apply` genuinely mutates the tree (unlike `show`),
but the harm from a wrong verification finding is a false conclusion, not
data loss -- the data survives either way, recoverable by hand, same
argument DR-277 requires against a hard deny. Denying `apply` outright
would also remove the EM's own legitimate use of it (restoring a
KNOWN-own entry without popping, e.g. to inspect before deciding whether
to drop) for a case where the tool cannot distinguish "verifying" from
"restoring" intent from the command text alone -- an advisory nudge that
still lets the command through fits both intents; a hard deny would only
fit one. Neither of DR-277's two named hard-deny carve-outs (irrecoverable
action, confinement/identity boundary) applies here: `apply` is
recoverable (module docstring "WHY DROP/CLEAR AND NOT POP/APPLY" above)
and this leg is not identity-gated -- it is a content nudge, not a
boundary.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, Optional

from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _BUNDLED_C_FLAG_RE,
    _normalize_executable_basename,
    _ps_normalize_verb_token,
    _real_git_subcommand,
    _segments_from_tokens,
    _strip_heredoc_bodies,
    _strip_leading_redirection_tokens,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._command_tokenizer import (
    _skip_wrapper_own_argv,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
    strip_powershell_prose_noise,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

# WIDENED 2026-08-19 (C6, docs/plans/2026-08-19-the-held-guard-cohort-
# becomes-dialect-safe.md): the prior `("Bash",)` hold existed because the
# `tokens is None -> _evaluate_legacy(cmd)` free-text fallback is bash-
# shaped and fails CLOSED on unparseable input, and PowerShell's here-
# string/backtick-escape shapes defeat the `shlex`-based tokenizer feeding
# it. The PowerShell leg below (`_evaluate_powershell`) never reaches that
# bash-shaped fallback -- its own `tokens is None` route goes to
# `_evaluate_legacy_powershell`, which strips here-string bodies and quoted
# spans via `_dialect.strip_powershell_prose_noise` (C2) before scanning,
# per Conventions (a). Same cohort, same plan: `block_worktree_creation.py`,
# `block_subagent_stash_creation.py`, `block_subagent_destructive_action.py`.
CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
#: `dispatch.py` hardcodes chain ordering explicitly, so this value governs
#: nothing at runtime; it matches the sibling non-identity-gated guards
#: (`block_worktree_creation`, the two sentinel guards) it is registered
#: alongside.
PRIORITY = 41

#: Cheap pre-filter gating whether the (more expensive) anchored tokenized
#: pass runs at all. A bare word match is sufficient here -- unlike the
#: worktree guard, there is no `--stash` flag anywhere in git's surface that
#: would need excluding by lookbehind.
_STASH_WORD_RE = re.compile(r"\bstash\b")

#: The two irrecoverable second-level `git stash` subcommands -- see module
#: docstring "WHY DROP/CLEAR AND NOT POP/APPLY". Everything else allows.
_DENY_SUBCOMMANDS = frozenset({"drop", "clear"})

#: The single plain word immediately following (whitespace-separated) a
#: `stash` match position -- used only by the narrow legacy fallback below.
_NEXT_WORD_AFTER_RE = re.compile(r"\s+(\S+)")

#: A bare leading `VAR=value` shell assignment token (`GIT_TRACE=1 git stash
#: drop`) -- `_strip_leading_subshell_and_env` only peels a literal `env` word
#: prefix, not a bare assignment, so command-position resolution needs its own
#: skip for the assignment-prefix shape.
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Passthrough wrapper binaries that run their remaining argv unchanged. Same
#: set the sibling guards tolerate; without it `nice git stash drop` resolves
#: its head to `nice` (never `git`) and the drop happens for real while this
#: guard allows.
_PASSTHROUGH_WRAPPERS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
        "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
    }
)

#: Shell interpreters whose `-c <string>` argument is executed, not inert text.
_C_FLAG_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _skip_leading_env_assignments(tokens: "list[str]") -> "list[str]":
    """Return `tokens` with any leading `VAR=value`-shaped tokens AND any
    leading no-op passthrough wrapper tokens (plus that wrapper's OWN
    argument(s), via `_skip_wrapper_own_argv`) removed, exposing the true
    command-position head."""
    i = 0
    n = len(tokens)
    while i < n:
        if _ENV_ASSIGNMENT_RE.match(tokens[i]):
            i += 1
            continue
        base = _normalize_executable_basename(tokens[i])
        if base in _PASSTHROUGH_WRAPPERS:
            i += 1
            i = _skip_wrapper_own_argv(tokens, i, base)
            continue
        break
    return tokens[i:]


def _classify_stash_subcommand(second: Optional[str]) -> Optional[str]:
    """Return a deny_kind label for the second-level `git stash` subcommand
    `second` (the token immediately following `stash`), or `None` (allow).

    `second is None` -- bare `git stash` -- allows: it is an implicit push,
    not a deletion. An unrecognized token also allows; see module docstring
    "DELIBERATE ALLOW-LIST" for why this guard is not default-deny.
    """
    if second is None:
        return None
    if second in _DENY_SUBCOMMANDS:
        return "git stash %s" % second
    return None


def _evaluate_legacy(
    text: str, classify=_classify_stash_subcommand
) -> Optional[str]:
    """Narrow free-text fallback -- see module docstring "FAIL-CLOSED
    FALLBACK, narrowly scoped". Only ever called on text already known to
    contain a `stash` word; never the guard's primary classification path.

    `classify` defaults to `_classify_stash_subcommand` (the drop/clear
    hard-deny set) and is overridden by `check_apply_advisory` below to
    reuse this exact walk for the `apply` advisory leg -- see that
    function's own docstring for why a parametrized classifier, not a
    second copy of this walk, is the right shape for a second leg that
    differs only in WHICH second-level subcommand it is looking for.
    """
    for m in _STASH_WORD_RE.finditer(text):
        nxt = _NEXT_WORD_AFTER_RE.match(text, m.end())
        second = nxt.group(1).rstrip(";&|") if nxt else None
        verdict = classify(second)
        if verdict is not None:
            return verdict
    return None


def _evaluate(cmd: str, classify=_classify_stash_subcommand) -> Optional[str]:
    """Primary classification: tokenize the full command (quote-aware,
    `;`/`&`/`|`-segmented), and for each segment resolve the REAL git
    subcommand from argv position via `_real_git_subcommand`. Falls back to
    `_evaluate_legacy`, scoped to the offending segment text only, on an
    unparseable segment or an unrecognized-global-option ambiguity.

    `classify` -- see `_evaluate_legacy`'s docstring above; threaded through
    unchanged so `check_apply_advisory` gets the identical shell-shape/
    tokenization handling this function's callers already rely on, without
    a second copy of the walk.
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return _evaluate_legacy(cmd, classify)

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue

        # Command-position discipline: a non-command-position MENTION of
        # `git` (an argument to another command, e.g. `echo git stash drop`)
        # must not read as an invocation.
        working = _strip_leading_subshell_and_env(seg_tokens)
        working = _skip_leading_env_assignments(working)
        if not working:
            continue

        # `sh -c 'git stash drop'` tokenizes its quoted payload as ONE shlex
        # word, so the head is the interpreter and the segment would be
        # skipped while the wrapped command drops for real. Unwrap and recurse
        # into the SAME `_evaluate` on the nested payload text.
        # `_BUNDLED_C_FLAG_RE` matches bundled short flags (`-ic`, `-ci`) too,
        # which an exact `"-c" in working[1:]` test misses.
        head_base = _normalize_executable_basename(working[0])
        if head_base in _C_FLAG_SHELL_INTERPRETERS:
            c_flag_positions = [
                i for i in range(1, len(working)) if _BUNDLED_C_FLAG_RE.match(working[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(working):
                    verdict = _evaluate(working[idx + 1], classify)
                    if verdict is not None:
                        return verdict
                    continue

        if head_base != "git":
            continue

        subcmd, ambiguous, remaining = _real_git_subcommand(working[1:])
        if ambiguous:
            seg_text = " ".join(shlex.quote(t) for t in seg_tokens)
            verdict = _evaluate_legacy(seg_text, classify)
            if verdict is not None:
                return verdict
            continue

        if subcmd != "stash":
            continue

        # `remaining[0]`, never a flag-skipping scan -- see module docstring
        # "CLASSIFICATION IS `remaining[0]`". 2026-08-23 fix (same
        # UNSCOPED-STASH GAP shape as the sibling create-side guards, see
        # `_strip_leading_redirection_tokens`'s docstring): this module never
        # applied the strip, so `git stash 2>&1 drop` displaced `remaining[0]`
        # to `"2>&1"`, `_classify_stash_subcommand` allowed it as an
        # unrecognized token (this module's own DELIBERATE ALLOW-LIST), and
        # the irrecoverable `drop`/`clear` this guard exists to catch sailed
        # through.
        stash_remaining = _strip_leading_redirection_tokens(remaining)
        second = stash_remaining[0] if stash_remaining else None
        verdict = classify(second)
        if verdict is not None:
            return verdict

    return None


def _evaluate_legacy_powershell(
    text: str, classify=_classify_stash_subcommand
) -> Optional[str]:
    """PowerShell-shaped free-text fallback for the `tokens is None` route
    (AC3 / Conventions (a)) -- NEVER routes to `_evaluate_legacy` above
    (bash-shaped free text, the exact spurious-deny source this plan exists
    to kill). Strips here-string bodies (`@'...'@`/`@"..."@`) and quoted
    spans via `_dialect.strip_powershell_prose_noise` (C2) before scanning,
    so a hazard-documenting prose string quoting `git stash drop` -- the
    doe-claude shape this plan's Problem section names -- does not read as
    an issued command. Still DENIES on a hit, per PM ruling: fail-closed
    posture preserved, no widening of exposure. A dropped deny here is peer
    stash data loss (module docstring, 2026-07-30 incident) -- the reason
    this route strips rather than allows outright.

    `classify` -- see `_evaluate_legacy`'s docstring; threaded through so
    `check_apply_advisory` reuses this exact PowerShell-shaped fallback
    unchanged.
    """
    stripped = strip_powershell_prose_noise(text)
    for m in _STASH_WORD_RE.finditer(stripped):
        nxt = _NEXT_WORD_AFTER_RE.match(stripped, m.end())
        second = nxt.group(1).rstrip(";&|") if nxt else None
        verdict = classify(second)
        if verdict is not None:
            return verdict
    return None


def _evaluate_powershell(cmd: str, classify=_classify_stash_subcommand) -> Optional[str]:
    """PowerShell-dialect leg of classification, parallel in shape to
    `_evaluate` above but sourcing tokens from `_dialect.resolve_
    segments_for_dialect` (tree-sitter-pwsh) instead of `shlex`.

    Quote-normalization (Conventions (b)): PowerShell tokens retain their
    surrounding quotes as emitted by the tokenizer, so every token is run
    through `_ps_normalize_verb_token` (quote-strip via `_dialect._strip_ps_
    quotes`, then backtick-escape removal -- imported directly from
    `block_subagent_destructive_action`, which already applies this exact
    normalization for its own PowerShell classifiers) BEFORE any literal
    comparison -- an unstripped `"drop"` would silently fail to match
    `_DENY_SUBCOMMANDS` and drop a real deny (fail-OPEN).

    `sh -c` recursion (Conventions (c) / Anti-scope): the payload of an
    `sh -c '...'`/`bash -c '...'`/etc. invocation is POSIX shell text even
    when the outer call arrives via the PowerShell tool -- the recursive
    call below is `_evaluate` (the BASH-dialect evaluator, tokenizing via
    `shlex`), never `_evaluate_powershell`, so the payload dialect is never
    inherited from the outer call. `powershell -Command "..."` is not in
    `_C_FLAG_SHELL_INTERPRETERS` at all, so it does not trigger this path.

    `classify` -- see `_evaluate`'s docstring; threaded through unchanged.
    """
    segments = resolve_segments_for_dialect(
        cmd, Dialect.POWERSHELL, guard_name="block_stash_destruction"
    )
    if segments is None:
        return _evaluate_legacy_powershell(cmd, classify)

    for tokens, _pipe_before in segments:
        if not tokens:
            continue
        clean = [_ps_normalize_verb_token(tok) for tok in tokens]

        head_base = _normalize_executable_basename(clean[0])
        if head_base in _C_FLAG_SHELL_INTERPRETERS:
            c_flag_positions = [
                i for i in range(1, len(clean)) if _BUNDLED_C_FLAG_RE.match(clean[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(clean):
                    # Recurse into the BASH evaluator on the `-c` payload --
                    # never `_evaluate_powershell` (Conventions (c)).
                    verdict = _evaluate(clean[idx + 1], classify)
                    if verdict is not None:
                        return verdict
                    continue

        if head_base != "git":
            continue

        subcmd, ambiguous, remaining = _real_git_subcommand(clean[1:])
        if ambiguous:
            seg_text = " ".join(clean)
            verdict = _evaluate_legacy_powershell(seg_text, classify)
            if verdict is not None:
                return verdict
            continue

        if subcmd != "stash":
            continue

        # `remaining[0]` is already quote-normalized (part of `clean`).
        # 2026-08-23 fix -- see the Bash leg's identical comment above.
        stash_remaining = _strip_leading_redirection_tokens(remaining)
        second = stash_remaining[0] if stash_remaining else None
        verdict = classify(second)
        if verdict is not None:
            return verdict

    return None


def _deny_reason(cmd: str, deny_kind: str) -> str:
    cmd_safe = cmd if len(cmd) <= 200 else cmd[:200] + "..."
    return (
        "BLOCKED: `%s` permanently discards stash content, and on a shared "
        "tree you cannot tell whose entry sits at a given stack position.\n\n"
        "  Command:  %s\n\n"
        "There is no undo -- a dropped stash leaves NO reflog entry.\n\n"
        "Use instead:\n"
        "  - Read content without touching the stack:\n"
        "        git show stash@{N}:<path> > <path>     (per path)\n"
        "        git stash show -p stash@{N}            (see the whole diff)\n"
        "  - Restore your own stash: `git stash pop stash@{N}`, named "
        "index, never position 0.\n"
    ) % (deny_kind, cmd_safe)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the stash-destruction gate against a PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM
    (see module docstring).
    """
    # Deliberately no try/except -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards (its `guard_chain` fail_closed=True
    # entries route an uncaught exception through the crash-deny wrapper).
    # Swallowing an unexpected error into a silent allow here defeats that.
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    # Heredoc bodies are stdin DATA, not shell command text. The deny-reason
    # display below still uses the ORIGINAL `cmd` so the operator sees what
    # they actually ran. `_strip_heredoc_bodies` matches bash `<<`-shaped
    # syntax only -- a no-op on PowerShell text carrying no such marker.
    cmd_for_classification = _strip_heredoc_bodies(cmd)

    if not _STASH_WORD_RE.search(cmd_for_classification):
        return None

    if dialect is Dialect.POWERSHELL:
        deny_kind = _evaluate_powershell(cmd_for_classification)
    else:
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


#: `apply`-ONLY -- see module docstring "APPLY ADVISORY LEG" (below,
#: appended 2026-08-30). Never `pop`: `pop` is the EM's own sanctioned
#: restore path (see "WHY DROP/CLEAR AND NOT POP/APPLY" above) and a
#: successful pop already shows the operator their content landed in the
#: tree, so there is nothing left to nudge. `apply` is the shape the
#: incident this leg answers actually used -- an unmodified stack position
#: checked for a clean return, read as "nothing unique in the stash",
#: which `apply` cannot tell you (see `_advisory_reason` below).
_ADVISORY_APPLY_KIND = "git stash apply"


def _classify_stash_apply_subcommand(second: Optional[str]) -> Optional[str]:
    """`classify` callable for the `apply` advisory leg -- see
    `_evaluate`'s docstring for the parametrization this reuses. Returns
    `_ADVISORY_APPLY_KIND` only for the literal second-level token `apply`;
    every other token (including `None`/bare `git stash`, `pop`, `drop`,
    `clear`, `push`, ...) is out of scope for this leg and allows.
    """
    if second == "apply":
        return _ADVISORY_APPLY_KIND
    return None


def _advisory_reason(cmd: str) -> str:
    cmd_safe = cmd if len(cmd) <= 200 else cmd[:200] + "..."
    return (
        "`git stash apply` only proves overlapping files match, not what "
        "the stash holds.\n"
        "Use instead: `git stash show --name-only stash@{N}` (read-only, "
        "lists actual contents).\n\n"
        "  Command:  %s\n"
    ) % (cmd_safe,)


def check_apply_advisory(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """ADVISORY_REWRITE leg (allow + `additionalContext`, never a deny) for
    `git stash apply` -- see module docstring "APPLY ADVISORY LEG".

    Reuses `_evaluate`/`_evaluate_powershell`/their `_evaluate_legacy*`
    fallbacks UNCHANGED via the `classify` parameter (see those functions'
    own docstrings) rather than a second tokenizer walk -- the only new
    code here is `_classify_stash_apply_subcommand` (a one-branch
    predicate) and this dispatch wrapper. `check` above (the drop/clear
    hard-deny) is untouched: this is an additional registered leg, not a
    replacement.

    Never identity-gated -- fires for every caller including the main-loop
    EM, same posture as `check` (see module docstring "WHY THE EM IS THE
    MORE DANGEROUS CALLER HERE, NOT THE LESS" -- an EM verifying a peer's
    stash for data loss is exactly the scenario this leg answers).
    """
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    cmd_for_classification = _strip_heredoc_bodies(cmd)

    if not _STASH_WORD_RE.search(cmd_for_classification):
        return None

    if dialect is Dialect.POWERSHELL:
        advisory_kind = _evaluate_powershell(
            cmd_for_classification, _classify_stash_apply_subcommand
        )
    else:
        advisory_kind = _evaluate(cmd_for_classification, _classify_stash_apply_subcommand)
    if advisory_kind is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": _advisory_reason(cmd),
        }
    }
