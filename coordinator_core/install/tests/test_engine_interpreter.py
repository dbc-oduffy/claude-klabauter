"""Behavior pins for coordinator_core.install.engine_interpreter (DR-478)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.install import engine_interpreter as ei


def _fake_pip(calls, *, rc=0, populate=True):
    def run(argv, **kwargs):
        calls.append(argv)
        if populate and "--target" in argv:
            target = argv[argv.index("--target") + 1]
            (ei.Path(target) / "yaml").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom" if rc else "")

    return run


def test_site_dir_is_under_given_home_and_defaults_to_settings_home(tmp_path, monkeypatch):
    assert ei.engine_site_dir(tmp_path) == tmp_path / "engine-site"
    monkeypatch.setattr(ei._settings_home, "settings_home", lambda: tmp_path)
    assert ei.engine_site_dir() == tmp_path / "engine-site"


def test_stamp_none_when_absent_and_zero_spawn(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    assert ei.stamp(tmp_path) is None


def test_ensure_installs_with_one_pip_target_spawn_and_stamps(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    out = ei.ensure(sys.executable, ["PyYAML>=6"], tmp_path)
    assert out == tmp_path / "engine-site"
    assert len(calls) == 1
    assert calls[0][:3] == [sys.executable, "-m", "pip"]
    assert calls[0][3:6] == ["install", "--target", str(out)]
    assert calls[0][6:] == ["PyYAML>=6"]
    assert "--break-system-packages" not in calls[0]
    assert (out / "yaml").is_dir()
    assert ei.stamp(tmp_path) == tuple(sys.version_info[:2])


def test_ensure_is_idempotent_with_zero_spawn_when_stamp_matches(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    ei.ensure(sys.executable, ["PyYAML>=6"], tmp_path)
    ei.ensure(sys.executable, ["PyYAML>=6"], tmp_path)
    assert len(calls) == 1


def test_ensure_reprovisions_when_stamp_names_another_interpreter(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    site = ei.engine_site_dir(tmp_path)
    site.mkdir()
    (site / "stale.so").write_text("x")
    (site / ".engine-site-stamp").write_text("2.7\n" + ei._specs_digest(["PyYAML>=6"]) + "\n")
    assert ei.stamp(tmp_path) == (2, 7)
    ei.ensure(sys.executable, ["PyYAML>=6"], tmp_path)
    assert len(calls) == 1
    assert not (site / "stale.so").exists()
    assert ei.stamp(tmp_path) == tuple(sys.version_info[:2])


def test_ensure_reprovisions_when_declared_specs_change(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    ei.ensure(sys.executable, ["PyYAML>=6"], tmp_path)
    ei.ensure(sys.executable, ["PyYAML>=6", "psutil"], tmp_path)
    assert len(calls) == 2


def test_ensure_failure_raises_and_leaves_no_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_pip([], rc=1, populate=False))
    with pytest.raises(RuntimeError, match="engine-site install failed"):
        ei.ensure(sys.executable, ["PyYAML>=6"], tmp_path)
    assert ei.stamp(tmp_path) is None


def test_ensure_probes_a_foreign_interpreter_version_once(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="3.9\n", stderr="")
        return _fake_pip([])(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    ei.ensure(str(tmp_path / "other-python"), ["PyYAML>=6"], tmp_path)
    assert ei.stamp(tmp_path) == (3, 9)


def _stamped(tmp_path, minor, *, with_yaml=True):
    """Write an engine-site stamp for ``minor`` (and a yaml dir unless told not to)."""
    site = ei.engine_site_dir(tmp_path)
    site.mkdir(parents=True)
    (site / ".engine-site-stamp").write_text(f"{minor}\nabc\n")
    if with_yaml:
        (site / "yaml").mkdir()


def _interp(tmp_path, name):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("")
    return str(path)


def test_health_ok_mismatch_missing_yaml_absent_unknown_with_zero_spawn(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    py315 = _interp(tmp_path, "python3.15")
    assert ei.health(py315, tmp_path).status == "missing"
    _stamped(tmp_path, "3.15")
    got = ei.health(py315, tmp_path)
    assert (got.status, got.stamp_minor, got.interp_minor) == ("ok", (3, 15), (3, 15))
    assert got.remedy == "python3 scripts/setup.py"
    assert ei.health(_interp(tmp_path, "python3.13"), tmp_path).status == "stamp-mismatch"
    assert ei.health(_interp(tmp_path, "python.exe"), tmp_path).status == "unknown"


def test_health_yaml_absent_when_stamp_matches_but_dir_gone(tmp_path):
    _stamped(tmp_path, "3.15", with_yaml=False)
    assert ei.health(_interp(tmp_path, "python3.15"), tmp_path).status == "yaml-absent"


def test_health_reads_minor_through_a_symlink_realpath(tmp_path):
    real = _interp(tmp_path, "python3.15")
    link = tmp_path / "python3"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks need privilege here")
    _stamped(tmp_path, "3.13")
    got = ei.health(str(link), tmp_path)
    assert (got.status, got.interp_minor) == ("stamp-mismatch", (3, 15))
