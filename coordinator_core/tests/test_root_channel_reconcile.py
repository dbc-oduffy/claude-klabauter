
from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from coordinator_core import root_channel_reconcile as rcr


@pytest.fixture
def machine_local(tmp_path: Path) -> Path:
    d = tmp_path / "machine-local"
    d.mkdir()
    return d


def _stub_registry(monkeypatch: pytest.MonkeyPatch, values: dict) -> None:
    monkeypatch.setattr(rcr, "_read_registry", lambda key: (values.get(key), False))


def test_all_channels_agreeing_and_present_is_ok(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(
        monkeypatch,
        {
            "repos.claude_klabauter": str(real),
            "publish.mirrors.claude_klabauter.path": str(real),
        },
    )
    (machine_local / ".claude-klabauter-root").write_text(str(real), encoding="utf-8")

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert report.ok
    assert report.disagreeing == ()
    assert report.absent_targets == ()
    assert rcr.disagreement_message([report]) is None


def test_pointer_disagreeing_with_registry_is_named_with_both_paths(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INCIDENT 1 (2026-08-22 install dogfood): the pointer file carried a
    foreign-platform path for the published mirror while the registry key was
    correct all along. The two channels must not both be reported as fine,
    and the message must carry the pointer file's own path -- an operator who
    is told only "no engine build stamp" has nothing to edit."""
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})
    pointer = machine_local / ".claude-klabauter-root"
    pointer.write_text(str(tmp_path / "no-such-klabauter"), encoding="utf-8")

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert not report.ok
    assert "pointer .claude-klabauter-root" in report.disagreeing
    assert "registry repos.claude_klabauter" in report.disagreeing
    assert report.absent_targets == ("pointer .claude-klabauter-root",)

    message = rcr.disagreement_message([report])
    assert message is not None
    assert str(pointer) in message, "the message must name the file to edit"
    assert "path does not exist" in message


def test_a_present_path_that_disagrees_is_still_a_finding(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _stub_registry(
        monkeypatch,
        {
            "repos.claude_klabauter": str(a),
            "publish.mirrors.claude_klabauter.path": str(b),
        },
    )

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert not report.ok
    assert report.absent_targets == ("pointer .claude-klabauter-root",)
    assert len(report.disagreeing) == 2


def test_silent_channels_never_count_as_disagreement(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "claude-klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert report.ok
    assert [c.value for c in report.channels if c.value] == [str(real)]


def test_trailing_separator_and_dot_segments_are_not_a_disagreement(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real) + "/"})
    (machine_local / ".claude-klabauter-root").write_text(
        str(tmp_path) + "/./klabauter\n", encoding="utf-8"
    )

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert report.ok


def test_unreadable_channel_is_reported_and_never_raises(
    machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rcr, "_read_registry", lambda key: (None, True))
    pointer = machine_local / ".claude-klabauter-root"
    pointer.mkdir()

    report = rcr.reconcile("claude_klabauter", machine_local)

    assert all(c.unreadable for c in report.channels)
    assert report.ok, "nothing spoke, so there is nothing to reconcile"


def test_unknown_root_name_raises_keyerror(machine_local: Path) -> None:
    with pytest.raises(KeyError):
        rcr.reconcile("not_a_root", machine_local)


def test_cli_exits_nonzero_on_a_split_and_zero_when_reconciled(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})
    pointer = machine_local / ".claude-klabauter-root"
    pointer.write_text(str(tmp_path / "gone"), encoding="utf-8")

    assert rcr.main(["--machine-local", str(machine_local)]) == 1
    assert str(pointer) in capsys.readouterr().out

    pointer.write_text(str(real), encoding="utf-8")
    assert rcr.main(["--machine-local", str(machine_local)]) == 0


def test_every_flagged_channel_carries_a_runnable_remedy_and_agreeing_roots_none(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "klabauter"
    real.mkdir()
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(real)})
    pointer = machine_local / ".claude-klabauter-root"
    pointer.write_text(str(tmp_path / "gone"), encoding="utf-8")
    report = rcr.reconcile("claude_klabauter", machine_local)

    lines = rcr.remedy_lines([report])

    assert len(lines) == 2
    pointer_line = next(l for l in lines if str(pointer) in l)
    assert str(real) in pointer_line and "write_text" in pointer_line
    registry_line = next(l for l in lines if "repos.claude_klabauter" in l)
    assert "machine-local" in registry_line and " set " in registry_line
    assert "/slash" not in "".join(lines) and "/coordinator:" not in "".join(lines)
    assert rcr.remedy_lines([rcr.reconcile("claude_klabauter", machine_local)]) == []


def test_message_cites_no_decision_record(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(tmp_path)})
    (machine_local / ".claude-klabauter-root").write_text(
        str(tmp_path / "elsewhere"), encoding="utf-8"
    )

    message = rcr.disagreement_message([rcr.reconcile("claude_klabauter", machine_local)])

    assert message is not None
    assert "DR-" not in message


def test_missing_pointer_has_a_remedy_but_unconfigured_roots_stay_silent(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(tmp_path)})
    report = rcr.reconcile("claude_klabauter", machine_local)
    pointer = machine_local / ".claude-klabauter-root"

    assert not report.ok
    message = rcr.disagreement_message([report])
    assert message is not None and str(pointer) in message
    assert "write_text" in message
    _stub_registry(monkeypatch, {})
    assert rcr.disagreement_message([rcr.reconcile("claude_klabauter", machine_local)]) is None


def test_ambiguous_existing_roots_require_an_operator_choice(tmp_path: Path) -> None:
    channels = tuple(
        rcr.Channel(f"registry {key}", key, str(tmp_path / key), True)
        for key in ("first", "second")
    )
    report = rcr.RootReconciliation("root", "purpose", channels,
                                    tuple(c.name for c in channels), ())
    lines = rcr.remedy_lines([report])
    assert len(lines) == 2
    assert all("<real-root>" in line for line in lines)


def test_registry_remedy_names_the_installed_script(
    tmp_path: Path, machine_local: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings home"))
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(tmp_path / "gone")})

    message = rcr.disagreement_message([rcr.reconcile("claude_klabauter", machine_local)])

    assert message is not None
    assert str(tmp_path / "settings home" / "bin" / "machine-local") in message
    assert "<settings-home>" not in message


def test_pointer_remedy_runs_with_shell_metacharacters(tmp_path: Path) -> None:
    root = tmp_path / "example-operator $root café"
    root.mkdir()
    pointer = tmp_path / "example-operator $settings" / ".claude-klabauter-root"
    channel = rcr.Channel("pointer .claude-klabauter-root", str(pointer), None, False)
    command = rcr._remedy_for(channel, str(root))
    argv = (
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-Command", command]
        if os.name == "nt" else ["/bin/sh", "-c", command]
    )

    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert pointer.read_text(encoding="utf-8") == str(root)
