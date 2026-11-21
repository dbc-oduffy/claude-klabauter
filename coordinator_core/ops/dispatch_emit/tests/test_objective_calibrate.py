"""Calibration: a falsifier must be red at base and green at the credited sha, else nothing pins."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import objective_contract as oc
from coordinator_core.ops.dispatch_emit.objective_calibrate import calibrate

pytestmark = [pytest.mark.complete]

RUN = "run-x"

SCRIPT = """#!__PY__
import json, os, sys
path = __TARGET__
ok = False
if os.path.isfile(path):
    ok = "Lens 8" in open(path, encoding="utf-8").read()
print(json.dumps({"axis": "lens8", "kind": "behaviour", "command": "grep", "base": None, "now": None,
                  "threshold": None, "comparator": "bool", "pass": ok, "evidence": path}))
print(json.dumps({"summary": True, "pass": ok, "failing": [] if ok else ["lens8"]}))
sys.exit(0 if ok else 1)
"""


def _script(target: str) -> str:
    return SCRIPT.replace("__PY__", sys.executable).replace("__TARGET__", repr(target))


def _claude_klabauter(tmp_path: Path, target: str, *, base_text: str | None = None) -> Path:
    root = tmp_path / "claude-klabauter"
    (root / "scripts").mkdir(parents=True)
    script = root / "scripts" / "falsify.py"
    script.write_text(_script(target), encoding="utf-8")
    script.chmod(0o755)
    if base_text is not None:
        (root / target).parent.mkdir(parents=True, exist_ok=True)
        (root / target).write_text(base_text, encoding="utf-8")
    run_dir = root / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    obj = oc.Objective(verbatim="v", falsifier_script="scripts/falsify.py", axes=[oc.Axis(id="lens8")],
                       written_at="t")
    (run_dir / oc.OBJECTIVE_FILE).write_text(json.dumps(obj.to_json()), encoding="utf-8")
    return root


def _objective(root: Path) -> dict:
    return json.loads((root / "state" / "warp" / RUN / oc.OBJECTIVE_FILE).read_text(encoding="utf-8"))


def test_green_at_base_on_undone_axis_fails_and_does_not_pin(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md", base_text="Lens 8\n")
    res = calibrate({"run_id": RUN, "at": "sizing", "credited": []}, root)
    assert res["verdict"] == "fail" and res["pinned"] is False
    assert any(p["problem"] == "green at base on an undone axis" for p in res["problems"])
    assert _objective(root)["falsifier_sha256"] is None


def test_unusable_falsifier_output_fails(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md")
    (root / "scripts" / "falsify.py").write_text(f"#!{sys.executable}\nprint('not json')\n", encoding="utf-8")
    res = calibrate({"run_id": RUN, "at": "wave0", "credited": []}, root)
    assert res["verdict"] == "fail" and res["pinned"] is False


def test_bad_sha_and_missing_objective_refuse(tmp_path):
    root = _claude_klabauter(tmp_path, "lens.md")
    res = calibrate({"run_id": RUN, "at": "wave0",
                     "credited": [{"sha": "--output=x", "repo_root": str(root), "axes": ["lens8"]}]}, root)
    assert res["verdict"] == "fail"
    assert calibrate({"run_id": "nope", "at": "wave0"}, root)["calibrated"] is False
