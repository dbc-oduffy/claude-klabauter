"""The landing's halted-run residue scan costs O(the landing's paths), not O(halted history)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import halted_hold_release as hr
from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUNS = 730
PATHS_PER_RUN = 5
AT_HEAD_RUN = 400
HOLDING_RUN = 17
MAX_SPAWNS = 1
MAX_PROCESS_SECONDS = 0.2


def _git(root: Path, *args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rid(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _fixture(root: Path) -> dict:
    """RUNS halted runs each withholding PATHS_PER_RUN dirty paths; only AT_HEAD_RUN's kept blobs equal HEAD."""
    _git(root, "init", "-q")
    run_ids: list = []
    for i in range(RUNS):
        run_id = _rid(300 + i, f"{i:06x}")
        run_ids.append(run_id)
        paths = [f"src/r{i}/m{j}.py" for j in range(PATHS_PER_RUN)]
        for j, p in enumerate(paths):
            _write(root / p, f"x = {i}, {j}\n")
    _git(root, "add", "src")
    _git(root, "commit", "-q", "-m", "head")
    head_blobs = {}
    for line in _git(root, "ls-tree", "-r", "HEAD").splitlines():
        meta, name = line.split("\t", 1)
        head_blobs[name] = meta.split()[2]
    stash = root.parent / (root.name + "-kept")  # outside the repo tree
    sources = []
    for i in range(RUNS):
        for j in range(PATHS_PER_RUN):
            _write(stash / f"{i}-{j}", f"x = {i + 1000}, {j}\n")
            sources.append(str(stash / f"{i}-{j}"))
    hashed = _git(root, "hash-object", "-w", "--stdin-paths", stdin="\n".join(sources) + "\n").splitlines()
    for i, run_id in enumerate(run_ids):
        paths = [f"src/r{i}/m{j}.py" for j in range(PATHS_PER_RUN)]
        kept = {
            p: head_blobs[p] if i == AT_HEAD_RUN else hashed[i * PATHS_PER_RUN + j]
            for j, p in enumerate(paths)
        }
        sizing = f"state/sizings/s{i}.yaml"
        rec = root / "state/warp" / run_id
        _write(root / sizing, "status: open\n")
        _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
        _write(rec / "halted.json", json.dumps({
            "status": "halted", "sizing_rel": sizing,
            "run_artifacts": {"withheld": {p: "halt-record-only" for p in paths}},
        }))
        _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": kept}}, "plan_gate": None}))
        idx.add_run(root, run_id, paths, sizing_rel=sizing)
        for p in paths:  # dirty: a clean-tree release cannot free the run
            _write(root / p, "# uncommitted\n")
    _write(root / idx.INDEX_DIR / idx.SENTINEL, "")  # steady state: the one-time rebuild is not the landing's cost
    return {"run_ids": run_ids}


def test_landing_residue_scan_is_bounded_at_730_withholding_runs(tmp_path, monkeypatch):
    run_ids = _fixture(tmp_path)["run_ids"]
    assert len(idx.run_ids(tmp_path)) == RUNS
    landed = [
        f"src/r{HOLDING_RUN}/m0.py",
        f"src/r{AT_HEAD_RUN}/m0.py",
        "src/new/unwithheld.py",
    ]
    committing = _rid(5, "ffffff")

    spawns: list = []
    real = git_run.run_git

    def counting(*args, **kwargs):
        spawns.append(args[0] if args else kwargs.get("args"))
        return real(*args, **kwargs)

    monkeypatch.setattr(git_run, "run_git", counting)
    start = time.process_time()
    landing_integrity.landing_invariant_nodes(tmp_path, landed, committing)
    elapsed = time.process_time() - start

    assert len(spawns) <= MAX_SPAWNS, f"{len(spawns)} git spawns: {spawns[:3]}"
    assert elapsed < MAX_PROCESS_SECONDS, f"{elapsed * 1000:.0f}ms process time"
    assert run_ids[AT_HEAD_RUN] not in idx.entries(tmp_path)  # kept at HEAD: retired from the index
    assert run_ids[HOLDING_RUN] in idx.entries(tmp_path)  # still holds: kept


def test_residue_paths_scoped_to_the_landing_names_only_held_landed_paths(tmp_path):
    _fixture(tmp_path)
    landed = [f"src/r{HOLDING_RUN}/m0.py", f"src/r{AT_HEAD_RUN}/m0.py", "src/new/unwithheld.py"]
    out = hr.residue_paths(tmp_path, _rid(5, "ffffff"), paths=landed)
    assert out == frozenset({f"src/r{HOLDING_RUN}/m0.py"})
