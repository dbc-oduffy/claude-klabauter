"""landing_integrity: parse arm, run-base resolver order, no spawn."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.win_portability import no_console_creationflags

A, B, C = "a" * 40, "b" * 40, "c" * 40


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")


def test_parse_arm(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _w(tmp_path, "bad.py", 'x = "a\nb"\n')
    _w(tmp_path, "ok.py", "x = 1\n")
    _w(tmp_path, "notes.txt", "def (\n")
    out = li.unparseable_py(tmp_path, ["bad.py", "ok.py", "notes.txt", "gone.py"])
    assert [(f.path, f.reason) for f in out] == [("bad.py", li.PY_UNPARSEABLE)]
    assert out[0].detail.startswith("line ")


def test_null_byte_source_is_a_finding(tmp_path):
    (tmp_path / "n.py").write_bytes(b"x = 1\x00\n")
    assert [f.reason for f in li.unparseable_py(tmp_path, ["n.py"])] == [li.PY_UNPARSEABLE]


def test_run_base_resolver_order(tmp_path):
    assert li.run_base_for(tmp_path, "r1", {}) is None
    _w(tmp_path, "scratch/warp/r1/run-base", C + "\n")
    assert li.run_base_for(tmp_path, "r1", {}) == C
    _w(tmp_path, "scratch/warp/r1/submission.json", json.dumps({"run_base_sha": B}))
    assert li.run_base_for(tmp_path, "r1", {}) == B
    assert li.run_base_for(tmp_path, "r1", {"run_base_sha": A}) == A
    assert li.run_base_for(tmp_path, "r1", {"run_base_sha": "zz"}) == B
    assert li.run_base_for(tmp_path, None, {}) is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_recorded_base_branch_does_not_import_dispatch_emit_op(tmp_path):
    _w(tmp_path, "scratch/warp/r1/run-base", C + "\n")
    repo_root = Path(__file__).resolve().parents[4]
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from pathlib import Path\n"
        "from coordinator_core.ops.dispatch_emit import landing_integrity as li\n"
        f"got = li.run_base_for(Path({str(tmp_path)!r}), 'r1', {{}})\n"
        f"assert got == {C!r}, got\n"
        "assert 'coordinator_core.ops.dispatch_emit.op' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr

