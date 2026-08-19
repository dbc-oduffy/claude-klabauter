"""coordinator_core.bash_guards.block_worktree_creation -- PreToolUse(Bash)
hard-deny guard closing the fleet-wide structural `git worktree` ban's
main-loop gap.

Why this guard exists alongside `block_subagent_destructive_action.py`:
that guard already denies `git worktree add` (and the other mutating
second-level subcommands) via its `_WORKTREE_MUTATING_SUBCOMMANDS`
classification -- but it is IDENTITY-GATED to resolved subagents only (see
that module's own docstring "IDENTITY AXIS" section, and its Layer-2 gate at
`check()`, which resolves `agent_id`/back-pointer `subagent_type` and fails
OPEN -- allows -- when no subagent identity resolves). The main-loop EM is
explicitly exempt there: an EM-typed `git worktree add ...` sails through
untouched. Worktrees on this fleet degrade badly on Windows and do not scale
to concurrent agents sharing one tree, so the ban needs a SECOND guard with
no identity gate at all -- this module. It fires on every caller, EM
included, exactly like the sibling `check_raw_pid_liveness.py`'s posture
("the raw-pid anti-pattern is wrong regardless of who types it").

DELIBERATE ALLOW-LIST -- do NOT "tighten" `remove`/`prune` to deny, ever.
Under a worktree-creation ban, removal is CLEANUP and must stay reachable:
`coordinator_core/ops/agent_worktree_sweep.py` (the stray-worktree reaper,
wired into `/workday-start` Step 0.6) shells out to `git worktree list
--porcelain` and `git worktree remove` to clear abandoned worktrees left by
a dead/crashed session. Denying `remove`/`prune` here would make this guard
fight its own cleanup path -- an EM running the reaper to clear strays would
be blocked by the very guard whose job is to stop new strays from being
created. `list` (read-only enumeration) is allowed for the same reason the
reaper needs it. A bare `git worktree` (no second-level subcommand) also
lists and is treated identically to `list`.

DENY set (creation/mutation-adjacent, second-level subcommand after `git
worktree`): `add`, `move`, `repair`, `lock`, `unlock`. `add` is the actual
creation verb; `move`/`repair`/`lock`/`unlock` all operate on an existing
worktree's registration and are denied alongside it rather than carved out,
since none of them are the cleanup path the allow-list exists to protect.
Any OTHER, unenumerated second-level subcommand also denies -- default-deny
on the mutating/unknown side, matching the sibling guard's own
`_WORKTREE_MUTATING_SUBCOMMANDS` posture ("an UNRECOGNIZED second-level
token is denied too").

NOT a `--worktree` substring ban. `git restore -W` / `git restore
--worktree` is `git-restore`'s own unrelated flag (see
`block_subagent_destructive_action.py`'s `_RESTORE_WORKTREE_RE`) -- it never
creates a worktree. This guard REUSES the anchored-classifier approach from
that module (its `_real_git_subcommand`/`_tokenize_full_command`/
`_segments_from_tokens`/`_normalize_executable_basename`, imported directly
rather than re-implemented) to resolve the REAL git subcommand from argv
position, so `restore --worktree` never reaches the `worktree`-subcommand
branch at all -- it resolves to subcommand `"restore"`, which this guard
does not touch. A raw substring/regex ban on the text `--worktree` would
false-positive on exactly this shape; this module never does that.

Shell-shape handling (leading env-var assignments, `&&`/`;`/`|` chaining,
POSIX quoting) reuses the SAME tokenizer the sibling module's authoritative
tokenized pass uses (`_tokenize_full_command` + `_segments_from_tokens`),
rather than inventing a second, weaker parser: `FOO=bar git worktree add x`
resolves correctly because the git-token search below scans every token in
a segment for a basename-normalized `git` match (not just position 0), and
`cmd1 && git worktree add x` resolves correctly because the shared
tokenizer treats `&&`/`;`/`|` as always-separate punctuation tokens
(quote-aware -- a `;` inside a quoted string does not split).

FAIL-CLOSED FALLBACK, narrowly scoped. If the shared tokenizer cannot parse
a segment (unbalanced quoting) or `_real_git_subcommand` reports an
unrecognized-global-option ambiguity for a segment that DOES contain the
word `worktree`, this guard falls back to `_evaluate_legacy` -- a
free-text search for a stand-alone `worktree` word followed by whatever
plain word comes next -- rather than silently allowing. This fallback is
deliberately narrow: it is only ever invoked on text already known (via the
cheap `_WORKTREE_WORD_RE` pre-filter in `check()`) to contain a
non-`--worktree`-flag `worktree` word, so it never over-triggers on
ordinary git traffic that doesn't mention worktrees at all.

OVERRIDE -- NONE at this layer, by design. A subagent can set its own
process env, so a `COORDINATOR_OVERRIDE_*` escape hatch here would be
subagent-reachable and defeat the ban for the exact caller class this
guard's identity-gated sibling already covers (precedent:
`block_subagent_destructive_action.py`'s own "OVERRIDE-WITHHOLDING,
deliberate" section). The PM/EM override for the fleet-wide ban as a whole
lives in a repo-root sentinel checked by DoE-side hooks, not here -- this
guard's deny message deliberately does not name that sentinel (design-as-
offers: lead with the alternative, not the bypass).

HEREDOC-BODY FALSE-DENY FIX (2026-07-29). A heredoc body is stdin DATA, never
shell command text -- `check()` strips it (via `_strip_heredoc_bodies`,
imported from `block_subagent_destructive_action.py`, the same helper
`_sentinel_creation_guard.py` already reuses -- not a new copy) before
either the `_WORKTREE_WORD_RE` pre-filter or `_evaluate` run. Without this, a
benign `cat <<EOF > review.md ... EOF` persisting a document whose PROSE
happens to contain the words "git worktree add" (or names this guard's own
filename) could mis-tokenize: an unquoted `;`/`|` inside that prose starts a
new segment (`_segments_from_tokens` treats these as always-separating
punctuation, with no concept of "inside a heredoc body"), and if the next
body word after that punctuation is literally "git", the segment's resolved
head is "git" and the guard reads document prose as a live invocation.
Observed live 2026-07-29 -- a dispatched reviewer's findings heredoc, which
named this file by filename and quoted a `git worktree add` example, was
denied while persisting via the sanctioned Bash-redirect path. See
`state/bug-backlog/2026-07-29-worktree-guard-false-denies-documents-naming-
guard-files.yaml` (DoE-claude) for the incident. Anti-bypass: an interpreter
FED by a heredoc (`bash <<'EOF' ... EOF`) is untouched by this strip -- the
residual `bash <<'EOF'` line survives stripping and a real `git worktree
add` invocation outside the heredoc body still denies exactly as before.

Spec: fleet-wide structural git-worktree ban, main-loop leg (DoE-claude
dispatch, 2026-07-28) -- companion to `block_subagent_destructive_action.py`'s
pre-existing identity-gated `git worktree add` denial.

DIALECT CONVERSION (2026-08-19, C4 of
`docs/plans/2026-08-19-the-held-guard-cohort-becomes-dialect-safe.md`). The
prior hold (Bash-only `MATCHERS`) is discharged. This is the HIGHEST
spurious-deny blast radius of the four held guards: `_classify_worktree_
subcommand` default-denies ANY unrecognized second word, so an unparseable
PowerShell string that merely contains the word `worktree` would deny
whatever plain word follows it, for a reason unrelated to what it does.

- Dialect resolved from `payload["tool_name"]` via `_dialect.
  dialect_from_tool_name` -- never inferred from command text (see
  `_dialect.py`'s own Anti-scope).
- BASH leg is UNCHANGED (AC6): `_evaluate`/`_evaluate_legacy` still run over
  `_command_tokenizer`/`block_subagent_destructive_action` helpers exactly
  as before this conversion.
- POWERSHELL leg tokenizes via `_dialect.resolve_segments_for_dialect`. When
  it cannot parse (`None`), this guard routes to C2's PowerShell-shaped
  scanner (`_dialect.strip_powershell_prose_noise`) -- NEVER to the
  bash-shaped `_evaluate_legacy` (PM ruling, Convention (a) of the cohort
  plan) -- and still DENIES on a hit: fail-closed posture preserved, no
  widening of exposure.
- Every PowerShell literal-token comparison strips quotes via
  `_dialect._strip_ps_quotes` FIRST (Convention (b)): `_flatten_powershell_
  tokens` emits raw source spans, so a token like `"add"` arrives quoted, not
  `add`. Skipping this strip would make an unrecognized-but-quoted second
  word default-deny even when the real word is a recognized allow-listed
  form -- this guard's own fail-CLOSED risk direction per Convention (b).
- `sh -c '...'`/`bash -c '...'` payload text is always Bash, even when the
  OUTER call is PowerShell (Convention (c)) -- the recursive unwrap below
  always calls the Bash-shaped `_evaluate`, never a PowerShell-dialect
  re-entry, so it never inherits the outer dialect.

Spec backlink: `pln-the-held-guard-cohort-becomes-a94c56` § C4
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
from coordinator_core.bash_guards._command_tokenizer import (
    _skip_wrapper_own_argv,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    dialect_from_tool_name,
    resolve_segments_for_dialect,
    strip_powershell_prose_noise,
)
from coordinator_core.bash_guards._dialect import _strip_ps_quotes
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

#: Cheap pre-filter (mirrors the sibling module's Layer-1 posture): a
#: negative lookbehind excludes a `worktree` immediately preceded by a word
#: character or hyphen, so `--worktree`/`-Wworktree`-shaped flag text does
#: NOT count as a candidate `git worktree` invocation on its own -- the real
#: subcommand-vs-flag disambiguation still happens in the anchored tokenized
#: pass below; this only gates whether that (more expensive) pass runs at
#: all.
_WORKTREE_WORD_RE = re.compile(r"(?<![\w-])worktree\b")

#: Second-level `git worktree` subcommand classification -- see module
#: docstring "DELIBERATE ALLOW-LIST" / "DENY set" sections for the full
#: rationale on each member.
_DENY_SUBCOMMANDS = frozenset({"add", "move", "repair", "lock", "unlock"})
_ALLOW_SUBCOMMANDS = frozenset({"list", "remove", "prune"})

#: The single plain word immediately following (whitespace-separated) a
#: `worktree` match position -- used only by the narrow legacy fallback
#: below.
_NEXT_WORD_AFTER_RE = re.compile(r"\s+(\S+)")

#: A bare leading `VAR=value` shell assignment token (`GIT_TRACE=1 git
#: worktree add ...`) -- `_strip_leading_subshell_and_env` (imported from
#: the sibling module) only peels a literal `env` word prefix, not a bare
#: assignment with no `env` keyword, so this guard's own command-position
#: resolution needs its own skip for the assignment-prefix shape (mirrors
#: `_sentinel_creation_guard.py`'s `_env_skip_index`).
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


#: Passthrough wrapper binaries that run their remaining argv unchanged
#: (BX-13 fix, 2026-07-29, confirmed live): `nice git worktree add ...` was
#: never recognized -- `_strip_leading_subshell_and_env` only peels `env`/
#: a subshell-open, and this guard's own `_skip_leading_env_assignments`
#: only peeled a bare `VAR=value` token, so the resolved argv0 landed on
#: `nice` (not `git`) and the invocation allowed while still creating the
#: worktree for real. Same set `dispatch_checks.py`'s `_BYPASS_PREFIX`
#: already tolerates.
#: Widened (2026-07-29, code-reviewer Finding 3) -- see the sibling copy in
#: `block_subagent_destructive_action.py` for the full rationale: `setsid`/
#: `strace`/`doas`/`busybox` were unrecognized passthrough wrappers, so
#: `setsid git worktree add ../wt-1 x` landed the resolved head on `setsid`
#: (never `git`) and the worktree was still created for real.
_PASSTHROUGH_WRAPPERS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
        "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
    }
)

#: Shell interpreters whose `-c <string>` argument is executed, not inert
#: text -- see `_evaluate`'s BX-13 fix comment.
_C_FLAG_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): the skip
#: below tolerated the wrapper BINARY token but never the wrapper's OWN
#: argument(s) -- `timeout 30 git worktree add ...`, `ionice -c2 git
#: worktree add ...`, `stdbuf -oL git worktree add ...` all landed argv0 on
#: `30`/`-c2`/`-oL` (not `git`), so the worktree was still created for real
#: while this guard allowed. `_skip_wrapper_own_argv` itself now lives in
#: `_command_tokenizer.py` (2026-07-30, M8 consolidation) -- imported above
#: rather than hand-maintained here; see that module's own docstring for the
#: five-copy history this closes.


def _skip_leading_env_assignments(tokens: "list[str]") -> "list[str]":
    """Return `tokens` with any leading `VAR=value`-shaped tokens AND any
    leading no-op passthrough wrapper tokens (`nice`/`time`/etc., plus that
    wrapper's OWN argument(s) -- see `_skip_wrapper_own_argv`) removed,
    exposing the true command-position head."""
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


def _classify_worktree_subcommand(second: Optional[str]) -> Optional[str]:
    """Return a deny_kind label for the second-level `git worktree`
    subcommand `second` (the token immediately following `worktree`), or
    `None` (allow). `second is None` -- bare `git worktree` -- allows: it
    lists worktrees, same as the explicit `list` form.
    """
    if second is None:
        return None
    if second in _ALLOW_SUBCOMMANDS:
        return None
    if second in _DENY_SUBCOMMANDS:
        return "git worktree %s" % second
    return "unrecognized subcommand (default-deny)"


def _evaluate_legacy(text: str) -> Optional[str]:
    """Narrow free-text fallback -- see module docstring "FAIL-CLOSED
    FALLBACK, narrowly scoped". Only ever called on text already known to
    contain a genuine (non-flag) `worktree` word; never the guard's primary
    classification path.
    """
    m = _WORKTREE_WORD_RE.search(text)
    if not m:
        return None
    nxt = _NEXT_WORD_AFTER_RE.match(text, m.end())
    second = nxt.group(1).rstrip(";&|") if nxt else None
    return _classify_worktree_subcommand(second)


def _evaluate(cmd: str) -> Optional[str]:
    """Primary classification: tokenize the full command (quote-aware,
    `;`/`&`/`|`-segmented -- reused from the sibling module, see module
    docstring "Shell-shape handling"), and for each segment, resolve the
    REAL git subcommand from argv position via `_real_git_subcommand`
    (which itself skips git's own global options). Falls back to
    `_evaluate_legacy`, scoped to the offending segment/command text only,
    on an unparseable segment or an unrecognized-global-option ambiguity.
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return _evaluate_legacy(cmd)

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue

        # Review: code-reviewer -- Finding 2 (P2, 2026-07-28): scanning
        # every token in the segment for the first `git`-basename match
        # (the pre-fix behavior here) treats a non-command-position
        # MENTION of "git" (an argument to another command, e.g. `echo git
        # worktree add x`) as an invocation -- the exact false-positive
        # class `block_subagent_destructive_action.py`'s "COMMAND-POSITION
        # GIT-TOKEN FIX" closes for its own sibling checks, in this same
        # diff, but that fix was never applied to this guard. Reuse the
        # SAME command-position discipline (`_strip_leading_subshell_and_
        # env` peels a leading subshell-open `(` / `env` prefix so the
        # remaining head token is the true command-position executable)
        # instead of a position-independent token scan.
        working = _strip_leading_subshell_and_env(seg_tokens)
        working = _skip_leading_env_assignments(working)
        if not working:
            continue

        # BX-13 fix (2026-07-29, confirmed live): `sh -c 'git worktree add
        # ...'` (or `bash -c`/`zsh -c`/etc.) was never unwrapped -- the
        # quoted `-c` argument tokenizes as ONE shlex word, so `working[0]`
        # was the shell interpreter itself, never `git`, and the segment was
        # silently skipped while the wrapped command still created the
        # worktree for real. Unwrap and recurse into the SAME `_evaluate` on
        # the nested payload text.
        #
        # BUNDLED-`-c`-FLAG FIX (2026-07-29, code-reviewer Finding 2,
        # confirmed live): the exact-token `"-c" in working[1:]` test missed
        # a BUNDLED short flag -- `sh -ic 'git worktree add ...'` tokenizes
        # its second token as the literal string `-ic`, which is never
        # exactly `"-c"`, so the unwrap never fired and the quoted payload
        # was never re-scanned. `_BUNDLED_C_FLAG_RE` (imported from the
        # sibling destructive-action guard, which already fixed this exact
        # gap for its own `-c` detection) matches any bundled-or-standalone
        # `-c` short flag (`-c`, `-ic`, `-ci`, ...).
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

        if subcmd != "worktree":
            continue

        second = remaining[0] if remaining else None
        verdict = _classify_worktree_subcommand(second)
        if verdict is not None:
            return verdict

    return None


def _ps_normalize_token(tok: str) -> str:
    """Normalize a single PowerShell token to its plain comparison spelling:
    strip a surrounding quote first (`_dialect._strip_ps_quotes`'s own
    "verbatim quoted span" contract -- see Convention (b) cited in this
    module's docstring), THEN remove a no-op backtick escape, matching
    `block_subagent_destructive_action._ps_normalize_verb_token`'s own
    order.
    """
    return _strip_ps_quotes(tok).replace("`", "")


def _evaluate_powershell_legacy(text: str) -> Optional[str]:
    """PowerShell-shaped free-text fallback for the `tokens is None` route
    (C2's scanner, Convention (a) -- NEVER the bash-shaped `_evaluate_legacy`
    above). `text` is stripped of here-string bodies and quoted spans via
    `_dialect.strip_powershell_prose_noise` before the same
    `_WORKTREE_WORD_RE` + "next plain word" scan the bash leg's own
    `_evaluate_legacy` uses -- still DENIES on a hit (PM ruling: fail-closed
    posture preserved, no widening of exposure)."""
    scannable = strip_powershell_prose_noise(text)
    m = _WORKTREE_WORD_RE.search(scannable)
    if not m:
        return None
    nxt = _NEXT_WORD_AFTER_RE.match(scannable, m.end())
    second = nxt.group(1).rstrip(";&|") if nxt else None
    if second is not None:
        second = _ps_normalize_token(second)
    return _classify_worktree_subcommand(second)


def _evaluate_powershell(cmd: str) -> Optional[str]:
    """PowerShell-dialect leg of classification: tokenize AND segment via
    `_dialect.resolve_segments_for_dialect` (quote-aware, `;`/`&`-segmented
    -- see `_dialect.py`'s own "Output shape"), and for each segment resolve
    the REAL git subcommand from argv position exactly like the bash leg
    (`_real_git_subcommand`, dialect-agnostic -- it operates on a
    `List[str]`, never on shell-shaped text). Falls back to
    `_evaluate_powershell_legacy` on an unparseable segment/command or an
    unrecognized-global-option ambiguity, per Convention (a).
    """
    segments = resolve_segments_for_dialect(
        cmd, Dialect.POWERSHELL, guard_name="block_worktree_creation"
    )
    if segments is None:
        return _evaluate_powershell_legacy(cmd)

    for seg_tokens, _pipe_before in segments:
        if not seg_tokens:
            continue

        # Command-position discipline (same reasoning as the bash leg's own
        # comment above `_evaluate`): normalize every token's quoting BEFORE
        # any comparison, per Convention (b).
        clean = [_ps_normalize_token(t) for t in seg_tokens]
        working = _skip_leading_env_assignments(clean)
        if not working:
            continue

        # `sh -c '...'`/`bash -c '...'` typed from a PowerShell call --
        # Convention (c): the payload is Bash even under a PowerShell outer
        # call, so the recursive call below is the Bash-shaped `_evaluate`,
        # never a PowerShell re-entry. `powershell -Command "..."` is NOT in
        # `_C_FLAG_SHELL_INTERPRETERS` at all, so it never reaches here.
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
            verdict = _evaluate_powershell_legacy(seg_text)
            if verdict is not None:
                return verdict
            continue

        if subcmd != "worktree":
            continue

        second = remaining[0] if remaining else None
        verdict = _classify_worktree_subcommand(second)
        if verdict is not None:
            return verdict

    return None


def _deny_reason(deny_kind: str) -> str:
    return (
        "BLOCKED: %s banned fleet-wide -- breaks on Windows, does not "
        "scale across concurrent agents in one tree. Work here instead; "
        "branch isolation needs PM/EM sign-off.\n\n"
        "Use instead:\n"
        "  `git worktree list` / `git worktree remove` / `git worktree prune`"
    ) % deny_kind


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the worktree-creation-ban gate against a PreToolUse payload.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM
    (see module docstring).
    """
    # Deliberately no try/except here -- fail-CLOSED-on-exception is the
    # dispatcher's job for hard-deny guards (dispatch.py's guard_chain
    # fail_closed=True entries route an uncaught exception through its
    # crash-deny wrapper); catching and swallowing an unexpected error into
    # a silent allow here would defeat that contract.
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    dialect = dialect_from_tool_name(payload.get("tool_name") or "")

    if dialect is Dialect.POWERSHELL:
        # PowerShell has no POSIX heredoc syntax -- the bash leg's
        # `_strip_heredoc_bodies` does not apply here. A quoted/here-string
        # `worktree` mention is inherently safe against this cheap
        # pre-filter too: when the tokenizer parses cleanly, a quoted span
        # is ONE atomic token (`_ATOMIC_ARGUMENT_NODE_TYPES`), never split
        # into separate `git`/`worktree`/`add` words a segment's head could
        # resolve to, so the real classification below never misreads
        # prose as an invocation on the parses-cleanly route.
        if not _WORKTREE_WORD_RE.search(cmd):
            return None
        deny_kind = _evaluate_powershell(cmd)
        if deny_kind is None:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _deny_reason(deny_kind),
            }
        }

    # Heredoc bodies are stdin DATA, not shell command text -- strip them
    # before classification (same fix already applied by the sibling
    # `block_subagent_destructive_action.py`/`_sentinel_creation_guard.py`,
    # reusing THEIR `_strip_heredoc_bodies`, not a new copy). Without this,
    # a benign `cat <<EOF > review.md ... EOF` persisting a document whose
    # PROSE happens to contain the words "git worktree add" (or names this
    # guard's own filename) could tokenize its heredoc body as if it were
    # live shell text: an unquoted `;`/`|` inside that prose starts a new
    # `_segments_from_tokens` segment, and if the next body word after that
    # punctuation is literally "git", the segment's head resolves to "git"
    # and the guard misreads document prose as a real invocation (observed
    # live, 2026-07-29: a reviewer's findings heredoc denied on exactly this
    # shape -- see `state/bug-backlog/2026-07-29-worktree-guard-false-
    # denies-documents-naming-guard-files.yaml` in DoE-claude). Anti-bypass:
    # an interpreter FED by a heredoc (`bash <<'EOF' ... EOF`) is untouched
    # by this strip -- the residual `bash <<'EOF'` line survives and, if it
    # independently contains a real `git worktree add` invocation outside
    # the heredoc, still denies.
    #
    # Review: coordinator:code-reviewer (Finding 2, guard-message-size-
    # discipline) -- the deny-reason display below no longer echoes the raw
    # `cmd` at all (message-size compression dropped that line and the
    # `_deny_reason` parameter that carried it); it names only the resolved
    # `deny_kind` class (e.g. "git worktree add"). This comment previously
    # promised per-invocation command visibility that no longer exists --
    # corrected to match current behavior rather than leaving the stale
    # promise in place.
    cmd_for_classification = _strip_heredoc_bodies(cmd)

    if not _WORKTREE_WORD_RE.search(cmd_for_classification):
        return None

    deny_kind = _evaluate(cmd_for_classification)
    if deny_kind is None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(deny_kind),
        }
    }
