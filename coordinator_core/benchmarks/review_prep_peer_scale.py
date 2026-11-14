"""review.prep at peer scale: N peer commits, N halted peer runs, M dirty declared paths, deep history.

Extends `review_prep_dirty_worktree` (imports its `seed`, stage recorder and git-verb attribution) with
the three shapes that overran the 30s warm budget on a shared worktree: one peer commit per peer in
`base..HEAD` (the peer-base walk), one halted record per peer withholding declared paths with kept blobs
absent from HEAD (the halted-hold history walk), and at least `MIN_HISTORY` commits since the run base.
`review_prep._handler` runs in `run_id` mode in a child process. Reported per scenario:

- `process_ms` / `procs`: the child's process tree (own plus child CPU) minus an import-only child.
- `spawn_count` and `git_verbs`: `subprocess.Popen` creations inside the handler, total and by git verb.
- `legs`: per `review_prep_legs` leg, inclusive `wall_ms`, `cpu_ms` (own thread) and `spawns`.

    python coordinator_core/benchmarks/review_prep_peer_scale.py [--peers 20] [--dirty 150]
        [--small-peers 5 --small-dirty 30] [--runs 5] [--enforce]

`--enforce` exits 1 when any sample's `process_ms` >= `PROCESS_BAR_MS`, the median `process_ms` (reported
as `median_ms`) >= `REVIEW_PREP_BAR_MS`, or the spawn count differs between the small and the large scenario.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

PROCESS_BAR_MS = 500.0  # kill bar: the slowest sample must stay under it
REVIEW_PREP_BAR_MS = 200.0  # CLAUDE.md's per-process fix bar: the median sample must stay under it
RUNS = 5
SETUP_SAMPLES = 3  # the import-only baseline is a median: one 15ms-quantised sample would land its jitter in every delta
MIN_HISTORY = 120
PEER_PATHS = 2
WITHHELD_PER_PEER = 4
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _stream_blob(data: bytes) -> bytes:
    return b"blob\ndata %d\n" % len(data) + data + b"\n"


def _stream_commit(ref: str, ident: str, ts: int, message: str, files: dict, parent: str = "") -> bytes:
    msg = message.encode("utf-8")
    out = [f"commit {ref}\nauthor {ident} {ts} +0000\ncommitter {ident} {ts} +0000\n".encode(),
           b"data %d\n" % len(msg), msg, b"\n"]
    if parent:
        out.append(f"from {parent}\n".encode())
    for rel, data in files.items():
        out.append(f"M 644 inline {rel}\ndata {len(data)}\n".encode() + data + b"\n")
    return b"".join(out)


def _fast_import(repo: Path, stream: bytes) -> None:
    from coordinator_core.git.run import run_git

    got = run_git(["-C", str(repo), "fast-import", "--quiet"], input=stream, binary=True, timeout=60.0)
    if not got.ok:
        raise RuntimeError(f"fast-import failed rc={got.returncode}: {got.stderr}")


def seed_peer_scale(repo: Path, peers: int, dirty: int) -> dict:
    """Seed `repo` in `run_id` mode and return the handler params.

    On top of `review_prep_dirty_worktree.seed`: `MIN_HISTORY` filler commits plus `peers` peer commits
    (distinct Session-Id and Deliverable-Id trailers) after the run base, each peer touching `PEER_PATHS`
    tracked declared paths, and one halted run record per peer withholding `WITHHELD_PER_PEER` declared
    paths with a kept blob that differs from HEAD, from the base and from every committed version. Each halt
    also writes its `halted_withheld_index` entry, as a real halt does, so the one-time `.built-v2`
    rebuild migration is not billed to every review."""
    from coordinator_core.benchmarks import review_prep_dirty_worktree as dw
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as index
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    params = dw.seed(repo, dirty, "runid")
    base = params["base"]
    declared = json.loads((repo / "state" / "warp" / dw.RUN_ID / "manifest.json").read_text(encoding="utf-8"))[
        "review_declared_paths"]
    tracked = [p for p in declared if "/m" in p]
    ident, ts0 = "t <t@example.com>", int(time.time()) - 86400
    ref = "refs/heads/main"
    stream, parent = [], base
    total = MIN_HISTORY + peers
    stride = max(1, total // max(peers, 1))
    peer_i = 0
    for n in range(total):
        is_peer = peer_i < peers and n % stride == stride - 1
        if is_peer:
            files = {tracked[(PEER_PATHS * peer_i + k) % len(tracked)]: f"v = 2 peer {peer_i}\n".encode()
                     for k in range(PEER_PATHS)}
            message = f"peer commit {peer_i}\n\nSession-Id: peer-sess-{peer_i}\nDeliverable-Id: peer-deliv-{peer_i}\n"
            peer_i += 1
        else:
            files = {f"hist/h{n}.txt": f"history {n}\n".encode()}
            message = f"history {n}\n"
        stream.append(_stream_commit(ref, ident, ts0 + n, message, files, parent))
        parent = ""
    while peer_i < peers:  # stride rounding left peers unplaced
        files = {tracked[(PEER_PATHS * peer_i + k) % len(tracked)]: f"v = 2 peer {peer_i}\n".encode()
                 for k in range(PEER_PATHS)}
        message = f"peer commit {peer_i}\n\nSession-Id: peer-sess-{peer_i}\nDeliverable-Id: peer-deliv-{peer_i}\n"
        stream.append(_stream_commit(ref, ident, ts0 + total + peer_i, message, files))
        peer_i += 1
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() - 600))
    for i in range(peers):
        withheld = [declared[(WITHHELD_PER_PEER * i + k) % len(declared)] for k in range(WITHHELD_PER_PEER)]
        kept = {p: f"v = 9 kept {i} {p}\n".encode() for p in withheld}
        for data in kept.values():
            stream.append(_stream_blob(data))
        rdir = repo / RECORD_DIR_ROOT / f"warp-peer-{i:02d}-{stamp}-{i:06x}"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "halted.json").write_text(json.dumps({
            "status": "halted", "run_base_sha": base,
            "run_artifacts": {"withheld": {p: "peer hunks" for p in withheld}},
        }), encoding="utf-8")
        (rdir / "kept.json").write_text(json.dumps({
            "chunks": {"C0": {"paths": {p: _blob_sha(d) for p, d in kept.items()}}},
        }), encoding="utf-8")
        (rdir / "manifest.json").write_text(json.dumps({"review_declared_paths": withheld}), encoding="utf-8")
        index.add_run(repo, rdir.name, withheld, sizing_rel="", declared=withheld)
    (repo / index.INDEX_DIR / index.SENTINEL).write_bytes(b"")
    _fast_import(repo, b"".join(stream))
    dw._git(repo, "reset", "-q")
    return params


def _install_leg_recorder(rec) -> dict:
    """Wrap `review_prep_legs.leg` so each leg records inclusive wall, thread CPU and spawns."""
    from coordinator_core.ops import review_prep_legs

    legs: dict = {}
    real = review_prep_legs.leg

    class _Timed:
        def __init__(self, name: str) -> None:
            self.name = name
            self.inner = real(name)

        def __enter__(self):
            self.w, self.c = time.perf_counter(), time.thread_time()
            self.s = sum(b["spawns"] for b in rec.stages.values())
            return self.inner.__enter__()

        def __exit__(self, *exc):
            got = self.inner.__exit__(*exc)
            b = legs.setdefault(self.name, {"calls": 0, "wall_ms": 0.0, "cpu_ms": 0.0, "spawns": 0})
            b["calls"] += 1
            b["wall_ms"] += (time.perf_counter() - self.w) * 1000
            b["cpu_ms"] += (time.thread_time() - self.c) * 1000
            b["spawns"] += sum(x["spawns"] for x in rec.stages.values()) - self.s
            return got

    review_prep_legs.leg = _Timed
    return legs


#: Modules `review_prep._handler` imports lazily on its first call. A warm engine has them loaded, so the
#: import-only child loads them too: the measured delta is the op's work, not first-import cost.
_WARM_MODULES = (
    "coordinator_core.ops.dispatch_emit.halted_hold_release",
    "coordinator_core.ops.dispatch_emit.sibling_claims",
    "coordinator_core.ops.dispatch_emit.peer_hunk_gate",
    "coordinator_core.ops.dispatch_emit.halted_criterion_met",
    "coordinator_core.ops.dispatch_emit.halted_phantom_release",
    "coordinator_core.ops.dispatch_emit.halted_hunk_attribution",
    "coordinator_core.ops.dispatch_emit.run_hunk_claims",
    "coordinator_core.ops.dispatch_emit.run_withhold",
    "coordinator_core.ops.dispatch_emit.stage_dirt_snapshot",
    "coordinator_core.ops.dispatch_emit.stopped_runs",
    "coordinator_core.ops.dispatch_emit.own_checkpoint_paths",
    "coordinator_core.ops.dispatch_emit.co_declared_settle",
    "coordinator_core.ops.warp_run_deliverable",
    "coordinator_core.ops.review_peer_bases",
    "coordinator_core.ops.review_slice_id",
    "coordinator_core.ops.review_mint.op",
    "coordinator_core.shared_read",
    "coordinator_core.git.ignore_match",
    "coordinator_core.ops.ceremony.commit_companion",
    "coordinator_core.ops.dispatch_emit.emission_receipt",
    "coordinator_core.ops.dispatch_emit.run_scratch",
    "coordinator_core.ops.review_mint.roster",
    "coordinator_core.git.ref_cas_retry",
)


def child_main(repo: Path, params: dict, setup_only: bool) -> int:
    import importlib

    from coordinator_core.benchmarks import review_prep_dirty_worktree as dw

    rec = dw._Recorder()
    review_prep = dw._instrument(rec)
    for name in _WARM_MODULES:
        importlib.import_module(name)
    legs = _install_leg_recorder(rec)
    if setup_only:
        return 0
    rec.last_wall, rec.last_cpu = time.perf_counter(), time.process_time()
    reply = review_prep._handler(params, repo)
    rec._tick()
    verbs: Counter = Counter()
    for b in rec.stages.values():
        verbs.update(b["git"])
    print(json.dumps({
        "error": reply.get("error"),
        "verdict": reply.get("verdict"),
        "spawn_count": sum(b["spawns"] for b in rec.stages.values()),
        "git_verbs": dict(verbs),
        "legs": {k: {m: (round(v, 1) if isinstance(v, float) else v) for m, v in b.items()} for k, b in legs.items()},
    }))
    return 0


def _run_child(repo: Path, params_path: Path, out_dir: Path, tag: str, setup_only: bool) -> dict:
    from coordinator_core.benchmarks import process_time

    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--repo", str(repo), "--params", str(params_path)]
    if setup_only:
        cmd.append("--setup-only")
    out, err = out_dir / f"{tag}.out", out_dir / f"{tag}.err"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    measured = process_time.single_invocation_tree_process_time(
        cmd, env=env, cwd=str(_REPO_ROOT), stdout_path=str(out), stderr_path=str(err))
    if measured["rc"] != 0:
        raise RuntimeError(f"{tag}: child rc={measured['rc']}: {err.read_text(errors='replace')[-2000:]}")
    measured["payload"] = None if setup_only else json.loads(out.read_text(encoding="utf-8").strip().splitlines()[-1])
    return measured


def measure(peers: int, dirty: int, tree: bool) -> dict:
    """One fresh-repo scenario. With `tree`, also `process_ms` and `procs` net of an import-only child."""
    from coordinator_core.benchmarks.isolated_clone import record_fixture_repo

    with record_fixture_repo(_REPO_ROOT, [], prefix="rp-peer", label="review_prep_peer_scale") as repo:
        params = seed_peer_scale(repo, peers, dirty)
        params_path = repo.parent / "params.json"
        params_path.write_text(json.dumps(params), encoding="utf-8")
        setups = [_run_child(repo, params_path, repo.parent, f"setup{i}", True) for i in range(SETUP_SAMPLES)] if tree else []
        full = _run_child(repo, params_path, repo.parent, "full", False)
    payload = full["payload"]
    if payload["error"]:
        raise RuntimeError(f"review.prep (peers={peers}, dirty={dirty}) errored: {payload['error']}")
    result = {"peers": peers, "dirty_declared": dirty, "spawn_count": payload["spawn_count"],
              "git_verbs": payload["git_verbs"], "verdict": payload["verdict"], "legs": payload["legs"]}
    if tree:
        setup = sorted(setups, key=lambda m: m["process_time_ms"])[len(setups) // 2]
        result["process_ms"] = round(full["process_time_ms"] - setup["process_time_ms"], 1)
        result["procs"] = full["procs"] - setup["procs"]
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--peers", type=int, default=20)
    ap.add_argument("--dirty", type=int, default=150)
    ap.add_argument("--small-peers", type=int, default=5)
    ap.add_argument("--small-dirty", type=int, default=30)
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--enforce", action="store_true")
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--params")
    args = ap.parse_args(argv)
    from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home

    if args.child:
        with fixture_settings_home(Path(args.repo).parent):
            return child_main(Path(args.repo), json.loads(Path(args.params).read_text(encoding="utf-8")), args.setup_only)

    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir(prefix="review-prep-peer-") as parent, fixture_settings_home(parent):
        declare_benchmark_origin()
        small = measure(args.small_peers, args.small_dirty, tree=False)
        runs = sorted((measure(args.peers, args.dirty, tree=True) for _ in range(max(1, args.runs))),
                      key=lambda r: r["process_ms"])
        large = runs[len(runs) // 2]
    large["spawn_count_small"] = small["spawn_count"]
    large["spawn_constant"] = all(small["spawn_count"] == r["spawn_count"] for r in runs)
    large["within_bar"] = all(r["process_ms"] < PROCESS_BAR_MS for r in runs)
    large["median_ms"] = large["process_ms"]
    large["within_review_bar"] = large["process_ms"] < REVIEW_PREP_BAR_MS
    print(f"[peers={args.peers} dirty={args.dirty}] process_ms={large['process_ms']} "
          f"within_review_bar={large['within_review_bar']} procs={large['procs']} "
          f"spawn_count={large['spawn_count']} (peers={args.small_peers} dirty={args.small_dirty}: "
          f"{small['spawn_count']}) git_verbs={large['git_verbs']}")
    for name, b in sorted(large["legs"].items(), key=lambda kv: -kv[1]["wall_ms"]):
        print(f"  {name:24s} calls={b['calls']:<3d} spawns={b['spawns']:<4d} wall_ms={b['wall_ms']:<9} cpu_ms={b['cpu_ms']}")
    print(json.dumps(large))
    ok = large["spawn_constant"] and large["within_bar"] and large["within_review_bar"]
    return 1 if (args.enforce and not ok) else 0


if __name__ == "__main__":
    sys.path[:] = [str(_REPO_ROOT)] + [p for p in sys.path if Path(p or ".").resolve() != Path(__file__).resolve().parent]
    sys.exit(main())
