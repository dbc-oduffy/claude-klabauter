"""
Tests for coordinator_core.ops.coordinator_setup_state — parity tests against
the bash oracle.

Covers: seed-on-first-record, idempotent first-occurrence-wins, check exit
codes, status absence/presence, bad-arg rejection, auto-record self-heal, and
the bash-oracle-faithful "missing .claude parent dir -> record fails" quirk.

Port of: coordinator-setup-state.sh (DoE b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.coordinator_setup_state import main


def _env(tmp_path, claude_home_exists=True):
    home = tmp_path / "home"
    home.mkdir()
    if claude_home_exists:
        (home / ".claude").mkdir()
    return {"CLAUDE_HOME": str(home), "HOME": str(home)}, home / ".claude" / "coordinator-setup-state.yaml"


def _run(monkeypatch, env, *args):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return main(list(args))


def test_check_before_record_fails(tmp_path, monkeypatch, capsys):
    env, _ = _env(tmp_path)
    rc = _run(monkeypatch, env, "check", "setup_concluded")
    assert rc == 1


def test_status_before_record_nonzero(tmp_path, monkeypatch, capsys):
    env, _ = _env(tmp_path)
    rc = _run(monkeypatch, env, "status")
    assert rc == 1
    out = capsys.readouterr().out
    assert "No coordinator setup-state receipt at" in out


def test_record_seeds_and_writes(tmp_path, monkeypatch, capsys):
    env, state_file = _env(tmp_path)
    rc = _run(monkeypatch, env, "record", "setup_concluded")
    assert rc == 0
    assert state_file.is_file()
    content = state_file.read_text()
    assert "version: 1" in content
    assert "setup_concluded_at:" in content


def test_check_after_record_passes(tmp_path, monkeypatch):
    env, _ = _env(tmp_path)
    _run(monkeypatch, env, "record", "setup_concluded")
    rc = _run(monkeypatch, env, "check", "setup_concluded")
    assert rc == 0


def test_record_idempotent_first_occurrence_wins(tmp_path, monkeypatch, capsys):
    env, state_file = _env(tmp_path)
    _run(monkeypatch, env, "record", "setup_concluded")
    first = [l for l in state_file.read_text().splitlines() if l.startswith("setup_concluded_at:")][0]
    rc = _run(monkeypatch, env, "record", "setup_concluded")
    assert rc == 0
    out = capsys.readouterr().out
    assert "already recorded; leaving unchanged." in out
    second = [l for l in state_file.read_text().splitlines() if l.startswith("setup_concluded_at:")][0]
    assert first == second


def test_independent_milestones_coexist(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    _run(monkeypatch, env, "record", "setup_concluded")
    _run(monkeypatch, env, "record", "orientation_started")
    assert _run(monkeypatch, env, "check", "orientation_started") == 0
    assert _run(monkeypatch, env, "check", "orientation_completed") == 1
    content = state_file.read_text()
    assert "setup_concluded_at:" in content
    assert "orientation_started_at:" in content


@pytest.mark.parametrize(
    "args",
    [
        ("record", "bogus"),
        ("record",),
        ("check", "bogus"),
        (),
        ("--help",),
        ("frobnicate",),
    ],
)
def test_bad_or_missing_args_rejected(tmp_path, monkeypatch, args):
    env, _ = _env(tmp_path)
    rc = _run(monkeypatch, env, *args)
    assert rc == 2


def test_status_prints_receipt(tmp_path, monkeypatch, capsys):
    env, _ = _env(tmp_path)
    _run(monkeypatch, env, "record", "orientation_started")
    capsys.readouterr()
    rc = _run(monkeypatch, env, "status")
    assert rc == 0
    out = capsys.readouterr().out
    assert "orientation_started_at:" in out


def test_status_header_only_file_nonzero(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    state_file.write_text("# header comment\nversion: 1\n")
    rc = _run(monkeypatch, env, "status")
    assert rc == 1


def test_record_fails_when_claude_home_missing(tmp_path, monkeypatch, capsys):
    """Bash-oracle-faithful quirk: record does NOT create a missing parent dir."""
    env, state_file = _env(tmp_path, claude_home_exists=False)
    rc = _run(monkeypatch, env, "record", "setup_concluded")
    assert rc == 1
    assert not state_file.is_file()
    err = capsys.readouterr().err
    assert "mktemp failed (seed)" in err


def test_auto_record_no_registry_noop(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    rc = _run(monkeypatch, env, "auto-record-if-source-is-live")
    assert rc == 0
    assert not state_file.is_file()


def _ml_dir(home: str) -> str:
    """Mirrors the module's ``_machine_local_dir`` resolution: settings-home
    (``<CLAUDE_HOME>/.coordinator-claude-settings``), NOT ``<CLAUDE_HOME>/.claude``
    -- the two live under different roots, which is exactly the bug this
    module's fix closed (it previously read the registry from the legacy
    ``<claude_home>/machine-local`` location instead of settings-home)."""
    return os.path.join(home, ".coordinator-claude-settings", "machine-local")


def test_auto_record_copy_install_noop(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    ml_dir = _ml_dir(env["HOME"])
    os.makedirs(ml_dir, exist_ok=True)
    with open(os.path.join(ml_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "copy_install"\n')
    rc = _run(monkeypatch, env, "auto-record-if-source-is-live")
    assert rc == 0
    assert not state_file.is_file()


def test_auto_record_source_is_live_records_silently(tmp_path, monkeypatch, capsys):
    env, state_file = _env(tmp_path)
    ml_dir = _ml_dir(env["HOME"])
    os.makedirs(ml_dir, exist_ok=True)
    with open(os.path.join(ml_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "source_is_live"\n')
    rc = _run(monkeypatch, env, "auto-record-if-source-is-live")
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert state_file.is_file()
    assert "setup_concluded_at:" in state_file.read_text()


def test_auto_record_idempotent_second_call(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    ml_dir = _ml_dir(env["HOME"])
    os.makedirs(ml_dir, exist_ok=True)
    with open(os.path.join(ml_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "source_is_live"\n')
    _run(monkeypatch, env, "auto-record-if-source-is-live")
    first = [l for l in state_file.read_text().splitlines() if l.startswith("setup_concluded_at:")][0]
    _run(monkeypatch, env, "auto-record-if-source-is-live")
    second = [l for l in state_file.read_text().splitlines() if l.startswith("setup_concluded_at:")][0]
    assert first == second


def test_auto_record_source_is_live_only_in_tracked_registry(tmp_path, monkeypatch):
    """Per-key fallthrough: a propagation_mode declared only in the tracked
    registry.toml is honored when registry.local.toml exists but is silent on
    the key (previously .local-only -- the tracked declaration was invisible)."""
    env, state_file = _env(tmp_path)
    ml_dir = _ml_dir(env["HOME"])
    os.makedirs(ml_dir, exist_ok=True)
    with open(os.path.join(ml_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write("schema = 1\n")
    with open(os.path.join(ml_dir, "registry.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "source_is_live"\n')
    rc = _run(monkeypatch, env, "auto-record-if-source-is-live")
    assert rc == 0
    assert state_file.is_file()
    assert "setup_concluded_at:" in state_file.read_text()


def test_auto_record_local_mode_wins_over_tracked(tmp_path, monkeypatch):
    """Per-key precedence: a mode declared in registry.local.toml wins over the
    tracked registry.toml -- local copy_install suppresses tracked
    source_is_live."""
    env, state_file = _env(tmp_path)
    ml_dir = _ml_dir(env["HOME"])
    os.makedirs(ml_dir, exist_ok=True)
    with open(os.path.join(ml_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "copy_install"\n')
    with open(os.path.join(ml_dir, "registry.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "source_is_live"\n')
    rc = _run(monkeypatch, env, "auto-record-if-source-is-live")
    assert rc == 0
    assert not state_file.is_file()
