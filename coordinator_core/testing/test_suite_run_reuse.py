"""Tests for suite_run_reuse: find_reusable cases, line formats, record_run best-effort."""

import pytest

from coordinator_core.testing import suite_run_record as srr
from coordinator_core.testing import suite_run_reuse as reuse

CMD = ["pytest", "-q"]
SHA = "a" * 40


def _setup(tmp_path, monkeypatch, *, fresh=True, command=None, outcome="pass",
           truncated=False, log=True, slot="current"):
    rec_dir = srr.record_dir(tmp_path, "default")
    rec_dir.mkdir(parents=True)
    if log:
        (rec_dir / f"{slot}.log").write_text("ok\n")
    rec = {
        "command": reuse.command_key(CMD) if command is None else command,
        "head_sha": SHA, "exit_code": 0 if outcome == "pass" else 1, "outcome": outcome,
        "failed_ids": None, "log": f"{slot}.log", "log_truncated": truncated,
        "freshness": {"fresh": fresh},
    }
    slots = {"current": None, "previous": None}
    slots[slot] = rec
    monkeypatch.setattr(
        reuse, "_suite_runs",
        lambda params: {"collections": {"default": slots}},
    )
    return rec_dir


def test_fresh_record_is_reused(tmp_path, monkeypatch):
    rec_dir = _setup(tmp_path, monkeypatch)
    r = reuse.find_reusable(tmp_path, "default", CMD)
    assert r == reuse.Reuse("default", SHA, 0, "pass", None, str(rec_dir / "current.log"))


def test_fail_outcome_is_reused(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, outcome="fail")
    assert reuse.find_reusable(tmp_path, "default", CMD).exit_code == 1


@pytest.mark.parametrize("kw", [
    {"fresh": False}, {"command": ["other"]}, {"outcome": "interrupted"},
    {"outcome": "timeout"}, {"truncated": True}, {"log": False}, {"slot": "previous"},
])
def test_not_reusable(tmp_path, monkeypatch, kw):
    _setup(tmp_path, monkeypatch, **kw)
    assert reuse.find_reusable(tmp_path, "default", CMD) is None


def test_no_record(tmp_path, monkeypatch):
    monkeypatch.setattr(reuse, "_suite_runs", lambda params: {"collections": {}})
    assert reuse.find_reusable(tmp_path, "default", CMD) is None


def test_command_key_resolves_like_which(monkeypatch):
    monkeypatch.setattr(reuse.shutil, "which", lambda n: "/x/" + n)
    assert reuse.command_key(["a", "b"]) == ["/x/a", "b"]
    monkeypatch.setattr(reuse.shutil, "which", lambda n: None)
    assert reuse.command_key(["a", "b"]) == ["a", "b"]


def test_line_formats():
    r = reuse.Reuse("c", SHA, 0, "pass", None, "/l")
    assert reuse.reused_line(r) == f"suite-run: reused c@{SHA} outcome=pass log=/l"
    assert reuse.ran_line("c", SHA, "fail", "/l") == f"suite-run: ran c@{SHA} outcome=fail log=/l"


def test_git_head_and_dirty_parses_porcelain_v2(monkeypatch):
    out = (
        f"# branch.oid {SHA}\n# branch.head main\n"
        "1 .M N... 100644 100644 100644 aaa bbb dir/a.py\n"
        "2 R. N... 100644 100644 100644 aaa bbb R100 new.py\told.py\n"
        "u UU N... 100644 100644 100644 100644 a b c conflict.py\n"
    )
    monkeypatch.setattr(
        reuse.subprocess, "run",
        lambda *a, **k: reuse.subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    assert reuse._git_head_and_dirty("r") == (SHA, ["dir/a.py", "new.py", "conflict.py"])


def test_git_head_and_dirty_git_failure(monkeypatch):
    monkeypatch.setattr(
        reuse.subprocess, "run",
        lambda *a, **k: reuse.subprocess.CompletedProcess(a, 128, stdout="", stderr="x"),
    )
    assert reuse._git_head_and_dirty("r") == (None, [])


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_tee_survives_log_write_failure(tmp_path):
    import subprocess
    import sys

    from coordinator_core.win_portability import no_console_creationflags

    proc = subprocess.Popen(
        [sys.executable, "-c", "print('x' * 200000)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_console_creationflags(),
    )
    tee = reuse.Tee(proc, str(tmp_path / "t.log"))
    tee._log.close()  # every later log write raises ValueError
    assert proc.wait(timeout=20) == 0
    assert tee.finish() is True


def test_record_run_swallows_failure(tmp_path, capsys):
    reuse.record_run(str(tmp_path), "Bad Name", CMD, SHA, [], "t", 0.0, 0, "pass", True, None, False)
    assert "suite-run record not written:" in capsys.readouterr().err


def test_record_run_writes_current(tmp_path):
    reuse.record_run(str(tmp_path), "default", CMD, SHA, [], "t", 0.0, 0, "pass", True, None, False)
    assert srr.read_runs(srr.record_dir(tmp_path, "default"))["current"]["outcome"] == "pass"
