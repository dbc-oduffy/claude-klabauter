from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SHIM_PATH = Path(__file__).resolve().parent.parent / "_resolve_claude_klabauter.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location(
        "_c5_remediation_names_root_shim", _SHIM_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_bin_dir(root: Path, with_sentinel: bool = True) -> Path:
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    if with_sentinel:
        (bin_dir / "archive-stamp-cli.py").write_text("", encoding="utf-8")
    return bin_dir


@pytest.fixture
def _registry(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)

    return SimpleNamespace(ml_dir=ml_dir, settings_home=settings_home)


def test_missing_target_under_live_tree_names_resolved_root(
    tmp_path, _registry, monkeypatch, capsys
):
    live_root = tmp_path / "live"
    _make_bin_dir(live_root, with_sentinel=True)

    (_registry.ml_dir / "registry.local.toml").write_text(
        f"[repos]\nclaude_klabauter = '{live_root.as_posix()}'\n",
        encoding="utf-8",
    )

    shim = _load_shim()
    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("does-not-exist-anywhere.py", argv=[])

    assert excinfo.value.code == 127
    err = capsys.readouterr().err
    assert "<engine-clone>" not in err
    assert f"{live_root.as_posix()}/scripts/setup.py" in err


def test_missing_target_under_resolved_engine_names_resolved_root(
    tmp_path, _registry, monkeypatch, capsys
):
    published_root = tmp_path / "published"
    _make_bin_dir(published_root, with_sentinel=True)
    (published_root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    stamp = published_root / "coordinator_core" / "_engine_stamp"
    stamp.write_text("sha:test-c5-stamp\n", encoding="utf-8")

    live_root = tmp_path / "live"
    _make_bin_dir(live_root, with_sentinel=True)

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))

    (_registry.ml_dir / "registry.local.toml").write_text(
        "[repos]\n"
        f"claude_klabauter = '{published_root.as_posix()}'\n"
        f"claude_klabauter = '{live_root.as_posix()}'\n"
        "\n"
        "[engine]\n"
        "target = 'main'\n",
        encoding="utf-8",
    )

    shim = _load_shim()
    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("does-not-exist-anywhere.py", argv=[])

    assert excinfo.value.code == 127
    err = capsys.readouterr().err
    assert "<engine-clone>" not in err
    assert f"{published_root.as_posix()}/scripts/setup.py" in err


def test_unresolvable_root_names_bootstrap_remedies_not_a_path(
    tmp_path, _registry, monkeypatch, capsys
):
    shim = _load_shim()
    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("anything.py", argv=[])

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "<engine-clone>" not in err
    assert "COORDINATOR_ENGINE_ROOT" in err
    assert ".claude-klabauter-live-root" in err
