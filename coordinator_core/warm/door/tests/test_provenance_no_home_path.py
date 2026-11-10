"""A written door provenance never carries the home directory or username."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.warm.door import build


def _write(tmp_path: Path, python_bin: Path) -> dict:
    exe = tmp_path / "door.exe"
    exe.write_bytes(b"fake image")
    engine_root = tmp_path / "engine"
    (engine_root / "coordinator_core").mkdir(parents=True)
    side = build.write_provenance(
        exe, "gcc", "gcc", engine_root, image_sha256="0" * 64, engine_stamp="stamp",
        rendered_source_sha256="1" * 64, python_bin=python_bin,
    )
    return json.loads(Path(side).read_text(encoding="utf-8"))


def test_home_interpreter_is_recorded_home_relative(tmp_path, monkeypatch):
    home = tmp_path / "operatorname"
    interp = home / "AppData" / "Python313" / "python.exe"
    interp.parent.mkdir(parents=True)
    interp.write_bytes(b"")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    out = tmp_path / "out"
    out.mkdir()
    proof = _write(out, interp)
    recorded = proof["python_bin"]
    assert recorded == "~/AppData/Python313/python.exe"
    assert "operatorname" not in json.dumps(proof)
    assert build.expand_python_bin(recorded) == interp.resolve()


def test_interpreter_outside_home_stays_absolute(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    interp = tmp_path / "opt" / "python.exe"
    interp.parent.mkdir()
    interp.write_bytes(b"")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert build.portable_python_bin(interp) == str(interp.resolve())


def test_tilde_record_with_unresolved_home_raises_typed_error(monkeypatch):
    import pytest

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("~")))
    with pytest.raises(build.HomeDirectoryUnresolvedError, match="USERPROFILE|HOME"):
        build.expand_python_bin("~/AppData/Python313/python.exe")


def test_tilde_record_with_resolved_home_still_expands(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert build.expand_python_bin("~/a/python.exe") == tmp_path / "a" / "python.exe"


def test_relative_recording_is_not_expanded_to_absolute():
    assert not build.expand_python_bin("python.exe").is_absolute()
