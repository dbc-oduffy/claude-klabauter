"""unimatrix-launch.py client, driven through an injected ssh runner returning the pinned reply."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_launch_under_test", BIN / "unimatrix-launch.py")
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


def runner_returning(reply, rc=0, err="", calls=None):
    def run(argv):
        if calls is not None:
            calls.append(argv)
        return rc, reply, err
    return run


OK = json.dumps({"ok": True, "session_name": "repo-ab12cd", "session_id": "s", "pid": 7, "family": "claude"}) + "\n"


def test_launch_prints_session_name_and_builds_control_argv(capsys):
    calls = []
    rc = L.main(["machine-a", "myrepo"], runner=runner_returning(OK, calls=calls))
    assert rc == 0
    assert capsys.readouterr().out.strip() == "repo-ab12cd"
    key = str(Path.home() / ".ssh" / "unimatrix_control_machine_a")
    assert calls == [["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                      "-o", "ConnectTimeout=5", "unimatrix-control-machine-a", "launch myrepo"]]


def test_run_ssh_timeout_becomes_failure_not_hang(monkeypatch):
    def boom(*a, **k):
        assert k["timeout"] == L.SSH_TIMEOUT_SECONDS
        raise L.subprocess.TimeoutExpired(a[0], k["timeout"])
    monkeypatch.setattr(L.subprocess, "run", boom)
    rc, out, err = L.run_ssh(["ssh"])
    assert rc == 255 and out == "" and "timed out" in err
    assert L.main(["machine-a", "r"], runner=L.run_ssh) == 1


def test_wake_uses_wake_verb(capsys):
    calls = []
    rc = L.main(["machine-a", "--wake", "repo-ab12cd"], runner=runner_returning(OK, calls=calls))
    assert rc == 0
    assert calls[0][-1] == "wake repo-ab12cd"
    assert capsys.readouterr().out.strip() == "repo-ab12cd"


def test_ok_false_prints_error_and_exits_1(capsys):
    reply = json.dumps({"ok": False, "error": "unknown repo key"}) + "\n"
    rc = L.main(["machine-a", "nope"], runner=runner_returning(reply, rc=1))
    cap = capsys.readouterr()
    assert rc == 1
    assert cap.out == ""
    assert "unknown repo key" in cap.err


def test_unparseable_reply_exits_1_with_ssh_stderr(capsys):
    rc = L.main(["machine-a", "r"], runner=runner_returning("", rc=255, err="Permission denied"))
    assert rc == 1
    assert "Permission denied" in capsys.readouterr().err


def test_ok_true_without_session_name_is_failure(capsys):
    rc = L.main(["machine-a", "r"], runner=runner_returning(json.dumps({"ok": True}) + "\n"))
    assert rc == 1


def test_reply_after_banner_noise_is_found():
    assert L.parse_reply("banner\n" + OK)["session_name"] == "repo-ab12cd"


def test_invalid_tokens_never_reach_ssh(capsys):
    calls = []
    rc = L.main(["machine-a", "a;rm -rf"], runner=runner_returning(OK, calls=calls))
    assert rc == 2
    assert calls == []


def test_repo_key_and_wake_are_exclusive():
    with pytest.raises(SystemExit):
        L.main(["machine-a", "r", "--wake", "n"], runner=runner_returning(OK))
    with pytest.raises(SystemExit):
        L.main(["machine-a"], runner=runner_returning(OK))
