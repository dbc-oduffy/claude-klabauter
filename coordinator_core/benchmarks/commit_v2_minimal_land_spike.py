"""Spike: a minimal scoped land of only the requirement inventory's kept set, bracketed per phase.

Inventory: docs/research/2026-10-24-commit-v2-pre-land-requirement-inventory.md (kept set and
per-phase budget). Written from the goal, not from `commit_v2._handler_body`; it imports no
`commit_v2` private helper and lands through `commit_paths`. `main` lands real commits on the
repo it runs in, so only the EM runs it. Two clocks: `process_ms` (`time.process_time()` delta,
the per_op_handler clock, excludes git children) and `child_inclusive_ms` (PhaseLedger total).
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from coordinator_core.ops.ceremony.commit_v2_phases import PhaseLedger

BAR_MS = 500.0

#: Child-inclusive per-phase budget (inventory "Kept set"), ms. Sums to 397; the rest is headroom.
BUDGET_MS: dict[str, float] = {
    "param_validation": 5.0,
    "main_commit_refusal": 5.0,
    "head_classification": 25.0,
    "resolve_session_id": 5.0,
    "claim_lookup": 110.0,
    "pre_commit_gates": 50.0,
    "eol_drift": 5.0,
    "apply_missing_trailers": 15.0,
    "skill_weight_check": 5.0,
    "publish_allowlist_drift": 5.0,
    "split_process_paths": 2.0,
    "land.product": 90.0,
    "land.process": 90.0,
    "release_committed_claims": 35.0,
    "record_ledger": 15.0,
    "post_land_reply": 5.0,
    "unbracketed": 20.0,
}

#: Kept post-land steps that need the live session or claim substrate, so the harness cannot run
#: them; charged at the inventory budget and subtracted from the AT-BAR threshold.
RESERVED_MS: dict[str, float] = {"release_committed_claims": 35.0, "record_ledger": 15.0}

#: Phases `minimal_land` brackets on a one-class call; `land.process` replaces `land.product`
#: when every path is process class.
KEPT_PHASES = (
    "param_validation",
    "main_commit_refusal",
    "head_classification",
    "resolve_session_id",
    "claim_lookup",
    "pre_commit_gates",
    "eol_drift",
    "apply_missing_trailers",
    "skill_weight_check",
    "publish_allowlist_drift",
    "split_process_paths",
    "land.product",
    "post_land_reply",
)

_ABSORBED = "Absorbed-Peer-Claim"


def threshold_ms() -> float:
    """AT-BAR threshold for both totals: the bar minus the reserved post-land lines."""
    return BAR_MS - sum(RESERVED_MS.values())


class _SpawnCount:
    """Counts `subprocess.Popen` constructions while active (seam-independent)."""

    def __init__(self) -> None:
        self.n = 0

    def __enter__(self) -> "_SpawnCount":
        counter = self
        self._orig = subprocess.Popen

        class _Counting(self._orig):  # type: ignore[misc, name-defined]
            def __init__(self, *a, **k):
                counter.n += 1
                super().__init__(*a, **k)

        subprocess.Popen = _Counting  # type: ignore[misc]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen = self._orig  # type: ignore[misc]


def _insert_after_subject(message: str, lines: Sequence[str]) -> str:
    head, sep, rest = message.partition("\n\n")
    block = "\n".join(lines)
    return f"{head}\n\n{block}\n\n{rest}" if sep else f"{message.rstrip(chr(10))}\n\n{block}\n"


def _run_gates(root: Path, paths: list[str]) -> tuple[Optional[str], dict[str, float]]:
    """Run G1-G9 in order over the tree this commit lands; returns (refusal, per-gate thread ms)."""
    from coordinator_core.authoring_leaks import leak_gate
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.ceremony import commit_gates as g
    from coordinator_core.ops.ceremony.commit_source import post_commit_reader

    read_source = post_commit_reader(
        root, resolve_git_common_dir(root), paths=paths, deleted_paths=()
    )
    gates: list[tuple[str, Callable[[], object]]] = [
        ("declared_deletion_gate", lambda: g.declared_deletion_gate(root, paths, ())),
        ("carry_gate", lambda: g.carry_gate(root, paths)),
        ("residue_gate", lambda: g.residue_gate(root, paths)),
        ("op_scope_coverage_gate", lambda: g.op_scope_coverage_gate(root, paths, read_source)),
        ("leak_gate", lambda: leak_gate(root, paths)),
        ("registration_quad_gate", lambda: g.registration_quad_gate(root, paths, read_source)),
        ("claude_md_budget_gate", lambda: g.claude_md_budget_gate(root, paths, read_source)),
        ("machine_path_leak_gate", lambda: g.machine_path_leak_gate(root, paths, read_source)),
        ("attribution_gate", lambda: g.attribution_gate(root, paths, vacated=())),
    ]
    gate_ms: dict[str, float] = {}
    for name, run in gates:
        t0 = time.thread_time()
        outcome = run()
        gate_ms[name] = (time.thread_time() - t0) * 1000.0
        if not outcome.passed:  # type: ignore[attr-defined]
            return "\n".join(outcome.diagnostics) or f"{name} refused", gate_ms  # type: ignore[attr-defined]
    return None, gate_ms


def _ignored_new_paths(root: Path, paths: list[str]) -> list[str]:
    """Paths absent from the index that a `.gitignore` rule matches."""
    from coordinator_core.git.git_index import parse_index_identity
    from coordinator_core.ops.ceremony.git_native import check_ignore, parse_check_ignore_stdin_z

    tracked = parse_index_identity(root, wanted=set(paths))
    fresh = [p for p in paths if p not in tracked]
    if not fresh:
        return []
    # Spawn reason: .gitignore rules have no in-process evaluator; asked only for a path the index lacks.
    res = check_ignore(root, fresh)
    if res.returncode not in (0, 1):
        return []
    matched = {m[3] for m in parse_check_ignore_stdin_z(res.stdout)}
    return [p for p in fresh if p in matched]


def _peer_disclosures(root: Path, paths: list[str], own_sid: str) -> list[str]:
    """One `claim_index.lookup`; a `Absorbed-Peer-Claim` line per live non-self claimant."""
    from coordinator_core.session import claim_index
    from coordinator_core.session import liveness
    from coordinator_core.session import touch_record

    try:
        found = claim_index.lookup(list(paths), cwd=str(root))
    except Exception:  # noqa: BLE001 -- an unanswerable claim state never refuses the land
        return []
    lines: list[str] = []
    for path in paths:
        kinds = (found.recorded_kind or {}).get(path, {})
        for sid in found.get(path, []):
            if sid == own_sid or sid == claim_index.UNANSWERABLE:
                continue
            if not touch_record.kind_blocks_a_peer_commit(kinds.get(sid)):
                continue
            try:
                live = liveness.session_live(sid, str(root))
            except Exception:  # noqa: BLE001
                continue
            if live:
                lines.append(f"{_ABSORBED} {sid} {path}")
    return lines


def minimal_land(
    worktree_root: Path,
    paths: list[str],
    message: str,
    *,
    ledger: PhaseLedger,
    session_id: Optional[str] = None,
) -> dict:
    """Land exactly `paths` with `message` plus trailers; never raises past a refusal.

    Returns `{"sha", "phases_ms", "process_ms", "child_inclusive_ms", "spawns", "gate_ms"}`, or
    `{"error", ...}` with the same timing keys.
    """
    root = Path(worktree_root)
    gate_ms: dict[str, float] = {}
    p0 = time.process_time()
    sc = _SpawnCount()

    def _out(**body: object) -> dict:
        phases = ledger.phases_ms()
        body.update(
            phases_ms=phases,
            process_ms=(time.process_time() - p0) * 1000.0,
            child_inclusive_ms=sum(phases.values()),
            spawns=sc.n,
            gate_ms=gate_ms,
        )
        return body

    with sc:
        try:
            with ledger.phase("param_validation"):
                bad = (
                    "no paths"
                    if not paths
                    else "empty message"
                    if not message.strip()
                    else next((f"directory in paths: {p}" for p in paths if (root / p).is_dir()), None)
                )
            if bad:
                return _out(error=bad)

            with ledger.phase("main_commit_refusal"):
                from coordinator_core.git.main_commit_policy import main_commit_refusal

                refused = main_commit_refusal(root, override_reason=None)
            if refused:
                return _out(error=refused)

            with ledger.phase("head_classification"):
                ignored = _ignored_new_paths(root, paths)
            if ignored:
                return _out(error=f"gitignored path(s) refused: {', '.join(ignored)}")

            with ledger.phase("resolve_session_id"):
                sid = session_id
                if not sid:
                    from coordinator_core.session import core as session_core

                    sid = session_core.resolve_session_id(str(root))

            with ledger.phase("claim_lookup"):
                holds = _peer_disclosures(root, paths, sid or "")
                if holds:
                    message = _insert_after_subject(message, holds)

            with ledger.phase("pre_commit_gates"):
                gate_refusal, gate_ms = _run_gates(root, paths)
            if gate_refusal:
                return _out(error=gate_refusal)

            with ledger.phase("eol_drift"):
                from coordinator_core.git.eol_declared import (
                    find_declared_eol_drift,
                    repair_declared_eol_drift,
                )

                try:
                    drifts = find_declared_eol_drift(root, paths)
                    if drifts:
                        repair_declared_eol_drift(root, drifts)
                except Exception:  # noqa: BLE001 -- a repair-side failure is worse than the drift
                    pass

            with ledger.phase("apply_missing_trailers"):
                from coordinator_core.git.commit_trailers import apply_missing_trailers

                message = apply_missing_trailers(message, root, list(paths), session_id_override=sid)

            with ledger.phase("skill_weight_check"):
                from coordinator_core.ops.ceremony.commit_admission import skill_weight_check_worktree

                weight_refusal, _note = skill_weight_check_worktree(root, paths, ())
            if weight_refusal:
                return _out(error=weight_refusal)

            with ledger.phase("publish_allowlist_drift"):
                from coordinator_core.ops.ceremony.commit_admission import (
                    publish_allowlist_drift_refusal,
                )

                drift = publish_allowlist_drift_refusal(root, {p: ("", "") for p in paths})
            if drift:
                return _out(error=drift)

            with ledger.phase("split_process_paths"):
                from coordinator_core.ops.ceremony.commit_gates import split_process_paths

                product, process = split_process_paths(root, paths)
            if product and process:
                return _out(error="mixed product and process paths: the spike lands one class")

            from coordinator_core.git.commit import (
                CommitRefused,
                commit_paths,
                hash_worktree_blobs_via_spawn,
            )

            land = "land.product" if product else "land.process"
            try:
                with ledger.phase(land):
                    # Spawn reason (inside blob_fallback): hash an eol=crlf path whose CR bytes
                    # git's check-in filter changes; the 2-path reference carries none.
                    outcome = commit_paths(
                        root,
                        paths,
                        message,
                        blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=root),
                        detect_rollback=True,
                    )
            except CommitRefused as exc:
                return _out(error=str(exc))

            with ledger.phase("post_land_reply"):
                reply = {"committed": True, "sha": outcome.sha}
            return _out(sha=reply["sha"])
        except Exception as exc:  # noqa: BLE001 -- the contract is a returned error, never a raise
            return _out(error=f"{type(exc).__name__}: {exc}")


def _median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def measure_tick_ms(rounds: int = 3) -> float:
    """Smallest non-zero `time.process_time()` delta seen over `rounds` spins, in ms."""
    deltas: list[float] = []
    for _ in range(rounds):
        t0 = time.process_time()
        t1 = t0
        while t1 == t0:
            t1 = time.process_time()
        deltas.append((t1 - t0) * 1000.0)
    return min(deltas)


def phase_findings(warm: Sequence[dict], tick_ms: float) -> tuple[list[tuple[str, float, float]], list[tuple[str, float]]]:
    """(over, watch) per phase median against `max(budget, tick_ms)`.

    `over` rows are `(phase, median, excess)` more than one tick past the effective budget;
    `watch` rows are `(phase, median)` past it by no more than one tick.
    """
    over: list[tuple[str, float, float]] = []
    watch: list[tuple[str, float]] = []
    for name, budget in BUDGET_MS.items():
        vals = [s["phases_ms"][name] for s in warm if name in s["phases_ms"]]
        if not vals:
            continue
        med = _median(vals)
        excess = med - max(budget, tick_ms)
        if excess > tick_ms:
            over.append((name, med, excess))
        elif excess > 0:
            watch.append((name, med))
    return over, watch


def judge(samples: Sequence[dict], tick_ms: Optional[float] = None) -> str:
    """`SPIKE: AT-BAR` or `SPIKE: OVER-BAR -- <phase> <ms>` over the warm samples (2..N).

    Phase budgets are floored at one clock tick; a phase within one tick over is a PHASE-WATCH
    summary line, not a verdict change. `tick_ms` defaults to a run-time measurement.
    """
    warm = [s for s in samples[1:] if "sha" in s]
    if not warm:
        return "SPIKE: INSUFFICIENT -- no warm sample landed"
    if len(warm) < len(samples) - 1:
        return f"SPIKE: INSUFFICIENT -- {len(samples) - 1 - len(warm)} of {len(samples) - 1} warm samples refused"
    limit = threshold_ms()
    worst = ("process_ms", 0.0)
    for key in ("process_ms", "child_inclusive_ms"):
        vals = [s[key] for s in warm]
        for label, v in ((f"{key} median", _median(vals)), (f"{key} max", max(vals))):
            if v > limit and v > worst[1]:
                worst = (label, v)
    if worst[1]:
        return f"SPIKE: OVER-BAR -- {worst[0]} {worst[1]:.1f}"
    tick = measure_tick_ms() if tick_ms is None else tick_ms
    phase_sum = sum(
        _median([s["phases_ms"][n] for s in warm if n in s["phases_ms"]])
        for n in BUDGET_MS
        if any(n in s["phases_ms"] for s in warm)
    )
    if phase_sum > limit:
        return f"SPIKE: OVER-BAR -- phase median sum {phase_sum:.1f}"
    over, _watch = phase_findings(warm, tick)
    if over:
        name, med, _ = max(over, key=lambda r: r[2])
        return f"SPIKE: OVER-BAR -- {name} {med:.1f}"
    return "SPIKE: AT-BAR"


def _repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    raise SystemExit("no .git above the working directory")


def _summary(samples: Sequence[dict], tick_ms: float) -> Iterator[str]:
    warm = [s for s in samples[1:] if "sha" in s]
    yield f"TICK_MS: {tick_ms:.3f}"
    if samples:
        c = samples[0]
        yield (
            f"cold (sample 1): process_ms {c['process_ms']:.1f} "
            f"child_inclusive_ms {c['child_inclusive_ms']:.1f} spawns {c['spawns']}"
        )
    names = [n for n in BUDGET_MS if any(n in s["phases_ms"] for s in warm)]
    yield f"threshold (bar minus reserved {sum(RESERVED_MS.values()):.0f}ms): {threshold_ms():.1f}ms"
    for key in ("process_ms", "child_inclusive_ms"):
        vals = [s[key] for s in warm]
        if vals:
            yield f"{key}: median {_median(vals):.1f} min {min(vals):.1f} max {max(vals):.1f}"
    for n in names:
        vals = [s["phases_ms"][n] for s in warm if n in s["phases_ms"]]
        yield (
            f"phase {n}: median {_median(vals):.1f} min {min(vals):.1f} max {max(vals):.1f} "
            f"budget {BUDGET_MS[n]:.0f}"
        )
    gates = sorted({g for s in warm for g in s.get("gate_ms", {})})
    for gname in gates:
        vals = [s["gate_ms"][gname] for s in warm if gname in s.get("gate_ms", {})]
        yield f"gate {gname}: median {_median(vals):.1f} max {max(vals):.1f}"
    for name, med in phase_findings(warm, tick_ms)[1]:
        yield f"PHASE-WATCH -- {name} {med:.1f}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=7)
    ap.add_argument("--out-dir", required=True, help="repo-relative directory for the two record files")
    ap.add_argument("--repo-root", default=None)
    args = ap.parse_args(argv)

    root = Path(args.repo_root).resolve() if args.repo_root else _repo_root(Path.cwd())
    out_rel = Path(args.out_dir)
    if out_rel.is_absolute():
        raise SystemExit("--out-dir must be repo-relative")
    (root / out_rel).mkdir(parents=True, exist_ok=True)
    jsonl_rel = (out_rel / "2026-10-24-commit-v2-minimal-land.samples.jsonl").as_posix()
    md_rel = (out_rel / "2026-10-24-commit-v2-minimal-land.md").as_posix()
    jsonl, md = root / jsonl_rel, root / md_rel
    if not md.exists():
        md.write_text(
            "# commit_v2 minimal-land spike samples\n\n"
            "Each row carries the PREVIOUS sample's figures (a sample cannot record its own "
            "result into the commit that lands it).\n\n"
            "| sample | t_start | prev process_ms | prev child_inclusive_ms | prev spawns |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )

    results: list[dict] = []
    for i in range(1, args.samples + 1):
        prev = results[-1] if results else None
        row = {"sample": i, "t_start": time.time(), "prev": prev and {
            k: prev.get(k) for k in ("process_ms", "child_inclusive_ms", "spawns", "sha")
        }}
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        with open(md, "a", encoding="utf-8") as fh:
            p = row["prev"] or {}
            fmt = lambda k: f"{p[k]:.1f}" if isinstance(p.get(k), float) else p.get(k, "")  # noqa: E731
            fh.write(
                f"| {i} | {row['t_start']:.0f} | {fmt('process_ms')} | "
                f"{fmt('child_inclusive_ms')} | {p.get('spawns', '')} |\n"
            )
        res = minimal_land(
            root, [jsonl_rel, md_rel], f"spike: commit_v2 minimal land sample {i}\n",
            ledger=PhaseLedger(),
        )
        if "sha" not in res:
            print(f"sample {i}: refused: {res.get('error')}")
        results.append(res)

    tick_ms = measure_tick_ms()
    for line in _summary(results, tick_ms):
        print(line)
    committed = sum(1 for r in results if "sha" in r)
    print(f"committed: {committed}/{len(results)}")
    verdict = judge(results, tick_ms)
    print(verdict)
    return 0 if verdict == "SPIKE: AT-BAR" else 1


if __name__ == "__main__":
    sys.exit(main())
