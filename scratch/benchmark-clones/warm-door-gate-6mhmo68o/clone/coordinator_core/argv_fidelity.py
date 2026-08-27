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
Raising (instead of returning a body, error tuple, as cross-repo-memo.py's
_read_body_from_file_or_stdin does) means a caller cannot forget to check
an error return before using the resolved body.

Message register: refusal text follows docs/wiki/guard-messaging.md §
Register -- one fact, stated once, plus the terse alternative naming
--body-file. No self-legitimacy, no repetition, no reassurance, no
apology, never an override key.
"""
from __future__ import annotations

import sys


class ArgvFidelityError(ValueError):
    """Raised by resolve_body / refuse_newline_argv on a caller-facing failure.

    A ValueError subclass so a caller that only expects ValueError (e.g. an
    existing broad except clause) still catches it; callers that want the
    argparse-shaped usage+message+exit-2 behaviour catch it explicitly and
    call parser.error(str(exc)).
    """


def resolve_body(
    body: str | None,
    body_file: str | None,
    *,
    flag_name: str = "--body",
) -> str:
    """Resolve a body from either an argv value or a file, losslessly.

    Exactly one of `body` / `body_file` must be provided -- both present or
    both absent raises ArgvFidelityError. `body_file` of "-" reads stdin
    (the Unix curl/tar/git convention); any other `body_file` value is read
    as a UTF-8 file path. An unreadable path, or a resolved body that is
    empty (or all-whitespace), raises ArgvFidelityError rather than
    returning a hollow record.

    `flag_name` names the argv-value flag in error messages (e.g. "--body");
    the file-transport flag is always derived as f"{flag_name}-file".
    """
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
        if not resolved.strip():
            raise ArgvFidelityError(
                f"{body_file_flag} resolved to an empty body; pass a "
                f"non-empty file, '-' with non-empty stdin, or {flag_name}."
            )
        return resolved

    assert body is not None  # narrowed by the two guards above
    if not body.strip():
        raise ArgvFidelityError(f"{flag_name} must not be empty.")
    return body


def refuse_newline_argv(value: str | None, *, flag_name: str) -> None:
    """Raise ArgvFidelityError if `value` contains a newline.

    `value` is expected to be an argv-sourced string (e.g. args.body) --
    file-sourced text is never passed here, since a file is expected to
    carry real newlines. Does nothing when `value` is None (flag absent).
    """
    if value is None:
        return
    if "\n" in value:
        raise ArgvFidelityError(
            f"{flag_name} contains a newline; pass {flag_name}-file instead."
        )
