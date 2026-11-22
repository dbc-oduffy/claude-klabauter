"""unimatrix-emergency.py: key-file lifecycle, ssh argv and refusals, driven through an injected runner."""
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_emergency_under_test", BIN / "unimatrix-emergency.py")
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETBODYAAAA\n-----END OPENSSH PRIVATE KEY-----"
ENV = {E.ENV_VAR: KEY}


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _noop_restrict(path):
    return None


def recording_runner(calls, rc=0):
    def run(argv):
        key_file = Path(argv[argv.index("-i") + 1])
        calls.append({"argv": argv, "exists": key_file.is_file(), "content": key_file.read_text(encoding="utf-8"),
                      "mode": stat.S_IMODE(key_file.stat().st_mode)})
        return rc
    return run


def _leftovers(tmp_path):
    return [p for p in tmp_path.iterdir() if p.name.startswith("unimatrix-01-")]


def test_default_argv_uses_only_the_temp_identity_and_removes_it(tmp_path):
    calls = []
    rc = E.main(["machine-a"], env=ENV, runner=recording_runner(calls), restrict=_noop_restrict)
    assert rc == 0
    (call,) = calls
    argv = call["argv"]
    key = argv[argv.index("-i") + 1]
    assert argv == ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                    "-o", "ConnectTimeout=10", "unimatrix-control-machine-a"]
    assert call["exists"] and call["content"] == KEY + "\n"
    assert not Path(key).exists()
    assert _leftovers(tmp_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_posix_key_file_is_0600_with_default_restrict():
    calls = []
    assert E.main(["machine-a"], env=ENV, runner=recording_runner(calls)) == 0
    assert calls[0]["mode"] == 0o600


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL")
def test_windows_key_file_acl_has_inheritance_removed_and_current_user_only(tmp_path):
    seen = {}

    def run(argv):
        import subprocess
        key_file = argv[argv.index("-i") + 1]
        seen["acl"] = subprocess.run(["icacls", key_file], capture_output=True, text=True).stdout
        return 0

    assert E.main(["machine-a"], env=ENV, runner=run) == 0
    acl_lines = [ln for ln in seen["acl"].splitlines() if ":(" in ln]
    assert len(acl_lines) == 1
    assert E.getpass.getuser().lower() in acl_lines[0].lower()


def test_remote_command_follows_the_alias_and_dashdash_is_dropped():
    calls = []
    E.main(["machine-a", "--", "uptime", "-p"], env=ENV, runner=recording_runner(calls), restrict=_noop_restrict)
    assert calls[0]["argv"][-3:] == ["unimatrix-control-machine-a", "uptime", "-p"]


def test_key_file_removed_when_ssh_fails(tmp_path):
    calls = []
    rc = E.main(["machine-a"], env=ENV, runner=recording_runner(calls, rc=255), restrict=_noop_restrict)
    assert rc == 255
    assert _leftovers(tmp_path) == []


def test_key_file_removed_when_runner_raises(tmp_path):
    def boom(argv):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        E.emergency("machine-a", [], ENV, boom, _noop_restrict)
    assert _leftovers(tmp_path) == []


def test_restrict_failure_refuses_without_leaving_a_file_and_never_runs_ssh(tmp_path, capsys):
    def deny(path):
        raise E.Refusal("the key file could not be restricted to the current user")

    calls = []
    assert E.main(["machine-a"], env=ENV, runner=recording_runner(calls), restrict=deny) == 2
    assert calls == [] and _leftovers(tmp_path) == []
    assert "restricted" in capsys.readouterr().err


@pytest.mark.parametrize("env", [{}, {E.ENV_VAR: ""}, {E.ENV_VAR: "  \n"}])
def test_unset_or_empty_variable_is_a_typed_refusal_naming_it(env, tmp_path, capsys):
    calls = []
    assert E.main(["machine-a"], env=env, runner=recording_runner(calls), restrict=_noop_restrict) == 2
    assert calls == [] and _leftovers(tmp_path) == []
    assert E.ENV_VAR in capsys.readouterr().err


def test_a_value_that_is_not_a_private_key_is_refused_without_echoing_it(tmp_path, capsys):
    env = {E.ENV_VAR: "hunter2-not-a-key"}
    assert E.main(["machine-a"], env=env, runner=recording_runner([]), restrict=_noop_restrict) == 2
    out = capsys.readouterr()
    assert "hunter2" not in out.out + out.err and E.ENV_VAR in out.err
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("box", ["-oProxyCommand=x", "a b", "a;b", ""])
def test_invalid_box_is_refused(box, tmp_path, capsys):
    calls = []
    try:
        rc = E.main([box], env=ENV, runner=recording_runner(calls), restrict=_noop_restrict)
    except SystemExit as exit_:
        rc = exit_.code
    assert rc == 2 and calls == []
    assert _leftovers(tmp_path) == []


def test_key_material_never_reaches_output_or_argv(capsys):
    calls = []
    E.main(["machine-a", "id"], env=ENV, runner=recording_runner(calls), restrict=_noop_restrict)
    out = capsys.readouterr()
    assert "SECRETBODY" not in out.out + out.err
    assert not any("SECRETBODY" in a for a in calls[0]["argv"])


def test_single_line_value_with_literal_backslash_n_and_crlf_is_normalized():
    one_line = "-----BEGIN OPENSSH PRIVATE KEY-----\\nBODY\\n-----END OPENSSH PRIVATE KEY-----"
    assert E.normalize_key(one_line) == "-----BEGIN OPENSSH PRIVATE KEY-----\nBODY\n-----END OPENSSH PRIVATE KEY-----\n"
    crlf = KEY.replace("\n", "\r\n")
    assert E.normalize_key(crlf) == KEY + "\n"


def test_real_ssh_runner_returns_255_when_ssh_cannot_start(monkeypatch, capsys):
    def nope(*a, **k):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(E.subprocess, "run", nope)
    assert E.run_ssh(["ssh"]) == 255
    assert "could not run" in capsys.readouterr().err
