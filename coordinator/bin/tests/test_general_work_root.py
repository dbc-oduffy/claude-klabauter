"""general-work-root CLI: import-inert, same verbs and exit codes, resolves `_general_work` from the mirror sibling."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN / "general-work-root.py"

_STUB = '''\
from pathlib import Path

KEY = "coordinator.general_work_root"


class RootRefused(Exception):
    def __init__(self, root, conflict):
        self.root = root
        self.conflict = conflict


ROOT = Path(r"__ROOT__")
MODE = "__MODE__"


def resolve_root():
    if MODE == "refused":
        raise RootRefused("/r", "/c")
    return ROOT


def ensure_root():
    ROOT.mkdir(parents=True, exist_ok=True)
    return resolve_root()
'''


def _mirror(tmp_path: Path, monkeypatch, mode: str = "ok", with_seam: bool = True):
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(_SCRIPT, bin_dir / "general-work-root.py")
    if with_seam:
        scripts = tmp_path / "coordinator" / "hooks" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "_general_work.py").write_text(
            _STUB.replace("__ROOT__", str(tmp_path / "gw")).replace("__MODE__", mode), encoding="utf-8"
        )
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "_general_work", raising=False)
    spec = importlib.util.spec_from_file_location("gwr_mirror", bin_dir / "general-work-root.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import_does_not_touch_sys_path_or_load_the_implementation(tmp_path, monkeypatch):
    before = list(sys.path)
    monkeypatch.delitem(sys.modules, "_general_work", raising=False)
    spec = importlib.util.spec_from_file_location("gwr_inert", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert sys.path == before and "_general_work" not in sys.modules


def test_path_verb_prints_root_without_creating_it(tmp_path, monkeypatch, capsys):
    mod = _mirror(tmp_path, monkeypatch)
    assert mod.main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "gw")
    assert not (tmp_path / "gw").exists()


def test_ensure_json_creates_root_and_reports_shape(tmp_path, monkeypatch, capsys):
    mod = _mirror(tmp_path, monkeypatch)
    assert mod.main(["ensure", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep == {"root": str(tmp_path / "gw"), "exists": True, "git": False}


def test_refused_root_exits_3_naming_key_and_conflict(tmp_path, monkeypatch, capsys):
    mod = _mirror(tmp_path, monkeypatch, mode="refused")
    assert mod.main(["path"]) == 3
    cap = capsys.readouterr()
    assert "coordinator.general_work_root" in cap.err and cap.out.strip() == "/c"


def test_unresolvable_implementation_exits_2(tmp_path, monkeypatch, capsys):
    mod = _mirror(tmp_path, monkeypatch, with_seam=False)
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert mod.main(["path"]) == 2
    assert "unresolvable" in capsys.readouterr().err


def test_registered_coordinator_claude_root_supplies_the_implementation(tmp_path, monkeypatch, capsys):
    mod = _mirror(tmp_path / "mirror", monkeypatch, with_seam=False)
    coord = tmp_path / "coord"
    scripts = coord / "hooks" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "_general_work.py").write_text(
        _STUB.replace("__ROOT__", str(tmp_path / "gw2")).replace("__MODE__", "ok"), encoding="utf-8"
    )
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(coord) if key == "repos.coordinator_claude" else None)
    assert mod.main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "gw2")


def test_general_work_ensure_drives_the_cli(tmp_path, monkeypatch):
    from coordinator_core.install.general_work_ensure import ensure_general_work_root

    _mirror(tmp_path, monkeypatch)
    outcome = ensure_general_work_root(tmp_path)
    assert outcome.status == "ensured" and outcome.root == str(tmp_path / "gw")


def test_refusal_through_general_work_ensure(tmp_path, monkeypatch):
    from coordinator_core.install.general_work_ensure import ensure_general_work_root

    _mirror(tmp_path, monkeypatch, mode="refused")
    assert ensure_general_work_root(tmp_path).status == "refused"


@pytest.mark.parametrize("name", ["general-work-root.py", "general-work-root.cmd"])
def test_entrypoint_files_ship_in_bin(name):
    assert (_BIN / name).is_file()
