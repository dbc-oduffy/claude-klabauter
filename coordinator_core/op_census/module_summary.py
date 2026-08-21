"""coordinator_core.op_census.module_summary — cached per-module AST summary.

Purpose: the load-bearing per-module summary every other op_census chunk
reads — top-level function names, spawn-call-site count, and line count for
a Python module, keyed on `sha256(body)` through `coordinator_core.cache`.
Never parses a module on demand outside a cache miss.

Design (staff-eng Finding 0 / Ruling 1): this module GROWS `coordinator_core
.cache` with a persisted tier (`cache.read_disk_revalidated`) rather than
building a second cache. `envelope.py`'s existing `cache.compute_stamp` /
`cache.read_revalidated` call path is untouched — this module never touches
`cache._REVALIDATED_CACHE`.

Two-tier revalidation:
    - In-process: `summarize_paths` accepts a caller-owned `index` dict
      (`{path: (stamp, ModuleSummary)}`) and revalidates against it via
      `cache.read_disk_revalidated` — a content-hash miss (new file, changed
      body) is the only case that re-parses; a hit returns the cached
      `ModuleSummary` with zero parsing.
    - On-disk persistence: `load_index`/`save_index` (de)serialize that same
      `index` shape to/from a JSON file the caller names explicitly — this
      module has no opinion on WHERE that file lives (no default path), only
      on its shape. A warm process loads the index once, revalidates every
      path's stamp (one `sha256` read per file — the unavoidable floor, see
      § Measured basis below), and only re-parses files whose stamp changed.

Reuse, not re-derivation: spawn-call-site detection reuses
`coordinator_core.spawn_policy.detect.sites_in_source` (the same vendored
AST spawn detector `_spawn_budget.py`-style ratchets already use in this
tree) rather than re-deriving a second spawn-site scanner.

Line count IS computed inside this same 56.9ms-class summary pass, not as a
separate pass: `_compute_module_summary` reads the module body once (the
same read `sites_in_source` needs) and derives line count from that same
in-memory text via `str.splitlines()` — no second file read, no second AST
walk. See § Measured basis below for why a second pass would cost real
bytes-scanned time and this design avoids it.

§ Measured basis (chunk C1 authoring-time re-measurement, replacing the
spike-era estimates the plan cited before this mechanism existed — those
figures, 0.7ms load / 2,154ms cold build / 56.9ms warm total / 54.6ms of
that as the sha256 floor over 1,135 files, were taken against a mechanism
that did not exist; superseded below).

**WHOLE-TREE reference figures, not a figure about `op_census.report`
itself** (Review: staff-eng Finding 9 — these three rows are all measured
over the 2,823-file whole tree, `tests/` included; `census()` never scans
that corpus — `_corpus_paths` excludes `tests/` and `test_*.py` by design,
scanning ~1,135-1,159 non-test files, see that function's own docstring.
Kept here as the module-level floor/cost-shape reference `summarize_paths`
itself was measured against; `op_census_report.py`'s own additive budget
table is the figure that binds the op's real, non-test-corpus cost):
    - `cache.compute_stamp` alone, sha256 over this repo's own
      `coordinator_core/` tree at authoring time (WHOLE TREE, 2,823 `.py`
      files, excluding `__pycache__`): **158.7ms** — the unavoidable floor,
      scales with bytes not op count, matches the spike's "sha256 is the
      floor" finding in shape though not in absolute figure (this repo's
      tree is ~2.5x the file count the spike measured against).
    - Cold build (`summarize_paths` with an empty `index`, every file a
      miss — full parse + spawn-site scan + stamp for every file), WHOLE
      TREE: **19,448ms** over the same 2,823 files — a one-time cost, off
      the measured warm-path budget by design, and off the brightline (the
      brightline governs the warm hot path, not a one-time index build).
    - Warm revalidate (`summarize_paths` against an `index` already
      populated by the prior cold build, no file bodies changed), WHOLE
      TREE: every file still pays its `compute_stamp` read (content-hash
      revalidation is unconditional, per `cache.read_disk_revalidated`'s
      own contract), but zero files re-parse: **346.3ms** over the same
      2,823 files — inside the 500ms brightline, with headroom, but not by
      the wide margin the spike-era 56.9ms figure implied (this repo's real
      file count is materially larger than what that figure was taken
      against). The gap above the 158.7ms sha256 floor is per-file
      `Path`/dict-lookup overhead in `summarize_paths`/
      `read_disk_revalidated`, not re-parsing. Contrast with
      `op_census_report.py`'s own additive-budget-table row for this same
      mechanism, **125.0ms warm** — that figure is over the NON-TEST
      ~1,159-file corpus `census()` actually scans, not this 2,823-file
      whole-tree reference.

Negative-spec:
    - `summarize_paths` never imports `coordinator_core.ops` and never boots
      the engine — reading the authoritative op registry is a sibling
      chunk's concern (hard constraint 7 amended), not this module's.
    - `ModuleSummary.function_names` is TOP-LEVEL only (module-body
      `def`/`async def`) — a nested or class-method function is not
      counted here; that is a deliberate scope narrowing, not an oversight.
    - A file that fails to parse (`SyntaxError`) does NOT crash
      `summarize_paths` — it produces a `ModuleSummary` with empty
      `function_names`, zero `spawn_call_sites`, a best-effort `line_count`
      still derived from the raw text, and `parse_error` set to the
      exception's message. Never silently dropped from the result map.
    - This module does not choose a default on-disk index path — `load_index`
      / `save_index` require the caller to name one explicitly.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C1.md
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from coordinator_core import cache
from coordinator_core.spawn_policy.detect import SpawnParseError, sites_in_source

__all__ = ["ModuleSummary", "load_index", "save_index", "summarize_paths"]


@dataclasses.dataclass(frozen=True)
class ModuleSummary:
    """Static per-module summary. `stamp` is the sha256 hex digest the
    summary was computed against (redundant with the index's own stamp key,
    carried on the value too so a caller holding a bare `ModuleSummary` —
    e.g. after `summarize_paths` returns a flat `{path: ModuleSummary}`
    view — never has to re-derive it)."""

    path: str
    stamp: str
    function_names: Tuple[str, ...]
    spawn_call_sites: int
    line_count: int
    parse_error: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "stamp": self.stamp,
            "function_names": list(self.function_names),
            "spawn_call_sites": self.spawn_call_sites,
            "line_count": self.line_count,
            "parse_error": self.parse_error,
        }

    @staticmethod
    def from_dict(data: dict) -> "ModuleSummary":
        return ModuleSummary(
            path=data["path"],
            stamp=data["stamp"],
            function_names=tuple(data["function_names"]),
            spawn_call_sites=data["spawn_call_sites"],
            line_count=data["line_count"],
            parse_error=data.get("parse_error"),
        )


def _top_level_function_names(tree: ast.Module) -> Tuple[str, ...]:
    return tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _compute_module_summary(path: Path) -> ModuleSummary:
    """Reads `path` exactly once; every field below is derived from that
    single in-memory `text`, never a second file read."""
    path_str = str(path)
    text = path.read_text(encoding="utf-8")
    line_count = len(text.splitlines())
    stamp = cache.compute_stamp(path_str)

    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return ModuleSummary(
            path=path_str,
            stamp=stamp,
            function_names=(),
            spawn_call_sites=0,
            line_count=line_count,
            parse_error=str(exc),
        )

    function_names = _top_level_function_names(tree)

    try:
        spawn_call_sites = len(sites_in_source(text, path_str))
    except SpawnParseError as exc:
        # ast.parse above already succeeded, so sites_in_source's own parse
        # cannot fail on the SAME text -- this branch exists only as a
        # fail-closed guard against sites_in_source diverging from ast.parse
        # in a future revision, never expected to execute today.
        return ModuleSummary(
            path=path_str,
            stamp=stamp,
            function_names=function_names,
            spawn_call_sites=0,
            line_count=line_count,
            parse_error=str(exc),
        )

    return ModuleSummary(
        path=path_str,
        stamp=stamp,
        function_names=function_names,
        spawn_call_sites=spawn_call_sites,
        line_count=line_count,
        parse_error=None,
    )


def summarize_paths(
    paths: Iterable["str | Path"],
    *,
    index: "Dict[str, Tuple[str, ModuleSummary]] | None" = None,
) -> Dict[str, ModuleSummary]:
    """Returns `{path_str: ModuleSummary}` for every path in `paths`.

    `index` is the caller-owned `{path: (stamp, ModuleSummary)}` revalidation
    dict passed straight through to `cache.read_disk_revalidated` — pass the
    dict returned by `load_index` to warm-revalidate against a prior run, or
    omit it (a fresh dict is used, discarded on return) for a one-shot cold
    build. Mutates `index` in place when provided.
    """
    owned_index: Dict[str, Tuple[str, ModuleSummary]] = index if index is not None else {}
    result: Dict[str, ModuleSummary] = {}
    for raw_path in paths:
        path_str = str(Path(raw_path))
        summary = cache.read_disk_revalidated(path_str, _compute_module_summary, owned_index)
        result[path_str] = summary
    return result


def load_index(disk_path: "str | Path") -> Dict[str, Tuple[str, ModuleSummary]]:
    """Loads a previously `save_index`-written JSON file into the
    `{path: (stamp, ModuleSummary)}` shape `summarize_paths` expects.

    Returns an empty dict (never raises) if `disk_path` does not exist, is
    unreadable, or is not valid JSON in the expected shape — a missing or
    corrupt persisted index degrades to a cold build, not a crash.
    """
    disk_path = Path(disk_path)
    try:
        raw = json.loads(disk_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    index: Dict[str, Tuple[str, ModuleSummary]] = {}
    if not isinstance(raw, dict):
        return {}
    for path_str, entry in raw.items():
        try:
            stamp, summary_dict = entry
            index[path_str] = (stamp, ModuleSummary.from_dict(summary_dict))
        except (TypeError, ValueError, KeyError):
            # One corrupt entry degrades to a miss for that path on the next
            # summarize_paths call -- never poisons the whole index load.
            continue
    return index


def save_index(index: Dict[str, Tuple[str, ModuleSummary]], disk_path: "str | Path") -> None:
    """Serializes `index` to `disk_path` as JSON, creating parent
    directories as needed. Overwrites any existing file at `disk_path`."""
    disk_path = Path(disk_path)
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    serializable: Dict[str, Any] = {
        path_str: [stamp, summary.to_dict()] for path_str, (stamp, summary) in index.items()
    }
    disk_path.write_text(json.dumps(serializable, sort_keys=True), encoding="utf-8")
