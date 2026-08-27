"""coordinator_core.session.grant_directive — the ONE owner of the Tier-U
grant's argv contract, so a ceremony can run the write/handback in-process
instead of spawning an interpreter to do it.

Why this module exists at all is a cost fact, not a style preference. Every
engine invocation on this box is a cold spawn, and the composition recorder
put a number on what that buys: `workstream_complete` at p95 243s / max 320s
over 8 directives (`state/handoffs/2026-08-19-the-320-second-ceremony.md`).
A ceremony's grant write is a single ~1KB JSON write into
`.git/coordinator-sessions/<sid>/`; paying a fresh interpreter start plus a
`coordinator_core` import for it is the shape
`docs/wiki/cost-budgets-and-the-kill-disposition.md` exists to stop.

Two callers, one argv grammar:

  - `coordinator/bin/tier-u-grant-cli.py` — the shell entrypoint DoE's
    ceremonies and skills invoke BY NAME. It keeps `read`/`check` (whose
    output shapes are a shell contract) and delegates `grant`/`revoke` here.
  - `coordinator_core.merge_assemble.apply` — dispatches the ceremony's
    grant directives straight through `run_grant_directive`, no subprocess.
    `workweek_complete.apply` needs no such wiring: its dispatcher already
    loads every consumes-manifest CLI as a module and calls `main()`
    in-process (`_load_cli_module` / `_invoke_cli_main`), so its two grant
    directives already reach `main` -> here without spawning.

Negative-spec — do NOT re-parse this argv anywhere else. A second parser is
how the write's `--ceremony` and the handback's `--only-ceremony` drift
apart, and a drifted guard is a handback that silently never fires: the
grant then outlives the ceremony that minted it, which is the exact defect
the guard was added to prevent.

Spec backlink: cross-repo/inbox/2026-08-04-doe-claude-em-ceremony-grants-belong-in-code-not-prose.md
"""

from __future__ import annotations

from typing import Optional, Tuple

from coordinator_core.session.grant import revoke_tier_u_grant, write_tier_u_grant

#: Sentinel distinguishing "malformed argv" from the legitimate `None` an
#: unguarded `revoke` parses to. A plain `None` return could not tell the two
#: apart, and the unguarded form is the destructive one — it unlinks whatever
#: grant the session holds, including an explicit PM grant.
ARGS_INVALID = object()

#: Exit codes, matching the CLI trampoline convention the ceremonies already
#: read: the mapped function's bool maps True->0 / False->1, and a usage
#: error (a wrong argv shape, i.e. a defect in the emitting assembler) is 2.
EXIT_OK = 0
EXIT_FALSE = 1
EXIT_USAGE = 2


def parse_grant_args(rest: list[str]):
    """`grant <granted_by> <note> [--ceremony <name>]` — two positionals plus
    an optional flag anywhere in the remaining argv. Returns
    `(granted_by, note, ceremony)` or `ARGS_INVALID`."""
    ceremony: Optional[str] = None
    positional: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--ceremony":
            if i + 1 >= len(rest):
                return ARGS_INVALID
            ceremony = rest[i + 1]
            i += 2
            continue
        positional.append(rest[i])
        i += 1
    if len(positional) != 2:
        return ARGS_INVALID
    granted_by, note = positional
    return granted_by, note, ceremony


def parse_revoke_args(rest: list[str]):
    """`revoke [--only-ceremony <name>]` — nothing (the unguarded
    PM/session-owner form) or the guarded ceremony handback. Returns the
    ceremony name, `None` for the unguarded form, or `ARGS_INVALID`.

    A malformed guard argv must NEVER fall through to the unguarded form:
    that turns a typo into the destructive call."""
    if not rest:
        return None
    if len(rest) == 2 and rest[0] == "--only-ceremony" and rest[1]:
        return rest[1]
    return ARGS_INVALID


def run_grant_directive(args: list[str]) -> Tuple[int, str]:
    """Execute one `grant`/`revoke` directive in-process. Returns
    `(exit_code, message)`; `message` is diagnostic text for a non-zero code
    and empty on success — the caller decides where it goes (stderr for the
    CLI, the apply report for a ceremony).

    Raises nothing on a business `False`: an unresolvable session id is an
    INFRA condition, reported as `EXIT_FALSE` for the caller to tolerate.
    The DR-088 layer-5 guard fails CLOSED, so an unminted grant refuses the
    Tier-U consumer rather than authorizing it — taking a whole ceremony
    down over it would be strictly worse than the prose this replaced.

    A `ValueError` from `write_tier_u_grant` (bad enum, or the
    `granted_by`/`ceremony` cross-field rule) is a CALLER defect — an
    assembler emitting a wrong shape — and returns `EXIT_USAGE`, never
    `EXIT_FALSE`: a defect must not hide inside the tolerated bucket."""
    if not args:
        return EXIT_USAGE, "grant directive: no verb"
    verb, rest = args[0], args[1:]

    if verb == "grant":
        parsed = parse_grant_args(rest)
        if not isinstance(parsed, tuple):
            return EXIT_USAGE, "grant <granted_by> <note> [--ceremony <name>]"
        granted_by, note, ceremony = parsed
        try:
            ok = write_tier_u_grant(granted_by, note, ceremony=ceremony)
        except ValueError as exc:
            return EXIT_USAGE, f"grant: {exc}"
        return (EXIT_OK, "") if ok else (EXIT_FALSE, "grant: session id unresolvable")

    if verb == "revoke":
        parsed = parse_revoke_args(rest)
        if parsed is ARGS_INVALID:
            return EXIT_USAGE, "revoke [--only-ceremony <name>]"
        only_ceremony = parsed if isinstance(parsed, str) else None
        ok = revoke_tier_u_grant(only_ceremony=only_ceremony)
        return (EXIT_OK, "") if ok else (EXIT_FALSE, "revoke: session id unresolvable")

    return EXIT_USAGE, f"grant directive: unknown verb {verb!r}"
