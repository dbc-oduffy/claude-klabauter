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


def test_record_run_swallows_failure(tmp_path, capsys):
    reuse.record_run(str(tmp_path), "Bad Name", CMD, SHA, [], "t", 0.0, 0, "pass", True, None, False)
    assert "suite-run record not written:" in capsys.readouterr().err


def test_record_run_writes_current(tmp_path):
    reuse.record_run(str(tmp_path), "default", CMD, SHA, [], "t", 0.0, 0, "pass", True, None, False)
    assert srr.read_runs(srr.record_dir(tmp_path, "default"))["current"]["outcome"] == "pass"
