"""
coordinator_core.ops.gate_dimension_docstrings — the "docstrings" dimension
for `gate.validate_invocable` (C3, docs/plans/2026-07-20-merge-gate-dod-
engine-enforced.md § C3).

Purpose: plug a real `docstrings` check into `gate_validate_invocable`'s
dimension seam via `register_dimension("docstrings", ...)` at import time —
the same self-registration shape `gate_dimension_types.py` /
`gate_dimension_review.py` / `gate_dimension_latency.py` use. This module
never edits `gate_validate_invocable.py`; it plugs into the seam that module
already exposes.

WHAT THIS CHECKS: two tools, both required to PASS —

    1. Ruff `D1xx` (docstring-*presence* rules — D100-D107 "missing
       docstring in public X"; never `D2xx`/`D4xx` docstring-*content*/
       *style* rules, which the plan explicitly defers to review, not this
       gate) via `ruff check --select D1`.
    2. `interrogate --fail-under <floor>`, where `<floor>` is read from
       `.github/docstring-coverage-floor.json` (see "THE RATCHET" below),
       never recomputed at call time.

SCOPE — the ported-ops path SET, not `changed_files`. This is a deliberate
divergence from `gate_dimension_types.py`'s changed-file-scoped convention,
stated once here rather than left implicit: `interrogate --fail-under` is a
*corpus* percentage, not a per-file predicate — comparing a floor measured
over ~160 files against a run over one or two changed files would compare
two different populations and make the floor meaningless. The path set
comes from the landed C6 fragment `.github/ported-ops-paths.txt` — this
module reads it, it does not re-derive a walk (per the plan's explicit
instruction, mirroring `scripts/gen_ported_ops_fragment.py` /
`scripts/gen_dod_backlog_fragment.py`'s own derive-don't-pin convention).
`changed_files` is accepted (the `DimensionCheck` signature is fixed by
`gate_validate_invocable.DimensionCheck`) but not used to filter the path
set — it is unused by design, not an oversight.

A fragment entry that does not exist on disk (fragment staleness — verified
2026-08-14: 1 of 160 listed paths, `coordinator_core/ops/ceremony/
render_handoff_tracker.py`, does not exist) is silently filtered out of the
tool invocation rather than raising: both `ruff` (as a warning) and
`interrogate` (as a hard usage error) react differently to a missing path,
and neither reaction is this dimension's concern to fix — fragment
staleness is a C6/C9 concern. Zero surviving paths degrades to
`Verdict.UNAVAILABLE`, never a vacuous PASS.

TOOL RESOLUTION — this module never calls `shutil.which` itself. It calls
`coordinator_core.ops.gate_tool_resolve.resolve_tool()` (C1b's shared seam)
once per tool and branches on `.available`, exactly like
`gate_dimension_types.py`. Either tool missing (both are absent on the
machine this chunk was authored on, until installed ad hoc for the floor
measurement below) degrades the whole dimension to `Verdict.UNAVAILABLE`
naming which tool(s) and why — this dimension does not partially grade on
one tool alone, since a Ruff-clean, interrogate-unmeasured result (or vice
versa) is not a meaningful "docstrings" verdict either tool alone can carry.

THE RATCHET — `--fail-under 100` is unmeetable before C9's retrofit sweep
lands (per the plan's C3 entry, "100 is C9's terminal condition, not C3's").
This module reads its fail-under floor from
`.github/docstring-coverage-floor.json` rather than hardcoding a literal in
this file, so a later ratchet step (C9) can raise it mechanically (read,
recompute, rewrite the JSON) without touching this module's code. The floor
was measured once, at this chunk's landing, over the ported-ops path set's
existing-on-disk files (159 of 160): `interrogate` reported 88.4% actual
coverage; the JSON floor is 88.0, floored to a whole percent so a future run
landing at exactly 88.4% (or a hair below, from a version-to-version
interrogate scoring quirk) is not spuriously FAILed by float/tool-version
jitter at the exact measured boundary. The floor file's own `ratchet_rule`
field is the load-bearing text — this module does not enforce monotonicity
itself (it only *reads* the floor); a floor edit that lowers the number is a
docstring-coverage regression a reviewer must catch, not something this
module can detect from a single reading.

SHELL-OUT-CARVE-OUT STATUS — flagged, not resolved, by this module. Invoking
ruff and interrogate are real subprocess spawns.
`docs/reference/shell-out-carve-outs.md` class (f) ("optional 3rd-party
static-verification tool, PATH-resolved, degrades to UNAVAILABLE") is the
shape both sites match, but its `Sites:` list names only
`coordinator/bin/static-check`'s `run_pyright()` — per that doc's own
"Enumeration is constitutive, not illustrative" rule, a site that satisfies
a class's rationale but is not named is NOT sanctioned. `_run_ruff()` and
`_run_interrogate()` are implemented per the plan's explicit instruction
("implement the calls and REPORT the gap", mirroring C2's identical gap for
mypy) — the PM routes whether either becomes a new class-(f) `Sites:` entry,
not this chunk. A single PM ruling covering all the DoD tool sites at once
is already queued (per C2's own module docstring and this chunk's dispatch
brief).

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C3
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.ops.gate_tool_resolve import resolve_tool
from coordinator_core.ops.gate_validate_invocable import (
    DimensionResult,
    Verdict,
    register_dimension,
)
from coordinator_core.win_portability import no_console_creationflags

_RUFF_TIMEOUT_SECS = 120
_INTERROGATE_TIMEOUT_SECS = 120
_CREATIONFLAGS = no_console_creationflags()

# Relative to repo_root (or cwd, when repo_root is None) -- never an absolute
# path baked into this module, so the check works from any clone.
_PORTED_OPS_FRAGMENT = ".github/ported-ops-paths.txt"
_FLOOR_FILE = ".github/docstring-coverage-floor.json"


def _resolve(repo_root: Optional[Path], relative: str) -> Path:
    """Join `relative` onto `repo_root` when given, else treat it as
    cwd-relative -- the same `Optional[Path]` contract `gate_dimension_
    types._run_mypy`'s own docstring explains for `repo_root`."""
    base = repo_root if repo_root is not None else Path(".")
    return Path(base) / relative


def _load_ported_ops_paths(repo_root: Optional[Path]) -> List[str]:
    """Read `.github/ported-ops-paths.txt` verbatim -- comments (`#`-prefixed)
    and blank lines dropped, no re-derivation of the walk that produced it
    (per this module's docstring "SCOPE"). Returns `[]` if the fragment
    itself is missing (caller degrades that to UNAVAILABLE)."""
    fragment_path = _resolve(repo_root, _PORTED_OPS_FRAGMENT)
    if not fragment_path.is_file():
        return []
    lines = fragment_path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _existing_paths(repo_root: Optional[Path], paths: List[str]) -> List[str]:
    """Filter the fragment's path list to entries that exist on disk --
    fragment staleness (a listed path since renamed/removed) is a C6/C9
    concern this dimension only tolerates, never re-derives or reports as
    its own failure. See module docstring's "A fragment entry that does not
    exist on disk" paragraph."""
    return [p for p in paths if _resolve(repo_root, p).is_file()]


def _load_fail_under(repo_root: Optional[Path]) -> Optional[float]:
    """Read the ratchet floor from `.github/docstring-coverage-floor.json`.
    Returns `None` if the file is missing or malformed (caller degrades
    that to UNAVAILABLE -- gating without a real floor would be a silent
    pass in disguise)."""
    floor_path = _resolve(repo_root, _FLOOR_FILE)
    if not floor_path.is_file():
        return None
    try:
        payload = json.loads(floor_path.read_text(encoding="utf-8"))
        return float(payload["fail_under"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _run_ruff(
    ruff_path: str, py_files: List[str], repo_root: Optional[str]
) -> "tuple[int, str, str]":
    """Run `ruff check --select D1 <py_files>` in `repo_root`; never raises --
    mirrors `gate_dimension_types._run_mypy`'s own spawn-failure/timeout
    degrade shape."""
    try:
        result = subprocess.run(
            [ruff_path, "check", "--select", "D1", "--output-format=concise", *py_files],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_RUFF_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return (
            -1,
            "",
            f"ruff timed out after {_RUFF_TIMEOUT_SECS}s over {len(py_files)} file(s)",
        )
    except OSError as exc:
        print(f"skip: gate_dimension_docstrings._run_ruff failed: {exc}", file=sys.stderr)
        return -1, "", str(exc)


def _run_interrogate(
    interrogate_path: str, py_files: List[str], fail_under: float, repo_root: Optional[str]
) -> "tuple[int, str, str]":
    """Run `interrogate --fail-under <fail_under> <py_files>` in `repo_root`;
    never raises -- mirrors `_run_ruff` above."""
    try:
        result = subprocess.run(
            [interrogate_path, "--fail-under", str(fail_under), *py_files],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_INTERROGATE_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return (
            -1,
            "",
            f"interrogate timed out after {_INTERROGATE_TIMEOUT_SECS}s over {len(py_files)} file(s)",
        )
    except OSError as exc:
        print(f"skip: gate_dimension_docstrings._run_interrogate failed: {exc}", file=sys.stderr)
        return -1, "", str(exc)


def _check_docstrings(
    changed_files: List[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> DimensionResult:
    """The registered `docstrings` `DimensionCheck` (C3). See module
    docstring for tool resolution, scope, and the ratchet floor.
    `changed_files`/`diff_base` are accepted (fixed `DimensionCheck` shape)
    but unused -- see module docstring "SCOPE"."""
    del changed_files, diff_base  # unused by design; see module docstring "SCOPE"

    ruff_tool = resolve_tool("ruff")
    interrogate_tool = resolve_tool("interrogate")
    missing = [
        name
        for name, tool in (("ruff", ruff_tool), ("interrogate", interrogate_tool))
        if not tool.available
    ]
    if missing:
        reasons = "; ".join(
            tool.reason
            for name, tool in (("ruff", ruff_tool), ("interrogate", interrogate_tool))
            if not tool.available
        )
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.UNAVAILABLE,
            detail=f"tool(s) not available ({', '.join(missing)}): {reasons}",
        )

    fragment_paths = _load_ported_ops_paths(repo_root)
    if not fragment_paths:
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.UNAVAILABLE,
            detail=f"{_PORTED_OPS_FRAGMENT} missing or empty; nothing to check",
        )

    existing_paths = _existing_paths(repo_root, fragment_paths)
    if not existing_paths:
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.UNAVAILABLE,
            detail=(
                f"none of {len(fragment_paths)} path(s) in {_PORTED_OPS_FRAGMENT} "
                "exist on disk (stale fragment); nothing to check"
            ),
        )

    fail_under = _load_fail_under(repo_root)
    if fail_under is None:
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.UNAVAILABLE,
            detail=f"{_FLOOR_FILE} missing or malformed; no ratchet floor to gate against",
        )

    repo_root_str = str(repo_root) if repo_root is not None else None

    ruff_rc, ruff_out, ruff_err = _run_ruff(ruff_tool.path, existing_paths, repo_root_str)
    interrogate_rc, interrogate_out, interrogate_err = _run_interrogate(
        interrogate_tool.path, existing_paths, fail_under, repo_root_str
    )

    # Tool-broke case for either tool (neither 0 nor 1) -- reported as
    # UNAVAILABLE, never FAIL, mirroring gate_dimension_types._check_types'
    # rc==2 handling: a broken tool run must never masquerade as "found a
    # docstring gap".
    if ruff_rc not in (0, 1):
        last_err = ruff_err or ruff_out or f"ruff exited {ruff_rc} with no output"
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.UNAVAILABLE,
            detail=f"ruff did not complete a run (exit {ruff_rc}): {last_err}",
        )
    if interrogate_rc not in (0, 1):
        last_err = (
            interrogate_err or interrogate_out or f"interrogate exited {interrogate_rc} with no output"
        )
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.UNAVAILABLE,
            detail=f"interrogate did not complete a run (exit {interrogate_rc}): {last_err}",
        )

    ruff_failed = ruff_rc == 1
    interrogate_failed = interrogate_rc == 1
    if ruff_failed or interrogate_failed:
        details = []
        if ruff_failed:
            details.append(f"ruff D1xx: {ruff_out or ruff_err or 'violations found'}")
        if interrogate_failed:
            details.append(
                f"interrogate --fail-under {fail_under}: "
                f"{interrogate_out or interrogate_err or 'below floor'}"
            )
        return DimensionResult(
            dimension="docstrings",
            verdict=Verdict.FAIL,
            detail=" | ".join(details),
        )

    return DimensionResult(
        dimension="docstrings",
        verdict=Verdict.PASS,
        detail=(
            f"ruff D1xx clean and interrogate >= {fail_under}% over "
            f"{len(existing_paths)} ported-ops file(s)"
        ),
    )


register_dimension("docstrings", _check_docstrings)
