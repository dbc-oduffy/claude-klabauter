"""refresh_queries — byte-parity port of coordinator/bin/refresh-queries.js.

Expands query callouts in markdown files in-place. Callout format:

    <!-- BEGIN query: <type> [where=...] [sort=...] [limit=N] [since=...] -->
    ... (current expansion, will be overwritten) ...
    <!-- END query -->

The spec line (the BEGIN marker) is the source of truth; the block between
the markers is regenerated. Running twice with no data changes produces no
diff (idempotent) — EXCEPT for the duplicate-identical-marker bug inherited
from the oracle, see "Negative-spec" below.

Port source: coordinator/bin/refresh-queries.js (432 lines)
Spec backlink: archive/specs/2026-05-01-portable-ideas-from-obsidian-research.md
§W2 (Refresh Helper)
Port recipe: DoE scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-normalize-snippet.md (byte-parity port discipline)

query-records.js dependency — RESOLVED, native (2026-07-22):
    The oracle imports two DoE-side sibling modules: coordinator/bin/lib/
    sentinel-blocks.js (ported — coordinator_core.text.sentinel_blocks,
    reused directly below) and coordinator/bin/query-records.js (1924
    lines — its queryRecords()/formatRecords() pair). This module used to
    treat query-records.js as a live cross-language dependency, bridging to
    it via a bounded `node -e` subprocess. That bridge is RETIRED:
    queryRecords() is now
    `coordinator_core.ops.ceremony.records_query.query_records` (an
    in-process port covering every type it enumerates — handoff,
    handoff-archived, plan, cross-repo-memo, bug, debt, improvement,
    tracker, roadmap, health-status, decision-guide, completion), and
    formatRecords() is `coordinator_core.text.query_record_display.
    format_records` (a native port of the TYPE_DISPLAY renderer table,
    bounded to the same type set the native reader can enumerate — see
    that module's own negative-spec for the types deliberately NOT
    covered: lesson, decision, review, handoff-ledger, and the deep-
    research/sidecar types, none of which have a native queryRecords
    equivalent yet). See `_run_query_records_native` below. `node` is no
    longer a runtime dependency of this module.

Exit-code contract (main()) — HARDENED per addendum rule 3/3b (the oracle
conflates CLI-usage-error and business-fail into a single exit 1; this port
does not, since --check is invoked as a CI gate and a caller must be able
to tell "content is out of sync" apart from "this run was itself broken"):
    0 — success: no out-of-sync callouts found (or --check passed clean),
        and no per-callout processing errors.
    1 — BUSINESS fail: --check found >=1 out-of-sync file, OR >=1 callout
        hit a processing error (bad query type, malformed spec, missing END
        marker, sentinel-blocks replace failure, an unparseable --where/
        --since value). Matches the oracle's conflated exit 1 for these
        business-shaped outcomes.
    2 — CLI usage error (unknown argument). The oracle exits 1 for this via
        parseArgs' own process.exit(1) — deliberately NOT reproduced, so a
        caller cannot mistake a typo'd flag for "your docs are stale".
    3 — TRANSPORT failure: the native query+format call for ONE callout
        raised an exception outside its own documented business-error
        shapes (unknown query type, bad --where/--since) — i.e. an
        unexpected crash inside `query_records`/`format_records` itself.
        Distinguishable from a business "query type unknown" failure
        (folded into the per-callout warning/errorCount path, exit 1,
        exactly as the oracle's own try/catch around queryRecords does).
        Preserves this port's pre-existing per-callout crash-isolation
        property (see the next negative-spec) now that there is no
        subprocess boundary to isolate the crash for free.

Negative-spec (faithful oracle bug — DO NOT "fix" silently):
    Two callouts in the SAME file with an IDENTICAL begin-marker line (same
    type/where/sort/limit/since/format) only ever get the FIRST one
    refreshed. This traces to coordinator/bin/lib/sentinel-blocks.js's
    replaceBlock() doing `content.indexOf(beginMarker)` — a first-occurrence
    string search, not "the occurrence we just found" — so a second
    identical marker's replaceBlock() call re-targets (and no-ops against)
    the first occurrence, leaving the second stale forever. Confirmed live
    against the node oracle 2026-07-17 (two identical `lesson
    where=scope=nonexistent-marker-xyz` callouts in one file: only the
    first is refreshed; run reports "1 callout(s)" changed, not 2). This
    port reuses coordinator_core.text.sentinel_blocks.replace_block, which
    already ports that same `.find()`-first-occurrence semantics 1:1 — so
    the bug reproduces automatically, not via special-cased code here.

Negative-spec (deliberate, DOCUMENTED divergence from the oracle, not a
scope-drop): if formatRecords() throws inside the oracle's per-callout loop,
the exception is UNCAUGHT and crashes the entire node process (aborting the
whole file walk, not just that one callout). This port's native call is a
plain in-process function call per callout (no subprocess boundary since the
2026-07-22 de-node port); `_run_query_records_native` catches any exception
outside its own documented business-error shapes and raises
QueryRecordsTransportError for THAT callout only (surfaced to the caller as
exit 3), while other files already processed keep their on-disk writes.
This is strictly more resilient than the oracle, not a silent narrowing of
its documented behavior — called out here per addendum rule 7 (the isolation
property itself predates the de-node port; only the mechanism changed, from
a subprocess boundary to an explicit try/except).

Session self-claim (SCOPE-DROP CLOSED 2026-07-27):
    The oracle used to register every written path with the active
    coordinator session via lib/coordinator_session.js's selfClaim()
    (best-effort, no-op if the lib/session is absent). That JS shim is
    retired, and this module now calls the in-process Python equivalent,
    coordinator_core.session.claims.self_claim(path), for every path
    process_file() actually writes (never in --check mode, which writes
    nothing). Best-effort, matching the oracle: self_claim() itself never
    raises except on an empty path (never true here), and the call site
    additionally catches OSError/ValueError so a self-claim failure can
    never break the write it accompanies. track_touched_files.py (a
    PostToolUse hook keyed on Edit/Write/MultiEdit/NotebookEdit tool-call
    payloads) does not fire for this module's plain open()/write() calls, so
    there is no double-claim risk — claims.self_claim's own
    atomic_dedup_append still dedups regardless.

Roadmap-callout archive-follow (bug fix, 2026-08-04, cross-repo/inbox/
2026-08-04-example-market-data-repo-em-baton-terminal-state-not-cleared-
programmatically.md defect 2):
    ``_TYPE_TO_GLOB['handoff']`` (state/handoffs/*.md only) never followed a
    baton into ``archive/handoffs/`` — so a `<!-- BEGIN query: handoff
    where=roadmap_id=... -->` callout (every such callout on disk here is a
    roadmap `STUB-INDEX.md` chunk-status tracker) silently dropped a row the
    moment its baton archived, and — since nothing else re-triggers this
    refresh on archival — kept rendering that row's LAST live snapshot
    (frequently a stale `in_flight`) with a now-dead `../../handoffs/...`
    link, forever, until something else happened to touch the file. Fixed
    in ``_run_query_records_native`` by unioning in ``query_records(
    "handoff-archived", ..., where=<same where>)`` whenever the callout's
    type is ``handoff`` and its ``where`` clause carries ``roadmap_id=`` —
    the same live+archived union
    ``coordinator_core.roadmap.audit``'s stub-coverage checks already
    perform explicitly for the identical reason. Deliberately NOT a
    ``_TYPE_TO_GLOB['handoff']`` widening: that glob is relied on elsewhere
    (``session_hierarchy_derive.py``, ``ceremony/renderers.py``,
    ``roadmap/number_stubs.py``) for strictly-live semantics with no
    ``roadmap_id`` filter in play, so this fix is scoped to the
    roadmap_id-filtered shape only. Each archived record's own
    ``deployment_state`` (present on every archived baton's frontmatter,
    e.g. ``shipped``) renders correctly through the *same* ``handoff``
    ``TYPE_DISPLAY`` entry used for live records — no display-layer change
    needed — and its link resolves against its real
    ``archive/handoffs/**`` path (``_load_record``'s own ``rel_path``),
    never a dead ``state/handoffs/`` guess. A stub_id present in NEITHER
    ``state/handoffs/`` nor ``archive/handoffs/`` (a dangling predecessor
    pointer) simply does not appear in either query's result set — no
    crash, no dead link, degrading to silent omission rather than a false
    `in_flight` claim.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, TypedDict

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.session import claims
from coordinator_core.text.sentinel_blocks import replace_block
from coordinator_core.text.query_record_display import format_records
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.records_query import _sort_records

MUTATES = ["**/*.md"]

BEGIN_PREFIX = "<!-- BEGIN query:"
END_MARKER = "<!-- END query -->"

# roadmap STUB-INDEX chunk-status tracker (every such callout on disk in
# this repo is exactly that shape — `state/roadmap/*/STUB-INDEX.md`) rather
# same two calls rather than widening `_TYPE_TO_GLOB['handoff']` itself
_ROADMAP_ID_WHERE_RE = re.compile(r"(?:^|[\s(])roadmap_id=")

EXCLUDED_DIRS = frozenset({"node_modules", ".git", "archive"})


class ArgParseError(Exception):
    pass


class QueryRecordsBusinessError(Exception):
    pass


class QueryRecordsTransportError(Exception):
    pass


class ProcessFileResult(TypedDict):
    changed: bool
    changedCount: int
    errorCount: int


class ParsedArgs(TypedDict):
    root: Optional[str]
    check: bool
    files: Optional[str]


class QuerySpec(TypedDict):
    type: str
    where: Optional[str]
    sort: Optional[str]
    limit: object
    since: Optional[str]
    format: str


def parse_args(argv: List[str]) -> ParsedArgs:
    opts: ParsedArgs = {"root": None, "check": False, "files": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--root":
            i += 1
            opts["root"] = argv[i] if i < len(argv) else None
        elif a == "--check":
            opts["check"] = True
        elif a == "--files":
            i += 1
            opts["files"] = argv[i] if i < len(argv) else None
        else:
            raise ArgParseError(f"Unknown argument: {a}")
        i += 1
    return opts


def detect_root(specified: Optional[str]) -> str:
    if specified:
        return os.path.abspath(specified)
    out = show_toplevel()
    if out:
        return out
    return os.getcwd()


_LIMIT_LEADING_INT_RE = re.compile(r"^\s*[+-]?\d+")


def _parse_int_like_js(s: str) -> Optional[int]:
    m = _LIMIT_LEADING_INT_RE.match(s)
    if not m:
        return None
    return int(m.group(0))


def parse_query_spec(begin_marker: str) -> QuerySpec:
    inner = re.sub(r"^<!--\s*BEGIN query:\s*", "", begin_marker)
    inner = re.sub(r"\s*-->$", "", inner).strip()

    tokens = re.split(r"\s+", inner) if inner else [""]
    type_ = tokens[0]
    if not type_:
        raise ValueError(f"Empty query spec in: {begin_marker}")

    opts: QuerySpec = {
        "type": type_,
        "where": None,
        "sort": None,
        "limit": 50,
        "since": None,
        "format": "markdown-list",
    }

    for t in tokens[1:]:
        if t.startswith("where="):
            opts["where"] = t[len("where=") :]
        elif t.startswith("sort="):
            opts["sort"] = t[len("sort=") :]
        elif t.startswith("limit="):
            opts["limit"] = _parse_int_like_js(t[len("limit=") :])
        elif t.startswith("since="):
            opts["since"] = t[len("since=") :]
        elif t.startswith("format="):
            opts["format"] = t[len("format=") :]

    return opts


def walk_md(dir_: str, results: Optional[List[str]] = None) -> List[str]:
    if results is None:
        results = []
    try:
        entries = list(os.scandir(dir_))
    except OSError:
        return results

    for e in entries:
        try:
            is_dir = e.is_dir(follow_symlinks=False)
            is_file = e.is_file(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if e.name not in EXCLUDED_DIRS:
                walk_md(e.path, results)
        elif is_file and e.name.endswith(".md"):
            results.append(e.path)
    return results


def build_code_block_line_set(content: str) -> set:
    lines = content.split("\n")
    in_code: set = set()
    inside = False
    fence: Optional[str] = None
    for i, line in enumerate(lines):
        trimmed = line.lstrip()
        if not inside:
            if trimmed.startswith("```") or trimmed.startswith("~~~"):
                inside = True
                fence = trimmed[:3]
        else:
            in_code.add(i)
            if fence is not None and trimmed.startswith(fence):
                inside = False
                fence = None
    return in_code


def line_of_offset(content: str, offset: int) -> int:
    return content.count("\n", 0, offset)


def _run_query_records_native(
    query_opts: QuerySpec, root: str, from_dir: Optional[str] = None
) -> str:
    record_type = query_opts["type"]
    where = query_opts.get("where")
    since = query_opts.get("since")
    sort = query_opts.get("sort")
    limit = query_opts.get("limit")
    fmt = query_opts.get("format") or "markdown-list"

    captured_stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured_stderr):
            records = query_records(record_type, Path(root), where=where, since=since, limit=0)
            if record_type == "handoff" and where and _ROADMAP_ID_WHERE_RE.search(where):
                archived = query_records(
                    "handoff-archived", Path(root), where=where, since=since, limit=0
                )
                records = records + archived
    except ValueError as exc:
        sys.stderr.write(captured_stderr.getvalue())
        raise QueryRecordsBusinessError(str(exc)) from exc
    except SystemExit as exc:
        message = captured_stderr.getvalue().strip() or f"invalid query spec: {query_opts!r}"
        raise QueryRecordsBusinessError(message) from exc
    except Exception as exc:  # noqa: BLE001 — isolate to this callout, see docstring above
        sys.stderr.write(captured_stderr.getvalue())
        raise QueryRecordsTransportError(f"native records query crashed: {exc}") from exc
    else:
        sys.stderr.write(captured_stderr.getvalue())

    if sort:
        records = _sort_records(records, sort)
    if isinstance(limit, int) and limit > 0:
        records = records[:limit]

    try:
        return format_records(
            records,
            {"type": record_type, "format": fmt},
            root=Path(root),
            from_dir=Path(from_dir) if from_dir else Path(root),
        )
    except Exception as exc:  # noqa: BLE001 — isolate to this callout, see docstring above
        raise QueryRecordsTransportError(f"native format_records crashed: {exc}") from exc


#  coordinator/bin/refresh-queries.js L175-432)


def process_file(file_path: str, root: str, check_mode: bool) -> ProcessFileResult:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return {"changed": False, "changedCount": 0, "errorCount": 0}

    if BEGIN_PREFIX not in content:
        return {"changed": False, "changedCount": 0, "errorCount": 0}

    working = content
    changed_count = 0
    error_count = 0
    offset = 0

    while True:
        idx = working.find(BEGIN_PREFIX, offset)
        if idx == -1:
            break

        code_lines = build_code_block_line_set(working)
        marker_line = line_of_offset(working, idx)
        if marker_line in code_lines:
            offset = idx + len(BEGIN_PREFIX)
            continue

        line_start_idx = idx
        while line_start_idx > 0 and working[line_start_idx - 1] != "\n":
            line_start_idx -= 1
        text_before_marker = working[line_start_idx:idx]
        backticks_before = text_before_marker.count("`")
        if backticks_before % 2 == 1:
            offset = idx + len(BEGIN_PREFIX)
            continue

        line_end = working.find("\n", idx)
        if line_end == -1:
            break
        begin_marker = working[idx:line_end].strip()

        try:
            query_opts = parse_query_spec(begin_marker)
        except ValueError as exc:
            sys.stderr.write(f"  Warning: {file_path}: {exc}\n")
            error_count += 1
            offset = line_end + 1
            continue

        if END_MARKER not in working[line_end:]:
            sys.stderr.write(f"  Warning: {file_path}: BEGIN query without END query\n")
            error_count += 1
            offset = line_end + 1
            continue

        try:
            expansion = _run_query_records_native(
                query_opts, root, os.path.dirname(file_path)
            )
        except QueryRecordsBusinessError as exc:
            sys.stderr.write(f"  Warning: {file_path}: query failed: {exc}\n")
            error_count += 1
            offset = line_end + 1
            continue

        updated = replace_block(
            working, begin_marker, END_MARKER, expansion + "\n" if expansion else ""
        )
        if updated is None:
            sys.stderr.write(
                f"  Warning: {file_path}: sentinel-blocks replaceBlock returned null\n"
            )
            error_count += 1
            offset = line_end + 1
            continue

        if updated != working:
            changed_count += 1
        working = updated
        offset = idx + len(begin_marker)
        if offset >= len(working):
            break

    if changed_count > 0:
        if not check_mode:
            with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(working)
            try:
                claims.self_claim(file_path)
            except (OSError, ValueError) as exc:
                sys.stderr.write(
                    f"  Warning: {file_path}: self-claim failed (advisory only, "
                    f"write unaffected): {exc}\n"
                )
        return {"changed": True, "changedCount": changed_count, "errorCount": error_count}
    return {"changed": False, "changedCount": 0, "errorCount": error_count}


def resolve_files_opt(files_opt: str, root: str) -> List[str]:
    entries = [s.strip() for s in files_opt.split(",") if s.strip()]
    resolved: List[str] = []
    for entry in entries:
        abs_path = entry if os.path.isabs(entry) else os.path.abspath(os.path.join(root, entry))
        if not abs_path.endswith(".md"):
            continue
        try:
            if os.path.isfile(abs_path):
                resolved.append(abs_path)
        except OSError:
            pass

    if len(resolved) < len(entries):
        sys.stderr.write(
            f"Note: {len(entries) - len(resolved)} of {len(entries)} "
            "--files entries skipped (not found or not .md)\n"
        )
    return resolved


def main(argv: List[str]) -> int:
    try:
        opts = parse_args(argv)
    except ArgParseError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    root = detect_root(opts["root"])

    files = resolve_files_opt(opts["files"], root) if opts["files"] is not None else walk_md(root)

    total_changed = 0
    total_errors = 0

    try:
        for f in files:
            result = process_file(f, root, opts["check"])
            if result["changed"]:
                rel = os.path.relpath(f, root)
                label = "[would change]" if opts["check"] else "[updated]"
                print(f"{label} {rel} ({result['changedCount']} callout(s))")
                total_changed += 1
            total_errors += result["errorCount"]
    except QueryRecordsTransportError as exc:
        sys.stderr.write(f"refresh-queries: transport failure: {exc}\n")
        return 3

    if total_changed == 0 and total_errors == 0:
        print("All query callouts are up to date.")

    if opts["check"] and total_changed > 0:
        print(
            f"\n{total_changed} file(s) have out-of-sync query callouts. "
            "Run refresh-queries to fix.",
            file=sys.stderr,
        )
        return 1

    if total_errors > 0:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
