#!/usr/bin/env python3
"""check-project-type — compare a repo's declared project_type with its Unreal Engine markers.

Verdicts: ok | missing | seedable | conflict (fleet adds norepo). `--apply` seeds seedable and
missing repos and never rewrites a declared value. `--fleet` walks the machine-local repos.<slug>
registry through `check-projectragignore-fleet-drift.py::_load_repo_roots`.

Exit codes: 0 = every checked repo is ok (or norepo). 1 = a repo is missing, seedable or conflict
after the run. 2 = usage or environment error.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType

_FLEET_WALK = Path(__file__).resolve().with_name("check-projectragignore-fleet-drift.py")
_CLEAN = ("ok", "norepo")


def _engine() -> ModuleType:
    try:
        import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
        import cc_invoke

        cc_invoke.require_engine_on_path(__file__)
    except (ImportError, RuntimeError, OSError):
        pass
    from coordinator_core import project_type_check

    return project_type_check


def _fleet_roots() -> dict[str, Path]:
    spec = importlib.util.spec_from_file_location("_fleet_drift_walk", _FLEET_WALK)
    if spec is None or spec.loader is None:
        raise OSError(f"cannot load {_FLEET_WALK}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._load_repo_roots()


def _line(res: dict, label: str) -> str:
    text = f"{res['verdict']}  {label}: {res['reason']}"
    if res["applied"]:
        text += f" -> applied, now {res['after']}"
    elif res.get("repair"):
        text += f"\n    run: {res['repair']['command']}"
    elif res.get("hint"):
        text += f"\n    hint: {res['hint']}"
    return text


def _final(res: dict) -> str:
    return res["after"] if res["applied"] else res["verdict"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-project-type",
        description="Compare a repo's declared project_type with its UE markers.",
    )
    parser.add_argument("root", nargs="?", type=Path, help="one repo root (omit only with --fleet)")
    parser.add_argument("--apply", action="store_true", help="seed seedable and missing repos")
    parser.add_argument("--fleet", action="store_true", help="walk every registry repos.<slug> root")
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    args = parser.parse_args(argv)

    if args.fleet == (args.root is not None):
        print("check-project-type: give <root>, or --fleet without a <root>", file=sys.stderr)
        return 2
    try:
        ptc = _engine()
    except ImportError as exc:
        print(f"check-project-type: cannot import coordinator_core: {exc}", file=sys.stderr)
        return 2

    if not args.fleet:
        if not args.root.is_dir():
            print(f"check-project-type: not a directory: {args.root}", file=sys.stderr)
            return 2
        res = ptc.check_and_apply(args.root, args.apply)
        print(json.dumps(res, indent=2) if args.json else _line(res, res["root"]))
        return 0 if _final(res) in _CLEAN else 1

    try:
        roots = _fleet_roots()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"check-project-type: cannot read registry: {exc}", file=sys.stderr)
        return 2

    repos: list[dict] = []
    for slug, root in roots.items():
        if root.is_dir():
            res = ptc.check_and_apply(root, args.apply)
        else:
            res = {"root": str(root), "verdict": "norepo", "reason": "root-not-a-directory",
                   "applied": False, "after": None, "repair": None}
        repos.append({"slug": slug, **res})
    counts = {k: 0 for k in ("ok", "missing", "seedable", "conflict", "norepo")}
    for res in repos:
        counts[res["verdict"]] += 1
    counts["repaired"] = sum(1 for r in repos if r["applied"] and r["after"] == "ok")

    if args.json:
        print(json.dumps({"repos": repos, "counts": counts}, indent=2))
    else:
        for res in repos:
            print(_line(res, res["slug"]))
        print(
            f"{len(repos)} repos, {counts['repaired']} repaired, {counts['conflict']} conflicts"
        )
    return 0 if all(_final(r) in _CLEAN for r in repos) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
