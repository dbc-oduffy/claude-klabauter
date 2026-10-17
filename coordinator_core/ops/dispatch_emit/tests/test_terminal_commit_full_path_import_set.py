"""
coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_full_path_import_set --
the plan-run ``dispatch.terminal_commit`` full path loads no module it only needs for
a light helper.

Purpose: in a fresh interpreter, after the benchmark's own plan-run fixture runs end to end
(``build_fixture`` + ``_prepare_close`` + ``_run_full_path``), the modules below are absent from
``sys.modules``. Each was once loaded for one value: the recorded run base, an evidence sidecar
name, a mode predicate.

Negative-spec:
    - Does NOT copy the benchmark driver: it is imported.
    - Does NOT assert modules owned by other rows' files (``commit_v2``, ``archive_stamp``,
      ``plan_status_transition``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[4]

_ABSENT = (
    "coordinator_core.ops.dispatch_emit.op",
    "coordinator_core.ops.plan_tasks_mutate",
    "coordinator_core.ops.dispatch_emit.halted_record_commit",
    "coordinator_core.ops.dispatch_emit.grind_commit_contract",
    "coordinator_core.ops.dispatch_emit.roadmap_fanout_commit",
)

_PROBE = """
import json, sys
from pathlib import Path
from coordinator_core.benchmarks import terminal_commit_full_path as drv
repo, script = drv.build_fixture(Path(sys.argv[1]))
drv._prepare_close(repo)
out = drv._run_full_path(repo, script)
ok = out.get("review_stamp") == "minted" and out.get("plan_status") == "implemented"
print("RESULT=" + json.dumps({"ok": ok, "loaded": [m for m in json.loads(sys.argv[2]) if m in sys.modules]}))
"""


@pytest.mark.spawns_process
def test_plan_run_full_path_does_not_load_the_cut_modules(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path), json.dumps(_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    got = json.loads(line[len("RESULT="):])
    assert got["ok"], "full path did not complete; an import-set reading of a failed run proves nothing"
    assert got["loaded"] == []


def test_mirrored_names_match_their_originals():
    from coordinator_core.ops import plan_tasks_mutate
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    assert tc._EVIDENCE_SIDECAR_SUFFIX == plan_tasks_mutate.EVIDENCE_SIDECAR_SUFFIX


@pytest.mark.parametrize("params", [
    None, {}, {"halted_by": "x"}, {"halted_by": {}}, {"halted_by": {"halted": ""}},
    {"halted_by": {"halted": 3}}, {"halted_by": {"halted": "kind"}},
    {"fanout": {"plans": []}}, {"fanout": {"plans": ["a"]}}, {"fanout": {"plans": ["a", ""]}},
    {"fanout": {"plans": "a"}}, {"fanout": []},
    {"grind": {"rows": []}}, {"grind": {"rows": "x"}}, {"grind": {}}, {"grind": []},
])
def test_mode_predicates_agree_with_their_originals(params):
    from coordinator_core.ops.dispatch_emit import grind_commit_contract as g
    from coordinator_core.ops.dispatch_emit import halted_record_commit as h
    from coordinator_core.ops.dispatch_emit import roadmap_fanout_commit as f
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    assert tc._is_halted_record_call(params) == h.is_halted_record_call(params)
    assert tc._is_fanout_record_call(params) == f.is_fanout_record_call(params)
    assert tc._is_grind_call(params) == g.is_grind_call(params)
