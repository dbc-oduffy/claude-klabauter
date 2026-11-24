"""publish's end-of-run block runs the Unimatrix inbound gate and a nonzero gate exit fails the run."""
from __future__ import annotations

import ast
import importlib.util
import io
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish():
    spec = importlib.util.spec_from_file_location("publish_inbound_gate_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish()


def _stub_gate(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "unimatrix-inbound-gate.py"
    path.write_text(body, encoding="utf-8")
    return path


def _run(gate: Path, **kw):
    out, err = io.StringIO(), io.StringIO()
    ok = publish.dispatch_end_of_run_unimatrix_inbound_gate(gate_path=gate, out=out, err=err, **kw)
    return ok, out.getvalue(), err.getvalue()


def test_the_default_gate_path_is_the_tracked_sibling_script():
    assert publish._UNIMATRIX_INBOUND_GATE_PATH == _BIN_DIR / "unimatrix-inbound-gate.py"
    assert publish._UNIMATRIX_INBOUND_GATE_PATH.is_file()


def test_every_box_arriving_passes_and_echoes_the_per_box_lines(tmp_path):
    gate = _stub_gate(tmp_path, "print('mini: arrived')\nprint('example-fleet: queued-then-delivered')\n")
    ok, out, err = _run(gate)
    assert ok is True
    assert "[inbound-gate] mini: arrived" in out
    assert err == ""


def test_a_nonzero_gate_exit_fails_and_names_the_missing_box(tmp_path):
    gate = _stub_gate(tmp_path, "import sys\nprint('mini: missing (hop: ssh)')\nsys.exit(1)\n")
    ok, out, err = _run(gate)
    assert ok is False
    assert "mini: missing (hop: ssh)" in out
    assert "exit 1" in err


def test_a_missing_gate_script_fails_closed(tmp_path):
    ok, _, err = _run(tmp_path / "absent.py")
    assert ok is False
    assert "not found" in err


def test_a_hung_gate_is_killed_and_fails(tmp_path):
    gate = _stub_gate(tmp_path, "import time\ntime.sleep(60)\n")
    ok, _, err = _run(gate, timeout_s=0.5)
    assert ok is False
    assert "killed" in err


def _run_round_tree():
    tree = ast.parse((_BIN_DIR / "publish.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_round")


def test_run_round_calls_the_gate_beside_the_provenance_gate_with_default_arguments():
    fn = _run_round_tree()
    calls = {
        n.func.id: n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id in ("dispatch_end_of_run_plugin_provenance_gate",
                          "dispatch_end_of_run_unimatrix_inbound_gate")
    }
    assert set(calls) == {"dispatch_end_of_run_plugin_provenance_gate",
                          "dispatch_end_of_run_unimatrix_inbound_gate"}
    gate_call = calls["dispatch_end_of_run_unimatrix_inbound_gate"]
    assert not gate_call.args and not gate_call.keywords


def test_the_gate_result_is_conjoined_into_gates_ok():
    fn = _run_round_tree()
    gates_ok = next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "gates_ok" for t in n.targets)
        and isinstance(n.value, ast.BoolOp)
    )
    names = {v.id for v in gates_ok.value.values if isinstance(v, ast.Name)}
    assert "unimatrix_inbound_ok" in names
