"""Resume-emit benchmark: the engine-ask submit hook's halted-run resume, through its record path.

Builds a tmp git repo (a sizing with a `plan:` edge, a plan whose spine has landed and open rows,
a halted `review_blocked` run with its halt-time `resume.json`, and `--commits` first-parent commits
after the plan), drives `submit_engine_ask.evaluate` (launch stubbed) and reports process ms (cold and
warm), git spawns, `commit_walk.commit_meta` calls, the decision, and a cProfile top-N.
`measure()` is the single entry the cost pin (`test_submit_hook_resume_emit_cost_pin.py`) drives.
The fixture is a small synthetic history: an UNDER-bar result here does not show the production
12.0s symptom is gone; only a run against a production-sized history does.

Run: `python -m coordinator_core.benchmarks.submit_hook_resume_emit [--commits N] [--top T]
[--profile-out PATH] [--enforce-bar] [--repo ROOT [--sizing REL] [--cold] [--stall-ms N]]`. `--repo` replays
`submit_engine_ask.evaluate` on a halted sizing in a real tree (the proof the fixture cannot give). Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git.run import run_git

#: The brightline process-time bar for the hook emit (CLAUDE.md § The brightline).
BAR_MS = 500.0
SIZING_REL = "state/sizings/2026-10-29-resume-bench.yaml"
PLAN_REL = "docs/plans/2026-10-29-resume-bench.md"
RUN_ID = "warp-2026-10-29-resume-bench-20261029T000000-aaaaaa"
#: Spine rows: A1 lands by a checkpoint commit; the rest stay open and are what the resume stages.
OPEN_ROWS = ("A2", "A3", "A4")
DEFAULT_COMMITS = 48
_SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _fast_import_stream(commits: int) -> bytes:
    """A `git fast-import` stream: seed, the plan, the A1 checkpoint, then churn commits that never
    touch an open row's declared write, so the outside-landing walk runs down to the plan's creation."""
    out: list[bytes] = []
    epoch = 1_700_000_000
    mark = 0

    def commit(message: str, files: dict[str, str]) -> None:
        nonlocal epoch, mark
        mark += 1
        epoch += 60
        msg = message.encode("utf-8")
        out.append(f"commit refs/heads/main\nmark :{mark}\ncommitter t <t@t.example> {epoch} +0000\n".encode())
        out.append(f"data {len(msg)}\n".encode() + msg + b"\n")
        if mark > 1:
            out.append(f"from :{mark - 1}\n".encode())
        for path, text in files.items():
            body = text.encode("utf-8")
            out.append(f"M 100644 inline {path}\ndata {len(body)}\n".encode() + body + b"\n")

    commit("seed", {"README.md": "seed\n"})
    commit("plan", {PLAN_REL: _plan_text()})
    commit(
        f"checkpoint(wave 1): 1 row - A1\n\nCheckpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
        {"src/a1.py": "a1\n"},
    )
    for n in range(commits):
        commit(
            f"churn {n}",
            {f"src/churn/m{n % 7}.py": f"{n}\n", f"docs/notes/n{n % 5}.md": f"{n}\n", f"pkg/p{n}.py": f"{n}\n"},
        )
    return b"".join(out)


def _plan_text() -> str:
    rows = [("A1", ["src/a1.py"])] + [(i, [f"src/{i.lower()}.py"]) for i in OPEN_ROWS]
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n" + "".join(f"    - {w}\n" for w in writes)
        for i, writes in rows
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _git(root: Path, *args: str, stdin: bytes | None = None) -> None:
    res = run_git(
        ["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args], input=stdin,
    )
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={res.returncode}): {res.stderr.strip()}")


def build_fixture(root: Path, commits: int = DEFAULT_COMMITS) -> None:
    """Populate `root` (an empty dir): a packed git history, the sizing, and a halted run record."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    _git(root, "init", "-q", "-b", "main")
    _git(root, "fast-import", "--quiet", stdin=_fast_import_stream(commits))
    _git(root, "reset", "-q", "--hard", "main")
    _git(root, "repack", "-adq")
    sizing = {
        "schema": "sizing-object", "name": "resume bench", "intent": "exercise the resume emit",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized", "plan": PLAN_REL,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-resume-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    (root / SIZING_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8", newline="\n")
    rl.start(root, RUN_ID)
    rl.bind(root, RUN_ID, SIZING_REL, ask_text="the ask")
    _halt_with_record(root)
    _git(root, "add", "-Af")
    _git(root, "commit", "-q", "-m", "halted run record (resume bench)")


def _halt_with_record(repo: Path) -> None:
    """Write the fixture's `halted` marker (base = HEAD) and build its `resume.json` in-process, as the
    detached halt-time build would; the detached spawn is silenced so it cannot race this build."""
    from coordinator_core.ops.dispatch_emit import halted_run, halt_resume_build
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    head = run_git(["-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    real = halted_run._RESUME_SPAWNER
    halted_run._RESUME_SPAWNER = lambda *a: None
    try:
        halted_run.write_halt_marker(
            record_dir(repo, RUN_ID), halted="review_blocked", sizing_rel=SIZING_REL, plan_rel=PLAN_REL,
            run_base_sha=head,
        )
    finally:
        halted_run._RESUME_SPAWNER = real
    halt_resume_build.build(repo, RUN_ID)


def seed_resume_fixture(repo: Path) -> str:
    """Seed `repo` (a real git tree, a scratch clone) with a halted `review_blocked` run on a plan
    whose A1 landed by a checkpoint commit and A2..A4 are open; commits the plan, sizing and checkpoint.
    Returns the sizing's repo-relative path. Idempotent; never touch a tree you care about."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    repo = Path(repo).resolve()
    run_dir = f"state/warp/{RUN_ID}"
    if _tracked(repo, f"{run_dir}/halted.json"):
        if not _tracked(repo, f"{run_dir}/resume-script.workflow.mjs"):
            from coordinator_core.ops.dispatch_emit import halt_resume_build

            halt_resume_build.build(repo, RUN_ID)
            _git(repo, "add", "-f", "--", run_dir, SIZING_REL)
            _git(repo, "commit", "-q", "-m", "halt-resume record (resume bench)")
        return SIZING_REL
    if _tracked(repo, SIZING_REL):
        _git(repo, "checkout", "-q", "HEAD", "--", SIZING_REL)
        _git(repo, "clean", "-fdxq", "--", run_dir, f"scratch/warp/{RUN_ID}*")
    else:
        _seed_plan_and_sizing(repo)
    rl.start(repo, RUN_ID)
    rl.bind(repo, RUN_ID, SIZING_REL, ask_text="the ask")
    _halt_with_record(repo)
    _git(repo, "add", "-f", "--", run_dir, SIZING_REL)
    _git(repo, "commit", "-q", "-m", "halted run record (resume bench)")
    return SIZING_REL


def _tracked(repo: Path, rel: str) -> bool:
    return run_git(["-C", str(repo), "ls-files", "--error-unmatch", "--", rel]).ok


def _seed_plan_and_sizing(repo: Path) -> None:
    sizing = {
        "schema": "sizing-object", "name": "resume bench", "intent": "exercise the resume emit",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized", "plan": PLAN_REL,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-resume-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    for rel, text in ((PLAN_REL, _plan_text()), (SIZING_REL, yaml.safe_dump(sizing, sort_keys=False))):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "--", PLAN_REL, SIZING_REL)
    _git(repo, "commit", "-q", "-m", "plan (resume bench)")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "a1.py").write_text("a1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "--", "src/a1.py")
    _git(repo, "commit", "-q", "-m", f"checkpoint(wave 1): 1 row - A1\n\nCheckpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234")


def reset_resume_fixture(repo: Path) -> None:
    """Return the seeded fixture to its committed halted state: each emitting call consumes the halt
    (stamps the sizing, resumes the marker, writes scripts), so every measured call needs a fresh one."""
    run_dir = f"state/warp/{RUN_ID}"
    _git(repo, "checkout", "-q", "HEAD", "--", SIZING_REL, PLAN_REL, run_dir)
    _git(repo, "clean", "-fdxq", "--", run_dir, f"scratch/warp/{RUN_ID}*")


def _top(stats: pstats.Stats, n: int) -> str:
    stats.sort_stats("cumulative").print_stats(n)
    return stats.stream.getvalue()


def measure(commits: int = DEFAULT_COMMITS, top: int = 20) -> dict:
    """Build the fixture (a halted run WITH its halt-time `resume.json`) and drive the submit hook's
    record path through `replay`: cold, warm (counted, median of five fresh halts), profiled.

    Returns `replay`'s report (`warm_process_ms`, `cold_process_ms`, `hook_git_spawns`, `resumes_halted_run`)
    plus `commit_meta_calls` over every call (a resume that walks commits is not reading the record)."""
    from coordinator_core.git import commit_walk

    mp = pytest.MonkeyPatch()
    scratch = Path(tempfile.mkdtemp(prefix="resume-emit-bench-", dir=_scratch_parent()))
    try:
        root = scratch / "repo"
        root.mkdir()
        build_fixture(root, commits)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.delenv("DELIVERABLE_ID", raising=False)
        meta_calls = [0]
        real_meta = commit_walk.commit_meta

        def counted(*a, **k):
            meta_calls[0] += 1
            return real_meta(*a, **k)

        mp.setattr(commit_walk, "commit_meta", counted)
        result = replay(root, SIZING_REL, top, reset=lambda: reset_resume_fixture(root))
        mp.setattr(commit_walk, "commit_meta", real_meta)
        return {"commits": commits, "commit_meta_calls": meta_calls[0], **result}
    finally:
        mp.undo()
        rmtree_or_raise(scratch, label="resume-emit-bench")


def halted_sizings(repo: Path, limit: int = 60) -> list[str]:
    """Distinct `sizing_rel`s of halted run records under `repo`'s `state/warp`, newest marker first."""
    import json
    import os

    base = repo / "state" / "warp"
    stamped: list[tuple[float, str]] = []
    try:
        entries = [e.path for e in os.scandir(base) if e.is_dir()]
    except OSError:
        return []
    for path in entries:
        marker = os.path.join(path, "halted.json")
        try:
            stamped.append((os.stat(marker).st_mtime, marker))
        except OSError:
            continue
    found: list[str] = []
    for _, marker in sorted(stamped, reverse=True):
        try:
            data = json.loads(Path(marker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = data.get("sizing_rel") if isinstance(data, dict) and data.get("status") == "halted" else None
        if isinstance(rel, str) and rel not in found:
            found.append(rel)
            if len(found) >= limit:
                break
    return found


def _emitted(out: dict) -> bool:
    """True when the hook allowed the submission and wrote a workflow script (the resume taking the run)."""
    hso = out.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "allow" and str(
        hso.get("permissionDecisionReason") or ""
    ).startswith("Engine submission: emitted")


def _halt_status(repo: Path) -> str | None:
    """The seeded run's `halted.json` status (`resumed` once an emit took the halted run)."""
    import json

    try:
        return json.loads((repo / "state" / "warp" / RUN_ID / "halted.json").read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return None


def _is_git(argv: tuple) -> bool:
    return bool(argv) and Path(argv[0]).stem.lower() == "git"


@contextlib.contextmanager
def _stalled_run_dir_reads(mp: pytest.MonkeyPatch, repo: Path, stall_ms: float):
    """Make every read under `repo/state/warp` sleep `stall_ms`: the `bounded_read` seam of
    `halt_resume_record` when it exists, else `pathlib.Path.read_text`. Yields the stalled-read count."""
    run_dir = (repo / "state" / "warp").resolve()
    hits = [0]
    delay = stall_ms / 1000.0

    def under(p) -> bool:
        try:
            Path(p).resolve().relative_to(run_dir)
            return True
        except (ValueError, OSError):
            return False

    try:
        from coordinator_core.ops.dispatch_emit import halt_resume_record as seam_mod
    except ImportError:
        seam_mod = None
    seam = getattr(seam_mod, "bounded_read", None)
    if seam is not None:
        def stalled_seam(path, *a, **k):
            if under(path):
                hits[0] += 1
                time.sleep(delay)
            return seam(path, *a, **k)

        mp.setattr(seam_mod, "bounded_read", stalled_seam)
    else:
        real = Path.read_text

        def stalled_read_text(self, *a, **k):
            if under(self):
                hits[0] += 1
                time.sleep(delay)
            return real(self, *a, **k)

        mp.setattr(Path, "read_text", stalled_read_text)

    # The hook's other bounded reads (fingerprint, live-run index) stall too, so the replay spends the
    # whole cumulative budget across keys rather than one key's share.
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
    from coordinator_core.ops.dispatch_emit import live_run_index

    for mod, name in ((hrr, "fingerprint_for_run"), (live_run_index, "read")):
        real_fn = getattr(mod, name)

        def stalled_fn(*a, _real=real_fn, **k):
            hits[0] += 1
            time.sleep(delay)
            return _real(*a, **k)

        mp.setattr(mod, name, stalled_fn)
    yield hits


def replay(
    repo: Path, sizing_rel: str | None = None, top: int = 20, cold_only: bool = False,
    stall_ms: float | None = None, reset=None,
) -> dict:
    """Drive `submit_engine_ask.evaluate` (launch stubbed) on a halted sizing in the real tree `repo`.

    Reports cold and warm process ms, git spawns of one warm call (`git_spawns` counts every spawn
    inside the hook call, `hook_git_spawns` only the git ones), the hook's decision and whether
    the launch stub was reached or a script was emitted (`reaches_launch`, the resume taking the
    halted run). `cold_only` skips the warm legs. `stall_ms` adds a stalled-read leg: run-dir reads
    sleep that long, and `stall_run_started` / `stall_process_ms` / `stall_wall_ms` report whether the
    run still started and what the hook cost. NOT read-only: every emitting call writes a workflow
    script and run record into `repo`, and candidates are tried newest first until one emits, so point
    it at a scratch clone."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit.halted_run import halted_run_to_resume

    repo = Path(repo).resolve()
    candidates = [sizing_rel] if sizing_rel else halted_sizings(repo)
    if not candidates:
        raise SystemExit(f"no halted run record under {repo / 'state' / 'warp'}; pass --sizing")
    launched: list = []
    payload: dict = {}

    def call() -> dict:
        launched.clear()
        def stub_launch(*a, **k):
            launched.append((a, k))
            return repo / "scratch" / "bench-launcher.workflow.mjs"

        return hook.evaluate(payload, launch=stub_launch)

    mp = pytest.MonkeyPatch()
    try:
        mp.chdir(repo)
        cold_ms = None
        tried = 0
        for sizing_rel in candidates:
            tried += 1
            payload.clear()
            payload.update({
                "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SESSION_ID,
                "cwd": str(repo), "tool_use_id": "replay-resume-emit",
                "tool_input": {"name": "coordinator:engine-ask", "args": {"sizing": sizing_rel}},
            })
            if reset:
                reset()
            was_halted = halted_run_to_resume(repo, sizing_rel) is not None
            t0 = time.process_time()
            first = call()
            if cold_ms is None:
                cold_ms = (time.process_time() - t0) * 1000.0
            if launched or _emitted(first):
                break
        consumed = _halt_status(repo) == "resumed" if reset else None
        if cold_only:
            out, spawn_argv, warm_ms, text = first, [], None, ""
        else:
            if reset:
                reset()
            counter_mp = pytest.MonkeyPatch()
            with _count_spawns_attributed(counter_mp) as spawns:
                out = call()
            spawn_argv = [s.argv for s in spawns]
            if reset:
                reset()
            profiler = cProfile.Profile()
            profiler.runcall(call)
            if reset:
                samples = []
                for _ in range(5):
                    reset()
                    p0 = time.process_time()
                    call()
                    samples.append((time.process_time() - p0) * 1000.0)
                warm_ms = round(sorted(samples)[len(samples) // 2], 1)
            else:
                try:
                    warm_ms = in_process_time_ms(call)["process_time_ms"]
                except ValueError:
                    warm_ms = None
            text = _top(pstats.Stats(profiler, stream=io.StringIO()), top)
        reached = bool(launched) or _emitted(out)
        stall: dict = {}
        if stall_ms is not None:
            stall_mp = pytest.MonkeyPatch()
            try:
                if reset:
                    reset()
                with _stalled_run_dir_reads(stall_mp, repo, stall_ms) as hits:
                    p0, w0 = time.process_time(), time.perf_counter()
                    stalled = call()
                    stall_process = (time.process_time() - p0) * 1000.0
                    stall_wall = (time.perf_counter() - w0) * 1000.0
            finally:
                stall_mp.undo()
            stall_decision = (stalled.get("hookSpecificOutput") or {}).get("permissionDecision")
            stall = {
                "stall_ms": stall_ms,
                "stalled_reads": hits[0],
                "stall_run_started": (bool(launched) or _emitted(stalled)) and stall_decision != "deny",
                "stall_decision": stall_decision,
                "stall_composed_inline": _emitted(stalled),
                "stall_process_ms": round(stall_process, 1),
                "stall_wall_ms": round(stall_wall, 1),
            }
    finally:
        mp.undo()
    decision = (out.get("hookSpecificOutput") or {}).get("permissionDecision")
    return {
        "repo": str(repo),
        "sizing": sizing_rel,
        "cold_process_ms": round(cold_ms, 1),
        "warm_process_ms": warm_ms,
        "git_spawns": len(spawn_argv),
        "hook_git_spawns": sum(1 for a in spawn_argv if _is_git(a)),
        "git_spawn_argv": spawn_argv,
        **stall,
        "candidates_tried": tried,
        "decision": decision,
        "reaches_launch": reached,
        "halt_consumed": consumed,
        "was_halted": was_halted,
        "record_hit": "resume-script" in str((out.get("hookSpecificOutput") or {}).get("permissionDecisionReason")),
        "resumes_halted_run": reached and decision != "deny" and was_halted,
        "reason": (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason"),
        "top": text,
    }


def _scratch_parent() -> str:
    from coordinator_core.temp_layout import managed_tempdir

    return str(managed_tempdir())


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commits", type=int, default=DEFAULT_COMMITS)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--profile-out", default=None, help="write the cProfile top-N here")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when warm process ms >= {BAR_MS:.0f}")
    ap.add_argument("--repo", default=None, help="replay evaluate on a halted sizing in this real tree")
    ap.add_argument("--sizing", default=None, help="with --repo: the sizing (default: newest halted run's)")
    ap.add_argument("--cold", action="store_true", help="with --repo: report the cold call alone")
    ap.add_argument("--stall-ms", type=float, default=None, help="with --repo: stall run-dir reads N ms")
    ap.add_argument("--seed-fixture", action="store_true", help="with --repo: seed a halted resume fixture (commits!) and replay it")
    args = ap.parse_args()
    if (args.cold or args.stall_ms is not None or args.seed_fixture) and not args.repo:
        ap.error("--cold, --stall-ms and --seed-fixture need --repo")
    if args.repo:
        reset = None
        if args.seed_fixture:
            repo = Path(args.repo).resolve()
            args.sizing = seed_resume_fixture(repo)
            reset = lambda: reset_resume_fixture(repo)  # noqa: E731
        result = replay(Path(args.repo), args.sizing, args.top, args.cold, args.stall_ms, reset)
    else:
        result = measure(args.commits, args.top)
    top = result.pop("top")
    for key, value in result.items():
        print(f"{key}: {value}")
    gauge = result["cold_process_ms"] if args.cold else result["warm_process_ms"]
    over = gauge is None or gauge >= BAR_MS
    print(f"bar: {'OVER' if over else 'UNDER'} {BAR_MS:.0f}ms process time {'cold' if args.cold else 'warm'}")
    print(top)
    if args.profile_out:
        Path(args.profile_out).write_text(top, encoding="utf-8", newline="\n")
    if not result["resumes_halted_run"]:
        return 1
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
