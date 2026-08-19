"""
coordinator/lib/resolve-claude-klabauter/tests/test_exec_cli_no_live_tree_fallback.py

Chunk C13 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md):
"Close the forwarder-set gap and retire exec_cli's live-tree fallback."

Pins the retirement of `exec_cli`'s C4b per-target fallback: once the
resolved class is `resolved-engine` and *target* is absent under that
root's `coordinator/bin/`, `exec_cli` must now fail loud (127, naming only
the ONE resolved root actually tried) rather than silently probing the
live working tree via `resolve_claude_klabauter_bin_dir()` and exec'ing from there.

Negative-spec:
  - Does NOT touch `resolve_claude_klabauter_bin_dir()` itself (DR-326's locator
    axis) — see `test_dispatch_prefers_stamped_engine.py`'s own coverage of
    that function; this file exercises `exec_cli` only.
  - Does NOT assert anything about `resolve_claude_klabauter_root_with_class()`'s own
    ladder (C3/C5) — that is covered by
    `test_dispatch_prefers_stamped_engine.py`. This file's fixtures set up a
    registry shape that ladder already resolves to `resolved-engine` and
    then focuses purely on `exec_cli`'s post-resolution per-target gate.
"""
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
    """Target absent under the resolved published-engine root, but PRESENT
    under the live tree — the exact shape C4b's fallback used to rescue.
    C13: must now exit 127 naming only the published root, never reaching
    for the live tree at all."""
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
    """The fail-loud message names the ONE resolved root actually tried and
    does not reference a second, live-tree root — C4b's retired message
    named both roots; C13's message names one."""
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
    """Positive control proving the live-tree probe is genuinely gone, not
    merely un-asserted: the live tree's `coordinator/bin/` has no sentinel
    at all (would raise `ClaudeKlabauterResolutionError` if C4b's
    `resolve_claude_klabauter_bin_dir()` fallback call still ran). C13's retired
    fallback caught that error and folded 'unresolvable live working tree'
    into the message; the current code must exit 127 cleanly instead,
    proving that rung is never reached."""
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
