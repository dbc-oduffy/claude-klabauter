"""Per-fire process census of the direct-exec `hook-run` door image, over every door registration.

Stages the door image as `<repo>/scratch/<run>/hook-run[.exe]` (the door routes by its own module
basename, so the staged name is what makes it dial `hooks.*`), rewrites every door registration of the
source hooks.json to exec form `{command: <staged>, args: ["--stdin-hook", ("--advisory"), "hooks.<op>"]}`
in a scratch hooks.json, and runs DoE's `door_spawn_census.py` over it in-process. DoE's census selects
only `door_launch.py` and shell-form registrations, so `select_doors` is extended on the loaded module
(never on DoE's file) to also select the staged exec form.

Bulk measurement, not an engine op: process counts per fire by family (bash, conhost, python, hook-run),
tree-attributed, plus tree CPU seconds (process time) and the elected warm server's write bytes
(`psutil` io_counters before/after each fire) with the files that grew in its service dir. The census is a
lower bound: a child that exits between polls is never seen, and `write_bytes` counts every write
the server made in the window, peers' requests included (`warm.idle_write_bytes_per_s` is the noise rate).
Fires are grouped per hook event (`per_event`, with a per-matcher split): one tool call fires every
registration of its event once.

Run: `python coordinator_core/benchmarks/hook_run_direct_exec_census.py [--census PATH] [--hooks-json PATH]
[--image PATH] [--fires N] [--out PATH] [--no-rewrite] [--rewritten-only]`. `--census` and `--hooks-json`
default to the registered `content_root` repo's `coordinator/tests/door_spawn_census.py` and
`coordinator/hooks/hooks.json`; with no registered repo a missing value is a typed refusal (exit 2) naming
the flag. Both the as-registered and the rewritten leg run unless `--rewritten-only`/`--no-rewrite`.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin

REPO_ROOT = Path(__file__).resolve().parents[2]
DOOR_DIR = REPO_ROOT / "coordinator_core" / "warm" / "door"
DEFAULT_FIRES = 5
DEFAULT_OUT = (REPO_ROOT / "docs" / "research" / "hook-run-direct-exec" / "2026-11-24-per-event-warm-census"
               / "outputs" / "baseline.json")
_SERVER_SCRIPT = ("coordinator_core", "warm", "server.py")
_IDLE_SECS = 1.0
_SNAPSHOT_CAP = 5000
STDIN_HOOK_FLAG = "--stdin-hook"
ADVISORY_FLAG = "--advisory"
#: door_launch.py flags that map to door semantics: `--pass-2` is "an advisory door never blocks".
_PASS_2 = "--pass-2"
_OP_PREFIX = "hooks."
_ENGINE_ROOT_SIDECAR = "door.engine-root.txt"
_FAMILIES = ("bash", "conhost", "python", "hook-run")


class CensusRefusal(Exception):
    """A typed refusal: the message names the flag that must be supplied or corrected."""


def staged_name() -> str:
    """The basename the door image must carry so the door routes to `hooks.*`."""
    return "hook-run.exe" if sys.platform == "win32" else "hook-run"


def default_image() -> Path:
    """`door-w.exe` when the repo carries it, else `door.exe` (`door` off win32)."""
    windowless = DOOR_DIR / "door-w.exe"
    if sys.platform == "win32" and windowless.is_file():
        return windowless
    return DOOR_DIR / ("door.exe" if sys.platform == "win32" else "door")


def door_op(hook: dict) -> Optional[tuple[str, bool]]:
    """`(hooks.<op>, advisory)` when `hook` is a door registration, else None.

    Recognises the `door_launch.py` exec form (python3 loader; `--advisory` or `--pass-2` marks an
    advisory door) and the shell form `<bin>/hook-run hooks.<op>`. Everything else is not a door.
    """
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return None
    args = hook.get("args")
    if args:
        if not any("door_launch.py" in str(a) for a in args):
            return None
        tokens = [str(a) for a in args]
    else:
        command = hook.get("command") or ""
        if "/bin/hook-run" not in command:
            return None
        tokens = command.replace('"', " ").split()
    ops = [t for t in tokens if t.startswith(_OP_PREFIX)]
    if not ops:
        return None
    return ops[0], bool({ADVISORY_FLAG, _PASS_2} & set(tokens))


def rewrite_hooks(doc: dict, staged: str) -> tuple[dict, int]:
    """A copy of the hooks document with every door registration rewritten to direct exec of `staged`.

    The rewritten hook keeps its other keys (timeout, async) and drops `shell`; non-door hooks are
    untouched. Returns `(rewritten_doc, doors_rewritten)`.
    """
    out = copy.deepcopy(doc)
    count = 0
    for groups in out.get("hooks", {}).values():
        for group in groups:
            hooks = group.get("hooks", [])
            for i, hook in enumerate(hooks):
                found = door_op(hook)
                if found is None:
                    continue
                op, advisory = found
                new = {k: v for k, v in hook.items() if k not in ("command", "args", "shell")}
                new["command"] = staged
                new["args"] = [STDIN_HOOK_FLAG, *([ADVISORY_FLAG] if advisory else []), op]
                hooks[i] = new
                count += 1
    return out, count


def stage_image(image: Path, run_dir: Path) -> Path:
    """Hardlink (copy on failure) `image` to `run_dir/hook-run[.exe]`, with the engine-root sidecar beside it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    staged = run_dir / staged_name()
    try:
        os.link(image, staged)
    except OSError:
        shutil.copy2(image, staged)
    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or str(Path.home() / ".coordinator-claude-settings")
    for sidecar in (image.parent / _ENGINE_ROOT_SIDECAR, Path(home) / "bin" / _ENGINE_ROOT_SIDECAR):
        if sidecar.is_file():
            shutil.copy2(sidecar, run_dir / _ENGINE_ROOT_SIDECAR)
            break
    return staged


def load_census(path: Path) -> ModuleType:
    """Import DoE's census module from `path` and extend its door selection to the staged exec form."""
    spec = importlib.util.spec_from_file_location("door_spawn_census", path)
    if spec is None or spec.loader is None:
        raise CensusRefusal(f"--census {path}: not an importable python file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original = module.select_doors

    def select_doors(hooks_json: Path) -> list:
        doors = list(original(hooks_json))
        doc = json.loads(Path(hooks_json).read_text(encoding="utf-8"))
        for event, groups in doc.get("hooks", {}).items():
            for group in groups:
                for hook in group.get("hooks", []):
                    args = hook.get("args") or []
                    if (STDIN_HOOK_FLAG in args
                            and Path(str(hook.get("command", ""))).stem == "hook-run"):
                        doors.append({"event": event, "matcher": group.get("matcher", ""),
                                      "form": "direct", "hook": hook})
        return doors

    module.select_doors = select_doors
    return module


def server_engine_root(cmdline: list[str]) -> Optional[Path]:
    """The engine clone a `coordinator_core/warm/server.py` command line runs, else None."""
    for arg in cmdline:
        parts = Path(arg.strip('"')).parts
        if len(parts) > 3 and tuple(p.lower() for p in parts[-3:]) == _SERVER_SCRIPT:
            return Path(*parts[:-3])
    return None


def find_warm_servers() -> list[dict]:
    """Every live warm server process: pid, ppid, parent liveness, engine root, write bytes.

    `parent_alive` is False for an orphan (parent gone, or its pid reused by a younger process).
    """
    import psutil

    out = []
    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "create_time"]):
        info = proc.info
        root = server_engine_root(info.get("cmdline") or [])
        if root is None:
            continue
        parent_alive = False
        try:
            parent = psutil.Process(info["ppid"])
            parent_alive = parent.create_time() <= info["create_time"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        out.append({"pid": info["pid"], "ppid": info["ppid"], "parent_alive": parent_alive,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info["create_time"])),
                    "engine_root": str(root)})
    return out


def breadcrumb_pid(engine_root: Path) -> Optional[int]:
    """The pid the engine clone's warm breadcrumb names, None when unreadable or unstamped."""
    try:
        from coordinator_core.warm.breadcrumb import read_breadcrumb

        record = read_breadcrumb(engine_root)
    except Exception:  # noqa: BLE001 -- an unreadable breadcrumb only demotes "elected" to "all"
        return None
    pid = record.get("pid") if record else None
    return pid if isinstance(pid, int) else None


def warm_inventory(servers: list[dict]) -> dict:
    """Leak report over `servers`: duplicates per engine root, orphans, and which pids are elected."""
    elected = [s["pid"] for s in servers if breadcrumb_pid(Path(s["engine_root"])) == s["pid"]]
    roots: dict[str, int] = {}
    for s in servers:
        roots[s["engine_root"]] = roots.get(s["engine_root"], 0) + 1
    return {"servers": servers, "elected_pids": elected,
            "duplicate_roots": sorted(r for r, n in roots.items() if n > 1),
            "orphan_pids": [s["pid"] for s in servers if not s["parent_alive"]]}


def snapshot_tree(root: Path, cap: int = _SNAPSHOT_CAP) -> dict[str, tuple[int, int]]:
    """`{relative path: (mtime_ns, size)}` of the files under `root`, bounded at `cap` entries."""
    out: dict[str, tuple[int, int]] = {}
    stack = [root]
    while stack and len(out) < cap:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            st = entry.stat(follow_symlinks=False)
                            out[os.path.relpath(entry.path, root)] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def changed_files(before: dict, after: dict) -> list[dict]:
    """Files created or rewritten between two snapshots, with their size delta in bytes."""
    rows = []
    for rel, (mtime, size) in after.items():
        old = before.get(rel)
        if old is None or old != (mtime, size):
            rows.append({"file": rel.replace("\\", "/"), "size_delta": size - (old[1] if old else 0),
                         "created": old is None})
    return sorted(rows, key=lambda r: r["file"])


class WarmProbe:
    """Reads the selected warm servers' cumulative write bytes and the service dirs' file state.

    Selects the breadcrumb-elected servers; with none elected (an unstamped clone) it selects every
    live server and says so in `scope`. No server means every reading is None.
    """

    def __init__(self, inventory: dict) -> None:
        import psutil

        self.inventory = inventory
        elected = set(inventory["elected_pids"])
        chosen = [s for s in inventory["servers"] if s["pid"] in elected] or inventory["servers"]
        self.scope = "elected" if elected else ("all-servers" if chosen else "none")
        self.pids = [s["pid"] for s in chosen]
        self.procs = []
        for pid in self.pids:
            try:
                self.procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue
        self.dirs: list[Path] = []
        for root in sorted({s["engine_root"] for s in chosen}):
            try:
                from coordinator_core.warm.breadcrumb import svc_dir

                self.dirs.append(svc_dir(Path(root)))
            except Exception:  # noqa: BLE001 -- an unresolvable service dir only loses the file list
                continue

    def write_bytes(self) -> Optional[int]:
        """Sum of the selected servers' cumulative `write_bytes`, None when none can be read."""
        total, seen = 0, False
        for proc in self.procs:
            try:
                total += proc.io_counters().write_bytes
                seen = True
            except Exception:  # noqa: BLE001 -- a server that exited mid-census contributes nothing
                continue
        return total if seen else None

    def snapshot(self) -> dict:
        return {str(d): snapshot_tree(d) for d in self.dirs}

    def files_between(self, before: dict, after: dict) -> list[dict]:
        rows: list[dict] = []
        for d, snap in after.items():
            rows += changed_files(before.get(d, {}), snap)
        return rows

    def idle(self, secs: float = _IDLE_SECS) -> dict:
        """Write bytes and changed files over `secs` with no hook fired: the peer-traffic noise floor."""
        b0, s0, t0 = self.write_bytes(), self.snapshot(), time.perf_counter()
        time.sleep(secs)
        b1, s1 = self.write_bytes(), self.snapshot()
        elapsed = time.perf_counter() - t0
        rate = None if b0 is None or b1 is None else max(0, b1 - b0) / elapsed
        return {"write_bytes_per_s": rate, "files": self.files_between(s0, s1)}


def instrument_fire(census, probe: WarmProbe, records: list) -> None:
    """Replace the census module's `fire_once` with one that also records tree CPU seconds and the
    warm server's write bytes and grown files per fire, appending one dict per fire to `records`.

    The return value keeps the shape `run_census` aggregates (`rc`, `tree`, `window`).
    """
    import psutil

    def fire_once(argv: list[str], env: dict, cwd: str) -> dict:
        before = set(psutil.pids())
        bytes0, snap0 = probe.write_bytes(), probe.snapshot()
        started = time.perf_counter()
        root = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=cwd, creationflags=census._NO_CONSOLE)
        tree = {root.pid}
        seen: dict[tuple, Optional[str]] = {}
        seen_tree: dict[tuple, Optional[str]] = {}
        assert root.stdin and root.stdout and root.stderr
        procs: dict[tuple, Any] = {}
        cpu: dict[tuple, float] = {}
        known = set(before) | {root.pid}

        def track(key: tuple, proc) -> None:
            procs[key] = proc

        try:
            rp = psutil.Process(root.pid)
            track((root.pid, rp.create_time()), rp)
        except psutil.Error:
            pass

        def scan() -> None:
            for pid in set(psutil.pids()) - known:
                known.add(pid)
                try:
                    p = psutil.Process(pid)
                    key = (pid, p.create_time())
                    fam = census._family(p.name())
                    ppid = p.ppid()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                seen[key] = fam
                if ppid in tree:
                    tree.add(pid)
                    seen_tree[key] = fam
                    track(key, p)
            for key, p in procs.items():
                try:
                    t = p.cpu_times()
                    cpu[key] = t.user + t.system
                except psutil.Error:
                    continue

        try:
            root.stdin.write(json.dumps(census.BENIGN_EVENT).encode())
            root.stdin.close()
        except OSError:
            pass
        deadline = time.monotonic() + 60
        done_at = None
        while time.monotonic() < deadline:
            scan()
            if root.poll() is not None:
                done_at = done_at or time.monotonic()
                if time.monotonic() - done_at > census._TAIL_S:
                    break
            time.sleep(census._POLL_S)
        if root.poll() is None:
            root.kill()
        rc = root.wait()
        root.stdout.close()
        root.stderr.close()
        wall = time.perf_counter() - started
        bytes1, snap1 = probe.write_bytes(), probe.snapshot()

        def tally(d: dict) -> dict:
            out = {n: 0 for n in census.NAMES}
            for fam in d.values():
                if fam:
                    out[fam] += 1
            out["total"] = len(d)
            return out

        root_fam = census._family(Path(argv[0]).name)
        tree_counts = tally(seen_tree)
        if root_fam:
            tree_counts[root_fam] += 1
        tree_counts["total"] += 1
        records.append({"cpu_s": sum(cpu.values()), "wall_s": wall,
                        "warm_write_bytes": None if bytes0 is None or bytes1 is None else max(0, bytes1 - bytes0),
                        "warm_files": probe.files_between(snap0, snap1)})
        return {"rc": rc, "tree": tree_counts, "window": tally(seen)}

    census.fire_once = fire_once


def file_key(rel: str) -> str:
    """A file's family key: digit runs collapse to `N`, so per-request files
    (`ack-journal/d20261124/9712-6178-N.outcome.json`) count as one family."""
    return re.sub(r"\d+", "N", rel)


def collapse_files(rows: list[dict]) -> dict[str, dict]:
    """`{family key: {files, size_delta}}` over changed-file rows."""
    out: dict[str, dict] = {}
    for r in rows:
        slot = out.setdefault(file_key(r["file"]), {"files": 0, "size_delta": 0})
        slot["files"] += 1
        slot["size_delta"] += r["size_delta"]
    return dict(sorted(out.items()))


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def attach_fires(door_rows: list[dict], doors: list[dict], records: list[dict], fires: int,
                 idle_rate: Optional[float]) -> list[dict]:
    """Merge per-fire `records` (door-major, `fires` each) into the per-door rows.

    Adds `matcher`, `cpu_s` (mean tree CPU seconds), `warm_write_bytes` (mean raw delta),
    `warm_write_bytes_net` (raw less `idle_rate` x the fire's wall time, floored at 0) and `warm_files`
    (file -> fires in which it grew). Rows without a record (a short `records`) are left unannotated.
    """
    out = []
    for i, row in enumerate(door_rows):
        chunk = records[i * fires:(i + 1) * fires]
        new = dict(row)
        new["matcher"] = doors[i].get("matcher", "") if i < len(doors) else ""
        if chunk:
            raw = [r["warm_write_bytes"] for r in chunk if r["warm_write_bytes"] is not None]
            net = [max(0.0, r["warm_write_bytes"] - (idle_rate or 0.0) * r["wall_s"])
                   for r in chunk if r["warm_write_bytes"] is not None]
            files: dict[str, int] = {}
            for r in chunk:
                for f in r["warm_files"]:
                    key = file_key(f["file"])
                    files[key] = files.get(key, 0) + 1
            new.update({"cpu_s": round(_mean([r["cpu_s"] for r in chunk]), 4),
                        "warm_write_bytes": _mean(raw) if raw else None,
                        "warm_write_bytes_net": _mean(net) if net else None,
                        "warm_files": dict(sorted(files.items()))})
        out.append(new)
    return out


def group_per_event(door_rows: list[dict]) -> dict:
    """Per hook event, what one tool call costs: every registration of the event fires once.

    Sums the per-door mean tree counts by family, tree CPU seconds and warm write bytes; `by_matcher`
    repeats the sums per matcher, since a PreToolUse tool call fires only the matching matchers.
    """
    def fold(rows: list[dict]) -> dict:
        folded: dict = {"registrations": len(rows)}
        for fam in (*_FAMILIES, "total"):
            folded[f"{fam}_tree"] = round(sum(r[f"{fam}_tree"] for r in rows), 3) if rows and f"{fam}_tree" in rows[0] else 0.0
        folded["cpu_s"] = round(sum(r.get("cpu_s", 0.0) for r in rows), 4)
        raw = [r["warm_write_bytes"] for r in rows if r.get("warm_write_bytes") is not None]
        net = [r["warm_write_bytes_net"] for r in rows if r.get("warm_write_bytes_net") is not None]
        folded["warm_write_bytes"] = round(sum(raw), 1) if raw else None
        folded["warm_write_bytes_net"] = round(sum(net), 1) if net else None
        files: dict[str, int] = {}
        for r in rows:
            for name, n in r.get("warm_files", {}).items():
                files[name] = files.get(name, 0) + n
        folded["warm_files"] = dict(sorted(files.items()))
        return folded

    events: dict[str, list[dict]] = {}
    for row in door_rows:
        events.setdefault(row["event"], []).append(row)
    out = {}
    for event, rows in events.items():
        entry = fold(rows)
        matchers: dict[str, list[dict]] = {}
        for row in rows:
            matchers.setdefault(row.get("matcher", ""), []).append(row)
        entry["by_matcher"] = {m: fold(rs) for m, rs in matchers.items()}
        out[event] = entry
    return out


def default_doe_paths() -> tuple[Optional[Path], Optional[Path]]:
    """`(census, hooks.json)` of the registered `content_root` repo, `(None, None)` when unregistered."""
    try:
        home = os.environ.get("COORDINATOR_SETTINGS_HOME") or str(Path.home() / ".coordinator-claude-settings")
        sys.path.insert(0, str(Path(home) / "bin"))
        try:
            from claude_machine_local import repos

            root = Path(str(repos.doe_claude))
        finally:
            sys.path.pop(0)
    except Exception:  # noqa: BLE001 -- unregistered repo is a typed refusal in run()
        return None, None
    return root / "coordinator" / "tests" / "door_spawn_census.py", root / "coordinator" / "hooks" / "hooks.json"


def render_per_event(label: str, per_event: dict) -> str:
    """One text table: per event, registrations, mean tree process counts by family, CPU s, warm bytes."""
    lines = [f"[{label}] per event (one tool call = every registration of the event once)",
             f"{'event':<15}{'regs':>5}{'bash':>7}{'conhost':>9}{'python':>8}{'hook-run':>10}{'total':>7}"
             f"{'cpu_s':>8}{'warm_B':>10}{'warm_net_B':>12}"]
    for event, e in per_event.items():
        raw = "n/a" if e["warm_write_bytes"] is None else f"{e['warm_write_bytes']:.0f}"
        net = "n/a" if e["warm_write_bytes_net"] is None else f"{e['warm_write_bytes_net']:.0f}"
        lines.append(f"{event:<15}{e['registrations']:>5}{e['bash_tree']:>7.1f}{e['conhost_tree']:>9.1f}"
                     f"{e['python_tree']:>8.1f}{e['hook-run_tree']:>10.1f}{e['total_tree']:>7.1f}"
                     f"{e['cpu_s']:>8.2f}{raw:>10}{net:>12}")
    return "\n".join(lines)


def summarize(result: dict) -> dict:
    """Per-fire tree totals by family (bash, conhost, python, hook-run, total) plus the per-door rows."""
    agg = result["aggregate"]
    families = {n: agg[f"mean_{n}_tree_per_fire"] for n in (*_FAMILIES, "total")}
    return {
        "doors": agg["doors"],
        "fires_per_door": agg["fires_per_door"],
        "forms": result["forms"],
        "per_fire_tree": families,
        "per_fire_total": families["total"],
        "per_fire_window": {n: agg[f"mean_{n}_window_per_fire"] for n in (*_FAMILIES, "total")},
        "rc_sets": sorted({tuple(d["rc_set"]) for d in result["doors"]}),
        "door_rows": [
            {"op": d["op"], "event": d["event"], "form": d["form"], "total_tree": d["total_tree"],
             "conhost_tree": d["conhost_tree"], "rc_set": d["rc_set"]}
            for d in result["doors"]
        ],
    }


def _leg(census, hooks_path: Path, fires: int, probe: Optional[WarmProbe], idle_rate: Optional[float]) -> dict:
    """Run the census over `hooks_path`; summarize with per-fire CPU and warm bytes and the per-event grouping."""
    records: list[dict] = []
    if probe is not None:
        instrument_fire(census, probe, records)
    doors = census.select_doors(hooks_path)
    summary = summarize(census.run_census(fires, hooks_path, census.PLUGIN_ROOT))
    if records:
        summary["door_rows"] = attach_fires(summary["door_rows"], doors, records, fires, idle_rate)
        summary["per_event"] = group_per_event(summary["door_rows"])
    return summary


def measure(census, hooks_json: Path, fires: int, image: Optional[Path], rewrite: bool, scratch: Path,
            probe: Optional[WarmProbe] = None, idle_rate: Optional[float] = None) -> dict:
    """One census run: the source hooks unrewritten, or rewritten to direct exec of the staged image."""
    if not rewrite:
        return {"label": "unrewritten", "image": None, **_leg(census, hooks_json, fires, probe, idle_rate)}
    assert image is not None
    run_dir = scratch / f"census-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    try:
        staged = stage_image(image, run_dir)
        doc = json.loads(hooks_json.read_text(encoding="utf-8"))
        rewritten, count = rewrite_hooks(doc, str(staged))
        if count == 0:
            raise CensusRefusal(f"--hooks-json {hooks_json}: no door registration to rewrite")
        scratch_hooks = run_dir / "hooks.json"
        scratch_hooks.write_text(json.dumps(rewritten, indent=2), encoding="utf-8")
        return {"label": "rewritten", "image": image.name, "staged_as": staged.name,
                "doors_rewritten": count, **_leg(census, scratch_hooks, fires, probe, idle_rate)}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def run(census_path: Optional[Path], hooks_json: Optional[Path], image: Optional[Path], fires: int,
        out: Optional[Path], rewrite: bool = True, with_unrewritten: bool = False) -> dict:
    """Validate arguments, run the census leg(s), write `out`, and return the result document."""
    if census_path is None:
        raise CensusRefusal("--census is required: the path to DoE's coordinator/tests/door_spawn_census.py")
    if hooks_json is None:
        raise CensusRefusal("--hooks-json is required: the source hooks.json whose door registrations are measured")
    for flag, path in (("--census", census_path), ("--hooks-json", hooks_json)):
        if not path.is_file():
            raise CensusRefusal(f"{flag} {path}: no such file")
    if fires < 1:
        raise CensusRefusal("--fires must be >= 1")
    if rewrite:
        image = image or default_image()
        if not image.is_file():
            raise CensusRefusal(f"--image {image}: no such file")
    census = load_census(census_path)
    scratch = REPO_ROOT / "scratch" / "hook-run-direct-exec-census"
    scratch.mkdir(parents=True, exist_ok=True)
    inventory = warm_inventory(find_warm_servers())
    probe = WarmProbe(inventory) if inventory["servers"] else None
    idle = probe.idle() if probe is not None else None
    idle_rate = idle["write_bytes_per_s"] if idle else None
    runs = []
    if with_unrewritten or not rewrite:
        runs.append(measure(census, hooks_json, fires, None, False, scratch, probe, idle_rate))
    if rewrite:
        runs.append(measure(census, hooks_json, fires, image, True, scratch, probe, idle_rate))
    doc = {
        "schema": "hook-run-direct-exec-census/v2",
        "census": str(census_path),
        "hooks_json": str(hooks_json),
        "fires": fires,
        "per_fire_total": {r["label"]: r["per_fire_total"] for r in runs},
        "warm": {"inventory": inventory, "scope": probe.scope if probe else "none",
                 "service_dirs": [str(d) for d in probe.dirs] if probe else [],
                 "idle_write_bytes_per_s": idle_rate,
                 "idle_files": collapse_files(idle["files"]) if idle else {}},
        "runs": runs,
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry: exit 0 with the per-label per-fire totals on stdout (the full document goes to `--out`);
    a typed refusal exits 2 on stderr."""
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--census", type=Path, default=None)
    ap.add_argument("--hooks-json", type=Path, default=None)
    ap.add_argument("--image", type=Path, default=None)
    ap.add_argument("--fires", type=int, default=DEFAULT_FIRES)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-rewrite", action="store_true", help="measure the source hooks.json unrewritten only")
    ap.add_argument("--rewritten-only", action="store_true", help="measure the rewritten leg only")
    ap.add_argument("--with-unrewritten", action="store_true", help="accepted for compatibility; the default")
    args = ap.parse_args(argv)
    default_census, default_hooks = (None, None)
    if args.census is None or args.hooks_json is None:
        default_census, default_hooks = default_doe_paths()
    try:
        doc = run(args.census or default_census, args.hooks_json or default_hooks, args.image, args.fires,
                  args.out, rewrite=not args.no_rewrite, with_unrewritten=not args.rewritten_only)
    except CensusRefusal as exc:
        sys.stderr.write(f"hook_run_direct_exec_census: {exc}\n")
        return 2
    print(json.dumps(doc["per_fire_total"], indent=2))
    warm = doc["warm"]
    print(f"warm: scope={warm['scope']} elected={warm['inventory']['elected_pids']} "
          f"orphans={warm['inventory']['orphan_pids']} duplicate_roots={warm['inventory']['duplicate_roots']} "
          f"idle_write_bytes_per_s={warm['idle_write_bytes_per_s']}")
    for r in doc["runs"]:
        if "per_event" in r:
            print(render_per_event(r["label"], r["per_event"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
