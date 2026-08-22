"""
coordinator_core.benchmarks.baseline_store -- Append-only, code_sha-keyed
conformance-record baseline store.

Purpose: durable measurement history for the qsub-01 latency benchmark
harness. Every call to `append()` writes ONE new JSONL line -- this store
is intentionally APPEND-ONLY, never overwrite-on-green. A `code_sha` may
accumulate multiple lines for the same `op` across repeated runs (e.g. two
benchmark passes on the same commit); readers that want "the latest
measurement for op X at sha Y" must select the last matching line
themselves (see `read_all` / `query`) -- this module does not collapse
history on write, because collapsing on write is exactly the
overwrite-on-green behavior this store is designed to avoid (the Data Science Reviewer C7).

Store layout (C3) -- TWO physically separate artifacts under
`benchmarks/baselines/`, never one file wearing two hats. Naming only "a
per-machine partition, tracked" would leave an implementer free to
conflate a field filter with a file; this module keeps them apart:

- `baselines/runs/<machine>.jsonl` -- the append-only RUN HISTORY, one file
  per machine, written by every `append()` call (this is `__main__.py`'s
  append target -- see `DEFAULT_STORE_PATH`). Stays gitignored
  (`coordinator_core/.gitignore`): it grows unboundedly and is dev-box
  churn, not evidence of record.
- `baselines/tracked-<machine>.jsonl` -- the curated, TRACKED baseline: one
  line per op, overwritten wholesale only by a deliberate refresh action
  (see `write_tracked_baseline()`), never appended to by an ordinary
  benchmark run. This is the file git tracks -- and it needs NO negation to
  do so: `coordinator_core/.gitignore` excludes only `benchmarks/baselines/runs/`,
  and this file lives outside that, directly under `baselines/`. A `!` negation
  would in fact be inert here, because git does not descend into an excluded
  directory (see that file's own comment). It is the one a future gate consumer
  (qsub-03) should read as "the baseline", not the runs history.

Refresh discipline (C1 section 4): refresh is keyed to the AMBIENT BAND the
box was under when measured, NOT a bare staleness/age cap -- an age check
only answers "is this number recent", never "was it measured at a
representative load", which is the question that decides whether a
comparison is honest. Every tracked entry carries its own
`ambient_before`/`ambient_after`/`ambient_delta` (record.py, C2) so a
consumer can see the band it was measured in, not just when it was
measured. Regeneration is a deliberate, human/CI-triggered action -- C8's
first population run populates the tracked partition for the first time;
after that, refreshing it is a documented re-run of the same action, never
a side effect of every benchmark invocation (see `write_tracked_baseline`).
A tracked baseline measured under a different ambient band than the
current comparison does not by itself fail a gate: per C4, a band
mismatch/unknown band downgrades the verdict to advisory, it never turns a
verdict red on its own.

`query()` partitions by `machine` (C3): a record measured on one box can
never silently become another box's baseline, and a `machine=None` record
(a pre-C2, v1 record) is never served as anyone's baseline at all -- see
`query()`'s docstring.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C7, § C3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, List, Optional

from coordinator_core.benchmarks.record import ConformanceRecord, compose_machine_id

BASELINES_DIR = Path(__file__).parent / "baselines"
"""Package-relative root of the baseline store's two partitions -- stable
regardless of caller cwd. Never write directly under this dir except via
`runs_path()`/`tracked_baseline_path()`: a file placed here loose would be
neither the ignored run history nor the tracked curated partition."""

RUNS_DIR = BASELINES_DIR / "runs"
"""The ignored, append-only run-history partition's directory. Every file
under here is one machine's unbounded append log -- see `runs_path()`."""


def runs_path(machine: Optional[str] = None) -> Path:
    """Return the per-machine run-history JSONL path: `baselines/runs/<machine>.jsonl`.

    Purpose: `append()`'s default target. `machine` defaults to this box's
    own identity (`compose_machine_id()`) so a bare call always appends to
    THIS box's own run history, never another box's file."""
    resolved_machine = machine if machine is not None else compose_machine_id()
    return RUNS_DIR / f"{resolved_machine}.jsonl"


def tracked_baseline_path(machine: Optional[str] = None) -> Path:
    """Return the per-machine curated, tracked baseline JSONL path:
    `baselines/tracked-<machine>.jsonl` -- a file directly under
    `baselines/`, never `baselines/<machine>/...` (git does not descend
    into the excluded `baselines/` directory, so a negation can only ever
    re-include a file living directly inside it -- see module docstring
    and `coordinator_core/.gitignore`). `machine` defaults to this box's
    own identity."""
    resolved_machine = machine if machine is not None else compose_machine_id()
    return BASELINES_DIR / f"tracked-{resolved_machine}.jsonl"


_DEFAULT_STORE_PATH_CACHE: Optional[Path] = None


def __getattr__(name: str) -> Path:
    """Resolve `DEFAULT_STORE_PATH` on first read rather than at import.

    This box's default run-history JSONL path -- `__main__.py`'s append
    target. Still resolved once and cached, and `compose_machine_id()` is
    stable for the lifetime of a process, so the value is unchanged; only
    the moment of resolution moves.

    Negative-spec: this is NOT a lazy-import convenience. `compose_machine_id()`
    calls `platform.system()`, whose first call costs ~55-88ms on Windows, and
    this module is dragged into `coordinator_core.ops`'s eager registration by
    `ops/gate_dimension_latency.py`. Computing a default-argument constant at
    import time therefore charged that cost to every eager `import
    coordinator_core.ops` -- ~283 ops paying a benchmark store's hostname
    lookup. Do not restore a module-level `DEFAULT_STORE_PATH = runs_path()`.
    PEP 562 keeps the read API (and `monkeypatch.setattr`, which the
    gate_dimension_latency tests rely on) working unchanged.
    """
    if name == "DEFAULT_STORE_PATH":
        global _DEFAULT_STORE_PATH_CACHE
        if _DEFAULT_STORE_PATH_CACHE is None:
            _DEFAULT_STORE_PATH_CACHE = runs_path()
        return _DEFAULT_STORE_PATH_CACHE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _resolve_path(path: Optional[Path]) -> Path:
    """Return the caller-supplied path, or DEFAULT_STORE_PATH if None.

    Reads through `globals()` before the PEP 562 `__getattr__` fallback:
    module-level `__getattr__` fires on module ATTRIBUTE access, never on a
    global-name lookup inside this module, so a bare `DEFAULT_STORE_PATH` here
    would raise NameError. The globals() probe is also what keeps
    `monkeypatch.setattr(baseline_store, "DEFAULT_STORE_PATH", ...)` effective
    for this function -- monkeypatch writes a real module global, which must
    win over the cached value.
    """
    if path is not None:
        return path
    override = globals().get("DEFAULT_STORE_PATH")
    return override if override is not None else __getattr__("DEFAULT_STORE_PATH")


def append(record: ConformanceRecord, path: Optional[Path] = None) -> Path:
    """Append one ConformanceRecord as a single JSONL line to the store.

    Purpose: the store's sole write path. Never truncates, never rewrites
    existing lines -- opens in append mode and creates parent directories
    on first use. Returns the resolved store path actually written to.
    """
    store_path = _resolve_path(path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with store_path.open("a", encoding="utf-8") as f:
        f.write(record.to_json())
        f.write("\n")
    return store_path


def read_all(path: Optional[Path] = None) -> List[ConformanceRecord]:
    """Read every record in the store, in append order.

    Purpose: the store's full-history read path. Returns an empty list if
    the store file does not exist yet (a fresh store has no history, not
    an error). Blank lines are skipped defensively.
    """
    store_path = _resolve_path(path)
    if not store_path.exists():
        return []
    records: List[ConformanceRecord] = []
    with store_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(ConformanceRecord.from_json(line))
    return records


def query(
    op: Optional[str] = None,
    code_sha: Optional[str] = None,
    machine: Optional[str] = None,
    path: Optional[Path] = None,
) -> Iterator[ConformanceRecord]:
    """Yield records from the store filtered by op, code_sha, and/or machine.

    Purpose: the store's scoped-read path -- lets a consumer (e.g. a future
    qsub-03 gate) ask "every record for op X" or "every record at sha Y"
    without reading history it doesn't need. All filters are optional and
    compose as AND; omitting all of them is equivalent to read_all() as an
    iterator, MINUS the machine partition dropped below. Does not collapse
    duplicates -- a caller wanting the latest entry for (op, code_sha) must
    select the last yielded match itself, matching the module's
    append-only, no-collapse-on-write contract.

    Machine partition (C3): a record with `machine is None` (a pre-C2, v1
    record) is NEVER yielded, regardless of whether `machine` is passed --
    a record with no machine identity names no box its timings are valid
    under, so it can never be served as anyone's baseline. When `machine`
    IS passed, only records whose `record.machine == machine` are yielded
    -- a record measured on one box can never silently become another
    box's baseline. `machine=None` (the default) yields every
    machine-tagged record across every box; pass an explicit `machine` to
    scope to one box's history.
    """
    for record in read_all(path):
        if record.machine is None:
            continue
        if op is not None and record.op != op:
            continue
        if code_sha is not None and record.code_sha != code_sha:
            continue
        if machine is not None and record.machine != machine:
            continue
        yield record


def write_tracked_baseline(
    records: List[ConformanceRecord],
    machine: Optional[str] = None,
    path: Optional[Path] = None,
) -> Path:
    """Overwrite the curated, tracked baseline partition with `records`,
    one line per op, sorted by op for a deterministic diff.

    Purpose: the tracked partition's SOLE write path -- the deliberate
    refresh action named in the module docstring's Refresh discipline
    section (C8's first population run; a documented re-run thereafter).
    This is a wholesale OVERWRITE, the deliberate opposite of `append()`'s
    contract: the tracked partition is curated (one line per op, the
    latest trusted measurement), not a growing history, so collapsing to
    the given `records` on every call is correct here and would be wrong
    for the run-history partition. Never called from a benchmark run's
    ordinary path (`__main__.py` calls `append()`, not this).

    Does not itself decide which ambient band is "trustworthy" or whether
    a refresh is due -- that policy lives with the caller (C8's refresh
    action); this function's only job is writing the given records out
    as the new tracked partition content. Raises no exception for an
    empty `records` list -- writes an empty file, mirroring `append()`'s
    own no-surprises-on-empty-input posture.
    """
    store_path = path if path is not None else tracked_baseline_path(machine)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda r: r.op)
    with store_path.open("w", encoding="utf-8") as f:
        for record in ordered:
            f.write(record.to_json())
            f.write("\n")
    return store_path


def read_tracked_baseline(
    machine: Optional[str] = None, path: Optional[Path] = None
) -> List[ConformanceRecord]:
    """Read the curated, tracked baseline partition in full.

    Purpose: the tracked partition's read counterpart to
    `write_tracked_baseline()` -- returns an empty list if the tracked
    file does not exist yet (before C8's first refresh has ever run, not
    an error)."""
    store_path = path if path is not None else tracked_baseline_path(machine)
    return read_all(store_path)
