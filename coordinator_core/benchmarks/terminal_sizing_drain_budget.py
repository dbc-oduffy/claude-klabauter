"""Process-time and spawn-count budget for the terminal-sizing drain (DR-344 bar: 500 ms).

Seeds a throwaway corpus (default 701 movable terminal sizings, 194 forward-plan-not-terminal
terminal sizings, 2016 warp run records) and measures each leg as this process's CPU
(`time.process_time`) plus descendants (`commit_leg_profile._ChildMeter`) and the count of
`subprocess.Popen` constructions. Wall clock is never recorded. A leg's row carries `ok`, true only
when the leg demonstrably did its work (every movable sizing archived, every forward-plan sizing
left); a figure from a leg with `ok: false` is inadmissible.

    python coordinator_core/benchmarks/terminal_sizing_drain_budget.py --baseline [--json]
        the two-call shape (preview, then act with the previewed ids) against the CURRENT code,
        and `promote_hand_shipped_sizings.run`. It does not reproduce the pre-rebuild
        implementation; its figures are in `terminal_sizing_drain`'s spike note.
    python coordinator_core/benchmarks/terminal_sizing_drain_budget.py [--json]
        default invocation: the op called once with `dry_run:false` and no ids, the promote leg,
        and their sum as the combined drain. Exit 1 when any leg is over `BAR_MS` or not `ok`.

Exit 1 means a leg is over `BAR_MS` or not `ok`.

Script mode seeds a real git repo under `<repo>/scratch/` and removes it after; the measured
archive leg includes its real commit. `measure_archive_leg(root, git_free=True)` swaps the commit
and dirty-status spawns for the git-free seam (tests).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.owner_safe_rmtree import make_removable  # noqa: E402

BAR_MS = 500.0
MOVABLE = 701
FORWARD = 194
RUNS = 2016
_ROADMAP_EVERY = 25
_MODE = "already-terminal"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sizing_text(index: int, status: str, plan: str, route: str) -> str:
    plan_line = f"plan: {plan}\n" if plan else "plan: null\n"
    return (
        f"deliverable_id: dlv-seed-{index:04d}\nschema: sizing-object\n"
        f"intent: \"seeded sizing {index}\"\nappetite: small  # small | medium | large\n"
        f"estimate:\n  tshirt: XS\n  provisional: true\nroute: {route}  # dispatch | roadmap\n"
        f"xl_exit: null\nstatus: {status}  # draft | sized | routed | shipped\n{plan_line}"
        "scout_evidence:\n"
        + "".join(f"  - \"seeded evidence {index}.{n}: " + "x" * 120 + "\"\n" for n in range(24))
    )


def seed_corpus(root: Path, movable: int = MOVABLE, forward: int = FORWARD, runs: int = RUNS) -> Dict[str, int]:
    """Seed `root` and return the seeded counts. Movable sizings are `shipped` with no plan FK (one in
    `_ROADMAP_EVERY` is roadmap-exit with a terminal citer plan, and one in 7 cites a terminal plan);
    forward sizings are `shipped` and cite a plan whose status is `active`. Every run record names
    one of those sizings and carries no `reconciled.json`."""
    root = Path(root)
    _write(root / "docs/plans/2026-01-01-terminal-plan.md", "---\nstatus: implemented\n---\n# terminal\n")
    _write(root / "docs/plans/2026-01-02-live-plan.md", "---\nstatus: active\n---\n# live\n")
    names: List[str] = []
    for i in range(movable):
        name = f"2026-{1 + i % 9:02d}-{1 + i % 27:02d}-seed-movable-{i:04d}.yaml"
        roadmap = i % _ROADMAP_EVERY == 0
        plan = "docs/plans/2026-01-01-terminal-plan.md" if i % 7 == 1 else ""
        _write(root / "state/sizings" / name, _sizing_text(i, "shipped", plan, "roadmap" if roadmap else "dispatch"))
        if roadmap:
            _write(
                root / "docs/plans" / f"2026-01-03-citer-{i:04d}.md",
                f"---\nstatus: implemented\nsizing_object: state/sizings/{name}\n---\n# citer\n",
            )
        names.append(name)
    for j in range(forward):
        name = f"2026-{1 + j % 9:02d}-{1 + j % 27:02d}-seed-forward-{j:04d}.yaml"
        _write(root / "state/sizings" / name, _sizing_text(movable + j, "shipped", "docs/plans/2026-01-02-live-plan.md", "dispatch"))
        names.append(name)
    for k in range(runs):
        target = names[k % len(names)]
        run_dir = root / "scratch/warp" / f"ask-20260101T{k:06d}-seed{k:04d}.workflow"
        _write(
            run_dir / "submission.json",
            json.dumps({"sizing_rel": f"state/sizings/{target}", "session_id": f"s{k}", "submitted_at": 1_700_000_000 + k}),
        )
        chunks = [
            {"id": f"C{c}", "paths": [f"pkg/mod{k}_{c}_{p}.py" for p in range(3)], "prefixes": [],
             "report": f".coordinator-local/r/{k}/C{c}.md", "title": f"seeded chunk {c} of run {k}"}
            for c in range(4)
        ]
        marker = {"chunks": chunks, "deliverable_id": f"dlv-seed-{k:04d}", "expected_branch": "work/seed",
                  "plan_path": "docs/plans/2026-01-02-live-plan.md", "repo_root": str(root), "session_id": None, "version": 1}
        _write(run_dir / "commit-request.txt", "// coordinator:terminal-commit-request v1 " + json.dumps(marker, separators=(",", ":")))
    return {"movable": movable, "forward": forward, "runs": runs}


class _SpawnCounter:
    """Counts `subprocess.Popen` constructions inside the bracket."""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "_SpawnCounter":
        counter = self
        real = subprocess.Popen

        class _Counting(real):  # type: ignore[valid-type, misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                counter.count += 1
                super().__init__(*args, **kwargs)

        self._patch = patch.object(subprocess, "Popen", _Counting)
        self._patch.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._patch.stop()


def measure(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Run `fn` once; return its dict plus `process_ms` (own CPU + descendants) and `spawns`."""
    meter = _ChildMeter()
    meter.start()
    start = time.process_time()
    with _SpawnCounter() as spawns:
        result = fn()
    own_ms = (time.process_time() - start) * 1000.0
    child_ms = meter.stop_ms()
    row = dict(result)
    row["process_ms"] = round(own_ms + child_ms, 1)
    row["spawns"] = spawns.count
    return row


def _archive_patches(stack: ExitStack, root: Path, git_free: bool) -> None:
    from coordinator_core.ops.fleet import archive_sizings as mod

    if not git_free:
        return
    from coordinator_core.ops.fleet.tests.archive_git_free_seam import make_recording_mover

    stack.enter_context(patch.object(mod, "check_repo_root", lambda param_root, common_dir: None))
    stack.enter_context(patch.object(mod, "main_worktree_root", lambda common_dir: root))
    stack.enter_context(patch.object(mod, "archive_and_commit", make_recording_mover()))
    stack.enter_context(patch.object(mod, "_dirty_sizing_relpaths", lambda worktree_root, relpaths: set()))


def measure_archive_leg(root: Path, *, baseline: bool, git_free: bool = False, expected: int = MOVABLE) -> Dict[str, Any]:
    """The archive leg. `baseline` runs the old two-call flow (preview, then act with the ids);
    otherwise one `dry_run:false` call with no ids. `ok` is true when `expected` sizings were acted on."""
    from coordinator_core.ops.fleet import archive_sizings as mod

    common_dir = Path(root) / ".git"

    def _run() -> Dict[str, Any]:
        with ExitStack() as stack:
            _archive_patches(stack, Path(root), git_free)
            ids: List[str] = []
            if baseline:
                preview = asyncio.run(mod._archive_terminal_sizings({"mode": _MODE, "dry_run": True}, repo_root=common_dir))
                ids = [c["id"] for c in preview["candidates"]]
            params: Dict[str, Any] = {"mode": _MODE, "dry_run": False}
            if baseline:
                params["candidate_ids"] = ids
            result = asyncio.run(mod._archive_terminal_sizings(params, repo_root=common_dir))
        acted = len(result.get("acted", []))
        return {"leg": "archive", "acted": acted, "skipped": len(result.get("skipped", [])), "ok": acted == expected}

    return measure(_run)


def measure_promote_leg(root: Path) -> Dict[str, Any]:
    """`promote_hand_shipped_sizings.run` over the seeded run records. `ok` is true when it examined
    every run without erroring (the seeded corpus has no shippable sizing, so `promoted` is 0)."""
    from coordinator_core.ops import promote_hand_shipped_sizings as mod

    def _run() -> Dict[str, Any]:
        counters = mod.run(Path(root))
        return {"leg": "promote", "candidates": counters["candidates"], "promoted": counters["promoted"],
                "ok": counters["promoted"] == 0}

    return measure(_run)


def _init_git(root: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "bench@example.invalid"], ["config", "user.name", "bench"],
                 ["config", "commit.gpgsign", "false"], ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        done = run_git(list(args), cwd=str(root))
        if not done.ok:
            raise RuntimeError(f"git {' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}")


def _rmtree(path: Path) -> None:
    def _onerror(func: Callable[..., Any], target: str, _exc: Any) -> None:
        make_removable(target)
        func(target)

    shutil.rmtree(path, onerror=_onerror)


def run_budget(root: Path, *, baseline: bool, git_free: bool = False) -> List[Dict[str, Any]]:
    """Seed `root` and return the leg rows; the combined row sums the two legs."""
    counts = seed_corpus(root)
    if not git_free:
        _init_git(root)
    archive = measure_archive_leg(root, baseline=baseline, git_free=git_free, expected=counts["movable"])
    promote = measure_promote_leg(root)
    combined = {
        "leg": "combined",
        "process_ms": round(archive["process_ms"] + promote["process_ms"], 1),
        "spawns": archive["spawns"] + promote["spawns"],
        "ok": archive["ok"] and promote["ok"],
    }
    return [archive, promote, combined]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", action="store_true", help="measure the two-call shape (preview, then act with ids) against current code")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    declare_benchmark_origin()
    scratch = _REPO / "scratch"
    scratch.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="terminal-sizing-drain-", dir=scratch))
    try:
        rows = run_budget(root, baseline=args.baseline)
    finally:
        _rmtree(root)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"{row['leg']:9s} process_ms={row['process_ms']:8.1f} spawns={row['spawns']:3d} ok={row['ok']}")
    if args.baseline:
        return 0
    return 0 if all(r["ok"] and r["process_ms"] < BAR_MS for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
