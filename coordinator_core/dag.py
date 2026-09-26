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

# spawns on Windows. 0 (no-op) on POSIX where CREATE_NO_WINDOW doesn't exist.
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


EDGE_KIND_META: Dict[str, Dict[str, Any]] = {
    'predecessor':             {'field': 'predecessor',             'multi': False},
    'additional_predecessors': {'field': 'additional_predecessors', 'multi': True},
    'forked_from':             {'field': 'forked_from',             'multi': False},
    'origin_handoff':          {'field': 'origin_handoff',          'multi': False},
}

# Two default edge sets answer two DIFFERENT questions, and conflating them is
#   ARCHIVAL_EDGE_KINDS asks "WHAT POINTS AT this node?" — all three lineage
#   kinds, INCLUDING `forked_from`. This is `referenced_by`'s own default
#   has-children mean SUPERSEDE, stating outright that an archived successor
#   The SET IS UNCHANGED and the name is kept: it is a vocabulary primitive
#   CONTINUATION_EDGE_KINDS asks "MAY THIS WORKSTREAM CONCLUDE?" / "does a

#: The ARCHIVAL default — "what points at this node?" All three lineage edge
ARCHIVAL_EDGE_KINDS: FrozenSet[str] = frozenset(
    {'predecessor', 'additional_predecessors', 'forked_from'}
)

#: The CONTINUATION default — "may this workstream conclude?" / "does a
CONTINUATION_EDGE_KINDS: FrozenSet[str] = frozenset(
    {'predecessor', 'additional_predecessors'}
)

# Deliberately NOT folded into EDGE_KIND_META itself. NOTE: test_dag_edge_kinds.py
# such as `blocked_by` must not be added to EDGE_KIND_META:
# This sits *beside* EDGE_KIND_META as a pure addition, so neither the default
# sets in handoff_children.py / archival.py nor EDGE_KIND_META's own shape change.
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

# Bounded at _MAX_FRONTMATTER_CACHE entries; oldest half evicted on overflow.
# Separation from cache._REVALIDATED_CACHE is intentional.
# _FRONTMATTER_CACHE is dag-local for independent clearability in tests and dedicated
# eviction footprint. pcore-06/10/11 consumers use cache._REVALIDATED_CACHE; the two

_FRONTMATTER_CACHE: Dict[Tuple[str, str], dict] = {}

# NEGATIVE SPEC: this cap must stay comfortably ABOVE the largest handoff corpus
_MAX_FRONTMATTER_CACHE: int = 4096


def _strip_inline_comment(text: str) -> str:
    in_single = False
    in_double = False
    for i, c in enumerate(text):
        if c == '"' and not in_single:
            in_double = not in_double
        elif c == "'" and not in_double:
            if in_single and i + 1 < len(text) and text[i + 1] == "'":
                continue
            in_single = not in_single
        elif c == '#' and not in_single and not in_double:
            if i > 0 and text[i - 1] in (' ', '\t'):
                if i + 1 >= len(text) or text[i + 1] in (' ', '\t'):
                    return text[:i].rstrip()
    return text


def _parse_scalar(text: str) -> Any:
    text = _strip_inline_comment(text).strip()
    if not text or text in ('null', '~'):
        return None
    if text == 'true':
        return True
    if text == 'false':
        return False
    try:
        as_int = int(text)
        if str(as_int) == text:
            return as_int
    except ValueError:
        pass
    try:
        as_float = float(text)
        if math.isfinite(as_float):
            return as_float
    except ValueError:
        pass
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _parse_inline_list(text: str) -> List[Any]:
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

    item = ''.join(current).strip()
    if item:
        items.append(_parse_scalar(item))

    return items


#: Mirrors schema.js BLOCK_SCALAR_RE (schema.js:42).
_BLOCK_SCALAR_RE = re.compile(r'^([|>])([+-]?[0-9]?|[0-9]?[+-]?)\s*(#.*)?$')


def _consume_block_scalar(lines: List[str], start: int, key_indent: int) -> tuple:
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

    This function previously appended
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


def _closes_quote(value: str, quote: str) -> bool:
    body = value[1:]
    idx = 0
    while idx < len(body):
        if body[idx] != quote:
            idx += 1
            continue
        if quote == "'" and body[idx + 1:idx + 2] == "'":
            idx += 2
            continue
        if quote == '"' and idx and body[idx - 1] == '\\':
            idx += 1
            continue
        return True
    return False


def _parse_yaml_mapping_block(lines: List[str], base_indent: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip()
        stripped_ws = stripped.strip()

        if not stripped_ws or stripped_ws.startswith('#'):
            i += 1
            continue

        indent = len(raw) - len(raw.lstrip())
        if indent < base_indent:
            break

        colon_idx = stripped_ws.find(':')
        if colon_idx == -1:
            i += 1
            continue

        key = stripped_ws[:colon_idx].strip()
        rest = stripped_ws[colon_idx + 1:]
        rest_stripped = rest.strip()

        if not rest_stripped or rest_stripped.startswith('#'):
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
                    nested_lines = lines[next_i:]
                    nested_value = _parse_yaml_block(nested_lines, next_indent)
                    result[key] = nested_value
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
            block_value, next_i = _consume_block_scalar(lines, i + 1, indent)
            result[key] = block_value
            i = next_i
            continue
        else:
            comment_stripped = _strip_inline_comment(rest_stripped)
            if comment_stripped.startswith('[') and comment_stripped.endswith(']'):
                result[key] = _parse_inline_list(comment_stripped)
                i += 1
                continue
            quote = rest_stripped[:1]
            if quote in ('"', "'") and not _closes_quote(rest_stripped, quote):
                folded = [rest_stripped]
                j = i + 1
                while j < len(lines):
                    nxt_s = lines[j].rstrip().strip()
                    if not nxt_s:
                        break
                    folded.append(nxt_s)
                    if _closes_quote(quote + nxt_s, quote):
                        j += 1
                        break
                    j += 1
                result[key] = _parse_scalar(' '.join(folded))
                i = j
                continue
            result[key] = _parse_scalar(rest_stripped)

        i += 1

    return result


def _parse_frontmatter(content: str) -> dict:
    cursor = 0
    while True:
        ws_match = re.match(r'^\s*', content[cursor:])
        ws_len = len(ws_match.group(0)) if ws_match else 0
        after_ws = cursor + ws_len
        if content[after_ws:after_ws + 4] == '<!--':
            close_idx = content.find('-->', after_ws + 4)
            if close_idx == -1:
                return {}
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

    if after_first[:first_newline].strip():
        return {}

    rest = after_first[first_newline + 1:]
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


def _read_meta(file_path: str) -> dict:
    try:
        file_path = os.path.abspath(file_path)
        raw = Path(file_path).read_bytes()
        stamp = hashlib.sha256(raw).hexdigest()
        cache_key = (file_path, stamp)
        if cache_key in _FRONTMATTER_CACHE:
            return _FRONTMATTER_CACHE[cache_key]
        content = raw.decode('utf-8', errors='replace')
        parsed = _parse_frontmatter(content)
        if len(_FRONTMATTER_CACHE) >= _MAX_FRONTMATTER_CACHE:
            evict_keys = list(_FRONTMATTER_CACHE.keys())[: _MAX_FRONTMATTER_CACHE // 2]
            for k in evict_keys:
                del _FRONTMATTER_CACHE[k]
        _FRONTMATTER_CACHE[cache_key] = parsed
        return parsed
    except Exception:
        return {}


def read_handoff_meta(file_path: str) -> dict:
    return _read_meta(file_path)


# Process-lifetime cache, same bounded-eviction idiom as _FRONTMATTER_CACHE
# CORRECTNESS HAZARD — git history is not immutable mid-sweep: boot_sweep's

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
        sys.stderr.write(
            f'dag: git-history check failed for {repo_rel_path!r}: {e}\n'
        )
        result = False

    if len(_EVER_TRACKED_CACHE) >= _MAX_EVER_TRACKED_CACHE:
        # _FRONTMATTER_CACHE's eviction idiom above.
        evict_keys = list(_EVER_TRACKED_CACHE.keys())[: _MAX_EVER_TRACKED_CACHE // 2]
        for k in evict_keys:
            del _EVER_TRACKED_CACHE[k]
    _EVER_TRACKED_CACHE[cache_key] = result
    return result


# as an OPTIONAL param — absent (None), both fall back to the original
# MEASURED numbers (DoE-claude corpus, one full envelope.emit() run, fresh
#     SEPARATE, unrelated `git merge-base --is-ancestor` hot spot, tracked
#     PYTHONHASHSEED pinned) measured fallback spawns 313 -> 308 (-5) on
#     DoE-claude and 39 -> 38 (-1) on claude-klabauter's own corpus. CORRECTION
#     priority_resolve.py's NEGATIVE-SPEC block). Verified independently: the
#   - FRESH PROCESS ONLY: _EVER_TRACKED_CACHE (below) is process-lifetime, so
#     _EVER_TRACKED_CACHE and bump the generation) between "before" and
# miss against a COMPLETE cache stops being "unknown" and becomes "provably

class GitHistoryCache(set):

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
        if promisor.returncode == 0 and promisor.stdout.strip().lower() == 'true':
            return False
    except Exception:
        return False

    return True


def build_git_history_cache(repo_root: str, timeout_s: float = 15.0) -> Optional[GitHistoryCache]:
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
        sys.stderr.write(f'dag: build_git_history_cache failed: {e}\n')
        return None


def _memoized_ever_tracked(
    repo_rel_path: str,
    memo: Dict[str, bool],
    repo_root: str,
    git_history_cache: Optional[Set[str]],
) -> bool:
    key = repo_rel_path.replace('\\', '/')
    if key in memo:
        return memo[key]
    if git_history_cache is not None and key in git_history_cache:
        result = True
    elif git_history_cache is not None and getattr(git_history_cache, 'complete', False):
        result = False
    else:
        result = _git_path_ever_tracked(key, repo_root)
    memo[key] = result
    return result


_BATON_FAMILY_DIRS: Tuple[str, ...] = ('state/handoffs', 'archive/handoffs')


def _ref_names_foreign_family(ref: str) -> bool:
    ref_dir = os.path.dirname(os.path.normpath(str(ref)).replace('\\', '/')).strip('/')
    if not ref_dir:
        return False
    return not any(
        ref_dir == family or ref_dir.startswith(family + '/')
        for family in _BATON_FAMILY_DIRS
    )


#: Anchored full-string match so a ref that merely CONTAINS hex (a filename
_SHA_SHAPED_REF_RE = re.compile(r'^[0-9a-f]{7,40}$')


def resolve_target(
    ref: Any,
    handoff_dir: str,
    repo_root: str,
    git_history_cache: Optional[Set[str]] = None,
    id_index: Optional[Union['_LazyHandoffIdIndex', Dict[str, str]]] = None,
    *,
    include_history_tier: bool = True,
) -> Optional[str]:
    if ref is None:
        return None
    target = str(ref).strip()
    if not target or target in ('none', 'null'):
        return None

    # spawns, not the scan. A ref that merely CONTAINS hex (e.g. a filename)
    if _SHA_SHAPED_REF_RE.match(target):
        return None

    if id_index and not target.endswith('.md') and target in id_index:
        return id_index[target]

    _tier3_memo: Dict[str, bool] = {}

    def ever_tracked(repo_rel_path: str) -> bool:
        return _memoized_ever_tracked(repo_rel_path, _tier3_memo, repo_root, git_history_cache)

    if os.path.isabs(target):
        if os.path.exists(target):
            return target
        if repo_root and include_history_tier:
            norm_root = repo_root.rstrip('/\\')
            if target.startswith(norm_root):
                rel = target[len(norm_root):].lstrip('/\\')
                if ever_tracked(rel):
                    return 'git-history'
        return None

    basename = os.path.basename(target)
    candidates = [
        os.path.normpath(os.path.join(handoff_dir, target)),
        os.path.normpath(os.path.join(repo_root, target)),
    ]
    # Basename recovery (the tiers below) is STALE-PATH recovery within the baton
    if not _ref_names_foreign_family(target):
        candidates.extend([
            os.path.normpath(os.path.join(repo_root, 'state', 'handoffs', basename)),
            os.path.normpath(os.path.join(repo_root, 'archive', 'handoffs', target)),
            os.path.normpath(os.path.join(repo_root, 'archive', 'handoffs', basename)),
        ])

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    archive_dir = os.path.normpath(os.path.join(repo_root, 'archive', 'handoffs'))
    if os.path.isdir(archive_dir) and not _ref_names_foreign_family(target):
        try:
            for entry in os.listdir(archive_dir):
                if re.match(r'^\d{4}-\d{2}$', entry):
                    candidate = os.path.join(archive_dir, entry, basename)
                    if os.path.exists(candidate):
                        return candidate
        except OSError:
            pass

    if repo_root and include_history_tier:
        if ever_tracked(target):
            return 'git-history'
        norm_root_rel = repo_root.rstrip('/\\')
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


_resolve_target = resolve_target


def build_handoff_id_index(
    paths: List[str], metas: Optional[Dict[str, dict]] = None
) -> Dict[str, str]:
    index: Dict[str, str] = {}
    for p in paths:
        if metas is not None:
            meta = metas.get(os.path.abspath(p))
            if meta is None:
                meta = _read_meta(p)
            hid = meta.get('handoff_id') if meta else None
        else:
            hid = _raw_scan_handoff_id(p)
            if hid is _RAW_SCAN_FALLBACK:
                meta = _read_meta(p)
                hid = meta.get('handoff_id') if meta else None
        if hid is None:
            continue
        hid_str = str(hid).strip()
        if hid_str:
            index[hid_str] = os.path.abspath(p)
    return index


_RAW_SCAN_FALLBACK = object()

#: start of a line (MULTILINE) — mirrors `_parse_frontmatter`'s own
#: `re.search(r'^---\s*$', ..., re.MULTILINE)` closing-terminator match.
_RAW_SCAN_TERMINATOR_RE = re.compile(r'^---\s*$', re.MULTILINE)

_RAW_SCAN_HANDOFF_ID_RE = re.compile(r'^handoff_id:[ \t]*(\S.*?)[ \t]*$', re.MULTILINE)

_RAW_SCAN_WINDOW_BYTES = 4096


def _raw_scan_handoff_id(file_path: str) -> Any:
    """Best-effort `handoff_id` extraction from the first 4KB of *file_path*
    via a terminator-anchored regex, instead of a full YAML parse.

    Returns the found id (a possibly-empty string, or None if the key is
    absent-but-the-window-was-sufficient), or the `_RAW_SCAN_FALLBACK`
    sentinel when the window cannot answer the question at all:
      - the closing `---` frontmatter terminator does not appear inside the
        4KB window (file's frontmatter may extend past it), or
      - a `handoff_id:` line is found but its value is quoted, folded, or
        otherwise not a bare unambiguous scalar the regex can read.

    The scan locates the closing terminator FIRST and searches only the
    bytes BEFORE it — this is what keeps a `handoff_id:` line appearing in
    the file's BODY (prose, a fenced code block, past the closing `---`)
    from being picked up: the body is never in the searched span at all.

    Skips an optional leading HTML-comment prologue before the opening
    `---`, same as `_parse_frontmatter` and `plan_gate._skip_leading_
    comments` — a banner record read from byte 0 without this skip would
    read as having no frontmatter at all and fall back silently (Review
    2026-09-12: 0/194 live records carry one today, so this is
    future-proofing, not a live break).
    """
    try:
        with open(file_path, 'rb') as fh:
            raw = fh.read(_RAW_SCAN_WINDOW_BYTES)
    except OSError:
        return _RAW_SCAN_FALLBACK

    try:
        text = raw.decode('utf-8', errors='replace')
    except Exception:
        return _RAW_SCAN_FALLBACK
    # (common in this corpus) leaves a trailing '\r' inside the MULTILINE
    # value regexes -- '\r' is not in `[ \t]`, so `_RAW_SCAN_HANDOFF_ID_RE`'s
    text = text.replace('\r\n', '\n')

    cursor = 0
    while True:
        ws_match = re.match(r'^\s*', text[cursor:])
        ws_len = len(ws_match.group(0)) if ws_match else 0
        after_ws = cursor + ws_len
        if text[after_ws:after_ws + 4] == '<!--':
            close_idx = text.find('-->', after_ws + 4)
            if close_idx == -1:
                return _RAW_SCAN_FALLBACK
            cursor = close_idx + 3
        else:
            cursor = after_ws
            break
    text = text[cursor:]

    if not text.startswith('---'):
        return _RAW_SCAN_FALLBACK
    first_newline = text.find('\n')
    if first_newline == -1:
        return _RAW_SCAN_FALLBACK
    term_match = _RAW_SCAN_TERMINATOR_RE.search(text, first_newline + 1)
    if term_match is None:
        return _RAW_SCAN_FALLBACK

    header = text[:term_match.start()]
    id_match = _RAW_SCAN_HANDOFF_ID_RE.search(header)
    if id_match is None:
        return None

    value = id_match.group(1)
    if not value or value[0] in ('"', "'", '|', '>') or '#' in value:
        return _RAW_SCAN_FALLBACK

    return value


def _scan_handoff_corpus_paths(repo_root: str) -> List[str]:
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
    return _scan_handoff_corpus_paths(repo_root)


def _repo_root_from_handoff_dir(handoff_dir: str) -> str:
    return os.path.normpath(os.path.join(handoff_dir, '..', '..'))


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
            continue
        field_names = [kind_meta['field']] + list(EDGE_KIND_FIELD_ALIASES.get(kind, ()))
        for field_name in field_names:
            val = node_meta.get(field_name)
            if val is None:
                continue
            if kind_meta['multi']:
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


class _LazyHandoffIdIndex:

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
    *,
    include_history_tier: bool = True,
) -> Dict[str, Any]:
    if edge_kinds is None:
        edge_kinds = {'predecessor'}
    if node_gate is None:
        node_gate = lambda meta: True  # noqa: E731

    abs_start = os.path.abspath(start_path)

    if handoff_dir is None:
        handoff_dir = os.path.dirname(abs_start)
    if repo_root is None:
        repo_root = _repo_root_from_handoff_dir(handoff_dir)

    # (EDGE_KIND_FIELD_ALIASES) — the common walk_forward({'predecessor'})
    id_index: Optional[Union['_LazyHandoffIdIndex', Dict[str, str]]] = None
    if repo_root and any(EDGE_KIND_FIELD_ALIASES.get(k) for k in edge_kinds):
        id_index = _LazyHandoffIdIndex(repo_root)

    nodes: Dict[str, dict] = {}
    ordered_paths: List[str] = []
    terminated_early = ''

    gray_set: Set[str] = set()
    black_set: Set[str] = set()

    dfs_stack = [{'path': abs_start, 'phase': 'enter'}]

    while dfs_stack:
        frame = dfs_stack.pop()
        abs_path = frame['path']

        if frame['phase'] == 'exit':
            gray_set.discard(abs_path)
            black_set.add(abs_path)
            continue


        if abs_path in black_set:
            continue

        if abs_path in gray_set:
            terminated_early = 'lineage-cycle'
            continue

        meta = _read_meta(abs_path)

        if not node_gate(meta):
            black_set.add(abs_path)
            continue

        gray_set.add(abs_path)

        nodes[abs_path] = meta
        ordered_paths.append(abs_path)

        dfs_stack.append({'path': abs_path, 'phase': 'exit'})

        raw_edges = handoff_edges(meta, edge_kinds)

        edges_to_push: List[str] = []
        for raw_ref in raw_edges:
            target_abs = resolve_target(
                raw_ref, handoff_dir, repo_root, id_index=id_index,
                include_history_tier=include_history_tier,
            )
            if target_abs is None:
                terminated_early = 'missing-link'
                continue
            if target_abs == 'git-history':
                continue
            edges_to_push.append(target_abs)

        for edge_path in reversed(edges_to_push):
            dfs_stack.append({'path': edge_path, 'phase': 'enter'})

    return {
        'nodes': nodes,
        'orderedPaths': ordered_paths,
        'terminatedEarly': terminated_early,
    }


def build_reverse_edge_index(
    live_set: List[str],
    handoff_dir: Optional[str] = None,
    id_index_source: Optional[List[str]] = None,
    metas: Optional[Dict[str, dict]] = None,
    edge_kinds: Optional[Set[str]] = None,
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
    # COVERAGE, not a default to be overridden lightly. Whatever is indexed
    # Found 2026-09-01: `origin_handoff` is not in ARCHIVAL_EDGE_KINDS, so a
    all_kinds = set(ARCHIVAL_EDGE_KINDS) if edge_kinds is None else set(edge_kinds)

    id_index: Optional[Dict[str, str]] = None
    if any(EDGE_KIND_FIELD_ALIASES.get(k) for k in all_kinds):
        id_index = build_handoff_id_index(
            id_index_source if id_index_source is not None else live_set,
            metas=metas,
        )

    by_abspath: Dict[str, List[Tuple[str, str]]] = {}
    by_basename: Dict[str, List[Tuple[str, str]]] = {}

    # NEGATIVE SPEC — do not "parallelise the reads". Tried and reverted
    for node_abs_path in live_set:
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
        'edge_kinds': frozenset(all_kinds),
    }


def referenced_by_indexed(
    target: str,
    index: Dict[str, Any],
    edge_kinds: Optional[Set[str]] = None,
    exclude: Optional[Union[List[str], Set[str], FrozenSet[str]]] = None,
) -> Dict[str, Any]:
    if edge_kinds is None:
        edge_kinds = set(ARCHIVAL_EDGE_KINDS)

    # such an index carries exactly ARCHIVAL_EDGE_KINDS by construction.
    covered = index.get('edge_kinds')
    if covered is None:
        covered = frozenset(ARCHIVAL_EDGE_KINDS)
    missing = set(edge_kinds) - set(covered)
    if missing:
        raise ValueError(
            "referenced_by_indexed: index does not cover edge kind(s) "
            f"{sorted(missing)} (covers {sorted(covered)}). Rebuild with "
            "build_reverse_edge_index(..., edge_kinds=<the kinds you will "
            "ask for>). Answering from this index would report no "
            "referencers, which is not the same as there being none."
        )

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

    exclude_set: Set[str] = set()
    if exclude:
        exclude_iter = exclude if isinstance(exclude, (set, frozenset)) else exclude
        for ex in exclude_iter:
            if ex:
                exclude_set.add(os.path.abspath(str(ex)))

    if exclude_set:
        filtered_live_set = [p for p in live_set if os.path.abspath(p) not in exclude_set]
    else:
        filtered_live_set = live_set

    abs_target = os.path.abspath(target)
    target_basename = os.path.basename(abs_target)

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
            resolved_ref = resolve_target(
                raw_ref,
                node_handoff_dir,
                repo_root,
                id_index=id_index,
                include_history_tier=False,
            )
            if resolved_ref is None or resolved_ref == 'git-history':
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


# claude-klabauter by the 2026-07-03 relocation. That reclaim brought in each SUCCESSOR
# Mirrors JS WAIVED_DANGLING_PREDECESSORS verbatim (same keys/values).
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


# batch corpus sweep apply the IDENTICAL reachability rule — not just the same
# SAME-REPO-ONLY FOREIGN-BATON CARVE-OUT: there is no per-record repo-identity

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
            return
        try:
            resolved = resolve_target(value, directory, repo_root, git_history_cache)
        except Exception:
            return
        if resolved is None:
            violations.append({
                'field': field,
                'value': value,
                'reason': 'unresolvable in live ∪ archive-on-disk ∪ git-history (provably never-existed)',
            })

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
