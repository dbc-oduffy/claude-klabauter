"""A caller-widened referencer test is declared on the leg but the seam never runs it (real git)."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import compose_script
from .conftest import REVIEW_KW
from .test_seam_leg_executes_declared_paths import _test_row_waves
from .test_seam_wave_boundary import ABSENT_EDGES, CLEAN_REPLY, P2, _NODE, _row, _run_script

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@_NODE
def test_a_caller_widened_referencer_test_is_declared_but_the_seam_never_runs_it(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (repo / "tests" / "test_ref.py").write_text("from a import x\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    script = compose_script(
        _test_row_waves()[:1] + [[_row("P2-C1", ["b.py"], P2, reads=["a.py"])]],
        name="n", description="d", plan_path="state/mise-inventory/x.spine.md",
        expected_branch="work/x", run_base_sha="a" * 40, seam=ABSENT_EDGES,
        repo_root=repo, **REVIEW_KW,
    )
    declared = script.split("declared: [", 1)[1].split("]", 1)[0]
    assert "'tests/test_ref.py'" in declared
    out = _run_script(tmp_path, script, CLEAN_REPLY)
    listed = json.loads(out["seamParams"][0].split("Tests: ", 1)[1].split(". Step 2", 1)[0])
    assert "tests/test_a.py" in listed
    assert "tests/test_ref.py" not in listed
