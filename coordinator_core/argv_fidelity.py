"""
coordinator_core.argv_fidelity

Shared argv-fidelity seam for coordinator CLIs that accept a multi-line
--body: resolving a body from --body/--body-file without losing lines, and
refusing a --body value that would already have lost lines by the time
argparse sees it.

Why this module exists: on Windows, cmd.exe truncates its ENTIRE command
line at the first LF during its own parse, before a .cmd launcher body ever
runs. A multi-line --body reaching a CLI through its generated .cmd
forwarder therefore arrives holding only its first line -- SILENTLY, when
--body is the trailing flag: argparse still sees a well-formed value, the
write succeeds, and the record lands short with no error anywhere. The two
functions here close that class: resolve_body gives every caller a
lossless transport (--body-file, including a - stdin sentinel), and
refuse_newline_argv refuses the lossy one outright instead of letting it
through silently.

Shape copied from two callers that already ship this pattern -- see
coordinator/bin/cross-repo-memo.py (its - stdin sentinel and empty-body
guard, born from the 2026-07-22 body-drop verdict) and
coordinator/bin/coordinator-queue-append.py (its --body/--body-file
mutual-exclusion block in main()). This module does not invent a third
shape; it lifts the shared one out so coordinator-lesson-add.py,
coordinator-lesson-promote.py, and queue-triage.py can share it too.

Error-raising convention: both functions raise ArgvFidelityError (a
ValueError subclass) rather than calling argparse.ArgumentParser.error
directly. Neither function owns a parser instance -- resolve_body and
refuse_newline_argv are meant to run from a CLI's post-parse validation,
where the caller already holds the parser. The caller catches
ArgvFidelityError and hands the message to parser.error(str(exc)), which
prints it alongside usage and exits 2 -- the same shape
coordinator-queue-append.py's own mutual-exclusion block already produces.
Raising (instead of returning a value, error tuple, as
coordinator/bin/cross-repo-memo.py's own `_build_and_validate_scoped_to`
does) means a caller cannot forget to check an error return before using
the resolved body.

Message register: refusal text follows docs/wiki/guard-messaging.md §
Register -- one fact, stated once, plus the terse alternative naming
--body-file. No self-legitimacy, no repetition, no reassurance, no
apology, never an override key.
"""
from __future__ import annotations

import sys


class ArgvFidelityError(ValueError):
    pass


def resolve_body(
    body: str | None,
    body_file: str | None,
    *,
    flag_name: str = "--body",
    allow_empty: bool = False,
) -> str:
    body_file_flag = f"{flag_name}-file"

    if body is not None and body_file is not None:
        raise ArgvFidelityError(
            f"{flag_name} and {body_file_flag} are mutually exclusive."
        )

    if body is None and body_file is None:
        raise ArgvFidelityError(
            f"one of {flag_name} or {body_file_flag} is required."
        )

    if body_file is not None:
        if body_file == "-":
            resolved = sys.stdin.read()
        else:
            try:
                with open(body_file, "r", encoding="utf-8") as f:
                    resolved = f.read()
            except OSError as exc:
                raise ArgvFidelityError(
                    f"{body_file_flag} unreadable: {exc}"
                ) from exc
        if not resolved.strip() and not allow_empty:
            raise ArgvFidelityError(
                f"{body_file_flag} resolved to an empty body; pass a "
                f"non-empty file, '-' with non-empty stdin, or {flag_name}."
            )
        return resolved

    assert body is not None
    if not body.strip() and not allow_empty:
        raise ArgvFidelityError(f"{flag_name} must not be empty.")
    return body


def resolve_optional_prose(
    inline: str | None,
    from_file: str | None,
    *,
    flag_name: str,
    allow_empty: bool = False,
) -> str | None:
    """Resolve an OPTIONAL prose flag, losslessly, or None when absent.

    resolve_body requires exactly one of its two arguments and raises when
    both are absent -- the wrong shape for a flag where absent is the
    common case (every C3-C11 flag this exists for). This function adds
    exactly one case on top of resolve_body: both `inline` and `from_file`
    absent returns None instead of raising. It does not wrap resolve_body's
    both-absent error to swallow it -- that would make an unreadable-file
    error and an absent flag indistinguishable at the call site, which is
    the shape this function exists to prevent. Every other case -- both
    supplied, inline-only, file-only, an unreadable file, a hollow result --
    delegates to resolve_body unchanged, including its exact error text.

    Before delegating, this calls refuse_newline_argv(inline,
    flag_name=flag_name) so a newline-bearing inline value is refused
    outright rather than reaching resolve_body's mutual-exclusion or
    empty checks first. This refusal is never platform-conditional -- it
    fires on every host, not only where cmd.exe's own truncation would
    otherwise apply, because the failure mode it prevents (a value already
    short by the time argparse sees it) is indistinguishable from a value
    that arrived intact but was deliberately short.

    `allow_empty` is passed straight through to resolve_body and carries
    the same restriction: it exists only for a flag whose emptiness is the
    caller's intent (the archive-stamp-cli correct-handoff-body
    --new-string "" precedent), never for a flag whose emptiness means
    "the caller forgot".

    Stdin sentinel: `from_file` of "-" is passed through to resolve_body,
    which reads sys.stdin.read() unchanged -- this function does not
    special-case it. On a non-tty stdin that is already at EOF (the common
    case for an agent's Bash tool, which attaches /dev/null), stdin.read()
    resolves to "", and resolve_body's own hollow-result check then raises
    ArgvFidelityError (unless allow_empty=True, in which case it silently
    resolves to ""). This function does not add or relax that behaviour;
    a caller passing `-` with allow_empty=True accepts that an
    already-exhausted stdin and a deliberately empty one are indistinguishable.
    """
    if inline is None and from_file is None:
        return None
    refuse_newline_argv(inline, flag_name=flag_name)
    return resolve_body(
        inline, from_file, flag_name=flag_name, allow_empty=allow_empty
    )


def refuse_newline_argv(
    value: str | None,
    *,
    flag_name: str,
    remedy: str | None = None,
) -> None:
    if value is None:
        return
    if "\n" in value:
        raise ArgvFidelityError(
            f"{flag_name} contains a newline; "
            + (remedy or f"pass {flag_name}-file instead.")
        )
