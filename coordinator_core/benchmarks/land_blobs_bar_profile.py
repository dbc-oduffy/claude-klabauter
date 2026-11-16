"""Process-time bar profile of the declared-blob landing op, with N live sibling landings in the same tree.

One landing of a clean run at default params, over a fixture whose record dir carries the run's recorded blob
(`kept.json`) while N sibling processes land their own runs (own session, run, plan, path) in the same repo at
the same moment (a go-file barrier), so index locks, ref moves and record-dir scans are live. Process time is the
measured process plus its descendants (`commit_leg_profile._ChildMeter`); wall clock is never recorded. Every
spawn the measured landing makes is captured by argv and checked against `land_blobs_contract.SPAWN_LEDGER`.

    python coordinator_core/benchmarks/land_blobs_bar_profile.py --live-siblings 6 [--target T] [--json] [--verify]

Targets: `prototype` (the spike: `run_landing`'s in-process writer behind a recorded-set guard and a bounded
ref-moved retry), `run_landing` (the terminal-commit landing as it stands), `op` (`land_blobs.land`). The default
is `op` once `land_blobs.py` exists, else `prototype`. Exit 2 when the measured landing is over `BAR_MS`, did not
commit, or spawned outside the ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402
from coordinator_core.benchmarks.land_run_slow_disk_profile import (  # noqa: E402
    DEFAULT_CHAIN,
    DEFAULT_TRACKED,
    OFF_CHAIN_BASE,
    OWN_LINES,
    PATH_P,
    PLAN,
    RUN_ID,
    SCRIPT,
    SELF,
    _bind_session,
    _git,
    _write,
    build_land_fixture,
)
from coordinator_core.ops.dispatch_emit import land_blobs_contract as contract  # noqa: E402

BAR_MS = contract.BAR_MS
WHOLE_OP = contract.OP_NAME
RESULT_MARK = "LAND-BLOBS-RESULT "
WORKER_WAIT_SECS = 180.0
TARGETS = ("prototype", "run_landing", "op")
REVIEW = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}


@dataclass
class AxisRow:
    """One measured axis."""

    axis: str
    process_time_ms: float = 0.0
    spawns: int = 0
    attribution: dict = field(default_factory=dict)


# --- the spike prototype -----------------------------------------------------------------------------------


def _recorded_blobs(root: Path, run_id: str) -> dict:
    """`{path: blob sha}` from the run's own `kept.json`; empty when the run recorded nothing."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    try:
        kept = json.loads((root / RECORD_DIR_ROOT / run_id / "kept.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for chunk in (kept.get("chunks") or {}).values() if isinstance(kept, dict) else ():
        out.update({p.replace("\\", "/"): s for p, s in ((chunk or {}).get("paths") or {}).items()})
    return out


def _runs_recording(root: Path, own_run: str, paths: set) -> list:
    """Run ids other than `own_run` whose `kept.json` holds a blob for any of `paths`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    found: list = []
    try:
        with os.scandir(root / RECORD_DIR_ROOT) as it:
            dirs = [e for e in it if e.name != own_run and e.is_dir(follow_symlinks=False)]
    except OSError:
        return found
    for entry in dirs:
        if paths & set(_recorded_blobs(root, entry.name)):
            found.append(entry.name)
    return sorted(found)


def _write_landing(root: Path, run_id: str, present: list, deleted: list, scans: dict, spine: dict,
                   message: str) -> dict:
    """One read pass over the declared paths (guard 1 compares each check-in blob with the run's recorded blob),
    then tree, pack and index written in process and one compare-and-swap on the ref."""
    from coordinator_core import dispatch_abandon
    from coordinator_core.git import commit as gc
    from coordinator_core.git import git_objects, git_state
    from coordinator_core.git import index_write as iw
    from coordinator_core.git.call_io_scope import call_io_scope
    from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
    from coordinator_core.ops.dispatch_emit import run_landing as rl

    gitdir, common = resolve_git_dir(root), resolve_git_common_dir(root)
    old_head = git_state.head_sha(root)
    if old_head is None or rl._needs_fallback(root, gitdir, {}):
        return rl._fallback_commit(root, present, deleted, message, {})
    by_dir: dict = {}
    for rel in present:
        d, _, name = rel.rpartition("/")
        by_dir.setdefault(d, []).append(name)
    all_dirs = {""}
    for rel in (*present, *deleted):
        d = rel.rpartition("/")[0]
        while d:
            all_dirs.add(d)
            d = d.rpartition("/")[0]
    trees = {d: dict(spine.get(d, {})) for d in all_dirs}
    recorded = _recorded_blobs(root, run_id)
    pending: dict = {}
    index_new: dict = {}
    buffers: dict = {}
    foreign: list = []
    with call_io_scope():
        form = rl._CheckinForm(root, by_dir)
        for d, names in by_dir.items():
            head_dir = spine.get(d, {})
            for name in names:
                rel = f"{d}/{name}" if d else name
                entry = scans[d][name]
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    return rl._fallback_commit(root, present, deleted, message, {})
                data, st = rl._read_file(entry)
                data = form(d, name, rel, data)
                sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
                if rel in recorded and recorded[rel] != sha:
                    foreign.append(rel)
                    continue
                rl._stage(rel, name, d, data, sha, st, head_dir.get(name), trees, pending, index_new, buffers, spine)
    if foreign:
        return rl._error(f"{len(foreign)} staged path(s) carry a hunk outside run {run_id}'s recorded set",
                         refused=contract.FOREIGN_HUNK, foreign_paths=sorted(foreign),
                         foreign_runs=_runs_recording(root, run_id, set(foreign)))
    findings = rl._secret_findings(common, spine, buffers)
    if findings:
        from coordinator_core.ops.dispatch_emit import landing_secrets as ls

        return rl._error(ls.refusal(findings) or "secret in diff", refused=contract.SECRET_IN_DIFF)
    for rel in deleted:
        d, _, name = rel.rpartition("/")
        trees[d].pop(name, None)
        index_new[rel.encode("utf-8")] = None
    root_tree = rl._write_trees(trees, pending)
    if root_tree == git_state.head_tree_sha(root):
        return {"committed": False, "nothing_to_commit": True}
    name, email = gc._identity(root)
    who = f"{name} <{email}> {gc._stamp()}"
    body = f"tree {root_tree}\nparent {old_head}\nauthor {who}\ncommitter {who}\n\n{message}".encode("utf-8")
    commit_sha = hashlib.sha1(b"commit %d\x00" % len(body) + body).hexdigest()
    pending[commit_sha] = (b"commit", body)
    git_objects._flush_pack(common, pending)
    target = gc._cas_target(root)
    index_bytes = rl._spliced_index(gitdir / "index", index_new)
    if target is None:
        return rl._fallback_commit(root, present, deleted, message, {})
    if index_bytes is None:
        return rl._error("the index was unreadable or not the v2 shape", refused=contract.INDEX_LOCK_BUSY)
    ref_gitdir, ref = target
    if dispatch_abandon.is_abandoned():
        return rl._error("dispatch gave up before the ref update", refused=contract.DISPATCH_ABANDONED)
    try:
        fd = os.open(str(gitdir / "index.lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return rl._error("index.lock is held by a peer", refused=contract.INDEX_LOCK_BUSY)
    prepared = iw.PreparedSplice(gitdir / "index.lock", gitdir / "index")
    try:
        prepared._claim_owner()
        with os.fdopen(fd, "wb") as handle:
            handle.write(index_bytes)
        if not git_objects.cas_ref(ref_gitdir, ref, old_head, commit_sha, reflog_committer=f"{name} <{email}>",
                                   reflog_message=f"commit: {message.splitlines()[0]}", head_gitdir=gitdir):
            prepared.abort()
            return rl._error("HEAD moved while landing", refused=contract.HEAD_MOVED)
    except BaseException:
        prepared.abort()
        raise
    prepared.publish()
    return {"committed": True, "sha": commit_sha, "landed_paths": sorted([*present, *deleted]),
            "changed_paths": sorted([*buffers, *deleted])}


def _prototype_attempt(root: Path, params: dict) -> dict:
    from coordinator_core.git import git_state
    from coordinator_core.ops.dispatch_emit import run_landing as rl

    request, _run_dir, _text = rl._read_request(root, params)
    landing, stranded, _chunk_ids, _by_chunk = rl._declared(root, request, set(params.get("incomplete_chunks") or []))
    if not landing:
        return {"committed": False, "nothing_to_commit": True, "stranded": stranded}
    spine = git_state.read_tree_spine(root, landing)
    if spine is None:
        raise rl._Refused("head-unreadable", "HEAD's tree could not be read")
    scans = rl._scandir_all(root, {p.rpartition("/")[0] for p in landing})
    present, deleted, _dropped = rl._live_paths(root, landing, scans, spine)
    return _write_landing(root, params["run_id"], present, deleted, scans, spine, f"land: {params['run_id']}\n")


def prototype_land(params: dict, repo_root: Path) -> dict:
    """The spike's landing: review floor, declared set, recorded-set guard, `run_landing._commit` (tree, pack and
    index in process, one compare-and-swap), and a bounded retry when the ref or the index lock moved."""
    from coordinator_core.lifecycle import main_worktree_root
    from coordinator_core.ops.dispatch_emit import run_landing as rl

    root = main_worktree_root(repo_root)
    floor = rl._review_floor(params.get("inline_review"))
    if floor is not None:
        return floor
    reply: dict = {}
    for attempt in range(contract.RETRY_ATTEMPTS):
        try:
            reply = _prototype_attempt(root, params)
        except rl._Refused as exc:
            return rl._error(str(exc), refused=exc.kind, **exc.extra)
        if reply.get("refused") not in contract.RETRIED_REFUSALS:
            return reply
        time.sleep(0.005 * (attempt + 1))
    return reply


def _target_call(target: str):
    if target == "prototype":
        return prototype_land
    if target == "run_landing":
        from coordinator_core.ops.dispatch_emit import run_landing

        return run_landing.land_run
    from coordinator_core.ops.dispatch_emit import land_blobs

    return land_blobs.land


def default_target() -> str:
    from importlib.util import find_spec

    return "op" if find_spec("coordinator_core.ops.dispatch_emit.land_blobs") else "prototype"


# --- fixture -----------------------------------------------------------------------------------------------


def live_spec(i: int) -> dict:
    """The identity of live sibling `i`: its own session, run, script, plan and path."""
    return {
        "session": f"{0x1000 + i:08x}-0000-4000-8000-0000000000c0",
        "run_id": f"plan-20261028T1000{i:02d}-c1{i:04x}",
        "script": f"run-live{i}.mjs",
        "plan": f"docs/plans/live-{i}.md",
        "path": f"src/live{i}/p.py",
    }


def record_kept(root: Path, run_id: str, path: str) -> str:
    """Write the run's `kept.json` naming the worktree blob of `path`; returns the blob sha."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    data = (root / path).read_bytes()
    sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/kept.json", json.dumps({"chunks": {"C1": {"paths": {path: sha}}}}))
    return sha


def add_live_sibling_runs(root: Path, count: int) -> list:
    """For each live sibling: a stamped checkpoint commit adding its own lines to its own path, a worktree edit
    past it, its recorded blob, `run.json` and emitted script."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    specs = [live_spec(i) for i in range(count)]
    for spec in specs:
        sess, path, plan = spec["session"], spec["path"], spec["plan"]
        _write(root, path, "head = 0\n" + OWN_LINES)
        _write(root, plan, "# live plan\n")
        _git(["add", "--", path, plan], root)
        message = (f"checkpoint(wave 0): 1 row - C1\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {OFF_CHAIN_BASE}\n"
                   f"Checkpoint-Not-Landed: run-open\nSession-Id: {sess}\n")
        _git(["commit", "-q", "-m", message, "--", path, plan], root)
        _write(root, path, "head = 0\n" + OWN_LINES + "tail = 1\n")
        record_kept(root, spec["run_id"], path)
        _write(root, f"{RECORD_DIR_ROOT}/{spec['run_id']}/run.json", json.dumps({"run_id": spec["run_id"]}))
        request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(path,)),), plan_path=plan)
        _write(root, spec["script"], "// emitted\n" + render_marker(request) + "\n")
    return specs


def _params(base: str, spec: "dict | None") -> dict:
    return {
        "script_path": SCRIPT if spec is None else spec["script"], "incomplete_chunks": [],
        "inline_review": {**REVIEW, "prep": {"run_base_sha": base}},
        "session_id": SELF if spec is None else spec["session"],
        "run_id": RUN_ID if spec is None else spec["run_id"], "run_base_sha": base,
    }


def _outcome(reply: object) -> str:
    if not isinstance(reply, dict):
        return "no-reply"
    return str(reply.get("refused") or ("committed" if reply.get("committed") else "not-committed"))


# --- workers -----------------------------------------------------------------------------------------------


class _Spawns:
    """argv of every process the measured landing starts (`subprocess.Popen.__init__` seam)."""

    def __init__(self) -> None:
        self.argvs: list = []
        self._real = subprocess.Popen.__init__

    def install(self) -> None:
        seen, real = self.argvs, self._real

        def init(popen, args, *a, **k):
            seen.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
            return real(popen, args, *a, **k)

        subprocess.Popen.__init__ = init

    def remove(self) -> None:
        subprocess.Popen.__init__ = self._real


def _measure(call, root: Path, params: dict, target: str) -> AxisRow:
    spawns = _Spawns()
    meter = _ChildMeter()
    reply: object = None
    error = ""
    spawns.install()
    meter.start()
    cpu0 = time.process_time()
    try:
        reply = call(params, root / ".git")
    except BaseException as exc:  # noqa: BLE001 -- a raised op is a row, not a crash of the harness
        error = type(exc).__name__
    finally:
        ms = round((time.process_time() - cpu0) * 1000.0 + meter.stop_ms(), 3)
        spawns.remove()
    out = error and f"raised:{error}" or _outcome(reply)
    return AxisRow(
        WHOLE_OP, ms, len(spawns.argvs),
        {"target": target, "outcome": out, "spawn_argvs": spawns.argvs, "unledgered_spawns": contract.unledgered(spawns.argvs),
         "sha": reply.get("sha") if isinstance(reply, dict) else None,
         "error": str(reply.get("error", "")) if isinstance(reply, dict) else error},
    )


def _worker(tag: str, root: Path, base: str, sync: Path, target: str) -> int:
    """A live-run process: import the landing, announce `ready-<tag>`, wait for `go`, then land (the measured
    worker `m` prints its measured row, a sibling `s<i>` prints its outcome)."""
    declare_benchmark_origin()
    call = _target_call(target)
    measured = tag == "m"
    spec = None if measured else live_spec(int(tag[1:]))
    if measured:
        _bind_session()
    else:
        os.environ["COORDINATOR_SESSION_ID"] = spec["session"]
    (sync / f"ready-{tag}").write_text("1", encoding="utf-8")
    give_up = time.monotonic() + WORKER_WAIT_SECS
    while not (sync / "go").exists():
        if time.monotonic() > give_up:
            return 3
        time.sleep(0.005)
    params = _params(base, spec)
    if measured:
        payload = asdict(_measure(call, root, params, target))
    else:
        try:
            payload = {"outcome": _outcome(call(params, root / ".git"))}
        except Exception as exc:  # noqa: BLE001 -- a raised sibling is an outcome, not a crash
            payload = {"outcome": f"raised:{type(exc).__name__}"}
    print(RESULT_MARK + json.dumps(payload))
    return 0


def _spawn_worker(tag: str, root: Path, base: str, sync: Path, target: str) -> subprocess.Popen:
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", tag, "--worker-root", str(root),
            "--worker-base", base, "--worker-sync", str(sync), "--target", target]
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _result_of(tag: str, proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=WORKER_WAIT_SECS)
    for line in reversed(out.splitlines()):
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
    raise RuntimeError(f"live worker {tag} rc={proc.returncode} printed no result: {err.strip()[-400:]}")


def run_live(workdir: Path, live: int, tracked: int, chain: int, target: str, verify: bool) -> AxisRow:
    """The measured landing (its own process) with `live` sibling processes landing their own runs in the same
    tree at the same moment."""
    declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        root, base = build_land_fixture(workdir, mode="clean", tracked=tracked, chain=chain, stack=stack)
        recorded = record_kept(root, RUN_ID, PATH_P)
        add_live_sibling_runs(root, live)
        sync = workdir / "sync"
        sync.mkdir()
        tags = ["m"] + [f"s{i}" for i in range(live)]
        with ThreadPoolExecutor(max_workers=len(tags)) as pool:
            procs = dict(zip(tags, pool.map(lambda t: _spawn_worker(t, root, base, sync, target), tags)))
        try:
            give_up = time.monotonic() + WORKER_WAIT_SECS
            while not all((sync / f"ready-{tag}").exists() for tag in tags):
                dead = [t for t, p in procs.items() if p.poll() is not None]
                if dead or time.monotonic() > give_up:
                    raise RuntimeError(f"live workers not ready (exited early: {dead}): "
                                       + "; ".join(procs[t].communicate()[1].strip()[-300:] for t in dead))
                time.sleep(0.01)
            (sync / "go").write_text("1", encoding="utf-8")
            results = {tag: _result_of(tag, proc) for tag, proc in procs.items()}
        finally:
            for proc in procs.values():
                if proc.poll() is None:
                    proc.kill()
        row = AxisRow(**results["m"])
        row.attribution["live_siblings"] = live
        row.attribution["live_sibling_outcomes"] = {t: r["outcome"] for t, r in results.items() if t != "m"}
        if verify:
            row.attribution["verified"] = _verify(root, row, recorded)
    return row


def _verify(root: Path, row: AxisRow, recorded: str) -> bool:
    """The landed blob of the measured path equals the recorded blob and the repository is intact (git spawns,
    outside the measured run)."""
    if row.attribution["outcome"] != "committed":
        return False
    landed = _git(["rev-parse", f"HEAD:{PATH_P}"], root).strip()
    fsck = subprocess.run(["git", "fsck", "--no-dangling", "--no-progress"], cwd=str(root), capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return landed == recorded and fsck.returncode == 0


def render(row: AxisRow) -> str:
    verdict = "OVER" if row.process_time_ms >= BAR_MS else "ok"
    return (f"{row.axis} target={row.attribution['target']} process_time_ms={row.process_time_ms:.1f} "
            f"spawns={row.spawns} unledgered={len(row.attribution['unledgered_spawns'])} {verdict} "
            f"outcome={row.attribution['outcome']} siblings={row.attribution['live_sibling_outcomes']}")


def passed(row: AxisRow) -> bool:
    return (row.process_time_ms < BAR_MS and row.attribution["outcome"] == "committed"
            and not row.attribution["unledgered_spawns"] and row.attribution.get("verified", True))


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live-siblings", type=int, default=6, metavar="N")
    ap.add_argument("--target", choices=TARGETS, default=None)
    ap.add_argument("--tracked", type=int, default=DEFAULT_TRACKED)
    ap.add_argument("--chain", type=int, default=DEFAULT_CHAIN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verify", action="store_true", help="check the landed blob and `git fsck` after the run")
    ap.add_argument("--dir", default=None)
    for hidden in ("--worker", "--worker-root", "--worker-base", "--worker-sync"):
        ap.add_argument(hidden, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    target = args.target or default_target()
    if args.worker:
        return _worker(args.worker, Path(args.worker_root), args.worker_base, Path(args.worker_sync), target)
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.temp_layout import make_managed_temp_dir, mkdtemp_in

    workdir = make_managed_temp_dir(prefix="lbbar-") if args.dir is None else mkdtemp_in("lbbar-", args.dir)
    try:
        with fixture_settings_home(workdir):
            row = run_live(workdir, args.live_siblings, args.tracked, args.chain, target, args.verify)
    finally:
        rmtree_or_raise(workdir, label="land-blobs-bar")
    print(json.dumps([asdict(row)], indent=1) if args.json else render(row))
    if not passed(row):
        print(f"BAR: {WHOLE_OP} not under {BAR_MS:.0f}ms process time on a clean, ledgered, committed landing (DR-344)",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
