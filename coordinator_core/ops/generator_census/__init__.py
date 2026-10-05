"""Declared GENERATES/MUTATES pair census, rebuilt under the 500ms brightline.

Requirement: a census of module-level GENERATES / MUTATES / MUTATES_APPEND /
GENERATES_EXTERNAL / UNSTAMPED_BY_DESIGN declarations and their validity
against the tracked tree. It never detects write behaviour.

Record contract: `Pair(generator, artifact, stamp_key, sources)` and
`GeneratorRecord(generator, pairs, verdict, detail, mutates=())`; every
`detail` string is byte-identical to the oracle fixture. Records group by
sweep dir (`coordinator/bin`, `bin`, `coordinator_core`), sorted by path
within each group.

Invariants (design decisions 4-6):
- Glob validity is prefix-bisect over sorted tracked paths with
  `fnmatchcase` semantics (case-sensitive). Rejections run in order: no match,
  catch-all, wildcard-free; runtime-ledger sets under `state/` or
  `.coordinator-local/` are exempt from no-match. Glob validity and `sources`
  existence are never cached.
- Records are byte-stable: field names, types, and order never change.
- Nothing spawns, warm or cold. `census` binds the in-process cold finder
  (`assemble.find_candidates`); `assemble(repo_root, *, cold, cache_dir)` never names it.
- Test scaffolding (tests/ dirs, `test_*.py`, `conftest.py`, `coordinator_core/testing/`,
  `coordinator_core/benchmarks/`) is outside the census.

Submodule APIs:
  tracked.read_tracked(repo_root) -> TrackedScope(
      paths: tuple[str, ...] (sorted repo-wide), scope: tuple[str, ...],
      clean: Mapping[str, str] (path -> blob sha), dirty: tuple[str, ...],
      index_source: str)
  globs.PathMatcher(sorted_paths).any_match(pattern) -> bool;
      globs.has_wildcard(p); globs.is_catch_all(p);
      globs.is_runtime_ledger(patterns)
  declarations.Declarations(generates, mutates, mutates_append,
      generates_external, unstamped_by_design): each None when absent, a
      MALFORMED marker when non-evaluable;
      declarations.extract(source: str, *, owned_constants: Mapping[str, str])
          -> Declarations | None;
      declarations.owned_constants(source_of_machinery_paths: str)
          -> dict[str, str]
  store.load(cache_dir, header) -> dict[str, Declarations | None];
      store.save(cache_dir, header, entries) -> None;
      store.extractor_digest() -> str
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.generator_census.records import GeneratorRecord, Pair

__all__ = ["GeneratorRecord", "Pair", "census"]


def census(repo_root: Path) -> list[GeneratorRecord]:
    """Return the declared-pair records for `repo_root`, binding the cold finder and machinery cache dir."""
    from coordinator_core.ops.generator_census.assemble import assemble, find_candidates
    from coordinator_core.session import machinery_paths

    return assemble(
        repo_root, cold=find_candidates, cache_dir=machinery_paths.cache_dir(str(repo_root))
    )
