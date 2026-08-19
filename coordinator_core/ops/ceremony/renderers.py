"""
coordinator_core.ops.ceremony.renderers — pure-Python ports of the ceremony-tail
node render/regen scripts, replacing the ``node``/``bash`` subprocess spawns the
OLD ``wsc_commit.py`` tail shelled out to.

C8c (this chunk): ``refresh_roadmap_callout`` — port of ``refresh-queries.js``'s
BEGIN/END query-block regen (432 LOC in the node source), scoped to the single
real call shape the old caller invoked: ``--files <one path>``
against ``state/roadmap/<roadmap_id>/STUB-INDEX.md``. ``_ROADMAP_ID_ALLOWLIST_RE``
is defined here as the single canonical copy — it was ported from the OLD
``wsc_commit.py`` (retired 2026-07-29, kill-list op removal) and ``tail_ops.py``
imports it from this module rather than carrying its own compiled copy, so the
two guards can never silently drift apart.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C8c

Negative-spec (C8c, refresh_roadmap_callout):
  - Does NOT implement the full ``refresh-queries.js`` CLI surface (``--check``,
    unscoped ``--root`` full-tree walk, the self-claim shim). Only the
    ``--files <single-path>`` shape the roadmap-callout wrapper actually invokes
    — the wrapper is the sole production caller of this regen logic.
  - Does NOT implement query types beyond ``handoff`` / ``handoff-archived`` /
    ``cross-repo-memo`` — the same bounded set C8a's ``records_query`` helper
    serves; an unrecognized query type in a callout's BEGIN line degrades to a
    per-callout warning + error count (mirrors the node original's behavior),
    never a hard crash.
  - Does NOT implement the full ``query-records.js`` ``--where`` engine (``<``,
    ``>``, ``in``, ``!=``, ``<=``, ``>=``, ``--since``) — reuses C8a's
    ``query_records``, which is equality-AND-only (same bounded scope).
  - Does NOT let the roadmap_id allowlist regex exist in more than one place —
    this module owns the single canonical ``_ROADMAP_ID_ALLOWLIST_RE`` copy;
    ``tail_ops.py`` (the only other consumer) imports it from here instead of
    carrying its own compiled copy, so the two guards can never silently drift
    apart.

``_collect_handoffs_with_parse_errors`` (general-purpose helper, retained):
does NOT call C8a's ``query_records`` for its scan — that helper silently
skips unparseable files by design (its own negative-spec defers fail-loud
surfacing to this caller). This function does its own frontmatter-optional
scan so parse-error handoffs are surfaced (``frontmatter: None`` stub rows),
never dropped. See ``test_renderers_unreadable_handoff.py`` for the
unreadable-file contract this promise covers.
"""

from __future__ import annotations
import sys

# Generator-provenance declaration: every render function in this module is
# pure and returns a markdown string with no disk I/O of its own. The former
# C9 disk seam, coordinator_core.ops.ceremony.render_handoff_tracker (this
# module's only in-tree writer), was retired 2026-08-14 along with the
# handoff-tracker render path -- see docs/plans/2026-08-14-retire-the-
# handoff-tracker-and-project-tracker-renders.md § C2.
GENERATES = []

import os
import re
from pathlib import Path
from typing import Any, Callable, Optional, Set

from coordinator_core.dag import (
    _read_meta,
    as_history_membership_set,
    build_git_history_cache,
    resolve_target,
)
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops.ceremony.records_query import _collect_files, query_records
from coordinator_core.ops.fleet._common import rel_id
from coordinator_core.ops.records_query import _apply_consumed_marker
from coordinator_core.ops._relative_link import relative_markdown_target

# roadmap_id is attacker-influenceable frontmatter on a shared work/* branch and is
# interpolated into a subprocess arg -- mirrors the DoE pickup skill's allowlist guard.
# Canonical single copy (see module docstring C8c negative-spec) — originally ported
# from the OLD wsc_commit.py, which no longer exists; ``tail_ops.py`` imports this
# name rather than compiling its own copy.
_ROADMAP_ID_ALLOWLIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

class _GitHistoryCacheProvider:
    """Lazily builds ``dag.build_git_history_cache(repo_root)`` on the FIRST
    tier-3 need and memoizes the result — including a ``None`` result from a
    git failure — for the rest of one render.

    Rationale: a render whose every lineage/plan candidate resolves in tier
    1 (live) or tier 2 (archived-on-disk) — the healthy steady state a repo
    trends toward as archives get swept properly — never touches tier 3 at
    all. Building the cache unconditionally at render start would pay one
    ``git log --all --name-only --no-renames`` subprocess spawn on EVERY
    ceremony/commit render for a tier that's never consulted. Deferring the
    build to the first ``_resolve_candidate_path`` call that actually falls
    through to it means the all-clean case costs zero subprocess spawns.

    ``_built`` is a separate flag, not "is ``_cache`` still ``None``?",
    because a git failure legitimately produces a ``None`` cache — collapsing
    "haven't tried yet" and "tried, git failed" onto the same sentinel would
    re-spawn (and re-fail) git on every subsequent candidate in exactly the
    corpus (not a repo, git missing) where the retry is guaranteed to fail
    again, defeating the whole point of building "at most once per render".
    """

    __slots__ = ("_repo_root", "_built", "_cache")

    def __init__(self, repo_root: str) -> None:
        self._repo_root = repo_root
        self._built = False
        self._cache: Optional[Set[str]] = None

    def get(self) -> Optional[Set[str]]:
        """Return the built cache, building it on the FIRST call only —
        every subsequent call (including after a git failure) returns the
        memoized result without spawning a second subprocess."""
        if not self._built:
            self._cache = build_git_history_cache(self._repo_root)
            self._built = True
        return self._cache

# ---------------------------------------------------------------------------
# Op-result key, matching the OLD wsc_commit.py's _OP_ROADMAP_CALLOUT constant
# shape (acted/skipped/failed dict) — C9 will wire this into the new tail under
# whatever op-key name it chooses; this module only produces the result shape.
# ---------------------------------------------------------------------------
_OP_ROADMAP_CALLOUT = "renderers:refresh_roadmap_callout"

_BEGIN_PREFIX = "<!-- BEGIN query:"
_END_MARKER = "<!-- END query -->"


# ---------------------------------------------------------------------------
# Per-type markdown-list display functions (port of query-records.js's
# TYPE_DISPLAY, bounded to the three types records_query.py (C8a) serves).
# ---------------------------------------------------------------------------


def _display_handoff(link_path: str, fm: dict) -> str:
    title = fm.get("title") or os.path.basename(link_path)
    state = fm.get("deployment_state") or fm.get("status") or "unknown"
    return f"- [{title}]({link_path}) — {state}"


def _display_handoff_archived(link_path: str, fm: dict) -> str:
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    shipped_in = fm.get("shipped_in")
    suffix = f" (shipped: {shipped_in})" if shipped_in else ""
    return f"- [{title}]({link_path}) — {status}{suffix}"


def _display_cross_repo_memo(link_path: str, fm: dict) -> str:
    title = fm.get("title") or os.path.basename(link_path)
    status = fm.get("status") or "unknown"
    from_repo = fm.get("from") or "?"
    return f"- [{title}]({link_path}) — {status} (from {from_repo})"


_TYPE_DISPLAY: dict[str, Callable[[str, dict], str]] = {
    "handoff": _display_handoff,
    "handoff-archived": _display_handoff_archived,
    "cross-repo-memo": _display_cross_repo_memo,
}


# ---------------------------------------------------------------------------
# Query spec parser — parses "<!-- BEGIN query: type [key=value ...] -->"
# ---------------------------------------------------------------------------


def _parse_query_spec(begin_marker: str) -> dict[str, Any]:
    """Parse a BEGIN-query marker line into ``{type, where, sort, limit}``.

    Mirrors ``refresh-queries.js``'s ``parseQuerySpec`` — strips the
    ``<!-- BEGIN query:`` prefix / `` -->`` suffix, tokenizes on whitespace,
    and recognizes ``where=``/``sort=``/``limit=`` key=value tokens. Unknown
    tokens are ignored (forward-compat), matching the node original.
    """
    inner = begin_marker
    if inner.startswith("<!--"):
        # Strip "<!--" + optional whitespace + "BEGIN query:" (allow whitespace variance).
        stripped = inner[4:].lstrip()
        prefix = "BEGIN query:"
        if stripped.startswith(prefix):
            inner = stripped[len(prefix):]
        else:
            inner = stripped
    inner = inner.strip()
    if inner.endswith("-->"):
        inner = inner[: -len("-->")]
    inner = inner.strip()

    tokens = inner.split()
    if not tokens:
        raise ValueError(f"Empty query spec in: {begin_marker}")

    qtype = tokens[0]
    opts: dict[str, Any] = {"type": qtype, "where": None, "sort": None, "limit": 50}

    for tok in tokens[1:]:
        if tok.startswith("where="):
            opts["where"] = tok[len("where="):]
        elif tok.startswith("sort="):
            opts["sort"] = tok[len("sort="):]
        elif tok.startswith("limit="):
            try:
                opts["limit"] = int(tok[len("limit="):])
            except ValueError:
                print(f"skip: _parse_query_spec: opts[\"limit\"] = int(tok[len(\"limit=\"):]) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
        # Unknown tokens (e.g. format=) are ignored — forward compat, same as the node original.

    return opts


# ---------------------------------------------------------------------------
# Sort — records_query.py (C8a) has no --sort; applied here as a post-query step,
# mirroring query-records.js's queryRecords, which applies --sort after --where.
# ---------------------------------------------------------------------------


def _compare_values(a: str, b: str) -> int:
    """Numeric compare when both coerce to float; else lexicographic string compare."""
    try:
        na, nb = float(a), float(b)
        return -1 if na < nb else (1 if na > nb else 0)
    except (TypeError, ValueError):
        print(f"skip: _compare_values: na, nb = float(a), float(b) failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _sort_records(records: list[dict], sort_spec: Optional[str]) -> list[dict]:
    if not sort_spec:
        return records
    desc = sort_spec.startswith("-")
    field = sort_spec[1:] if desc else sort_spec

    def _key_str(rec: dict) -> str:
        val = rec.get("frontmatter", {}).get(field, "")
        return "" if val is None else str(val)

    import functools

    def _cmp(a: dict, b: dict) -> int:
        c = _compare_values(_key_str(a), _key_str(b))
        return -c if desc else c

    return sorted(records, key=functools.cmp_to_key(_cmp))


# ---------------------------------------------------------------------------
# Sentinel-block replace — port of lib/sentinel-blocks.js's findMarkers/replaceBlock.
# ---------------------------------------------------------------------------


def _find_markers(content: str, begin_marker: str, end_marker: str) -> Optional[dict]:
    bi = content.find(begin_marker)
    if bi == -1:
        return None
    ei = content.find(end_marker, bi + len(begin_marker))
    if ei == -1:
        return None

    begin_line_start = bi
    while begin_line_start > 0 and content[begin_line_start - 1] != "\n":
        begin_line_start -= 1
    begin_line_end = bi + len(begin_marker)
    if begin_line_end < len(content) and content[begin_line_end] == "\r":
        begin_line_end += 1
    if begin_line_end < len(content) and content[begin_line_end] == "\n":
        begin_line_end += 1

    end_line_start = ei
    while end_line_start > 0 and content[end_line_start - 1] != "\n":
        end_line_start -= 1
    end_line_end = ei + len(end_marker)
    if end_line_end < len(content) and content[end_line_end] == "\r":
        end_line_end += 1
    if end_line_end < len(content) and content[end_line_end] == "\n":
        end_line_end += 1

    text_before_begin = content[begin_line_start:bi]
    text_before_end = content[end_line_start:ei]

    begin_is_own_line = text_before_begin.strip() == ""
    end_is_own_line = text_before_end.strip() == ""

    return {
        "begin_start": begin_line_start if begin_is_own_line else bi,
        "begin_end": begin_line_end if begin_is_own_line else bi + len(begin_marker),
        "end_start": end_line_start if end_is_own_line else ei,
        "end_end": end_line_end if end_is_own_line else ei + len(end_marker),
    }


def _replace_block(content: str, begin_marker: str, end_marker: str, new_block: str) -> Optional[str]:
    pos = _find_markers(content, begin_marker, end_marker)
    if pos is None:
        return None

    head = content[: pos["begin_end"]]
    tail = content[pos["end_start"]:]

    body = new_block
    if body and not body.endswith("\n"):
        body += "\n"

    return head + body + tail


# ---------------------------------------------------------------------------
# Fenced-code-block / inline-backtick detection — port of buildCodeBlockLineSet
# + lineOfOffset + the inline-backtick-span check inline in processFile.
# ---------------------------------------------------------------------------


def _build_code_block_line_set(content: str) -> set[int]:
    lines = content.split("\n")
    in_code: set[int] = set()
    inside = False
    fence: Optional[str] = None
    for i, raw_line in enumerate(lines):
        trimmed = raw_line.lstrip()
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


def _line_of_offset(content: str, offset: int) -> int:
    return content.count("\n", 0, offset)


# ---------------------------------------------------------------------------
# formatRecords — markdown-list rendering with depth-correct link rewriting.
# ---------------------------------------------------------------------------


def _format_records(records: list[dict], query_opts: dict, root: Path, from_dir: Path) -> str:
    display_fn = _TYPE_DISPLAY.get(query_opts["type"])
    lines: list[str] = []
    for rec in records:
        fn = display_fn or (lambda p, fm: f"- [{fm.get('title') or p}]({p})")
        rel_path = rec["path"]
        hash_idx = rel_path.find("#")
        path_part = rel_path if hash_idx == -1 else rel_path[:hash_idx]
        fragment = "" if hash_idx == -1 else rel_path[hash_idx:]
        link_path = os.path.relpath(str((root / path_part).resolve()), str(from_dir)).replace("\\", "/")
        link_path += fragment
        lines.append(fn(link_path, rec.get("frontmatter") or {}))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# processFile — port of refresh-queries.js's processFile, scoped to the single
# explicit-path shape the roadmap-callout wrapper invokes (--files <one path>).
# ---------------------------------------------------------------------------


def _process_file(file_path: Path, root: Path, *, check_mode: bool = False) -> dict[str, Any]:
    """Expand every ``<!-- BEGIN query: ... -->`` callout in ``file_path`` in place.

    Returns ``{"changed": bool, "changed_count": int, "error_count": int}``.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        print(f"skip: _process_file: content = file_path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {"changed": False, "changed_count": 0, "error_count": 0}

    if _BEGIN_PREFIX not in content:
        return {"changed": False, "changed_count": 0, "error_count": 0}

    working = content
    changed_count = 0
    error_count = 0
    offset = 0

    while True:
        idx = working.find(_BEGIN_PREFIX, offset)
        if idx == -1:
            break

        code_lines = _build_code_block_line_set(working)
        marker_line = _line_of_offset(working, idx)
        if marker_line in code_lines:
            offset = idx + len(_BEGIN_PREFIX)
            continue

        line_start_idx = idx
        while line_start_idx > 0 and working[line_start_idx - 1] != "\n":
            line_start_idx -= 1
        text_before_marker = working[line_start_idx:idx]
        backticks_before = text_before_marker.count("`")
        if backticks_before % 2 == 1:
            offset = idx + len(_BEGIN_PREFIX)
            continue

        line_end = working.find("\n", idx)
        if line_end == -1:
            break
        begin_marker = working[idx:line_end].strip()

        if _END_MARKER not in working[line_end:]:
            error_count += 1
            offset = line_end + 1
            continue

        try:
            query_opts = _parse_query_spec(begin_marker)
        except ValueError:
            print(f"skip: _process_file: query_opts = _parse_query_spec(begin_marker) failed: {sys.exc_info()[1]}", file=sys.stderr)
            error_count += 1
            offset = line_end + 1
            continue

        qtype = query_opts["type"]
        try:
            records = query_records(qtype, root, where=query_opts["where"], limit=0)
        except (ValueError, SystemExit):
            print(f"skip: _process_file: records = query_records(qtype, root, where=query_opts[\"where\"], limit= failed: {sys.exc_info()[1]}", file=sys.stderr)
            error_count += 1
            offset = line_end + 1
            continue

        records = _sort_records(records, query_opts["sort"])
        limit = query_opts.get("limit") or 0
        if limit > 0:
            records = records[:limit]

        from_dir = file_path.parent
        expansion = _format_records(records, query_opts, root, from_dir)

        updated = _replace_block(
            working, begin_marker, _END_MARKER, expansion + "\n" if expansion else ""
        )
        if updated is None:
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
            file_path.write_text(working, encoding="utf-8", newline="\n")
        return {"changed": True, "changed_count": changed_count, "error_count": error_count}
    return {"changed": False, "changed_count": 0, "error_count": error_count}


# ---------------------------------------------------------------------------
# Top-level entrypoint — mirrors the OLD wsc_commit.py's
# _tail_refresh_roadmap_callout return contract ({acted, skipped, failed}), but
# in-process (no ``bash``/``node`` subprocess spawn).
# ---------------------------------------------------------------------------


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        return value[1:-1]
    return value


def refresh_roadmap_callout(worktree_root: Path, roadmap_id: str) -> dict[str, Any]:
    """Re-render a roadmap's ``STUB-INDEX.md`` query callout, in-process.

    Disposable render nicety — every failure mode degrades to a clean skip,
    matching the OLD ``_tail_refresh_roadmap_callout`` contract this
    replaces. Never raises on a missing roadmap dir, a missing
    callout, or an invalid ``roadmap_id``.
    """
    roadmap_id = _strip_matching_quotes(roadmap_id or "")

    if not roadmap_id or ".." in roadmap_id or not _ROADMAP_ID_ALLOWLIST_RE.match(roadmap_id):
        return {"acted": [], "skipped": [f"{_OP_ROADMAP_CALLOUT}:no-roadmap-id"], "failed": []}

    stub_index = worktree_root / "state" / "roadmap" / roadmap_id / "STUB-INDEX.md"
    if not stub_index.is_file():
        return {
            "acted": [], "skipped": [f"{_OP_ROADMAP_CALLOUT}:stub-index-not-found"], "failed": [],
        }

    try:
        content = stub_index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: refresh_roadmap_callout: content = stub_index.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {
            "acted": [], "skipped": [f"{_OP_ROADMAP_CALLOUT}:stub-index-not-readable"], "failed": [],
        }

    if _BEGIN_PREFIX not in content:
        return {"acted": [], "skipped": [f"{_OP_ROADMAP_CALLOUT}:no-query-callout"], "failed": []}

    result = _process_file(stub_index, worktree_root)

    if result["error_count"] > 0:
        return {
            "acted": [], "skipped": [],
            "failed": [f"{_OP_ROADMAP_CALLOUT}:{result['error_count']}-callout-error(s)"],
        }

    if result["changed"]:
        return {
            "acted": [f"{_OP_ROADMAP_CALLOUT}:{rel_id(stub_index, worktree_root)}"],
            "skipped": [], "failed": [],
        }

    return {"acted": [], "skipped": [f"{_OP_ROADMAP_CALLOUT}:up-to-date"], "failed": []}


# =============================================================================
# ---------------------------------------------------------------------------
# Markdown table helpers
# ---------------------------------------------------------------------------


def _md_link(text: Optional[str], target: Optional[str]) -> str:
    if not text or not target:
        return text or ""
    return f"[{text}]({target})"


def _cell(val) -> str:
    if val is None or val == "":
        return ""
    return str(val)


def _render_table(headers: list[str], rows: list[list]) -> str:
    """Render a markdown table from headers and rows (list of lists).

    Padding is minimal (no column alignment), matching the node CLI's
    machine-generated output. Mirrors ``renderTable``
    (render-handoff-tracker.js:282-291).
    """
    if not rows:
        return "_none_"
    sep = ["---" for _ in headers]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(sep) + " |",
        *("| " + " | ".join(_cell(v) for v in row) + " |" for row in rows),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parse-error-aware handoff scan (frontmatter=None rows surfaced, not dropped)
# ---------------------------------------------------------------------------


def _collect_handoffs_with_parse_errors(root: Path) -> list[dict]:
    """Enumerate ``state/handoffs/*.md`` in readdir order, returning parse-error
    stubs (``frontmatter: None``) inline with parsed records.

    C8a's ``query_records`` silently skips unparseable files by design (its own
    negative-spec defers fail-loud surfacing to this caller). Mirrors the
    ``includeUnparseable: true`` path in ``query-records.js`` (queryRecords:1386-1393)
    as consumed by ``render-handoff-tracker.js``'s ``allHandoffOpts``.
    """
    results: list[dict] = []
    for fpath in _collect_files(root, "handoff"):
        rel_path = rel_id(fpath, root)
        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError as exc:
            # DIAGNOSTIC FIX (2026-07-22): this function's own docstring
            # promises "frontmatter=None rows surfaced, not dropped" — an
            # unreadable file was previously dropped via a bare `continue`,
            # contradicting that promise. Now it surfaces the same stub shape
            # the parse-error path below already uses.
            print(f"skip: _collect_handoffs_with_parse_errors: text = fpath.read_text(encoding=\"utf-8\") failed: {exc}", file=sys.stderr)
            results.append({"path": rel_path, "frontmatter": None})
            continue

        split = split_frontmatter(text)
        if split is None:
            results.append({"path": rel_path, "frontmatter": None})
            continue

        fm = _read_meta(str(fpath))
        if not fm:
            results.append({"path": rel_path, "frontmatter": None})
            continue

        _apply_consumed_marker(fm, split.body_with_leading_newline)
        results.append({"path": rel_path, "frontmatter": fm})

    return results


# ---------------------------------------------------------------------------
# Plans join — ``docs/plans/*.md`` frontmatter joined onto
# ``predecessor_handoff:`` against ``state/handoffs/`` rows. Single collection
# helper feeds ``render_plans_index_markdown`` (the full plans index) — its
# former co-consumer, ``render_repo_section`` (the tracker's compact
# per-handoff annotation + remainder pointer), was retired 2026-08-14 (see
# docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-
# renders.md § C2) — see module negative-spec for why a second parser is out
# of scope.
# ---------------------------------------------------------------------------

#: Review-sidecar filename suffixes excluded from the plans glob (grep
#: ``docs/plans/*.md`` at authoring time for the live set; extend here if a
#: new sidecar convention is adopted).
_PLAN_SIDECAR_SUFFIXES = (
    ".docs-check",
    ".eng-director-review",
    ".node-map",
    ".the Staff Engineer-review",
    ".plan-coverage-check",
    ".prior-art-check",
    ".review",
    ".review-the Director of Engineering",
    ".sonnet-review",
)


#: ``docs/plans/*.md`` entries that index the directory rather than being plans
#: in it. ``INDEX.md`` is this module's own render target — without this
#: exclusion the generated index counts itself as a plan and the tracker's
#: remainder pointer drifts one ahead of the index it points at.
_PLAN_DIR_INDEX_FILENAMES = frozenset({"INDEX.md", "README.md"})


def _is_plan_sidecar(filename: str, existing_names: frozenset[str] = frozenset()) -> bool:
    """True if ``filename`` (a ``docs/plans/*.md`` entry) is a review sidecar or
    a directory index, not a base plan.

    Two rules, OR-ed together (union, not replacement):

    1. The exact-suffix allowlist (``_PLAN_SIDECAR_SUFFIXES``) — the
       original, enumerated set. Kept because the structural rule below
       requires the base plan to still exist on disk: a sidecar whose base
       plan was deleted would otherwise silently start counting as a plan
       (a regression the suffix list alone never had).
    2. STRUCTURAL: ``filename``'s stem has a proper dot-prefix (e.g.
       ``foo`` or ``foo.bar`` out of ``foo.bar.baz``) such that
       ``<prefix>.md`` exists as a file in the same directory
       (``existing_names``). This catches every drifted/timestamped/
       reversed-word-order sidecar convention (``.enrichment-note``,
       ``.review-integration``, ``.review-the Staff Engineer``, ``.coverage-check``,
       ``.plan-coverage-check.2026-07-21T13-22-52Z``, ...) without needing
       each one individually enumerated — the enumeration approach is what
       drifted in the first place.

    Review: staff-eng F5 — this structural rule is convention-blind, not
    just suffix-blind: on the live corpus it excludes exactly 5 files, 4 of
    them intended timestamped sidecars but the 5th
    (``2026-07-05-strang-05-advisory-hook-degrade-silent-routing.phase0.md``)
    a genuine authored deliverable doc that merely happens to sit alongside
    a same-stem-prefixed base file. Any future ``<plan>.<companion>.md``
    naming convention — not just review sidecars — will vanish from the
    plans index the same way. Known casualty, not fixed here: the false
    positive is low-harm (one doc silently omitted from a generated index)
    and disambiguating "genuine companion doc" from "sidecar" would require
    content inspection this structural, filename-only rule was deliberately
    kept out of.
    """
    if filename in _PLAN_DIR_INDEX_FILENAMES:
        return True
    stem = filename[: -len(".md")] if filename.endswith(".md") else filename
    if any(stem.endswith(suffix) for suffix in _PLAN_SIDECAR_SUFFIXES):
        return True
    tokens = stem.split(".")
    for i in range(1, len(tokens)):
        candidate = ".".join(tokens[:i]) + ".md"
        if candidate != filename and candidate in existing_names:
            return True
    return False


def _collect_plans_with_parse_errors(root: Path) -> list[dict]:
    """Enumerate base ``docs/plans/*.md`` files (review sidecars excluded),
    returning parse-error stubs (``frontmatter: None``) inline with parsed
    records — same shape as ``_collect_handoffs_with_parse_errors``.

    Sorted alphabetically (``os.listdir`` + ``sorted``) for deterministic,
    cross-platform output; unlike the handoff scan this has no node-parity
    requirement of its own, but a stable order still avoids readdir-order
    flakiness across OSes.
    """
    plans_dir = root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []

    all_names = sorted(os.listdir(plans_dir))
    existing_names = frozenset(n for n in all_names if (plans_dir / n).is_file())

    results: list[dict] = []
    for name in all_names:
        # ``name not in existing_names`` skips directories named ``*.md`` —
        # without it such an entry reaches ``read_text`` below and degrades
        # into a spurious parse-error stub instead of being ignored.
        if (
            not name.endswith(".md")
            or name not in existing_names
            or _is_plan_sidecar(name, existing_names)
        ):
            continue
        fpath = plans_dir / name
        rel_path = rel_id(fpath, root)
        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"skip: _collect_plans_with_parse_errors: fpath.read_text(encoding=\"utf-8\") failed: {exc}", file=sys.stderr)
            results.append({"path": rel_path, "frontmatter": None})
            continue

        split = split_frontmatter(text)
        if split is None:
            results.append({"path": rel_path, "frontmatter": None})
            continue

        fm = _read_meta(str(fpath))
        if not fm:
            results.append({"path": rel_path, "frontmatter": None})
            continue

        results.append({"path": rel_path, "frontmatter": fm})

    return results


def _plan_slug(rel_path: str) -> str:
    """``docs/plans/2026-07-19-foo.md`` -> ``2026-07-19-foo``."""
    name = os.path.basename(rel_path)
    return name[: -len(".md")] if name.endswith(".md") else name


def _normalize_path(value: str) -> str:
    """Backslash -> forward slash, for cross-platform ``predecessor_handoff:``
    comparison (a Windows-authored plan may record backslashes)."""
    return value.replace("\\", "/")


def _index_handoffs_by_basename(dir_path: Path, root: Path) -> dict[str, str]:
    """Map ``basename -> repo-relative path`` for every ``.md`` file under
    ``dir_path``, searched recursively (covers both the flat
    ``archive/handoffs/X.md`` layout and the month-subfoldered
    ``archive/handoffs/2026-07/X.md`` one — both exist on disk today).

    First match wins on a basename collision (``sorted`` for determinism);
    collisions are not expected in practice and are not specially diagnosed.
    """
    index: dict[str, str] = {}
    if not dir_path.is_dir():
        return index
    for fpath in sorted(dir_path.rglob("*.md")):
        if fpath.is_file():
            index.setdefault(fpath.name, rel_id(fpath, root))
    return index


def _classify_handoff_location(path: Path, root: Path) -> str:
    """Classify a candidate path (known to exist on disk — every caller only
    invokes this after its own ``.is_file()`` check) as ``"live"``/
    ``"archived"`` when it sits under ``state/handoffs/`` or
    ``archive/handoffs/`` (recursively), else ``"invalid-target"`` — the
    file exists but is the wrong KIND of file (e.g. ``forked_from:`` pointing
    at a ``docs/problems/...`` doc, not a handoff). Existence and validity
    are different questions; conflating "wrong kind" with "gone" would hide
    a real schema violation behind the same label as a pruned/deleted file.

    ``_join_plans_to_handoffs`` maps ``"invalid-target"`` back onto its own
    ``"gone"`` state explicitly (plans rendering has no fourth state and
    must stay byte-identical).
    """
    for state_name, base in (
        ("live", root / "state" / "handoffs"),
        ("archived", root / "archive" / "handoffs"),
    ):
        try:
            path.relative_to(base)  # fs-only: membership check, never stringified
            return state_name
        except ValueError:
            continue
    return "invalid-target"


def _resolve_candidate_path(
    candidate: str,
    root: Path,
    live_index: dict[str, str],
    archive_index: dict[str, str],
    *,
    handoff_dir: Optional[Path] = None,
    git_history_cache: Optional[_GitHistoryCacheProvider] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve one already-normalized candidate path against both handoff
    trees, falling through to a git-history tier-3 check when neither tree
    resolves it. Used by ``_join_plans_to_handoffs`` — the shared resolution
    algorithm this join is held to (reuse, not a second parallel resolver).

    Tries, in order: (1) the candidate as a literal repo-relative path
    (handles a value already pointing into ``archive/handoffs/...``, or a
    not-yet-moved ``state/handoffs/...`` path); (2) a basename-only lookup
    against both trees (handles a bare basename value — the convention a
    handoff's own ``predecessor:`` field commonly uses — and a
    ``state/handoffs/...``-shaped value whose target has since moved to
    archive under a different subpath); (3) when ``handoff_dir`` is supplied
    and neither tree resolved it, ``coordinator_core.dag.resolve_target`` —
    the existing 3-tier resolver (live ∪ archive-on-disk ∪ git-history) —
    to distinguish a disk-absent-but-ever-git-tracked target ("pruned but
    recoverable") from one truly unresolvable anywhere ("gone").

    Returns ``(resolution_state, resolved_path, archived_path)`` —
    ``resolution_state`` in ``"live"`` / ``"archived"`` / ``"pruned"`` /
    ``"gone"`` / ``"invalid-target"``. ``resolved_path``/``archived_path``
    are both ``None`` for ``"pruned"`` — there is no path on disk to point
    at, only git history's word that the target once existed.
    ``"invalid-target"`` is only reachable via the literal-path branch
    (``_classify_handoff_location`` is never consulted on the
    basename-fallback branch) — a caller with no fourth/fifth state of its
    own (the plans join) must collapse both ``"invalid-target"`` and
    ``"pruned"`` to ``"gone"`` itself; that disposition differs between
    callers and is not this function's call to make.

    Tier-3 dispatch note: this function does NOT reimplement any
    git-history logic — it calls ``dag.resolve_target`` (which itself
    re-tries tiers 1/2 before falling through to tier 3) rather than
    extracting just the git-check portion, so there is exactly one
    git-history resolution algorithm in the codebase, not two. When
    ``handoff_dir`` is omitted (the caller doesn't want tier-3 — e.g. a
    context with no meaningful repo root), this function's behavior is
    byte-identical to its pre-tier-3 form: it stops at ``"gone"``.
    """
    candidate_path = root / candidate
    if candidate_path.is_file():
        kind = _classify_handoff_location(candidate_path, root)
        if kind == "live":
            return "live", candidate, None
        elif kind == "archived":
            return "archived", None, candidate
        return "invalid-target", None, None

    basename = os.path.basename(candidate)
    if basename in live_index:
        return "live", live_index[basename], None
    if basename in archive_index:
        return "archived", None, archive_index[basename]

    if handoff_dir is not None:
        # Materialize the cache HERE — the first (and only) point in a
        # render where tier-3 is actually needed — not upfront. See
        # _GitHistoryCacheProvider's docstring for why this is what makes
        # the all-clean-resolves-in-tiers-1/2 case spawn zero subprocesses.
        cache = git_history_cache.get() if git_history_cache is not None else None
        # Strip any `dag.GitHistoryCache` (with its `.complete` flag) down to
        # a bare `set` before handing it to `resolve_target` — see
        # `dag.as_history_membership_set`'s docstring for the full rationale
        # (this provider's cache is built at most once and reused for the
        # REST of one render, so a target pruned/committed after the
        # snapshot was taken must still resolve correctly within the same
        # render). A HIT stays free either way (membership test on a bare set
        # is just as O(1)), so this changes nothing for the perf-critical
        # accept path — only a MISS is affected, and only in the
        # falls-through-to-per-call direction.
        cache = as_history_membership_set(cache)
        resolved = resolve_target(candidate, str(handoff_dir), str(root), cache)
        if resolved == "git-history":
            return "pruned", None, None

    return "gone", None, None


#: Case-insensitive sentinel values meaning "not declared" for an ID-valued
#: frontmatter field (``deliverable_id``, formerly ``plan_id``/
#: ``origin_plan_id``). Review: staff-eng F7 — a defensive guard, not
#: theoretical: ``coordinator_core/frontmatter/primitives.py`` (~line 334)
#: documents that ``field: null  # comment`` reads back as the truthy
#: STRING ``"null"``, not Python ``None``. Without this guard, that shape
#: would satisfy a bare ``if val:`` truthiness check and mass-join every
#: such plan/handoff onto one arbitrary index bucket keyed on the literal
#: string ``"null"``.
_ID_NULL_SENTINELS = frozenset({"", "null"})


def _normalize_id_value(raw: Any) -> Optional[str]:
    """Normalize a declared ID-valued frontmatter field (``deliverable_id``)
    to either a candidate identifier string or ``None`` ("not declared").
    See ``_ID_NULL_SENTINELS`` for why this is more than a bare truthiness
    check."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _ID_NULL_SENTINELS:
        return None
    return text


def _index_handoffs_by_field(root: Path, field_name: str) -> dict[str, list[tuple[str, str]]]:
    """Map a declared handoff frontmatter field value -> every ``(path, state)``
    handoff (live or archived) that declares it, across BOTH ``state/handoffs/``
    (live) and ``archive/handoffs/**`` (archived).

    Backs the ID-based plans join (``deliverable_id``) —
    the identifier-valued counterpart to ``_index_handoffs_by_basename``'s
    path-valued index. Reuses ``query_records`` (the C8a helper already used
    elsewhere in this module for the same two record types), rather than a
    second parallel disk scan.
    """
    index: dict[str, list[tuple[str, str]]] = {}
    for state_name, qtype in (("live", "handoff"), ("archived", "handoff-archived")):
        for rec in query_records(qtype, root):
            fm = rec.get("frontmatter")
            if not fm:
                continue
            val = _normalize_id_value(fm.get(field_name))
            if val is None:
                continue
            index.setdefault(val, []).append((rec["path"], state_name))
    return index


def _resolve_deliverable_match(
    matches: list[tuple[str, str]]
) -> tuple[Optional[tuple[str, str]], int]:
    """Resolve possibly-several ``deliverable_id``-based (path, state)
    matches to either a single unambiguous winner or an ambiguity count.

    Review: staff-eng F1 — the prior ``_pick_best_handoff_match`` picked a
    live-then-lexicographically-first candidate unconditionally, silently
    collapsing genuine ownership ambiguity (measured: 12 of 40
    ``deliverable_id``-resolved plans on the live corpus have 2-8 candidate
    handoffs) into a single arbitrary edge with no signal reaching the
    render. This index exists to distinguish ownership from mention, so a
    coin-flip presented as an authoritative edge is worse than an honest
    non-answer (see ``state/lessons/2026-08-03-a-determinate-read-of-an-
    incomplete-reco-d84fa0d42c89.yaml``).

    Live still wins over archived when both tiers have matches — that is
    not ambiguity, it is normal "superseded by a live re-delivery" shape.
    Ambiguity is instead: more than one candidate survives WITHIN the
    winning tier (e.g. 2+ live handoffs independently declaring the same
    ``deliverable_id``). Returns ``(match, 0)`` when exactly one candidate
    survives in the winning tier, or ``(None, N)`` when ``N > 1`` candidates
    tie within that tier — the caller routes the latter to ``"gone"`` with
    an ambiguity reason rather than picking one.
    """
    if not matches:
        return None, 0
    live_matches = [m for m in matches if m[1] == "live"]
    pool = live_matches or matches
    if len(pool) > 1:
        return None, len(pool)
    return pool[0], 0


def _join_plans_to_handoffs(
    plan_records: list[dict], root: Path, *, git_history_cache: Optional[_GitHistoryCacheProvider] = None
) -> list[dict]:
    """Join each plan record onto its owning handoff, if resolvable, via one
    of TWO independently-attempted edges, in order:

      1. ``predecessor_handoff:`` — a PATH-valued edge, resolved via
         ``_resolve_candidate_path`` against BOTH the live and archived
         handoff trees (unchanged from before this join grew ID-based
         resolution).
      2. ``deliverable_id:`` — an IDENTIFIER-valued edge. Confirmed
         genuinely joinable: the SAME ``dlv-...`` value is independently
         declared on both a plan and the handoff(s) that deliver it (e.g.
         ``docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md`` and
         ``state/handoffs/2026-07-22_162946_wsc-rebuild-port-regression-audit.md``
         both carry ``dlv-wsc-pure-python-rebuild-dd4482``) — not merely
         format-alike. Resolved via ``_index_handoffs_by_field``, ambiguity
         handled by ``_resolve_deliverable_match`` (see F1 review comment
         there).

    ``plan_id:`` <-> ``origin_plan_id:`` was a THIRD edge attempted here and
    is now DROPPED (review: staff-eng F2). It was justified in prose by "43/43
    live values resolve" — that claim does not reproduce: on the live corpus
    ``_index_handoffs_by_field(root, "origin_plan_id")`` yields 6 index keys,
    not 43 (74 of 78 live handoffs carry ``origin_plan_id: null``), and the
    edge resolves 0 of 162 plans. Removed rather than re-derived, so a future
    session doesn't re-attempt it on the strength of a false docstring claim —
    the handoff side simply does not populate ``origin_plan_id`` on this
    corpus.

    ``stub_id`` was investigated and DROPPED: 1 plan declares it, 10
    handoffs declare it, and none of the values overlap (plan:
    ``strang-11``; handoff values are a disjoint ``strang-*``/``sat-*``/
    ``qsub-*`` roadmap-stub namespace) — it does not genuinely resolve to a
    handoff, so no edge is fabricated for it (per this join's own
    "honest narrower coverage beats a wrong join" mandate).

    Each edge is attempted independently and short-circuits on the first
    one that resolves to ``"live"``/``"archived"`` — ``predecessor_handoff``
    first (existing, most-specific edge), then ``deliverable_id``. A
    ``deliverable_id`` matching more than one handoff within its winning
    tier is NOT picked (see ``_resolve_deliverable_match``) — it counts as
    unresolved and carries an explicit ambiguity count instead, so ambiguous
    ownership is never silently collapsed to one arbitrary edge. A plan with
    no edge resolving at all (including one with nothing declared) reports
    ``"gone"``.

    Returns one entry per plan record:
        {"path", "slug", "status",
         "declared_predecessor" (or None), "declared_deliverable_id" (or None),
         "ambiguous_deliverable_id_count" (0, or N > 1 when the deliverable_id
         edge tied across N candidates in its winning tier), "resolution_method"
         ("predecessor_handoff" | "deliverable_id" | None),
         "resolution_state" ("live" | "archived" | "gone"),
         "resolved_handoff_path" (or None), "archived_handoff_path" (or None)}

    ``status`` degrades to ``"unknown"`` when absent (no enum/schema for this
    field — see baton negative-spec).

    Three-state resolution (root cause: a target archived-then-pruned is a
    normal lifecycle outcome, not bad data — see caller docstrings):
      - ``"live"``     — target found under ``state/handoffs/``.
        ``resolved_handoff_path`` is set; ``archived_handoff_path`` is None.
      - ``"archived"`` — target found under ``archive/handoffs/**`` (flat or
        month-subfoldered). ``archived_handoff_path`` is set;
        ``resolved_handoff_path`` stays None — that field's meaning (a LIVE
        target) is preserved for existing callers keyed off it (e.g. the
        tracker's per-handoff-row plan annotation, which only ever applies to
        live handoff rows).
      - ``"gone"``     — found in neither tree, AND (deliberate D5 collapse,
        see below) also covers a target that is disk-absent but
        git-recoverable (``"pruned"`` at the ``_resolve_candidate_path``
        layer). Covers a dangling pointer (field present, target deleted
        outright), a git-recoverable-but-pruned target, and an absent
        ``predecessor_handoff:`` (field never set) — ``declared_predecessor``
        is what lets a render distinguish the first two from the third,
        since flattening all of them to "no predecessor" loses the
        actionable ones.

    D5 — pruned/recoverable collapses to "gone" here: the plans join
    answers "is this plan's predecessor handoff still reachable" (a plan
    with a git-recoverable-but-pruned predecessor has no live tracker row
    either way — recoverability doesn't change the render action). Same
    explicit-branch treatment as the existing ``invalid-target`` collapse
    below, not a silent fallthrough.

    ``predecessor_handoff`` resolution tries, in order: (1) the declared
    value as a literal repo-relative path (handles a value that already
    points into ``archive/handoffs/...``, or a not-yet-moved
    ``state/handoffs/...`` path); (2) a basename-only lookup against both
    trees (handles a bare basename value — the convention a handoff's own
    ``predecessor:`` field uses — and a ``state/handoffs/...``-shaped value
    whose target has since moved to archive under a different subpath).
    ``deliverable_id`` is a separate, ID-valued edge (see above) attempted
    only when ``predecessor_handoff`` doesn't resolve — it is NOT folded
    into this path-resolution algorithm, since an identifier cannot go
    through a path resolver.
    """
    live_index = _index_handoffs_by_basename(root / "state" / "handoffs", root)
    archive_index = _index_handoffs_by_basename(root / "archive" / "handoffs", root)
    handoff_dir = root / "state" / "handoffs"
    deliverable_index = _index_handoffs_by_field(root, "deliverable_id")
    # Join key canonicalized (C6b/AC11) -- a declared fork pair's raw ids
    # are re-keyed onto the same canonical winner so a plan carrying one
    # leg still joins a handoff declaring the other.
    from coordinator_core.ops.deliverable_equivalence import canonicalize, load_equivalence_map

    _renderers_equivalence_map = load_equivalence_map(root)
    _canonical_deliverable_index: dict[str, list[tuple[str, str]]] = {}
    for _raw_did, _entries in deliverable_index.items():
        _canonical_deliverable_index.setdefault(
            canonicalize(_raw_did, _renderers_equivalence_map), []
        ).extend(_entries)
    deliverable_index = _canonical_deliverable_index

    joined: list[dict] = []
    for record in plan_records:
        fm = record["frontmatter"]
        slug = _plan_slug(record["path"])
        status = str(fm.get("status")) if fm and fm.get("status") else "unknown"
        predecessor = fm.get("predecessor_handoff") if fm else None
        deliverable_id = _normalize_id_value(fm.get("deliverable_id")) if fm else None

        resolution_state = "gone"
        resolved: Optional[str] = None
        archived: Optional[str] = None
        resolution_method: Optional[str] = None
        ambiguous_deliverable_id_count = 0

        if predecessor:
            candidate = _normalize_path(str(predecessor))
            resolution_state, resolved, archived = _resolve_candidate_path(
                candidate,
                root,
                live_index,
                archive_index,
                handoff_dir=handoff_dir,
                git_history_cache=git_history_cache,
            )
            if resolution_state == "invalid-target":
                # (D3): plans rendering has no fourth state — collapses to
                # "gone". Explicit branch, not silent fallthrough, so a
                # reader doesn't mistake this for an oversight.
                resolution_state, resolved, archived = "gone", None, None
            elif resolution_state == "pruned":
                # (D5): plans rendering draws no distinction between
                # "permanently gone" and "pruned but git-recoverable" — a
                # plan with either kind of dangling predecessor gets no live
                # tracker row regardless, so recoverability isn't actionable
                # here. Explicit branch, not silent fallthrough — see this
                # function's docstring (D5) for the full rationale.
                resolution_state, resolved, archived = "gone", None, None
            if resolution_state in ("live", "archived"):
                resolution_method = "predecessor_handoff"

        if resolution_state not in ("live", "archived") and deliverable_id:
            match, ambiguous_count = _resolve_deliverable_match(
                deliverable_index.get(
                    canonicalize(deliverable_id, _renderers_equivalence_map), []
                )
            )
            if match:
                match_path, match_state = match
                resolution_state = match_state
                resolved = match_path if match_state == "live" else None
                archived = match_path if match_state == "archived" else None
                resolution_method = "deliverable_id"
            elif ambiguous_count:
                ambiguous_deliverable_id_count = ambiguous_count

        joined.append(
            {
                "path": record["path"],
                "slug": slug,
                "status": status,
                "declared_predecessor": str(predecessor) if predecessor else None,
                "declared_deliverable_id": deliverable_id,
                "ambiguous_deliverable_id_count": ambiguous_deliverable_id_count,
                "resolution_method": resolution_method,
                "resolution_state": resolution_state,
                "resolved_handoff_path": resolved,
                "archived_handoff_path": archived,
            }
        )
    return joined


#: Marker stamped on the generated ``docs/plans/INDEX.md`` — a reader (or a
#: future hand-edit guard) can tell at a glance this file is a render, not
#: authored prose. Provenance-free by itself (no source commit / generation
#: time baked in) — see ``_plans_index_marker`` for the full stamped line a
#: render actually emits, which appends both so a reader can tell staleness
#: from disagreement.
PLANS_INDEX_GENERATED_MARKER = (
    "<!-- generated by coordinator_core/ops/ceremony/renderers.py::render_plans_index_markdown "
    "— do not hand-edit -->"
)


def _plans_index_marker(source_commit: Optional[str], generated_at: Optional[str]) -> str:
    """Build the full generated-marker line, with provenance appended.

    ``source_commit``/``generated_at`` are threaded in as PARAMETERS, not
    resolved here via a wall-clock read or a ``git rev-parse`` shell-out —
    this renderer is tested for byte-stable output, and baking either
    directly into the render function would break that. A caller with
    nothing to supply (every existing caller, today) gets ``"unknown"`` for
    both rather than an omitted field, so the marker's shape never varies
    with whether provenance was supplied.
    """
    return (
        f"{PLANS_INDEX_GENERATED_MARKER} "
        f"(source: {source_commit or 'unknown'} | generated: {generated_at or 'unknown'})"
    )

#: Repo-root-relative directory the rendered ``INDEX.md`` itself lives in —
#: markdown links resolve relative to THIS directory, not repo root, so every
#: link target below must be relativized against it rather than emitted as
#: the raw repo-root-relative path ``_join_plans_to_handoffs`` produces.
PLANS_INDEX_DIR = "docs/plans"

#: Repo-root-relative path of the rendered index file itself — a named
#: module-level constant paired with ``PLANS_INDEX_DIR`` rather than an
#: inline literal at the call site, matching ``generate_exec_summary.py``'s
#: ``_EXEC_SUMMARY_OUT_PATH`` convention for the same purpose (Review:
#: coordinator-code-reviewer bd2f004c — the two call sites this diff
#: unified onto one shared ``_relative_link`` helper had inconsistent
#: out_path conventions).
PLANS_INDEX_OUT_PATH = f"{PLANS_INDEX_DIR}/INDEX.md"


def _index_relative_link(text: Optional[str], repo_relative_target: Optional[str]) -> str:
    """Build a markdown link whose target resolves correctly from
    ``PLANS_INDEX_DIR`` — the directory the rendered index file itself lives
    in — rather than from repo root. Routes through the shared
    ``coordinator_core.ops._relative_link`` helper (not hardcoded ``../../``
    concatenation) so this stays correct if the index's own location ever
    moves, and stays the same generating rule ``generate_exec_summary.py``
    uses for its own MANAGED-section links."""
    if not repo_relative_target:
        return _md_link(text, repo_relative_target)
    rel = relative_markdown_target(repo_relative_target, PLANS_INDEX_OUT_PATH)
    return _md_link(text, rel)


def _unlinked_reason(j: dict) -> str:
    """Build the ``## Unlinked`` "Why unlinked" cell, specific to what was
    actually tried for this plan rather than one fixed string — a plan can
    fail to resolve via either ``predecessor_handoff`` or ``deliverable_id``
    (see ``_join_plans_to_handoffs``), and the reason should say which of
    those were declared-but-unresolved, not just "no predecessor_handoff:
    declared" regardless of what else was tried.

    A plan with nothing declared at all keeps the original, unchanged
    message (nothing WAS tried, so there is nothing more specific to say).

    Review: staff-eng F4 — the former ``plan_id``/``origin_plan_id`` clause
    is dropped along with that edge (F2); it accused every plan carrying a
    ``plan_id:`` of a defect that was really an unpopulated handoff-side
    field. Review: staff-eng F7 — bracket access throughout (every key here
    is always present on a ``_join_plans_to_handoffs`` result dict, so
    ``.get`` was inconsistent, not defensive).
    """
    parts: list[str] = []
    if j["declared_predecessor"]:
        parts.append(
            f"dangling — `{j['declared_predecessor']}` no longer exists on disk "
            f"under state/handoffs/ or archive/handoffs/ (git history is the "
            f"only remaining trace)"
        )
    if j["ambiguous_deliverable_id_count"]:
        parts.append(
            f"deliverable_id `{j['declared_deliverable_id']}` is ambiguous across "
            f"{j['ambiguous_deliverable_id_count']} handoffs"
        )
    elif j["declared_deliverable_id"]:
        parts.append(f"deliverable_id `{j['declared_deliverable_id']}` matches no handoff")
    if not parts:
        return "no `predecessor_handoff:` declared"
    return "; ".join(parts)


def render_plans_index_markdown(
    root: Path,
    *,
    git_history_cache: Optional[_GitHistoryCacheProvider] = None,
    source_commit: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render the full ``docs/plans/`` index — every base plan, in exactly one
    of three sections: ``## Linked`` (resolves to a LIVE handoff),
    ``## Archived`` (resolves to a handoff under ``archive/handoffs/**`` —
    a normal "consumed then archived" outcome, not dangling), or
    ``## Unlinked`` (resolves in neither tree — ``"gone"``: either a
    dangling pointer to a deleted target, or no ``predecessor_handoff:``
    declared at all).

    Negative-spec: this renders ``docs/plans/INDEX.md``, never
    ``docs/plans/README.md``. The README is a hand-authored narrative index
    scoped to one plan batch; the two artifacts differ in kind and a render
    into the README would destroy curated prose.

    Reuses the same ``_collect_plans_with_parse_errors`` /
    ``_join_plans_to_handoffs`` pair the now-retired ``render_repo_section``
    used for its compact remainder pointer (see docs/plans/2026-08-14-retire-
    the-handoff-tracker-and-project-tracker-renders.md § C2) — one
    plan-collection helper, kept for this surviving render target (Item B
    design call). Returns a markdown string (no trailing newline); does not
    write to disk.

    ``git_history_cache`` (optional): forwarded to ``_join_plans_to_handoffs``
    — see ``_join_handoff_lineage``'s matching parameter docstring. This
    render never surfaces a "pruned" state of its own (D5 collapses it to
    ``"gone"``), so the cache here is a perf thread only, not a correctness
    requirement — but threading it avoids a per-unresolved-plan subprocess
    spawn regardless.

    ``source_commit``/``generated_at`` (optional): provenance stamped into
    the emitted marker line (see ``_plans_index_marker``) — a caller that
    knows the commit this render ran at / the wall-clock time threads them
    in; a caller that doesn't (every current caller) leaves both ``None``
    and the marker reads ``"unknown"`` for each. Kept out of this function's
    own resolution logic (no wall-clock read, no ``git`` shell-out here) so
    this renderer's byte-stable-output contract holds regardless of caller.
    """
    plan_joins = _join_plans_to_handoffs(
        _collect_plans_with_parse_errors(root), root, git_history_cache=git_history_cache
    )
    linked = [j for j in plan_joins if j["resolution_state"] == "live"]
    archived = [j for j in plan_joins if j["resolution_state"] == "archived"]
    unlinked = [j for j in plan_joins if j["resolution_state"] == "gone"]

    # Review: staff-eng F1 — resolution_method was computed by the join and
    # consumed by nothing; surfaced here as its own column so a reader can
    # tell a declared edge (predecessor_handoff) from an inferred one
    # (deliverable_id) rather than the two looking identical in the table.
    linked_headers = ["Plan", "Status", "Predecessor handoff", "Resolved via"]
    linked_rows = [
        [
            _index_relative_link(os.path.basename(j["path"]), j["path"]),
            j["status"],
            _index_relative_link(
                os.path.basename(j["resolved_handoff_path"]), j["resolved_handoff_path"]
            ),
            j["resolution_method"] or "",
        ]
        for j in linked
    ]

    archived_headers = ["Plan", "Status", "Archived handoff"]
    archived_rows = [
        [
            _index_relative_link(os.path.basename(j["path"]), j["path"]),
            j["status"],
            _index_relative_link(
                os.path.basename(j["archived_handoff_path"]), j["archived_handoff_path"]
            ),
        ]
        for j in archived
    ]

    unlinked_headers = ["Plan", "Status", "Why unlinked"]
    unlinked_rows = [
        [
            _index_relative_link(os.path.basename(j["path"]), j["path"]),
            j["status"],
            _unlinked_reason(j),
        ]
        for j in unlinked
    ]

    lines = [
        "# Plans Index",
        "",
        _plans_index_marker(source_commit, generated_at),
        "",
        "## Linked",
        "",
        _render_table(linked_headers, linked_rows),
        "",
        "## Archived",
        "",
        _render_table(archived_headers, archived_rows),
        "",
        "## Unlinked",
        "",
        _render_table(unlinked_headers, unlinked_rows),
    ]
    return "\n".join(lines)
