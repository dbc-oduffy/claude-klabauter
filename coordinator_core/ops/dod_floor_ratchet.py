"""
coordinator_core.ops.dod_floor_ratchet — the per-dimension DoD floor ratchet
(C9 leg (c), docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C9).

Purpose: give every DoD dimension (docstrings, types, tests, review, ...) a
floor artifact that can only ever be RAISED, mechanically -- never lowered,
never hand-typed. C3 shipped `.github/docstring-coverage-floor.json` as one
dimension's own ad hoc floor file, ratchet-only "by convention" (its
`ratchet_rule` field is prose a reviewer has to notice, not a mechanism). C9
leg (c) generalizes that shape into a shared seam every dimension can reuse,
and makes the ratchet-only property a refusal any caller hits, not a norm a
diff can silently violate.

ARTIFACT SHAPE -- per-dimension file family, not one shared multi-dimension
file. Two options were on the table:
  (A) one shared `.github/dod-floors.json` keyed by dimension name, or
  (B) one file per dimension, `.github/<dimension>-floor.json` (docstrings
      keeps its existing landed filename; every other dimension gets its own
      file the first time something calls `raise_floor()` for it).
(B) is what this module implements. Reasons: (1) requirement 2's own text
("its current consumer must keep working unchanged") is satisfied for free
under (B) -- `gate_dimension_docstrings._load_fail_under` already reads
`.github/docstring-coverage-floor.json` verbatim by its exact landed path and
`{"fail_under": ...}` shape; (B) does not move, rename, or reshape that file
at all, it only recognizes it as the "docstrings" member of a family sharing
one schema. (A) would require either migrating that file's bytes into a
nested key (touching a committed, tested consumer this chunk's anti-scope
forbids editing) or running two divergent on-disk shapes side by side
forever. (2) A per-dimension file is the natural unit of "what changed" in a
diff -- a PR that raises the types floor produces a one-file diff readable
without a JSON-path into a shared blob, the same reviewability C3's own file
already has. (3) No dimension needs to read another dimension's floor
atomically with its own (each dimension module only ever reads its own
floor), so the shared-file's one advantage -- atomic multi-dimension reads --
buys nothing here.

THE RATCHET -- `write_floor()` is the ONLY write path this module exposes.
It is refused, with a `RatchetRefused` naming the stored and attempted
values, whenever the new value is LOWER than the stored one. Equal is a
no-op (the file is left untouched, byte-for-byte, and no exception is
raised -- an idempotent re-measurement landing on the same value is not an
error). Three stored-floor states are distinguished, not two: a genuinely
ABSENT file is "no floor yet" and the first `write_floor()` call for a
dimension always succeeds and creates the file; a READABLE file is ratchet-
compared as above; a PRESENT-BUT-MALFORMED file (unparseable JSON, wrong
shape, non-numeric/out-of-range value) raises `StoredFloorUnreadable` and
refuses the write outright -- corruption is an error state a human repairs
or deletes deliberately, never a silent reset to whatever the next caller
measures. This is mechanical, not conventional: nothing about the call
shape lets a caller skip the comparison the way a hand-edited JSON file
always could.

MEASUREMENT SEAM -- `write_floor()` takes a `FloorMeasurement`, never a bare
number a caller could invent. A `FloorMeasurement` is produced by a
`Measurer` (a zero-argument callable returning one), and `MEASURERS` is a
name -> `Measurer` registry a dimension module registers into (mirroring
`gate_validate_invocable.register_dimension`'s own self-registration shape).
`raise_floor()` is the CLI/orchestration-facing entrypoint: it looks up the
registered measurer for a dimension, calls it, and feeds the result to
`write_floor()` -- there is no code path from "a human typed a percentage on
the command line" to a floor file being written. Tests exercise `write_floor`
directly with a synthetic `FloorMeasurement` (the seam this module documents
as swappable) and exercise `raise_floor` with a registered fake `Measurer`,
never a real tool invocation (mypy/ruff/interrogate/diff-cover are all
unresolvable on this machine per C1b -- see `gate_tool_resolve`).

Only "docstrings" has a real, registered measurer as of this chunk (it
reruns the same `interrogate` invocation `gate_dimension_docstrings._run_
interrogate` already performs, over the same C6 ported-ops path set, and
parses interrogate's own `TOTAL` percentage out of its stdout -- interrogate
prints a coverage summary table whose last data row's last column is the
actual percentage). "types" / "tests" / "review_stamp" / "latency" have no
measurer registered yet -- `raise_floor()` for an unregistered dimension
raises a legible `KeyError`-shaped `ValueError` naming which dimension has no
measurer and pointing at `register_measurer`, rather than silently doing
nothing or accepting a hand-typed value as a fallback.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C9
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from coordinator_core.win_portability import no_console_creationflags

_INTERROGATE_TIMEOUT_SECS = 120
_CREATIONFLAGS = no_console_creationflags()

# dimension -> floor filename, relative to repo_root. "docstrings" is C3's
# already-landed, already-tested filename -- kept verbatim (see module
# docstring "ARTIFACT SHAPE"). Every other dimension gets a filename this
# module mints the first time `write_floor()` is called for it; no file is
# pre-created for a dimension that has never been measured.
_FLOOR_FILENAMES: Dict[str, str] = {
    "docstrings": "docstring-coverage-floor.json",
}


def _floor_filename(dimension: str) -> str:
    """The on-disk filename for `dimension`'s floor file, relative to
    `.github/`. Falls back to `<dimension>-floor.json` for any dimension not
    named in `_FLOOR_FILENAMES` (i.e. everything except "docstrings", whose
    landed filename predates this module and does not follow the pattern)."""
    return _FLOOR_FILENAMES.get(dimension, f"{dimension}-floor.json")


def floor_path(dimension: str, repo_root: Optional[Path] = None) -> Path:
    """Full path to `dimension`'s floor file. `repo_root` is `Optional[Path]`
    like every other DoD-gate module's own `repo_root` param -- `None` means
    cwd-relative, mirroring `gate_dimension_docstrings._resolve`."""
    base = repo_root if repo_root is not None else Path(".")
    return Path(base) / ".github" / _floor_filename(dimension)


class RatchetRefused(ValueError):
    """Raised by `write_floor()` when an attempted floor is lower than the
    stored one. Carries the stored and attempted values as attributes (not
    just baked into the message) so a caller/test can assert on them without
    parsing prose."""

    def __init__(self, dimension: str, stored: float, attempted: float) -> None:
        self.dimension = dimension
        self.stored = stored
        self.attempted = attempted
        super().__init__(
            f"dod_floor_ratchet.write_floor: refused to lower the {dimension!r} "
            f"floor from {stored} to {attempted} -- a floor may only be raised. "
            f"If {attempted} is a genuine re-measurement showing regression, "
            "that regression is the thing to fix, not this file."
        )


class StoredFloorUnreadable(ValueError):
    """Raised by `write_floor()` when a floor file exists on disk but cannot
    be parsed into a valid `{"fail_under": <number>}` record -- unparseable
    JSON, the wrong shape, or a non-numeric/out-of-range value. Distinct from
    `RatchetRefused`: this is not a ratchet comparison, it is a refusal to
    treat corruption as "no floor recorded yet". Carries `path` so a caller
    can act on it without parsing the message."""

    def __init__(self, dimension: str, path: Path) -> None:
        self.dimension = dimension
        self.path = path
        super().__init__(
            f"dod_floor_ratchet.write_floor: stored floor for {dimension!r} "
            f"is unreadable: {path}. Repair or delete the file."
        )


@dataclass(frozen=True)
class FloorMeasurement:
    """One real measurement of a dimension's coverage/quality percentage,
    with its own provenance. Never hand-constructed from a CLI-supplied
    number in production code -- see module docstring "MEASUREMENT SEAM".
    Tests construct this directly; that is the documented seam, not a
    loophole in it."""

    value: float
    head_sha: str
    measured_via: str


Measurer = Callable[[Optional[Path]], FloorMeasurement]
"""A zero-config (besides `repo_root`) callable that performs a real
measurement and returns its result. Registered per-dimension via
`register_measurer`; see module docstring "MEASUREMENT SEAM"."""

MEASURERS: Dict[str, Measurer] = {}


def register_measurer(dimension: str, measurer: Measurer) -> None:
    """Register `measurer` as the `Measurer` for `dimension`. Mirrors
    `gate_validate_invocable.register_dimension`'s self-registration shape --
    a dimension module calls this at import time rather than this module
    hardcoding a per-dimension dispatch table that would have to import every
    dimension module eagerly (and risk the import-order clobbering
    `test_gate_dimension_docstrings.py` / `test_gate_validate_invocable.py`
    already guard against)."""
    MEASURERS[dimension] = measurer


def _read_floor_state(dimension: str, repo_root: Optional[Path] = None):
    """Tri-state stored-floor read: `("absent", None)`, `("malformed", None)`,
    or `("ok", payload)`. The one place the absent/malformed/readable
    distinction is made -- `read_floor()` and `write_floor()` both derive
    their behavior from this rather than each re-deriving it (module
    docstring shape-check reuse requirement)."""
    path = floor_path(dimension, repo_root)
    if not path.is_file():
        return "absent", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "malformed", None
    if not isinstance(payload, dict) or "fail_under" not in payload:
        return "malformed", None
    try:
        float(payload["fail_under"])
    except (TypeError, ValueError):
        return "malformed", None
    return "ok", payload


def read_floor(dimension: str, repo_root: Optional[Path] = None) -> Optional[dict]:
    """Read `dimension`'s stored floor record. Returns `None` if the file is
    missing or malformed (not a partial dict) -- mirrors `gate_dimension_
    docstrings._load_fail_under`'s own tolerant-missing/malformed handling,
    generalized to the whole record rather than just the `fail_under`
    field. This function alone does not distinguish absent from malformed
    (both read as `None`) -- `write_floor()` uses `_read_floor_state`
    directly to make that distinction where it matters: a malformed file is
    a corrupt ratchet, not a fresh start."""
    _, payload = _read_floor_state(dimension, repo_root)
    return payload


def write_floor(
    dimension: str,
    measurement: FloorMeasurement,
    repo_root: Optional[Path] = None,
    *,
    purpose: Optional[str] = None,
) -> Optional[Path]:
    """The one write path for a dimension's floor file. Raises
    `RatchetRefused` if `measurement.value` is lower than the stored
    `fail_under`. Is a no-op (returns `None`, no write) if equal. Writes and
    returns the path if `measurement.value` is higher, or no stored floor
    exists yet.

    `measurement` must be a `FloorMeasurement` -- there is no overload
    accepting a bare `float`, by design (module docstring "MEASUREMENT
    SEAM").

    A present-but-unreadable stored floor raises `StoredFloorUnreadable`
    rather than being treated as "no floor yet" -- only a genuinely absent
    file is a fresh start."""
    path = floor_path(dimension, repo_root)
    state, existing = _read_floor_state(dimension, repo_root)

    if state == "malformed":
        raise StoredFloorUnreadable(dimension, path)

    if state == "ok":
        stored = float(existing["fail_under"])
        if measurement.value < stored:
            raise RatchetRefused(dimension, stored, measurement.value)
        if measurement.value == stored:
            return None

    record = {
        "_purpose": purpose
        or (
            f"Ratchet floor for the {dimension!r} DoD dimension "
            "(C9, docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md "
            "§ C9). RULE: fail_under may only be RAISED, never lowered; "
            "written exclusively by coordinator_core.ops.dod_floor_ratchet."
            "write_floor -- never hand-edit this number."
        ),
        "fail_under": measurement.value,
        "measured_at_head_sha": measurement.head_sha,
        "measured_via": measurement.measured_via,
        "ratchet_rule": (
            "monotonic non-decreasing; update this value only by calling "
            "dod_floor_ratchet.write_floor with a real FloorMeasurement -- "
            "never by hand-tuning it down to make a run pass"
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def raise_floor(dimension: str, repo_root: Optional[Path] = None) -> Optional[Path]:
    """Look up `dimension`'s registered `Measurer`, run it, and feed the
    result to `write_floor()`. Raises `ValueError` naming the dimension if no
    measurer is registered for it -- there is no fallback to a hand-typed
    value (module docstring "MEASUREMENT SEAM")."""
    measurer = MEASURERS.get(dimension)
    if measurer is None:
        raise ValueError(
            f"dod_floor_ratchet.raise_floor: no measurer registered for "
            f"dimension {dimension!r} (registered: {sorted(MEASURERS)}); "
            "call register_measurer() before raise_floor() can run for it -- "
            "a hand-typed value is not accepted as a substitute."
        )
    measurement = measurer(repo_root)
    return write_floor(dimension, measurement, repo_root)


# ---------------------------------------------------------------------------
# The one production measurer registered by this module itself: "docstrings",
# reusing the same interrogate invocation shape `gate_dimension_docstrings.
# _run_interrogate` performs, over the same C6 ported-ops path set. Kept in
# this module (not imported from `gate_dimension_docstrings`, whose private
# helpers are that module's own) so this module has zero import-order
# coupling to it -- consistent with `gate_dimension_docstrings.py`'s own
# "this module never edits/depends on sibling internals" convention.
# ---------------------------------------------------------------------------

_PORTED_OPS_FRAGMENT = ".github/ported-ops-paths.txt"
# Review: coordinator:code-reviewer-3c4f24d7 -- anchored to line-start (MULTILINE)
# and matches the LAST such line, not the first `re.search` hit anywhere in the
# blob. interrogate's real summary TOTAL row is its last "TOTAL ..." line; a
# per-file row whose docstring/path happens to contain the literal token
# "TOTAL" followed by a stray percentage earlier in the output no longer wins.
_INTERROGATE_TOTAL_RE = re.compile(r"^TOTAL[^\d]*?(\d+(?:\.\d+)?)\s*%", re.MULTILINE)


def _load_ported_ops_paths(repo_root: Optional[Path]) -> list:
    base = repo_root if repo_root is not None else Path(".")
    fragment_path = Path(base) / _PORTED_OPS_FRAGMENT
    if not fragment_path.is_file():
        return []
    lines = fragment_path.read_text(encoding="utf-8").splitlines()
    paths = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    return [p for p in paths if (Path(base) / p).is_file()]


def _git_head_sha(repo_root: Optional[Path]) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root) if repo_root is not None else None,
            timeout=30,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        sha = result.stdout.strip()
        return sha if result.returncode == 0 and sha else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _measure_docstrings(repo_root: Optional[Path]) -> FloorMeasurement:
    """Production `Measurer` for the "docstrings" dimension. Requires
    `interrogate` on PATH (via `gate_tool_resolve.resolve_tool`) -- raises
    `RuntimeError` naming the gap if it is not, rather than fabricating a
    value. Not exercised by this chunk's tests directly (interrogate is
    unresolvable on the authoring machine per C1b); tests stub the
    `Measurer` seam instead, per module docstring "MEASUREMENT SEAM"."""
    from coordinator_core.ops.gate_tool_resolve import resolve_tool

    tool = resolve_tool("interrogate")
    if not tool.available:
        raise RuntimeError(f"dod_floor_ratchet._measure_docstrings: {tool.reason}")

    paths = _load_ported_ops_paths(repo_root)
    if not paths:
        raise RuntimeError(
            f"dod_floor_ratchet._measure_docstrings: {_PORTED_OPS_FRAGMENT} "
            "missing or empty; nothing to measure"
        )

    result = subprocess.run(
        [tool.path, *paths],
        capture_output=True,
        text=True,
        cwd=str(repo_root) if repo_root is not None else None,
        timeout=_INTERROGATE_TIMEOUT_SECS,
        stdin=subprocess.DEVNULL,
        **_CREATIONFLAGS,
    )
    out = result.stdout
    matches = list(_INTERROGATE_TOTAL_RE.finditer(out))
    match = matches[-1] if matches else None
    if match is None:
        raise RuntimeError(
            "dod_floor_ratchet._measure_docstrings: could not parse a TOTAL "
            f"percentage out of interrogate's output: {out!r}"
        )
    value = float(match.group(1))
    return FloorMeasurement(
        value=value,
        head_sha=_git_head_sha(repo_root),
        measured_via=f"python -m interrogate over {len(paths)} ported-ops path(s), interrogate output",
    )


register_measurer("docstrings", _measure_docstrings)


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="raise a DoD dimension's ratchet floor from a real measurement"
    )
    parser.add_argument(
        "dimension",
        help=f"one of the registered dimensions: {sorted(MEASURERS)}",
    )
    args = parser.parse_args(argv)

    try:
        result = raise_floor(args.dimension)
    except (ValueError, RuntimeError, RatchetRefused, StoredFloorUnreadable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result is None:
        print(f"{args.dimension}: no change (measurement equals the stored floor)")
    else:
        print(f"{args.dimension}: floor raised, wrote {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
