from __future__ import annotations

import ast
from pathlib import Path

import coordinator_core.install.general_work_ensure as mod
from coordinator_core.install.general_work_ensure import ensure_general_work_root

_OK = '''
import json, sys
from pathlib import Path
def main(argv=None):
    assert argv == ["ensure", "--json"]
    log = Path(__file__).with_name("calls.log")
    log.write_text(log.read_text() + "x" if log.exists() else "x")
    print(json.dumps({"root": "R", "exists": True, "git": True}))
    return 0
'''

_REFUSED = '''
import sys
def main(argv=None):
    print("general-work-root: refused: A conflicts with B", file=sys.stderr)
    print("B")
    return 3
'''


def _coord(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "coord" / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "general-work-root.py").write_text(body)
    return tmp_path / "coord"


def test_ensure_called_once_and_root_reported(tmp_path):
    coord = _coord(tmp_path, _OK)
    outcome = ensure_general_work_root(coord)
    assert outcome.status == "ensured" and outcome.root == "R"
    assert (coord / "coordinator" / "bin" / "calls.log").read_text() == "x"


def test_refused_root_is_an_outcome_not_a_crash(tmp_path):
    outcome = ensure_general_work_root(_coord(tmp_path, _REFUSED))
    assert outcome.status == "refused" and "conflicts with B" in outcome.detail


def test_missing_script_and_unresolved_root(tmp_path):
    assert ensure_general_work_root(tmp_path).status == "script_missing"
    assert ensure_general_work_root(None).status == "script_missing"


def test_script_crash_is_failed_outcome(tmp_path):
    coord = _coord(tmp_path, "def main(argv=None):\n    raise RuntimeError('boom')\n")
    outcome = ensure_general_work_root(coord)
    assert outcome.status == "failed" and "boom" in outcome.detail


def test_script_sys_exit_is_failed_outcome(tmp_path):
    coord = _coord(tmp_path, "import sys\ndef main(argv=None):\n    sys.exit(2)\n")
    assert ensure_general_work_root(coord).status == "failed"


def test_this_module_spawns_no_subprocess_and_never_names_git():
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "subprocess"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "subprocess":
                offenders.append(node.module)
        elif isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in {"run", "Popen", "check_call", "check_output"}
                and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"
            ):
                offenders.append(f"subprocess.{f.attr}")
            if any(isinstance(a, ast.Constant) and a.value == "git" for a in node.args):
                offenders.append("call with 'git' arg")
    assert not offenders, offenders
