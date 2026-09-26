"""coordinator_core.learn_lessons_pipeline.apply — the learn-lessons PIPELINE
ceremony's MUTATING half (AC1's halt, § D3).

Purpose: recomputes `learn_lessons_pipeline.brief()` in-process (never trusts
a caller-supplied decision object — the same posture every sibling apply
half takes) and dispatches its `directives[]` through
`coordinator_core.contract.apply_base.execute_directives`, the shared
directive-execution engine every `dispatch_table`-consuming assembler in
this baton composes rather than re-derives (`baton_assemble`,
`merge_assemble`, `consolidate_assemble`, and now this package). A non-zero
exit from any dispatched directive raises inside its handler;
`execute_directives` then returns `APPLY_EXIT_PARTIAL_MUTATION` naming the
failed directive and dispatches nothing further — later steps never run
against state an earlier step failed to establish.

Two closed dispatch tables, matching the two directive shapes C3's
`brief()` emits:

    `_CLI_DISPATCH` — a closed dict literal over `CONSUMES_MANIFEST`
    (`extract-lessons`, `lessons-outbox-drain`, `age-sweep-lessons`) —
    NEVER `getattr`, NEVER `importlib.import_module` on a brief-derived
    string, NEVER a subprocess, matching the security-load-bearing
    construction `workstream_complete/apply.py` documents. Each
    `_dispatch_*` handler loads its script ONCE through
    `ceremony_common.cli_dispatch.load_cli_module` at a fixed
    `Path(__file__)`-relative path (this module's own location, via
    `resolve_cli_script_root`) and calls its `main(argv)` in-process —
    never spawned as a child process.

    `_OP_DISPATCH = {"stamp-run-complete": _dispatch_stamp_run_complete}` —
    the sixth directive's seam. `d-stamp-run-complete` carries `op:` rather
    than `cli:` because `run_stamp.py` is an in-package engine module, not a
    `coordinator/bin` script — admitting it into `_CLI_DISPATCH` would put a
    non-script into a manifest whose whole point is "every `cli` is a real,
    manifest-listed bin script". `execute_directives` resolves an `op`
    directive through `resolve_op`, which checks THIS table for an adapter
    and then calls `apply_base.assert_dispatchable("learn_lessons_pipeline",
    "stamp-run-complete")` — a default-deny admission check against
    `coordinator_core.authz.dispatchable.ASSEMBLER_DISPATCHABLE` that C5
    populates. `run_stamp` is therefore a member of neither table's
    counterpart oracle (`CONSUMES_MANIFEST`) and is never looked up by
    `_resolve_script_path` — the two tables are both literals, and both
    lookups fail closed.

Bin-directory importability: two of the three `CONSUMES_MANIFEST` CLIs
(`age-sweep-lessons.py`, `lessons-outbox-drain.py`) resolve a bare
`import lib` from inside their own call graph, which only resolves when
`coordinator/bin` is on `sys.path` AHEAD of any foreign `lib` already bound
in `sys.modules` (win32 ships its own `lib` namespace package).
`_ensure_import_path` calls `coordinator_core.bin_lib_binding.
ensure_bin_lib_bound` — the one stdlib-only leaf that puts the bin dir on
`sys.path` and evicts a foreign `sys.modules["lib"]`, and the only home of
that ladder in the tree.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C4

Negative-spec:
    - Do NOT resolve a `cli`/`op` name via `getattr`, `importlib.
      import_module` on a brief-derived string, or a subprocess spawn
      anywhere in this module — both dispatch tables are closed dict
      literals, and every load goes through `cli_dispatch.load_cli_module`.
    - Do NOT mark any of the six directives `advisory` — advisory means a
      failure does NOT halt the run, which is the one way to lose AC1
      silently while every test still passes.
    - Do NOT add `run_stamp` to `_CLI_DISPATCH`, and do NOT add any
      `CONSUMES_MANIFEST` member to `_OP_DISPATCH` — the two tables stay
      disjoint, matching the two distinct seams `execute_directives`
      resolves `cli`/`op` directives through.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

from coordinator_core.bin_lib_binding import ensure_bin_lib_bound
from coordinator_core.ceremony_common.cli_dispatch import (
    invoke_cli_main,
    load_cli_module,
    resolve_cli_script_root,
)
from coordinator_core.contract import apply_base
from coordinator_core.contract.apply_base import assert_in_repo_root
from coordinator_core.learn_lessons_pipeline import CONSUMES_MANIFEST, brief
from coordinator_core.learn_lessons_pipeline.run_stamp import stamp_run_complete

#: `ASSEMBLER_DISPATCHABLE` lookup (§ C5) — matches the key
ASSEMBLER_NAME = "learn_lessons_pipeline"

#: The `coordinator/bin` directory holding every `CONSUMES_MANIFEST`
_CLI_SCRIPT_ROOT = resolve_cli_script_root()

#: The two `CONSUMES_MANIFEST` members whose own call graph does a bare
_NEEDS_BIN_IMPORTABLE = frozenset({"age-sweep-lessons", "lessons-outbox-drain"})

_LOADED_MODULES: dict[str, ModuleType] = {}


def _resolve_script_path(name: str) -> Path:
    """A `CONSUMES_MANIFEST` bareword resolves to either `<name>.py` or an
    extensionless launcher shim `<name>` under `_CLI_SCRIPT_ROOT` — a
    two-candidate literal lookup, never a glob/search, mirroring
    `workstream_complete.apply._resolve_script_path`."""
    py_path = _CLI_SCRIPT_ROOT / f"{name}.py"
    if py_path.exists():
        return py_path
    return _CLI_SCRIPT_ROOT / name


def _ensure_import_path(cli_name: str) -> None:
    """Ensures `coordinator/bin` is importable and evicts a foreign `lib`
    already bound in `sys.modules`, BEFORE `load_cli_module` runs, for the
    two `CONSUMES_MANIFEST` members that need it. Calls the shared leaf
    (§ module docstring) rather than re-deriving a third copy of this
    ladder."""
    if cli_name in _NEEDS_BIN_IMPORTABLE:
        ensure_bin_lib_bound(str(_CLI_SCRIPT_ROOT))


def _load(cli_name: str) -> ModuleType:
    """Loads (once, cached) the `CONSUMES_MANIFEST` script named by
    `cli_name`, in-process, via the shared `cli_dispatch.load_cli_module`
    primitive — never a subprocess."""
    cached = _LOADED_MODULES.get(cli_name)
    if cached is not None:
        return cached
    _ensure_import_path(cli_name)
    module_name = f"_learn_lessons_pipeline_cli_{cli_name.replace('-', '_')}"
    module = load_cli_module(module_name, _resolve_script_path(cli_name))
    _LOADED_MODULES[cli_name] = module
    return module


def _write_target_indexes(cli_name: str, args: list[str]) -> list[int]:
    """Names the `args` indexes this module's own `build_directives` fills
    with a WRITE-target path for `cli_name`, never a read-only source path
    (23b). `lessons-outbox-drain read <root>...` is the one deliberate
    exception: its `<root>...` tail is `ops.learn_lessons_roots.
    resolve_roots()`'s PEER repo list, read-only by design and legitimately
    outside `repo_root` — containment here would refuse the very cross-repo
    drain this pipeline exists to run, so `read` names no index at all."""
    if cli_name == "extract-lessons":
        if args and args[0] == "extract":
            indexes = [1]
            if "-o" in args:
                indexes.append(args.index("-o") + 1)
            return indexes
        if args and args[0] == "verify":
            return [1, 2]
        return []
    if cli_name == "lessons-outbox-drain":
        if args and args[0] == "assert-empty":
            return [1]
        return []
    if cli_name == "age-sweep-lessons":
        return [0]
    return []


def _assert_write_targets_in_repo_root(
    cli_name: str, args: list[str], repo_root: Path
) -> None:
    """Refuses (raises `OutOfRepoPath`) a dispatched fixer CLI whose own
    WRITE-target argument resolves outside `--repo-root` (23b — the
    2026-09-24 example-game-repo-em memo's "fixers can write outside --repo-root"
    sub-claim). Reuses `apply_base.assert_in_repo_root`, the same
    containment primitive `scoped_commit` already applies to a commit
    target, rather than re-deriving a second copy of that check here."""
    for idx in _write_target_indexes(cli_name, args):
        if idx >= len(args):
            continue
        assert_in_repo_root(Path(args[idx]), repo_root)


def _run_cli(cli_name: str, args: list[str], repo_root: Path) -> dict[str, Any]:
    _assert_write_targets_in_repo_root(cli_name, args, repo_root)
    module = _load(cli_name)
    exit_code, stdout, stderr, _exit_class = invoke_cli_main(module, args)
    if exit_code != 0:
        raise RuntimeError(
            f"{cli_name} exited {exit_code} (args={args!r}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr}


def _dispatch_extract_lessons(args: list[str], repo_root: Path) -> dict[str, Any]:
    return _run_cli("extract-lessons", args, repo_root)


def _dispatch_lessons_outbox_drain(args: list[str], repo_root: Path) -> dict[str, Any]:
    return _run_cli("lessons-outbox-drain", args, repo_root)


def _dispatch_age_sweep_lessons(args: list[str], repo_root: Path) -> dict[str, Any]:
    return _run_cli("age-sweep-lessons", args, repo_root)


#: The closed dispatch table over `CONSUMES_MANIFEST` — never `getattr`,
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "extract-lessons": _dispatch_extract_lessons,
    "lessons-outbox-drain": _dispatch_lessons_outbox_drain,
    "age-sweep-lessons": _dispatch_age_sweep_lessons,
}


def _dispatch_stamp_run_complete(args: list[str], repo_root: Path) -> dict[str, Any]:
    """Handles `d-stamp-run-complete` by direct import of
    `run_stamp.stamp_run_complete` — never through `_CLI_DISPATCH`, `run_stamp`
    is not a `CONSUMES_MANIFEST` member (§ module docstring)."""
    runs_dir = Path(args[0])
    run_date = args[1]
    sentinel = stamp_run_complete(runs_dir, run_date)
    return {"sentinel": str(sentinel)}


#: disjoint from `_CLI_DISPATCH` (§ module docstring negative-spec).
_OP_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "stamp-run-complete": _dispatch_stamp_run_complete,
}

_DISPATCH_TABLE: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    **_CLI_DISPATCH,
    **_OP_DISPATCH,
}


def apply(
    repo_root: Path, *, roots: Optional[list[str]] = None
) -> tuple[int, dict[str, Any]]:
    """Recomputes `brief(repo_root, roots=roots)` in-process and dispatches
    its `directives[]` through the shared `apply_base.execute_directives`
    engine against `_DISPATCH_TABLE`. Returns `(exit_code, report)` —
    `apply_base.APPLY_EXIT_PARTIAL_MUTATION` with `report["failed_directive"]`
    set the moment any directive's handler raises; nothing after it
    dispatches."""
    envelope = brief(repo_root, roots=roots)
    return apply_base.execute_directives(
        envelope["directives"],
        envelope["judgment_points"],
        repo_root,
        _DISPATCH_TABLE,
        assembler_name=ASSEMBLER_NAME,
    )
