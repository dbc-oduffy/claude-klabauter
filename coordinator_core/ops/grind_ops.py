"""coordinator_core.ops.grind_ops — the queue-grind engine's closed op list:
`lessons.extract` (source), `lessons.verify_extraction` (verify) and
`doctrine.surface_split_regenerate` (regenerate), the three names
`coordinator_core.contract.grind_vocab` pins closed.

Purpose: thin in-process `(params, repo_root) -> dict` adapters over
`coordinator/bin/extract-lessons.py::extract()` / `verify()` and
`coordinator/bin/generate-doctrine-surface-split.py::regenerate_split_dir()`.
`lessons.verify_extraction` takes `{manifest, records}` and returns
`{ok, failing_ids}`; `records` is spilled to a throwaway tempfile because
`verify()` reads from disk. The bin scripts are engine-provisioned
(`resolve_cli_script_root()`), never joined against `repo_root`.

Measured process time (`test_grind_ops.py::test_measured_under_budget`):
`lessons.extract` ~8ms, `lessons.verify_extraction` <1ms,
`doctrine.surface_split_regenerate` ~18ms.

Negative-spec:
    - No re-deriving the backing functions' decision logic.
    - No subprocess in `lessons.extract` / `lessons.verify_extraction`; no
      redirecting the process-global stdout/stderr (the daemon is async) —
      `verify()` takes its own `err`/`out` streams.
    - `doctrine.surface_split_regenerate` spawns exactly one `git status` on
      its default path (`dirty_bodies()` guards a peer's uncommitted body);
      pinned by `test_grind_ops.py::
      test_doctrine_surface_split_regenerate_default_path_spawns_exactly_once`.
    - No `Path.cwd()` / `Path(__file__)` repo resolution: `repo_root` is the
      only base for a relative params path.
    - No `coordinator/bin/grind-*.py` front door; the op registry is the only
      caller.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § C9
"""
from __future__ import annotations

import json
import re
import tempfile
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from coordinator_core.ceremony_common.cli_dispatch import (
    load_cli_module,
    resolve_cli_script_root,
)
from coordinator_core.ipc import register_op

_SCRIPT_ROOT = resolve_cli_script_root()

_LOADED_MODULES: dict[str, ModuleType] = {}

_SUSPECT_LINE = re.compile(r"^  (\S+):")


def _load(script_stem: str) -> ModuleType:
    cached = _LOADED_MODULES.get(script_stem)
    if cached is not None:
        return cached
    module_name = f"_grind_ops_{script_stem.replace('-', '_')}"
    module = load_cli_module(module_name, _SCRIPT_ROOT / f"{script_stem}.py")
    _LOADED_MODULES[script_stem] = module
    return module


def _resolve_path(repo_root: Optional[Path], value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or repo_root is None:
        return path
    return repo_root / path


@register_op("lessons.extract")
def _lessons_extract(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    module = _load("extract-lessons")
    lessons_dir = _resolve_path(repo_root, params["lessons_dir"])
    shortname = params.get("shortname") or lessons_dir.parent.name
    since = params.get("since")
    include_md = bool(params.get("include_md", False))
    records, stats = module.extract(lessons_dir, shortname, since, include_md)
    return {"exit_code": 0, "records": records, "stats": stats}


def _parse_failing_ids(stderr_text: str) -> list[str]:
    """Extracts the failing routing-record ids `verify()` names in its
    `GROUNDING GATE VERDICT: FAIL` stderr block — never re-deriving the
    checks themselves, only reading the ids `verify()` already decided to
    report. The advisory-notes block (printed first, same `  {id}: `
    line shape) is excluded by only scanning lines after the FAIL header."""
    marker = "GROUNDING GATE VERDICT: FAIL"
    idx = stderr_text.find(marker)
    if idx == -1:
        return []
    failing_ids: list[str] = []
    for line in stderr_text[idx:].splitlines():
        match = _SUSPECT_LINE.match(line)
        if match:
            failing_ids.append(match.group(1))
    return failing_ids


class VerifyRefusalError(RuntimeError):
    pass


@register_op("lessons.verify_extraction")
def _lessons_verify_extraction(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    module = _load("extract-lessons")
    extraction_path = _resolve_path(repo_root, params["manifest"])
    if not extraction_path.exists():
        raise VerifyRefusalError(f"lessons.verify_extraction: manifest not found: {extraction_path}")
    records = params["records"]

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump({"records": records}, tmp)
        tmp.close()
        routing_path = Path(tmp.name)
        stderr_buf = StringIO()
        exit_code = module.verify(
            extraction_path, routing_path, err=stderr_buf, out=StringIO()
        )
    finally:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)

    if exit_code == 0:
        return {"ok": True, "failing_ids": []}
    if exit_code == 1:
        return {"ok": False, "failing_ids": _parse_failing_ids(stderr_buf.getvalue())}
    raise VerifyRefusalError(
        f"lessons.verify_extraction: verify() refused (exit {exit_code}): "
        f"{stderr_buf.getvalue().strip()}"
    )


@register_op("doctrine.surface_split_regenerate")
def _doctrine_surface_split_regenerate(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    module = _load("generate-doctrine-surface-split")
    split_dir = _resolve_path(repo_root, params["split_dir"])
    check_mode = bool(params.get("check_mode", False))
    allow_dirty = bool(params.get("allow_dirty", False))
    exit_code = module.regenerate_split_dir(
        split_dir, check_mode=check_mode, allow_dirty=allow_dirty
    )
    return {"exit_code": exit_code}
