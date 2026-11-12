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


@pytest.fixture(autouse=True)
def _settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))


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


def write_record(tmp_path, box="machine-a", **fields):
    d = tmp_path / "unimatrix" / "control-far"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{box}.json").write_text(json.dumps(fields), encoding="utf-8")


_DRIVE = "".join(["C", ":"])
WIN_PY = _DRIVE + "\\Py\\python.exe"
WIN_SCRIPT = _DRIVE + "\\Users\\a b\\unimatrix\\unimatrix-control.py"
WIN = {"platform": "win32", "interpreter": WIN_PY, "script": WIN_SCRIPT, "shell": "cmd"}
POSIX = {"platform": "darwin", "interpreter": "/opt/py thon/bin/python3",
         "script": "/home/a/unimatrix/unimatrix-control.py", "shell": "posix"}


def test_win32_cmd_record_sends_double_quoted_paths(tmp_path, capsys):
    write_record(tmp_path, **WIN)
    calls = []
    assert L.main(["machine-a", "myrepo"], runner=runner_returning(OK, calls=calls)) == 0
    assert calls[0][-1] == f'"{WIN_PY}" "{WIN_SCRIPT}" launch myrepo'
    assert capsys.readouterr().out.strip() == "repo-ab12cd"


def test_win32_powershell_record_prefixes_call_operator(tmp_path):
    write_record(tmp_path, **{**WIN, "shell": "powershell"})
    calls = []
    L.main(["machine-a", "--wake", "repo-ab12cd"], runner=runner_returning(OK, calls=calls))
    assert calls[0][-1] == f'& "{WIN_PY}" "{WIN_SCRIPT}" wake repo-ab12cd'


def test_posix_record_sends_shlex_join(tmp_path, capsys):
    write_record(tmp_path, **POSIX)
    calls = []
    assert L.main(["machine-a", "myrepo"], runner=runner_returning(OK, calls=calls)) == 0
    assert calls[0][-1] == "'/opt/py thon/bin/python3' /home/a/unimatrix/unimatrix-control.py launch myrepo"
    L.main(["machine-a", "--wake", "n1"], runner=runner_returning(OK, calls=calls))
    assert calls[1][-1].endswith(" wake n1")


@pytest.mark.parametrize("bad", [
    {k: v for k, v in POSIX.items() if k != "script"},
    {**POSIX, "shell": "fish"},
    {**POSIX, "interpreter": "python3"},
    {**WIN, "script": "unimatrix\\unimatrix-control.py"},
    {**WIN, "platform": "linux"},
])
def test_unusable_record_is_treated_as_absent(tmp_path, bad):
    write_record(tmp_path, **bad)
    calls = []
    L.main(["machine-a", "myrepo"], runner=runner_returning(OK, calls=calls))
    assert calls[0][-1] == "launch myrepo"


def test_corrupt_record_is_treated_as_absent(tmp_path):
    d = tmp_path / "unimatrix" / "control-far"
    d.mkdir(parents=True)
    (d / "machine-a.json").write_text("{not json", encoding="utf-8")
    calls = []
    L.main(["machine-a", "myrepo"], runner=runner_returning(OK, calls=calls))
    assert calls[0][-1] == "launch myrepo"


def test_no_record_failure_appends_install_hint(capsys):
    L.main(["machine-a", "r"], runner=runner_returning("", rc=255, err="denied"))
    assert "run unimatrix-install.py --control-key machine-a to record the far invocation" in capsys.readouterr().err
    reply = json.dumps({"ok": False, "error": "nope"}) + "\n"
    L.main(["machine-a", "r"], runner=runner_returning(reply, rc=1))
    assert "to record the far invocation" in capsys.readouterr().err


def test_record_present_failure_has_no_install_hint(tmp_path, capsys):
    write_record(tmp_path, **POSIX)
    L.main(["machine-a", "r"], runner=runner_returning("", rc=255, err="denied"))
    assert "to record the far invocation" not in capsys.readouterr().err


def test_repo_key_and_wake_are_exclusive():
    with pytest.raises(SystemExit):
        L.main(["machine-a", "r", "--wake", "n"], runner=runner_returning(OK))
    with pytest.raises(SystemExit):
        L.main(["machine-a"], runner=runner_returning(OK))
