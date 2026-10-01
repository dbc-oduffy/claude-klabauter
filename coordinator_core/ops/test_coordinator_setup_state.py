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

import re

import yaml

from coordinator_core.ops import coordinator_setup_state as css
from coordinator_core.ops.coordinator_setup_state import (
    SetupReceipt,
    _state_file,
    main,
    record_setup_concluded,
)


@pytest.fixture(autouse=True)
def _drop_settings_home_override(monkeypatch):
    """Neutralise ``COORDINATOR_SETTINGS_HOME`` for every test in this module.

    ``_ml_dir`` below (and the module's own ``_machine_local_dir``) resolve the
    registry as ``<CLAUDE_HOME>/.coordinator-claude-settings/machine-local``,
    which is only the resolved location while COORDINATOR_SETTINGS_HOME is
    unset -- it is the FIRST rung of that precedence. The suite-root home
    quarantine (``coordinator_core/conftest.py::_quarantine_real_home``) does
    not clear it, so on a box where an operator exports it the auto-record
    cases read the operator's REAL registry.local.toml and assert against
    whatever propagation_mode that machine happens to declare instead of the
    one the test just wrote.
    """
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


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


def test_paths_absolute_when_home_unset_userprofile_set(tmp_path):
    """Native Windows sets USERPROFILE and not HOME. Resolving off HOME alone
    produced a RELATIVE `.claude/...` path, so `record` wrote its receipt into
    the process CWD — a real file, truthfully reported, in whatever repo the
    operator was standing in — while `~/.claude`'s receipt stayed stale. Sibling
    repos gate setup chaining on that file, so the miss was silent."""
    env = {"USERPROFILE": str(tmp_path)}

    state_file = _state_file(env)

    assert os.path.isabs(state_file)
    assert state_file == os.path.join(str(tmp_path), ".claude", "coordinator-setup-state.yaml")


def test_claude_home_still_wins_over_both_home_vars(tmp_path):
    env = {
        "CLAUDE_HOME": str(tmp_path / "explicit"),
        "HOME": str(tmp_path / "posix"),
        "USERPROFILE": str(tmp_path / "windows"),
    }
    assert _state_file(env).startswith(str(tmp_path / "explicit"))


def test_home_wins_over_userprofile_when_both_set(tmp_path):
    env = {"HOME": str(tmp_path / "posix"), "USERPROFILE": str(tmp_path / "windows")}
    assert _state_file(env).startswith(str(tmp_path / "posix"))


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
    rc = _run(monkeypatch, env, "record", "orientation_started")
    assert rc == 0
    assert state_file.is_file()
    content = state_file.read_text()
    assert "version: 1" in content
    assert "orientation_started_at:" in content


def test_check_after_record_passes(tmp_path, monkeypatch):
    env, _ = _env(tmp_path)
    _run(monkeypatch, env, "record", "orientation_started")
    rc = _run(monkeypatch, env, "check", "orientation_started")
    assert rc == 0


def test_record_idempotent_first_occurrence_wins(tmp_path, monkeypatch, capsys):
    env, state_file = _env(tmp_path)
    _run(monkeypatch, env, "record", "orientation_started")
    first = [l for l in state_file.read_text().splitlines() if l.startswith("orientation_started_at:")][0]
    rc = _run(monkeypatch, env, "record", "orientation_started")
    assert rc == 0
    out = capsys.readouterr().out
    assert "already recorded; leaving unchanged." in out
    second = [l for l in state_file.read_text().splitlines() if l.startswith("orientation_started_at:")][0]
    assert first == second


def test_independent_milestones_coexist(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    _run(monkeypatch, env, "record", "orientation_completed")
    _run(monkeypatch, env, "record", "orientation_started")
    assert _run(monkeypatch, env, "check", "orientation_started") == 0
    assert _run(monkeypatch, env, "check", "setup_concluded") == 1
    content = state_file.read_text()
    assert "orientation_completed_at:" in content
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
    rc = _run(monkeypatch, env, "record", "orientation_started")
    assert rc == 1
    assert not state_file.is_file()
    err = capsys.readouterr().err
    assert "mktemp failed (seed)" in err


def test_auto_record_is_a_noop_even_when_source_is_live(tmp_path, monkeypatch, capsys):
    env, state_file = _env(tmp_path)
    ml_dir = os.path.join(env["HOME"], ".coordinator-claude-settings", "machine-local")
    os.makedirs(ml_dir, exist_ok=True)
    with open(os.path.join(ml_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write('[plugin.mirrors.coordinator-claude]\npropagation_mode = "source_is_live"\n')
    rc = _run(monkeypatch, env, "auto-record-if-source-is-live")
    assert rc == 0
    assert capsys.readouterr() == ("", "")
    assert not state_file.is_file()


def _receipt(**over):
    base = dict(
        phases_ran=("Phase 1", "Phase 3"),
        phases_skipped=(("defender", 'declined: # "x"'),),
        coordinator_version="1.2.3",
        engine_ref="abc123",
    )
    base.update(over)
    return SetupReceipt(**base)


def test_record_setup_concluded_is_noop_via_cli(tmp_path, monkeypatch, capsys):
    env, state_file = _env(tmp_path)
    assert _run(monkeypatch, env, "record", "setup_concluded") == 0
    assert "nothing recorded" in capsys.readouterr().out
    assert not state_file.is_file()


def test_record_setup_concluded_single_atomic_write(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    calls = []
    real = css._atomic_write
    monkeypatch.setattr(css, "_atomic_write", lambda t, c: (calls.append(t), real(t, c))[1])
    assert record_setup_concluded(_receipt(), env) == 0
    assert len(calls) == 1
    data = yaml.safe_load(state_file.read_text())
    assert "setup_concluded_at" in data
    assert set(data["setup_receipt"]) == {
        "phases_ran",
        "phases_skipped",
        "coordinator_version",
        "engine_ref",
    }
    assert data["setup_receipt"]["phases_ran"] == ["Phase 1", "Phase 3"]
    assert data["setup_receipt"]["engine_ref"] == "abc123"


def test_record_setup_concluded_existing_stamp_is_byte_identical(tmp_path):
    env, state_file = _env(tmp_path)
    state_file.write_bytes(b"version: 1\nsetup_concluded_at: 2026-01-01T00:00:00Z\n")
    before = state_file.read_bytes()
    assert record_setup_concluded(_receipt(), env) == 0
    assert state_file.read_bytes() == before


def test_record_setup_concluded_reader_idiom_and_roundtrip(tmp_path, monkeypatch):
    env, state_file = _env(tmp_path)
    assert record_setup_concluded(_receipt(coordinator_version=None, engine_ref=None), env) == 0
    text = state_file.read_text()
    assert re.search(r"^setup_concluded_at:[ \t]+[^ \t#]", text, re.MULTILINE)
    assert not re.search(r"^setup_receipt:[ \t]+\S", text, re.MULTILINE)
    assert _run(monkeypatch, env, "check", "setup_concluded") == 0
    data = yaml.safe_load(text)
    assert data["setup_receipt"]["phases_skipped"] == [
        {"id": "defender", "reason": 'declined: # "x"'}
    ]
    assert data["setup_receipt"]["coordinator_version"] is None
    assert [l for l in text.splitlines() if re.match(r"^[a-z_]+_at:", l)] == [
        l for l in text.splitlines() if l.startswith("setup_concluded_at:")
    ]


def test_record_setup_concluded_missing_claude_dir_returns_1(tmp_path, capsys):
    env, state_file = _env(tmp_path, claude_home_exists=False)
    assert record_setup_concluded(_receipt(), env) == 1
    assert not state_file.is_file()
    assert "mktemp failed" in capsys.readouterr().err


def test_doubled_claude_home_is_rejected_on_every_subcommand(tmp_path, monkeypatch):
    """CLAUDE_HOME is the PARENT of `.claude`; the resolver appends that
    segment. `CLAUDE_HOME=$HOME/.claude` resolved the receipt to
    `$HOME/.claude/.claude/coordinator-setup-state.yaml` -- and where that
    directory already existed the write SUCCEEDED, so the module's
    fail-loud-on-missing-parent safeguard never fired. The receipt split, the
    canonical file stopped being updated, and coordinator install.md's Phase 7
    orientation gate read PENDING on every install thereafter."""
    home = tmp_path / "home"
    (home / ".claude" / ".claude").mkdir(parents=True)
    env = {"CLAUDE_HOME": str(home / ".claude"), "HOME": str(home)}

    for args in (
        ("record", "orientation_started"),
        ("check", "setup_concluded"),
        ("status",),
    ):
        assert _run(monkeypatch, env, *args) == 2, args

    assert not (home / ".claude" / ".claude" / "coordinator-setup-state.yaml").exists()


def test_doubled_claude_home_names_the_value_to_pass_instead(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    env = {"CLAUDE_HOME": str(home / ".claude"), "HOME": str(home)}

    _run(monkeypatch, env, "status")

    err = capsys.readouterr().err
    assert "CLAUDE_HOME" in err
    assert repr(str(home)) in err
