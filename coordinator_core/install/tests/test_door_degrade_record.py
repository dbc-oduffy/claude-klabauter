"""A per-name native-door build failure degrades the name AND leaves a durable
`cold_failed` row carrying the `install-door:` cause prefix."""

from __future__ import annotations

import pytest

from coordinator_core.install import door_install, substrate
from coordinator_core.warm import telemetry


@pytest.fixture
def engine_root(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")
    monkeypatch.setattr(door_install, "launcher_is_installable", lambda *a, **k: True)
    monkeypatch.setattr("coordinator_core.warm.engine_root.is_engine_root", lambda r: True)
    monkeypatch.setattr(telemetry, "svc_dir", lambda engine_root=None: tmp_path / "svc")
    return root


def _raise(exc):
    def _inner(*args, **kwargs):
        raise exc

    return _inner


@pytest.mark.parametrize(
    "exc", [door_install.DoorInstallError("no C compiler"), SystemExit("compile failed")]
)
def test_failed_build_degrades_and_records_a_prefixed_row(tmp_path, engine_root, monkeypatch, exc):
    monkeypatch.setattr(door_install, "install_named_forwarder", _raise(exc))
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    assert substrate._write_native_door_forwarder(
        "some-tool", bin_dst, check_only=False, engine_root=engine_root
    ) is None

    rows = door_install.install_door_degrade_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == telemetry.KIND_COLD_FAILED
    assert rows[0]["cause"].startswith("install-door:some-tool: ")


def test_check_only_records_nothing(tmp_path, engine_root, monkeypatch):
    monkeypatch.setattr(door_install, "install_named_forwarder", _raise(door_install.DoorInstallError("x")))
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    substrate._write_native_door_forwarder("some-tool", bin_dst, check_only=True, engine_root=engine_root)
    assert door_install.install_door_degrade_rows() == []


def test_reader_ignores_other_planes_cold_failed_rows(tmp_path, engine_root):
    telemetry.record_degrade(kind=telemetry.KIND_COLD_FAILED, cause="dialect-guard 'x': nope")
    assert door_install.install_door_degrade_rows() == []


def test_record_never_raises_into_the_install(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("sink down")

    monkeypatch.setattr(telemetry, "record_degrade", _boom)
    door_install.record_install_door_degrade("n", RuntimeError("x"))


def test_per_name_exceptions_are_oserror_only():
    src = substrate.__file__
    text = open(src, encoding="utf-8").read()
    assert "_PER_NAME_DEGRADE_EXCEPTIONS = (OSError,)" in text
