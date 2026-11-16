"""
Corpus lint: claude-klabauter's own state/handoffs/ + archive/handoffs/ must contain zero
dangling handoff lineage references.

Rationale: emit.cadence resolves every handoff's lineage fields (predecessor,
forked_from, origin_handoff, additional_predecessors[], and their ID companions
predecessor_id/origin_handoff_id) on EVERY run. A reference that can never
resolve — not on disk, not archived, not even in git history — costs a
tier-3 git-history subprocess probe on every single run forever, for no
payoff: nobody acts on it, because nothing surfaces it as a fact to fix. This
test is that surfacing: it runs once, at test time, and fails loud with the
offending file and the exact unresolvable value, so the rot gets fixed instead
of re-discovered at runtime cost on every cadence tick.

Reuses the corpus's own referential-integrity machinery rather than
reimplementing resolution:
  - coordinator_core.dag.check_lineage_reachability — PATH-field reachability
    (predecessor / forked_from / additional_predecessors[] / origin_handoff)
    via live-disk ∪ archive-on-disk ∪ git-history resolution. A target that
    was legitimately archived, or even later pruned from disk entirely but
    was once git-tracked, resolves via the archive-on-disk or git-history
    tier and is NOT a violation — only "provably never-existed" is.
  - coordinator_core.frontmatter.schema_validate._check_referential_integrity_id_refs
    — ID-field existence (predecessor_id / origin_handoff_id) against a local
    handoff_id index, plus the never-silently-disagree invariant.

Negative-spec: does NOT flag a reference to an archived handoff, nor a
path-field reference to a handoff since pruned from disk that was once
git-tracked (resolves via the git-history sentinel) — both are legitimate
history, not rot. Only flags a reference that is unresolvable in ALL
tiers (path fields) or absent from the on-disk handoff_id index (ID fields).
"""
from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.dag import build_git_history_cache, check_lineage_reachability
from coordinator_core.frontmatter.schema_validate import (
    _build_handoff_id_index,
    _check_referential_integrity_id_refs,
    parse_frontmatter,
)

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _iter_handoff_md_files() -> list[str]:
    """Every *.md under state/handoffs/ (flat) and archive/handoffs/ (recursive,
    flat AND month-foldered) — the same two shelves _build_handoff_id_index scans."""
    paths: list[str] = []
    for base in ('state/handoffs', 'archive/handoffs'):
        base_dir = _REPO_ROOT / base
        if not base_dir.is_dir():
            continue
        for root, _dirs, files in os.walk(base_dir):
            for name in sorted(files):
                if name.endswith('.md'):
                    paths.append(os.path.join(root, name))
    return sorted(paths)


def test_no_dangling_handoff_lineage_references_in_corpus():
    """Every predecessor / forked_from / origin_handoff / additional_predecessors[]
    reference in claude-klabauter's own handoff corpus must resolve to a real artifact —
    live, archived, or (for path fields) provably once-git-tracked."""
    repo_root = str(_REPO_ROOT)
    git_history_cache = build_git_history_cache(repo_root)

    failures: list[str] = []
    for abs_path in _iter_handoff_md_files():
        repo_rel = os.path.relpath(abs_path, repo_root).replace('\\', '/')
        content = Path(abs_path).read_text(encoding='utf-8')
        frontmatter = parse_frontmatter(content)['frontmatter']
        if not frontmatter:
            continue
        violations = check_lineage_reachability(
            frontmatter,
            repo_root,
            handoff_dir=os.path.dirname(abs_path),
            record_repo_rel_path=repo_rel,
            git_history_cache=git_history_cache,
        )
        for v in violations:
            failures.append(
                f'{repo_rel}: field "{v["field"]}" = "{v["value"]}" — {v["reason"]}'
            )

    assert not failures, (
        f'{len(failures)} dangling handoff lineage reference(s) found in claude-klabauter\'s '
        'own corpus (state/handoffs/ + archive/handoffs/). Point the field at an '
        'existing handoff, or clear it if the target was never authored:\n  '
        + '\n  '.join(failures)
    )


_ID_TO_PATH_COMPANION = {'predecessor_id': 'predecessor', 'origin_handoff_id': 'origin_handoff'}


def _id_warnings_without_path_companion(frontmatter: dict, warnings: list[dict]) -> list[dict]:
    """Dangling-ID warnings the record's own path companion does not cover.

    Corpus pruning deletes consumed handoffs, so an ID whose target was pruned
    no longer resolves in the on-disk index. The path field beside it still
    names the target, and test_no_dangling_handoff_lineage_references_in_corpus
    proves that path resolves (live, archived, or git history). An ID with no
    path companion has nothing else carrying its lineage and stays a finding.
    """
    kept = []
    for w in warnings:
        companion = _ID_TO_PATH_COMPANION.get(w['field'])
        value = frontmatter.get(companion) if companion else None
        if value is None or str(value).strip() in ('', 'none'):
            kept.append(w)
    return kept


def test_id_warning_filter_keeps_an_id_with_no_path_companion():
    warnings = [{'field': 'predecessor_id', 'error': 'x'}]
    assert _id_warnings_without_path_companion({'predecessor_id': 'hnd-x-000000'}, warnings) == warnings
    assert _id_warnings_without_path_companion(
        {'predecessor_id': 'hnd-x-000000', 'predecessor': 'none'}, warnings
    ) == warnings


def test_id_warning_filter_drops_an_id_whose_path_companion_is_set():
    warnings = [{'field': 'predecessor_id', 'error': 'x'}]
    fm = {'predecessor_id': 'hnd-x-000000', 'predecessor': '2026-08-01-x.md'}
    assert _id_warnings_without_path_companion(fm, warnings) == []


def test_no_dangling_handoff_id_references_in_corpus():
    """Every predecessor_id / origin_handoff_id reference must resolve to a
    known handoff_id in the local corpus, and must agree with its path-field
    companion when both are set (never-silently-disagree)."""
    repo_root = str(_REPO_ROOT)
    handoff_id_index = _build_handoff_id_index(repo_root)

    dangling: list[str] = []
    disagreeing: list[str] = []
    for abs_path in _iter_handoff_md_files():
        repo_rel = os.path.relpath(abs_path, repo_root).replace('\\', '/')
        content = Path(abs_path).read_text(encoding='utf-8')
        frontmatter = parse_frontmatter(content)['frontmatter']
        if not frontmatter:
            continue
        errors, warnings = _check_referential_integrity_id_refs(frontmatter, handoff_id_index)
        for w in _id_warnings_without_path_companion(frontmatter, warnings):
            dangling.append(f'{repo_rel}: field "{w["field"]}" — {w["error"]}')
        for e in errors:
            disagreeing.append(f'{repo_rel}: field "{e["field"]}" — {e["error"]}')

    assert not dangling, (
        f'{len(dangling)} dangling handoff_id reference(s) found in claude-klabauter\'s own '
        'corpus. The target is absent from the local handoff_id index — either it '
        'was never authored (repoint or clear the *_id field) or it was pruned '
        'from disk (null the *_id field; the path-field companion still carries '
        'the git-history-resolvable lineage):\n  ' + '\n  '.join(dangling)
    )
    assert not disagreeing, (
        f'{len(disagreeing)} predecessor/predecessor_id (or origin_handoff/'
        'origin_handoff_id) disagreement(s) found — the ID companion resolves to '
        'a DIFFERENT artifact than its path-field companion names:\n  '
        + '\n  '.join(disagreeing)
    )
