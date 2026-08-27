"""
coordinator_core.ops.gate_dimension_types — the "types" dimension for
`gate.validate_invocable` (C2, docs/plans/2026-07-20-merge-gate-dod-engine-
enforced.md § C2).

Purpose: plug a real `types` check into `gate_validate_invocable`'s
dimension seam via `register_dimension("types", ...)` at import time — the
same self-registration shape `gate_dimension_review.py` / `gate_dimension_
latency.py` use. This module never edits `gate_validate_invocable.py`; it
plugs into the seam that module already exposes.

WHAT THIS CHECKS: mypy, scoped ONLY to the `.py` files in `changed_files`,
run against this repo's single `[tool.mypy]` home in `pyproject.toml` — no
`mypy.ini`, no ad hoc flags that would diverge from that config. The
per-module strict-override ledger in `pyproject.toml` (`[[tool.mypy.
overrides]]`) IS the migration artifact this dimension measures against; see
that block's own comment for why it is seeded empty at this chunk's landing
(nobody has run mypy against this tree yet, so no module has *earned* a
stricter override — see "Reconciliation" below for why the tool has never
run here, and TOOL RESOLUTION below for how that state is reported).

TOOL RESOLUTION — this module never calls `shutil.which` itself. It calls
`coordinator_core.ops.gate_tool_resolve.resolve_tool("mypy")` (C1b's shared
seam) and branches on `.available`: `False` returns `Verdict.UNAVAILABLE`
with `result.reason` as the detail (mypy not on PATH is the expected,
verified-live state on the machine this chunk was authored on — that is not
a bug in this module, it is the honest degrade path C1b specifies). `True`
uses `result.path` as the executable, never a bare `"mypy"` string that
would re-invoke PATH resolution a second time.

RECONCILIATION WITH `coordinator/bin/static-check` (pyright) — stated here
per the plan's explicit "silence on this is not acceptable" (C2). This repo
already has a live type-checking surface: `coordinator/bin/static-check`
wraps pyright (landed 2026-08-11, class-(f) shell-out carve-out in
`docs/reference/shell-out-carve-outs.md`) as an executor-facing spot-check
tool an agent can run mid-task. This module does NOT replace it and does not
route through it. The two coexist by design, on this basis: the per-module
strict-override ledger in `[tool.mypy]` is a **mypy-shaped artifact pyright
has no equivalent of** — pyright has no per-module override list a repo can
grow as a migration ledger, only a single global strictness mode. mypy is
therefore the only tool that can *host* the ledger this dimension measures
against; static-check/pyright remains the fast, interactive spot-check tool
a dispatched executor reaches for mid-session, and this dimension is the
gate-facing, ledger-aware assertion that runs at `gate.validate_invocable`
call time. Neither tool's verdict is derived from the other's; a future
session may find they disagree on a given file, and that is expected of two
different type checkers with different inference engines, not a bug either
tool needs to route around.

SCOPE — changed-file-set only, never a whole-tree sweep. `changed_files` is
filtered to `.py` paths (mirroring `gate_dimension_review.py`'s and
`gate_dimension_latency.py`'s own path-scoped convention); a `changed_files`
set with zero `.py` entries returns `Verdict.UNAVAILABLE` ("nothing to
type-check"), never a vacuous PASS (per `gate_validate_invocable._overall_
verdict`'s "PASS must be earned" rule).

SHELL-OUT-CARVE-OUT STATUS — flagged, not resolved, by this module. Invoking
mypy is a real subprocess spawn. `docs/reference/shell-out-carve-outs.md`'s
class (f) ("optional 3rd-party static-verification tool, PATH-resolved,
degrades to UNAVAILABLE") is the shape this site matches, but ITS `Sites:`
list names only `coordinator/bin/static-check` `run_pyright()` — per that
doc's own "Enumeration is constitutive, not illustrative" rule, a site that
satisfies a class's rationale but is not named is NOT sanctioned. This
module's `_run_mypy()` is implemented per the plan's explicit instruction
("implement the call and REPORT the gap prominently") — the PM routes
whether it becomes a new class-(f) `Sites:` entry, not this chunk.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C2
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.external_tool_budget import bound_for
from coordinator_core.ops.gate_tool_resolve import resolve_tool
from coordinator_core.ops.gate_validate_invocable import (
    DimensionResult,
    Verdict,
    register_dimension,
)
from coordinator_core.win_portability import no_console_creationflags

_MYPY_SITE = "coordinator_core/ops/gate_dimension_types.py :: _run_mypy"
_MYPY_TIMEOUT_SECS = bound_for(_MYPY_SITE)
_CREATIONFLAGS = no_console_creationflags()


def _python_changed_files(changed_files: List[str]) -> List[str]:
    """Filter `changed_files` down to `.py` paths only -- mirrors the other
    dimension modules' path-scoped convention (see module docstring
    "SCOPE")."""
    return [f for f in changed_files if f.endswith(".py")]


def _run_mypy(
    mypy_path: str, py_files: List[str], repo_root: Optional[str]
) -> "tuple[int, str, str]":
    """Run `mypy <py_files>` in `repo_root`; never raises -- a spawn failure
    or timeout degrades to a non-zero rc + diagnostic stderr, same shape
    `gate_dimension_review._run_git` uses for its own subprocess call.

    `repo_root` is `Optional[str]`, not `str`: the op handler's own
    `repo_root: Optional[Path]` param is optional (see `gate_validate_
    invocable._gate_validate_invocable`), and `_check_types` passes it
    through unresolved rather than defaulting it -- there is no "current
    repo root" this module should guess at. `subprocess.run(..., cwd=None)`
    is `subprocess`'s own documented "inherit the caller's cwd" behavior, so
    a `None` here is a legal, intentional cwd, not a missing value that
    needs coercing to a default.

    No shell flag anywhere in this call (`shell=False` is subprocess's own
    default) -- argv is a Python list, portable on Windows and POSIX alike
    (CLAUDE.md "Windows is first-class"); `mypy_path` is the resolved
    executable from `gate_tool_resolve.resolve_tool`, never a bare `"mypy"`
    string re-triggering PATH resolution.
    """
    try:
        result = subprocess.run(
            [mypy_path, *py_files],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_MYPY_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        # Sentinel rc (never a real mypy exit code) so the caller's rc==1
        # branch -- reserved for mypy's own "ran, found errors" contract --
        # never misreads a timeout as a type-error FAIL.
        return (
            -1,
            "",
            f"mypy timed out after {_MYPY_TIMEOUT_SECS:.0f}s over {len(py_files)} file(s)",
        )
    except OSError as exc:
        print(f"skip: gate_dimension_types._run_mypy failed: {exc}", file=sys.stderr)
        return -1, "", str(exc)


def _check_types(
    changed_files: List[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> DimensionResult:
    """The registered `types` `DimensionCheck` (C2). See module docstring
    for tool resolution, scope, and the pyright reconciliation."""
    py_files = _python_changed_files(changed_files)
    if not py_files:
        return DimensionResult(
            dimension="types",
            verdict=Verdict.UNAVAILABLE,
            detail="no changed .py files; nothing for mypy to type-check",
        )

    tool = resolve_tool("mypy")
    if not tool.available:
        return DimensionResult(
            dimension="types",
            verdict=Verdict.UNAVAILABLE,
            detail=tool.reason,
        )

    repo_root_str = str(repo_root) if repo_root is not None else None
    rc, out, err = _run_mypy(tool.path, py_files, repo_root_str)

    if rc == 0:
        return DimensionResult(
            dimension="types",
            verdict=Verdict.PASS,
            detail=f"mypy clean over {len(py_files)} changed file(s)",
        )
    if rc == 1:
        # mypy's own exit-code contract: 1 == "ran successfully, found type
        # errors" -- a real FAIL, not a tool malfunction.
        detail_body = out or err or "mypy reported errors (no output captured)"
        return DimensionResult(
            dimension="types",
            verdict=Verdict.FAIL,
            detail=detail_body,
        )
    # rc == 2 (mypy's own "fatal error" exit code) or any other non-0/1 value
    # -- the tool itself did not complete a real run (bad config, internal
    # crash, or our own subprocess-layer failure, which `_run_mypy` surfaces
    # as rc=-1, not rc=1 -- a timeout/OSError never lands in the rc==1
    # branch above). This branch is the tool-broke case, reported as
    # UNAVAILABLE, never FAIL -- a broken tool run must never masquerade as
    # "found type errors").
    # Review: coordinator:code-reviewer wsc-B-dimensions — comment described
    # a rc==1 subprocess-layer-failure path that cannot occur as written
    # (_run_mypy returns -1, not 1, on timeout/OSError).
    last_err = err or out or f"mypy exited {rc} with no output"
    return DimensionResult(
        dimension="types",
        verdict=Verdict.UNAVAILABLE,
        detail=f"mypy did not complete a run (exit {rc}): {last_err}",
    )


register_dimension("types", _check_types)
