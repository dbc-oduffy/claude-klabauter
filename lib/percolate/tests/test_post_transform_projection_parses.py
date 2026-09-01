"""Post-transform projection parse guard (ledger item 16, C9;
state/lessons/2026-08-19-the-klabauter-mirror-is-a-percolate-targ-0571a1291280.yaml;
state/lessons/2026-08-05-coverage-that-builds-its-own-input-never-tests-the-door-production-uses.yaml).

Purpose: NOTHING re-parses the bytes a real publish actually writes. Every existing
`tomllib`/`json.loads`/`yaml.safe_load` call anywhere in the percolate test suite reads
SOURCE content -- never the transform's OUTPUT. That is exactly the blind spot that let
DoE ship a malformed `registry.toml.example` on every publish while their own
source-parse check stayed green throughout the incident (the first fix instinct a
source-parse check embodies would never have caught it either).

`percolate-store.yaml`'s `base.depersonalize` maps BOTH `claude-klabauter` and `claude-klabauter` to
`claude-klabauter` -- the sibling's exact bug shape. Longest-first ordering
(`substitute.order_entries`) plus real `\\b` word-boundary anchors
(`substitute._compile_key_pattern`) mean no double-mangling today, and all publish-
eligible structured files were inspected individually at authoring time: none currently
puts both colliding tokens as distinct keys in the same structured file. The defect this
module closes is that NOTHING WOULD CATCH IT IF ONE DID -- this is that catch, not a fix
to a presently-corrupt file (none is).

Mechanism (review: staff-eng -- the production entry point is
`coordinator_core.percolate.engine._apply_content_transforms`, with params resolved from
`setup/percolate-hooks/percolate-store.yaml` via `store.load_store` + `store.resolve_target`
+ `engine._resolve_transform_params`, never by calling `depersonalize()` directly with
hand-supplied params -- that would be coverage that builds its own input, never testing
the door production uses):

  - Publish-eligible structured-file enumeration is DERIVED from
    `setup/publish-targets.portable` row by row, mirroring
    `test_scrub_table_shape_publish_surface.py`'s own derivation technique (never a
    hardcoded file list -- a hardcoded list rots the day a target/file changes).
  - For each `(row, file)` pair, this test reads the file's SOURCE bytes, resolves that
    row's fully-composed section (`store.resolve_target`), and drives the WHOLE file's
    text through `engine._apply_content_transforms` in one call -- never a line-local
    slice. `substitute.apply_substitutions` (reached via `depersonalize`) tokenizes the
    WHOLE text to decide identifier-vs-prose and disables that detection entirely on a
    tokenize failure, so a line-local drive would silently test a different code path
    than a real sweep exercises.
  - The transformed RESULT is parsed with the same format the source suffix implies
    (`tomllib`/`json`/`yaml`), using a duplicate-key-detecting variant of each parser
    (plain `json.loads`/`yaml.safe_load` silently let a later duplicate key overwrite an
    earlier one -- exactly the silent-mangling shape this module exists to catch; bare
    `tomllib.loads` already raises on a duplicate key natively, so no wrapper is needed
    there). A parse failure OR a detected duplicate key fails the test loudly, naming the
    colliding token(s) and the file.

Negative-spec: does NOT call `depersonalize()`/`apply_substitutions()` directly with
hand-assembled params anywhere in this module -- every transform call in this file goes
through `engine._apply_content_transforms` fed by `store.resolve_target`'s real,
fully-composed (including case-variant expansion) section, the same door
`run_content_transform_sweep` and every real publish entrypoint uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from coordinator.lib.percolate.allowlist import parse_allowlist_csv, split_inclusion_exclusion
from coordinator.lib.percolate.targets import _iter_portable_rows
from coordinator_core.percolate import engine
from coordinator_core.percolate.store import load_store, resolve_target
from coordinator_core.wire_paths import rel_id

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PORTABLE_TARGETS_PATH = _REPO_ROOT / "setup" / "publish-targets.portable"
_STORE_PATH = _REPO_ROOT / "setup" / "percolate-hooks" / "percolate-store.yaml"

_STRUCTURED_SUFFIXES = {".yaml", ".yml", ".json", ".toml"}


def _parse_portable_rows(path: Path) -> list[dict[str, str]]:
    """Parse `setup/publish-targets.portable`-shaped rows into
    `{"name", "source_subdir", "allowlist"}` dicts -- mirrors
    `test_scrub_table_shape_publish_surface._parse_portable_rows` field-for-field
    (both derive from the same file, kept as separate small reads rather than an
    import across sibling test modules)."""
    rows = []
    for raw_row in _iter_portable_rows(path):
        fields = raw_row.split("|")
        name = fields[0].strip()
        source_subdir = fields[3].strip() if len(fields) > 3 else ""
        allowlist_csv = fields[6].strip() if len(fields) > 6 else ""
        rows.append({"name": name, "source_subdir": source_subdir, "allowlist": allowlist_csv})
    return rows


def _resolve_published_files(source_root: Path, allowlist_csv: str) -> list[Path]:
    """The set of files a row would actually publish (§ `_parse_portable_rows`'s own
    sibling in `test_scrub_table_shape_publish_surface.py` -- same derivation, same
    reason: read-only file SET, never a materialized `build_allowlisted_source` copy)."""
    if not source_root.is_dir():
        return []
    if not allowlist_csv:
        return [p for p in source_root.rglob("*") if p.is_file()]

    entries, exclusions = split_inclusion_exclusion(parse_allowlist_csv(allowlist_csv))
    included: list[Path] = []
    for entry in entries:
        candidate = source_root / entry
        if candidate.is_file():
            included.append(candidate)
        elif candidate.is_dir():
            included.extend(f for f in candidate.rglob("*") if f.is_file())

    def _is_excluded(rel_posix: str) -> bool:
        return any(
            rel_posix == excl or rel_posix.startswith(excl + "/") for excl in exclusions
        )

    return [
        f for f in included
        if not _is_excluded(f.relative_to(source_root).as_posix())
    ]


class _DuplicateKeyError(ValueError):
    """Raised by the duplicate-key-detecting loaders below -- carries the colliding
    key so a failing assertion can name it, not just "a duplicate exists somewhere"."""


def _json_loads_no_duplicates(text: str) -> Any:
    def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise _DuplicateKeyError(key)
            seen[key] = value
        return seen

    return json.loads(text, object_pairs_hook=_pairs_hook)


def _yaml_safe_load_no_duplicates(text: str) -> Any:
    class _StrictLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: yaml.SafeLoader, node: yaml.Node, deep: bool = False) -> dict:
        seen: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise _DuplicateKeyError(str(key))
            seen[key] = loader.construct_object(value_node, deep=deep)
        return seen

    _StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )
    return yaml.load(text, Loader=_StrictLoader)


def _parse_no_duplicates(suffix: str, text: str) -> Any:
    """Parse `text` as the format `suffix` implies, raising `_DuplicateKeyError` (naming
    the colliding key) or the parser's own native error on malformed content. Bare
    `tomllib.loads` already refuses a duplicate key without a wrapper (native TOML
    behaviour), so no strict variant is needed for that branch."""
    lowered = suffix.lower()
    if lowered in (".yaml", ".yml"):
        return _yaml_safe_load_no_duplicates(text)
    if lowered == ".json":
        return _json_loads_no_duplicates(text)
    if lowered == ".toml":
        import tomllib

        return tomllib.loads(text)
    raise AssertionError(f"unsupported structured suffix {suffix!r}")


def _iter_structured_publish_targets() -> Iterator[tuple[str, Path]]:
    """Yield `(row_name, source_file)` for every publish-eligible structured
    (`.yaml`/`.yml`/`.json`/`.toml`) file this repo's portable rows currently declare."""
    for row in _parse_portable_rows(_PORTABLE_TARGETS_PATH):
        source_root = _REPO_ROOT / row["source_subdir"] if row["source_subdir"] else _REPO_ROOT
        for path in _resolve_published_files(source_root, row["allowlist"]):
            if path.suffix.lower() in _STRUCTURED_SUFFIXES:
                yield row["name"], path


def _projection_cases() -> list[tuple[str, str]]:
    """`(row_name, posix_path)` pairs for pytest parametrization -- stringified so
    failing test IDs are legible file paths, not `Path` reprs."""
    return sorted(
        {(name, path.as_posix()) for name, path in _iter_structured_publish_targets()}
    )


_CASES = _projection_cases()


@pytest.mark.skipif(not _CASES, reason="no publish-eligible structured files declared")
@pytest.mark.parametrize("row_name,file_posix", _CASES, ids=[c[1] for c in _CASES])
def test_transformed_projection_parses_with_no_duplicate_keys(row_name: str, file_posix: str) -> None:
    """Drives the REAL production door (`store.resolve_target` + `engine._resolve_
    transform_params` + `engine._apply_content_transforms`, § module docstring) over one
    publish-eligible structured file's whole text, then re-parses the RESULT (never the
    source) with a duplicate-key-detecting loader. A malformed or duplicate-keyed
    projection fails here, naming the row/file/collision, instead of shipping silently
    green the way DoE's `registry.toml.example` incident did."""
    source_path = Path(file_posix)
    store = load_store(_STORE_PATH)
    section = resolve_target(store, row_name)
    params = engine._resolve_transform_params(section)

    text = source_path.read_text(encoding="utf-8")
    rel_path = rel_id(source_path, _REPO_ROOT)

    try:
        _parse_no_duplicates(source_path.suffix, text)
    except Exception:
        pytest.skip(
            f"row {row_name!r}: {rel_path} does not parse as {source_path.suffix} in "
            "SOURCE (a deliberately-malformed fixture, e.g. for a CLI parse-error test) "
            "-- this module asserts the transform does not BREAK parseability, which "
            "presupposes the source was parseable to begin with"
        )

    transformed = engine._apply_content_transforms(text, params, rel_path)

    try:
        _parse_no_duplicates(source_path.suffix, transformed)
    except _DuplicateKeyError as exc:
        pytest.fail(
            f"row {row_name!r}: {rel_path} gained duplicate key {exc.args[0]!r} after "
            "the content-transform sweep -- two distinct source tokens collided onto "
            "the same rewritten key (§ this module's docstring, the "
            "claude-klabauter/claude-klabauter -> claude-klabauter collision shape)"
        )
    except Exception as exc:  # noqa: BLE001 -- re-raise as a named, loud failure
        pytest.fail(
            f"row {row_name!r}: {rel_path} does not parse as {source_path.suffix} after "
            f"the content-transform sweep: {exc!r}"
        )
