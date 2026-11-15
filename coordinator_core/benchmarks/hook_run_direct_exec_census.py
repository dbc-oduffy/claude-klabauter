"""Per-fire process census of the direct-exec `hook-run` door image, over every door registration.

Stages the door image as `<repo>/scratch/<run>/hook-run[.exe]` (the door routes by its own module
basename, so the staged name is what makes it dial `hooks.*`), rewrites every door registration of the
source hooks.json to exec form `{command: <staged>, args: ["--stdin-hook", ("--advisory"), "hooks.<op>"]}`
in a scratch hooks.json, and runs DoE's `door_spawn_census.py` over it in-process. DoE's census selects
only `door_launch.py` and shell-form registrations, so `select_doors` is extended on the loaded module
(never on DoE's file) to also select the staged exec form.

Bulk measurement, not an engine op: process counts per fire by family (bash, conhost, python, hook-run),
tree-attributed. The census is a lower bound: a child that exits between polls is never seen.

Run: `python coordinator_core/benchmarks/hook_run_direct_exec_census.py --census PATH --hooks-json PATH
[--image PATH] [--fires N] [--out PATH] [--no-rewrite] [--with-unrewritten]`. `--census` and `--hooks-json`
are required; a missing value is a typed refusal (exit 2) naming the flag.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin

REPO_ROOT = Path(__file__).resolve().parents[2]
DOOR_DIR = REPO_ROOT / "coordinator_core" / "warm" / "door"
DEFAULT_FIRES = 5
DEFAULT_OUT = (REPO_ROOT / "docs" / "research" / "hook-run-direct-exec" / "2026-11-15-spawn-census"
               / "outputs" / "baseline.json")
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


def load_census(path: Path):
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


def measure(census, hooks_json: Path, fires: int, image: Optional[Path], rewrite: bool, scratch: Path) -> dict:
    """One census run: the source hooks unrewritten, or rewritten to direct exec of the staged image."""
    plugin_root = census.PLUGIN_ROOT
    if not rewrite:
        return {"label": "unrewritten", "image": None,
                **summarize(census.run_census(fires, hooks_json, plugin_root))}
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
        result = census.run_census(fires, scratch_hooks, plugin_root)
        return {"label": "rewritten", "image": image.name, "staged_as": staged.name,
                "doors_rewritten": count, **summarize(result)}
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
    runs = []
    if with_unrewritten or not rewrite:
        runs.append(measure(census, hooks_json, fires, None, False, scratch))
    if rewrite:
        runs.append(measure(census, hooks_json, fires, image, True, scratch))
    doc = {
        "schema": "hook-run-direct-exec-census/v1",
        "census": str(census_path),
        "hooks_json": str(hooks_json),
        "fires": fires,
        "per_fire_total": {r["label"]: r["per_fire_total"] for r in runs},
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
    ap.add_argument("--no-rewrite", action="store_true", help="measure the source hooks.json unrewritten")
    ap.add_argument("--with-unrewritten", action="store_true", help="also measure the source hooks.json unrewritten")
    args = ap.parse_args(argv)
    try:
        doc = run(args.census, args.hooks_json, args.image, args.fires, args.out,
                  rewrite=not args.no_rewrite, with_unrewritten=args.with_unrewritten)
    except CensusRefusal as exc:
        sys.stderr.write(f"hook_run_direct_exec_census: {exc}\n")
        return 2
    print(json.dumps(doc["per_fire_total"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
