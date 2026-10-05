"""The declared-pair census: tracked scope, blob-keyed declarations, one record per declaring module.

`assemble` answers "what record does each declaring module get?". It never detects write
behaviour and never names a candidate finder: the caller injects `cold`, the function that
narrows a set of cache-missing paths to those holding a column-0 declaration token.

Invariants:
    - Test scaffolding (tests/ dirs, `test_*.py`, `conftest.py`, `coordinator_core/testing/`,
      `coordinator_core/benchmarks/`) is outside the census; the cold finder is never offered it.
    - `cold` is called exactly once per run, with the sorted cache-missing clean paths (possibly
      none). Dirty paths are read from the worktree, never offered to `cold`, never cached.
    - Glob validity and `sources` existence are re-evaluated every run against the live tracked
      set and the filesystem; only extracted declarations are cached.
    - Records group by sweep dir, sorted by path within each group.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from coordinator_core.ops.generator_census import declarations, globs, store, tracked
from coordinator_core.ops.generator_census.records import GeneratorRecord, Pair
from coordinator_core.ops.staleness_git import Verdict

__all__ = ["assemble", "find_candidates"]

MACHINERY_PATHS = "coordinator_core/session/machinery_paths.py"
_EXCLUDED_DIRS = ("coordinator_core/testing/", "coordinator_core/benchmarks/")
_CANDIDATE = re.compile(
    rb"^(?:GENERATES|MUTATES|MUTATES_APPEND|GENERATES_EXTERNAL|UNSTAMPED_BY_DESIGN)"
    rb"[ \t]*(?::[^=\r\n]*)?=",
    re.MULTILINE,
)
_EXTERNAL_NOTE = "GENERATES_EXTERNAL (destination is caller-supplied and foreign to this repo)"

ColdFinder = Callable[[Path, Sequence[str]], Sequence[str]]


def find_candidates(repo_root: Path, missing: Sequence[str]) -> list[str]:
    """The paths among `missing` holding a column-0 declaration token; spawns nothing."""
    return [
        rel
        for rel in missing
        if (data := _read(repo_root, rel)) is not None
        and (b"GENERATES" in data or b"MUTATES" in data or b"UNSTAMPED_BY_DESIGN" in data)
        and _CANDIDATE.search(data)
    ]


def _is_test_scaffolding(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (
        "/tests/" in path
        or base.startswith("test_")
        or base == "conftest.py"
        or path.startswith(_EXCLUDED_DIRS)
    )


def _read(repo_root: Path, rel: str) -> bytes | None:
    try:
        with open(os.path.join(repo_root, rel), "rb") as f:
            return f.read()
    except OSError:
        return None


def _normalised(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _field(decls: Mapping[str, object], name: str) -> tuple[bool, object]:
    """(declared, value) of one declaration name."""
    return name in decls, decls.get(name)


def _str_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(s, str) and s for s in value)


def _pairs(rel: str, value: object, repo_root: Path) -> tuple[list[Pair], str | None]:
    if value == declarations.MALFORMED:
        return [], f"{rel} has a GENERATES assignment that is not a literal list"
    if not isinstance(value, list):
        return [], f"{rel} GENERATES is not a list"
    pairs = []
    for entry in value:
        if not isinstance(entry, dict):
            return [], f"{rel} GENERATES entry is not a mapping: {entry!r}"
        artifact = entry.get("artifact")
        if not isinstance(artifact, str) or not artifact:
            return [], f"{rel} GENERATES entry missing a valid artifact: {entry!r}"
        stamp_key = entry.get("stamp_key")
        if not isinstance(stamp_key, str) or not stamp_key:
            return [], f"{rel} GENERATES entry missing a valid stamp_key: {entry!r}"
        sources = entry.get("sources")
        if not _str_list(sources) or not all(
            os.path.exists(os.path.join(repo_root, s)) for s in sources  # type: ignore[union-attr]
        ):
            return [], (
                f"{rel} GENERATES entry for {artifact!r} has malformed sources "
                f"(empty, not a list, or naming an absent path): {sources!r}"
            )
        pairs.append(Pair(rel, artifact, stamp_key, tuple(sources)))  # type: ignore[arg-type]
    return pairs, None


def _mutates(rel: str, patterns: object, matcher: globs.PathMatcher) -> tuple[str, str]:
    """("ok" | "ledger", "") for a valid declaration, else ("bad", message)."""
    if not _str_list(patterns):
        return "bad", (
            f"{rel} has a MUTATES declaration that is not a non-empty list of non-empty "
            f"strings: {patterns!r}"
        )
    assert isinstance(patterns, list)
    if not matcher.matches_any(patterns):
        if globs.is_runtime_ledger(patterns):
            return "ledger", ""
        return "bad", f"{rel} MUTATES pattern(s) match no currently-tracked path: {patterns!r}"
    broad = [p for p in patterns if globs.is_catch_all(p)]
    if broad:
        return "bad", (
            f"{rel} MUTATES pattern(s) {broad!r} match the whole corpus; "
            "a pathspec needs a literal directory segment or file extension"
        )
    concrete = [p for p in patterns if not globs.has_wildcard(p)]
    if len(concrete) == 1:
        return "bad", (
            f"{rel} MUTATES pattern '{concrete[0]}' names a concrete path; "
            "a fixed artifact declares GENERATES"
        )
    if concrete:
        return "bad", (
            f"{rel} MUTATES pattern(s) {concrete!r} name a concrete path; "
            "a fixed artifact declares GENERATES"
        )
    return "ok", ""


def _record(
    rel: str, decls: Mapping[str, object], repo_root: Path, matcher: globs.PathMatcher
) -> GeneratorRecord | None:
    def undeclared(detail: str) -> GeneratorRecord:
        return GeneratorRecord(rel, (), Verdict.UNDECLARED, detail)

    has_gen, gen = _field(decls, "GENERATES")
    has_unstamped, unstamped = _field(decls, "UNSTAMPED_BY_DESIGN")
    has_mutates, mutates = _field(decls, "MUTATES")
    has_append, append = _field(decls, "MUTATES_APPEND")
    has_external, external = _field(decls, "GENERATES_EXTERNAL")

    pairs: list[Pair] = []
    if has_gen:
        pairs, error = _pairs(rel, gen, repo_root)
        if error:
            return undeclared(error)
    if has_unstamped and not _str_list(unstamped):
        return undeclared(
            f"{rel} UNSTAMPED_BY_DESIGN must be a non-empty list of glob strings: {unstamped!r}"
        )

    items: list[str] = []
    mutates_field: list[str] = []
    bad: list[str] = []
    mutates_item = ""
    if has_mutates:
        kind, message = _mutates(rel, mutates, matcher)
        if kind == "bad":
            bad.append(message)
        else:
            mutates_item = (
                f"runtime-ledger: MUTATES {mutates!r} matches no tracked path"
                if kind == "ledger"
                else f"MUTATES = {mutates!r}"
            )
            mutates_field = list(mutates)  # type: ignore[call-overload]
    append_item = ""
    if has_append:
        if _str_list(append) and not any(globs.has_wildcard(p) for p in append):  # type: ignore[union-attr]
            append_item = f"MUTATES_APPEND = {append!r} (append-only ledger or surgical edit)"
            mutates_field = list(append) + mutates_field  # type: ignore[call-overload]
        else:
            bad.append(
                f"{rel} MUTATES_APPEND must be a non-empty list of concrete (wildcard-free) "
                f"paths: {append!r}"
            )
    external_item = ""
    if has_external:
        if external is True:
            external_item = _EXTERNAL_NOTE
        else:
            bad.append(f"{rel} GENERATES_EXTERNAL must be the literal True: {external!r}")
    items = [i for i in (mutates_item, append_item, external_item) if i]

    if has_gen:
        notes = []
        if has_unstamped and gen:
            exempt = [
                p.artifact
                for p in pairs
                if any(fnmatch.fnmatchcase(p.artifact, g) for g in unstamped)  # type: ignore[union-attr]
            ]
            if exempt:
                pairs = [p for p in pairs if p.artifact not in exempt]
                notes.append(
                    f"UNSTAMPED_BY_DESIGN {unstamped!r} exempts {exempt!r} from staleness comparison"
                )
        if external_item:
            notes.append(external_item)
        notes += bad
        base = (
            f"{rel} declares GENERATES = [] (declared-empty, no artifacts)"
            if not gen
            else f"{rel} declares {len(pairs)} pair(s)"
        )
        return GeneratorRecord(
            rel, tuple(pairs), None, "; ".join([base, *notes]), tuple(mutates_field)
        )

    if bad:
        return undeclared(bad[0])
    if not items:
        return None
    suffix = (
        " (corpus mutator, no staleness contract)"
        if items == [mutates_item] and not mutates_item.startswith("runtime-ledger")
        else " (no staleness contract)"
    )
    return GeneratorRecord(
        rel,
        (),
        Verdict.MUTATES_DECLARED,
        f"{rel} declares " + "; ".join(items) + suffix,
        tuple(mutates_field),
    )


def _blank_to_none(result: object) -> object | None:
    if result is None or (isinstance(result, Mapping) and not result):
        return None
    return result


def assemble(repo_root: Path, *, cold: ColdFinder, cache_dir: str | os.PathLike) -> list[GeneratorRecord]:
    """Return one record per module that declares GENERATES/MUTATES provenance, in sweep order."""
    repo_root = Path(repo_root)
    scope = tracked.read_tracked(repo_root)
    swept = [p for p in scope.scope if not _is_test_scaffolding(p)]
    clean = {p: scope.clean[p] for p in swept if p in scope.clean}
    dirty = [p for p in scope.dirty if not _is_test_scaffolding(p)]

    machinery_sha = scope.clean.get(MACHINERY_PATHS)
    machinery_bytes: bytes | None = None
    if machinery_sha is None:
        machinery_bytes = _read(repo_root, MACHINERY_PATHS)
        machinery_sha = (
            "absent"
            if machinery_bytes is None
            else "dirty:" + hashlib.sha1(_normalised(machinery_bytes)).hexdigest()
        )

    owned: dict[str, str] | None = None

    def extract(data: bytes) -> object | None:
        nonlocal owned, machinery_bytes
        if owned is None:
            if machinery_bytes is None:
                machinery_bytes = _read(repo_root, MACHINERY_PATHS)
            owned = (
                {}
                if machinery_bytes is None
                else declarations.owned_constants(machinery_bytes.decode("utf-8", "replace"))
            )
        return _blank_to_none(declarations.extract(data.decode("utf-8", "replace"), owned))

    header = {
        "extractor": store.extractor_digest(),
        "py": list(sys.version_info[:2]),
        "machinery": machinery_sha,
    }
    cached = store.load(cache_dir, header)
    entries = {sha: cached[sha] for sha in set(clean.values()) if sha in cached}

    misses = sorted(p for p, sha in clean.items() if sha not in cached)
    candidates = set(cold(repo_root, misses))
    fresh: dict[str, object | None] = {}
    for rel in misses:
        sha = clean[rel]
        if rel in candidates and (data := _read(repo_root, rel)) is not None:
            fresh[sha] = extract(data)
    for rel in misses:
        if rel not in candidates:
            fresh.setdefault(clean[rel], None)
    entries.update(fresh)

    dirty_decls: dict[str, object] = {}
    for rel in dirty:
        data = _read(repo_root, rel)
        if data is not None and _CANDIDATE.search(data):
            found = extract(data)
            if found is not None:
                dirty_decls[rel] = found

    matcher = globs.PathMatcher(scope.paths)
    records = []
    for sweep_dir in tracked.SWEEP_DIRS:
        for rel in swept:
            if not rel.startswith(sweep_dir):
                continue
            decls = entries.get(clean[rel]) if rel in clean else dirty_decls.get(rel)
            if decls is None:
                continue
            record = _record(rel, decls, repo_root, matcher)
            if record is not None:
                records.append(record)

    if entries != cached:
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass
        store.save(cache_dir, header, entries)
    return records
