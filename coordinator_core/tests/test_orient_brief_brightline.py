"""Brightline gate for `coordinator_core.orient_brief` (DR-344): 500ms process time, child-inclusive.

Every figure is `single_invocation_tree_process_time`, so spawned children count. Never measured
through `coordinator-invoke`: the door dials an already-running server whose work falls outside the
job object, which is an unconditional PASS (`process_time.py`, "A SIXTH TRAP"). The warm and cold
legs invoke `python -m coordinator_core.orient_brief`; the `orient-assemble` CLI route is the
informational leg and never blocks.
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time
from coordinator_core.machine_resolver import merged_flat_registry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENGINE_ROOT = Path(__file__).resolve().parents[2]


def _trees() -> dict:
    flat = merged_flat_registry()
    trees = {"claude-klabauter": _ENGINE_ROOT}
    doe = flat.get("repos.content_root")
    if doe:
        trees["coordinator-content-repo"] = Path(doe)
    return trees


_TREES = _trees()

BAR_MS = 500.0
WARM_STOP_MS = 200.0
WARM_PROCS_CEILING = 2
COLD_PROCS_CEILING = 4
WARM_CALLS = 8
REPEATS = 3

# Import, one untimed warm-up call, then N timed calls. N=0 is the import-plus-warm-up baseline.
_WARM_CHILD = """
import sys
sys.path.insert(0, {engine_root!r})
from coordinator_core import orient_brief
try:
    orient_brief.brief("day")
    for _ in range(int(sys.argv[1])):
        orient_brief.brief("day")
except NotImplementedError as exc:
    print("NOT_IMPLEMENTED " + str(exc))
"""


@pytest.fixture(scope="module")
def scratch():
    with tempfile.TemporaryDirectory(prefix="orient-brightline-") as d:
        yield Path(d)


@pytest.fixture(params=sorted(_TREES))
def tree(request) -> Path:
    path = _TREES[request.param]
    if not (path / ".git").exists():
        pytest.skip(f"{request.param} tree not present at {path}")
    return path


def _measure(cmd, cwd: Path, out: Path) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    stdout, stderr = out.with_suffix(".out"), out.with_suffix(".err")
    res = single_invocation_tree_process_time(
        cmd, cwd=str(cwd), stdout_path=str(stdout), stderr_path=str(stderr)
    )
    res["text"] = stdout.read_text(errors="replace") + stderr.read_text(errors="replace")
    return res


def test_warm_op_is_under_the_bar(tree, scratch):
    child = scratch / "warm_child.py"
    child.write_text(_WARM_CHILD.format(engine_root=str(_ENGINE_ROOT)), encoding="utf-8")
    deltas, procs, text = [], [], ""
    for i in range(REPEATS):
        base = _measure([sys.executable, str(child), "0"], tree, scratch / f"b{i}")
        timed = _measure([sys.executable, str(child), str(WARM_CALLS)], tree, scratch / f"t{i}")
        text += base["text"] + timed["text"]
        deltas.append((timed["process_time_ms"] - base["process_time_ms"]) / WARM_CALLS)
        procs.append((timed["procs"] - base["procs"]) / WARM_CALLS)
    warm_ms, warm_procs = statistics.mean(deltas), statistics.mean(procs)
    print(f"warm op {tree.name}: {warm_ms:.1f}ms/call, {warm_procs:.2f} procs/call")

    assert warm_ms <= WARM_STOP_MS, f"warm {warm_ms:.1f}ms exceeds the {WARM_STOP_MS:.0f}ms stop rule"
    assert warm_ms < BAR_MS
    if "NOT_IMPLEMENTED" in text:
        print("count assertion skipped: a family raised NotImplementedError")
        return
    assert warm_procs <= WARM_PROCS_CEILING, f"warm {warm_procs:.2f} procs/call"


def test_cold_op_is_under_the_bar(tree, scratch):
    cmd = [sys.executable, "-m", "coordinator_core.orient_brief", "brief", "--cadence", "day"]
    # cwd is the tree under measurement; PYTHONPATH supplies this engine to `-m`.
    res = single_invocation_tree_process_time(
        cmd,
        env={**os.environ, "PYTHONPATH": str(_ENGINE_ROOT)},
        cwd=str(tree),
        stdout_path=str(scratch / "cold.out"),
        stderr_path=str(scratch / "cold.err"),
    )
    text = (scratch / "cold.out").read_text(errors="replace") + (
        scratch / "cold.err"
    ).read_text(errors="replace")
    print(f"cold op {tree.name}: {res['process_time_ms']:.1f}ms, {res['procs']} procs")

    assert res["process_time_ms"] < BAR_MS, f"cold {res['process_time_ms']:.1f}ms"
    if "NotImplementedError" in text:
        print("count assertion skipped: a family raised NotImplementedError")
        return
    assert res["procs"] <= COLD_PROCS_CEILING, f"cold {res['procs']} procs"


def test_cold_cli_route_is_informational(tree, scratch):
    """The `orient-assemble` CLI route's cost is recorded, never asserted (bug 44fd2158744a).

    The route pays the trampoline and door-resolution floor ahead of the op, which the op's own
    bar does not govern. Only a clean exit is asserted, so the route cannot rot silently.
    """
    cmd = [
        sys.executable,
        str(_ENGINE_ROOT / "coordinator" / "bin" / "orient-assemble.py"),
        "brief",
        "--cadence",
        "day",
    ]
    res = single_invocation_tree_process_time(
        cmd,
        cwd=str(tree),
        stdout_path=str(scratch / "cli.out"),
        stderr_path=str(scratch / "cli.err"),
    )
    print(f"INFORMATIONAL cold CLI route {tree.name}: {res['process_time_ms']:.1f}ms, {res['procs']} procs")
    assert (scratch / "cli.out").read_text(errors="replace").lstrip().startswith("{")
