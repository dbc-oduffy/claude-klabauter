"""Process time and spawn count of `coordinator-invoke rea_ledger.write`, warm and cold.

Drives the registered op through the real CLI at default arguments against a throwaway ledger:
one batch per kind, one broken-chain revision, one non-canonical import. Process time and process
counts only, never wall clock. The "before" axis is the absent op (base has no
`rea_ledger.write`), so there is no baseline column.

The warm leg boots an isolated warm engine from a hardlink clone of this tree (the working tree
carries no build stamp, so it is never a warm-server host). A warm call's cost is the client's
process tree plus the CPU the engine tree charged during the call; the cold leg sets
`COORDINATOR_WARM=0` and the client tree carries all of it. Exits non-zero when a case does not
behave as specified or a warm call exceeds the 500ms bar.

    python coordinator_core/benchmarks/rea_ledger_write_bench.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.engine_op_overhead import WarmEngine, isolated_warm_engine  # noqa: E402
from coordinator_core.benchmarks.isolated_clone import mkdtemp_for_clone  # noqa: E402
from coordinator_core.benchmarks.process_time import batched_process_time_ms  # noqa: E402
from coordinator_core.rea_ledger.canonical import canonical_bytes, ledger_root, revision_digest  # noqa: E402

#: Warm process-time bar per case. Question: "does the writer fit the brightline?" (DR-344).
WARM_BUDGET_MS = 500.0

#: The production door; COORDINATOR_DOOR_ENGINE_ROOT (in the engine env) points it at the clone.
_DOOR = Path(os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings") / "bin" / (
    "coordinator-invoke.exe" if os.name == "nt" else "coordinator-invoke"
)

#: Ledger files after the sequence: one claim, one evidence record, one unknown chain. A refused
#: case returns {"error": ...} as a result (rc 0), so refusal is read off the disk: the non-canonical
#: claim adds no second claim file and the broken-chain revision adds no third chain line.
_EXPECTED_FILES = 3
_EXPECTED_CHAIN_LINES = 2


def _canon(record: dict) -> str:
    return canonical_bytes(record).decode("utf-8")


def _unknown(revision: int, previous: "str | None", question: str) -> dict:
    return {
        "unknown_id": "u-bench", "revision": revision, "previous_revision_digest": previous,
        "question": question, "coverage": "partial", "coverage_scope": "claim",
        "epistemic_mode": "observed", "source_authority": "shipped-artifact",
        "observed_at": "2026-11-01T12:00:00+00:00",
    }


def _cases() -> dict:
    """label -> (kind, [record text]), in the order the ledger sees them."""
    claim = {"id": "c1", "claim_text": "the parser keeps raw bytes"}
    evidence = {
        "schema_version": "evidence-record/v1", "id": "e1", "claim_id": "c1", "stance": "supports",
        "source_kind": "file", "source_ref": "src/parser.c", "locator": "10-20", "excerpt": None,
        "observed_at": "2026-11-01T12:00:00+00:00", "observer": "bench",
    }
    rev1 = _unknown(1, None, "what does X do?")
    rev2 = _unknown(2, revision_digest(rev1), "follow-up")
    broken = _unknown(3, "sha256:" + "0" * 64, "broken chain")
    return {
        "claim": ("claim", [_canon(claim)]),
        "evidence_record": ("evidence_record", [_canon(evidence)]),
        "unknown_rev1": ("unknown_revision", [_canon(rev1)]),
        "unknown_rev2": ("unknown_revision", [_canon(rev2)]),
        "unknown_broken_chain": ("unknown_revision", [_canon(broken)]),
        "claim_non_canonical": ("claim", [json.dumps({"id": "c2", "claim_text": "x"}, indent=2)]),
    }


def _engine_cpu_ms(pid: int) -> dict:
    """pid -> user+system ms for the engine process and every live descendant."""
    import psutil

    out = {}
    parent = psutil.Process(pid)
    for proc in [parent, *parent.children(recursive=True)]:
        try:
            t = proc.cpu_times()
            out[proc.pid] = (t.user + t.system) * 1000.0
        except psutil.Error:
            continue
    return out


def _client(engine: WarmEngine, warm: bool) -> list:
    """The CLI argv prefix: the production door when warm, the clone's Python CLI when cold (the
    door's cold path resolves the published engine, which lacks this op until it is published)."""
    if warm:
        return [str(_DOOR)]
    return [sys.executable, str(engine.root / "coordinator" / "bin" / "coordinator-invoke.py")]


def _measure(
    client_argv: list, engine: WarmEngine, env: dict, repo: Path, warm: bool, kind: str, records: list
) -> dict:
    """One call: client tree process time, plus the engine tree's CPU delta when warm."""
    params = json.dumps({"kind": kind, "records": records}, ensure_ascii=False)
    cmd = [*client_argv, "rea_ledger.write", params, "--repo", str(repo)]
    before = _engine_cpu_ms(engine.pid) if warm else {}
    client = batched_process_time_ms(cmd, k=1, env=env)
    after = _engine_cpu_ms(engine.pid) if warm else {}
    engine_ms = sum(ms - before.get(pid, 0.0) for pid, ms in after.items())
    return {
        "process_time_ms": client["process_time_ms"] + engine_ms,
        "client_ms": client["process_time_ms"],
        "engine_ms": engine_ms,
        "procs_per_call": client["procs_per_call"] + len(set(after) - set(before)),
        "rc": client["rc"],
    }


def _leg(engine: WarmEngine, warm: bool) -> dict:
    """Run every case once against a fresh ledger; label -> measurement."""
    env = {**engine.env, "COORDINATOR_WARM": "1" if warm else "0"}
    if not warm:
        env["COORDINATOR_ENGINE_ROOT"] = str(engine.root)
    client = _client(engine, warm)
    repo = mkdtemp_for_clone(_REPO, prefix="rea_write_bench_")
    try:
        (repo / ".git").mkdir()
        batched_process_time_ms([*client, "ping", "{}"], k=1, env=env)
        cases = _cases()
        out = {
            "claim": _measure(client, engine, env, repo, warm, *cases["claim"]),
            "evidence_record": _measure(client, engine, env, repo, warm, *cases["evidence_record"]),
            "unknown_rev1": _measure(client, engine, env, repo, warm, *cases["unknown_rev1"]),
            "unknown_rev2": _measure(client, engine, env, repo, warm, *cases["unknown_rev2"]),
            "unknown_broken_chain": _measure(client, engine, env, repo, warm, *cases["unknown_broken_chain"]),
            "claim_non_canonical": _measure(client, engine, env, repo, warm, *cases["claim_non_canonical"]),
        }
        root = ledger_root(repo)
        out["__ledger_files__"] = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        chain = root / "unknown_revision" / "u-bench.jsonl"
        out["__chain_lines__"] = len(chain.read_bytes().splitlines()) if chain.exists() else 0
        return out
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def main() -> int:
    declare_benchmark_origin()
    with isolated_warm_engine(_REPO, entrypoints=["coordinator-invoke"]) as engine:
        legs = {"warm": _leg(engine, True), "cold (COORDINATOR_WARM=0)": _leg(engine, False)}
    failures = []
    for mode, leg in legs.items():
        print(f"[{mode}] process_time_ms (client+engine) / procs_per_call / rc")
        for label, m in leg.items():
            if label.startswith("__"):
                continue
            print(
                f"  {label:22s} {m['process_time_ms']:8.1f} (client {m['client_ms']:.1f} + engine "
                f"{m['engine_ms']:.1f}) {m['procs_per_call']:4.1f} rc={m['rc']}"
            )
            if m["rc"] != 0:
                failures.append(f"{mode}/{label}: rc {m['rc']} != 0")
            if mode == "warm" and m["process_time_ms"] > WARM_BUDGET_MS:
                failures.append(f"warm/{label}: {m['process_time_ms']:.1f}ms exceeds {WARM_BUDGET_MS}ms")
        files = leg["__ledger_files__"]
        print(f"  ledger files: {files}; unknown chain lines: {leg['__chain_lines__']}")
        if len(files) != _EXPECTED_FILES or not any(f.startswith("unknown_revision") for f in files):
            failures.append(f"{mode}: expected claim, evidence and one unknown chain file, found {files}")
        if leg["__chain_lines__"] != _EXPECTED_CHAIN_LINES:
            failures.append(f"{mode}: chain has {leg['__chain_lines__']} lines, expected {_EXPECTED_CHAIN_LINES}")
    if failures:
        print("FAIL:\n  " + "\n  ".join(failures))
        return 1
    print("OK: every case behaved as specified; warm cases within the 500ms bar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
