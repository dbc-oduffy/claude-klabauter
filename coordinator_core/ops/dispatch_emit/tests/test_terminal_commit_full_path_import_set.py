"""
coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_full_path_import_set --
the plan-run ``dispatch.terminal_commit`` full path loads no module it only needs for
a light helper.

Purpose: in a fresh interpreter, after the benchmark's own plan-run fixture runs end to end
(``build_fixture`` + ``_prepare_close`` + ``_run_full_path``), the modules below are absent from
``sys.modules``. Each was once loaded for one value: the recorded run base, an evidence sidecar
name, a mode predicate. The probe pins the opt-in policy as the suite does.

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
    "coordinator_core.ops.dispatch_emit.roadmap_fanout_commit",
    "coordinator_core.ops.dispatch_emit.inventory_mint",
    "coordinator_core.ops.review_freeze_diff",
    "coordinator_core.ops.review_partition_slices",
)

_PROBE = """
import json, sys
from pathlib import Path
from coordinator_core.benchmarks import terminal_commit_full_path as drv
repo, script = drv.build_fixture(Path(sys.argv[1]))
drv._prepare_close(repo)
# The conftest pin is a monkeypatch the child interpreter never sees.
from coordinator_core.ops.dispatch_emit import process_artifact_policy as _pap
_pap.hosts_coordinator_state = lambda root: True
from coordinator_core.ops.dispatch_emit.tests import _legacy_terminal_commit_run as legacy
out = legacy.run(repo, script)
ok = out.get("review_stamp") == "minted" and out.get("plan_status") == "implemented"
print("RESULT=" + json.dumps({"ok": ok, "loaded": [m for m in json.loads(sys.argv[2]) if m in sys.modules]}))
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
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


_BRANCH_PROBE = """
import json, sys
from pathlib import Path
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest
root = Path(sys.argv[1])
plan = root / "plan.md"
plan.write_text("---\\ntitle: x\\n---\\n", encoding="utf-8")
rows = tc._source_rows_by_plan(root, "plan.md", ["C1"])
ent = tc._entangled_chunks(root, CommitRequest(chunks=()), set(), {})
print("RESULT=" + json.dumps({
    "rows": {k: sorted(v) for k, v in rows.items()}, "ent": ent,
    "loaded": [m for m in json.loads(sys.argv[2]) if m in sys.modules],
}))
"""

_BRANCH_ABSENT = (
    "coordinator_core.ops.dispatch_emit.inventory_mint",
    "coordinator_core.ops.review_freeze_diff",
    "coordinator_core.ops.review_partition_slices",
    "coordinator_core.authoring_leaks.import_closure",
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_branch_gated_imports_stay_behind_their_branch(tmp_path):
    """A plan-mode marker never reaches inventory_mint; a chunk set with no stranded file never
    reaches the import-closure walk."""
    result = subprocess.run(
        [sys.executable, "-c", _BRANCH_PROBE, str(tmp_path), json.dumps(_BRANCH_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    got = json.loads(line[len("RESULT="):])
    assert got["rows"] == {"plan.md": ["C1"]}
    assert got["ent"] == {}
    assert got["loaded"] == []


_PREP_PROBE = """
import json, sys
from coordinator_core.ops import review_prep as rp
before = [m for m in json.loads(sys.argv[1]) if m in sys.modules]
lazy = rp.freeze_diff
from coordinator_core.ops.review_freeze_diff import freeze_diff
print("RESULT=" + json.dumps({"before": before, "lazy_is_real": lazy is freeze_diff}))
"""

_PREP_ABSENT = (
    "coordinator_core.ops.review_freeze_diff",
    "coordinator_core.ops.review_partition_slices",
    "coordinator_core.ops.review_stamp",
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_importing_review_prep_does_not_load_the_freeze_closure():
    """`_reconcile_prep` needs only `stored_reply_for_run`; the freeze/partition/product-files
    callables load on first use and a patched module global still wins."""
    result = subprocess.run(
        [sys.executable, "-c", _PREP_PROBE, json.dumps(_PREP_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    got = json.loads(line[len("RESULT="):])
    assert got["before"] == []
    assert got["lazy_is_real"] is True


def test_review_prep_lazy_names_resolve_to_their_originals():
    from coordinator_core.ops import review_freeze_diff, review_partition_slices, review_prep, review_stamp

    assert review_prep.freeze_diff is review_freeze_diff.freeze_diff
    assert review_prep.partition_slices is review_partition_slices.partition_slices
    assert review_prep.diff_product_files is review_stamp.product_files


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
    from coordinator_core.ops.dispatch_emit import halted_record_request
    from coordinator_core.ops.dispatch_emit import roadmap_fanout_commit as f
    from coordinator_core.ops.dispatch_emit import terminal_commit as tc

    assert tc._is_halted_record_call(params) == halted_record_request.is_halted_record_call(params)
    assert tc._is_fanout_record_call(params) == f.is_fanout_record_call(params)
    assert tc._is_grind_call(params) == g.is_grind_call(params)
