"""``emit-dispatch-workflow --inventory --lanes``: flag validation and the printed Workflow lines.

Pins: every lane flag without ``--inventory`` is a usage error (exit 2), ``--part`` requires
``--lanes``, ``--lanes`` refuses ``--out``/``--fire``, and a successful run prints one
``Workflow(...)`` line per emitted script.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import admission, cli as cli_module, op

from .conftest import REVIEW_KW
from .test_op_lanes import RUN, _inventory, _write_master


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    monkeypatch.setattr(op, "_load_review_inputs", lambda route: tuple(REVIEW_KW.values()))
    monkeypatch.setattr(
        admission, "await_admission", lambda *a, **kw: {"verdict": "disabled", "reasons": [], "waited_s": 0}
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--lanes"],
        ["--plan", "p.md", "--lanes"],
        ["--part", "a"],
        ["--lane-count", "2"],
        ["--hot-files", "5"],
        ["--inventory", "i.md", "--part", "a"],
        ["--inventory", "i.md", "--lane-count", "2"],
        ["--inventory", "i.md", "--lanes", "--out", "x.workflow.mjs"],
        ["--inventory", "i.md", "--lanes", "--fire"],
    ],
)
def test_lane_flag_misuse_is_a_usage_error(argv, capsys):
    assert cli_module.main(argv) == cli_module.EXIT_USAGE
    assert "ERROR" in capsys.readouterr().err


def test_lanes_prints_one_workflow_line_per_emitted_script(tmp_path, capsys):
    inv = _write_master(
        tmp_path,
        _inventory([("R1", "pkg/one.py", "-", "pending"), ("R2", "pkg/two.py", "-", "pending")]),
    )
    argv = ["--inventory", str(inv), "--lanes", "--hot-files", "0", "--lane-count", "2", "--repo-root", str(tmp_path)]

    assert cli_module.main(argv) == cli_module.EXIT_OK

    captured = capsys.readouterr()
    reply = json.loads(captured.out)
    assert [p["part"] for p in reply["parts"]] == ["a", "b"]
    lines = [ln for ln in captured.err.splitlines() if ln.strip().startswith("Workflow(")]
    assert len(lines) == 2
    assert all(f"{RUN}-" in ln and ".workflow.mjs" in ln for ln in lines)
