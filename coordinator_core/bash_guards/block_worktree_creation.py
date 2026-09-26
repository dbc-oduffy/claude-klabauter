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

_WORKTREE_WORD_RE = re.compile(r"(?<![\w-])worktree\b")

#: docstring "DELIBERATE ALLOW-LIST" / "DENY set" sections for the full
_DENY_SUBCOMMANDS = frozenset({"add", "move", "repair", "lock", "unlock"})
_ALLOW_SUBCOMMANDS = frozenset({"list", "remove", "prune"})

_NEXT_WORD_AFTER_RE = re.compile(r"\s+(\S+)")

#: A bare leading `VAR=value` shell assignment token (`GIT_TRACE=1 git
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


#: worktree for real. Same set `dispatch_checks.py`'s `_BYPASS_PREFIX`
_PASSTHROUGH_WRAPPERS = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
        "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
    }
)

_C_FLAG_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _skip_leading_env_assignments(tokens: "list[str]") -> "list[str]":
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
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return _evaluate_legacy(cmd)

    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue

        # class `block_subagent_destructive_action.py`'s "COMMAND-POSITION
        # GIT-TOKEN FIX" closes for its own sibling checks, in this same
        working = _strip_leading_subshell_and_env(seg_tokens)
        working = _skip_leading_env_assignments(working)
        if not working:
            continue

        # BUNDLED-`-c`-FLAG FIX (2026-07-29, code-reviewer Finding 2,
        # was never re-scanned. `_BUNDLED_C_FLAG_RE` (imported from the
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
    segments = resolve_segments_for_dialect(
        cmd, Dialect.POWERSHELL, guard_name="block_worktree_creation"
    )
    if segments is None:
        return _evaluate_powershell_legacy(cmd)

    for seg_tokens, _pipe_before in segments:
        if not seg_tokens:
            continue

        clean = [_ps_normalize_token(t) for t in seg_tokens]
        working = _skip_leading_env_assignments(clean)
        if not working:
            continue

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
            # Built from the QUOTE-NORMALIZED tokens, never `shlex.quote` over
            seg_text = " ".join(working)
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
        "BLOCKED: %s banned fleet-wide -- breaks Windows, doesn't scale "
        "across concurrent agents in one tree. Work here; branch isolation "
        "needs PM/EM sign-off.\n\n"
        "Use instead:\n"
        "  `git worktree list` / `git worktree remove` / `git worktree prune`"
    ) % deny_kind


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    dialect = dialect_from_tool_name(payload.get("tool_name") or "")

    if dialect is Dialect.POWERSHELL:
        # is ONE atomic token (`_ATOMIC_ARGUMENT_NODE_TYPES`), never split
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
