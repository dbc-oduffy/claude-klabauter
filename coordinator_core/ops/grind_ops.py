"""coordinator_core.ops.grind_ops — the queue-grind engine's closed op
list: `lessons.extract` (source), `lessons.verify_extraction` (verify) and
`doctrine.surface_split_regenerate` (regenerate) — the three op names the
vocabulary (`coordinator_core.contract.grind_vocab`'s `SOURCE_OPS` /
`VERIFY_OPS` / `REGENERATE_OPS`, C1) pins closed.

Purpose: each op is a thin `(params, repo_root) -> dict` adapter, in-process
and NEVER a subprocess, over an existing backing function:

    - `lessons.extract` wraps `coordinator/bin/extract-lessons.py::extract()`.
    - `lessons.verify_extraction` wraps `coordinator/bin/extract-lessons.py::
      verify()`, translated into the DR-404 verify-op wire contract this
      row's own spec pins: params `{manifest, records}`, return
      `{ok, failing_ids}` (exit 0 means ok). `records` arrives as an
      already-materialised list of routing-record dicts (the per-batch
      record the engine holds in memory, never a caller-supplied file);
      this adapter spills it to a throwaway JSON tempfile so it can reuse
      `verify()`'s existing disk-based grounding logic unchanged, and
      removes the tempfile in a `finally` before returning.
    - `doctrine.surface_split_regenerate` wraps `coordinator/bin/
      generate-doctrine-surface-split.py::regenerate_split_dir()`.

None of the three re-derives its backing function's decision logic — this
module is a registration/adapter seam only, mirroring
`coordinator_core.learn_lessons_pipeline.ops` (C5, the precedent commit is
c4528ac83a), whose own docstring states the identical posture. Registration
follows that same precedent's three registries: `_registry_map.py`'s
`OP_MODULE_MAP` (this module's dotted path), `authz/classification.py`'s
`OP_CLASSIFICATION` (DR-208 five-question affirmation per op) and
`op_scopes.py`'s `OP_KEY_SCOPE` (`"show_top"` for all three — each handler's
own `repo_root` arg is the already-resolved worktree root, forwarded
straight through to the backing function's own path arguments; the
`coordinator/bin` scripts these adapters load are ENGINE-provisioned and
resolved via `resolve_cli_script_root()`, never joined against `repo_root`).

Measured process time (this op's own handler body, warm interpreter, in
isolation — see `test_grind_ops.py::test_measured_under_budget`; a fixture
directory of 3 lesson files / a 3-record extraction+routing pair / a
2-section split source):

    - `lessons.extract`: ~8ms (includes the one-time `load_cli_module` cost
      of the first call in a process; subsequent calls reuse the cached
      module).
    - `lessons.verify_extraction`: <1ms.
    - `doctrine.surface_split_regenerate`: ~18ms (`check_mode`, first call
      in a process; includes the same one-time module-load cost above).

All three are comfortably under the 500ms "source op absorbed into emit"
ceiling this row's body sets — none needs the "run beforehand; pass its
output as --queue" carve-out. A real `state/lessons/` directory is orders
of magnitude smaller than the ~890-row bug queue this plan's entrypoint
budgets against, so this ceiling is not expected to bind in practice either;
re-measure here if a future `state/lessons/` census makes that reading
questionable.

Negative-spec:
    - Do NOT re-derive `extract()`/`verify()`/`regenerate_split_dir()`'s own
      decision logic here — thin adapters only.
    - Do NOT spawn a subprocess anywhere in this module — every backing
      script loads via `cli_dispatch.load_cli_module` and calls its Python
      functions directly, never `main(argv)` via a spawned process and
      never `subprocess.run`/`Popen`.
    - Do NOT resolve a repo path via `Path.cwd()`/`Path(__file__)` in any
      handler — the per-request resolved `repo_root` parameter is the only
      source for a params path that is not already absolute.
    - Do NOT add a `coordinator/bin/grind-*.py` front door — no bin door
      exists or is wanted for these ops; the op registry is the only
      caller (§ C9 body / D1 precedent).

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § C9
"""
from __future__ import annotations

import json
import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from coordinator_core.ceremony_common.cli_dispatch import (
    load_cli_module,
    resolve_cli_script_root,
)
from coordinator_core.ipc import register_op

#: The `coordinator/bin` directory holding the two backing scripts this
#: module loads — resolved from THIS module's own location, never
#: `repo_root` and never `Path.cwd()` (see `cli_dispatch.
#: resolve_cli_script_root`'s own docstring for why: these scripts are
#: ENGINE-provisioned, not part of the consumer repo this op operates on).
_SCRIPT_ROOT = resolve_cli_script_root()

_LOADED_MODULES: dict[str, ModuleType] = {}

#: A `verify()` suspect/note line's leading `  {id}: ` prefix — same shape
#: for both the notes block and the suspects block, so only the suspects
#: block (after the `VERDICT: FAIL` header) is ever fed through this.
_SUSPECT_LINE = re.compile(r"^  (\S+):")


def _load(script_stem: str) -> ModuleType:
    """Loads (once, cached) the named `coordinator/bin/<script_stem>.py`
    module in-process via the shared `cli_dispatch.load_cli_module`
    primitive — never a subprocess."""
    cached = _LOADED_MODULES.get(script_stem)
    if cached is not None:
        return cached
    module_name = f"_grind_ops_{script_stem.replace('-', '_')}"
    module = load_cli_module(module_name, _SCRIPT_ROOT / f"{script_stem}.py")
    _LOADED_MODULES[script_stem] = module
    return module


def _resolve_path(repo_root: Optional[Path], value: str) -> Path:
    """A params-supplied path resolves against the already-resolved
    `repo_root` when relative, and is used verbatim when already absolute
    — never against `Path.cwd()` (§ module docstring negative-spec)."""
    path = Path(value)
    if path.is_absolute() or repo_root is None:
        return path
    return repo_root / path


@register_op("lessons.extract")
async def _lessons_extract(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    """Source op: thin adapter over `extract-lessons.py::extract()`.

    params:
        lessons_dir: str, required — path to a `state/lessons/` directory
                     (repo_root-relative unless already absolute).
        shortname: optional str — forwarded to `extract()`; defaults to
                   `lessons_dir`'s parent directory name (its own default
                   is undefined, this adapter needs one every call makes).
        since: optional str (`YYYY-MM-DD`) — forwarded verbatim.
        include_md: optional bool, default False — forwarded verbatim.
    """
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


@register_op("lessons.verify_extraction")
async def _lessons_verify_extraction(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    """Verify op: thin adapter over `extract-lessons.py::verify()`, honouring
    the DR-404 verify-op wire contract this row's body pins.

    params:
        manifest: str, required — the trusted extraction file OR directory
                  (repo_root-relative unless already absolute), forwarded to
                  `verify()`'s own `extraction_path` argument.
        records: list[dict], required — the routing records to ground,
                 already materialised in memory (never a file path). Spilled
                 to a throwaway JSON tempfile (the `{"records": [...]}`
                 shape `verify()`'s own `_parse_records_file` already reads
                 for `.json`), removed in a `finally`.

    Returns `{"ok": bool, "failing_ids": list[str]}` — exit 0 from
    `verify()` means `ok=True`, `failing_ids=[]`; a non-zero exit reports
    every id `verify()`'s own stderr named as a grounding failure.
    """
    module = _load("extract-lessons")
    extraction_path = _resolve_path(repo_root, params["manifest"])
    records = params["records"]

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump({"records": records}, tmp)
        tmp.close()
        routing_path = Path(tmp.name)
        stdout_buf, stderr_buf = StringIO(), StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exit_code = module.verify(extraction_path, routing_path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if exit_code == 0:
        return {"ok": True, "failing_ids": []}
    return {"ok": False, "failing_ids": _parse_failing_ids(stderr_buf.getvalue())}


@register_op("doctrine.surface_split_regenerate")
async def _doctrine_surface_split_regenerate(
    params: dict[str, Any], repo_root: Optional[Path]
) -> dict[str, Any]:
    """Regenerate op: thin adapter over `generate-doctrine-surface-
    split.py::regenerate_split_dir()` — refreshes ONLY an already-split
    directory's `README.md` from its `_preamble.md`, never touching body
    files (see that function's own docstring).

    params:
        split_dir: str, required — an already-split directory (repo_root-
                   relative unless already absolute).
        check_mode: optional bool, default False — forwarded verbatim
                    (diff-only, no write).
        allow_dirty: optional bool, default False — forwarded verbatim.

    Returns `{"exit_code": int}` — `regenerate_split_dir()`'s own exit
    contract (0 ok, 1 drift under `check_mode`, 2 not a split directory,
    3 dirty bodies refused).
    """
    module = _load("generate-doctrine-surface-split")
    split_dir = _resolve_path(repo_root, params["split_dir"])
    check_mode = bool(params.get("check_mode", False))
    allow_dirty = bool(params.get("allow_dirty", False))
    exit_code = module.regenerate_split_dir(
        split_dir, check_mode=check_mode, allow_dirty=allow_dirty
    )
    return {"exit_code": exit_code}
