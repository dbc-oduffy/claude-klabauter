"""coordinator_core.ceremony_config.argv_only — author-time argv-only predicate.

Purpose: the single, importable definition of the argv-only contract that
`coordinator-ceremony-hook.py` enforces at fire time (`a1ff85d1f`) — mirrored
here so a consumer repo, or a CI gate in THIS repo, can check a candidate
`*_command` / `*_cmd` value BEFORE a ceremony ever runs it. See
`coordinator/bin/coordinator-validate-local-config` for the CLI that walks a
repo's `coordinator.local.md` through this predicate.

Provenance: example-cockpit-repo wrote their own TypeScript twin of this rule
(`tests/portability/ceremony-command-word-form.test.ts`) after their
`workday_complete_post_command` broke under the argv-only change, and asked
Claude-klabauter (`cross-repo/inbox/2026-08-06-example-cockpit-repo-em-argv-only-landed-
Cockpit-config-fixed.md`) to expose the check so they can delete their fork
and call this one instead. Rule naming (W1/W2/W3/W4) intentionally mirrors
theirs so the two implementations are recognisably the same rule.

CORRECTION carried from that memo, not repeated uncritically: the memo reads
the fire-time hook's `:186` WARN text ("VAR=value prefixes") as unreachable
for an actual `VAR=value` prefix, because such a prefix splits cleanly under
`shlex.split` and so never raises the `ValueError` that message is gated on.
That much is true. But the operator is not left silent — a clean split falls
through to the hook's launch-failure path (`:230`), whose message ALSO names
`VAR=value` prefixes explicitly. The real defect is sequencing, not silence:
the precise diagnosis is the trailing explanation on a confusing primary
signal (`No such file or directory: 'COCKPIT_STATE_SOURCE=firestore'`)
instead of the up-front one. Worth fixing for that reason, not the
"unreachable" one. Wiring `check_argv_only` into the hook's pre-exec path (so
a W1 routes straight to the precise message) is an EM follow-up once
`coordinator-ceremony-hook.py`'s current partitioned review lands — NOT done
by this module, which is standalone by design (see module docstring above).

Deliberately NOT checked here: whether argv[0] resolves on PATH. That is an
environment-dependent fact (a command can be argv-only-conformant on one
machine and PATH-absent on another) and belongs at fire time, in the actual
exec attempt, not at author time — folding it in here would make this
predicate machine-dependent, which defeats the point of an author-time CI
gate meant to be reproducible across machines.
"""

from __future__ import annotations

import dataclasses
import re
import shlex

__all__ = ["ArgvOnlyVerdict", "check_argv_only"]

_W1_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# W2 — shell metacharacters that, if they survive parsing UNQUOTED, can only
# have been meant as shell syntax: chaining/piping (&&, ||, |, ;, &), redirects
# (>, <, >>  — '>' as a substring also catches '>>'), command substitution
# (backtick, `$(`), unexpanded globs (*, ?, [), and a leading `~`. These are judged on
# PARSED tokens, deliberately, not the raw command string — see
# `_raw_quote_segments` below for why quoting must be resolved first.
# `&&` precedes a lone `&` deliberately: the scan returns on the first hit, so
# a chained command names the chain rather than the backgrounding character
# inside it. `&` alone was missing until 2026-08-31 -- `pnpm test &` parsed as
# CONFORMANT and handed a literal `&` to the program as an argv element,
# silently, instead of backgrounding anything (reported by example-cockpit-repo-em,
# direct grep).
_W2_SUBSTRINGS = ("&&", "||", "|", ";", ">", "<", "`", "$(", "&")
_W2_GLOB_CHARS = ("*", "?", "[")

# Tilde expansion is the shell's, not argv-only's: a LEADING `~` reached fire
# time as a literal and failed there as an unresolvable `argv[0]`
# (`~/bin/publish --fleet` parsed CONFORMANT). Judged only in leading position,
# because a `~` inside a token is an ordinary filename character (`file~`) and
# nothing expands it. Quoting resolves it exactly as it does the rest of W2.
_W2_LEADING_EXPANSION_CHARS = ("~",)


@dataclasses.dataclass(frozen=True)
class ArgvOnlyVerdict:

    conformant: bool
    rule: str | None
    detail: str
    argv: list[str] | None


def _raw_quote_segments(command: str) -> list[str]:
    """Tokenize `command` with quote characters PRESERVED in each token.

    This is `shlex.split(command, posix=False)` — deliberately the
    non-POSIX mode. POSIX mode (the mode used to build the canonical argv
    below) strips quote characters, which is exactly right for the argv a
    caller will exec, but wrong for the W2 metacharacter check: it would
    erase the difference between `'*.log'` (a glob character the AUTHOR
    quoted specifically so the shell would NOT expand it — conformant) and
    a bare `*.log` (an unexpanded glob that only makes sense as shell syntax
    — a W2 violation). Non-POSIX mode keeps the quote characters attached to
    each token, so a token that is wrapped start-to-end by one matching
    quote pair can be recognised and exempted from the metacharacter scan;
    everything else is scanned as written.

    Raises ValueError under the same unterminated-quote conditions POSIX
    mode does; callers of this helper only reach it after the POSIX parse
    (W3/W4) has already succeeded, so that should not fire in practice, but
    is not suppressed here — a genuine mismatch between the two shlex modes
    on the same string is a fact worth surfacing, not swallowing.
    """
    return shlex.split(command, posix=False)


def _is_fully_quoted(token: str) -> bool:
    return len(token) >= 2 and (
        (token[0] == '"' and token[-1] == '"')
        or (token[0] == "'" and token[-1] == "'")
    )


def _w2_violation(command: str) -> str | None:
    """Returns a detail sentence if a W2 shell metacharacter survives
    parsing unquoted, else None. See module-level `_W2_SUBSTRINGS` /
    `_W2_GLOB_CHARS` for the exact character/substring set and
    `_raw_quote_segments` / `_is_fully_quoted` for how quoting is resolved
    before the scan runs.
    """
    for token in _raw_quote_segments(command):
        if _is_fully_quoted(token):
            continue
        for needle in _W2_SUBSTRINGS:
            if needle in token:
                return (
                    f"token {token!r} carries unquoted shell metacharacter "
                    f"{needle!r}, which only has meaning as shell syntax "
                    "(chaining, piping, redirection, or command "
                    "substitution) — argv-only execution cannot honor it."
                )
        for ch in _W2_LEADING_EXPANSION_CHARS:
            if token.startswith(ch):
                return (
                    f"token {token!r} starts with unquoted {ch!r} — argv-only "
                    "execution does not expand it, so it reaches the program "
                    "literally; write the resolved path, or quote it if the "
                    "literal character is intended."
                )
        for ch in _W2_GLOB_CHARS:
            if ch in token:
                return (
                    f"token {token!r} carries an unquoted glob character "
                    f"{ch!r} — argv-only execution does not expand globs; "
                    "quote it if the literal character is intended, or "
                    "pre-expand it before configuring the command."
                )
    return None


def check_argv_only(command: str) -> ArgvOnlyVerdict:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return ArgvOnlyVerdict(
            conformant=False,
            rule="W3-unparseable",
            detail=f"command does not parse as shell-lexed tokens: {exc}",
            argv=None,
        )

    if not argv:
        return ArgvOnlyVerdict(
            conformant=False,
            rule="W4-empty",
            detail="command parses to an empty argv (blank or whitespace-only value).",
            argv=None,
        )

    if _W1_ASSIGNMENT_RE.match(argv[0]):
        return ArgvOnlyVerdict(
            conformant=False,
            rule="W1-assignment-prefix",
            detail=(
                f"first token {argv[0]!r} is a POSIX `VAR=value` assignment "
                "prefix — argv-only execution has no shell to interpret it "
                "as an env-var assignment; it becomes a literal (and "
                "unresolvable) argv[0] instead. Move the assignment into a "
                "wrapper script, or drop it if the target never reads the "
                "variable."
            ),
            argv=None,
        )

    w2_detail = _w2_violation(command)
    if w2_detail is not None:
        return ArgvOnlyVerdict(
            conformant=False,
            rule="W2-shell-metachar",
            detail=w2_detail,
            argv=None,
        )

    return ArgvOnlyVerdict(conformant=True, rule=None, detail="", argv=argv)
