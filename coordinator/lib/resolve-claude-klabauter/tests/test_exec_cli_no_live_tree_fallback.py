from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SHIM_PATH = Path(__file__).resolve().parent.parent / "_resolve_claude_klabauter.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("_c13_no_fallback_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_stamp(root: Path, body: str = "sha:test-c13-stamp\n") -> None:
    stamp = root / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(body, encoding="utf-8")


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
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    return SimpleNamespace(ml_dir=ml_dir, settings_home=settings_home)


def _resolved_engine_fixture(tmp_path, _registry, monkeypatch, live_bin_present=True):
    """Registry shape that `resolve_claude_klabauter_root_with_class()` resolves to
    `RESOLUTION_RESOLVED_ENGINE`: a stamped published root, a session
    outside both roots, and `engine.target` readable — mirrors
    `test_dispatch_uses_stamped_engine_as_last_resort`'s and
    `test_dispatch_denies_unstamped_engine_falls_through_to_live_tree`'s own
    setup shape."""
    published_root = tmp_path / "published"
    _make_bin_dir(published_root, with_sentinel=True)
    (published_root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    _write_stamp(published_root)

    live_root = tmp_path / "live"
    _make_bin_dir(live_root, with_sentinel=live_bin_present)

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
    return SimpleNamespace(published_root=published_root, live_root=live_root)


def test_missing_target_under_resolved_engine_fails_loud_no_live_probe(
    tmp_path, _registry, monkeypatch
):
    fixture = _resolved_engine_fixture(tmp_path, _registry, monkeypatch)
    (fixture.live_root / "coordinator" / "bin" / "only-on-live.py").write_text(
        "", encoding="utf-8"
    )

    shim = _load_shim()
    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("only-on-live.py", argv=[])

    assert excinfo.value.code == 127


def test_missing_target_error_names_resolved_root_only(
    tmp_path, _registry, monkeypatch, capsys
):
    fixture = _resolved_engine_fixture(tmp_path, _registry, monkeypatch)

    shim = _load_shim()
    with pytest.raises(SystemExit):
        shim.exec_cli("does-not-exist-anywhere.py", argv=[])

    err = capsys.readouterr().err
    assert fixture.published_root.as_posix() in err
    assert "live working tree" not in err
    assert "unresolvable live working tree" not in err


def test_missing_target_does_not_touch_broken_live_tree(
    tmp_path, _registry, monkeypatch, capsys
):
    fixture = _resolved_engine_fixture(
        tmp_path, _registry, monkeypatch, live_bin_present=False
    )

    shim = _load_shim()
    with pytest.raises(SystemExit) as excinfo:
        shim.exec_cli("does-not-exist-anywhere.py", argv=[])

    assert excinfo.value.code == 127
    err = capsys.readouterr().err
    assert "unresolvable live working tree" not in err
    assert fixture.published_root.as_posix() in err
