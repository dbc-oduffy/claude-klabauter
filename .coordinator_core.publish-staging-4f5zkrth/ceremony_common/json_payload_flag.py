"""coordinator_core.ceremony_common.json_payload_flag — resolves a JSON
payload CLI flag from either an inline argv token or a file, for the eleven
`--decisions` parse sites across the ceremony CLIs. (`workday_complete.brief`
also names `--decisions`, but it EMITS one into a directive args list that
`workday_complete.apply` dispatches in-process via importlib -- never a
command line -- so it has no corrupting transport to avoid and is not wired
here.)

Background: every generated `.cmd` forwarder ends in
`"%_py%" "%~dp0<target>" %*`, and on Windows that line silently strips
double quotes from, and splits on spaces within, any un-re-quoted `%*`
argument. A JSON payload — which is nothing but quotes and, for any
multi-key object, spaces — cannot survive that channel intact. The file
form (`--<flag>-file <path>`) exists so the payload never has to travel as
a command-line argument through a `.cmd` forwarder at all; the path itself
is quote-and-space-free by construction, so it survives.

Spec backlink:
docs/plans/2026-08-18-quote-safe-payloads-through-the-cmd-forw.md, chunk C1

Negative-spec:
    - Does NOT print, exit, or raise for a user-input error (missing value,
      unreadable file, malformed JSON, or a token that isn't ours). All
      twelve call sites already have their own error vocabulary and exit
      codes — one returns `_usage("pickup-assemble")`, another returns
      `int(WorkstreamApplyExitCode.TRANSPORT_FAIL)`, and they do not agree.
      A helper that owns error rendering could not be wired into sites
      whose rendering it does not match, so every error path here returns
      a message string for the caller to render in its own idiom instead.
    - Does NOT enforce payload SHAPE. `validate_decisions_shape` (or each
      site's equivalent) still runs afterwards, unchanged, on whatever this
      module hands back as `value`.
    - Does NOT detect the both-supplied case from a single token — that
      needs the whole argv, so it is a separate function
      (`detect_conflicting_payload_channels`) the caller invokes once
      before its token loop, not folded into `resolve_json_payload_flag`.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple, Optional, Sequence


class JsonPayloadFlag(NamedTuple):
    """The result of attempting to resolve one JSON-payload flag at argv
    index `i`. `consumed` is the number of tokens belonging to this flag —
    0 when `tokens[i]` is not one of `--<flag>` / `--<flag>-file` at all,
    else 2 (the flag token plus its value). `error`, when set, carries no
    program-name prefix — the caller prefixes its own, matching how each
    of the eleven existing sites already renders its diagnostics."""

    consumed: int
    value: Optional[Any]
    error: Optional[str]


def resolve_json_payload_flag(
    tokens: Sequence[str], i: int, flag: str = "decisions"
) -> JsonPayloadFlag:
    """Resolves `--<flag>` or `--<flag>-file` at `tokens[i]`, returning a
    `JsonPayloadFlag`. Neither form is preferred over the other here —
    `detect_conflicting_payload_channels` is what rejects supplying both,
    since that requires scanning the whole argv rather than one token.

    The file form reads UTF-8 explicitly and parses with the same
    `json.loads` the inline form uses, so a malformed payload produces the
    identical diagnostic shape regardless of which channel carried it —
    only the file path is appended, as provenance.

    `utf-8-sig`, not `utf-8`: Windows PowerShell 5.1's
    `Set-Content -Encoding utf8` writes a BOM, so the most obvious way for
    an operator on this platform to produce a payload file yields bytes
    that plain `utf-8` rejects with `Unexpected UTF-8 BOM`. That surfaces
    as `malformed --decisions JSON`, pointing the reader at their own
    payload — the same misdirection the file channel exists to end.
    `utf-8-sig` strips a BOM when present and decodes plain UTF-8
    unchanged when it is not."""
    inline_flag = f"--{flag}"
    file_flag = f"--{flag}-file"
    token = tokens[i]

    if token == inline_flag:
        if i + 1 >= len(tokens):
            return JsonPayloadFlag(consumed=1, value=None, error=f"--{flag} requires a value")
        raw = tokens[i + 1]
        try:
            return JsonPayloadFlag(consumed=2, value=json.loads(raw), error=None)
        except json.JSONDecodeError as exc:
            return JsonPayloadFlag(
                consumed=2,
                value=None,
                error=(
                    f"malformed --{flag} JSON: {exc}"
                    f"{_inline_transport_hint(raw, flag)}"
                ),
            )

    if token == file_flag:
        if i + 1 >= len(tokens):
            return JsonPayloadFlag(
                consumed=1, value=None, error=f"--{flag}-file requires a value"
            )
        path = tokens[i + 1]
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                raw = handle.read()
        except OSError as exc:
            return JsonPayloadFlag(
                consumed=2, value=None, error=f"--{flag}-file unreadable: {path}: {exc}"
            )
        try:
            return JsonPayloadFlag(consumed=2, value=json.loads(raw), error=None)
        except json.JSONDecodeError as exc:
            return JsonPayloadFlag(
                consumed=2,
                value=None,
                error=f"malformed --{flag} JSON (from {path}): {exc}",
            )

    return JsonPayloadFlag(consumed=0, value=None, error=None)


def _inline_transport_hint(raw: str, flag: str) -> str:
    """Returns a suffix naming the TRANSPORT when a payload that failed to
    parse carries the fingerprint of `.cmd`-forwarder quote-stripping, else
    an empty string.

    The bare `malformed --<flag> JSON` diagnostic blames the caller's
    payload, and on Windows that is usually wrong: the operator's JSON was
    well-formed and the `.cmd` forwarder's `%*` ate the double quotes on the
    way through, so the parser sees `{lesson-worth-capturing:{disposition:
    skip}}`. The error is correct-looking and misdirects — an EM who trusts
    it rewrites a payload that was never malformed instead of changing
    vehicle (cross-repo/inbox/2026-08-20-doe-claude-em-cmd-forwarder-eats-
    json-and-two-smaller-seams.md, item 1).

    Fingerprint, deliberately narrow: the received value opens a JSON
    container but contains no `"` at all. A stripped payload cannot contain
    one by construction, and a genuinely malformed payload that also happens
    to be entirely quote-free is not a shape any of these call sites'
    decisions maps can take — every key is a string. Truncation by cmd.exe's
    8191-char cap is a DIFFERENT failure with its own message (`The command
    line is too long.`) and never reaches this parse, so it is not hinted at
    here.

    Negative-spec:
        - Does NOT inspect the process's own argv, environment, or parent
          to confirm a `.cmd` forwarder was really the vehicle. The hint is
          phrased as a possibility to check, not an assertion, precisely
          because this module cannot see the transport. Claiming a cause it
          did not observe would trade one confident misdirection for
          another.
        - Does NOT fire for the file channel. A payload that travelled as a
          file never crossed a forwarder, so the hint would be noise there.
    """
    stripped = raw.lstrip()
    if not stripped.startswith(("{", "[")):
        return ""
    if '"' in raw:
        return ""
    return (
        f" — the received value contains no double quotes at all, the"
        f" fingerprint of a `.cmd` forwarder eating them on Windows;"
        f" the payload as sent may have been well-formed."
        f" Pass it as a file instead: --{flag}-file <path>."
    )

def detect_conflicting_payload_channels(
    tokens: Sequence[str], flag: str = "decisions"
) -> Optional[str]:
    """Scans the whole argv for both `--<flag>` and `--<flag>-file` present
    together — a usage error `resolve_json_payload_flag` cannot see from a
    single token, since it only inspects one flag position at a time. The
    caller invokes this once, before its token loop, and treats a non-`None`
    return as a usage-error message to render in its own vocabulary."""
    inline_flag = f"--{flag}"
    file_flag = f"--{flag}-file"
    has_inline = inline_flag in tokens
    has_file = file_flag in tokens
    if has_inline and has_file:
        return f"--{flag} and --{flag}-file are mutually exclusive"
    return None
