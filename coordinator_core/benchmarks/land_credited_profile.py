"""Measured plan for the minimum land: process time and spawns per `dispatch.land_run` requirement.

Replays a recorded run (`--real-record`) in a throwaway `--shared` clone at `--halted-scale K` in-window
halted runs and reports, per scale:

  base   `dispatch.land_run` replayed by `land_run_leg_budget_profile.run_real_record` (stops before the
         commit leg; median over `--base-repeat` clones)
  spike  an inline minimum land, one row per requirement of the plan (a)-(h), median/max process ms over
         `--repeat` reps in one clone. (a) credited rows' declared writes differing from HEAD and (h) the
         `commit_paths` CAS commit are the minimum land; (b)-(g) are measured one at a time, each as the
         marginal cost it would add. Rows tagged `existing` time the incumbent helper as a reference, never
         as a part of the new op. Imports run before the first timer (a warm engine).
  op     the registered new op (`--op`, default `dispatch.land_credited`) replayed through its `_handler`
         with a real commit in a fresh clone per rep, or `absent` while it is not in `OP_MODULE_MAP`.

Process time is this process plus descendants (`Profiler.tree_ms`); wall clock is never recorded. The scale
check fails loud (exit 3) unless the in-window halted runs the holds read sees are non-zero and exactly
`K` times the unscaled count: a scale-2 clone whose twins are not read as halted runs measures nothing.

    python coordinator_core/benchmarks/land_credited_profile.py --real-record ask-20261030T235004-d26504 --halted-scale 1
    python coordinator_core/benchmarks/land_credited_profile.py --real-record ask-20261030T235004-d26504 --halted-scale 2 --json
"""

from __future__ import annotations

import argparse
import importlib
import json
import statistics
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home  # noqa: E402
from coordinator_core.benchmarks import land_run_leg_budget_profile as base_profile  # noqa: E402
from coordinator_core.benchmarks.land_run_slow_disk_profile import SELF, _bind_session  # noqa: E402

BAR_MS = 500.0
FLAT_TOLERANCE = 0.10
BASELINE_REF = "154091e238"
DEFAULT_OP = "dispatch.land_credited"
EXIT_VACUOUS_SCALE = 3
_CLAIM_SOURCE = "coordinator_core/ops/dispatch_emit/stage_task_hijack.py:hijack_test_js"
_CLAIM_TARGET = "coordinator_core/ops/dispatch_emit/stage_task_hijack.py:classify"
REQUIREMENTS = {
    "a": "credited rows' declared writes differing from HEAD",
    "b": "run_artifacts candidate paths",
    "c": "superseded predecessors' record dirs",
    "d": "reviewed undeclared edits",
    "e": "peer-held / never-commit / sibling-repo / EM-withheld holds",
    "f": "halted-run holds",
    "g": "reach gate",
    "h": "commit via commit_paths (CAS ref move)",
}


class VacuousScale(RuntimeError):
    """The scaled clone's holds read saw no halted runs, or not `scale` times the unscaled count."""


@dataclass
class LegRow:
    req: str
    leg: str
    process_ms: list = field(default_factory=list)
    spawns: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def median_ms(self) -> float:
        return round(statistics.median(self.process_ms), 1) if self.process_ms else 0.0

    @property
    def max_ms(self) -> float:
        return round(max(self.process_ms), 1) if self.process_ms else 0.0


@dataclass
class SpikeContext:
    """What every leg reads: the clone, the replayed params and the run's declared plan/sizing."""

    clone: Path
    run_id: str
    params: dict
    plan_rel: str | None = None
    sizing_rel: str | None = None
    declared: list = field(default_factory=list)
    differing: list = field(default_factory=list)


def median(values: list) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def flat_within(scale1_ms: float, scale2_ms: float, tolerance: float = FLAT_TOLERANCE) -> bool:
    """True when the scale-2 figure is no more than `tolerance` above the scale-1 figure."""
    return scale2_ms <= scale1_ms * (1.0 + tolerance)


def assert_holds_scaled(before: int, after: int, scale: int) -> None:
    """Fail loud unless the holds read saw halted runs and exactly `scale` times as many after scaling."""
    if before <= 0 or after != before * scale:
        raise VacuousScale(f"halted runs the holds read sees: {before} unscaled, {after} at scale {scale} (want {before * scale}, non-zero)")


class _Clock:
    """Process-time-plus-descendants reads and a Popen counter; the same meter `profile_land` uses."""

    def __init__(self) -> None:
        self._prof = base_profile.Profiler(False)
        self.spawns = 0
        self._real_init = None

    def __enter__(self) -> "_Clock":
        self._prof.meter.start()
        self._real_init = subprocess.Popen.__init__
        clock, real = self, self._real_init

        def counted(inst, *a, **k):
            clock.spawns += 1
            return real(inst, *a, **k)

        subprocess.Popen.__init__ = counted
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._real_init
        self._prof.meter.stop_ms()

    def now(self) -> tuple:
        return self._prof.tree_ms(), self.spawns

    def run(self, fn):
        """`(result, process_ms, spawns)` of one call."""
        t0, s0 = self.now()
        result = fn()
        t1, s1 = self.now()
        return result, round(t1 - t0, 3), s1 - s0


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _marker_request(clone: Path, script_path: str):
    """The script's commit request: its inline marker, or one hop through its ask-run manifest's `marker_path`."""
    from coordinator_core.ops.dispatch_emit.commit_request import parse_manifest_marker, parse_marker

    text = (clone / script_path).read_text(encoding="utf-8")
    manifest_rel = parse_manifest_marker(text)
    if manifest_rel is not None:
        manifest = json.loads((clone / manifest_rel).read_text(encoding="utf-8"))
        text = (clone / manifest["marker_path"]).read_text(encoding="utf-8")
    return parse_marker(text)


def leg_a_credited_differing(c: SpikeContext) -> dict:
    """(a) The credited rows' declared writes differing from HEAD: the marker (inline, or one hop through the
    script's ask-run manifest to its `marker_path`) names the rows' paths; `scoped_changed` answers which
    differ, with no spawn. A gap in it falls back to every
    declared path present on disk, which `commit_paths` settles against HEAD itself (`no_delta`)."""
    from coordinator_core.git.scoped_changed import scoped_changed

    request = _marker_request(c.clone, c.params["script_path"])
    c.plan_rel = request.plan_path if request else None
    c.declared = sorted({_norm(p) for chunk in (request.chunks if request else ()) for p in chunk.paths})
    found = scoped_changed(c.clone, c.declared, [], [])
    if found is None:
        c.differing = [p for p in c.declared if (c.clone / p).exists()]
        return {"declared": len(c.declared), "differing": len(c.differing), "gap": True}
    changed, candidates = found
    c.differing = sorted(changed | {p for p in candidates if not p.endswith("/")})
    return {"declared": len(c.declared), "differing": len(c.differing), "gap": False}


def leg_b_run_artifacts(c: SpikeContext) -> dict:
    """(b) The run's own record dirs and manifest doc writes that differ from HEAD."""
    from coordinator_core.git.scoped_changed import scoped_changed

    docs = [_norm(p) for p in c.params.get("doc_paths") or ()]
    roots = [r for r in (f"state/warp/{c.run_id}", f"scratch/warp/{c.run_id}") if (c.clone / r).is_dir()]
    found = scoped_changed(c.clone, docs, roots, [])
    return {"changed": None if found is None else len(found[0]) + len(found[1]), "gap": found is None}


def leg_c_superseded_existing(c: SpikeContext) -> dict:
    """(c) Reference: the incumbent `land_run._superseded` over the clone's record dirs."""
    from coordinator_core.ops.dispatch_emit import land_run, run_token

    names, extra = land_run._superseded(c.clone, c.run_id, c.sizing_rel or "", None, run_token)
    return {"superseded": len(names), "record_files": len(extra)}


def leg_d_reviewed_undeclared(c: SpikeContext) -> dict:
    """(d) Reviewed edits no credited row declares: the review prep's file lists minus the declared writes."""
    from coordinator_core.git.scoped_changed import scoped_changed

    prep = (c.params.get("inline_review") or {}).get("prep") or {}
    reviewed = {_norm(p) for key in ("product_files", "slice_files") for p in prep.get(key) or ()}
    undeclared = sorted(reviewed - set(c.declared))
    found = scoped_changed(c.clone, undeclared, [], []) if undeclared else (set(), [])
    return {"reviewed": len(reviewed), "undeclared": len(undeclared), "gap": found is None}


def leg_e_holds_existing(c: SpikeContext) -> dict:
    """(e) Reference: the incumbent `land_run._peer_held` claim lookup plus the EM-withheld read."""
    from coordinator_core.ops.dispatch_emit import land_run, run_withhold

    held = land_run._peer_held(c.clone, c.declared, SELF, c.run_id, c.params["script_path"], plan_rel=c.plan_rel, run_base=c.params.get("run_base_sha"))
    em = run_withhold.read([c.clone / f"state/warp/{c.run_id}"])
    return {"peer_held": len(held), "em_withheld": len(em)}


def leg_f_halted_index(c: SpikeContext) -> dict:
    """(f) Halted-run holds from the halt-withheld index alone: one directory listing, one parse per run."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    by_path = halted_withheld_index.withheld_by_path(c.clone)
    wanted = set(c.declared)
    return {"index_paths": len(by_path), "withheld_declared": len(wanted & set(by_path))}


def leg_f_halted_existing(c: SpikeContext) -> dict:
    """(f) Reference: the incumbent `land_run.foreign_halted_withheld`."""
    from coordinator_core.ops.dispatch_emit import land_run

    out = land_run.foreign_halted_withheld(c.clone, c.run_id, c.plan_rel, c.declared)
    return {"withheld_declared": len(out)}


def leg_g_reach_existing(c: SpikeContext) -> dict:
    """(g) Reference: the incumbent reach gate over the run's delivery note."""
    from coordinator_core.ops.dispatch_emit.reach_claims import reach_gate

    note = c.clone / "docs" / "delivery" / f"{c.run_id}.md"
    try:
        text = note.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = None
    refusal, summary = reach_gate(c.clone, text, landing_paths=c.differing, deleted_paths=[], withheld_paths={})
    return {"note": text is not None, "checked": summary.get("checked", 0), "refused": refusal is not None}


def leg_g_reach_claim_existing(c: SpikeContext) -> dict:
    """(g) Reference: the incumbent reach gate when the note carries one claim (the tree scan loads only then)."""
    from coordinator_core.ops.dispatch_emit.reach_claims import reach_gate

    note = f"`{_CLAIM_SOURCE}` reaches `{_CLAIM_TARGET}`\n"
    refusal, summary = reach_gate(c.clone, note, landing_paths=c.differing, deleted_paths=[], withheld_paths={})
    return {"checked": summary.get("checked", 0), "unreachable": len(summary.get("unreachable", [])), "refused": refusal is not None}


def leg_h_commit(c: SpikeContext) -> dict:
    """(h) `commit_paths` over (a)'s paths: zero spawns, ref moved by CAS. A path gone from disk is a
    declared deletion."""
    from coordinator_core.git import commit as git_commit

    present = [p for p in c.differing if (c.clone / p).exists()]
    gone = [p for p in c.differing if p not in present]
    before = _head(c.clone)
    outcome = git_commit.commit_paths(c.clone, present, "spike: minimum land", deleted_paths=gone)
    return {"committed": len(present), "deleted": len(gone), "moved": outcome.sha != before and _head(c.clone) == outcome.sha, "sha": outcome.sha[:10]}


def _head(clone: Path) -> str:
    from coordinator_core.git.git_objects import read_ref_loose_or_packed

    head = (clone / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    return read_ref_loose_or_packed(clone / ".git", head[5:]) or ""


def dirty_credited_paths(c: SpikeContext, rep: int) -> list:
    """Untimed fixture step: append a line to every declared path present, so each rep commits the run's
    full declared shape (the recorded run's paths are already at HEAD in the clone)."""
    dirtied = []
    for rel in c.declared:
        target = c.clone / rel
        if target.is_file():
            with open(target, "a", encoding="utf-8", newline="") as fh:
                fh.write(f"\n# spike rep {rep}\n" if target.suffix in (".py", ".md") else f"\nspike rep {rep}\n")
            dirtied.append(rel)
    if not dirtied:
        raise FileNotFoundError("no credited path is present in the clone: the replay has nothing to commit")
    return dirtied


LEGS = (
    ("a", "credited_differing", leg_a_credited_differing),
    ("b", "run_artifacts", leg_b_run_artifacts),
    ("c", "superseded[existing]", leg_c_superseded_existing),
    ("d", "reviewed_undeclared", leg_d_reviewed_undeclared),
    ("e", "holds[existing]", leg_e_holds_existing),
    ("f", "halted_index", leg_f_halted_index),
    ("f", "halted[existing]", leg_f_halted_existing),
    ("g", "reach_gate[existing]", leg_g_reach_existing),
    ("g", "reach_claim[existing]", leg_g_reach_claim_existing),
    ("h", "commit_paths", leg_h_commit),
)


def warm_imports() -> None:
    """Import every module a leg touches before the first timer: a warm engine pays no import."""
    for name in ("coordinator_core.git.scoped_changed", "coordinator_core.git.commit", "coordinator_core.git.git_objects",
                 "coordinator_core.ops.dispatch_emit.commit_request", "coordinator_core.ops.dispatch_emit.land_run",
                 "coordinator_core.ops.dispatch_emit.run_token", "coordinator_core.ops.dispatch_emit.run_withhold",
                 "coordinator_core.ops.dispatch_emit.halted_withheld_index", "coordinator_core.ops.dispatch_emit.reach_claims",
                 "coordinator_core.ops.dispatch_emit.reach_claims_tree", "coordinator_core.session.claim_index"):
        importlib.import_module(name)


def halted_seen(clone: Path) -> int:
    """In-window halted runs the holds read sees: indexed (withheld paths) and `halted` inside the TTL."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    return len(set(halted_withheld_index.run_ids(clone)) & set(base_profile.in_window_halted_runs(clone)))


def seed_live_halted_markers(source_root: Path, clone: Path) -> int:
    """Copy each in-window `halted.json` the source worktree holds and the clone's HEAD lacks (halted records
    are not always committed), so the population the holds read sees does not depend on what HEAD tracks."""
    import shutil

    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    copied = 0
    for run in base_profile.in_window_halted_runs(source_root):
        src = source_root / RECORD_DIR_ROOT / run / halted_run.HALTED_FILE
        dst = clone / RECORD_DIR_ROOT / run / halted_run.HALTED_FILE
        if not dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied += 1
    return copied


@contextmanager
def replay_clone(run_id: str, source_root: Path, halted_scale: int):
    """Yield `(clone, params, halted_before, halted_after)`: the throwaway clone of `source_root` with the
    run's record seeded and its in-window halted runs multiplied `halted_scale`-fold."""
    from coordinator_core.session import core as session_core

    params = base_profile.real_record_params(source_root, run_id)
    with ExitStack() as stack:
        clone = base_profile.enter_record_fixture(stack, source_root, params, prefix="lcp", label="land_credited profile")
        base_profile.seed_real_record(source_root, clone, run_id)
        seed_live_halted_markers(source_root, clone)
        before = halted_seen(clone)
        base_profile.scale_halted_runs(clone, halted_scale)
        after = halted_seen(clone)
        base_profile.seed_parse_verdicts(clone)
        session_core.init(SELF, cwd=str(clone))
        yield clone, params, before, after


def run_spike(run_id: str, source_root: Path, halted_scale: int, repeat: int) -> dict:
    """The inline minimum land and each requirement's marginal cost, `repeat` reps in one clone."""
    declare_benchmark_origin()
    _bind_session()
    warm_imports()
    with replay_clone(run_id, source_root, halted_scale) as (clone, params, before, after):
        assert_holds_scaled(before, after, halted_scale)
        sizing = base_profile._json_of(source_root / "state" / "warp" / run_id / "halted.json").get("sizing_rel")
        ctx = SpikeContext(clone, run_id, params, sizing_rel=sizing)
        rows = {(req, name): LegRow(req, name) for req, name, _ in LEGS}
        with _Clock() as clock:
            leg_a_credited_differing(ctx)
            for rep in range(repeat):
                dirty_credited_paths(ctx, rep)
                for req, name, fn in LEGS:
                    detail, ms, spawns = clock.run(lambda fn=fn: fn(ctx))
                    row = rows[(req, name)]
                    row.process_ms.append(ms)
                    row.spawns = max(row.spawns, spawns)
                    row.detail = detail
    return {"rows": list(rows.values()), "halted_before": before, "halted_after": after, "halted_scale": halted_scale}


def run_base(run_id: str, source_root: Path, halted_scale: int, repeat: int) -> dict:
    """`dispatch.land_run` replayed to the commit leg boundary, `repeat` clones."""
    samples = []
    for _ in range(repeat):
        whole = base_profile.run_real_record(run_id, source_root, False, halted_scale)[0]
        samples.append(whole)
    return {"process_ms": [s.process_time_ms for s in samples], "spawns": max(s.spawns for s in samples),
            "halted_in_window": samples[0].attribution.get("halted_in_window"), "outcome": samples[0].attribution.get("outcome")}


def _plan_of(clone: Path, script_path: str) -> str:
    """Plan path of the script's commit marker: `land_run`'s replay params carry none, `dispatch.land_credited` requires one."""
    request = _marker_request(clone, script_path)
    return (request.plan_path if request else None) or "unknown"


def run_op(op: str, run_id: str, source_root: Path, halted_scale: int, repeat: int) -> dict:
    """The registered new op replayed through its module `_handler`, a real commit in a fresh clone per rep."""
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    if op not in OP_MODULE_MAP:
        return {"op": op, "absent": True}
    declare_benchmark_origin()
    _bind_session()
    module = importlib.import_module(OP_MODULE_MAP[op])
    samples, outcome, seen, landed = [], "", [], []
    for _ in range(repeat):
        with replay_clone(run_id, source_root, halted_scale) as (clone, params, before, after):
            assert_holds_scaled(before, after, halted_scale)
            seen.append(after)
            ctx = SpikeContext(clone, run_id, params)
            leg_a_credited_differing(ctx)
            dirty_credited_paths(ctx, 0)
            with _Clock() as clock:
                request = {"run_id": run_id, "script_path": params["script_path"], "plan_path": _plan_of(clone, params["script_path"])}
                result, ms, spawns = clock.run(lambda: module._handler(request, repo_root=clone / ".git"))
            samples.append((ms, spawns))
            outcome = str(result.get("refused") or result.get("sha") or result.get("error") or "ok") if isinstance(result, dict) else "?"
            landed.append(isinstance(result, dict) and bool(result.get("committed")))
    return {"op": op, "absent": False, "process_ms": [s[0] for s in samples], "spawns": max(s[1] for s in samples),
            "halted_in_window": seen[0], "outcome": outcome, "landed": all(landed)}


def render(report: dict) -> str:
    scale = report["halted_scale"]
    out = [f"halted-scale {scale}  baseline_ref {BASELINE_REF}  run {report['run_id']}", ""]
    base = report.get("base")
    if base:
        out.append(f"dispatch.land_run (base)   median {median(base['process_ms']):8.1f} ms  max {max(base['process_ms']):8.1f}  spawns {base['spawns']}  halted_in_window {base['halted_in_window']}  outcome {base['outcome']}")
    spike = report.get("spike")
    if spike:
        out.append(f"holds read saw {spike['halted_after']} in-window halted runs ({spike['halted_before']} unscaled)")
        out.append(f"{'req':3} {'leg':24} {'median_ms':>9} {'max_ms':>8} {'spawns':>6}  detail")
        for row in spike["rows"]:
            out.append(f"({row.req}) {row.leg:24} {row.median_ms:9.1f} {row.max_ms:8.1f} {row.spawns:6d}  {json.dumps(row.detail, sort_keys=True)}")
        minimum = sum(r.median_ms for r in spike["rows"] if r.leg in ("credited_differing", "commit_paths"))
        out.append(f"minimum land (a)+(h)       median sum {minimum:8.1f} ms")
    op = report.get("op")
    if op:
        if op["absent"]:
            out.append(f"{op['op']}   absent (not registered)")
        else:
            verdict = ("UNDER" if median(op["process_ms"]) < BAR_MS else "OVER") + f" {BAR_MS:.0f}ms" if op.get("landed") else "NOT A LANDING (a refused reply is not a measurement)"
            out.append(f"{op['op']}   median {median(op['process_ms']):8.1f} ms  max {max(op['process_ms']):8.1f}  spawns {op['spawns']}  halted_in_window {op['halted_in_window']}  outcome {op['outcome']}  {verdict}")
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--real-record", required=True, metavar="RUN_ID")
    ap.add_argument("--halted-scale", type=int, default=1, metavar="K")
    ap.add_argument("--source-repo", default=str(_REPO))
    ap.add_argument("--repeat", type=int, default=3, help="spike reps and new-op clones per scale")
    ap.add_argument("--base-repeat", type=int, default=1, help="land_run replay clones per scale (0 skips the base)")
    ap.add_argument("--op", default=DEFAULT_OP, help="registered op measured by name; absent until registered")
    ap.add_argument("--no-spike", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    source = Path(args.source_repo)
    report: dict = {"run_id": args.real_record, "halted_scale": args.halted_scale, "baseline_ref": BASELINE_REF}
    import tempfile

    with tempfile.TemporaryDirectory(prefix="lcp-home-") as home, fixture_settings_home(home):
        return _main(args, source, report)


def _main(args, source: Path, report: dict) -> int:
    try:
        if args.base_repeat:
            report["base"] = run_base(args.real_record, source, args.halted_scale, args.base_repeat)
        if not args.no_spike:
            report["spike"] = run_spike(args.real_record, source, args.halted_scale, args.repeat)
        report["op"] = run_op(args.op, args.real_record, source, args.halted_scale, args.repeat)
    except VacuousScale as exc:
        print(f"VACUOUS-SCALE: {exc}", file=sys.stderr)
        return EXIT_VACUOUS_SCALE
    if args.json:
        spike = report.get("spike")
        if spike:
            report["spike"] = {**spike, "rows": [{**asdict(r), "median_ms": r.median_ms} for r in spike["rows"]]}
        print(json.dumps(report, indent=1))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
