"""
coordinator_core.ops.parse_cli_args — JSON-RPC "cli.parse_flag" and
"cli.parse_date_flags" operations.

Purpose: a single reusable `$ARGUMENTS`-string parser shared by both ops (the
manifest collapses the pair into one module per the plan's Wave 2 chunk
design note). Ports two doctrine-fence shell idioms:

    - `case "$ARGUMENTS" in *--root*|*--target*) sed -En extract value ;; esac`
      (skills/repo-setup/SKILL.md:65) — generalized here into a single
      reusable `parse_flag(arguments, flag_names)` regex helper rather than
      hand-rolled `sed` per skill.
    - `--for-date <YYYY-MM-DD>` / `--only` extraction via `sed`, fail loud if
      `--only` lacks `--for-date` (commands/workday-complete.md:24).

Both are pure string parsing over an already-materialized `$ARGUMENTS`
string handed in as a param — no filesystem, no repo state, no shell.

Op-key / contract (frozen — see
`state/audits/2026-07-22-command-payload-inventory/op-classification.tsv`):

    cli.parse_flag
        params:   {arguments: str, flag_names: list[str]}
        response: {value: str|None, matched_flag: str|None}

    cli.parse_date_flags
        params:   {arguments: str}
        response: {for_date: str|None, only: bool}
        raises ValueError if only=True and for_date is None.

Scope-verdict `none` for both (per the classification manifest): a literal
string is parsed and handed back; no filesystem or repo-scoped state is
touched, so no `_origin_worktree`/`repo_root` resolution is required.

Negative-spec:
    - No `sed`/`case`/shell of any kind — pure `re` over the input string.
    - `parse_flag` takes an explicit `flag_names` list rather than a single
      hardcoded flag name, so the one regex helper serves every fence that
      today hand-rolls its own `sed` pattern.
    - `parse_date_flags`' `--only`-without-`--for-date` case raises
      `ValueError` rather than silently defaulting — mirrors the oracle's
      "fail loud if --only lacks --for-date" contract exactly; the handler
      surfaces this as a JSON-RPC error rather than swallowing it.
    - A flag with no following token, or a flag that is the last token in
      the string, resolves to `value: None` (matched_flag reports which
      name matched even when the value is absent) rather than raising or
      consuming an adjacent flag's own value.

Idempotency (AC7): both rows rate idempotency-hazard "none" per the
manifest — pure string parsing over a caller-supplied literal, no mutation,
no filesystem read. A second invocation with identical `arguments` returns a
byte-identical result; no DEC-7 note is required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op

_DATE_FLAG_NAMES: Tuple[str, ...] = ("--for-date",)
_ONLY_FLAG_NAMES: Tuple[str, ...] = ("--only",)


def _flag_pattern(flag_names: List[str]) -> re.Pattern:
    """Build a regex matching any of `flag_names` followed by an optional
    whitespace-delimited value token.

    Flag names are escaped and alternated longest-first so a shorter flag
    name that is a prefix of a longer one (e.g. `--for` vs `--for-date`)
    never shadows the longer match. The value group is non-greedy and
    stops at the next whitespace run or end-of-string, mirroring the
    `sed -En` extraction the oracle fence ran.
    """
    escaped = sorted((re.escape(name) for name in flag_names), key=len, reverse=True)
    alternation = "|".join(escaped)
    return re.compile(rf"(?P<flag>{alternation})(?:\s+(?P<value>\S+))?")


def parse_flag(arguments: str, flag_names: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Find the first occurrence of any name in `flag_names` within
    `arguments` and return `(value, matched_flag)`.

    `value` is the whitespace-delimited token immediately following the
    matched flag, or `None` when the flag has no following token (trailing
    flag, or immediately followed by another `--flag`). `matched_flag` is
    the literal flag name that matched (from `flag_names`, not a normalized
    form), or `None` if nothing matched.
    """
    if not arguments or not flag_names:
        return None, None
    match = _flag_pattern(flag_names).search(arguments)
    if match is None:
        return None, None
    value = match.group("value")
    if value is not None and value.startswith("--"):
        value = None
    return value, match.group("flag")


def parse_date_flags(arguments: str) -> dict:
    """Extract `--for-date <YYYY-MM-DD>` and `--only` from `arguments`.

    Raises `ValueError` when `--only` is present without `--for-date`,
    mirroring the oracle fence's "fail loud" contract exactly.
    """
    for_date, _ = parse_flag(arguments, list(_DATE_FLAG_NAMES))
    only_value, only_flag = parse_flag(arguments, list(_ONLY_FLAG_NAMES))
    only = only_flag is not None
    if only and for_date is None:
        raise ValueError(
            "cli.parse_date_flags: --only given without --for-date "
            f"(arguments={arguments!r})"
        )
    return {"for_date": for_date, "only": only}


@register_op("cli.parse_flag")
def _handler_parse_flag(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'cli.parse_flag' handler.

    `params["arguments"]` (contractual) is the raw `$ARGUMENTS` string;
    `params["flag_names"]` (contractual) is the list of flag spellings to
    search for.
    """
    arguments = params.get("arguments") or ""
    flag_names = list(params.get("flag_names") or [])
    value, matched_flag = parse_flag(arguments, flag_names)
    return {"value": value, "matched_flag": matched_flag}


@register_op("cli.parse_date_flags")
def _handler_parse_date_flags(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'cli.parse_date_flags' handler.

    `params["arguments"]` (contractual) is the raw `$ARGUMENTS` string.
    Propagates `parse_date_flags`'s `ValueError` on `--only` without
    `--for-date` — the JSON-RPC dispatch layer surfaces raised exceptions
    as error responses, so no local catch is needed here.
    """
    arguments = params.get("arguments") or ""
    return parse_date_flags(arguments)
