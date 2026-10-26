"""Spike: what a 2-path peer-claim lookup costs per candidate mechanism, read-only on the live store.

Candidates: `today_split` (the full O(sinks) walk, the pre-rebuild lookup), P (per-path streams
verified against the claimant's cached sink fold) and J (a journal of sink signatures plus a
reverse index). Prototype state is written under `--work-dir` only; the sessions dir is read, never
written, and no commit lands. The one exception is the production `claim_index.lookup` that `main`
calls for recorded kinds, which builds its own index beside the sessions dir on a first call.
Clocks are `time.process_time()` deltas (excludes git children) and PhaseLedger ms (thread
CPU plus the dispatch child bucket). Sample 1 is cold; medians and max judge samples 2..N. Spawns
expected: none; `_SpawnCount` reports any that occur.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.ops.ceremony.commit_v2_phases import PhaseLedger
from coordinator_core.session import claim_index, touch_record
from coordinator_core.session.path_dialect import canonicalize_relative_path

# Per-candidate lookup + deduplicated liveness budget for a 2-path commit_v2 land (plan S2 target).
MEDIAN_BAR_MS = 25.0
MAX_BAR_MS = 110.0

GATE_NAMES = (
    "declared_deletion_gate", "carry_gate", "residue_gate", "op_scope_coverage_gate", "leak_gate",
    "registration_quad_gate", "claude_md_budget_gate", "machine_path_leak_gate", "attribution_gate",
)


class _SpawnCount:
    """Counts `subprocess.Popen` constructions while active."""

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


def _norm(path: str) -> str:
    return canonicalize_relative_path(path)


def _blocks(kind: Optional[str]) -> bool:
    return touch_record.kind_blocks_a_peer_commit(kind)


def _fold(sink: str) -> tuple[dict, dict]:
    claims, held, _degraded, _reasons = touch_record.read_stream_claims_and_held_kind(sink)
    return claims, held


def _uncache(sink: str) -> None:
    touch_record._FOLD_CACHE.pop(str(sink), None)


def _timed(ledger: PhaseLedger, name: str, fn: Callable[[], object]) -> tuple[object, float]:
    """`(value, process_ms)`; the PhaseLedger accumulates the thread+child figure under `name`."""
    p0 = time.process_time()
    with ledger.phase(name):
        value = fn()
    return value, (time.process_time() - p0) * 1000.0


def _shape(claimants: dict, held_by: dict) -> dict:
    return {
        "claimants": {p: sorted(s) for p, s in claimants.items()},
        "held_by": {s: sorted(k) for s, k in held_by.items()},
    }


def lookup_shape(result) -> dict:
    """A `claim_index.lookup` result reduced to the comparable `{claimants, held_by}` shape."""
    return _shape(dict(result), dict(result.held_by or {}))


# -- (a) today -----------------------------------------------------------------------------------

def today_split(base: str, paths: Sequence[str]) -> tuple[dict, dict]:
    """`claim_index.lookup`'s work split into enumerate, signature stats, fold copy and aggregate.

    Returns `(shape, parts_ms)`. The stats part is the stat share of the fold part, timed alone.
    """
    t = {}
    p0 = time.process_time()
    pairs, _complete = claim_index._enumerate_claim_sinks(base)
    t["enumerate"] = (time.process_time() - p0) * 1000.0
    p0 = time.process_time()
    for sink, _sid, _agent in pairs:
        touch_record._live_signature(sink)
    t["stats"] = (time.process_time() - p0) * 1000.0
    p0 = time.process_time()
    folds = [claim_index._read_stream_claims(sink) for sink, _sid, _agent in pairs]
    t["fold"] = (time.process_time() - p0) * 1000.0
    p0 = time.process_time()
    claims: dict[str, set] = {}
    kinds: dict[str, dict] = {}
    events = []
    for (_sink, sid, _agent), read in zip(pairs, folds):
        stream_claims, ok = read
        if not ok:
            continue
        stream_held = read.held_kind
        for path, ev in stream_claims.items():
            events.append((ev, path, sid, stream_held))
    # Latest ts wins per (path, sid) across all of a sid's streams, as claim_index.rebuild folds.
    # A TOUCH sorts after a RELEASE at equal ts (touch_record's cross-stream tie rule).
    events.sort(key=lambda row: (row[0].timestamp, row[0].verb == touch_record.VERB_TOUCH))
    for ev, path, sid, stream_held in events:
        if ev.verb == touch_record.VERB_TOUCH:
            claims.setdefault(path, set()).add(sid)
            if path in stream_held:
                per_sid = kinds.setdefault(path, {})
                per_sid[sid] = claim_index._merge_held_kind(
                    per_sid.get(sid, claim_index._NO_HELD), stream_held[path]
                )
        else:
            claims.get(path, set()).discard(sid)
            kinds.get(path, {}).pop(sid, None)
    result = {p: sorted(claims.get(_norm(p)) or ()) for p in paths}
    wanted = {sid for sids in result.values() for sid in sids}
    held: dict[str, list] = {}
    if wanted:
        for key, sids in claims.items():
            for sid in sids:
                if sid in wanted and kinds.get(key, {}).get(sid, claim_index._NO_HELD) != touch_record.KIND_READ:
                    held.setdefault(sid, []).append(key)
    t["aggregate"] = (time.process_time() - p0) * 1000.0
    t["total"] = sum(t[k] for k in ("enumerate", "fold", "aggregate"))
    return _shape(result, held), t


# -- (b) candidate P -----------------------------------------------------------------------------

def _p_file(work_dir: Path, path: str) -> Path:
    return work_dir / "p" / (hashlib.sha1(_norm(path).encode("utf-8")).hexdigest() + ".jsonl")


def p_append(work_dir: Path, path: str, verb: str, sid: str, agent: Optional[str],
             sink: str, ts: float, kind: Optional[str]) -> None:
    """One event onto the path's stream and the sid -> sinks map; what a claim writer would do."""
    stream = _p_file(work_dir, path)
    stream.parent.mkdir(parents=True, exist_ok=True)
    with open(stream, "a", encoding="utf-8") as fh:
        fh.write(json.dumps([verb, sid, agent, ts, kind, sink]) + "\n")
    by_sid = _p_by_sid(work_dir)
    sinks = by_sid.setdefault(sid, [])
    if sink not in sinks:
        sinks.append(sink)
        (work_dir / "by_sid.json").write_text(json.dumps(by_sid), encoding="utf-8")


def _p_by_sid(work_dir: Path) -> dict:
    try:
        return json.loads((work_dir / "by_sid.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def p_backfill(base: str, work_dir: Path) -> int:
    """Build the per-path streams from the live corpus; returns the stream count."""
    work_dir.mkdir(parents=True, exist_ok=True)
    pairs, _complete = claim_index._enumerate_claim_sinks(base)
    by_sid: dict[str, list] = {}
    lines: dict[Path, list] = {}
    for sink, sid, agent in pairs:
        claims, _held = _fold(sink)
        if claims:
            by_sid.setdefault(sid, []).append(sink)
        for path, ev in claims.items():
            lines.setdefault(_p_file(work_dir, path), []).append(
                json.dumps([ev.verb, sid, agent, ev.timestamp, ev.kind, sink])
            )
    (work_dir / "p").mkdir(parents=True, exist_ok=True)
    for stream, rows in lines.items():
        stream.write_text("\n".join(rows) + "\n", encoding="utf-8")
    (work_dir / "by_sid.json").write_text(json.dumps(by_sid), encoding="utf-8")
    return len(lines)


def p_lookup(work_dir: Path, paths: Sequence[str]) -> dict:
    """Read each path's stream, fold per sink, and verify each positive against the sink's fold.

    Per-sink only: it does not apply today's cross-stream ts fold, so it can read claimed where
    `today_split` reads released when one sid's sinks disagree on a path.
    """
    result: dict[str, set] = {}
    for path in paths:
        key = _norm(path)
        last: dict[str, list] = {}
        try:
            text = _p_file(work_dir, path).read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        for line in text.splitlines():
            row = json.loads(line)
            last[row[5]] = row
        sids: set = set()
        for sink, row in last.items():
            if row[0] != touch_record.VERB_TOUCH:
                continue
            claims, _held = _fold(sink)
            ev = claims.get(key)
            if ev is not None and ev.verb == touch_record.VERB_TOUCH:
                sids.add(row[1])
        result[path] = sids
    wanted = {sid for sids in result.values() for sid in sids}
    held: dict[str, list] = {}
    if wanted:
        by_sid = _p_by_sid(work_dir)
        for sid in wanted:
            merged: dict[str, object] = {}
            for sink in by_sid.get(sid, ()):
                claims, stream_held = _fold(sink)
                for key, ev in claims.items():
                    if ev.verb == touch_record.VERB_TOUCH:
                        merged[key] = claim_index._merge_held_kind(
                            merged.get(key, claim_index._NO_HELD), stream_held.get(key, ev.kind)
                        )
            for key, kind in merged.items():
                if kind != touch_record.KIND_READ:
                    held.setdefault(sid, []).append(key)
    return _shape(result, held)


# -- (c) candidate J -----------------------------------------------------------------------------

def _sig(sink: str) -> list:
    return list(touch_record._live_signature(sink) or (-1, -1))


def _j_entry(sink: str, sid: str, agent: Optional[str]) -> dict:
    sig = _sig(sink)
    claims, stream_held = _fold(sink)
    return {
        "sid": sid, "agent": agent, "sig": sig,
        "claims": {
            p: [stream_held.get(p, ev.kind)]
            for p, ev in claims.items() if ev.verb == touch_record.VERB_TOUCH
        },
    }


def _j_index(sinks: dict) -> dict:
    paths: dict[str, list] = {}
    for sink, entry in sinks.items():
        for path in entry["claims"]:
            paths.setdefault(path, []).append(sink)
    return paths


def _j_write(journal: Path, sinks: dict) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"sinks": sinks, "paths": _j_index(sinks)}), encoding="utf-8")


def j_build(base: str, journal: Path) -> int:
    """Build the journal from the live corpus; returns the sink count."""
    pairs, _complete = claim_index._enumerate_claim_sinks(base)
    sinks = {sink: _j_entry(sink, sid, agent) for sink, sid, agent in pairs}
    _j_write(journal, sinks)
    return len(sinks)


def j_lookup(base: str, journal: Path, paths: Sequence[str], force_stale: int = 0) -> tuple[dict, int]:
    """Validate sink signatures against the journal, re-fold the changed sinks, answer from it.

    `force_stale` marks that many journal sinks changed regardless of their signature, to cost a
    miss of known size. Returns `(shape, sinks_refolded)`.

    Per-sink only, like `p_lookup`: no cross-stream ts fold, so it can read claimed where
    `today_split` reads released when one sid's sinks disagree on a path.
    """
    data = json.loads(journal.read_text(encoding="utf-8"))
    sinks: dict = data["sinks"]
    pairs, _complete = claim_index._enumerate_claim_sinks(base)
    live = {sink: (sid, agent) for sink, sid, agent in pairs}
    forced = set(sorted(sinks)[:force_stale])
    changed = [s for s in live if s not in sinks or s in forced or sinks[s]["sig"] != _sig(s)]
    gone = [s for s in sinks if s not in live]
    for sink in gone:
        del sinks[sink]
    for sink in changed:
        _uncache(sink)
        sinks[sink] = _j_entry(sink, *live[sink])
    if changed or gone:
        _j_write(journal, sinks)
        paths_index = _j_index(sinks)
    else:
        paths_index = data["paths"]
    result: dict[str, set] = {}
    for path in paths:
        result[path] = {sinks[s]["sid"] for s in paths_index.get(_norm(path), ())}
    wanted = {sid for sids in result.values() for sid in sids}
    held: dict[str, list] = {}
    if wanted:
        merged: dict[str, dict] = {}
        for entry in sinks.values():
            if entry["sid"] in wanted:
                per_sid = merged.setdefault(entry["sid"], {})
                for key, (kind,) in entry["claims"].items():
                    per_sid[key] = claim_index._merge_held_kind(per_sid.get(key, claim_index._NO_HELD), kind)
        for sid, per_sid in merged.items():
            for key, kind in per_sid.items():
                if kind != touch_record.KIND_READ:
                    held.setdefault(sid, []).append(key)
    return _shape(result, held), len(changed)


# -- (d) liveness --------------------------------------------------------------------------------

def blocking_holders(shape: dict, kinds: dict, own_sid: Optional[str]) -> list:
    """`(path, sid)` for every non-self claimant whose recorded kind blocks a peer commit."""
    return [
        (path, sid)
        for path, sids in shape["claimants"].items()
        for sid in sids
        if sid != own_sid and _blocks(kinds.get((path, sid)))
    ]


def liveness_cost(holders: Sequence[tuple], cwd: str) -> tuple[float, float, int, int]:
    """`(distinct_ms, per_pair_ms, distinct_count, pair_count)` for session_live + session_abandoned."""
    from coordinator_core.session import liveness

    def probe(sid: str) -> None:
        try:
            if liveness.session_live(sid, cwd):
                liveness.session_abandoned(sid, cwd)
        except Exception:  # noqa: BLE001 -- a probe failure is a cost sample, not a refusal
            pass

    distinct = sorted({sid for _path, sid in holders})
    p0 = time.process_time()
    for sid in distinct:
        probe(sid)
    distinct_ms = (time.process_time() - p0) * 1000.0
    p0 = time.process_time()
    for _path, sid in holders:
        probe(sid)
    pair_ms = (time.process_time() - p0) * 1000.0
    return distinct_ms, pair_ms, len(distinct), len(holders)


# -- (e) gate attribution ------------------------------------------------------------------------

def gate_attribution(root: Path, paths: Sequence[str]) -> tuple[dict, int]:
    """Per-gate thread ms over `paths` with a post-commit reader, as `_pre_commit_gates` runs them.

    Read-only: no index write, no land. Returns `(gate_ms, spawns)`.
    """
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.ceremony import commit_v2 as cv2
    from coordinator_core.ops.ceremony.commit_source import post_commit_reader

    paths = list(paths)
    gate_ms: dict[str, float] = {}
    with _SpawnCount() as spawns:
        read_source = post_commit_reader(root, resolve_git_common_dir(root), paths=paths, deleted_paths=[])
        gates = {
            "declared_deletion_gate": lambda: cv2.declared_deletion_gate(root, paths, []),
            "carry_gate": lambda: cv2.carry_gate(root, paths),
            "residue_gate": lambda: cv2.residue_gate(root, paths),
            "op_scope_coverage_gate": lambda: cv2.op_scope_coverage_gate(root, paths, read_source=read_source),
            "leak_gate": lambda: cv2.leak_gate(root, paths),
            "registration_quad_gate": lambda: cv2.registration_quad_gate(root, paths, read_source=read_source),
            "claude_md_budget_gate": lambda: cv2.claude_md_budget_gate(root, paths, read_source=read_source),
            "machine_path_leak_gate": lambda: cv2.machine_path_leak_gate(root, paths, read_source=read_source),
            "attribution_gate": lambda: cv2.attribution_gate(root, paths, vacated=[]),
        }
        for name in GATE_NAMES:
            t0 = time.thread_time()
            gates[name]()
            gate_ms[name] = (time.thread_time() - t0) * 1000.0
    return gate_ms, spawns.n


# -- verdict -------------------------------------------------------------------------------------

def spike_line(figures: dict) -> str:
    """The one verdict line from `{candidate: [per-sample lookup+liveness ms over samples 2..N]}`.

    A candidate clears when its median is at or under `MEDIAN_BAR_MS` and its max at or under
    `MAX_BAR_MS`; of several that clear the lower median wins.
    """
    stats = {name: (statistics.median(v), max(v)) for name, v in figures.items() if v}
    if not stats:
        return "SPIKE: OVER-BAR -- none n/a"
    clearing = {n: s for n, s in stats.items() if s[0] <= MEDIAN_BAR_MS and s[1] <= MAX_BAR_MS}
    if clearing:
        return f"SPIKE: AT-BAR {min(clearing, key=lambda n: clearing[n][0])}"
    best = min(stats, key=lambda n: stats[n][0])
    return f"SPIKE: OVER-BAR -- {best} {stats[best][0]:.1f}ms"


def _warm(values: Sequence[float]) -> list:
    return list(values[1:]) or list(values)


def _table(title: str, columns: dict) -> list:
    out = [f"== {title} ==", f"{'measure':<28}{'cold':>9}{'median':>9}{'max':>9}"]
    for name, vals in columns.items():
        warm = _warm(vals)
        out.append(f"{name:<28}{vals[0]:>9.1f}{statistics.median(warm):>9.1f}{max(warm):>9.1f}")
    return out


def _repo_root(start: Path) -> Path:
    for cand in (start, *start.parents):
        if (cand / ".git").exists():
            return cand
    return start


def default_paths(base: str, root: Path) -> list:
    """Two existing repo paths the live index shows held, most-claimed first."""
    claims = claim_index.rebuild(sessions_dir=base).claims
    held = [p for p in claims if (root / p).is_file()]
    held.sort(key=lambda p: (-len(claims[p]), p))
    return held[:2]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=9)
    ap.add_argument("--work-dir", required=True, help="repo-relative directory under scratch/")
    ap.add_argument("--paths", nargs=2, default=None, metavar=("A", "B"))
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--skip-gates", action="store_true", help="skip measure (e)")
    args = ap.parse_args(argv)

    root = Path(args.repo_root).resolve() if args.repo_root else _repo_root(Path.cwd())
    rel = Path(args.work_dir)
    if rel.is_absolute() or rel.parts[:1] != ("scratch",) or ".." in rel.parts:
        raise SystemExit("--work-dir must be repo-relative and under scratch/")
    work_dir = root / rel
    from coordinator_core.session import core as session_core

    base = session_core.sessions_dir(str(root))
    if not base:
        raise SystemExit("no sessions dir resolvable")
    paths = list(args.paths) if args.paths else default_paths(base, root)
    if len(paths) != 2:
        raise SystemExit("fewer than two held paths found; pass --paths A B")
    try:
        own_sid = session_core.resolve_session_id(str(root)) or None
    except Exception:  # noqa: BLE001 -- self is only excluded from the liveness set
        own_sid = None

    p0 = time.process_time()
    p_streams = p_backfill(base, work_dir / "P")
    p_backfill_ms = (time.process_time() - p0) * 1000.0
    p0 = time.process_time()
    j_build(base, work_dir / "J" / "journal.json")
    j_build_ms = (time.process_time() - p0) * 1000.0
    journal = work_dir / "J" / "journal.json"

    series: dict[str, list] = {k: [] for k in (
        "today.enumerate", "today.stats", "today.fold", "today.aggregate", "today.total",
        "P.lookup", "J.hit", "J.miss_k1", "J.miss_k10", "live.distinct", "live.per_pair",
    )}
    gate_series: dict[str, list] = {g: [] for g in GATE_NAMES}
    ledger_series: dict[str, list] = {}
    parity = {"P": True, "J": True}
    live_counts = (0, 0)
    spawns_total = 0
    for i in range(args.samples):
        if i == 0:
            touch_record._FOLD_CACHE.clear()
        ledger = PhaseLedger()
        today, parts = today_split(base, paths)
        for k in ("enumerate", "stats", "fold", "aggregate", "total"):
            series[f"today.{k}"].append(parts[k])
        with _SpawnCount() as sc:
            p_shape, p_ms = _timed(ledger, "P.lookup", lambda: p_lookup(work_dir / "P", paths))
            j_shape, j_ms = _timed(ledger, "J.hit", lambda: j_lookup(base, journal, paths)[0])
            _, m1 = _timed(ledger, "J.miss_k1", lambda: j_lookup(base, journal, paths, 1))
            _, m10 = _timed(ledger, "J.miss_k10", lambda: j_lookup(base, journal, paths, 10))
        spawns_total += sc.n
        series["P.lookup"].append(p_ms)
        series["J.hit"].append(j_ms)
        series["J.miss_k1"].append(m1)
        series["J.miss_k10"].append(m10)
        parity["P"] &= p_shape == today
        parity["J"] &= j_shape == today
        recorded = claim_index.lookup(paths, sessions_dir=base).recorded_kind or {}
        kinds = {(p, sid): k for p, by_sid in recorded.items() for sid, k in by_sid.items()}
        holders = blocking_holders(today, kinds, own_sid)
        d_ms, pair_ms, nd, npair = liveness_cost(holders, str(root))
        live_counts = (nd, npair)
        series["live.distinct"].append(d_ms)
        series["live.per_pair"].append(pair_ms)
        for name, ms in ledger.phases_ms().items():
            ledger_series.setdefault(name, []).append(ms)
        if not args.skip_gates:
            gate_ms, gate_spawns = gate_attribution(root, paths)
            spawns_total += gate_spawns
            for g, ms in gate_ms.items():
                gate_series[g].append(ms)

    print(f"paths: {paths}  own_sid: {own_sid}  samples: {args.samples}  spawns: {spawns_total}")
    print(f"backfill: P {p_backfill_ms:.1f}ms ({p_streams} streams)  J {j_build_ms:.1f}ms")
    print(f"liveness holders: distinct {live_counts[0]} vs per-(path,sid) {live_counts[1]}")
    print(f"parity vs today_split: P {'ok' if parity['P'] else 'MISMATCH'}  J {'ok' if parity['J'] else 'MISMATCH'}")
    for title, keys in (
        ("(a) today's lookup, process_ms", [k for k in series if k.startswith("today.")]),
        ("(b,c) candidates, process_ms", ["P.lookup", "J.hit", "J.miss_k1", "J.miss_k10"]),
        ("(d) liveness, process_ms", ["live.distinct", "live.per_pair"]),
    ):
        print("\n".join(_table(title, {k: series[k] for k in keys})))
    print("\n".join(_table("PhaseLedger ms (thread + child)", ledger_series)))
    if not args.skip_gates:
        print("\n".join(_table("(e) gates, thread ms", gate_series)))

    figures = {}
    for name, key in (("P", "P.lookup"), ("J", "J.hit")):
        if parity[name]:
            figures[name] = [a + b for a, b in zip(_warm(series[key]), _warm(series["live.distinct"]))]
    line = spike_line(figures)
    print(line)
    return 0 if line.startswith("SPIKE: AT-BAR") else 1


if __name__ == "__main__":
    sys.exit(main())
