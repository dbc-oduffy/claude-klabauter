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

Default store path: `coordinator_core/benchmarks/baselines/baseline.jsonl`,
resolved relative to this module's own file location so the store lives
inside the package regardless of the caller's cwd. Callers (e.g. the CLI
runner, __main__.py) may pass an explicit path to redirect appends/reads
elsewhere (tests use this to avoid mutating the shipped baseline file).

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C7.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, List, Optional

from coordinator_core.benchmarks.record import ConformanceRecord

DEFAULT_STORE_PATH = Path(__file__).parent / "baselines" / "baseline.jsonl"
"""Package-relative default JSONL store path -- stable regardless of cwd."""


def _resolve_path(path: Optional[Path]) -> Path:
    """Return the caller-supplied path, or DEFAULT_STORE_PATH if None."""
    return path if path is not None else DEFAULT_STORE_PATH


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
    path: Optional[Path] = None,
) -> Iterator[ConformanceRecord]:
    """Yield records from the store filtered by op and/or code_sha.

    Purpose: the store's scoped-read path -- lets a consumer (e.g. a future
    qsub-03 gate) ask "every record for op X" or "every record at sha Y"
    without reading history it doesn't need. Both filters are optional and
    compose as AND; omitting both is equivalent to read_all() as an
    iterator. Does not collapse duplicates -- a caller wanting the latest
    entry for (op, code_sha) must select the last yielded match itself,
    matching the module's append-only, no-collapse-on-write contract.
    """
    for record in read_all(path):
        if op is not None and record.op != op:
            continue
        if code_sha is not None and record.code_sha != code_sha:
            continue
        yield record
