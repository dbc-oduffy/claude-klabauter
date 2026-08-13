"""
coordinator_core.ops.completion_ops — in-place mutators for completion-entry commits
reconciliation and plan session appending.

Purpose: Port of two completion/plan ceremony writers:
  completion.reconcile_commits ← reconcile-completion-commits.sh (coordinator-claude 432e3285, 2026-07-22)
                                  (--append mode write logic)
  plan.append_session          ← append-plan-session.sh (coordinator-claude 2737ca3d, 2026-07-19)

Each mutates a caller-supplied entry in-place:
  - ``completion.reconcile_commits``: folds missing session SHAs into a completion entry's
    ``commits:`` YAML list (idempotent: already-present SHAs are skipped). The entry lives
    under ``archive/completed/**/*.md`` (empirical production noun — 61 confirmed
    ``status: pending-release`` entries, 0 under ``docs/plans/``) or ``docs/plans/``
    (DR-216's originally-stated noun, kept as a conservative superset); the guard permits both.
  - ``plan.append_session``: appends a session-tracking entry to the plan's
    ``agent_sessions:`` YAML list (idempotent: same session_id → no-op). Confined to
    ``docs/plans/<plan>.md`` (correct and confirmed noun for this op).
  (Review: code-reviewer — Finding 8, module docstring flatly asserted docs/plans/ for both
  ops without qualifying reconcile_commits' real production shape.)

Content-additive in-place ONLY (DR-216 D2(iii)): NEVER rewrites, reorders, or deletes
existing plan content. Read-modify-write to a temp file + atomic ``os.replace`` (DR-216 D3).

Byte-parity targets (``--append`` mode write logic only):
  completion.reconcile_commits:
    ``reconcile-completion-commits.sh`` (coordinator-claude 432e3285, 2026-07-22, awk write pass)
  plan.append_session:
    ``append-plan-session.sh`` (coordinator-claude 2737ca3d, 2026-07-19, mapfile/printf write pass)

MUTATING ops (DR-208 §2): write coordinator substrate directly — ``archive/completed/**/*.md``
or ``docs/plans/*.md`` completion sections (see per-op noun table above). NEVER writes
``state/handoffs/``, ``archive/handoffs/``, ``state/`` queue subdirs, rag's relational
store, or any path outside the caller-supplied plan file (DR-216 D2(iv)/D4 noun
confinement). No git commit from handlers (DR-216 D2(v)). Blocking FS I/O wrapped via
``asyncio.to_thread`` (DR-216 D3).

Caller ``repo_root`` threading: handler third arg receives ``git_common_dir(caller_worktree)``
via ``_OP_KEY_SCOPE: common_dir`` (ipc.py). Plan path is always a caller-supplied absolute
path parameter — the op does NOT derive it from ``repo_root`` (``plan_path`` is DR-216's noun,
not ``state/``). Both handlers enforce the empty-path guard (DR-216 D2(iv) minimum) AND a
containment guard (op-family path-containment sweep, 2026-07-08): the resolved ``plan_path``
must be under one of the op's allowed roots (``completion.reconcile_commits`` allows
``main_worktree_root(repo_root) / "archive" / "completed"`` and
``main_worktree_root(repo_root) / "docs" / "plans"``; ``plan.append_session`` allows only
``main_worktree_root(repo_root) / "docs" / "plans"``), mirroring ``handoff.has_live_children``'s
dual-root ``state/handoffs/`` + ``archive/handoffs/`` allow-list. ``repo_root is None`` is
rejected up front (same shape as ``handoff.transition``/``handoff.stamp``) since containment
requires a derivable worktree root.

Negative-spec (completion.reconcile_commits):
  - Pre-computed-SHA-list call path (``commits`` given, ``session_id`` omitted): does NOT
    run ``git log``, ``git merge-base``, or any other git command — the caller (facade or
    test) pre-computes the SHA list and passes it as ``commits``. Idempotency uses string
    equality (no short/full SHA normalization) on this path. Preserved verbatim for
    backward compat — every existing caller (bash facade in Zone-A mode, existing tests)
    is unaffected by the extension below.
  - ``status: pending-release`` guard is a caller obligation (facade checks before invoking).

REOPENED (session_id call path — DR-216 boundary reopened by PM 2026-07-19; supersedes the
  git-resolution carve-out above for this path only):
  When ``session_id`` is given, the op now owns git-resolution internally, composing
  ``coordinator_core.reconcile.commit_reality``'s git subprocess pattern (``_git``) rather
  than reinventing it. Byte-parity oracle for this path is the git-log/merge-base/chain-walk/
  canonicalization portion of ``reconcile-completion-commits.sh`` (Zone A, pre-append-mode-fork):
    - merge-base resolution against ``origin/main`` (unresolvable -> non-blocking no-op,
      mirrors the oracle's ``merge-base unresolved — reconcile skipped`` exit-0 path).
    - chain-slug expansion: ``chain_slug`` explicit param, else derived from the entry's own
      ``chain:`` frontmatter field (oracle Step 3.5) — walks ``archive/completed/**/*.md``
      for sibling ``authored_by:`` values sharing the same chain, and ``state/handoffs/`` +
      ``archive/handoffs/`` for ``claimed_by:`` (fallback ``consumed_by:``) values on handoffs
      sharing ``workstream: <slug>`` (authored_by UNION claimed_by, always seeded with the passed
      ``session_id`` — oracle Axis 1 chain-widening).
    - multi-session ``git log --grep=^Session-Id: <sid>$`` collection across every id in the
      widened chain set (oracle Step 4).
    - SHA canonicalization via ``git rev-parse --verify <sha>^{commit}`` for the entry's
      already-stored short SHAs, so delta computation matches on full-SHA equality — no
      short/ambiguous-SHA leakage (oracle Step 5-ish).
    - id-provenance-mismatch probe (oracle Step 6): when the widened chain set produces zero
      matching commits in range but the range DOES contain OTHER ``Session-Id:`` trailered
      commits, a ``provenance_warning`` string is returned (never raised, never blocks) —
      preserved exactly, oracle's ambiguity-discriminating diagnostic.
  ``worktree_root`` (repo root; derived by the handler via ``main_worktree_root(repo_root)``,
  where ``repo_root`` is the dispatch engine's common_dir-keyed resolution of the caller's
  ``_origin_worktree`` envelope field — never read directly out of ``params``, so a JSON-RPC
  caller cannot smuggle an arbitrary path in via the method's own parameter list; there is no
  UDS socket in the current in-process command-type model, retired by DR-215) is required for
  this path. (Review: code-reviewer — Finding 4: "socket-authoritative... never
  caller-suppliable" was stale post-DR-215 and overclaimed the trust boundary — the caller
  DOES influence which repo via ``_origin_worktree``, within common_dir containment.)
  ``session_id`` MUST pass the oracle's allowlist regex
  (``^[a-zA-Z0-9][a-zA-Z0-9_-]*$``, and not the literal ``"null"``) — ValueError otherwise,
  mirroring the oracle's exit-1 caller guard.
  Still does NOT: acquire a lock (in-process, serial-by-construction, DR-215), run
  ``git commit``/any mutating git verb, or read the ``status:`` guard (still a caller
  obligation).
  ACCEPTED RISK (Review: code-reviewer — F6): ``_strip_chain_date_prefix``'s date-prefix
  normalization (see that function's docstring) can conflate two genuinely unrelated chains
  that happen to share a base slug on different days (e.g. ``2026-07-20-cleanup`` and
  ``2026-07-26-cleanup``). This is a deliberate acceptance, not an oversight: the fix this
  normalization targets is a ``chain:`` (always date-prefixed) vs ``workstream:`` (never
  date-prefixed) mismatch, where the two sides frequently carry NO common date to require
  matching against — requiring same-date-on-both-sides would silently narrow the widening
  back below the literal-match floor and reintroduce the exact bug this normalization fixes.
  Disambiguating further would require inferring similarity beyond the declared
  ``chain_aliases:`` set, which this module's widening invariant (never match on inferred
  shape, only on literal/normalized/declared-alias equality) forbids. If same-slug
  cross-day collision becomes a real (not merely plausible) problem, the fix is a workflow
  one — give genuinely-unrelated same-slug work distinguishable slugs — not a code one.

Negative-spec (plan.append_session):
  - Does NOT acquire a file lock (in-process command-type, serial-by-construction, DR-215).
  - Session_id MUST be passed by caller; the op does not resolve from env/sentinel.
  - ``created_at`` is accepted as a parameter or generated at call time; NOT the oracle's
    ``_cs_now_iso`` side-channel.

Registered as ``completion.reconcile_commits`` and ``plan.append_session`` in
``ops/__init__.py`` (C3). Classified ``OpClass.MUTATING`` in ``authz/classification.py`` (C3).

Spec backlink: pln-strang-10-residual-writer-clus-b67ff8 § C2
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.ipc import register_op
from coordinator_core.machine_resolver import load_flat_registry_file, registry_dir
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.reconcile.commit_reality import _git as _reality_git

logger = logging.getLogger(__name__)

#: Session-id / chain-slug allowlist — byte-parity oracle:
#: reconcile-completion-commits.sh's ``_ID_ALLOWLIST_RE`` (prevents ERE-injection
#: into ``git log --grep``). The literal string "null" passes this regex but is
#: explicitly rejected by callers (oracle Step 3 comment) — see ``_validate_id``.
_ID_ALLOWLIST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


# ---------------------------------------------------------------------------
# completion.reconcile_commits — fold missing SHAs into commits: list
# ---------------------------------------------------------------------------
# Byte-parity oracle: reconcile-completion-commits.sh --append mode awk write pass.
# ---------------------------------------------------------------------------


#: Inline (flow-style) ``commits:`` value — ``commits: []``, ``commits: ["a", "b"]``,
#: ``commits: [a, b]``, with an optional trailing ``#`` comment. Group 1 is the raw
#: interior; ``_split_flow_items`` turns it into bare SHA strings.
_COMMITS_FLOW_RE = re.compile(r"^commits:\s*\[(.*)\]\s*(#.*)?$")

#: Block-style ``commits:`` key — the value is empty, items follow as ``  - "<sha>"``
#: lines. A trailing comment is permitted. Deliberately NOT ``^commits:``: that looser
#: form also matches a populated flow list, which is what corrupted entries fleet-wide
#: (example-market-data-repo, 2026-07-22) by appending block items under a flow line.
_COMMITS_BLOCK_RE = re.compile(r"^commits:\s*(#.*)?$")

#: Any ``commits:`` key at all — used only to detect a shape matching neither of the
#: two above, so it can fail loud rather than be silently mis-handled.
_COMMITS_ANY_RE = re.compile(r"^commits:")


def _split_flow_items(interior: str) -> list[str]:
    """Split the interior of a flow-style YAML list into bare, de-quoted item strings.

    ``'"a", "b"'`` → ``["a", "b"]``; ``'a, b'`` → ``["a", "b"]``; ``''`` → ``[]``.
    Order is preserved (load-bearing: the flow→block normalization must not reorder).
    """
    items: list[str] = []
    for raw in interior.split(","):
        val = raw.strip().strip('"').strip("'").strip()
        if val:
            items.append(val)
    return items


def _parse_existing_commits(content: str) -> set[str]:
    """Extract the set of SHA strings already present in the ``commits:`` YAML list.

    Matches the oracle's stored-SHA extraction awk (Step 5):
      - List item detection: ``^[[:space:]]+-[[:space:]]`` (one or more spaces, then ``-``, then space)
      - Value extraction: strip leading ``^[[:space:]]*-[[:space:]]*`` then strip surrounding quotes.
      - Block exit: next top-level alpha key (``^[[:alpha:]]``).

    Returns a set of bare SHA strings (quotes stripped, whitespace stripped).
    """
    shas: set[str] = set()
    fm_n = 0
    in_commits = False

    for line in content.splitlines():
        if line == "---":
            fm_n += 1
            if fm_n == 2:
                break
            continue

        if fm_n != 1:
            continue

        # Inside frontmatter: track commits: block.
        flow = _COMMITS_FLOW_RE.match(line)
        if flow:
            # Flow-style list carries its items on the key line itself; there is no
            # block to enter. Collecting them here is what makes dedup correct against
            # a flow-style entry — without it every stored SHA reads as missing and
            # gets re-appended as a duplicate.
            shas.update(_split_flow_items(flow.group(1)))
            continue

        if _COMMITS_BLOCK_RE.match(line):
            in_commits = True
            continue

        if _COMMITS_ANY_RE.match(line):
            raise ValueError(
                f"unrecognized commits: shape in frontmatter — refusing to parse: {line!r}"
            )

        if in_commits:
            # List item: ^[[:space:]]+-[[:space:]]
            if re.match(r"^\s+-\s", line):
                val = re.sub(r"^\s*-\s*", "", line)
                val = re.sub(r"\s*#.*$", "", val)  # strip trailing inline comment
                val = val.strip().strip('"').strip("'")
                if val:
                    shas.add(val)
            # Next top-level alpha key → exit commits block (^[[:alpha:]])
            elif re.match(r"^[a-zA-Z]", line):
                in_commits = False

    return shas


def _apply_commits_fold(content: str, new_shas: list[str]) -> str:
    """Fold ``new_shas`` into the ``commits:`` YAML list (byte-parity port of the oracle awk).

    Matches the awk write pass of ``reconcile-completion-commits.sh --append``:
      - ``commits: [...]`` (inline flow-style, empty or populated): replaced with a
        ``commits:`` key + block SHA list items — existing elements first, in their
        original order, then the new SHAs.
      - ``commits:`` (multi-line): existing items buffered and reprinted; new SHAs appended
        immediately after the last existing item (when the commits block exits or frontmatter
        closes with the second ``---``).
      - SHA line format: ``  - "<sha>"`` (2-space indent, double-quoted) matching oracle's
        ``printf '  - "%s"\\n' "${sha}"``.
      - Lines outside the commits block are passed through unchanged.

    Negative-spec:
      - Does NOT reorder, rewrite, or delete existing content (DR-216 D2(iii)).
      - Empty ``new_shas`` → returns ``content`` unchanged (no write).
      - Flow-style ``commits: [...]`` → block-form rewrite is the ONE permitted byte-level
        substitution. DR-216 D2(iii) prohibits *reordering, rewriting, or deleting existing
        content*; a flow→block normalization emits the same elements, in the same order,
        with no element added or dropped — it changes YAML syntax, not content, so it is
        outside what D2(iii) protects. The empty case (``commits: []``) was always carved
        out on this reasoning as semantically additive; the populated case is carved out on
        the stronger ground that the alternative is emitting *invalid YAML*. Treating a
        populated flow list as a block key (the pre-2026-07-22 ``^commits:`` match) appended
        block items beneath a flow line, producing frontmatter ``yaml.safe_load`` rejects —
        which ``query-completions`` then dropped silently. Preserving byte-form is not worth
        corrupting the entry. Source: cross-repo memo 2026-07-22 (example-market-data-repo-em).
      - Any ``commits:`` shape matching neither flow nor block form raises ``ValueError``
        rather than being passed through. Loud failure on an unknown shape is strictly
        better than the silent-corruption blast radius above.
    """
    if not new_shas:
        return content

    # Format new SHA lines exactly as the oracle: printf '  - "%s"\n' "${sha}"
    # Oracle awk stores them as $0 (no trailing newline); print new_lines[i] adds \n.
    # Python equivalent: store without newline, join via '\n' + trailing '\n'.
    new_sha_lines = [f'  - "{sha}"' for sha in new_shas]

    lines = content.splitlines(keepends=False)
    result_lines: list[str] = []
    fm_n = 0
    in_commits = False
    commits_buf: list[str] = []  # buffer existing commits: list items (without trailing \n)

    for line in lines:
        if line == "---":
            fm_n += 1
            if fm_n == 2 and in_commits:
                # Flush buffered existing entries, then new SHAs, then the closing fence.
                # Mirrors awk: printf "%s", commits_buf (already has \n each); print new_lines[i].
                result_lines.extend(commits_buf)
                result_lines.extend(new_sha_lines)
                commits_buf = []
                in_commits = False
            result_lines.append(line)
            continue

        if fm_n == 1:
            # Inline flow-style list, empty OR populated: commits: [] / commits: ["a","b"]
            # (with optional trailing comment). Normalized to block form, existing items
            # re-emitted first in their original order, then the new SHAs.
            flow = _COMMITS_FLOW_RE.match(line)
            if flow:
                result_lines.append("commits:")
                result_lines.extend(f'  - "{item}"' for item in _split_flow_items(flow.group(1)))
                result_lines.extend(new_sha_lines)
                in_commits = False
                continue

            # commits: key (multi-line form) — value empty, items follow as - lines.
            if _COMMITS_BLOCK_RE.match(line):
                in_commits = True
                result_lines.append(line)
                continue

            # Neither flow nor block: a shape this fold does not understand. Fail loud
            # rather than pass it through and append block items beneath it — silent
            # corruption is the failure mode this guard exists to prevent.
            if _COMMITS_ANY_RE.match(line):
                raise ValueError(
                    f"unrecognized commits: shape in frontmatter — refusing to fold: {line!r}"
                )

            if in_commits:
                # List item: ^[[:space:]]+-[[:space:]]
                if re.match(r"^\s+-\s", line):
                    # Buffer existing entry exactly as-is (oracle: commits_buf = commits_buf $0 "\n").
                    commits_buf.append(line)
                    continue
                else:
                    # Exiting commits block (next frontmatter key or unexpected line).
                    # Oracle: printf "%s", commits_buf (flush buffer); for i print new_lines[i].
                    result_lines.extend(commits_buf)
                    result_lines.extend(new_sha_lines)
                    commits_buf = []
                    in_commits = False
                    result_lines.append(line)
                    continue

        result_lines.append(line)

    # Malformed frontmatter guard: if we're still in_commits at EOF.
    if in_commits:
        result_lines.extend(commits_buf)
        result_lines.extend(new_sha_lines)

    # Reconstruct: '\n'.join (no trailing newline from join) + '\n' (mirrors printf '%s\n' which
    # adds newline after each element, including the final one).
    return "\n".join(result_lines) + "\n"


# ---------------------------------------------------------------------------
# Git-resolution machinery (REOPENED session_id call path) — composes
# commit_reality.py's _git subprocess pattern; byte-parity oracle:
# reconcile-completion-commits.sh Zone A (pre-append-mode-fork).
# ---------------------------------------------------------------------------


def _validate_id(value: str, label: str) -> None:
    """Allowlist-validate a session-id / chain-slug value (oracle Step 3 guard).

    Raises ValueError (mirrors the oracle's exit-1 caller guard) when ``value``
    is empty, the literal string ``"null"`` (passes the regex but is explicitly
    rejected — oracle Step 3 comment), or fails ``_ID_ALLOWLIST_RE``.
    """
    if not value or value == "null":
        raise ValueError(
            f"completion.reconcile_commits: {label} is empty or the literal 'null'"
        )
    if not _ID_ALLOWLIST_RE.match(value):
        raise ValueError(
            f"completion.reconcile_commits: {label} must match "
            f"{_ID_ALLOWLIST_RE.pattern!r}: {value!r}"
        )


def _read_frontmatter_field(content: str, key: str) -> Optional[str]:
    """Read a single top-level frontmatter field's value (first block only).

    Byte-parity port of the oracle's awk field-extraction idiom (used for
    ``chain:``, ``authored_by:``, ``claimed_by:``, ``consumed_by:``, ``workstream:``):
      - Scans only the first ``---``…``---`` frontmatter block.
      - Strips the ``key:`` prefix, trailing inline ``# comment``, and
        surrounding single/double quotes.
      - Returns None when the key is absent or the block is malformed.
    """
    fm_n = 0
    prefix_re = re.compile(rf"^{re.escape(key)}:\s*")
    for line in content.splitlines():
        if line == "---":
            fm_n += 1
            if fm_n == 2:
                return None
            continue
        if fm_n != 1:
            continue
        if prefix_re.match(line):
            val = prefix_re.sub("", line, count=1)
            val = re.sub(r"\s*#.*$", "", val)  # strip trailing inline comment
            val = val.strip().strip('"').strip("'")
            return val
    return None


def _derive_chain_slug(entry_content: str) -> str:
    """Derive the ``chain:`` slug from a completion entry's frontmatter (oracle Step 3.5).

    Normalizes "null"/empty/allowlist-failing values to "" (no-chain), matching the
    oracle's WARN-and-skip behavior on an invalid slug rather than raising.
    """
    slug = _read_frontmatter_field(entry_content, "chain") or ""
    if slug == "null":
        slug = ""
    if slug and not _ID_ALLOWLIST_RE.match(slug):
        logger.warning(
            "completion.reconcile_commits: chain slug %r fails allowlist — "
            "chain resolution skipped",
            slug,
        )
        slug = ""
    return slug


def _iter_files(root: Path) -> List[Path]:
    """Recursively enumerate ``*.md`` files under ``root`` (oracle's ``grep -rl`` scope).

    Returns [] when root doesn't exist — mirrors the oracle's ``[[ -d ]]`` guard.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


#: ``chain:`` slugs are always date-prefixed (``YYYY-MM-DD-<slug>``); sibling
#: ``workstream:`` values on handoffs never carry that prefix — a literal-equality
#: comparison between the two therefore matched nothing (empirically: 0 siblings
#: widened on every 2026-07-26 repair-session entry). Fixed-width and unambiguous,
#: so stripping is strictly invertible.
_CHAIN_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _strip_chain_date_prefix(slug: str) -> str:
    """Strip a leading ``YYYY-MM-DD-`` date prefix from a chain slug (Axis 1 fix).

    No-op when no prefix is present, so applying it to both comparison sides only
    ever widens matches, never narrows — same shape as this module's earlier
    chain-widening normalization (frontmatter-field vs literal-line match, see the
    Finding-2 comment above in ``_collect_chain_session_ids``).

    ACCEPTED RISK (Review: code-reviewer — F6, see module negative-spec for full
    rationale): stripping means two unrelated same-base-slug chains on different days
    (``2026-07-20-cleanup`` vs ``2026-07-26-cleanup``) widen together. Deliberate —
    disambiguating further would require inferring beyond the declared
    ``chain_aliases:`` set, which this module's widening invariant forbids.
    """
    return _CHAIN_DATE_PREFIX_RE.sub("", slug, count=1)


def _read_frontmatter_list_field(content: str, key: str) -> List[str]:
    """Read a top-level frontmatter list field (first block only).

    Supports both inline flow syntax (``key: [a, b]``) and YAML block-list syntax
    (``key:`` followed by indented ``- item`` lines). Returns ``[]`` when the key is
    absent, empty, or the block is malformed — matching this module's None-safe
    convention for other frontmatter readers (``_read_frontmatter_field``).
    """
    lines = content.splitlines()
    fm_n = 0
    prefix_re = re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    item_re = re.compile(r"^\s*-\s*(.+?)\s*$")

    def _clean(raw: str) -> str:
        raw = re.sub(r"\s*#.*$", "", raw)
        return raw.strip().strip('"').strip("'")

    i = 0
    while i < len(lines):
        line = lines[i]
        if line == "---":
            fm_n += 1
            if fm_n == 2:
                return []
            i += 1
            continue
        if fm_n != 1:
            i += 1
            continue
        m = prefix_re.match(line)
        if m:
            rest = _clean(m.group(1))
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1]
                return [_clean(item) for item in inner.split(",") if _clean(item)]
            if rest:
                # scalar value under a list key — tolerate as a single-item list.
                return [rest]
            # block-list form: gather the indented "- item" lines that follow.
            items: List[str] = []
            j = i + 1
            while j < len(lines):
                sub = lines[j]
                if sub == "---":
                    break
                sub_m = item_re.match(sub)
                if sub_m is None:
                    break
                val = _clean(sub_m.group(1))
                if val:
                    items.append(val)
                j += 1
            return items
        i += 1
    return []


def _chain_membership_matches(
    entry_slug: Optional[str],
    entry_aliases: Sequence[str],
    target_slug: str,
) -> bool:
    """Does a completion entry / handoff belong to ``target_slug``'s chain?

    Two DISTINCT match shapes, deliberately kept apart (Fix Half 1 / Fix Half 2):

    1. Date-prefix normalization — ``entry_slug`` and ``target_slug`` are the same
       logical chain modulo a ``YYYY-MM-DD-`` prefix. Strict, invertible transform.

    2. Declared alias membership — an umbrella slug vs sub-slug is NEVER inferred
       from slug shape (no prefix/substring matching, no fuzzy-match warning: a
       warning only audits a false positive after the fact, and this module's
       output attributes commits to completion records under a byte-parity
       contract). ``entry_aliases`` is the entry's own ``chain_aliases:``
       frontmatter list — matched only against that explicitly declared set.
    """
    # Review: code-reviewer — F5: `target_slug in normalized_aliases` (the
    # unnormalized disjunct) was dead code — normalized_aliases only ever
    # contains stripped values, so target_slug can only match it when
    # target_slug already carries no date prefix, in which case
    # target_norm == target_slug and the disjunct below already covers it.
    target_norm = _strip_chain_date_prefix(target_slug)
    if entry_slug is not None and _strip_chain_date_prefix(entry_slug) == target_norm:
        return True
    normalized_aliases = {_strip_chain_date_prefix(alias) for alias in entry_aliases}
    return target_norm in normalized_aliases


def _collect_chain_session_ids(
    worktree_root: Path,
    chain_slug: str,
    seed_session_id: str,
) -> Tuple[List[str], List[str]]:
    """Widen a lone session-id into the full chain session-id set (oracle Step 3.5, Axis 1).

    (a) authored_by: from every archive/completed/**/*.md entry whose frontmatter carries
        ``chain: <slug>`` or ``chain: "<slug>"`` (oracle's dual grep pattern — the whole
        file, not just the frontmatter block, matching the oracle's plain ``grep -rl``).
    (b) claimed_by (new; consumed_by fallback for pre-migration handoffs): from handoffs
        under state/handoffs/ (state-root-seam-resolved, per-repo for a sibling repo /
        claude-klabauter-central when worktree_root IS the meta-repo) and archive/handoffs/ whose
        frontmatter carries ``workstream: <slug>``.

    Chain membership (both (a) and (b)) is decided by ``_chain_membership_matches``, not raw
    equality: ``chain:`` slugs are ``YYYY-MM-DD-`` prefixed but ``workstream:`` values never
    are, so a literal comparison found zero siblings in production; and an umbrella-slug vs
    sub-slug relationship widens ONLY via an entry's own explicitly declared
    ``chain_aliases:`` list — never inferred from slug shape (no prefix/substring matching).

    Returns (dedup_validated_sids, warnings) — dedup preserves the oracle's allowlist-skip
    WARN semantics (skips + warns rather than raising) for any malformed collected id.
    """
    warnings: List[str] = []
    chain_sids: List[str] = [seed_session_id]

    if chain_slug:
        # (a) archive/completed/**/*.md sharing the same chain: slug. File-selection
        # scans every file recursively (oracle's whole-tree scope, byte-parity on WHICH
        # files are considered), but the value comparison uses _read_frontmatter_field
        # (already used two lines below for authored_by) rather than a literal-line
        # match — a raw "chain: <slug>" / 'chain: "<slug>"' string-equality check missed
        # cosmetic drift (trailing inline comment, alternate quoting, whitespace) that
        # _read_frontmatter_field already normalizes, silently under-counting chain
        # siblings. (Review: code-reviewer — Finding 2, P1: literal whole-file string
        # match dropped semantically-valid siblings; frontmatter-field comparison is a
        # strict superset of the literal match, never narrower.)
        completed_root = worktree_root / "archive" / "completed"
        for entry_path in _iter_files(completed_root):
            try:
                text = entry_path.read_text(encoding="utf-8")
            except OSError as exc:
                # DIAGNOSTIC FIX (2026-07-22): an unreadable completion entry was
                # previously dropped silently, under-widening the chain and
                # excluding commits under a missed sibling session id from
                # delta_shorts without any trail. Now surfaced like every other
                # skip reason in this module.
                warnings.append(f"chain scan: unreadable completion entry {entry_path}: {exc}")
                continue
            entry_chain = _read_frontmatter_field(text, "chain")
            entry_aliases = _read_frontmatter_list_field(text, "chain_aliases")
            if not _chain_membership_matches(entry_chain, entry_aliases, chain_slug):
                continue
            ab = _read_frontmatter_field(text, "authored_by")
            if ab and ab != "null":
                chain_sids.append(ab)

        # (b) handoffs (state/handoffs/ + archive/handoffs/) sharing workstream: slug.
        # Same frontmatter-comparison rationale as (a) above.
        handoff_dirs, handoff_dirs_warning = _resolve_handoff_dirs(worktree_root)
        if handoff_dirs_warning:
            warnings.append(handoff_dirs_warning)
        for hdir in handoff_dirs:
            for hf_path in _iter_files(hdir):
                try:
                    text = hf_path.read_text(encoding="utf-8")
                except OSError as exc:
                    # DIAGNOSTIC FIX (2026-07-22): same rationale as the
                    # archive/completed/ scan above — surface, don't drop.
                    warnings.append(f"chain scan: unreadable handoff {hf_path}: {exc}")
                    continue
                entry_workstream = _read_frontmatter_field(text, "workstream")
                entry_aliases = _read_frontmatter_list_field(text, "chain_aliases")
                if not _chain_membership_matches(entry_workstream, entry_aliases, chain_slug):
                    continue
                cb = _read_frontmatter_field(text, "claimed_by") or _read_frontmatter_field(
                    text, "consumed_by"
                )
                if cb and cb != "null":
                    chain_sids.append(cb)

    dedup: List[str] = []
    seen: Set[str] = set()
    for csid in chain_sids:
        if not csid or csid == "null":
            continue
        if not _ID_ALLOWLIST_RE.match(csid):
            warnings.append(
                f"chain session-id {csid!r} fails allowlist — skipped"
            )
            continue
        if csid in seen:
            continue
        seen.add(csid)
        dedup.append(csid)

    return dedup, warnings


def _resolve_handoff_dirs(worktree_root: Path) -> Tuple[List[Path], Optional[str]]:
    """Resolve the two handoff-search roots (state/handoffs/ + archive/handoffs/).

    Mirrors the oracle's ``$(coordinator_state_root)/handoffs`` + ``archive/handoffs``
    dual-root scan, but derives the state root directly from the already-known
    ``worktree_root`` (socket-authoritative, supplied by the IPC engine) rather than
    the oracle's cwd-based ``git rev-parse`` — this is strictly more robust for an
    op invocation, which has no guaranteed cwd binding to the target repo.

    Returns ``(dirs, warning)`` — ``warning`` is a diagnostic string, non-None only
    when ``worktree_root`` genuinely IS the meta-repo (``is_meta_repo`` truthy) but
    resolving the central claude-klabauter root then raised, so the caller falls back to the
    per-repo ``state/handoffs`` dir instead of the real central location — meaning
    ``claimed_by:`` (or legacy ``consumed_by:``) handoffs living there are invisible to
    Axis-1 widening. Surfaced
    both via ``logger.warning`` and the returned string (threaded into the caller's
    ``warnings`` list) so the miss has a diagnostic trail like every other skip reason
    in this module. (Review: code-reviewer — Finding 1, P1: this except branch
    previously swallowed the exception with zero diagnostic.)
    """
    state_root = worktree_root / "state"
    warning: Optional[str] = None
    try:
        from coordinator_core.meta_repo_identity import is_meta_repo

        if is_meta_repo(str(worktree_root)):
            from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root

            state_root = Path(coordinator_claude_klabauter_root()) / "state"
    except Exception as exc:  # noqa: BLE001 — best-effort central-root resolution
        logger.warning(
            "completion.reconcile_commits: meta-repo central-root resolution "
            "failed, falling back to per-repo state dir: %s",
            exc,
        )
        warning = (
            f"meta-repo central-root resolution failed ({exc}) — falling back to "
            "per-repo state dir; claimed_by (or legacy consumed_by) handoffs in the real central location "
            "may be invisible to chain-widening"
        )
    return [state_root / "handoffs", worktree_root / "archive" / "handoffs"], warning


def _collect_session_log(
    worktree_root: Path, merge_base: str, session_ids: Sequence[str]
) -> List[Tuple[str, str]]:
    """Collect (short_sha, full_sha) pairs across all chain session-ids (oracle Step 4).

    BATCHED (2026-08 spawn-amplification fix, chunk C9): one ``git log`` walk of
    ``merge_base..HEAD`` replaces one ``git log --grep=^Session-Id: <sid>$`` spawn
    per session id — this loop resolves MANY session ids against ONE range, the
    batchable "one ref, in-memory membership" shape (§ Anti-scope 1/2/4 governing
    discrimination), NOT independent ranges: there is exactly one range
    (``merge_base..HEAD``) shared by every iteration, so no
    ``reachable(positives) \\ reachable(negatives)`` collapse risk applies. The
    single walk emits each commit's short sha, full sha, and full body (``%B``,
    which carries the ``Session-Id:`` trailer line the oracle's ``--grep`` matched
    against); Python then re-applies the oracle's exact per-line anchored match
    (``^Session-Id: <sid>$``) against that body, grouped by session id.

    No inter-session dedup — the oracle's comment holds: a commit carries exactly one
    ``Session-Id:`` trailer, so cross-session duplicates don't occur in practice.
    Ordering: newest-first WITHIN each session-id's own contribution only — the
    per-session-id result blocks are concatenated in session-iteration order, not
    globally chronologically merged, so a widened multi-session chain's overall
    list is NOT a single newest-first ordering across the whole delta. (Review:
    code-reviewer — Finding 3: caller docstring's unqualified "newest-first"
    overclaimed for the multi-session case.) The batched walk preserves this: the
    single ``git log`` output is itself newest-first, and per-session grouping is
    a stable filter of that order, so within-session order is unchanged; the
    final concatenation still iterates ``session_ids`` in caller order, not the
    walk's chronological order.
    """
    if not session_ids:
        return []

    result = _reality_git(
        worktree_root,
        ["log", "--pretty=%h%x1f%H%x1f%B%x1e", f"{merge_base}..HEAD"],
    )
    if result.returncode != 0:
        return []

    session_id_re = re.compile(r"^Session-Id: (.+)$", re.MULTILINE)
    by_session: dict[str, List[Tuple[str, str]]] = {}
    for record in result.stdout.split("\x1e"):
        record = record.lstrip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        short_sha, full_sha, body = parts
        short_sha = short_sha.strip()
        full_sha = full_sha.strip()
        for m in session_id_re.finditer(body):
            csid = m.group(1)
            by_session.setdefault(csid, []).append((short_sha, full_sha))

    pairs: List[Tuple[str, str]] = []
    for csid in session_ids:
        pairs.extend(by_session.get(csid, []))
    return pairs


def _canonicalize_stored_shas(
    worktree_root: Path, stored_shas: Sequence[str]
) -> Tuple[Set[str], List[str]]:
    """Canonicalize stored (possibly short) SHAs to full SHAs via ``git cat-file --batch-check``.

    BATCHED (2026-08 spawn-amplification fix, chunk C9): one ``git cat-file
    --batch-check``, stdin-fed with every ``<sha>^{commit}`` candidate, replaces
    one ``git rev-parse --verify`` spawn per stored sha — this loop resolves
    independent OBJECT questions (each stdin line answered independently of
    every other), the batchable shape (§ Anti-scope 1/2/4 governing
    discrimination) — not a range walk, so no
    ``reachable(positives) \\ reachable(negatives)`` collapse risk applies.
    Mirrors ``coordinator_core.ops.emit.envelope.classify_shas_on_origin_main``'s
    batch-check idiom: ``--batch-check`` emits exactly one output line per input
    line, in input order, even for objects that don't resolve (printed as
    ``<input> missing``) — line-for-line zip against the input list recovers
    per-sha canonicalization without depending on ``%(objectname)`` prefix-matching
    an abbreviated input string (§ Anti-scope 25: the returned-set-vs-requested-set
    reconciliation this needs — unlike ``--ignore-missing`` batched ``git log``,
    ``--batch-check`` never silently drops a line, so no separate reconciliation
    pass is required here).

    ``commit_reality._git`` is NOT reused for this spawn — it has no stdin-feed
    parameter (a read-only-verb choke point, not a general git runner; out of
    scope for this chunk to extend) — this composes ``subprocess.run`` directly,
    the same shape ``classify_shas_on_origin_main`` itself uses for its own
    ``--batch-check`` call.

    A rev-parse-equivalent failure (ambiguous/unknown ref, or a genuine
    subprocess failure) still WARNs, never crashes — the SHA is simply treated
    as unmatched (never false-folded), same contract as the original per-sha loop.
    """
    full_set: Set[str] = set()
    warnings: List[str] = []
    candidates = [sha for sha in stored_shas if sha]
    if not candidates:
        return full_set, warnings

    from coordinator_core.win_portability import no_console_creationflags

    def _warn_all() -> Tuple[Set[str], List[str]]:
        return full_set, [f"rev-parse failed for {sha} — treating as unmatched" for sha in candidates]

    stdin_payload = "\n".join(f"{sha}^{{commit}}" for sha in candidates) + "\n"
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(worktree_root),
            timeout=120,
            **no_console_creationflags(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return _warn_all()

    if result.returncode != 0:
        return _warn_all()

    lines = result.stdout.splitlines()
    if len(lines) != len(candidates):
        # Malformed/unexpected batch-check output shape — degrade every entry
        # rather than risk misaligning a line to the wrong sha.
        return _warn_all()

    for sha, line in zip(candidates, lines):
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit":
            full_set.add(parts[0])
        else:
            warnings.append(f"rev-parse failed for {sha} — treating as unmatched")

    return full_set, warnings


def resolve_chain_commits(
    worktree_root: Path,
    plan_path: str,
    session_id: str,
    chain_slug: Optional[str] = None,
) -> dict:
    """Resolve the delta short-SHA list for a completion entry (oracle Zone A).

    Composes commit_reality.py's ``_git`` subprocess pattern to perform the oracle's
    merge-base resolution, chain-slug expansion, multi-session commit collection, and
    SHA canonicalization internally — the caller no longer pre-computes this.

    Returns:
        {
          "delta_shorts": [str, ...],   # newest-first WITHIN each chain session-id's
                                         # own contribution (oracle parity); session-id
                                         # groups are concatenated in iteration order,
                                         # NOT globally chronologically interleaved —
                                         # see _collect_session_log. (Review:
                                         # code-reviewer — Finding 3.)
          "delta_count": int,
          "merge_base": str | None,
          "chain_session_ids": [str, ...],
          "merge_base_unresolved": bool, # True iff merge-base against origin/main failed (non-blocking)
          "skip_reason": str | None,
          "provenance_warning": str | None,
          "warnings": [str, ...],        # allowlist-skip / rev-parse-failure diagnostics
        }

    Raises:
        ValueError: ``session_id`` fails the oracle's allowlist/"null" guard
            (mirrors the oracle's exit-1 caller guard — fail loud on a
            correctness-critical id, never silently substitute).
    """
    _validate_id(session_id, "session_id")

    entry_content = Path(plan_path).read_text(encoding="utf-8")

    resolved_chain_slug = chain_slug if chain_slug is not None else _derive_chain_slug(entry_content)
    if resolved_chain_slug and not _ID_ALLOWLIST_RE.match(resolved_chain_slug):
        logger.warning(
            "completion.reconcile_commits: chain slug %r fails allowlist — "
            "chain resolution skipped",
            resolved_chain_slug,
        )
        resolved_chain_slug = ""

    merge_base_result = _reality_git(worktree_root, ["merge-base", "origin/main", "HEAD"])
    merge_base = merge_base_result.stdout.strip() if merge_base_result.returncode == 0 else ""
    if not merge_base:
        return {
            "delta_shorts": [],
            "delta_count": 0,
            "merge_base": None,
            "chain_session_ids": [],
            "merge_base_unresolved": True,
            "skip_reason": "merge-base unresolved — reconcile skipped",
            "provenance_warning": None,
            "warnings": [],
        }

    chain_sids, chain_warnings = _collect_chain_session_ids(
        worktree_root, resolved_chain_slug, session_id
    )

    session_log = _collect_session_log(worktree_root, merge_base, chain_sids)

    stored_shas = sorted(_parse_existing_commits(entry_content))
    stored_full_set, rev_parse_warnings = _canonicalize_stored_shas(worktree_root, stored_shas)

    delta_shorts: List[str] = []
    for short_sha, full_sha in session_log:
        if full_sha not in stored_full_set:
            delta_shorts.append(short_sha)

    provenance_warning: Optional[str] = None
    if not delta_shorts and not session_log:
        any_trailer = _reality_git(
            worktree_root,
            ["log", "--pretty=%H", "--grep=^Session-Id: ", f"{merge_base}..HEAD"],
        )
        if any_trailer.returncode == 0 and any_trailer.stdout.strip():
            count_result = _reality_git(
                worktree_root, ["rev-list", "--count", f"{merge_base}..HEAD"]
            )
            count_str = count_result.stdout.strip() if count_result.returncode == 0 else "?"
            provenance_warning = (
                f"reconcile-completion-commits: {count_str} commit(s) in "
                f"{merge_base}..HEAD carry Session-Id: trailer(s) but NONE match the "
                f"entry authored_by/chain set ({','.join(chain_sids)}) — id-provenance "
                "mismatch; commits: NOT reconciled. Check authored_by."
            )

    return {
        "delta_shorts": delta_shorts,
        "delta_count": len(delta_shorts),
        "merge_base": merge_base,
        "chain_session_ids": chain_sids,
        "merge_base_unresolved": False,
        "skip_reason": None,
        "provenance_warning": provenance_warning,
        "warnings": [*chain_warnings, *rev_parse_warnings],
    }


def reconcile_completion_commits(
    plan_path: str,
    commits: Optional[list[str]] = None,
    session_id: Optional[str] = None,
    chain_slug: Optional[str] = None,
    worktree_root: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Fold missing SHAs into the completion entry's ``commits:`` list (idempotent).

    Byte-parity port of the write pass of ``reconcile-completion-commits.sh --append``.
    The oracle's git-log and merge-base resolution are caller obligations; this function
    receives the pre-computed short SHA list and applies only the file-write step.

    Idempotency: SHAs already present in the file's ``commits:`` list are skipped (string
    equality). Re-running with the same ``commits`` list produces no change (matches oracle
    ``delta=0 OK`` path).

    DR-216 bounds (D2):
      (i)  Per-record idempotent: re-folding an already-present SHA is a no-op.
      (ii) Git-reversible: only appends SHA items; never rewrites/reorders/deletes.
      (iii) Content-additive in-place only: awk-equivalent fold preserves all existing content.
      (iv)  Confined to the caller-supplied ``plan_path`` (the ``docs/plans/*.md`` noun).
      (v)   No git commit.

    REOPENED session_id call path (see module negative-spec): when ``session_id`` is
    given, ``commits`` is ignored and the delta short-SHA list is resolved internally
    via ``resolve_chain_commits`` (git-log/merge-base/chain-walk/canonicalization),
    composing commit_reality.py's git machinery. ``worktree_root`` is required on this
    path. Two early-exit outcomes precede the fold, both oracle-parity no-ops:
      - merge-base against origin/main unresolvable -> ``{"merge_base_unresolved": True,
        "no_op": True, "appended": 0}`` (non-blocking, mirrors the oracle's
        exit-0 "reconcile skipped" path).
      - zero delta (``delta_count == 0``) -> ``{"appended": 0, "no_op": True,
        "provenance_warning": <str|None>}`` (oracle's ``delta=0 OK`` path — the
        id-provenance-mismatch probe fires here, never blocking).

    dry_run (read-only mode, added to unblock coordinator-claude's detect-only facade branch —
    docs/plans referenced in the op's producer notes): when True, every git-resolution
    and delta-computation step still runs in full (identical to the apply path), but the
    mutation region — ``_apply_commits_fold`` / ``tempfile.mkstemp`` / ``os.replace`` — is
    skipped entirely. NOTHING is written to ``plan_path`` on this path; ``appended`` is
    always ``0``. The two early-return no-ops (``merge_base_unresolved`` and
    ``delta_count == 0``) execute identically regardless of ``dry_run`` — they precede the
    dry_run gate and already never write, so their return SHAPE is dry_run-invariant by
    construction (see ``TestDryRunReturnShapeInvariant`` in the test module for the
    assertion). The delta SHA list is now included in the return payload
    UNCONDITIONALLY (``delta_shorts``) — both dry-run and apply paths — so a dry-run
    caller can reproduce the same output the apply path would have produced.

    Parameters:
        plan_path:     Absolute path to the completion plan file.
        commits:       List of short SHA strings to fold in (oracle's ``delta_shorts``).
                       Backward-compat path only — ignored when ``session_id`` is given.
        session_id:    REOPENED path trigger. When given, commits are resolved internally.
        chain_slug:    Optional override for the REOPENED path's chain-widening slug;
                       defaults to the entry's own ``chain:`` frontmatter field.
        worktree_root: Repo root for git resolution — required when ``session_id`` is given.
        dry_run:       When True, computes the delta but performs ZERO writes. Defaults to
                       False — see the handler docstring for why this default deliberately
                       diverges from handoff.reconcile_open's default-True dry_run.

    Returns:
        dict with keys: ``plan_path``, ``appended`` (int count of SHAs folded in — always
        ``0`` when ``dry_run`` is True), ``skipped`` (int count of SHAs already present),
        ``no_op`` (bool), ``dry_run`` (bool, echoed back), ``delta_shorts`` (list[str] — the
        SHAs that were folded in, or would have been under dry_run), and — on the
        REOPENED path only — ``merge_base_unresolved`` (bool), ``provenance_warning``
        (str | None), ``chain_session_ids`` (list[str]), ``resolution_warnings`` (list[str]).

    Raises:
        ValueError: ``session_id`` given but invalid (allowlist/"null" guard), or
            ``session_id`` given without ``worktree_root``.

    Locking (DR-216 § D2 criterion (vi), AMENDED 2026-08-06): the read-modify-write
    of ``plan_path`` runs under ``coordinator_core.locked_write.locked_rmw``, keyed
    to ``plan_path``, whenever a git repo root is resolvable for it (``worktree_root``
    when given, else derived directly from ``plan_path`` — the backward-compat
    pre-computed-commits call path is not guaranteed a git repo at all, e.g. a bare
    tmp-dir entry in a unit test, matching ``plan_status_transition._stamp_implemented``'s
    identical no-repo plain-RMW fallback). ``resolve_chain_commits`` (merge-base,
    chain-walk, multi-session ``git log``) deliberately runs BEFORE and OUTSIDE the
    lock: holding the lock across several git subprocesses would widen the
    contention window on a lock that also guards a break-glass path (another
    session annotating a plan to warn its executing owner) enough to make the 10s
    ``LockTimeout`` a realistic, harmful outcome. The delta short-SHA list
    (``commits``) resolved outside the lock may therefore be stale by the time the
    mutate closure runs — the closure re-derives ``existing`` from the FRESH
    lock-held read and re-filters ``commits`` against it, so a SHA already folded
    in by a concurrent writer between resolution and the lock is never
    double-appended (no-op-safe by construction, not merely by convention).
    """
    path = Path(plan_path)

    resolution_meta: dict = {}
    if session_id is not None:
        if not worktree_root:
            raise ValueError(
                "completion.reconcile_commits: worktree_root is required when "
                "session_id is given (REOPENED git-resolution path)"
            )
        resolved = resolve_chain_commits(
            Path(worktree_root), plan_path, session_id, chain_slug=chain_slug
        )
        resolution_meta = {
            "merge_base_unresolved": resolved["merge_base_unresolved"],
            "provenance_warning": resolved["provenance_warning"],
            "chain_session_ids": resolved["chain_session_ids"],
            "resolution_warnings": resolved["warnings"],
        }
        if resolved["merge_base_unresolved"]:
            return {
                "plan_path": plan_path,
                "appended": 0,
                "skipped": 0,
                "no_op": True,
                "dry_run": dry_run,
                "delta_shorts": [],
                **resolution_meta,
            }
        if resolved["delta_count"] == 0:
            return {
                "plan_path": plan_path,
                "appended": 0,
                "skipped": 0,
                "no_op": True,
                "dry_run": dry_run,
                "delta_shorts": [],
                **resolution_meta,
            }
        commits = resolved["delta_shorts"]

    if commits is None:
        commits = []

    # Closure-captured state — locked_rmw's return value (new_text) doesn't tell
    # us whether a write happened, so the rich structured fields this function
    # returns are threaded out via `_state` (idiom: plan_status_transition.
    # _stamp_implemented's `_state` dict).
    _state: dict = {
        "appended": 0,
        "skipped": 0,
        "no_op": True,
        "dry_run": dry_run,
        "delta_shorts": [],
    }

    def mutate(old_text: str) -> str:
        # `existing`/`new_shas` are re-derived from the FRESH lock-held read —
        # see the "Locking" docstring section above for why this must not reuse
        # the pre-lock-resolution `commits` computation's own stale content.
        existing = _parse_existing_commits(old_text)
        new_shas = [sha for sha in commits if sha not in existing]
        skipped = len(commits) - len(new_shas)

        if not new_shas:
            _state.update(
                appended=0, skipped=skipped, no_op=True, dry_run=dry_run, delta_shorts=[]
            )
            return old_text  # byte-identical -> locked_rmw skips the write

        if dry_run:
            # Read-only gate: NOTHING is written on this path — old_text is
            # returned unchanged so locked_rmw's own no-op skip applies.
            _state.update(
                appended=0, skipped=skipped, no_op=False, dry_run=True, delta_shorts=new_shas
            )
            return old_text

        modified = _apply_commits_fold(old_text, new_shas)
        _state.update(
            appended=len(new_shas),
            skipped=skipped,
            no_op=False,
            dry_run=False,
            delta_shorts=new_shas,
        )
        return modified

    from coordinator_core.locked_write import locked_rmw

    lock_repo_root: Optional[Path] = Path(worktree_root) if worktree_root else None
    if lock_repo_root is None:
        from coordinator_core.archive_stamp import _resolve_repo_root_for

        _derived_worktree, _derived_common = _resolve_repo_root_for(path)
        lock_repo_root = _derived_common

    if lock_repo_root is not None:
        locked_rmw(path, mutate, repo_root=lock_repo_root)
    else:
        # No resolvable git repo root for `plan_path` — same fallback shape as
        # `plan_status_transition._stamp_implemented`'s no-repo branch: locking
        # exists to serialise concurrent writers sharing one lock-sidecar
        # namespace keyed off a repo's git common dir, and a path outside any
        # git worktree has no such namespace and (by this module's existing
        # backward-compat contract, exercised only by the pre-computed-commits
        # call path) no concurrent-writer hazard to serialise against.
        if not path.exists():
            raise FileNotFoundError(str(path))
        old_text = path.read_text(encoding="utf-8")
        new_text = mutate(old_text)
        if new_text != old_text:
            dir_path = path.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new_text)
                os.replace(tmp_path, str(path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    # Best-effort tmp-file cleanup on the error path; the
                    # original exception is re-raised below regardless.
                    pass
                raise

    return {
        "plan_path": plan_path,
        "appended": _state["appended"],
        "skipped": _state["skipped"],
        "no_op": _state["no_op"],
        "dry_run": _state["dry_run"],
        "delta_shorts": _state["delta_shorts"],
        **resolution_meta,
    }


# ---------------------------------------------------------------------------
# plan.append_session — append session-tracking entry to agent_sessions: list
# ---------------------------------------------------------------------------
# Byte-parity oracle: append-plan-session.sh (mapfile/printf write pass).
# ---------------------------------------------------------------------------


def _now_iso_utc() -> str:
    """Return the current UTC time in ISO-8601 format matching ``_cs_now_iso()``.

    Oracle: ``date -u +"%Y-%m-%dT%H:%M:%SZ"`` → e.g., ``2026-07-06T12:00:00Z``.
    """
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_session_append(
    content: str,
    session_id: str,
    status: str,
    created_at: str,
) -> tuple[str, bool]:
    """Append a session-tracking entry to the ``agent_sessions:`` YAML list.

    Byte-parity port of ``append-plan-session.sh`` (mapfile/printf write pass).

    Entry format (oracle encoding contract):
        ``  - "<session_id>|<status>|<created_at>"``

    Idempotency (Layer 3 / oracle dedup): if a list item already contains ``session_id``
    as its first ``|``-delimited field, returns ``(content, False)`` without modification.

    Insert position (mirrors oracle Bash logic):
        - Key exists + entries present: insert after the last existing entry (last_entry_idx).
        - Key exists + no entries: insert after the key line (agent_sessions_key_idx).
        - Key absent: insert ``agent_sessions:`` block + entry immediately before the closing
          ``---`` fence (close_idx).

    Write format (mirrors ``printf '%s\\n' "${new_lines[@]}"``:
        Lines stored without trailing newline; output joined with ``\\n`` + trailing ``\\n``.

    Returns:
        (modified_content, True) on success; (content, False) on idempotent no-op.

    Raises:
        ValueError: malformed frontmatter (no leading or closing ``---`` fence).
    """
    new_entry = f"{session_id}|{status}|{created_at}"
    new_line = f'  - "{new_entry}"'

    # Oracle: mapfile -t lines < "${plan_file}" → strips trailing newlines per element.
    lines = content.splitlines(keepends=False)
    total_lines = len(lines)

    if total_lines == 0 or lines[0] != "---":
        raise ValueError("malformed frontmatter: no leading --- fence")

    # Find closing --- fence (oracle: for (( i=1; i<total_lines; i++ )) ... if [[ "${lines[$i]}" == "---" ]])
    close_idx = -1
    for i in range(1, total_lines):
        if lines[i] == "---":
            close_idx = i
            break

    if close_idx < 0:
        raise ValueError("malformed frontmatter: no closing --- fence")

    # Parse agent_sessions: block within frontmatter.
    in_agent_sessions = False
    agent_sessions_key_idx = -1
    last_entry_idx = -1

    for i in range(1, close_idx):
        line = lines[i]
        if line == "agent_sessions:":
            in_agent_sessions = True
            agent_sessions_key_idx = i
            continue

        if in_agent_sessions:
            # List item: re.match(r'^\s+-\s', line) mirrors oracle [[ "${line}" =~ ^[[:space:]]*-[[:space:]] ]]
            if re.match(r"^\s+-\s", line):
                last_entry_idx = i
                # Idempotency check: strip leading '  - ' and surrounding quotes, take first |field.
                entry_raw = re.sub(r"^\s*-\s*", "", line).strip('"').strip("'")
                existing_sid = entry_raw.split("|")[0]
                if existing_sid == session_id:
                    return content, False  # Already present — Layer 3 dedup.
            elif line and not re.match(r"^\s", line):
                # Non-blank, non-indented line → next frontmatter key; exit block.
                in_agent_sessions = False

    # Build modified lines (oracle: new_lines=(); for i: new_lines+=("${lines[$i]}") ... ).
    if agent_sessions_key_idx >= 0:
        # Key exists: insert after last entry or after key if no entries yet.
        insert_after = last_entry_idx if last_entry_idx >= 0 else agent_sessions_key_idx
        new_lines: list[str] = []
        for i in range(total_lines):
            new_lines.append(lines[i])
            if i == insert_after:
                new_lines.append(new_line)
    else:
        # Key absent: insert agent_sessions: block immediately before the closing --- fence.
        # Oracle: if (( i == close_idx )): push "agent_sessions:" + new_line, then push lines[i].
        new_lines = []
        for i in range(total_lines):
            if i == close_idx:
                new_lines.append("agent_sessions:")
                new_lines.append(new_line)
            new_lines.append(lines[i])

    # Reconstruct: mirrors printf '%s\n' "${new_lines[@]}" (newline after each element).
    modified = "\n".join(new_lines) + "\n"
    return modified, True


def append_plan_session(
    plan_path: str,
    session_id: str,
    status: str = "working",
    created_at: Optional[str] = None,
) -> dict:
    """Append a session-tracking record to the plan's ``agent_sessions:`` YAML list.

    Byte-parity port of ``append-plan-session.sh`` (mapfile/printf write pass).

    Entry encoding contract (oracle): ``"<session_id>|<status>|<created_at>"``
    where ``created_at`` is an ISO-8601 UTC timestamp (``YYYY-MM-DDThh:mm:ssZ``).

    Idempotency: if ``session_id`` is already present as the first ``|``-field of any
    existing entry, returns without writing (matches oracle Layer-3 dedup).

    DR-216 bounds (D2):
      (i)  Per-record idempotent: same session_id → no-op.
      (ii) Git-reversible: only appends a new list entry; never rewrites/reorders/deletes.
      (iii) Content-additive in-place only.
      (iv)  Confined to the caller-supplied ``plan_path`` (the ``docs/plans/*.md`` noun).
      (v)   No git commit.

    Parameters:
        plan_path:   Absolute path to the plan file.
        session_id:  Session ID to record (caller-resolved; not derived from env/sentinel here).
        status:      Entry status field (default ``"working"``).
        created_at:  ISO-8601 UTC timestamp; generated via ``_now_iso_utc()`` when absent.

    Returns:
        dict with keys: ``plan_path``, ``appended`` (bool), ``session_id``, ``created_at``.

    Locking (DR-216 § D2 criterion (vi), AMENDED 2026-08-06): the read-modify-write
    of ``plan_path`` runs under ``coordinator_core.locked_write.locked_rmw``, keyed
    to ``plan_path``, whenever a git repo root is resolvable for it — this function
    takes no ``worktree_root`` parameter, so the repo root is derived directly from
    ``plan_path`` itself (idiom: ``plan_status_transition._stamp_implemented``'s own
    ``_resolve_repo_root_for(plan_path)`` call). The transform between read and
    write is pure in-memory (no git subprocess), so the whole mutate closure is the
    critical section — there is no "expensive work to keep outside the lock" split
    here, unlike ``reconcile_completion_commits``.
    """
    if not created_at:
        created_at = _now_iso_utc()

    path = Path(plan_path)

    _state: dict = {"appended": False}

    def mutate(old_text: str) -> str:
        modified, was_appended = _apply_session_append(old_text, session_id, status, created_at)
        _state["appended"] = was_appended
        return modified if was_appended else old_text

    from coordinator_core.archive_stamp import _resolve_repo_root_for
    from coordinator_core.locked_write import locked_rmw

    _derived_worktree, lock_repo_root = _resolve_repo_root_for(path)

    if lock_repo_root is not None:
        locked_rmw(path, mutate, repo_root=lock_repo_root)
    else:
        # No resolvable git repo root for `plan_path` — same fallback shape as
        # `plan_status_transition._stamp_implemented`'s no-repo branch (see
        # `reconcile_completion_commits`'s identical fallback for the full
        # rationale): a path outside any git worktree has no lock-sidecar
        # namespace and no concurrent-writer hazard to serialise against.
        if not path.exists():
            raise FileNotFoundError(str(path))
        old_text = path.read_text(encoding="utf-8")
        new_text = mutate(old_text)
        if new_text != old_text:
            dir_path = path.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new_text)
                os.replace(tmp_path, str(path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    # Best-effort tmp-file cleanup on the error path; the
                    # original exception is re-raised below regardless.
                    pass
                raise

    if not _state["appended"]:
        return {
            "plan_path": plan_path,
            "appended": False,
            "session_id": session_id,
            "created_at": created_at,
            "no_op": True,
        }

    return {
        "plan_path": plan_path,
        "appended": True,
        "session_id": session_id,
        "created_at": created_at,
        "no_op": False,
    }


# ---------------------------------------------------------------------------
# JSON-RPC handlers
# ---------------------------------------------------------------------------


@register_op("completion.reconcile_commits")
async def _reconcile_commits_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``completion.reconcile_commits`` handler.

    MUTATING (DR-216): folds missing SHAs into the caller-supplied plan file's
    ``commits:`` YAML list. Blocking FS I/O runs via ``asyncio.to_thread`` (DR-216 D3).

    Required params:
        plan_path (str): absolute path to the completion plan file.
        commits (list[str] | str): short SHAs to fold in; accepts comma-separated string.
            Ignored when ``session_id`` is given (REOPENED path resolves internally).

    Optional params (REOPENED git-resolution path — see module negative-spec):
        session_id (str): when given, triggers internal git resolution (merge-base +
            chain-walk + multi-session log collection + SHA canonicalization) instead
            of trusting the caller-supplied ``commits`` list. Must pass the oracle's
            allowlist and not be the literal "null".
        chain_slug (str): optional override for chain-widening; defaults to the entry's
            own ``chain:`` frontmatter field when omitted.
        dry_run (bool, optional) — DEFAULTS TO FALSE. This DIVERGES deliberately from
            handoff.reconcile_open's default-True dry_run: that op is an autonomous
            policy sweep, this op is caller-invoked with an explicit plan_path and
            already has coordinator-claude-side callers (``--append``) that rely on writes happening
            by default — defaulting True here would silently stop every existing
            append caller from writing. When True, computes the full delta (identical
            git resolution to the apply path) but performs ZERO writes — no
            ``_apply_commits_fold``, no ``mkstemp``, no ``os.replace``. A non-bool
            value fails CONSERVATIVE the opposite direction from the default: it
            coerces to True (dry-run / no mutation), not False, since an unrecognized
            value should never silently authorize a write.

    Returns:
        {plan_path, appended, skipped, no_op, dry_run, delta_shorts} — plus, on the
        REOPENED path, {merge_base_unresolved, provenance_warning, chain_session_ids,
        resolution_warnings}. ``delta_shorts`` is populated unconditionally (both
        dry-run and apply paths) so a dry-run caller can reproduce the apply path's
        output contract (see coordinator-claude's reconcile-completion-commits.sh detect-only branch,
        which this dry_run mode exists to let that facade eventually collapse onto).

    ``repo_root`` is provided by the IPC engine (``_OP_KEY_SCOPE: common_dir``) and, since
    the op-family path-containment sweep (2026-07-08), is used to derive the allowed-root(s)
    (``archive/completed/`` and ``docs/plans/``) for the containment check — ``plan_path``
    itself remains the DR-216 noun and is never derived from ``repo_root``. The REOPENED
    path additionally threads ``main_worktree_root(repo_root)`` through as
    ``worktree_root`` for git resolution — derived from ``repo_root``, never read directly
    out of ``params``, so it cannot be smuggled by a JSON-RPC caller via the method's own
    parameter list (no UDS socket in the current in-process model — retired by DR-215).
    (Review: code-reviewer — Finding 4/7: docstring was stale after the containment guard
    landed, and "socket-authoritative" overclaimed the trust boundary post-DR-215.)

    DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    """
    plan_path: str = params.get("plan_path", "")
    commits = params.get("commits", [])
    if isinstance(commits, str):
        commits = [c.strip() for c in commits.split(",") if c.strip()]
    elif not isinstance(commits, list):
        commits = []

    # A present-but-empty "session_id" param is a fail-loud condition, not "not given" —
    # distinguish key-absent from key-present-but-empty/falsy here so an upstream
    # serialization bug that empties the field can't silently reroute the request onto
    # the backward-compat pre-computed-commits path instead of raising the allowlist
    # guard this module explicitly designed for this exact input shape. (Review:
    # code-reviewer — Finding 5, P2: "" or None coerced an explicitly-empty session_id
    # to "not given", the opposite of the module's stated fail-loud posture.)
    raw_session_id = params.get("session_id", None)
    if raw_session_id == "":
        return {
            "error": "completion.reconcile_commits: session_id is empty or the literal 'null'",
            "no_op": True,
        }
    session_id: Optional[str] = raw_session_id or None
    chain_slug: Optional[str] = params.get("chain_slug") or None

    # dry_run: default False (deliberate divergence from handoff.reconcile_open's
    # default-True — see docstring above). Non-bool coerces to True: an unrecognized
    # value must never silently authorize a write, so the fail-conservative direction
    # here is the opposite of the absent-key default.
    raw_dry_run = params.get("dry_run", False)
    dry_run: bool = raw_dry_run if isinstance(raw_dry_run, bool) else True

    # DR-216 D2(iv) noun confinement: empty-path guard (mandatory).
    if not plan_path:
        return {"error": "completion.reconcile_commits: 'plan_path' param is required", "no_op": True}

    # Op-family path-containment sweep, 2026-07-08: repo_root is required to derive the
    # worktree root (common_dir scope keying guarantees the command-type invoker always
    # supplies --repo; see ipc.py's _OP_KEY_SCOPE["completion.reconcile_commits"] = "common_dir").
    # Mirrors handoff.transition/handoff.stamp's repo_root-required gate — containment is not
    # optional, so the worktree root must always be derivable.
    if repo_root is None:
        return {
            "error": (
                "completion.reconcile_commits: repo_root is required "
                "(no founding root available — handler called without socket-authoritative common_dir)"
            ),
            "no_op": True,
        }

    worktree = main_worktree_root(repo_root)

    # Containment: resolved plan_path MUST be under <worktree>/archive/completed/ (the real
    # production noun — completion entries with status: pending-release empirically live here;
    # 61 confirmed, 0 under docs/plans/) OR <worktree>/docs/plans/ (DR-216's originally-stated
    # noun, kept as a conservative superset in case that framing has a real flow).
    # Mirrors handoff.has_live_children's dual-root allow-list (state/handoffs/ + archive/handoffs/).
    # docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4.
    # (Review: code-reviewer — Finding 1, P1: docs/plans/-only rejected the oracle's real
    # archive/completed/ entries; widened to both allowed roots.)
    p = Path(plan_path)
    if not p.is_absolute():
        p = worktree / p
    contained = contained_path(
        p, [worktree / "docs" / "plans", worktree / "archive" / "completed"]
    )
    if contained is None:
        return {
            "error": (
                "completion.reconcile_commits: plan_path escapes "
                f"docs/plans/ or archive/completed/: {plan_path!r}"
            ),
            "no_op": True,
        }
    plan_path = str(contained)

    try:
        return await asyncio.to_thread(
            reconcile_completion_commits,
            plan_path=plan_path,
            commits=commits,
            session_id=session_id,
            chain_slug=chain_slug,
            worktree_root=str(worktree),
            dry_run=dry_run,
        )
    except ValueError as exc:
        # session_id allowlist/"null" guard (oracle Step 3 exit-1 parity) — never
        # crash the handler; return the structured error shape like every other guard.
        return {"error": f"completion.reconcile_commits: {exc}", "no_op": True}


@register_op("plan.append_session")
async def _append_session_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``plan.append_session`` handler.

    MUTATING (DR-216): appends a session-tracking entry to the caller-supplied plan
    file's ``agent_sessions:`` YAML list. Blocking FS I/O runs via ``asyncio.to_thread``
    (DR-216 D3).

    Required params:
        plan_path  (str): absolute path to the plan file.
        session_id (str): session ID to record; falls back to CLAUDE_CODE_SESSION_ID env var.

    Optional params:
        status     (str): entry status (default ``"working"``).
        created_at (str): ISO-8601 UTC timestamp; generated if absent.

    Returns:
        {plan_path, appended, session_id, created_at, no_op}

    DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    """
    plan_path: str = params.get("plan_path", "")
    session_id: str = params.get(
        "session_id", os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    )
    status: str = params.get("status", "working")
    created_at: Optional[str] = params.get("created_at") or None

    # DR-216 D2(iv) noun confinement: empty-path guard (mandatory).
    if not plan_path:
        return {"error": "plan.append_session: 'plan_path' param is required", "no_op": True}

    # Op-family path-containment sweep, 2026-07-08: repo_root is required to derive the
    # worktree root (common_dir scope keying guarantees the command-type invoker always
    # supplies --repo; see ipc.py's _OP_KEY_SCOPE["plan.append_session"] = "common_dir").
    # Mirrors handoff.transition/handoff.stamp's repo_root-required gate — containment is not
    # optional, so the worktree root must always be derivable.
    if repo_root is None:
        return {
            "error": (
                "plan.append_session: repo_root is required "
                "(no founding root available — handler called without socket-authoritative common_dir)"
            ),
            "no_op": True,
        }

    worktree = main_worktree_root(repo_root)

    # Containment: resolved plan_path MUST be under <worktree>/docs/plans/ (the DR-216 noun).
    # docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4.
    p = Path(plan_path)
    if not p.is_absolute():
        p = worktree / p
    contained = contained_path(p, [worktree / "docs" / "plans"])
    if contained is None:
        return {
            "error": f"plan.append_session: plan_path escapes docs/plans/: {plan_path!r}",
            "no_op": True,
        }
    plan_path = str(contained)

    return await asyncio.to_thread(
        append_plan_session,
        plan_path=plan_path,
        session_id=session_id,
        status=status,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# completion.flip_to_released — per-entry release-tag resolution + frontmatter flip
# ---------------------------------------------------------------------------
# Byte-parity oracle: [coordinator-claude] coordinator/skills/merging-to-main/SKILL.md
# § Step 1.65 item 3's python3 -<<'PYEOF' block. Contract provisional — see module docstring.
# ---------------------------------------------------------------------------

#: Frontmatter fields this op writes, in write order (mirrors the oracle's ``fields`` dict).
_FLIP_FIELD_ORDER: Tuple[str, ...] = ("status", "released_in", "released_at", "released_sha")


def _tag_sha(worktree_root: Path, tag: str) -> Optional[str]:
    """Return the commit SHA a tag resolves to, or ``None`` if it doesn't exist yet.

    Byte-parity port of the oracle's ``tag_sha()`` (``git rev-list -n 1 <tag>``). A
    failure here is expected, not exceptional: ``candidate_tags``' last element is
    conventionally the release currently being cut, which may not exist as a real
    tag yet at flip time (tagging happens after this step in the ceremony).
    """
    result = _reality_git(worktree_root, ["rev-list", "-n", "1", tag])
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _tag_date(worktree_root: Path, sha_or_tag: str) -> Optional[str]:
    """Return a commit's author date as ``YYYY-MM-DD``, or ``None`` on failure.

    Byte-parity port of the oracle's ``tag_date()`` (``git log -1 --format=%ad
    --date=short <sha_or_tag>``).
    """
    result = _reality_git(worktree_root, ["log", "-1", "--format=%ad", "--date=short", sha_or_tag])
    date = result.stdout.strip()
    return date if result.returncode == 0 and date else None


def _current_head_sha(worktree_root: Path) -> Optional[str]:
    """Return the worktree's current ``HEAD`` commit SHA, or ``None`` on failure."""
    result = _reality_git(worktree_root, ["rev-parse", "HEAD"])
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _today_utc_date() -> str:
    """Return today's date (UTC) as ``YYYY-MM-DD`` — fallback when a tag has no commit yet."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _contains_all_commits(worktree_root: Path, tag: str, commits: Sequence[str]) -> bool:
    """True iff every commit in ``commits`` is an ancestor of ``tag``.

    BATCHED (2026-08 spawn-amplification fix, chunk C9): one ``git rev-list <tag>``
    plus in-memory membership replaces one ``git merge-base --is-ancestor`` spawn
    per commit — this loop resolves MANY commits against ONE ref (``tag``), the
    batchable "one-ref-many-commits" shape (§ Anti-scope 1/2/4 governing
    discrimination), NOT independent ranges: there is exactly one ref
    involved, so no ``reachable(positives) \\ reachable(negatives)`` collapse risk
    applies. Reuses the proven shape of
    ``coordinator_core.ops.emit.envelope.classify_shas_on_origin_main`` (its
    ancestor-set half): everything printed by ``git rev-list <tag>`` is, by
    definition, both a valid commit AND an ancestor of ``tag``.

    ``commits`` (this module's own ``commits:`` frontmatter values, always short
    SHAs — see ``_apply_commits_fold``) must be canonicalized to full length
    before the membership test, since ``git rev-list`` always prints full SHAs —
    a short sha can never string-match a full one. Reuses
    ``_canonicalize_stored_shas`` (this module's own batched canonicalizer, chunk
    C9) rather than re-deriving a second canonicalization primitive; a commit
    that fails to canonicalize (bad/unknown ref) is, by the original loop's own
    contract (non-zero ``merge-base --is-ancestor`` returncode -> False), never
    an ancestor.

    Same primitive SHAPE as ``orphan_branch_sweep.py``'s ``_is_ancestor``, composed
    here over this module's own cwd-aware ``_reality_git(worktree_root, args)``
    rather than imported — see module docstring for why the oracle module's
    two-arg ``_is_ancestor`` (no ``cwd`` parameter, always runs against the
    process's own cwd) is not safe to reuse for a ``common_dir``-scoped op
    resolving state in the CALLER's worktree. Empty ``commits`` is never a match
    (oracle: ``if commits and contains_all(...)``).
    """
    if not commits:
        return False

    rev_list = _reality_git(worktree_root, ["rev-list", tag])
    if rev_list.returncode != 0:
        return False
    ancestor_set = set(rev_list.stdout.split())

    unique_commits = set(commits)
    canonical, _warnings = _canonicalize_stored_shas(worktree_root, list(unique_commits))
    if len(canonical) != len(unique_commits):
        # At least one commit failed to canonicalize (bad/unknown ref) — never
        # an ancestor, matching the original loop's returncode-!=0 -> False.
        return False

    return canonical.issubset(ancestor_set)


def _resolve_release_tag(
    worktree_root: Path, candidate_tags: Sequence[str], commits: Sequence[str]
) -> str:
    """Resolve the earliest candidate tag whose history contains every commit in ``commits``.

    ``candidate_tags`` is caller-supplied, ordered oldest-first (the oracle's own
    ``git tag --list ... --sort=creatordate`` enumeration is a caller concern, not this
    op's — see module docstring negative-spec). Falls through to the LAST element of
    ``candidate_tags`` (the release currently being cut) when no earlier tag contains
    the commits — mirrors the oracle's ``resolved_tag = release_tag_cut`` fallback.
    """
    for tag in candidate_tags:
        if _contains_all_commits(worktree_root, tag, commits):
            return tag
    return candidate_tags[-1]


def _apply_flip_fields(content: str, fields: dict) -> str:
    """Rewrite/insert frontmatter fields in place (byte-parity port of the oracle's write pass).

    For each line in the first (and only) frontmatter block: a line whose key matches a
    key in ``fields`` is replaced wholesale with ``"<key>: <value>"``; any key in
    ``fields`` never encountered before the closing ``---`` fence is inserted immediately
    before that fence (oracle: ``for key, val in fields.items(): if key not in seen: ...``
    inserted at ``n == 2``). Lines outside the frontmatter block, and frontmatter lines
    whose key is not in ``fields``, pass through unchanged (DR-216 D2(iii) parity).

    Raises:
        ValueError: malformed frontmatter (fewer than two ``---`` fence lines).
    """
    out: list[str] = []
    n = 0
    seen: set[str] = set()
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped == "---":
            n += 1
            if n == 2:
                for key, val in fields.items():
                    if key not in seen:
                        out.append(f"{key}: {val}\n")
            out.append(line)
            continue
        if n == 1:
            key = stripped.split(":", 1)[0]
            if key in fields:
                out.append(f"{key}: {fields[key]}\n")
                seen.add(key)
                continue
        out.append(line)

    if n < 2:
        raise ValueError("malformed frontmatter: fewer than two '---' fence lines")

    return "".join(out)


def _flip_one_entry(
    worktree_root: Path, entry_path: Path, candidate_tags: Sequence[str]
) -> Tuple[Optional[dict], Optional[str]]:
    """Resolve and (conditionally) flip a single completion entry.

    Returns ``(flipped_record, skip_reason)`` — exactly one is non-``None``.
    ``flipped_record`` is ``{"path", "released_in", "released_at", "released_sha"}``.
    A ``skip_reason`` covers: entry not readable, ``status:`` is not
    ``pending-release``, or the resolved fields are already byte-identical to what is
    on disk (idempotent no-op — see module docstring's idempotency note; this branch
    is a port-time addition, not a byte-parity target of the oracle, which never
    re-runs the same entry twice).

    Locking (DR-216 § D2 criterion (vi), AMENDED 2026-08-06): the actual
    frontmatter rewrite runs under ``coordinator_core.locked_write.locked_rmw``,
    keyed to ``entry_path``. The four git subprocesses this function runs to
    resolve ``resolved_tag``/``sha``/``date`` (``_resolve_release_tag`` ->
    ``_contains_all_commits`` (N calls) -> ``_tag_sha`` -> ``_tag_date`` /
    ``_current_head_sha``) all run BEFORE and OUTSIDE the lock — same rationale
    as ``reconcile_completion_commits``: holding the lock across several git
    subprocesses would widen the contention window on a lock that also guards a
    break-glass annotation path. The pre-lock read (``text``) that seeds
    ``commits``/``current_status`` for that resolution may be stale by the time
    the lock is acquired; the mutate closure therefore re-derives
    ``current_status`` from the FRESH lock-held read and re-applies the same
    idempotency/already-released guards against it, so a concurrent flip
    between resolution and the lock is never double-applied.
    """
    try:
        text = entry_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{entry_path}: unreadable ({exc})"

    current_status = _read_frontmatter_field(text, "status")
    if current_status not in ("pending-release", "released"):
        return None, f"{entry_path}: status is not pending-release"

    commits = sorted(_parse_existing_commits(text))
    resolved_tag = _resolve_release_tag(worktree_root, candidate_tags, commits)

    sha = _tag_sha(worktree_root, resolved_tag)
    if not sha:
        # resolved_tag has no commit history yet — the release currently being cut,
        # not yet tagged (oracle: sha, date = merge_sha, merge_date fallback).
        sha = _current_head_sha(worktree_root) or ""
    date = _tag_date(worktree_root, sha) if sha else None
    if not date:
        date = _today_utc_date()

    fields = {
        "status": "released",
        "released_in": resolved_tag,
        "released_at": date,
        "released_sha": sha,
    }

    # Closure-captured state — locked_rmw's return value doesn't distinguish a
    # real flip from a skip (idiom: reconcile_completion_commits's `_state`,
    # plan_status_transition._stamp_implemented's `_state`).
    _state: dict = {"record": None, "skip_reason": None}

    def mutate(old_text: str) -> str:
        # Re-validate against the FRESH read (see docstring "Locking" section):
        # the git-resolved `fields` above were computed against the pre-lock
        # `text`, which may already be stale.
        fresh_status = _read_frontmatter_field(old_text, "status")
        if fresh_status not in ("pending-release", "released"):
            _state["skip_reason"] = f"{entry_path}: status is not pending-release"
            return old_text

        if fresh_status == "released":
            if (
                _read_frontmatter_field(old_text, "released_in") == resolved_tag
                and _read_frontmatter_field(old_text, "released_at") == date
                and _read_frontmatter_field(old_text, "released_sha") == sha
            ):
                # Idempotent no-op (port-time addition — see module docstring): a
                # second invocation re-resolving to the SAME fields writes nothing.
                _state["skip_reason"] = f"{entry_path}: already released (idempotent no-op)"
                return old_text
            # Already released with DIFFERENT fields than this invocation would
            # resolve (e.g. re-run against a different candidate_tags set, or a
            # concurrent writer already flipped it differently) — never
            # silently overwrite a prior release attribution; skip rather than
            # re-flip (DR-216 D2(iii) parity: no rewriting existing content).
            _state["skip_reason"] = (
                f"{entry_path}: already released with different fields — not re-flipping"
            )
            return old_text

        modified = _apply_flip_fields(old_text, fields)
        _state["record"] = {
            "path": str(entry_path),
            "released_in": resolved_tag,
            "released_at": date,
            "released_sha": sha,
        }
        return modified

    from coordinator_core.locked_write import LockTimeout, locked_rmw

    try:
        locked_rmw(entry_path, mutate, repo_root=worktree_root)
    except FileNotFoundError as exc:
        return None, f"{entry_path}: unreadable ({exc})"
    except LockTimeout as exc:
        return None, f"{entry_path}: timed out waiting for file lock ({exc})"

    return _state["record"], _state["skip_reason"]


def flip_completion_entries_to_released(
    worktree_root: str,
    entry_paths: list[str],
    candidate_tags: list[str],
) -> dict:
    """Resolve each entry to its earliest containing release tag and flip its frontmatter.

    Byte-parity port of the merging-to-main SKILL.md § Step 1.65 item 3 write pass.
    For every entry in ``entry_paths`` whose ``status:`` is ``pending-release``: resolve
    the earliest tag in ``candidate_tags`` (ordered oldest-first) whose history contains
    every commit in the entry's ``commits:`` list, then rewrite ``status``/
    ``released_in``/``released_at``/``released_sha`` in place.

    Idempotent (port-time addition — see module docstring): an entry already carrying
    the exact resolved fields is skipped without a write.

    Parameters:
        worktree_root:  Absolute path to the repo worktree root (git operations run here).
        entry_paths:    Completion-entry paths (``archive/completed/**/*.md`` or
                        ``docs/plans/*.md``) to consider — already containment-checked
                        by the caller/handler.
        candidate_tags: Ordered oldest-first release-tag names; the LAST element is
                        treated as the release currently being cut (fallback target
                        when no earlier tag contains an entry's commits).

    Returns:
        {"flipped": [{"path", "released_in", "released_at", "released_sha"}, ...],
         "skipped": [str, ...]}  — ``skipped`` entries are ``"<path>: <reason>"`` strings.

    Raises:
        ValueError: ``candidate_tags`` is empty (nothing to resolve against).
    """
    if not candidate_tags:
        raise ValueError(
            "completion.flip_to_released: candidate_tags must be non-empty"
        )

    root = Path(worktree_root)
    flipped: list[dict] = []
    skipped: list[str] = []

    for raw_path in entry_paths:
        record, skip_reason = _flip_one_entry(root, Path(raw_path), candidate_tags)
        if record is not None:
            flipped.append(record)
        else:
            skipped.append(skip_reason or f"{raw_path}: skipped")

    return {"flipped": flipped, "skipped": skipped}


@register_op("completion.flip_to_released")
async def _flip_to_released_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``completion.flip_to_released`` handler.

    MUTATING: resolves each caller-supplied completion entry to its earliest
    containing release tag and rewrites its frontmatter ``status``/``released_in``/
    ``released_at``/``released_sha`` fields in place. Blocking FS + git I/O runs via
    ``asyncio.to_thread``. Contract provisional pending DR-084 — see module docstring.

    Required params:
        entry_paths    (list[str]): completion-entry paths to consider.
        candidate_tags (list[str]): ordered oldest-first release-tag names; the LAST
            element is treated as the release currently being cut (fallback target).

    Returns:
        {"flipped": [{"path", "released_in", "released_at", "released_sha"}, ...],
         "skipped": [str, ...]}
        or ``{"error": str, "no_op": True}`` on a param/containment guard failure.

    ``repo_root`` is provided by the IPC engine (``_OP_KEY_SCOPE: common_dir``, tail
    pass) and used to derive the worktree root for both git resolution and the
    per-entry containment check — mirrors ``completion.reconcile_commits`` above
    (same allowed roots: ``archive/completed/`` and ``docs/plans/``).
    """
    raw_entry_paths = params.get("entry_paths", [])
    if not isinstance(raw_entry_paths, list):
        return {
            "error": "completion.flip_to_released: 'entry_paths' must be a list",
            "no_op": True,
        }

    raw_candidate_tags = params.get("candidate_tags", [])
    if not isinstance(raw_candidate_tags, list) or not raw_candidate_tags:
        return {
            "error": "completion.flip_to_released: 'candidate_tags' must be a non-empty list",
            "no_op": True,
        }

    if repo_root is None:
        return {
            "error": (
                "completion.flip_to_released: repo_root is required "
                "(no founding root available — handler called without socket-authoritative common_dir)"
            ),
            "no_op": True,
        }

    worktree = main_worktree_root(repo_root)

    # Containment: mirrors completion.reconcile_commits's dual-root allow-list
    # (archive/completed/ and docs/plans/) — see docs/problems/
    # 2026-07-08-op-family-path-containment-investigation.md § 4.
    allowed_roots = [worktree / "docs" / "plans", worktree / "archive" / "completed"]
    contained_entry_paths: list[str] = []
    skipped_uncontained: list[str] = []
    for raw_path in raw_entry_paths:
        if not isinstance(raw_path, str) or not raw_path:
            skipped_uncontained.append(f"{raw_path!r}: not a non-empty string")
            continue
        p = Path(raw_path)
        if not p.is_absolute():
            p = worktree / p
        contained = contained_path(p, allowed_roots)
        if contained is None:
            skipped_uncontained.append(
                f"{raw_path}: escapes docs/plans/ or archive/completed/"
            )
            continue
        contained_entry_paths.append(str(contained))

    if not contained_entry_paths:
        return {"flipped": [], "skipped": skipped_uncontained}

    try:
        result = await asyncio.to_thread(
            flip_completion_entries_to_released,
            worktree_root=str(worktree),
            entry_paths=contained_entry_paths,
            candidate_tags=[str(t) for t in raw_candidate_tags],
        )
    except ValueError as exc:
        return {"error": f"completion.flip_to_released: {exc}", "no_op": True}

    result["skipped"] = [*skipped_uncontained, *result["skipped"]]
    return result


# ---------------------------------------------------------------------------
# completion.day_coverage_sweep — reverse (commit -> entry) membership sweep
# ---------------------------------------------------------------------------
# Purpose: every existing writer in this module (resolve_chain_commits /
# _collect_chain_session_ids) is PULL-side — it starts from an entry that
# already exists and walks outward for commits to fold in. Nothing here ever
# starts from a commit and asks which entry (if any) should own it. This is
# the missing REVERSE direction: a flat, read-only membership test of "does
# any archive/completed/**/*.md commits: list already claim this SHA",
# day-bounded rather than merge_base..HEAD-bounded (the latter is
# branch-divergence-scoped and silently shrinks as origin/main advances —
# see day_coverage_sweep's own docstring). READ-ONLY: writes no commits:
# field, mutates nothing on disk.
#
# NOT wired through @register_op / the JSON-RPC op-classification quad
# (op_scopes.py / authz/classification.py / ops/_registry_map.py /
# authz/registration_quad.py) — those four files are outside this chunk's
# write-scope. Precedent for a read-only op living outside that machinery:
# coordinator_core.ops.query_completions.main, invoked directly from its CLI
# trampoline (coordinator/bin/query-completions.py) rather than via
# cc_invoke.route()/route_mutation(). day-coverage-sweep.py follows the same
# shape — direct import, no IPC dispatch.
# ---------------------------------------------------------------------------

#: A Session-Id trailer line within a commit body (git trailer convention —
#: same key this module's chain-widening already greps for via
#: ``git log --grep=^Session-Id: <sid>$``, see ``_collect_session_log``).
_SESSION_ID_TRAILER_RE = re.compile(r"^Session-Id:\s*(\S+)", re.MULTILINE)

#: Calendar-day argument shape for ``day_coverage_sweep`` (``YYYY-MM-DD``).
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Commit subject emitted by the cross-repo memo DELIVERY path — literally
#: ``f"cross-repo: deliver {title} memo from {sender}"`` in
#: ``coordinator_core.ops.fleet.memo_send._commit_delivered_memo``. A commit
#: carrying this subject IN THIS TREE was produced by a SIBLING repo's engine
#: committing across the tree boundary: claude-klabauter's own sends land in the
#: receiver's tree, never here, so the sender slug is by construction foreign
#: (empirically: claude-central-em, coordinator-claude-em, example-cockpit-repo-em,
#: example-retrieval-repo-em, example-market-data-repo-em, example-retrieval-repo-ue-addon-em,
#: example-game-repo-em, example-store-repo-em — never a claude-klabauter slug across all 602
#: delivery commits in history).
_FOREIGN_DELIVERY_SUBJECT_RE = re.compile(r"^cross-repo: deliver .+ memo from \S+$")

#: The only tree the delivery path writes: ``_commit_delivered_memo`` runs a
#: pathspec-scoped ``git commit -- <memo_relpath>`` where ``memo_relpath`` is
#: the inbox file it just created. Used as the STRUCTURAL half of the
#: foreign-authored predicate, so a subject string alone can never classify.
_FOREIGN_DELIVERY_PATH_PREFIX = "cross-repo/inbox/"

#: Registry key prefix under which the machine-local registry declares every
#: fleet repo root it knows (``"repos.example_doctrine_repo" = '/Users/…/coordinator-claude'``).
#: Read through ``machine_resolver`` — the sanctioned cross-repo path-resolution
#: substrate — never a hardcoded sibling path or a ``__file__``-relative walk.
_REGISTRY_REPO_KEY_PREFIX = "repos."

#: Session-scoped artifact directory a coordinator session leaves in the repo it
#: is HOMED in: one subdirectory per session, named with the session's FULL id
#: (``state/subagent-share/<session-id>/``, the DR-091 reviewer-sidecar home).
_SESSION_SHARE_REL = ("state", "subagent-share")

#: Ceremony records, whose filenames are ``<session-id-prefix>-<UTC-stamp>.json``
#: under a per-ceremony subdirectory (``state/ceremony/wsc/<prefix>-<stamp>.json``).
_SESSION_CEREMONY_REL = ("state", "ceremony")

#: Length of the session-id prefix a ceremony filename carries — the first 12
#: characters of the UUID, i.e. through the end of its second group
#: (``468b3c12-ad00-…`` -> ``468b3c12-ad0``). 48 bits of the id, so a
#: cross-session prefix collision is not a practical concern; ambiguous
#: prefixes are dropped rather than guessed (see ``_sibling_homed_session_ids``).
_CEREMONY_SID_PREFIX_LEN = 12


def _registry_repo_roots() -> List[Path]:
    """Every repo root the machine-local registry declares under ``repos.*``.

    File precedence mirrors ``machine_resolver.registry_get``
    (``registry.local.toml`` shadows ``registry.toml``); enumeration is by key
    prefix rather than a per-key ``registry_get`` call because the caller wants
    the whole fleet, not one named sibling. Returns [] when the registry is
    absent or unreadable — ``load_flat_registry_file`` already degrades a
    missing or malformed TOML to an empty dict, so no repo-resolution failure
    can raise out of a read-only diagnostic.
    """
    reg_dir = registry_dir()
    roots: dict = {}
    for fname in ("registry.local.toml", "registry.toml"):
        for key, value in load_flat_registry_file(reg_dir / fname).items():
            if not key.startswith(_REGISTRY_REPO_KEY_PREFIX):
                continue
            if key in roots or not isinstance(value, str) or not value:
                continue
            roots[key] = Path(value)
    return list(roots.values())


def _sibling_homed_session_ids(
    worktree_root: Path, session_ids: Set[str]
) -> Set[str]:
    """Identify which of ``session_ids`` are homed in a SIBLING fleet repo.

    A coordinator session leaves session-scoped artifacts — a
    ``state/subagent-share/<session-id>/`` directory, a
    ``state/ceremony/<kind>/<session-id-prefix>-<stamp>.json`` record — in the
    repo it is homed in, i.e. the tree it was launched against. A session homed
    in coordinator-claude that commits into claude-klabauter's tree therefore leaves those
    artifacts in coordinator-claude's tree, not here. Since completion entries are single-repo
    by construction (``completion.reconcile_commits`` enumerates a session's
    commits via ``git log --grep`` against ``main_worktree_root(repo_root)``
    only, and an entry is written at the session's own workstream-complete in
    its own home repo), claude-klabauter owes no completion entry for such a session and
    none will ever exist.

    PREDICATE — positive evidence in a sibling tree, and nothing else:
    a session id is classified sibling-homed only when a registry-declared repo
    OTHER than this one carries one of the two session-scoped artifacts above
    for that exact id. Presence of the same session's artifacts in THIS tree is
    deliberately NOT disqualifying: a sibling-homed session that dispatches a
    subagent while working in claude-klabauter's tree provisions a share directory here
    too (4 of 95 sibling-homed ids over 2026-07-26..29 did exactly that), so
    treating a local footprint as proof of local homing would misclassify them.

    Why NOT the inverse test, "absent from this repo's own ``state/`` tree":
    it is the tempting cheap predicate and it fails OPEN. 36 of 85 claude-klabauter-homed
    orphan session ids over 2026-07-26..29 left no ``state/`` footprint at all
    (a session that dispatches no subagent and runs no ceremony leaves none),
    so absence-of-local-footprint would relabel 36 genuine orphans as somebody
    else's problem — the exact failure this partition exists to avoid, and
    strictly worse than the honest over-count it replaces.

    Why NOT the commit trailers: they carry no home-repo marker. Across the
    same window the trailer key sets of the two populations differ only in
    incidental content (``Deliverable-Id`` appears on 75 of 94 sibling-homed
    orphan commits and 6 of 87 claude-klabauter-homed ones — a baton marker, not a home
    marker). Nothing in the trailer block names the authoring session's tree.
    Making this classifiable from the commit alone needs a new trailer, which
    is a fleet-wide contract change, not a local one.

    Why NOT ``~/.claude/projects/<mangled-repo-path>/<session-id>.jsonl``, the
    transcript directory that made the population visible in the first place:
    it is Claude Code's private machine-local layout, outside every repo, on a
    path a portable cross-platform engine must not assume exists. A
    registry-resolved sibling repo read is a coordinator-owned artifact shape
    on a coordinator-sanctioned resolution path, which this engine already
    depends on elsewhere.

    FAILS CLOSED at every rung: an unreadable registry, a sibling clone that
    is not present on this machine, or an unreadable ``state/`` tree all yield
    fewer classifications, leaving the affected commits in ``orphaned`` — an
    honest over-count, never a silent exoneration.

    Args:
        worktree_root: this repo's root — already a worktree root, not a git
            common dir, so it is resolved directly rather than through
            ``main_worktree_root`` (whose contract is ``common_dir.parent``).
            Excluded from the sibling scan by resolved-path identity, so this
            repo's own registry entry can never classify its own sessions.
        session_ids: the candidate ids to classify (the sweep passes only the
            ids that would otherwise be reported orphaned).

    Returns:
        The subset of ``session_ids`` with sibling-tree evidence. Empty set
        when there is none, or when nothing resolves.
    """
    if not session_ids:
        return set()

    self_root = worktree_root.resolve()

    # Ambiguous prefixes are dropped rather than guessed — a ceremony filename
    # cannot distinguish two ids sharing their first 12 characters.
    by_prefix: dict = {}
    for sid in session_ids:
        by_prefix.setdefault(sid[:_CEREMONY_SID_PREFIX_LEN], []).append(sid)
    unique_prefixes = {p: ids[0] for p, ids in by_prefix.items() if len(ids) == 1}

    sibling_homed: Set[str] = set()
    for repo_root in _registry_repo_roots():
        try:
            if repo_root.resolve() == self_root or not repo_root.is_dir():
                continue
        except OSError:
            continue

        share_dir = repo_root.joinpath(*_SESSION_SHARE_REL)
        try:
            for child in share_dir.iterdir():
                if child.name in session_ids and child.is_dir():
                    sibling_homed.add(child.name)
        except OSError:
            pass

        ceremony_dir = repo_root.joinpath(*_SESSION_CEREMONY_REL)
        try:
            kinds = list(ceremony_dir.iterdir())
        except OSError:
            kinds = []
        for kind in kinds:
            try:
                records = list(kind.iterdir())
            except OSError:
                continue
            for record in records:
                sid = unique_prefixes.get(record.name[:_CEREMONY_SID_PREFIX_LEN])
                if sid is not None and record.name[_CEREMONY_SID_PREFIX_LEN:].startswith("-"):
                    sibling_homed.add(sid)

    return sibling_homed


def _day_commit_log(worktree_root: Path, day: str) -> List[Tuple[str, Optional[str]]]:
    """Enumerate (full_sha, session_id_or_None) for every HEAD commit whose
    COMMIT date (not author date) falls within the UTC calendar day ``day``.

    DAY-BOUNDED against ``day``'s UTC midnight-to-midnight window —
    deliberately NOT ``merge_base..HEAD``-bounded the way
    ``resolve_chain_commits`` scopes its own git-log calls. That bound is
    branch-divergence-scoped: it silently narrows every time ``origin/main``
    advances, which is exactly the blind spot this sweep exists to avoid.

    Review: code-reviewer — F3. Plain ``--since``/``--until`` is NOT used to
    bound the walk: git's revision walk assumes roughly-monotonic commit
    dates and can terminate early (or skip) once it believes it has passed
    the ``--since`` threshold, which silently drops commits on a branch with
    non-monotonic dates — a merge pulling in commits from a diverged branch,
    or clock skew across the ~8 concurrently-committing peer sessions this
    repo runs under. That's exactly the "silently shrinking bound" failure
    class this sweep exists to replace (see module header re:
    ``merge_base..HEAD``). ``--since-as-filter`` (git >= 2.13) is a pure
    output filter that never affects which commits are walked — it never
    early-terminates — so it is used here as a coarse pre-filter only. The
    Python-side ``day_start``/``day_end`` bound below is the actual
    correctness guarantee (never trusting ``--since``/``--until`` alone),
    applied to the parsed strict-ISO commit date (``%cI``) of every walked
    commit.

    Commit-date (not author-date) and UTC are the chosen axis/timezone —
    consistent with this module's other git-log consumers
    (``_collect_session_log``, ``resolve_chain_commits``), none of which
    reference author-date anywhere. Returns [] (never raises) when the day
    has zero commits or the underlying ``git log`` call fails — mirrors this
    module's git-command WARN-not-raise posture throughout
    (``_canonicalize_stored_shas``, ``resolve_chain_commits``'s merge-base
    probe).
    """
    day_start = datetime.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc
    )
    day_end = day_start + datetime.timedelta(days=1)

    result = _reality_git(
        worktree_root,
        [
            "log",
            "HEAD",
            f"--since-as-filter={day} 00:00:00 +0000",
            f"--until={day} 23:59:59 +0000",
            "--pretty=format:%H%x1f%cI%x00%B%x03",
        ],
    )
    if result.returncode != 0 or not result.stdout:
        return []

    pairs: List[Tuple[str, Optional[str]]] = []
    for record in result.stdout.split("\x03"):
        record = record.strip("\n")
        if not record:
            continue
        head, _, body = record.partition("\x00")
        sha, _, commit_date_iso = head.partition("\x1f")
        sha = sha.strip()
        if not sha:
            continue
        try:
            commit_dt = datetime.datetime.fromisoformat(commit_date_iso.strip())
        except ValueError:
            # Unparseable commit date: don't silently drop a commit whose
            # SHA git *did* return — fail open (include it) rather than
            # trusting an unbounded string comparison.
            commit_dt = None
        if commit_dt is not None:
            commit_dt = commit_dt.astimezone(datetime.timezone.utc)
            if not (day_start <= commit_dt < day_end):
                continue
        match = _SESSION_ID_TRAILER_RE.search(body)
        pairs.append((sha, match.group(1) if match else None))
    return pairs


def _foreign_delivery_commits(
    worktree_root: Path, day: str, day_shas: Set[str]
) -> Set[str]:
    """Identify the commits on ``day`` a sibling repo's engine authored.

    PREDICATE — conjunction of two facts, both required:
      1. the subject is the delivery subject claude-klabauter's own memo-send engine
         generates (``_FOREIGN_DELIVERY_SUBJECT_RE``), and
      2. every path the commit touches is under ``cross-repo/inbox/``
         (``_FOREIGN_DELIVERY_PATH_PREFIX``) — the pathspec
         ``_commit_delivered_memo`` scopes its commit to.

    Why NOT "has no ``Session-Id:`` trailer", the tempting structural fact:
    it is a strictly WIDER set and using it would hide real orphans. Over
    2026-07-26..29, 208 commits carry no trailer, but only 171 are
    deliveries; the other 37 are claude-klabauter's own commits that simply lost the
    trailer (subjects like ``install:``, ``dogfood:``, ``fleet:``, ``F7b:``).
    Those 37 are exactly the genuine orphans the sweep exists to surface, so
    absence-of-trailer is corroborating evidence, never the test.

    Why NOT the subject regex alone: a subject is free text any session can
    write, and the memo title inside it is caller-supplied. Pairing it with
    the touched-path set makes the predicate structural — a commit is
    classified foreign only if it looks like a delivery AND touched nothing
    but the inbox, which is what the delivery path can physically produce.

    Presence of a trailer is deliberately NOT disqualifying. Before the
    all-hooks-off delivery mechanism landed, the receiver's own
    ``prepare-commit-msg`` hook injected a FALSE ``Session-Id`` onto foreign
    deliveries (see ``memo_send``'s module docstring); 172 historical
    delivery commits carry one. Those commits are still foreign-authored and
    still have no claude-klabauter session behind them.

    Merge commits are excluded by construction — ``--name-only`` emits no
    paths for them, and an empty path set is never treated as satisfying
    condition 2.

    Args:
        worktree_root: repo root (git worktree).
        day: ``YYYY-MM-DD`` — the same UTC calendar day ``_day_commit_log``
            walked.
        day_shas: the in-day SHA set ``_day_commit_log`` already resolved.
            That helper stays the sole authority on day membership (it
            applies the Python-side ``%cI`` bound this call does not); this
            function only annotates SHAs it already returned.

    Returns:
        The subset of ``day_shas`` that is foreign-authored. Returns an
        empty set (never raises) when the git call fails — mirrors this
        module's WARN-not-raise posture, and failing to the empty set keeps
        those commits in the orphaned bucket rather than silently hiding
        them.
    """
    if not day_shas:
        return set()

    result = _reality_git(
        worktree_root,
        [
            "log",
            "HEAD",
            f"--since-as-filter={day} 00:00:00 +0000",
            f"--until={day} 23:59:59 +0000",
            "--name-only",
            "--pretty=format:%x03%H%x1f%s",
        ],
    )
    if result.returncode != 0 or not result.stdout:
        return set()

    foreign: Set[str] = set()
    for chunk in result.stdout.split("\x03"):
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        sha, _, subject = lines[0].partition("\x1f")
        sha = sha.strip()
        if sha not in day_shas:
            continue
        if not _FOREIGN_DELIVERY_SUBJECT_RE.match(subject.strip()):
            continue
        paths = [line for line in lines[1:] if line.strip()]
        if not paths:
            continue
        if all(path.startswith(_FOREIGN_DELIVERY_PATH_PREFIX) for path in paths):
            foreign.add(sha)
    return foreign


def day_coverage_sweep(worktree_root: Path, day: str) -> dict:
    """Flat reverse (commit -> entry) membership sweep for one calendar day.

    For every commit landing on ``day`` (UTC, commit-date — see
    ``_day_commit_log``), tests flat membership against every ``commits:``
    list under ``archive/completed/**/*.md`` — the missing reverse direction
    this module never had (every existing writer pulls commits toward an
    entry that already exists; nothing here previously started from a commit
    and asked which entry should own it). This is a flat set-membership test,
    NOT a per-entry chain derivation (``resolve_chain_commits`` /
    ``_collect_chain_session_ids`` already own that pull-side shape).

    Unclaimed commits are partitioned four ways:
      - FOREIGN-AUTHORED: a sibling repo's engine committed the commit
        directly into this tree while delivering a cross-repo memo. No
        claude-klabauter session authored it, so no completion entry can ever claim
        it and none is expected — see ``_foreign_delivery_commits`` for the
        predicate and why absence of a ``Session-Id:`` trailer is NOT it.
        Tested BEFORE the trailer-driven buckets below, because the
        pre-hooks-off delivery mechanism injected false trailers onto some
        foreign commits; tested AFTER ``claimed``, because an entry that
        literally lists the SHA is ground truth either way.
      - RECOVERABLE: the commit carries a ``Session-Id:`` trailer matching
        a known entry's ``authored_by`` or ``chain`` frontmatter value —
        i.e. an entry that could plausibly claim it already exists.
      - IN-FLIGHT: the commit's ``Session-Id:`` matches an *open* handoff's
        ``claimed_by`` (or legacy ``consumed_by``) field — work is still
        underway, no completion entry is expected yet.
      - SIBLING-HOMED: the commit's ``Session-Id:`` belongs to a session homed
        in another fleet repo, which committed into this tree. Completion
        entries are single-repo by construction, so this repo owes no entry —
        see ``_sibling_homed_session_ids`` for the predicate and for why the
        commit trailers, the local-footprint inverse, and Claude Code's
        transcript directory were each rejected as the signal. Tested LAST of
        the four, after ``recoverable`` and ``in_flight``: a trailer matching
        one of THIS repo's own entries or open handoffs is local positive
        evidence of local ownership and outranks sibling-tree evidence, so this
        partition only ever reclassifies what would otherwise be orphaned.
      - GENUINELY ORPHANED: none of the above — no trailer, or a trailer
        matching nothing known, on a commit some claude-klabauter session did author.

    READ-ONLY: performs zero writes. Does not fold anything into any
    entry's ``commits:`` list, mutate any handoff, or run any mutating git
    verb — a pure diagnostic composing this module's existing read-only git
    subprocess pattern (``_reality_git``) and file-scan helpers (``_iter_files``,
    ``_parse_existing_commits``, ``_read_frontmatter_field``,
    ``_canonicalize_stored_shas``, ``_resolve_handoff_dirs``).

    Args:
        worktree_root: repo root (git worktree), same noun as every other
            git-resolution entry point in this module.
        day: ``YYYY-MM-DD`` (UTC calendar day to sweep).

    Returns:
        {
          "day": str,
          "total_commits": int,
          "claimed_count": int,
          "unclaimed_count": int,
          "foreign": [full_sha, ...],
          "recoverable": [full_sha, ...],
          "in_flight": [full_sha, ...],
          "sibling_homed": [full_sha, ...],
          "orphaned": [full_sha, ...],
          "foreign_count": int,
          "recoverable_count": int,
          "in_flight_count": int,
          "sibling_homed_count": int,
          "orphaned_count": int,
        }

        ``claimed_count`` plus the five partition counts reconcile exactly
        against ``total_commits``, which stays the true day total.

    Raises:
        ValueError: ``day`` does not match ``YYYY-MM-DD``.
    """
    if not _DAY_RE.match(day):
        raise ValueError(f"day_coverage_sweep: day must be YYYY-MM-DD: {day!r}")

    day_commits = _day_commit_log(worktree_root, day)

    completed_root = worktree_root / "archive" / "completed"
    all_stored_shas: Set[str] = set()
    known_session_ids: Set[str] = set()
    for entry_path in _iter_files(completed_root):
        try:
            text = entry_path.read_text(encoding="utf-8")
        except OSError:
            continue
        all_stored_shas.update(_parse_existing_commits(text))
        authored_by = _read_frontmatter_field(text, "authored_by")
        if authored_by and authored_by != "null":
            known_session_ids.add(authored_by)
        chain = _read_frontmatter_field(text, "chain")
        if chain and chain != "null":
            known_session_ids.add(chain)

    claimed_full, _canon_warnings = _canonicalize_stored_shas(
        worktree_root, sorted(all_stored_shas)
    )

    # Ledger-first (coordinator_core.claim_state.resolve_claim_state): a
    # branch-switch-reverted frontmatter mirror (the incident that module's
    # docstring names) must not make an actively-claimed baton look
    # unclaimed here — that is the false GENUINELY-ORPHANED alarm this read
    # exists to close. Only widens what counts as claimed (never narrows):
    # a live ledger holder is added to ``open_claimed_by`` in addition to
    # whatever the raw mirror already contributed, so a commit that would
    # otherwise land in ``orphaned`` can be reclassified ``in_flight``, but
    # nothing already claimed/foreign/recoverable/sibling-homed loses that
    # status and no commit gains ``orphaned`` status it didn't already have.
    open_claimed_by: Set[str] = set()
    handoff_dirs, _handoff_dirs_warning = _resolve_handoff_dirs(worktree_root)
    open_handoff_dir = handoff_dirs[0]  # state/handoffs (or meta-repo central root)
    for hf_path in _iter_files(open_handoff_dir):
        try:
            text = hf_path.read_text(encoding="utf-8")
        except OSError:
            continue
        claimed_by = _read_frontmatter_field(text, "claimed_by") or _read_frontmatter_field(
            text, "consumed_by"
        )
        if claimed_by and claimed_by != "null":
            open_claimed_by.add(claimed_by)
        try:
            claim_state = resolve_claim_state(hf_path, repo_root=worktree_root)
        except Exception:
            # Fail-closed-to-mirror: an unreadable/errored ledger resolution
            # is not evidence of a live claim — the raw mirror read above
            # (already folded in) stays the only signal for this handoff.
            claim_state = None
        if claim_state is not None and claim_state.holder:
            open_claimed_by.add(claim_state.holder)

    foreign_shas = _foreign_delivery_commits(
        worktree_root, day, {sha for sha, _sid in day_commits}
    )

    # Only the ids that would otherwise be reported orphaned are candidates —
    # the sibling scan never gets a chance to override local positive evidence.
    orphan_candidate_ids = {
        session_id
        for sha, session_id in day_commits
        if session_id
        and sha not in claimed_full
        and sha not in foreign_shas
        and session_id not in known_session_ids
        and session_id not in open_claimed_by
    }
    sibling_homed_ids = _sibling_homed_session_ids(worktree_root, orphan_candidate_ids)

    claimed_count = 0
    foreign: List[str] = []
    recoverable: List[str] = []
    in_flight: List[str] = []
    sibling_homed: List[str] = []
    orphaned: List[str] = []

    for sha, session_id in day_commits:
        if sha in claimed_full:
            claimed_count += 1
            continue
        if sha in foreign_shas:
            foreign.append(sha)
        elif session_id and session_id in known_session_ids:
            recoverable.append(sha)
        elif session_id and session_id in open_claimed_by:
            in_flight.append(sha)
        elif session_id and session_id in sibling_homed_ids:
            sibling_homed.append(sha)
        else:
            orphaned.append(sha)

    return {
        "day": day,
        "total_commits": len(day_commits),
        "claimed_count": claimed_count,
        "unclaimed_count": (
            len(foreign)
            + len(recoverable)
            + len(in_flight)
            + len(sibling_homed)
            + len(orphaned)
        ),
        "foreign": foreign,
        "recoverable": recoverable,
        "in_flight": in_flight,
        "sibling_homed": sibling_homed,
        "orphaned": orphaned,
        "foreign_count": len(foreign),
        "recoverable_count": len(recoverable),
        "in_flight_count": len(in_flight),
        "sibling_homed_count": len(sibling_homed),
        "orphaned_count": len(orphaned),
    }
