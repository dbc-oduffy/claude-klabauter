"""Spike: process time of one batched caller/callee query per chunk against the example-retrieval-repo structural store.

Run once: `python -m coordinator_core.benchmarks.symbol_context_spike`. Prints JSON; no loop over the fleet.
Chunk selection (which files and symbols to simulate) is untimed setup; the timed figure covers the
read-only connect plus one query per simulated chunk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.hooks.example_retrieval_repo_detect import find_example_retrieval_repo_dir

CHUNKS = 16
SYMBOLS_PER_CHUNK = 4
LIMIT = 48
STORE = "structural_project.sqlite3"
TOTAL_BAR_MS = 500.0
STEP_BAR_MS = 200.0

_SELECT_CHUNKS = """
SELECT s.file, s.name FROM symbols s
JOIN (SELECT s2.file AS f, COUNT(*) AS n FROM calls c JOIN symbols s2 ON s2.id = c.callee_id
      WHERE s2.file LIKE 'coordinator_core/%' GROUP BY s2.file ORDER BY n DESC LIMIT ?) top ON top.f = s.file
WHERE s.kind IN ('function', 'method') AND s.id IN (SELECT callee_id FROM calls)
"""


def _query(n_names: int, n_files: int) -> str:
    # Approximates dispatch_emit.symbol_context._QUERY: lacks its authority filter and DISTINCT.
    nm = ",".join("?" * n_names)
    fl = ",".join("?" * n_files)
    return f"""
SELECT 'caller' AS role, p.name, p.file, p.line, e.name AS of
FROM symbols e JOIN calls c ON c.callee_id = e.id JOIN symbols p ON p.id = c.caller_id
WHERE e.name IN ({nm}) AND e.file IN ({fl})
UNION ALL
SELECT 'callee' AS role, p.name, p.file, p.line, e.name AS of
FROM symbols e JOIN calls c ON c.caller_id = e.id JOIN symbols p ON p.id = c.callee_id
WHERE e.name IN ({nm}) AND e.file IN ({fl})
LIMIT {LIMIT}
"""


def _simulated_chunks(conn: sqlite3.Connection) -> list[tuple[list[str], list[str]]]:
    by_file: dict[str, list[str]] = {}
    for file, name in conn.execute(_SELECT_CHUNKS, (CHUNKS,)):
        by_file.setdefault(file, []).append(name)
    chunks = []
    for file, names in by_file.items():
        chunks.append((sorted(set(names))[:SYMBOLS_PER_CHUNK], [file]))
    return chunks[:CHUNKS]


def main() -> int:
    rag = find_example_retrieval_repo_dir(os.getcwd())
    if rag is None:
        print(json.dumps({"error": "no-index"}))
        return 2
    uri = "file:" + os.path.join(rag, STORE) + "?mode=ro"
    setup = sqlite3.connect(uri, uri=True)
    chunks = _simulated_chunks(setup)
    setup.close()

    steps_ms: list[float] = []
    rows_per_chunk: list[int] = []
    t0 = time.process_time()
    conn = sqlite3.connect(uri, uri=True)
    connect_ms = (time.process_time() - t0) * 1000
    for names, files in chunks:
        t = time.process_time()
        rows = conn.execute(_query(len(names), len(files)), names + files + names + files).fetchall()
        steps_ms.append((time.process_time() - t) * 1000)
        rows_per_chunk.append(len(rows))
    total_ms = (time.process_time() - t0) * 1000
    conn.close()

    worst = max([connect_ms, *steps_ms])
    out = {
        "chunks": len(chunks),
        "rows_per_chunk": rows_per_chunk,
        "connect_ms": round(connect_ms, 2),
        "step_ms": [round(s, 2) for s in steps_ms],
        "worst_step_ms": round(worst, 2),
        "total_process_ms": round(total_ms, 2),
        "verdict": "PROCEED" if total_ms <= TOTAL_BAR_MS and worst <= STEP_BAR_MS else "PIVOT",
    }
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    declare_benchmark_origin()
    sys.exit(main())
