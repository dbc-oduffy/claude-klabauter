"""
coordinator_core.ops.query_completions — Port of: query-completions.sh
(DoE b5a4192c, 2026-07-20) (DOE-PORT R1, variant #1 — direct-import trampoline, no registered op).

Purpose: thin CLI wrapper over the native records-query seam
(``coordinator_core.ops.ceremony.records_query.query_records``) with
``record_type="completion"`` pre-set.

Node-subprocess retirement: this module originally shelled out to the co-located
DoE ``query-records.js`` (``subprocess.run([*resolve_launchable(...), "--type",
"completion", *argv])``, inheriting stdout). It now calls ``ceremony.records_query
.query_records`` in-process — no ``node`` binary, no subprocess spawn, no
``resolve_launchable``/``_script_dir`` co-location lookup. Only the flag surface
the old wrapper actually documented is supported: ``--since``, ``--where``,
``--limit``, ``--format``, ``--root``. ``--sort`` and ``--older-than`` were listed
in the bash oracle's usage banner but are NOT implemented by the native seam
(``ceremony.records_query``'s own negative-spec) — passing either now fails loud
with a clear message instead of silently behaving differently, per this repoint's
brief.

Output parity:
    - ``--format json`` emits the bare ``json.dumps(records, indent=2)`` array
      shape, matching query-records.js's ``JSON.stringify(records, null, 2)``
      (bin/query-records.js:1601-1603).
    - ``--format paths`` emits one repo-relative path per line
      (bin/query-records.js:1604).
    - default (``markdown-list``) renders natively via the same completion-type
      display format query-records.js's ``TYPE_DISPLAY.completion`` uses
      (bin/query-records.js:307-340, completion entry at :314): ``- **{title}**
      [{nature}] (chain: {chain|'none'}) — {commits joined, or 'no-commit'}``.
    - default ``--limit`` is 50 when the flag is omitted, matching
      query-records.js's ``parseArgs`` default (bin/query-records.js:525) — NOT the
      seam's own unlimited (``limit=0``) default. ``--limit 0`` is passed through
      as "unlimited", matching the oracle's ``opts.limit && opts.limit > 0`` guard
      (0 is falsy in JS, so an explicit 0 also disables the cap there).

Spec backlink: docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md § Chunk 2

Negative-spec:
    - Does NOT implement ``--sort`` or ``--older-than`` -- the native seam this
      module calls does not support either (see
      ``coordinator_core.ops.ceremony.records_query``'s own negative-spec); a user
      passing them gets a fail-loud stderr message and exit code 1, not a silent
      behavioral difference from the old node-forwarding wrapper.
    - Does NOT implement ``--unattached``/``--fleet``/synthetic types -- out of
      scope, ``record_type`` is hard-pinned to ``"completion"``.
    - Does NOT reproduce query-records.js's end-of-run ``_printSkippedSummary()``
      stderr line (a batch parse-failure count across the whole collected set) --
      the native seam surfaces per-file parse failures individually (or a single
      collect-error fail-open note), not a trailing summary count. Low-risk
      cosmetic gap, not a correctness difference.
    - Only inspects argv[0] for the ``--help``/``-h`` intercept, matching the bash
      oracle's ``[[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]`` guard (index 0
      only) -- ``-h``/``--help`` anywhere else in the argument list is parsed as a
      (now-unsupported) ordinary flag, same position-sensitivity as before.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.win_portability import no_console_creationflags

_HELP_TEXT = """query-completions.sh — Query completion-log entries.

Native reader over completion-log entries (record_type="completion"); no longer
forwards to query-records.js.

Usage:
  query-completions.sh [--since <date>] [--where "<expr>"]
                       [--limit N] [--root <path>]
                       [--format markdown-list|json|paths]

Examples:
  query-completions.sh --since 7d
  query-completions.sh --where "status=pending-release"
  query-completions.sh --since 2026-05-01 --format json

Unsupported (fails loud, does not silently differ): --sort, --older-than.
Default --limit is 50 when omitted; pass --limit 0 for unlimited.
"""

_SUPPORTED_FLAGS = {"--since", "--where", "--limit", "--format", "--root"}
_UNSUPPORTED_FLAGS = {"--sort", "--older-than"}
_VALID_FORMATS = ("markdown-list", "json", "paths")
_DEFAULT_LIMIT = 50  # query-records.js parseArgs() default (bin/query-records.js:525)

_MISSING = object()


def _js_field(fm: dict, key: str) -> str:
    """Render ``fm[key]`` the way a JS template literal (``${fm.key}``) would.

    A missing key stringifies as ``"undefined"``; a present-but-``None`` value
    stringifies as ``"null"`` -- distinguishing YAML-absent from YAML-null the
    same way JS distinguishes ``undefined`` from ``null``, for byte parity with
    query-records.js's ``TYPE_DISPLAY.completion`` (bin/query-records.js:314).
    """
    val = fm.get(key, _MISSING)
    if val is _MISSING:
        return "undefined"
    if val is None:
        return "null"
    return str(val)


def _format_completion_markdown(records: list) -> str:
    """Native port of ``TYPE_DISPLAY.completion`` (bin/query-records.js:314).

    ``- **{title}** [{nature}] (chain: {chain|'none'}) — {commits.join(', ')|'no-commit'}``
    """
    lines = []
    for rec in records:
        fm = rec["frontmatter"]
        title = _js_field(fm, "title")
        nature = _js_field(fm, "nature")
        chain = fm.get("chain") or "none"
        commits = fm.get("commits")
        commits_str = ", ".join(commits) if commits else "no-commit"
        lines.append(f"- **{title}** [{nature}] (chain: {chain}) — {commits_str}")
    return "\n".join(lines)


def _parse_args(argv: List[str]) -> dict:
    """Normalize ``--flag=value``/``--flag value`` forms and collect known flags.

    Mirrors query-records.js's ``--key=value`` normalization (bin/query-records.js
    :537-546) so both invocation styles work. Raises ``ValueError`` with a
    fail-loud message on an unsupported or malformed flag -- caller converts this
    to a stderr write + exit code 1.
    """
    normalized: List[str] = []
    for a in argv:
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            normalized.extend([k, v])
        else:
            normalized.append(a)

    opts = {"since": None, "where": None, "limit": str(_DEFAULT_LIMIT), "format": "markdown-list", "root": None}
    i = 0
    while i < len(normalized):
        flag = normalized[i]
        if flag in _UNSUPPORTED_FLAGS:
            raise ValueError(
                f"{flag} is not supported by the native records reader "
                f"(query-records.js's --sort/--older-than have no seam equivalent yet). "
                f"Supported flags: {', '.join(sorted(_SUPPORTED_FLAGS))}."
            )
        if flag not in _SUPPORTED_FLAGS:
            raise ValueError(
                f"unknown flag: {flag}. Supported flags: {', '.join(sorted(_SUPPORTED_FLAGS))}."
            )
        i += 1
        if i >= len(normalized):
            raise ValueError(f"{flag} requires a value.")
        opts[flag[2:].replace("-", "_")] = normalized[i]
        i += 1

    if opts["format"] not in _VALID_FORMATS:
        raise ValueError(f"invalid --format {opts['format']!r}. Valid: {', '.join(_VALID_FORMATS)}.")

    try:
        opts["limit"] = int(opts["limit"])
    except ValueError as exc:
        raise ValueError(f"invalid --limit {opts['limit']!r}: must be an integer.") from exc

    return opts


def _detect_root(specified: Optional[str]) -> Path:
    """Port of query-records.js's ``detectRoot`` (bin/query-records.js:585-591):
    ``--root`` if given, else ``git rev-parse --show-toplevel``, else cwd.
    """
    if specified:
        return Path(specified).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        return Path.cwd()
    if result.returncode != 0:
        return Path.cwd()
    return Path(result.stdout.strip())


def main(argv: List[str]) -> int:
    """CLI entry: --help intercept, else query completion records natively."""
    if argv and argv[0] in ("--help", "-h"):
        sys.stdout.write(_HELP_TEXT)
        return 0

    try:
        opts = _parse_args(argv)
    except ValueError as exc:
        sys.stderr.write(f"query-completions: {exc}\n")
        return 1

    root = _detect_root(opts["root"])

    try:
        records = query_records(
            "completion",
            root,
            where=opts["where"],
            since=opts["since"],
            limit=opts["limit"],
        )
    except SystemExit as exc:
        # query_records propagates sys.exit(1) for an unparseable --where clause
        # or invalid --since value (bin/query-records.js's own parseWhereExpr /
        # parseSince do the same, stderr message already written) -- same
        # fail-loud contract as the retired node-forwarding call's exit code.
        return int(exc.code) if isinstance(exc.code, int) else 1

    if opts["format"] == "json":
        output = json.dumps(records, indent=2)
    elif opts["format"] == "paths":
        output = "\n".join(r["path"] for r in records)
    else:
        output = _format_completion_markdown(records)

    if output:
        sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
