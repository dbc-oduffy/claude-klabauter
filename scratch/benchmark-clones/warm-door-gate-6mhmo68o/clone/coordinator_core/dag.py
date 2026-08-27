"""
dag.py — Python port of bin/lib/walk-handoff-dag.js.

Port source: plugins/coordinator-claude/coordinator/bin/lib/walk-handoff-dag.js
Spec backlink: docs/plans/2026-06-29-handoff-lineage-dag-fan-in-fan-out.md § Primitive interface

Purpose: edge-kind-aware handoff DAG traversal primitive. Shared kernel for forward
accumulation (LoE aggregation) and reverse-membership testing (archival has-live-children
guard). Centralises the edge-kind SSOT so no consumer re-derives which frontmatter fields
are edges.

Dual-homed EDGE_KIND_META SSOT:
  After the pcore-03 beachhead, EDGE_KIND_META (predecessor / additional_predecessors /
  forked_from / origin_handoff) lives in BOTH walk-handoff-dag.js (JS, for
  aggregate-chain-loe.sh) AND this module (Python, for the two flipped veneers). A future
  edge-kind addition MUST be applied in both places or the consumers silently diverge. The
  designated convergence point is the pcore-XX stub that flips aggregate-chain-loe.sh; until
  then treat both sites as authoritative.
  See: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § D4.

  origin_handoff provenance edge (ratified spinoff-provenance-ancestry contract):
    ``origin_handoff`` is a PROVENANCE edge — namespace-disjoint from lineage edges
    (predecessor / additional_predecessors / forked_from) and kept OUT of the default
    walk_forward edge set (``{'predecessor'}``) and the default referenced_by set
    (``{'predecessor', 'additional_predecessors', 'forked_from'}``). Walkable ONLY via
    explicit ``edge_kinds={'origin_handoff'}`` — callers must opt in deliberately.
    Ratification memo: cross-repo/inbox/2026-07-07-spinoff-provenance-claude-klabauter-ratified.md

Cycle-vs-convergence semantics (verbatim from JS, walk-handoff-dag.js:9-31):
  - gray-set re-encounter → genuine back-edge (authoring error) → terminatedEarly='lineage-cycle'
  - black-set re-encounter → benign diamond convergence → skip (continue), NOT abort.
  This preserves diamond summation (the Director of Engineering F1) AND surfaces true authoring cycles (the Director of Engineering F6).

Exports:
  EDGE_KIND_META        — dict mapping edge-kind name → {field, multi}; the SSOT constant.
  handoff_edges(node_meta, edge_kinds) → list[str]
  walk_forward(start_path, edge_kinds, node_gate, handoff_dir, repo_root) → dict
  referenced_by(target, live_set, edge_kinds, handoff_dir, exclude) → dict
  check_lineage_reachability(frontmatter, repo_root, handoff_dir, record_repo_rel_path,
      git_history_cache) → list[dict]
    Shared reachability RULE kernel (C6 GAP1 backfill) — checks predecessor / forked_from /
    additional_predecessors[] / origin_handoff via resolve_target's 3-tier resolution
    (live ∪ archive-on-disk ∪ git-history). kind:recovery predecessor (a SHA, not a path) is
    skipped — same-repo-only foreign-baton carve-out.
  build_git_history_cache(repo_root, timeout_s) → GitHistoryCache | None
    Batch-sweep perf primitive — primes a Set of every repo-relative path that ever appeared
    in a diff anywhere in history (added, modified, deleted, or either side of a rename) via
    one subprocess pass, for callers doing many check_lineage_reachability calls in one
    sweep. Optional; absent → per-call resolution. The returned GitHistoryCache is a plain
    set subclass (every existing Set[str]-shaped use keeps working) plus a `.complete` flag —
    True only when the priming pass is confirmed to have covered the repo's FULL history
    (not shallow, not a partial/filtered clone) — that licenses treating a cache MISS as
    authoritative ("never tracked") instead of merely "unknown, fall through per-path".
  resolve_target(ref, handoff_dir, repo_root, git_history_cache) → str | None
    Promoted (C2, F1 — mirrors JS's exported `_resolveTarget`) 3-tier resolver: live ∪
    archive-on-disk ∪ git-history. Returns an absolute disk path (tier 1/2), the sentinel
    string 'git-history' (tier 3 — disk-absent, git-known), or None (unresolvable in all
    three tiers).
  invalidate_git_history_cache() → None
    Bumps the _git_path_ever_tracked process-lifetime cache generation, discarding
    stale entries. MUST be called after any successful commit made mid-process (e.g.
    boot_sweep's archive_and_commit / rm_and_commit) — see the comment block above
    _EVER_TRACKED_CACHE for the correctness rationale.

Negative-spec (mirrors JS):
  - Does NOT implement topological sort, shortest-path, or betweenness.
  - Does NOT throw on cycle — uses terminatedEarly='lineage-cycle' instead.
  - Does NOT abort on missing-link — skips unresolvable edges, continues, sets terminatedEarly.
  - Does NOT auto-infer adjacency as ancestry — only explicit frontmatter edge fields followed.
  - No external dependencies — stdlib-only.
"""

import hashlib
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple, Union

# DR-054 console-flash guard: suppress the transient console window subprocess
# spawns on Windows. 0 (no-op) on POSIX where CREATE_NO_WINDOW doesn't exist.
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Edge-kind field map — the SSOT for which frontmatter keys are DAG edges.
# Consumers supply a set of edge-kind names to select which subset to follow.
# ---------------------------------------------------------------------------

#: Map from edge-kind name → the frontmatter field that carries it.
#: 'additional_predecessors' is an array field; all others are scalar.
#: 'origin_handoff' is a provenance edge kept out of the default walk/referenced_by sets —
#: walkable only via explicit edge_kinds={'origin_handoff'}; see module docstring.
#: Dual-homed with walk-handoff-dag.js — see module docstring for sync obligation.
EDGE_KIND_META: Dict[str, Dict[str, Any]] = {
    'predecessor':             {'field': 'predecessor',             'multi': False},
    'additional_predecessors': {'field': 'additional_predecessors', 'multi': True},
    'forked_from':             {'field': 'forked_from',             'multi': False},
    'origin_handoff':          {'field': 'origin_handoff',          'multi': False},
}

# ---------------------------------------------------------------------------
# Archival vs. continuation edge-kind SSOT (723aadac4b1d follow-up).
#
# Two default edge sets answer two DIFFERENT questions, and conflating them is
# the defect 723aadac4b1d fixed at five call sites:
#
#   ARCHIVAL_EDGE_KINDS asks "is it SAFE TO MOVE this node?" — all three
#   lineage kinds legitimately block, INCLUDING `forked_from`: archiving a
#   node a live spinoff `forked_from` would strand that spinoff's own origin
#   pointer. This is `referenced_by`'s own default (below) and the set
#   `archival.reverse_membership`'s callers depend on.
#
#   CONTINUATION_EDGE_KINDS asks "MAY THIS WORKSTREAM CONCLUDE?" / "does a
#   review obligation propagate to this node?" — `forked_from` is deliberately
#   ABSENT. A spinoff is a niece, not a descendant: it was forked OUT of its
#   parent precisely so the parent could finish without waiting on it, and
#   schema rule A3a-3 (`frontmatter/schema_validate.py::_cf_spinoff_
#   predecessor_none`) forces every spinoff kind's `predecessor` to `none`,
#   so a spinoff can never walk back to what it forked from — the edge is
#   structurally one-way. Blocking a conclusion question on a live spinoff
#   re-couples the two at the exact moment the deliberate decoupling is
#   supposed to pay off.
#
# Every representation of these two sets elsewhere in the tree (CSV strings
# for wire params, other frozensets) MUST derive from these two constants,
# not restate the literal — see
# coordinator_core/tests/test_dag_edge_kind_ssot.py, the single test that
# pins every representation to these two constants so a future drift is a
# failing test, not a comment nobody reads.
#
# Origin: commit 723aadac4b1d "conclusion gates: a spinoff is a niece, not a
# live child (five call sites)"; example-cockpit-repo-em, 2026-08-05,
# cross-repo/inbox/2026-08-05-example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-
# as-live-children.md.
# ---------------------------------------------------------------------------

#: The ARCHIVAL default — "is it safe to move this node?" All three lineage
#: edge kinds; `forked_from` legitimately blocks here. See module comment
#: block above.
ARCHIVAL_EDGE_KINDS: FrozenSet[str] = frozenset(
    {'predecessor', 'additional_predecessors', 'forked_from'}
)

#: The CONTINUATION default — "may this workstream conclude?" / "does a
#: review obligation propagate here?" `forked_from` is deliberately absent —
#: see module comment block above.
CONTINUATION_EDGE_KINDS: FrozenSet[str] = frozenset(
    {'predecessor', 'additional_predecessors'}
)

# ---------------------------------------------------------------------------
# Id-suffixed pointer-field aliases (C6 pointer-normalization seam, 2026-07-26).
#
# DoE's on-disk baton corpus also carries `predecessor_id` (73 occurrences) and
# `origin_handoff_id` (25) — frontmatter fields that name the SAME edge kind as
# `predecessor` / `origin_handoff` but by handoff_id rather than by path/filename.
# Deliberately NOT folded into EDGE_KIND_META itself. NOTE: test_dag_edge_kinds.py
# only asserts four per-key equalities (predecessor / additional_predecessors /
# forked_from / origin_handoff) — there is no length or set assertion, so it would
# NOT catch a new key being added here. The real reasons a stub-id-valued edge kind
# such as `blocked_by` must not be added to EDGE_KIND_META:
#   1. It is stub-id-valued, where every existing entry in this constant is
#      path-valued (the frontmatter field names a file, not a stub id).
#   2. The JS twin walk-handoff-dag.js (see module docstring) would drift out of
#      sync with this dual-homed SSOT.
# This sits *beside* EDGE_KIND_META as a pure addition, so neither the default
# sets in handoff_children.py / archival.py nor EDGE_KIND_META's own shape change.
#
# handoff_edges() reads both the primary field and its alias(es) for a kind;
# resolve_target() resolves an id-shaped ref (no '.md' suffix) via an
# `id_index` (handoff_id -> absolute path) built by build_handoff_id_index().
# No other edge kind has an id-suffixed alias today — additional_predecessors
# and forked_from are absent from this map on purpose (grep confirms zero
# additional_predecessors_id / forked_from_id occurrences in either corpus).
# ---------------------------------------------------------------------------
EDGE_KIND_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    'predecessor':    ('predecessor_id',),
    'origin_handoff': ('origin_handoff_id',),
}

__all__ = [
    "EDGE_KIND_META",
    "EDGE_KIND_FIELD_ALIASES",
    "ARCHIVAL_EDGE_KINDS",
    "CONTINUATION_EDGE_KINDS",
    "handoff_edges",
    "walk_forward",
    "referenced_by",
    "resolve_target",
    "check_lineage_reachability",
    "build_git_history_cache",
    "GitHistoryCache",
    "invalidate_git_history_cache",
    "build_handoff_id_index",
    "read_handoff_meta",
    "scan_repo_handoff_corpus",
]

# ---------------------------------------------------------------------------
# Frontmatter parse cache: (abs_path, content_hash) → dict
# Re-keyed from mtime_float → sha256 content-hash (C3, R5 same-second mtime fix).
# Bounded at _MAX_FRONTMATTER_CACHE entries; oldest half evicted on overflow.
# Rationale: docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md
# (successor to docs/decisions/2026-07-03-tri-plane-ownership-boundary.md § DD#1)
# Review: code-reviewer — F7: separation from cache._REVALIDATED_CACHE is intentional.
# _FRONTMATTER_CACHE is dag-local for independent clearability in tests and dedicated
# eviction footprint. pcore-06/10/11 consumers use cache._REVALIDATED_CACHE; the two
# caches coexist — the same file can be cached in both if both paths are exercised.
# ---------------------------------------------------------------------------

_FRONTMATTER_CACHE: Dict[Tuple[str, str], dict] = {}

# NEGATIVE SPEC: this cap must stay comfortably ABOVE the largest handoff corpus
# the engine scans, or corpus-wide consumers fall off a capacity cliff.
# `referenced_by` rescans the caller's whole path list on every call, so a loop
# over N live batons touches N x M paths in sequential order. Once M exceeds the
# cap, oldest-half eviction drops each entry before the next pass revisits it —
# a ~100% miss rate that silently turns O(N + M) parses into O(N x M).
# Measured on the 2026-08-18 corpora (DoE-claude M=676/N=231, claude-klabauter M=567/N=147):
# at cap 512 the differential-oracle sweeps cost ~110s each and stalled the fast
# tier at 98-99%; sizing the cap above M cut ~64% of that. Raise this, never
# lower it, when a corpus grows — and do not tune it down for memory without
# re-measuring those sweeps.
_MAX_FRONTMATTER_CACHE: int = 4096

# ---------------------------------------------------------------------------
# Minimal YAML frontmatter parser (stdlib-only, handles handoff file subset)
# ---------------------------------------------------------------------------

def _strip_inline_comment(text: str) -> str:
    """Strip a trailing YAML inline comment from a scalar string.

    A '#' is a comment opener only when NOT inside a quoted span AND preceded by
    whitespace AND followed by whitespace or end-of-string (mirrors JS stripInlineComment).
    """
    in_single = False
    in_double = False
    for i, c in enumerate(text):
        if c == '"' and not in_single:
            in_double = not in_double
        elif c == "'" and not in_double:
            # YAML single-quoted escape: '' → literal '. Skip both chars.
            if in_single and i + 1 < len(text) and text[i + 1] == "'":
                continue
            in_single = not in_single
        elif c == '#' and not in_single and not in_double:
            # Comment opener only when preceded by whitespace and followed by
            # whitespace or end-of-string.
            if i > 0 and text[i - 1] in (' ', '\t'):
                if i + 1 >= len(text) or text[i + 1] in (' ', '\t'):
                    return text[:i].rstrip()
    return text


def _parse_scalar(text: str) -> Any:
    """Parse a YAML scalar value (after inline-comment stripping).

    Returns None for null/~/none; bool for true/false; int or float for
    numeric strings; strips surrounding single/double quotes; returns str
    otherwise. Mirrors JS parseScalar in schema.js.
    """
    text = _strip_inline_comment(text).strip()
    if not text or text in ('null', '~'):
        return None
    if text == 'true':
        return True
    if text == 'false':
        return False
    # Numeric
    try:
        as_int = int(text)
        # Ensure we don't accept floats that parse as int (e.g. "1.5")
        if str(as_int) == text:
            return as_int
    except ValueError:
        # Deliberate type-coercion cascade, not an error path — most scalars
        # are non-numeric strings, so this fires on nearly every normal
        # parse; fall through to the float attempt below.
        pass
    try:
        as_float = float(text)
        # Reject non-finite results (inf/-inf/nan): a short SHA like "229e792"
        # is also valid scientific notation and float() overflows it to inf
        # WITHOUT raising. YAML 1.1 spells infinity as .inf, never as a bare
        # decimal literal, so an overflowing literal is never an intended
        # float — fall through to string handling instead.
        if math.isfinite(as_float):
            return as_float
    except ValueError:
        # Same cascade — not numeric either; falls through to string handling.
        pass
    # Quoted string — strip quotes and handle single-quoted '' escape
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _parse_inline_list(text: str) -> List[Any]:
    """Parse a YAML inline list: '[a, b, c]' → ['a', 'b', 'c'].

    Uses quote-aware comma splitting so items whose value contains a comma
    inside a quoted span (e.g. '"path,with,comma.md"') are not split mid-item.
    Items are individually parsed through _parse_scalar.
    """
    text = text.strip()
    if not (text.startswith('[') and text.endswith(']')):
        return []
    inner = text[1:-1].strip()
    if not inner:
        return []

    items = []
    current: List[str] = []
    in_single = False
    in_double = False

    for ch in inner:
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == ',' and not in_single and not in_double:
            item = ''.join(current).strip()
            if item:
                items.append(_parse_scalar(item))
            current = []
        else:
            current.append(ch)

    # Flush the final item
    item = ''.join(current).strip()
    if item:
        items.append(_parse_scalar(item))

    return items


#: Matches a YAML block-scalar indicator (`|` literal or `>` folded), optionally
#: followed by a chomping/indentation modifier and a trailing comment.
#: Mirrors schema.js BLOCK_SCALAR_RE (schema.js:42).
_BLOCK_SCALAR_RE = re.compile(r'^([|>])([+-]?[0-9]?|[0-9]?[+-]?)\s*(#.*)?$')


def _consume_block_scalar(lines: List[str], start: int, key_indent: int) -> tuple:
    """Consume a YAML block-scalar body (`|`/`>`) starting at ``start``.

    Review: code-reviewer P1 — dag.py's mapping parser previously had no
    block-scalar branch at all, so a `field: |` block whose body contained
    markdown bullets or colon-bearing lines was misparsed: the key was set to
    the literal string "|", colon-less body lines were silently dropped, and
    colon-bearing body lines were silently absorbed as spurious top-level
    keys — corrupting any predecessor/forked_from/origin_handoff/
    additional_predecessors field that happened to follow the block scalar in
    the same record. Ports schema.js consumeBlockScalar (schema.js:59) /
    schema_validate.py _consume_block_scalar verbatim: the block continues
    through blank lines and ends only at a non-blank line indented at or
    below key_indent; body lines are joined with '\\n' verbatim (never
    re-parsed as YAML/list) and the block's own indentation is stripped.
    Returns (value, next_line).
    """
    body_lines: List[str] = []
    i = start
    last_content_line = -1
    while i < len(lines):
        raw = lines[i]
        if raw.strip() == '':
            body_lines.append('')
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= key_indent:
            break
        body_lines.append(raw)
        last_content_line = len(body_lines) - 1
        i += 1

    trimmed_body = [] if last_content_line == -1 else body_lines[: last_content_line + 1]
    strip_indent = 0
    for l in trimmed_body:
        if l.strip() != '':
            strip_indent = len(l) - len(l.lstrip())
            break
    text = '\n'.join('' if l == '' else l[strip_indent:] for l in trimmed_body)
    return text, i


def _parse_yaml_block(lines: List[str], base_indent: int) -> Any:
    """Parse a YAML mapping or list block starting at base_indent.

    Handles the subset used in handoff frontmatter:
    - flat key: value mappings
    - block lists (  - item)
    - inline lists (key: [a, b])
    - nested mappings (for completeness, though handoff files rarely use them)
    Returns a dict or list depending on the first non-blank line's structure.
    """
    # Peek at first non-blank line to decide: mapping or list?
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent < base_indent:
            return {}
        if stripped.startswith('- ') or stripped == '-':
            return _parse_yaml_list_block(lines, base_indent)
        else:
            return _parse_yaml_mapping_block(lines, base_indent)
    return {}


#: Matches a bare, unquoted YAML mapping key at the start of a list-entry's
#: dash line (e.g. "carry_id: cf-alpha-123" -> key "carry_id"). Requires the
#: colon be followed by whitespace or end-of-line so that scalar strings
#: which merely contain a colon (URLs, SHAs-with-colon-adjacent-punctuation,
#: quoted strings) are never misidentified as a mapping start.
_LIST_ENTRY_KEY_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_-]*)\s*:(\s|$)')


def _parse_yaml_list_block(lines: List[str], base_indent: int) -> List[Any]:
    """Parse a YAML block list at base_indent.

    Each ``- `` entry is either a plain scalar (``- foo``, returned via
    ``_parse_scalar``, unchanged behavior — every existing caller expects
    flat string lists to keep parsing as strings) or a sequence-of-mappings
    entry (``- key: value`` optionally followed by more-indented ``key:
    value`` continuation lines belonging to the SAME entry). The two are
    told apart by ``_LIST_ENTRY_KEY_RE`` matching the dash line's content —
    real YAML makes this same distinction irrespective of whether
    continuation lines follow, so a lone ``- key: value`` with no
    continuation is still a one-key mapping, not a string.

    Review: code-reviewer / DAG-401 — this function previously appended
    every ``- `` line as an opaque single-line scalar via ``_parse_scalar``,
    silently dropping every more-indented continuation line of a
    sequence-of-mappings entry (e.g. a second/third key on a
    ``carried_items:``-shaped block). This is the READ-side twin of the
    write-side gap C0 of docs/plans/2026-07-26-structured-sibling-evidence-gates.md
    fixed for the nested-block WRITE primitive; C0 left this READ path
    unfixed (see coordinator_core/ops/handoff_gate_aging.py's C6 scope-
    boundary note, written against the bug this function now closes).
    Silent truncation is the dangerous failure direction here: a gate
    evaluator reading a truncated `gate_evidence` leg can read an
    unsatisfied gate as satisfied.
    """
    items: List[Any] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent < base_indent:
            break
        if stripped == '-':
            items.append(None)
            i += 1
            continue
        if stripped.startswith('- '):
            item_text = stripped[2:].strip()
            if _LIST_ENTRY_KEY_RE.match(item_text):
                # Sequence-of-mappings entry: the dash line supplies the
                # first key:value pair; gather every more-indented line
                # that follows (up to the next entry/dedent) as
                # continuation, then hand the whole entry to the existing
                # mapping-block parser rather than writing a second one.
                entry_indent = indent + 1
                entry_lines = [(' ' * entry_indent) + item_text]
                j = i + 1
                while j < n:
                    raw2 = lines[j]
                    stripped2 = raw2.strip()
                    if stripped2 == '' or stripped2.startswith('#'):
                        entry_lines.append(raw2)
                        j += 1
                        continue
                    indent2 = len(raw2) - len(raw2.lstrip())
                    if indent2 <= indent:
                        break
                    entry_lines.append(raw2)
                    j += 1
                items.append(_parse_yaml_mapping_block(entry_lines, entry_indent))
                i = j
                continue
            items.append(_parse_scalar(item_text))
            i += 1
            continue
        i += 1
    return items


def _parse_yaml_mapping_block(lines: List[str], base_indent: int) -> Dict[str, Any]:
    """Parse a YAML mapping block at base_indent.

    Handles scalar values, block lists, inline lists, and nested mappings.
    """
    result: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip()
        stripped_ws = stripped.strip()

        # Skip blank lines and comments
        if not stripped_ws or stripped_ws.startswith('#'):
            i += 1
            continue

        indent = len(raw) - len(raw.lstrip())
        if indent < base_indent:
            break

        # Must be a key: value line at this indent level
        colon_idx = stripped_ws.find(':')
        if colon_idx == -1:
            i += 1
            continue

        key = stripped_ws[:colon_idx].strip()
        rest = stripped_ws[colon_idx + 1:]
        rest_stripped = rest.strip()

        if not rest_stripped or rest_stripped.startswith('#'):
            # Value is null OR nested block on following lines
            # Look ahead past blank/comment lines to find first real line
            next_i = i + 1
            while next_i < len(lines):
                peek = lines[next_i].rstrip()
                peek_s = peek.strip()
                if peek_s and not peek_s.startswith('#'):
                    break
                next_i += 1

            if next_i < len(lines):
                next_raw = lines[next_i]
                next_indent = len(next_raw) - len(next_raw.lstrip())
                if next_indent > indent:
                    # Nested block — parse it
                    nested_lines = lines[next_i:]
                    nested_value = _parse_yaml_block(nested_lines, next_indent)
                    result[key] = nested_value
                    # Advance i past all lines consumed by the nested block
                    j = next_i
                    while j < len(lines):
                        r = lines[j]
                        r_s = r.strip()
                        if r_s and not r_s.startswith('#'):
                            r_indent = len(r) - len(r.lstrip())
                            if r_indent < next_indent:
                                break
                        j += 1
                    i = j
                    continue

            result[key] = None
        elif _BLOCK_SCALAR_RE.match(rest_stripped):
            # Block scalar (`|` literal or `>` folded). The value is the opaque
            # text of the following more-indented lines — never re-parsed as
            # YAML, never treated as a list even when a line begins with '- '.
            block_value, next_i = _consume_block_scalar(lines, i + 1, indent)
            result[key] = block_value
            i = next_i
            continue
        else:
            # Inline value — check for inline list or scalar
            comment_stripped = _strip_inline_comment(rest_stripped)
            if comment_stripped.startswith('[') and comment_stripped.endswith(']'):
                result[key] = _parse_inline_list(comment_stripped)
            else:
                result[key] = _parse_scalar(rest_stripped)

        i += 1

    return result


def _parse_frontmatter(content: str) -> dict:
    """Extract and parse YAML frontmatter from markdown content.

    Handles optional leading HTML comment blocks (to match JS parseFrontmatter
    behavior). Returns parsed frontmatter dict, or {} on any parse error.
    Mirrors the JS parseFrontmatter in schema.js.
    """
    cursor = 0
    # Skip optional leading HTML comment blocks
    while True:
        ws_match = re.match(r'^\s*', content[cursor:])
        ws_len = len(ws_match.group(0)) if ws_match else 0
        after_ws = cursor + ws_len
        if content[after_ws:after_ws + 4] == '<!--':
            close_idx = content.find('-->', after_ws + 4)
            if close_idx == -1:
                return {}  # Unclosed comment
            cursor = close_idx + 3
        else:
            cursor = after_ws
            break

    remaining = content[cursor:]
    if not remaining.startswith('---'):
        return {}

    after_first = remaining[3:]
    first_newline = after_first.find('\n')
    if first_newline == -1:
        return {}

    # Guard: nothing but whitespace between --- and newline
    if after_first[:first_newline].strip():
        return {}

    rest = after_first[first_newline + 1:]
    # Find closing ---
    close_match = re.search(r'^---\s*$', rest, re.MULTILINE)
    if close_match is None:
        return {}

    yaml_block = rest[:close_match.start()]
    yaml_lines = yaml_block.split('\n')

    try:
        result = _parse_yaml_mapping_block(yaml_lines, 0)
        if not result:
            return {}
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Utility: parse frontmatter from a file path, cached by (path, content_hash).
# Returns {} on any I/O or parse error.
# ---------------------------------------------------------------------------

def _read_meta(file_path: str) -> dict:
    """Read and parse YAML frontmatter from file_path.

    Cached by (abs_path, sha256-content-hash) — re-keyed from mtime to sha256 body
    hash (C3, R5 fix). A same-second body write produces a different hash and
    is correctly treated as a cache miss.

    I/O cost: one file read per lookup. Raw bytes are read once; the sha256 stamp
    is computed in-memory; content is decoded from the same buffer. This eliminates
    the TOCTOU window that existed when stamp (READ 1) and content (READ 2) were
    separate syscalls, and halves per-miss I/O.

    Cache key is os.path.abspath(file_path) to prevent duplicate cache entries for
    equivalent path strings (./foo vs foo vs /abs/foo).

    Backlink: docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md
    (successor to docs/decisions/2026-07-03-tri-plane-ownership-boundary.md § DD#1)
    Returns {} on any error.
    """
    try:
        # Review: code-reviewer — F5: normalize path to prevent spurious cache misses on equivalent paths
        file_path = os.path.abspath(file_path)
        # Review: code-reviewer — F2: read bytes once; compute stamp in-memory; decode from same buffer
        # (eliminates TOCTOU window between stamp read and content read, halves per-miss I/O)
        raw = Path(file_path).read_bytes()
        stamp = hashlib.sha256(raw).hexdigest()
        cache_key = (file_path, stamp)
        if cache_key in _FRONTMATTER_CACHE:
            return _FRONTMATTER_CACHE[cache_key]
        content = raw.decode('utf-8', errors='replace')
        parsed = _parse_frontmatter(content)
        if len(_FRONTMATTER_CACHE) >= _MAX_FRONTMATTER_CACHE:
            # Evict the oldest half when full to keep memory bounded.
            evict_keys = list(_FRONTMATTER_CACHE.keys())[: _MAX_FRONTMATTER_CACHE // 2]
            for k in evict_keys:
                del _FRONTMATTER_CACHE[k]
        _FRONTMATTER_CACHE[cache_key] = parsed
        return parsed
    except Exception:
        return {}


def read_handoff_meta(file_path: str) -> dict:
    """Public alias for ``_read_meta`` — a caller outside this module (e.g. a
    per-emit-run cache building a corpus-wide frontmatter map once, instead of
    relying on ``walk_forward``'s own per-call DFS to populate it) reads
    through the SAME process-lifetime, content-hash-keyed cache this module's
    internals use, rather than re-deriving frontmatter parsing.
    """
    return _read_meta(file_path)


# ---------------------------------------------------------------------------
# Utility: git-history-aware existence check (tier 3 of resolve_target).
#
# Spec backlink: DoE-claude:pln-handoff-spinoff-machinery-robu-0d0f15 § C2 (F1)
#
# A target that is disk-absent (relocated, e.g. flat archive → month-foldered
# archive) but git-reachable is NOT "never existed". Best-effort: any git failure
# (not a repo, git missing, timeout) resolves to "not found" rather than raising —
# a resolver used inside a write-time hard-reject must never itself crash the caller.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _git_path_ever_tracked memoization (2026-07-23 boot_sweep 10s-timeout perf fix).
#
# Measured against a 72-handoff/497-plan/37-memo corpus: a boot_sweep run spawned
# 1053 `git log --all -- <path>` subprocesses (~13.9ms each, ~14.6s total —
# effectively the entire op's wall-clock) to resolve only 20 UNIQUE
# (repo_root, repo_rel_path) values — the same ~20 questions asked ~50x each.
# 14 of those 20 unique paths are never-tracked (return False); negative
# results are the expensive majority, so caching MUST cover both outcomes.
#
# Process-lifetime cache, same bounded-eviction idiom as _FRONTMATTER_CACHE
# above (evict oldest half on overflow) — deliberately duplicated rather than
# shared, for the same independent-clearability rationale as that cache.
#
# CORRECTNESS HAZARD — git history is not immutable mid-sweep: boot_sweep's
# archival helpers (archive_and_commit / rm_and_commit in ops/fleet/_common.py)
# create commits PARTWAY THROUGH a sweep. A path cached as False before such a
# commit could become git-tracked after it — a naive whole-process cache would
# then serve a stale negative for the remainder of the run. Fixed via a
# module-level generation counter: every cache entry is keyed on the
# generation it was computed under, and invalidate_git_history_cache() (called
# by archive_and_commit / rm_and_commit after every successful commit) bumps
# the generation AND drops the now-unreachable prior-generation entries, so a
# post-commit lookup is guaranteed to re-query git rather than read a stale
# cached False. Invalidation contract: any code path that mutates this repo's
# git history (creates/amends a commit) between _git_path_ever_tracked calls
# in the same process MUST call invalidate_git_history_cache() immediately
# after the commit succeeds, or subsequent lookups may read stale results.
# ---------------------------------------------------------------------------

_EVER_TRACKED_CACHE: Dict[Tuple[int, str, str], bool] = {}
_MAX_EVER_TRACKED_CACHE: int = 2048
_EVER_TRACKED_GENERATION: int = 0


def invalidate_git_history_cache() -> None:
    """Bump the git-history generation counter, discarding every
    ``_git_path_ever_tracked`` cache entry computed under a prior generation.

    Call this immediately after any successful git commit that mutates the
    repo's history within the same process — currently ``archive_and_commit``
    and ``rm_and_commit`` in ``coordinator_core/ops/fleet/_common.py``. See the
    module-level comment block above ``_EVER_TRACKED_CACHE`` for the full
    correctness rationale (a path cached False pre-commit can become tracked
    post-commit; without invalidation a boot_sweep run would serve that stale
    negative for its remainder).
    """
    global _EVER_TRACKED_GENERATION
    _EVER_TRACKED_GENERATION += 1
    _EVER_TRACKED_CACHE.clear()


def _git_path_ever_tracked(repo_rel_path: str, repo_root: str) -> bool:
    """True if repo_rel_path was ever a git-tracked path at any point in this
    repo's full history (``git log --all`` has at least one entry for it) —
    covers a path relocated or deleted between commits, disk-absent now but
    git-known. Best-effort: any git failure → False. Mirrors JS _gitPathEverTracked.

    Process-lifetime-memoized on (generation, repo_root, repo_rel_path) — see
    the module-level comment block above ``_EVER_TRACKED_CACHE`` for the cache
    shape, eviction policy, and the mid-sweep git-mutation invalidation
    contract (``invalidate_git_history_cache()``). The cache is a pure
    speed-up: every non-cached code path and the returned value are unchanged
    from the pre-cache implementation, including the best-effort False-on-
    error contract.
    """
    if not repo_rel_path:
        return False

    cache_key = (_EVER_TRACKED_GENERATION, repo_root, repo_rel_path)
    if cache_key in _EVER_TRACKED_CACHE:
        return _EVER_TRACKED_CACHE[cache_key]

    try:
        out = subprocess.run(
            ['git', 'log', '--all', '--max-count=1', '--format=%H', '--', repo_rel_path],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
            creationflags=_CREATIONFLAGS,
        )
        result = bool(out.stdout.strip())
    except Exception as e:
        # Best-effort per module docstring above — not a repo, git missing,
        # timeout, etc. all resolve to "not found" rather than raising.
        sys.stderr.write(
            f'dag: git-history check failed for {repo_rel_path!r}: {e}\n'
        )
        result = False

    if len(_EVER_TRACKED_CACHE) >= _MAX_EVER_TRACKED_CACHE:
        # Evict the oldest half when full to keep memory bounded — mirrors
        # _FRONTMATTER_CACHE's eviction idiom above.
        evict_keys = list(_EVER_TRACKED_CACHE.keys())[: _MAX_EVER_TRACKED_CACHE // 2]
        for k in evict_keys:
            del _EVER_TRACKED_CACHE[k]
    _EVER_TRACKED_CACHE[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Batch-sweep git-history memoization (C6 GAP1 perf, code-reviewer F5).
#
# resolve_target's tier-3 fallback spawns a `git log --all` subprocess per
# unresolved field per record — over a large corpus this multiplies into
# hundreds-to-thousands of full history-walk subprocess spawns for a nightly
# sweep. build_git_history_cache primes a SINGLE `git log --all --name-only
# --no-renames` pass once per validate_all_records-shaped invocation, building
# an in-memory set of every repo-relative path that ever appeared on either
# side of any commit's diff (add, modify, delete, or either side of a rename)
# anywhere in history. Threaded into resolve_target/check_lineage_reachability
# as an OPTIONAL param — absent (None), both fall back to the original
# per-call subprocess path unchanged, so the write-time single-record hook
# path (which never primes a cache) keeps its current behaviour byte-for-byte.
#
# Widened 2026-07-29. Two changes from the original ADD-only pass:
#   - --diff-filter=A dropped entirely, so modify/delete paths are captured
#     too — a path that was deleted still WAS tracked, which is exactly what
#     _git_path_ever_tracked answers.
#   - --no-renames forces git to decompose a rename into a plain delete-of-
#     old-name + add-of-new-name pair rather than a single combined "R"
#     diff-status entry, so --name-only surfaces BOTH the old and new path as
#     separate lines instead of collapsing them into one the filter would
#     drop. (This is a behavioural widening, not just a filter change: with
#     rename detection left on, a renamed path's target name never appears in
#     --name-only output as an "A" line at all, regardless of --diff-filter.)
#
# MEASURED numbers (DoE-claude corpus, one full envelope.emit() run, fresh
# process each time — see the fresh-process warning below):
#   - Landing the ADD-only cache (commit 9667177c) reduced fresh-process
#     TOTAL subprocess spawns 466 -> 448 for the run. Of that ~448-452,
#     ~309-314 are _git_path_ever_tracked's per-path `git log --all
#     --max-count=1 -- <path>` fallback — i.e. the ADD-only cache barely
#     touched the fallback it exists to short-circuit; it was nearly inert
#     in practice despite landing correctly. (~108 of the remainder are a
#     SEPARATE, unrelated `git merge-base --is-ancestor` hot spot, tracked
#     and dispatched separately — not this cache's concern. ~35 are genuine
#     one-off spawns.)
#   - This widening (isolated before/after trees, only dag.py differing,
#     PYTHONHASHSEED pinned) measured fallback spawns 313 -> 308 (-5) on
#     DoE-claude and 39 -> 38 (-1) on claude-klabauter's own corpus. CORRECTION
#     (measured against claude-klabauter's own corpus only — the DoE-claude
#     figures above are left as historical record, not re-verified here):
#     the prior text characterized the residual spawns on THIS corpus as
#     "orphaned / malformed predecessor references" hitting a correct
#     fail-open contract. That was wrong on both halves for
#     ops/emit/priority_resolve.py::_build_parent_map's call path — ~210 of
#     ~239 spawns there were well-formed `predecessor_id` handoff-ids (e.g.
#     "hnd-consolidate-the-resolver-seam--13b7fa") reaching this tier-3 PATH
#     oracle only because that call site omitted `id_index`, not because the
#     refs were malformed; an id can never resolve as a path, so every one
#     was a guaranteed miss, and all 42 distinct ids also carried a sibling
#     `predecessor:` path that already resolved on disk. Fixed at the call
#     site via `resolve_target(..., include_history_tier=False)` rather than
#     by passing `id_index` there (which would have changed which parents
#     that call site finds — a deliberately preserved behaviour, see
#     priority_resolve.py's NEGATIVE-SPEC block). Verified independently: the
#     widened cache is a strict superset of the ADD-only one (12064 -> 14473
#     raw entries on DoE-claude, zero entries lost) and none of the remaining
#     fallback candidates are in it.
#   - FRESH PROCESS ONLY: _EVER_TRACKED_CACHE (below) is process-lifetime, so
#     a second emit() in the SAME process serves most lookups from that cache
#     and under-reports the true per-run spawn count by roughly 3x. This is
#     exactly how a bad "450 -> 137" figure got into an earlier commit
#     message — it was two emit() calls in one process, not two fresh runs.
#     Always launch a new interpreter (or at minimum clear
#     _EVER_TRACKED_CACHE and bump the generation) between "before" and
#     "after" measurements.
#
# Cache correctness note: even widened, this is a single-pass heuristic over
# `git log --all` diffs — a cache miss is deliberately treated as "unknown,
# fall through", never "definitely absent". The cache is a fast-path accept,
# never a fast-path reject; the per-call `git log --all -- <path>` fallback in
# _git_path_ever_tracked remains the authority on a miss.
#
# 2026-07-29 — cache-miss-is-authoritative (309-spawn elimination). The prior
# paragraph's "miss = unknown" rule was correct when the cache was ADD-only
# and genuinely incomplete (see the widening note above). Once the priming
# pass is a full add/modify/delete/rename sweep over EVERY ref (`--all`), a
# miss against a COMPLETE cache stops being "unknown" and becomes "provably
# never tracked" — the per-path `git log --all -- <path>` fallback spawn is
# then pure waste (this is the ~308-per-run fallback spawns the module
# comment above measured: every one resolves False either way, cache-miss or
# subprocess). GitHistoryCache below carries a `complete` flag alongside the
# path set so a miss can be answered authoritatively ONLY when the priming
# pass is known to have seen the repo's FULL history — never merely because a
# cache object happened to be supplied.
#
# `complete` is False (never trust a miss) unless BOTH of the following are
# affirmatively confirmed by _git_history_is_complete() at cache-build time:
#   1. NOT a shallow clone (`git rev-parse --is-shallow-repository` == false).
#      A shallow clone truncates history by construction — a miss there means
#      nothing about whether the path was EVER tracked, only that it wasn't
#      tracked within the fetched depth. Answering authoritatively would
#      manufacture confident false negatives, exactly the fail-closed
#      regression this change must avoid.
#   2. NOT a partial/filtered clone (`remote.origin.promisor` != true). A
#      promisor remote means blob/tree objects (and the commits that touch
#      them) may be fetched lazily on demand rather than present up front —
#      `git log --all --name-only` over a partial clone can silently omit
#      paths whose objects were never materialized locally, which is the
#      same "the priming pass didn't actually see everything" hazard as a
#      shallow clone, just triggered by object-filtering instead of
#      depth-limiting.
# Any git failure (missing binary, timeout, not a repo, non-zero exit) during
# either check resolves to `complete=False` — fail-closed, same best-effort
# posture as every other git call in this module. `complete` is computed ONCE
# per build_git_history_cache() call, not re-probed per lookup: a per-lookup
# `rev-parse`/`config` spawn would trade the 308 per-path fallback spawns for
# a different per-path spawn and net nothing.
#
# Deliberately OUT OF SCOPE — grafts, replace refs, and other ref-rewriting
# mechanisms:
#   - `git log --all` already walks every ref (branches, tags, and — per git's
#     own docs — the refs under `refs/replace/` are applied transparently to
#     ALL git history-reading plumbing, including `git log`, with no opt-out
#     needed) so a replace-ref does not create a blind spot for this cache's
#     `--name-only` pass — it sees the replaced content, same as any other
#     `git log --all` consumer already relied upon elsewhere in this module.
#   - Grafts (`.git/info/grafts` / the deprecated `--grafts` mechanism) rewrite
#     a commit's PARENT list, not its own diff — they can make history look
#     shorter/differently-shaped when walked, but they do not remove a path
#     from the diff of any commit `git log --all` still visits, so they do not
#     create an unseen-path hazard the way a shallow/partial clone does. Grafts
#     are also long-deprecated in favour of replace refs (already covered
#     above) and no fleet repo this module runs against uses them.
#   - Neither is cheaply detectable in the general case (no single git query
#     answers "are grafts or exotic replace-ref rewrites in play") — named
#     here as a considered, not silently skipped, scope boundary rather than
#     added to the two checks above.
#
# Spec backlink: DoE-claude:pln-handoff-spinoff-machinery-robu-0d0f15 § C2 (F5)
# ---------------------------------------------------------------------------

class GitHistoryCache(set):
    """A ``set[str]`` of every repo-relative path ever seen in a diff
    anywhere in the repo's history (see ``build_git_history_cache``), plus a
    ``complete`` flag recording whether that priming pass is known to have
    covered the repo's FULL history.

    Subclasses the builtin ``set`` (not a wrapper) so every existing
    consumer that treats the return value of ``build_git_history_cache`` as
    a plain ``Set[str]`` — membership tests, iteration, ``len()``,
    ``sorted()`` — keeps working completely unchanged; only
    ``_memoized_ever_tracked`` (this module) reads ``.complete``.

    ``complete=True`` licenses treating a MISS against this set as
    authoritative ("never tracked", per the module comment block above) —
    never assume it on a bare ``set`` a caller supplies directly (e.g. a test
    fixture): ``_memoized_ever_tracked`` reads ``.complete`` via
    ``getattr(..., False)`` specifically so an object with no such attribute
    (any plain ``set``) is treated as incomplete and always falls through to
    the per-call fallback, preserving pre-existing behaviour for any caller
    that doesn't go through ``build_git_history_cache``.
    """

    def __init__(self, paths: Set[str], complete: bool) -> None:
        super().__init__(paths)
        self.complete = complete


def as_history_membership_set(cache: Optional[Set[str]]) -> Optional[Set[str]]:
    """Strip a ``GitHistoryCache`` (or any set carrying a ``.complete`` flag)
    down to a bare ``set`` before handing it to a caller that will hold onto
    it and reuse it across MULTIPLE ``resolve_target``/``_memoized_ever_tracked``
    calls spanning more than one snapshot-worthy moment (a whole run, not one
    atomic build-then-consume).

    The rule this codifies: **do not trust ``.complete`` across a cache that
    outlives its snapshot.** ``_memoized_ever_tracked`` treats a MISS against
    a ``.complete=True`` cache as authoritative ("never tracked anywhere in
    history") — correct only for a cache built and consumed atomically within
    one operation, where nothing pruned/committed after the snapshot could
    ever be asked about. A cache reused across a longer-lived run (an entire
    render, an entire emit, an entire whole-tree lint) can easily be asked
    about a path pruned or added AFTER the snapshot was taken; fast-rejecting
    that miss as "never tracked" produces a wrong answer for anything that
    changed mid-run. Stripping to a bare ``set`` here makes every subsequent
    MISS fall through to ``getattr(..., "complete", False)`` reading ``False``
    (no such attribute on a bare set), so ``_memoized_ever_tracked`` always
    bounces to the real per-call git check instead of fast-rejecting.

    A HIT is unaffected either way — membership test on a bare ``set`` is
    just as O(1) as on a ``GitHistoryCache`` — so this only ever changes
    behaviour in the miss-falls-through direction, never the reverse. See
    ``GitHistoryCache``'s own docstring above for the complementary case
    (atomic build-then-consume, where trusting ``.complete`` is legitimate
    and this helper should NOT be used).

    Returns ``None`` unchanged (no cache to strip) and passes through a
    caller-supplied plain ``set`` (no ``.complete`` to begin with) as an
    equivalent bare set.
    """
    if cache is None:
        return None
    return set(cache)


def _git_history_is_complete(repo_root: str, timeout_s: float = 5.0) -> bool:
    """True only if this repo's history is confirmed FULL — not shallow, not
    a partial/filtered clone — so a miss against a cache primed via `git log
    --all --name-only` can be trusted as "never tracked" rather than merely
    "not seen by this fetch". Fail-closed: any git failure (missing binary,
    timeout, non-repo, non-zero exit) returns False. See the module comment
    block above ``GitHistoryCache`` for the full rationale and the grafts/
    replace-refs scope boundary.
    """
    try:
        shallow = subprocess.run(
            ['git', 'rev-parse', '--is-shallow-repository'],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            text=True,
            creationflags=_CREATIONFLAGS,
        )
        if shallow.returncode != 0 or shallow.stdout.strip() != 'false':
            # Non-zero exit (not a repo, git missing) or 'true' (shallow) —
            # either way we cannot trust a miss. `--is-shallow-repository`
            # prints exactly 'true' or 'false' on success; anything else is
            # treated the same as a failure.
            return False
    except Exception:
        return False

    try:
        promisor = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.promisor'],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            text=True,
            creationflags=_CREATIONFLAGS,
        )
        # `git config --get` exits 1 (not an error — key simply unset) when
        # there is no promisor remote configured, which is the common case;
        # only an explicit 'true' value marks a partial/filtered clone.
        if promisor.returncode == 0 and promisor.stdout.strip().lower() == 'true':
            return False
    except Exception:
        return False

    return True


def build_git_history_cache(repo_root: str, timeout_s: float = 15.0) -> Optional[GitHistoryCache]:
    """Prime a set of every repo-relative path that ever appeared in a diff
    anywhere in this repo's history — added, modified, deleted, or either
    side of a rename — via a single `git log --all --name-only --no-renames`
    pass. Best-effort: any git failure returns None — callers must treat a
    None cache identically to an absent one (fall back to per-call
    resolution). Mirrors JS buildGitHistoryCache, extended with a `.complete`
    flag (see ``GitHistoryCache`` / ``_git_history_is_complete`` above) that
    licenses treating a miss as authoritative rather than merely unknown.
    """
    if not repo_root:
        return None
    try:
        out = subprocess.run(
            ['git', 'log', '--all', '--name-only', '--no-renames', '--pretty=format:'],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            text=True,
            creationflags=_CREATIONFLAGS,
        )
        result: Set[str] = set()
        for line in out.stdout.split('\n'):
            trimmed = line.strip()
            if trimmed:
                result.add(trimmed)
        complete = _git_history_is_complete(repo_root, timeout_s=min(timeout_s, 5.0))
        return GitHistoryCache(result, complete)
    except Exception as e:
        # Best-effort per docstring above — callers treat a None cache
        # identically to an absent one (fall back to per-call resolution).
        sys.stderr.write(f'dag: build_git_history_cache failed: {e}\n')
        return None


# ---------------------------------------------------------------------------
# Per-call tier-3 memoization helper (dedup follow-up to the boot_sweep
# 10s-timeout fix above). Module-level and independently unit-tested so its
# correctness never depends on how many call sites resolve_target's tier-3
# block ends up with, or whether any of them return early on a True result —
# a caller-side control-flow invariant is not something this helper needs.
# ---------------------------------------------------------------------------

def _memoized_ever_tracked(
    repo_rel_path: str,
    memo: Dict[str, bool],
    repo_root: str,
    git_history_cache: Optional[Set[str]],
) -> bool:
    """Ask "was repo_rel_path ever git-tracked?", memoized per-call in `memo`.

    Buys at most one lookup per unique repo_rel_path per caller-supplied
    `memo` dict — on top of the process-lifetime cache already inside
    _git_path_ever_tracked, which itself avoids a subprocess spawn on a
    repeat but not the Python-level call overhead. Stores the actual computed
    result (never a bare "seen" flag), so a repeat ask for the same key
    always returns the true stored answer — a caller may ask the same
    repo_rel_path any number of times, in any order, and get a consistent
    answer every time.

    Cache-miss-is-authoritative (2026-07-29): a HIT is always True. A MISS is
    authoritative False ONLY when `git_history_cache.complete` is True (see
    ``GitHistoryCache`` above) — i.e. the priming pass is confirmed to have
    covered the repo's full history. ``getattr(..., 'complete', False)``
    deliberately treats any cache object without a `.complete` attribute (a
    bare ``set``, e.g. a test fixture, or a caller that built one by hand
    rather than via ``build_git_history_cache``) as incomplete, so a miss
    against it still falls through to the per-call fallback exactly as
    before this change — this function's behaviour for such a caller is
    byte-for-byte unchanged.
    """
    # Separator normalization is load-bearing, not cosmetic. `git log
    # --name-only` emits forward-slash repo-relative paths on EVERY platform,
    # so a cache primed by build_git_history_cache() is keyed with '/'. Callers
    # derive repo_rel_path by slicing an os.path.normpath()-ed absolute
    # candidate, which on Windows is backslash-separated — so every lookup
    # missed, and under `complete=True` (cache-miss-is-authoritative) that miss
    # was reported as the authoritative verdict "provably never-existed" for
    # paths git demonstrably tracked. Normalize before both the memo key and
    # the membership test so the two key spaces agree.
    key = repo_rel_path.replace('\\', '/')
    if key in memo:
        return memo[key]
    if git_history_cache is not None and key in git_history_cache:
        result = True
    elif git_history_cache is not None and getattr(git_history_cache, 'complete', False):
        # Authoritative miss — the cache is a confirmed-complete enumeration
        # of every path ever touched by any commit reachable from any ref;
        # absence from it means "never tracked", no subprocess needed.
        result = False
    else:
        result = _git_path_ever_tracked(key, repo_root)
    memo[key] = result
    return result


# ---------------------------------------------------------------------------
# Exported: resolve_target — resolve a handoff path reference to an absolute
# path. Promoted (C2, F1) so consumers reuse resolution instead of duplicating
# it — notably a write-time lineage-reachability hard-reject and a batch
# backfill sweep. Three-tier resolution: live ∪ archive-on-disk ∪ git-history.
# ---------------------------------------------------------------------------

#: Directory prefixes a pointer may name and still be a baton reference.
#: A ref naming any OTHER directory names a different record family, and
#: basename-keyed recovery must not re-home it onto a same-basename baton.
_BATON_FAMILY_DIRS: Tuple[str, ...] = ('state/handoffs', 'archive/handoffs')


def _ref_names_foreign_family(ref: str) -> bool:
    """True when `ref` explicitly names a directory outside the baton families.

    A bare basename (no directory at all) is NOT foreign — bare-ref resolution
    against the handoff corpus is the convention this module exists to serve.

    Matching is per path SEGMENT, not by raw string prefix: a bare
    `startswith('state/handoffs')` also swallows a sibling like
    `state/handoffs-archive/`, silently re-admitting it to basename recovery.
    The ref is normalized first so a `./`-prefixed spelling of a genuine baton
    path is not misread as foreign and denied legitimate stale-path recovery.
    """
    ref_dir = os.path.dirname(os.path.normpath(str(ref)).replace('\\', '/')).strip('/')
    if not ref_dir:
        return False
    return not any(
        ref_dir == family or ref_dir.startswith(family + '/')
        for family in _BATON_FAMILY_DIRS
    )


def resolve_target(
    ref: Any,
    handoff_dir: str,
    repo_root: str,
    git_history_cache: Optional[Set[str]] = None,
    id_index: Optional[Dict[str, str]] = None,
    *,
    include_history_tier: bool = True,
) -> Optional[str]:
    """Resolve an edge-target reference to an absolute path on disk, or the
    sentinel string 'git-history' if the path is disk-absent but was ever
    git-tracked (relocated/deleted, not "never existed").

    Mirrors JS _resolveTarget: tries handoff_dir first, then
    archive/handoffs/<ref>, archive/handoffs/<basename>, month-foldered
    archive/handoffs/YYYY-MM/<basename>, then falls through to a git-history
    check (tier 3). Returns None only when unresolvable in ALL THREE tiers.
    Sentinel strings ('none', 'null', '') are treated as absent edges → None.

    Args:
        git_history_cache: optional pre-built cache from build_git_history_cache().
            When present, tier 3 checks the cache first (O(1)) before falling
            back to a per-call `git log --all` subprocess spawn. A HIT is
            always treated as definitive presence (fast-path ACCEPT). A MISS
            is treated as definitive absence (fast-path REJECT, no per-call
            fallback) ONLY when the cache is a `GitHistoryCache` whose
            `.complete` flag is True — see `GitHistoryCache`'s own docstring
            and `_memoized_ever_tracked` for the exact contract. A caller that
            reuses one cache across more than one snapshot-worthy moment (a
            whole run, not one atomic build-then-consume) MUST pass it through
            `as_history_membership_set()` first to strip `.complete` and force
            every miss to fall through to the per-call check instead of
            fast-rejecting.
        id_index: optional handoff_id -> absolute path map from
            build_handoff_id_index() (C6 pointer-normalization seam). A ref
            that does not end in '.md' is assumed to be a handoff_id (the
            shape `predecessor_id`/`origin_handoff_id` values take in the
            corpus, e.g. "hnd-foo-1a2b3c") rather than a filename/path, and is
            looked up here BEFORE the filename-based tiers below — those tiers
            cannot ever resolve an id-shaped string, so trying them first would
            just waste tier-3 git-history subprocess spawns on a guaranteed
            miss. A miss against id_index (index absent, or ref not in it)
            falls through to the normal tiers unchanged, so behaviour for
            path/filename-shaped refs is byte-for-byte unaffected.
        include_history_tier: keyword-only, defaults to True (current
            behaviour unchanged for every existing caller). Set False when
            the caller has no use for a 'git-history' answer — it discards
            the sentinel identically to None (see e.g.
            ops/emit/priority_resolve.py::_build_parent_map, whose only
            consumer of this return value treats 'git-history' and None as
            the same "no parent" outcome). Skips tier 3 entirely, including
            any `ever_tracked()` subprocess spawn, and returns None instead
            of 'git-history' at every point below that would otherwise
            return the sentinel. Tiers 1-2 (on-disk resolution) are
            unaffected either way.
    """
    if ref is None:
        return None
    target = str(ref).strip()
    if not target or target in ('none', 'null'):
        return None

    if id_index and not target.endswith('.md') and target in id_index:
        return id_index[target]

    # Memoization guard for tier 3 (below): buys at most one lookup per
    # unique repo-relative path per resolve_target() call — on top of the
    # process-lifetime cache in _git_path_ever_tracked, which still spawns
    # nothing on a repeat, but a duplicate call is otherwise redundant work
    # this closure can shortcut without a subprocess round-trip. Delegates to
    # _memoized_ever_tracked (module level, independently unit-tested) so
    # correctness doesn't depend on how many call sites exist below or
    # whether they return early on True.
    _tier3_memo: Dict[str, bool] = {}

    def ever_tracked(repo_rel_path: str) -> bool:
        return _memoized_ever_tracked(repo_rel_path, _tier3_memo, repo_root, git_history_cache)

    # Already absolute?
    if os.path.isabs(target):
        if os.path.exists(target):
            return target
        # Tier 3 for an absolute path: derive repo-relative form if possible.
        if repo_root and include_history_tier:
            norm_root = repo_root.rstrip('/\\')
            if target.startswith(norm_root):
                rel = target[len(norm_root):].lstrip('/\\')
                if ever_tracked(rel):
                    return 'git-history'
        return None

    # Bare filename or relative path — try under handoff_dir first
    basename = os.path.basename(target)
    candidates = [
        os.path.normpath(os.path.join(handoff_dir, target)),
        # Root-anchored live-handoff resolution — lets a walk that STARTED from an
        # archived (possibly month-nested) closing handoff still resolve a LIVE ancestor,
        # for both ref conventions: repo-relative (`state/handoffs/foo.md`) and bare
        # basename (`foo.md`). handoff_dir alone cannot cover both because it is the start
        # node's own dir, which is the archive dir — not `state/handoffs` — on the escape path.
        os.path.normpath(os.path.join(repo_root, target)),
    ]
    # Basename recovery (the tiers below) is STALE-PATH recovery within the baton
    # families — it must not re-home a pointer that explicitly names a different
    # family. `predecessor: cross-repo/inbox/<name>.md` on a handoff itself named
    # `<name>.md` (the cross-repo memo-pickup convention: the handoff inherits the
    # memo's slug) otherwise resolves onto the handoff itself once the memo moves
    # to `cross-repo/archive/`, and `referenced_by` then reports the baton as its
    # own referencer — a self-edge that blocks its archival forever. Same rule, and
    # same reasoning, as `tests/_baton_dag_oracle.build_children_index`'s
    # non-baton-family skip; the two implementations reach it independently.
    if not _ref_names_foreign_family(target):
        candidates.extend([
            os.path.normpath(os.path.join(repo_root, 'state', 'handoffs', basename)),
            os.path.normpath(os.path.join(repo_root, 'archive', 'handoffs', target)),
            os.path.normpath(os.path.join(repo_root, 'archive', 'handoffs', basename)),
        ])

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    # Month-foldered archive: archive/handoffs/YYYY-MM/<basename>
    archive_dir = os.path.normpath(os.path.join(repo_root, 'archive', 'handoffs'))
    if os.path.isdir(archive_dir) and not _ref_names_foreign_family(target):
        try:
            for entry in os.listdir(archive_dir):
                if re.match(r'^\d{4}-\d{2}$', entry):
                    candidate = os.path.join(archive_dir, entry, basename)
                    if os.path.exists(candidate):
                        return candidate
        except OSError:
            pass  # Archive dir unreadable — treat as empty, continue

    # Tier 3 — git-history. Try the ref as given (relative to repo_root, the
    # conventional form) and, if candidates[] resolve inside repo_root, also
    # try each of those re-derived as repo-relative.
    if repo_root and include_history_tier:
        if ever_tracked(target):
            return 'git-history'
        norm_root_rel = repo_root.rstrip('/\\')
        # Month-foldered archive paths must be offered to tier 3 explicitly.
        # The on-disk month-folder sweep above enumerates only directories that
        # still EXIST; a target age-pruned from disk leaves no such directory to
        # enumerate, and `archive/handoffs/<basename>` (flat) is not the path git
        # tracked it under. Derive the month from the basename's own YYYY-MM
        # prefix (the corpus's filename convention) and additionally offer every
        # month directory currently on disk, so a reference from anywhere
        # resolves a target ever tracked under archive/handoffs/YYYY-MM/.
        tier3_extra: List[str] = []
        foreign_family = _ref_names_foreign_family(target)
        month_match = re.match(r'^(\d{4}-\d{2})-\d{2}', basename)
        if month_match and not foreign_family:
            tier3_extra.append(f'archive/handoffs/{month_match.group(1)}/{basename}')
        if os.path.isdir(archive_dir) and not foreign_family:
            try:
                for entry in os.listdir(archive_dir):
                    if re.match(r'^\d{4}-\d{2}$', entry):
                        tier3_extra.append(f'archive/handoffs/{entry}/{basename}')
            except OSError:
                pass
        for cand_abs in candidates:
            if cand_abs.startswith(norm_root_rel):
                cand_rel = cand_abs[len(norm_root_rel):].lstrip('/\\')
                if ever_tracked(cand_rel):
                    return 'git-history'
        for cand_rel in tier3_extra:
            if ever_tracked(cand_rel):
                return 'git-history'

    return None


# Backward-compat internal alias — pre-extension callers within this module
# used the underscore-prefixed name.
_resolve_target = resolve_target


# ---------------------------------------------------------------------------
# Exported: build_handoff_id_index — the id-suffixed pointer-field resolver
# input (C6 pointer-normalization seam, 2026-07-26).
# ---------------------------------------------------------------------------

def build_handoff_id_index(
    paths: List[str], metas: Optional[Dict[str, dict]] = None
) -> Dict[str, str]:
    """Map frontmatter `handoff_id` -> absolute path, for the given handoff paths.

    This is the missing link for `predecessor_id` / `origin_handoff_id`
    frontmatter values: those fields name a handoff_id, not a filename or
    path, so resolve_target's filename-based tiers can never resolve them.
    Build this index once per scan set and pass it to
    resolve_target(..., id_index=...), which looks an id-shaped ref up here.

    Args:
        paths: Handoff file paths to index (typically the same live+archived
               scan set the caller is already walking — referenced_by's
               live_set, or a directory scan for walk_forward, which has no
               pre-built scan set of its own).

    Returns:
        dict mapping each non-blank `handoff_id` frontmatter value found to
        the absolute path of the file that declared it. A handoff with a
        missing/blank `handoff_id` is simply absent from the index — no
        special-casing, same as any other unresolvable pointer. On a
        (should-not-happen) handoff_id collision across two files, the
        later path in `paths` wins — callers do not currently rely on
        collision behaviour, so this is not treated as an error.
    """
    index: Dict[str, str] = {}
    for p in paths:
        # `metas` is a caller-supplied read the same way build_reverse_edge_index
        # takes one: this index otherwise re-reads the entire corpus a caller
        # has usually just read for its own reasons (62.5ms over 267 handoffs,
        # measured on the boot backstop). Keyed by os.path.abspath, matching
        # the key this function itself writes.
        meta = None if metas is None else metas.get(os.path.abspath(p))
        if meta is None:
            meta = _read_meta(p)
        hid = meta.get('handoff_id') if meta else None
        if hid is None:
            continue
        hid_str = str(hid).strip()
        if hid_str:
            index[hid_str] = os.path.abspath(p)
    return index


def _scan_handoff_corpus_paths(repo_root: str) -> List[str]:
    """Best-effort scan of state/handoffs/*.md + archive/handoffs/**/*.md.

    Used only to build the handoff_id index (build_handoff_id_index) for
    walk_forward, which — unlike referenced_by — is not handed a pre-scanned
    live_set to index against; it discovers nodes by walking edges outward
    from a single start_path, so an id-shaped edge needs a repo-wide index
    built up front. Missing/unreadable subtrees are silently skipped (an
    id-shaped ref then simply falls through resolve_target's id_index lookup
    unresolved, same as any other dangling pointer — this is a best-effort
    supplementary index, not the fail-closed live-set scan in
    coordinator_core/ops/handoff_children.py::_collect_handoff_paths).
    """
    paths: List[str] = []
    state_dir = os.path.join(repo_root, 'state', 'handoffs')
    if os.path.isdir(state_dir):
        try:
            for fn in os.listdir(state_dir):
                if fn.endswith('.md'):
                    full = os.path.join(state_dir, fn)
                    if os.path.isfile(full):
                        paths.append(full)
        except OSError:
            pass
    archive_dir = os.path.join(repo_root, 'archive', 'handoffs')
    if os.path.isdir(archive_dir):
        for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=lambda _exc: None):
            for fn in filenames:
                if fn.endswith('.md'):
                    full = os.path.join(dirpath, fn)
                    if os.path.isfile(full):
                        paths.append(full)
    return paths


def scan_repo_handoff_corpus(repo_root: str) -> List[str]:
    """Public alias for ``_scan_handoff_corpus_paths`` — the full on-disk
    handoff corpus (``state/handoffs/*.md`` + ``archive/handoffs/**/*.md``)
    for *repo_root*. Exposed for a caller building a corpus-wide structure
    ONCE per run (e.g. ``priority_resolve.PriorityResolveCache``) rather than
    letting each of many per-node calls re-scan the same directories.
    """
    return _scan_handoff_corpus_paths(repo_root)


# ---------------------------------------------------------------------------
# Utility: infer repo_root from a handoff_dir
# ---------------------------------------------------------------------------

def _repo_root_from_handoff_dir(handoff_dir: str) -> str:
    """Infer repo root from a handoff directory path.

    handoff_dir is typically <repo_root>/state/handoffs or
    <repo_root>/archive/handoffs — so root = two dirs up. Mirrors JS
    _repoRootFromHandoffDir.
    """
    return os.path.normpath(os.path.join(handoff_dir, '..', '..'))


# ---------------------------------------------------------------------------
# Exported: handoff_edges — the edge-kind SSOT kernel
# ---------------------------------------------------------------------------

def handoff_edges(node_meta: dict, edge_kinds: Set[str]) -> List[str]:
    """Return raw edge-target strings for the named edge kinds.

    Given parsed frontmatter metadata for a node and a set of edge-kind names,
    returns the resolved edge-target strings for those fields (raw string values,
    not path-resolved). Sentinels ('none', 'null', None, '') are excluded.

    Purpose: edge-kind SSOT kernel — no consumer re-derives which frontmatter
    fields are edges. Mirrors JS handoffEdges.

    Also reads each kind's id-suffixed alias field(s), if any (see
    EDGE_KIND_FIELD_ALIASES) — e.g. 'predecessor' additionally reads
    `predecessor_id`. The alias values are handoff_ids, not paths; they are
    resolved downstream by resolve_target()'s id_index lookup, not here —
    this function only collects raw (still-unresolved) reference strings.

    Args:
        node_meta:  Parsed frontmatter dict for the node.
        edge_kinds: Subset of EDGE_KIND_META keys (e.g. 'predecessor',
                    'additional_predecessors', 'forked_from', 'origin_handoff').

    Returns:
        List of raw edge-target references (not yet path-resolved).
    """
    result: List[str] = []
    for kind in edge_kinds:
        kind_meta = EDGE_KIND_META.get(kind)
        if kind_meta is None:
            continue  # Unknown edge kind — skip
        field_names = [kind_meta['field']] + list(EDGE_KIND_FIELD_ALIASES.get(kind, ()))
        for field_name in field_names:
            val = node_meta.get(field_name)
            if val is None:
                continue
            if kind_meta['multi']:
                # Array field — include each non-sentinel element
                items = val if isinstance(val, list) else []
                for item in items:
                    s = str(item).strip() if item is not None else ''
                    if s and s not in ('none', 'null'):
                        result.append(s)
            else:
                s = str(val).strip() if val is not None else ''
                if s and s not in ('none', 'null'):
                    result.append(s)
    return result


# ---------------------------------------------------------------------------
# Exported: walk_forward — forward DFS accumulation with gray/black cycle detection
# ---------------------------------------------------------------------------

class _LazyHandoffIdIndex:
    """Drop-in stand-in for a pre-built handoff_id -> abs-path dict, for
    resolve_target's `id_index` parameter, that defers the repo-wide
    corpus scan (build_handoff_id_index(_scan_handoff_corpus_paths(...)))
    until the FIRST time resolve_target actually needs it — i.e. the first
    id-shaped ref (`not target.endswith('.md')`) it is asked to look up.

    resolve_target's only interactions with id_index are: `if id_index`,
    `target in id_index`, and `id_index[target]` — this class implements
    exactly those three, and none of the rest of the Mapping protocol,
    because that's all resolve_target's contract requires.

    walk_forward's common case (edge_kinds={'predecessor'}, zero id-shaped
    refs on the walked path — the corpus scan is eligible by edge-kind but
    never actually triggered) never calls __contains__ with a non-'.md'
    ref, so the scan never runs. A walk that DOES hit one or more
    id-shaped refs triggers the scan on the first such ref and reuses the
    built dict (memoized on self) for every subsequent lookup in the same
    call — one scan per walk_forward() call, not one per id-shaped ref.
    """

    __slots__ = ('_repo_root', '_index')

    def __init__(self, repo_root: str) -> None:
        self._repo_root = repo_root
        self._index: Optional[Dict[str, str]] = None

    def _ensure_built(self) -> Dict[str, str]:
        if self._index is None:
            self._index = build_handoff_id_index(
                _scan_handoff_corpus_paths(self._repo_root)
            )
        return self._index

    def __bool__(self) -> bool:
        # Always truthy: this object stands in for an eligible-but-not-yet-
        # built index, mirroring the pre-lazy code's `if id_index:` where
        # id_index was already a (possibly empty) dict — an empty dict is
        # falsy in Python, but the eager code never reached that branch
        # with an empty dict short-circuiting the `if repo_root and any(...)`
        # eligibility check, so truthiness here does not need to model
        # "built and empty" — only "eligible to be consulted".
        return True

    def __contains__(self, key: str) -> bool:
        return key in self._ensure_built()

    def __getitem__(self, key: str) -> str:
        return self._ensure_built()[key]


def walk_forward(
    start_path: str,
    edge_kinds: Optional[Set[str]] = None,
    node_gate: Optional[Callable[[dict], bool]] = None,
    handoff_dir: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Forward traversal from start_path, following edges named in edge_kinds.

    Traversal:
      DFS with explicit stack to avoid recursion depth limits.
      BLACK set: fully-finished nodes. Re-encounter → benign diamond → skip (not abort).
      GRAY set: nodes currently on the active DFS path. Re-encounter → back-edge →
        terminatedEarly='lineage-cycle'.
      Each file is parsed at most once per (path, sha256-content-hash) via the frontmatter cache.
      orderedPaths reflects first-encounter order (terminal → roots).

    Missing-link handling:
      An edge-target that cannot be resolved → terminatedEarly='missing-link', but
      accumulation continues on remaining edges of the current node and the rest of
      the frontier.

    Mirrors JS walkForward exactly (walk-handoff-dag.js:190-302).

    Args:
        start_path:  Absolute (or resolvable) path to the starting node.
        edge_kinds:  Set of edge-kind names to follow. Defaults to {'predecessor'}.
        node_gate:   Callable(meta) → bool. If False, node is not expanded or counted.
                     Defaults to lambda meta: True.
        handoff_dir: Directory to resolve relative edge refs against. Inferred from
                     start_path's dirname if not provided.
        repo_root:   Explicit repository root for edge resolution. When None (default),
                     inferred two-dirs-up from handoff_dir — correct only when the start
                     node lives in `<repo_root>/state|archive/handoffs`; pass explicitly
                     when the start node may be month-nested under
                     `archive/handoffs/YYYY-MM/` (which breaks the two-up inference).

    Returns:
        dict with keys:
          'nodes'          — {abs_path: frontmatter_dict} for each visited node.
          'orderedPaths'   — list of abs paths in first-encounter order (start_path first).
          'terminatedEarly' — '' | 'lineage-cycle' | 'missing-link'.
    """
    if edge_kinds is None:
        edge_kinds = {'predecessor'}
    if node_gate is None:
        node_gate = lambda meta: True  # noqa: E731

    abs_start = os.path.abspath(start_path)

    if handoff_dir is None:
        handoff_dir = os.path.dirname(abs_start)
    if repo_root is None:
        repo_root = _repo_root_from_handoff_dir(handoff_dir)

    # C6 pointer-normalization seam: only pay for a repo-wide handoff_id scan
    # when an edge kind actually being followed has an id-suffixed alias
    # (EDGE_KIND_FIELD_ALIASES) — the common walk_forward({'predecessor'})
    # call always qualifies (predecessor_id is aliased), but a caller
    # restricted to e.g. {'forked_from'} alone skips the scan entirely.
    id_index: Optional[Union['_LazyHandoffIdIndex', Dict[str, str]]] = None
    if repo_root and any(EDGE_KIND_FIELD_ALIASES.get(k) for k in edge_kinds):
        id_index = _LazyHandoffIdIndex(repo_root)

    nodes: Dict[str, dict] = {}
    ordered_paths: List[str] = []
    terminated_early = ''

    # DFS with explicit stack using two-visit (enter/exit) frames.
    # gray_set: nodes on the current active DFS path (for back-edge detection).
    # black_set: fully finished nodes (for diamond convergence detection).
    gray_set: Set[str] = set()
    black_set: Set[str] = set()

    # Stack entries: {'path': str, 'phase': 'enter'|'exit'}
    dfs_stack = [{'path': abs_start, 'phase': 'enter'}]

    while dfs_stack:
        frame = dfs_stack.pop()
        abs_path = frame['path']

        if frame['phase'] == 'exit':
            # Finishing this node — mark black, remove from gray
            gray_set.discard(abs_path)
            black_set.add(abs_path)
            continue

        # phase == 'enter'

        # Diamond check: already fully finished (black)?
        if abs_path in black_set:
            # Benign convergence — skip, do not abort
            continue

        # Cycle check: currently on active path (gray)?
        if abs_path in gray_set:
            terminated_early = 'lineage-cycle'
            # Do not abort — just skip this re-encounter
            continue

        # Parse the node
        meta = _read_meta(abs_path)

        # nodeGate: if gate rejects, do not expand or count this node
        if not node_gate(meta):
            # Mark black immediately (don't visit its edges)
            black_set.add(abs_path)
            continue

        # Mark gray (entering)
        gray_set.add(abs_path)

        # Record this node (first encounter)
        nodes[abs_path] = meta
        ordered_paths.append(abs_path)

        # Push exit frame BEFORE processing edges so gray→black on the way back up
        dfs_stack.append({'path': abs_path, 'phase': 'exit'})

        # Collect edges
        raw_edges = handoff_edges(meta, edge_kinds)

        # Resolve edges and collect valid targets
        edges_to_push: List[str] = []
        for raw_ref in raw_edges:
            target_abs = resolve_target(raw_ref, handoff_dir, repo_root, id_index=id_index)
            if target_abs is None:
                # Unresolvable edge — record missing-link but continue
                terminated_early = 'missing-link'
                continue
            if target_abs == 'git-history':
                # Tier-3-only resolution: the target existed (proves the edge is
                # not "never existed") but has no disk path to walk into for
                # forward accumulation. Not a missing-link — just nothing
                # further to traverse from here.
                continue
            edges_to_push.append(target_abs)

        # Push in reverse order so first edge is processed first (DFS characteristic)
        for edge_path in reversed(edges_to_push):
            dfs_stack.append({'path': edge_path, 'phase': 'enter'})

    return {
        'nodes': nodes,
        'orderedPaths': ordered_paths,
        'terminatedEarly': terminated_early,
    }


# ---------------------------------------------------------------------------
# Exported: referenced_by — DIRECT non-transitive reverse-membership test
# ---------------------------------------------------------------------------

def build_reverse_edge_index(
    live_set: List[str],
    handoff_dir: Optional[str] = None,
    id_index_source: Optional[List[str]] = None,
    metas: Optional[Dict[str, dict]] = None,
) -> Dict[str, Any]:
    """One forward pass over live_set producing a reverse-edge index that
    `referenced_by_indexed` answers any number of single-target lookups from.

    WHY THIS EXISTS. `referenced_by` re-walks the entire live_set on every
    call — one `_read_meta` per node per call — because the only
    target-dependent step is the final path comparison. A caller asking about
    N candidates over an M-node index therefore pays N x M reads to answer a
    question that needs M. Measured on `session.boot_sweep`'s backstop before
    this function existed: 176 candidates over ~548 nodes = **96,534 file
    opens, 21.5s in `_read_meta`**, against a 200ms budget.

    `_FRONTMATTER_CACHE` does NOT solve this and is not the defect. It caches
    PARSING; every `_read_meta` still does `read_bytes()` + sha256 by design,
    to close the TOCTOU window between stamp-read and content-read. The cost
    is the reads, not the parses, so the fix has to be asking fewer times.

    Equivalence to `referenced_by`, which is what makes this safe to swap in:
    the per-node work (read frontmatter, enumerate `handoff_edges`, resolve
    each ref through `resolve_target`) is IDENTICAL and target-independent, so
    it is hoisted verbatim. Only the comparison against `abs_target` is
    per-target, and that is what `referenced_by_indexed` does in memory. Both
    resolution outcomes are preserved: a ref that resolves to a disk path is
    keyed by its absolute path, and one that is unresolvable or tier-3-only
    keeps the SAME basename fallback (including the `_ref_names_foreign_family`
    exclusion) under a separate key space.

    Tier 3 stays off here for exactly the reason `referenced_by` names in its
    own negative-spec: it can only return the sentinel, never a disk path, and
    a live-membership test needs a disk path on both sides.

    Returns an opaque index. Treat it as such — pass it to
    `referenced_by_indexed`, do not read its shape.
    """
    if handoff_dir is None:
        if live_set:
            handoff_dir = os.path.dirname(live_set[0])
        else:
            raise ValueError(
                "dag.build_reverse_edge_index: handoff_dir is required when "
                "live_set is empty"
            )
    repo_root = _repo_root_from_handoff_dir(handoff_dir)
    all_kinds = set(ARCHIVAL_EDGE_KINDS)

    # The id index resolves `*_id` edge aliases to paths, so it must span the
    # caller's FULL corpus even when the scan set below is narrowed — a live
    # node's id-form ref is resolved against every known handoff, not just the
    # ones being scanned for edges.
    id_index: Optional[Dict[str, str]] = None
    if any(EDGE_KIND_FIELD_ALIASES.get(k) for k in all_kinds):
        id_index = build_handoff_id_index(
            id_index_source if id_index_source is not None else live_set,
            metas=metas,
        )

    by_abspath: Dict[str, List[Tuple[str, str]]] = {}
    by_basename: Dict[str, List[Tuple[str, str]]] = {}

    # NEGATIVE SPEC — do not "parallelise the reads". Tried and reverted
    # 2026-08-23: an 8-worker ThreadPoolExecutor prefetching `_read_meta`
    # across this scan moved p50 from 546.9ms to 562.5ms over n=12, i.e.
    # nothing outside noise. `_read_meta` is not I/O-bound in the way the
    # shape suggests — its cost is the sha256 stamp and the frontmatter parse,
    # both CPU-bound and GIL-serialised, so threads add a pool and buy no
    # latency. Reach for a different lever (fewer nodes, cheaper per-node
    # work), not a wider one.
    for node_abs_path in live_set:
        # `metas` lets a caller that has ALREADY read the corpus hand its
        # parsed frontmatter in rather than making this scan read every file a
        # second time. `_read_meta`'s cache does not close that gap: it is
        # keyed by (abspath, content-hash), so every call still pays the read
        # and the hash to discover whether it hit. Measured on the boot
        # backstop, where three phases each walked the same 266 handoffs:
        # 31ms + 47ms + 94ms, most of it the same bytes read three times.
        meta = None if metas is None else metas.get(node_abs_path)
        if meta is None:
            meta = _read_meta(node_abs_path)
        node_handoff_dir = os.path.dirname(node_abs_path)
        for kind in all_kinds:
            for raw_ref in handoff_edges(meta, {kind}):
                resolved_ref = resolve_target(
                    raw_ref,
                    node_handoff_dir,
                    repo_root,
                    id_index=id_index,
                    include_history_tier=False,
                )
                if resolved_ref is None or resolved_ref == 'git-history':
                    if not _ref_names_foreign_family(raw_ref):
                        by_basename.setdefault(
                            os.path.basename(raw_ref), []
                        ).append((node_abs_path, kind))
                    continue
                by_abspath.setdefault(
                    os.path.abspath(resolved_ref), []
                ).append((node_abs_path, kind))

    return {
        'by_abspath': by_abspath,
        'by_basename': by_basename,
        'live_set_size': len(live_set),
    }


def referenced_by_indexed(
    target: str,
    index: Dict[str, Any],
    edge_kinds: Optional[Set[str]] = None,
    exclude: Optional[Union[List[str], Set[str], FrozenSet[str]]] = None,
) -> Dict[str, Any]:
    """`referenced_by`'s answer for one target, read out of a prebuilt index.

    Same return shape and same semantics; see `build_reverse_edge_index` for
    the equivalence argument. `edge_kinds` filters in memory exactly as
    `handoff_edges` filtered at scan time.
    """
    if edge_kinds is None:
        edge_kinds = set(ARCHIVAL_EDGE_KINDS)

    exclude_set: Set[str] = set()
    if exclude:
        for ex in exclude:
            if ex:
                exclude_set.add(os.path.abspath(str(ex)))

    abs_target = os.path.abspath(target)
    hits = list(index['by_abspath'].get(abs_target, ()))
    hits += list(index['by_basename'].get(os.path.basename(abs_target), ()))

    referencers: List[str] = []
    seen: Set[str] = set()
    for node_abs_path, kind in hits:
        if kind not in edge_kinds:
            continue
        if node_abs_path in seen or node_abs_path in exclude_set:
            continue
        seen.add(node_abs_path)
        referencers.append(node_abs_path)

    return {
        'referenced': len(referencers) > 0,
        'referencedBy': referencers,
    }


def referenced_by(
    target: str,
    live_set: List[str],
    edge_kinds: Optional[Set[str]] = None,
    handoff_dir: Optional[str] = None,
    exclude: Optional[Union[List[str], Set[str], FrozenSet[str]]] = None,
) -> Dict[str, Any]:
    """Test whether target is named as an edge-target by any node in live_set.

    Single-hop membership test, NOT transitive reachability. Mirrors JS referencedBy.

    Args:
        target:      Absolute path of the candidate node to test.
        live_set:    List of absolute paths to scan for references to target.
        edge_kinds:  Edge kinds to consider. Defaults to ARCHIVAL_EDGE_KINDS
                     (all three kinds — see that constant's docstring for why
                     `forked_from` legitimately blocks archival, distinct
                     from the CONTINUATION_EDGE_KINDS question).
        handoff_dir: Directory for resolving relative refs. Inferred from live_set[0]
                     dirname if not provided; falls back to cwd.
        exclude:     Paths (list or set) to drop from live_set before scanning.
                     Absent or empty → byte-identical behaviour to absent (backward compat).

    Returns:
        dict with keys:
          'referenced'   — True if any node in live_set (after exclusion) names target.
          'referencedBy' — list of absolute paths of nodes that reference target.
    """
    if edge_kinds is None:
        edge_kinds = set(ARCHIVAL_EDGE_KINDS)

    if handoff_dir is None:
        if live_set:
            handoff_dir = os.path.dirname(live_set[0])
        else:
            raise ValueError(
                "dag.referenced_by: handoff_dir is required when live_set is empty; "
                "os.getcwd() fallback removed (AC-5 — daemon cwd is not a valid repo root)."
            )
    repo_root = _repo_root_from_handoff_dir(handoff_dir)

    # Build exclusion set (resolved absolute paths)
    exclude_set: Set[str] = set()
    if exclude:
        exclude_iter = exclude if isinstance(exclude, (set, frozenset)) else exclude
        for ex in exclude_iter:
            if ex:
                exclude_set.add(os.path.abspath(str(ex)))

    # Filter live_set by exclusion set (no-op when exclude_set is empty)
    if exclude_set:
        filtered_live_set = [p for p in live_set if os.path.abspath(p) not in exclude_set]
    else:
        filtered_live_set = live_set

    abs_target = os.path.abspath(target)
    target_basename = os.path.basename(abs_target)

    # C6 pointer-normalization seam: build the handoff_id index only when an
    # edge kind actually being followed has an id-suffixed alias — the default
    # {'predecessor', 'additional_predecessors', 'forked_from'} always
    # qualifies (predecessor_id is aliased). filtered_live_set is already the
    # caller's full scan set, so no extra filesystem scan is needed here
    # (unlike walk_forward, which has no equivalent pre-scanned set).
    id_index: Optional[Dict[str, str]] = None
    if any(EDGE_KIND_FIELD_ALIASES.get(k) for k in edge_kinds):
        id_index = build_handoff_id_index(filtered_live_set)

    referencers: List[str] = []

    for node_abs_path in filtered_live_set:
        meta = _read_meta(node_abs_path)
        node_handoff_dir = os.path.dirname(node_abs_path)
        raw_edges = handoff_edges(meta, edge_kinds)

        for raw_ref in raw_edges:
            # NEGATIVE SPEC — tier 3 is off here by construction, not by oversight.
            # This loop's next branch collapses `None` and `'git-history'` onto one
            # handling path, and tier 3 can only ever return the sentinel (never a
            # disk path), so asking it changes no outcome this function can observe
            # while costing one `git log --all` spawn per (node, edge) pair over the
            # caller's whole scan set. Do NOT re-enable it to "resolve more refs": a
            # live-membership test needs a disk path on both sides, which tier 3 by
            # definition cannot supply. Same grounds as `emit/priority_resolve.py ::
            # _build_parent_map` and `pickup_assemble :: _resolve_lineage_artifact_path`.
            resolved_ref = resolve_target(
                raw_ref,
                node_handoff_dir,
                repo_root,
                id_index=id_index,
                include_history_tier=False,
            )
            if resolved_ref is None or resolved_ref == 'git-history':
                # Unresolvable, or tier-3-only (no disk path to compare against
                # abs_target — a live-membership test needs a disk path on both
                # sides). Fall back to basename comparison in either case —
                # except for a ref that explicitly names a non-baton family,
                # which names that file and not a same-basename baton (see
                # `_ref_names_foreign_family`; without this the resolve_target
                # fix just relocates the self-edge into this branch).
                if not _ref_names_foreign_family(raw_ref) and os.path.basename(raw_ref) == target_basename:
                    referencers.append(node_abs_path)
                    break
                continue
            if os.path.abspath(resolved_ref) == abs_target:
                referencers.append(node_abs_path)
                break

    return {
        'referenced': len(referencers) > 0,
        'referencedBy': referencers,
    }


# ---------------------------------------------------------------------------
# Waived pre-reclaim-boundary dangling predecessors (C6 GAP2, 2026-07-08).
#
# All five entries below were introduced to this repo by a SINGLE commit,
# `50e2847 reclaim(archive): DoE pre-July archive history from claude-klabauter`, which
# squash-reclaimed inert pre-July archive records that had been stranded in
# claude-klabauter by the 2026-07-03 relocation. That reclaim brought in each SUCCESSOR
# handoff (the record listed as a key below) but NOT its own predecessor,
# which lived and died entirely inside claude-klabauter's original (pre-split) repo
# history — never independently reclaimed because it was already
# consumed/superseded before the reclaim boundary. This is mechanically
# distinct from a git-history-tier-resolvable archive-relocation-stranded
# case: those targets ARE resolvable within this repo; these are provably
# absent from this repo's entire history because they were never part of it.
#
# Waiver shape: keyed by the RECORD's own repo-relative path (not the
# unresolvable target — a record can carry at most one waived edge in this
# narrow class), so a future edit to a waived record that introduces a NEW
# unrelated dangling edge is NOT silently covered by this list.
#
# Mirrors JS WAIVED_DANGLING_PREDECESSORS verbatim (same keys/values).
#
# Spec backlink: DoE-claude:pln-handoff-spinoff-machinery-robu-0d0f15 § C6 (GAP2)
# ---------------------------------------------------------------------------
WAIVED_DANGLING_PREDECESSORS: Dict[str, str] = {
    'archive/handoffs/2026-06-28_081122_d5714a02-8a54-4897-babf-457e5833ed9c.md':
        'state/handoffs/2026-06-27_224629_roadmap-stub-numbering-dependency-order.md',
    'archive/handoffs/2026-06-28_081627_52b35ed9-0b63-47fa-a501-a93734abff15.md':
        'state/handoffs/2026-06-27_223611_pickup-liveness-canonical-not-raw-pid.md',
    'archive/handoffs/2026-06-30_040412_e92b195b-799d-4e54-8baf-75882a82e659.md':
        'state/handoffs/2026-06-28_080820_eff4f4ab-5277-4e3c-8362-e0c229a2b9dc.md',
    'archive/handoffs/2026-06/2026-06-30_114057_80de4efd-6159-4812-a16f-7c8dc8578c9e.md':
        '2026-06-30_033536_b7d4f348.md',
    'archive/handoffs/2026-06/2026-06-30_185537_d92000f8.md':
        'state/handoffs/2026-06-27_095007_roadmap-ccos-7.md',
}


# ---------------------------------------------------------------------------
# Exported: check_lineage_reachability — shared reachability rule kernel
#
# Promoted (C6 GAP1 backfill, 2026-07-08) so a write-time PreToolUse hook and a
# batch corpus sweep apply the IDENTICAL reachability rule — not just the same
# resolve_target, but the same per-field rule set (which fields are checked,
# the kind:recovery same-repo-only carve-out, the "resolved is git-history
# sentinel → OK" logic).
#
# Checks predecessor / forked_from / additional_predecessors[] / origin_handoff
# (origin_handoff is a real state/handoffs/ path edge, walked the same way as
# the other three) via resolve_target (live ∪ archive-on-disk ∪ git-history,
# C2 F1). A target unresolvable in all three tiers is a hard violation —
# provably never-existed, not merely relocated.
#
# kind:recovery predecessor is a SHA, not a handoff path — SUBJECT TO THE
# SAME-REPO-ONLY FOREIGN-BATON CARVE-OUT: there is no per-record repo-identity
# discriminator, so an unreachable recovery SHA is NEVER rejected here — it
# may be a legitimate sibling-repo crash SHA per the deliberately-deferred
# foreign-baton boundary. This function does NOT check kind:recovery
# predecessor at all; the field is simply skipped.
#
# forked_from / additional_predecessors ARE checked unconditionally (no
# kind:recovery exemption) — the recovery convention is SHA-shaped ONLY on
# predecessor; a recovery handoff never carries a fan-in SHA in
# additional_predecessors[] or a branch-point SHA in forked_from.
#
# Negative-spec: does NOT walk transitively — each of the four fields is
# checked as a single direct edge, not a chain (unlike walk_forward's
# accumulation). Reachability is a per-field existence predicate here, not a
# graph traversal.
#
# Returns [] when frontmatter is None/absent, or when the fields are all
# absent/none/null — the common case, silent. Returns a list of
# {field, value, reason} violation dicts otherwise.
#
# Fail-open on any resolver error: an individual field's resolution raising
# is treated as "cannot prove unresolvable" (not a violation) — never crash
# or spuriously deny/reject on an infra hiccup.
#
# Mirrors JS checkLineageReachability verbatim.
#
# Spec backlink: DoE-claude:pln-handoff-spinoff-machinery-robu-0d0f15 § C2, § C6 (GAP1/GAP2), F5
# ---------------------------------------------------------------------------

def check_lineage_reachability(
    frontmatter: Optional[dict],
    repo_root: str,
    handoff_dir: Optional[str] = None,
    record_repo_rel_path: Optional[str] = None,
    git_history_cache: Optional[Set[str]] = None,
) -> List[Dict[str, str]]:
    """Check predecessor/forked_from/additional_predecessors[]/origin_handoff
    reachability for a single handoff record's frontmatter.

    Args:
        frontmatter: Parsed frontmatter for the handoff record.
        repo_root:   Absolute repo root.
        handoff_dir: Absolute dir the record's own relative path fields resolve
            against (defaults to <repo_root>/state/handoffs — the write-time
            hook's convention). A batch sweep should pass the record's own
            directory (state/handoffs/ OR archive/handoffs/<...>).
        record_repo_rel_path: The record's OWN repo-relative path (forward-slash
            form), used ONLY to key WAIVED_DANGLING_PREDECESSORS (C6 GAP2).
            Absent at write-time (a not-yet-written record can never be in the
            waive-list); a batch sweep should pass it.
        git_history_cache: Optional, threaded straight through to
            resolve_target's tier-3 check. Absent → per-call `git log --all`
            resolution. A batch sweep primes one via build_git_history_cache()
            once and passes it to every record's check_lineage_reachability call.

    Returns:
        List of {'field': str, 'value': str, 'reason': str} violation dicts.
    """
    if not frontmatter:
        return []
    directory = handoff_dir or os.path.join(repo_root, 'state', 'handoffs')
    waived_target = (
        WAIVED_DANGLING_PREDECESSORS.get(record_repo_rel_path)
        if record_repo_rel_path else None
    )
    violations: List[Dict[str, str]] = []

    def check_field(field: str, raw_value: Any) -> None:
        if raw_value is None:
            return
        value = str(raw_value).strip()
        if not value or value in ('none', 'null'):
            return
        if waived_target is not None and value == waived_target:
            return  # C6 GAP2 explicit waiver
        try:
            resolved = resolve_target(value, directory, repo_root, git_history_cache)
        except Exception:
            # Resolver raised — fail-open (cannot prove unresolvable).
            return
        if resolved is None:
            violations.append({
                'field': field,
                'value': value,
                'reason': 'unresolvable in live ∪ archive-on-disk ∪ git-history (provably never-existed)',
            })
        # resolved is an absolute path or the 'git-history' sentinel → both OK, no violation.

    kind = frontmatter.get('kind')
    if kind != 'recovery':
        check_field('predecessor', frontmatter.get('predecessor'))

    check_field('forked_from', frontmatter.get('forked_from'))
    check_field('origin_handoff', frontmatter.get('origin_handoff'))

    additional = frontmatter.get('additional_predecessors')
    if isinstance(additional, list):
        for idx, entry in enumerate(additional):
            check_field(f'additional_predecessors[{idx}]', entry)

    return violations


# ---------------------------------------------------------------------------
# CLI — thin shell interface for bash consumers
#
# CLI usage:
#   python3 -m coordinator_core.dag --start <path> [--edge-kinds a,b,c]
#     [--format paths|json] [--handoff-dir <dir>]
#   python3 -m coordinator_core.dag --reverse-membership <target>
#     --live-set-json <json-array> [--edge-kinds a,b,c] [--format paths|json]
#     [--exclude <path>]...
#
# Forward mode --format paths output contract (for bash consumers, no jq
# required):
#   <absolute-path-1>
#   <absolute-path-2>
#   ...
#   terminatedEarly=<value>
#
# Mirrors JS _runCli verbatim.
# ---------------------------------------------------------------------------

def _run_cli(argv: List[str]) -> int:
    import json
    import sys

    args = argv[1:]
    start_path: Optional[str] = None
    edge_kind_arg: Optional[str] = None
    fmt = 'paths'
    handoff_dir_arg: Optional[str] = None
    reverse_target: Optional[str] = None
    live_set_json: Optional[str] = None
    exclude_paths: List[str] = []

    i = 0
    while i < len(args):
        if args[i] == '--start' and i + 1 < len(args):
            i += 1
            start_path = args[i]
        elif args[i] == '--edge-kinds' and i + 1 < len(args):
            i += 1
            edge_kind_arg = args[i]
        elif args[i] == '--format' and i + 1 < len(args):
            i += 1
            fmt = args[i]
        elif args[i] == '--handoff-dir' and i + 1 < len(args):
            i += 1
            handoff_dir_arg = args[i]
        elif args[i] == '--reverse-membership' and i + 1 < len(args):
            i += 1
            reverse_target = args[i]
        elif args[i] == '--live-set-json' and i + 1 < len(args):
            i += 1
            live_set_json = args[i]
        elif args[i] == '--exclude' and i + 1 < len(args):
            i += 1
            exclude_paths.append(args[i])
        i += 1

    edge_kinds = set(
        s.strip() for s in edge_kind_arg.split(',') if s.strip()
    ) if edge_kind_arg else {'predecessor'}

    if reverse_target is not None:
        try:
            live_set = json.loads(live_set_json) if live_set_json else []
        except Exception as e:
            sys.stderr.write(f'walk-handoff-dag: failed to parse --live-set-json: {e}\n')
            return 1
        rev_kwargs: Dict[str, Any] = {'edge_kinds': edge_kinds}
        if handoff_dir_arg:
            rev_kwargs['handoff_dir'] = handoff_dir_arg
        if exclude_paths:
            rev_kwargs['exclude'] = exclude_paths
        rev_result = referenced_by(reverse_target, live_set, **rev_kwargs)
        if fmt == 'json':
            sys.stdout.write(json.dumps(rev_result, indent=2) + '\n')
        else:
            for p in rev_result['referencedBy']:
                sys.stdout.write(p + '\n')
            sys.stdout.write('referenced=' + ('true' if rev_result['referenced'] else 'false') + '\n')
        return 0

    if not start_path:
        sys.stderr.write('walk-handoff-dag: --start <path> is required\n')
        return 1

    fwd_kwargs: Dict[str, Any] = {'edge_kinds': edge_kinds}
    if handoff_dir_arg:
        fwd_kwargs['handoff_dir'] = handoff_dir_arg

    result = walk_forward(start_path, **fwd_kwargs)

    if fmt == 'json':
        sys.stdout.write(json.dumps(result, indent=2) + '\n')
    else:
        for p in result['orderedPaths']:
            sys.stdout.write(p + '\n')
        sys.stdout.write('terminatedEarly=' + result['terminatedEarly'] + '\n')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_run_cli(sys.argv))
