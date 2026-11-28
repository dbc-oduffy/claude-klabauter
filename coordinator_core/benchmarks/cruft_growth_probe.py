"""Falsifier probe for engine cruft: owned paths left and bytes grown across repeated killed and normal runs.

Each fixture run is a child process that writes, through the engine's own writers, a managed temp
dir, a scratch copy plus its run ledger, a warp dir, a subagent-share dir, a session-hub dir, a
temp git index (`scratch/gw/*.index`) and a warm stall record. Every `--kill-every`-th child dies
by `os._exit` with no atexit, finally or cleanup; the others end normally and fire the run-end
reap (`run_scratch_reap.reap_run`) over their own run. The parent fires the default SessionStart
prune (`prune_claude_temp.main`, drain) before each run and once after the last. Everything lives
under one tmp root; Temp is redirected into it, so no box directory is read or written.

Owned paths are backdated `--age-hours` (default 72: past a 24h owner-dead floor, inside the 7-day
age retention), so only a pruner keyed on owner liveness can remove a killed run's paths.

Output fields: `residual_owned_paths` (owned paths still present after the final prune, total and
per run and per kind), `bytes_after_run`, `peak_run_footprint_bytes`, `bytes_growth_run2_to_runN`,
`bytes_growth_per_run`, `bounded`, and per-prune `process_time_ms`/`spawns`/`listings`. Wall clock
is never recorded. Exit 0 only when residue is 0 and growth is bounded.

    python coordinator_core/benchmarks/cruft_growth_probe.py --runs 5 --kill-every 2 [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DEFAULT_AGE_HOURS = 72.0
DEFAULT_PAYLOAD_KIB = 16
KINDS = ("managed_temp", "scratch_copy", "warp_dir", "share_dir", "session_dir", "temp_index", "stall_dump")


@dataclass(frozen=True)
class Layout:
    """The fixture tree: a tmp repo, a redirected Temp dir, a warm svc dir and a manifest dir."""

    root: Path

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def stall_dir(self) -> Path:
        return self.root / "svc" / "stall-dumps"

    @property
    def manifests(self) -> Path:
        return self.root / "manifests"

    def build(self) -> "Layout":
        parts = self.root.parts
        if any(parts[i : i + 3] == ("coordinator", "_fleet", "run-temp") for i in range(len(parts))):
            raise ValueError(f"fixture root {self.root} sits under coordinator/_fleet/run-temp: children would skip the managed layout")
        (self.repo / ".git").mkdir(parents=True, exist_ok=True)
        (self.repo / ".git" / "index").write_bytes(b"DIRC" + (2).to_bytes(4, "big"))
        (self.repo / ".gitignore").write_text(
            "scratch/\ntasks/\n.coordinator-local/subagent-share/\n", encoding="utf-8"
        )
        for d in (self.temp, self.manifests, self.stall_dir, self.repo / "scratch"):
            d.mkdir(parents=True, exist_ok=True)
        return self


def run_id_for(index: int) -> str:
    return f"ask-20261126T0000{index:02d}-{index:06x}"


def session_id_for(index: int) -> str:
    return f"probe-sess-{index:02d}"


def _payload(kib: int) -> bytes:
    return b"x" * (kib * 1024)


def _write_tree(path: Path, payload: bytes) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "f.bin").write_bytes(payload)


def tree_bytes(path: Path) -> int:
    """Sum of file sizes under `path` (links not followed); 0 for a missing path."""
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
        except NotADirectoryError:
            try:
                total += os.stat(cur).st_size
            except OSError:
                pass
        except OSError:
            continue
    return total


def _backdate(path: Path, age_hours: float) -> None:
    """Set mtime/atime of `path` and everything under it to `age_hours` ago."""
    stamp = time.time() - age_hours * 3600
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        if os.path.isdir(cur) and not os.path.islink(cur):
            with os.scandir(cur) as it:
                stack.extend(e.path for e in it)
        os.utime(cur, (stamp, stamp))


def _manifest_path(layout: Layout, index: int) -> Path:
    return layout.manifests / f"run-{index:02d}.json"


def run_child(layout: Layout, index: int, killed: bool, payload_kib: int, age_hours: float) -> int:
    """One fixture run. Killed: dies after writing, skipping every cleanup. Normal: cleans up and reaps."""
    from coordinator_core import temp_layout
    from coordinator_core.ops.dispatch_emit import run_scratch, run_scratch_reap
    from coordinator_core.session import machinery_paths
    from coordinator_core.session_hub_path import session_dir_for
    from coordinator_core.warm import server as warm_server

    repo = layout.repo
    run_id, sid, data = run_id_for(index), session_id_for(index), _payload(payload_kib)
    owned: dict = {}
    with temp_layout.managed_temp_dir("cruft-probe", repo) as managed:
        _write_tree(managed, data)
        owned["managed_temp"] = managed
        scratch_rel = f"scratch/probe-copy-{index:02d}"
        warp_rel = f"scratch/warp/{run_id}"
        index_rel = f"scratch/gw/probe-{index:02d}.index"
        for rel in (scratch_rel, warp_rel):
            _write_tree(repo / rel, data)
        (repo / index_rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / index_rel).write_bytes(data)
        run_scratch.record_paths(repo, run_id, [scratch_rel, warp_rel])
        owned["scratch_copy"], owned["warp_dir"], owned["temp_index"] = (
            repo / scratch_rel, repo / warp_rel, repo / index_rel,
        )
        share = Path(machinery_paths.share_dir(str(repo), sid))
        _write_tree(share, data)
        session = session_dir_for(repo / ".git", sid)
        _write_tree(session, data)
        owned["share_dir"], owned["session_dir"] = share, session
        stall = warm_server._write_stall_stub(layout.stall_dir, None, key=f"probe{index:02d}", leg="cruft-probe")
        owned["stall_dump"] = stall
        for path in owned.values():
            _backdate(path, age_hours)
        manifest = {
            "index": index, "run_id": run_id, "session_id": sid, "killed": killed,
            "owned": {kind: str(path) for kind, path in owned.items()},
            "bytes": sum(tree_bytes(p) for p in owned.values()),
        }
        _manifest_path(layout, index).write_text(json.dumps(manifest), encoding="utf-8")
        if killed:
            sys.stdout.flush()
            os._exit(137)
    state = repo / "state" / "warp" / run_id
    state.mkdir(parents=True, exist_ok=True)
    (state / "halted.json").write_text(json.dumps({"status": "landed"}), encoding="utf-8")
    cpu0 = time.process_time()
    reap = run_scratch_reap.reap_run(repo, run_id)
    manifest["reap_process_time_ms"] = round((time.process_time() - cpu0) * 1000.0, 3)
    manifest["reap_removed"] = len(reap.get("removed", []))
    _manifest_path(layout, index).write_text(json.dumps(manifest), encoding="utf-8")
    return 0


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def spawn_child(layout: Layout, index: int, killed: bool, payload_kib: int, age_hours: float) -> int:
    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(layout.temp)
    cmd = [
        sys.executable, str(Path(__file__).resolve()), "--child", str(layout.root),
        "--index", str(index), "--payload-kib", str(payload_kib), "--age-hours", str(age_hours),
    ]
    if killed:
        cmd.append("--killed")
    return subprocess.run(
        cmd, env=env, capture_output=True, text=True, creationflags=_no_console(), cwd=str(_REPO)
    ).returncode


def sweep_once(layout: Layout, session_id: str) -> dict:
    """The default SessionStart prune over the fixture (`prune_claude_temp.main`, drain), metered."""
    from coordinator_core.benchmarks.run_scratch_reap_profile import _Meter
    from coordinator_core.ops import prune_claude_temp

    argv = ["prune_claude_temp.py", session_id, str(layout.repo), "--drain"]
    with _Meter() as meter:
        cpu0 = time.process_time()
        prune_claude_temp.main(argv)
        cpu = time.process_time() - cpu0
    return {"process_time_ms": round(cpu * 1000.0, 3), "spawns": meter.spawns, "listings": meter.listings}


def _present(path: str) -> bool:
    return os.path.lexists(path)


def run_probe(
    workdir: Path,
    runs: int,
    kill_every: int,
    *,
    payload_kib: int = DEFAULT_PAYLOAD_KIB,
    age_hours: float = DEFAULT_AGE_HOURS,
) -> dict:
    """Drive `runs` fixture runs under `workdir`; return the result dict (see module docstring)."""
    layout = Layout(Path(workdir)).build()
    saved = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")}
    saved_tempdir = tempfile.tempdir
    for key in saved:
        os.environ[key] = str(layout.temp)
    tempfile.tempdir = str(layout.temp)
    sweeps, bytes_after_run, kills = [], [], []
    try:
        for i in range(1, runs + 1):
            killed = kill_every > 0 and i % kill_every == 0
            kills.append(killed)
            sweeps.append(sweep_once(layout, session_id_for(i)))
            code = spawn_child(layout, i, killed, payload_kib, age_hours)
            if code != (137 if killed else 0):
                raise RuntimeError(f"probe child {i} exited {code}, expected {137 if killed else 0}")
            bytes_after_run.append(_fixture_bytes(layout))
        sweeps.append(sweep_once(layout, "probe-final"))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tempfile.tempdir = saved_tempdir
    manifests = [json.loads(_manifest_path(layout, i).read_text(encoding="utf-8")) for i in range(1, runs + 1)]
    per_run: list = []
    residual = 0
    by_kind = {kind: 0 for kind in KINDS}
    for m in manifests:
        left = [kind for kind, path in m["owned"].items() if _present(path)]
        for kind in left:
            by_kind[kind] += 1
        residual += len(left)
        per_run.append({"run": m["index"], "killed": m["killed"], "residual_owned_paths": len(left), "residual_kinds": left})
    peak = max((m["bytes"] for m in manifests), default=0)
    growth = bytes_after_run[-1] - bytes_after_run[1] if runs >= 3 else 0
    reaps = [m["reap_process_time_ms"] for m in manifests if "reap_process_time_ms" in m]
    return {
        "runs": runs, "kill_every": kill_every, "age_hours": age_hours, "payload_kib": payload_kib,
        "residual_owned_paths": residual,
        "residual_by_kind": by_kind,
        "per_run": per_run,
        "bytes_after_run": bytes_after_run,
        "final_bytes": _fixture_bytes(layout),
        "peak_run_footprint_bytes": peak,
        "bytes_growth_run2_to_runN": growth,
        "bytes_growth_per_run": round(growth / (runs - 2), 1) if runs >= 3 else 0.0,
        "bounded": growth <= peak,
        "prune_sweeps": sweeps,
        "reap_process_time_ms": reaps,
        "child_spawns": runs,
        "green": residual == 0 and growth <= peak,
    }


def _fixture_bytes(layout: Layout) -> int:
    """Bytes under the fixture root, the probe's own manifests excluded."""
    return sum(tree_bytes(p) for p in layout.root.iterdir() if p.name != "manifests")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--kill-every", type=int, default=2)
    p.add_argument("--payload-kib", type=int, default=DEFAULT_PAYLOAD_KIB)
    p.add_argument("--age-hours", type=float, default=DEFAULT_AGE_HOURS)
    p.add_argument("--json", action="store_true")
    p.add_argument("--child", metavar="ROOT", help=argparse.SUPPRESS)
    p.add_argument("--index", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--killed", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.child:
        return run_child(Layout(Path(args.child)), args.index, args.killed, args.payload_kib, args.age_hours)
    from coordinator_core import temp_layout

    # A workdir under `_fleet/run-temp` would make every child skip the managed layout and its dead-owner reap.
    with temp_layout.managed_temp_dir("cruft-growth-probe", _REPO) as work:
        result = run_probe(
            work, args.runs, args.kill_every, payload_kib=args.payload_kib, age_hours=args.age_hours
        )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"runs={result['runs']} kill_every={result['kill_every']} "
            f"residual_owned_paths={result['residual_owned_paths']} "
            f"bytes_growth_per_run={result['bytes_growth_per_run']} "
            f"bounded={result['bounded']} peak_run_footprint_bytes={result['peak_run_footprint_bytes']}"
        )
        print(f"residual_by_kind={result['residual_by_kind']}")
        print(f"bytes_after_run={result['bytes_after_run']} final_bytes={result['final_bytes']}")
        for s in result["prune_sweeps"]:
            print(f"prune process_time_ms={s['process_time_ms']} spawns={s['spawns']} listings={s['listings']}")
    return 0 if result["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
