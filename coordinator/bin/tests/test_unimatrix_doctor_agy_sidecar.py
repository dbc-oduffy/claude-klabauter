"""Pins for unimatrix-doctor's agy sidecar never-up check, driven through main([]) over tmp homes."""
import io
import json
import subprocess
import sys

import pytest

from .test_unimatrix_doctor import box  # noqa: F401  (fixture)

pytestmark = pytest.mark.complete

ID = "unimatrix-agy-leg"


def _cfg(tmp_path, *, declare=True, enabled=True, command=None, script=None):
    cfg = tmp_path / "agy-config"
    if declare:
        d = cfg / "sidecars" / ID
        d.mkdir(parents=True)
        (d / "sidecar.json").write_text(json.dumps({"command": command or sys.executable, "args": [script or str(tmp_path / "missing.py")]}))
    if enabled is not None:
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "config.json").write_text(json.dumps({"sidecars": {ID: {"enabled": enabled}}}))
    return cfg


def _lines(out):
    return [ln for ln in out.splitlines() if "agy sidecar" in ln or "agy config" in ln]


def test_declared_enabled_sidecar_without_a_trace_log_is_one_problem_naming_path_and_existence(box, tmp_path):
    script = tmp_path / "sidecar.py"
    script.write_text("")
    cfg = _cfg(tmp_path, script=str(script))
    rc, out = box.main(agy_config_dir=cfg)
    (line,) = _lines(out)
    assert rc == 1 and line.startswith("problem: ")
    assert ID in line and str(cfg / "sidecars" / ID / "sidecar.json") in line
    assert f"command {sys.executable} exists=True" in line and f"script {script} exists=True" in line
    assert "agy-sidecars.log absent" in line and "remedy: rerun unimatrix-install.py" in line


def test_missing_command_and_script_are_reported_false(box, tmp_path):
    cfg = _cfg(tmp_path, command=str(tmp_path / "gone-python"))
    (line,) = _lines(box.main(agy_config_dir=cfg)[1])
    assert "exists=False, script" in line and line.rstrip().count("exists=False") == 2


def test_trace_log_present_is_silent(box, tmp_path):
    (box.home / "unimatrix").mkdir(parents=True, exist_ok=True)
    (box.home / "unimatrix" / "agy-sidecars.log").write_text("{}\n")
    assert _lines(box.main(agy_config_dir=_cfg(tmp_path))[1]) == []


@pytest.mark.parametrize("kw", [{"declare": False}, {"enabled": False}, {"enabled": None}])
def test_undeclared_or_disabled_sidecar_is_silent(box, tmp_path, kw):
    assert _lines(box.main(agy_config_dir=_cfg(tmp_path, **kw))[1]) == []


def test_malformed_declaration_and_malformed_config_each_name_their_file(box, tmp_path):
    cfg = _cfg(tmp_path)
    decl = cfg / "sidecars" / ID / "sidecar.json"
    decl.write_text("{nope")
    (line,) = _lines(box.main(agy_config_dir=cfg)[1])
    assert str(decl) in line and "unreadable" in line
    decl.write_text("{}")
    (cfg / "config.json").write_text("[1")
    (line,) = _lines(box.main(agy_config_dir=cfg)[1])
    assert str(cfg / "config.json") in line and "unreadable" in line


def test_the_check_spawns_nothing(box, tmp_path):
    box.net.cmds.clear()
    box.main(agy_config_dir=_cfg(tmp_path))
    assert not any(ID in " ".join(map(str, c)) or "agy" in str(c[0]) for c in box.net.cmds)


def test_missing_settings_home_is_silent_not_a_crash(box, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME")
    assert _lines(box.main(agy_config_dir=cfg)[1]) == []
