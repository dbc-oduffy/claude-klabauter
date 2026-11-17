"""A written door provenance never carries the home directory or username."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from coordinator_core.warm.door import build
import pytest


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


def test_home_interpreter_is_recorded_home_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_provenance_records_no_engine_root_key_or_path(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    proof = _write(out, Path(sys.executable))
    assert "engine_root" not in proof
    assert str((out / "engine").resolve()) not in json.dumps(proof)
    assert proof["engine_stamp"] == "stamp"


def test_posix_provenance_records_no_engine_root_key_or_path(tmp_path: Path) -> None:
    from coordinator_core.warm.door import build_posix

    exe = tmp_path / "door"
    exe.write_bytes(b"fake image")
    engine_root = tmp_path / "engine"
    (engine_root / "coordinator_core").mkdir(parents=True)
    side = build_posix.write_provenance(
        exe, "gcc", engine_root, image_sha256="0" * 64, engine_stamp="stamp",
    )
    proof = json.loads(Path(side).read_text(encoding="utf-8"))
    assert "engine_root" not in proof
    assert str(engine_root.resolve()) not in json.dumps(proof)


def test_render_bakes_no_engine_root(tmp_path: Path) -> None:
    source = "int main(void){return 0;}\n"
    assert build._render(source, tmp_path) == source
    assert str(tmp_path.resolve()) not in build._render(source, tmp_path)


def test_interpreter_outside_home_stays_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    interp = tmp_path / "opt" / "python.exe"
    interp.parent.mkdir()
    interp.write_bytes(b"")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert build.portable_python_bin(interp) == str(interp.resolve())


def test_tilde_record_with_unresolved_home_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import pytest

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("~")))
    with pytest.raises(build.HomeDirectoryUnresolvedError, match="USERPROFILE|HOME"):
        build.expand_python_bin("~/AppData/Python313/python.exe")


def test_tilde_record_with_resolved_home_still_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert build.expand_python_bin("~/a/python.exe") == tmp_path / "a" / "python.exe"


def test_relative_recording_is_not_expanded_to_absolute() -> None:
    assert not build.expand_python_bin("python.exe").is_absolute()
