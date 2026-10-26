"""Real-git cases for the `record_committed` flag stamped by canvas_harvest.probe_stamp."""

import json
import subprocess

import pytest

from coordinator_core.canvas_harvest import model
from coordinator_core.canvas_harvest.tests.test_probe_stamp import (  # noqa: F401
    _config_dir,
    _record,
    _setup,
    _unreadable,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )


def _committed_repo(tmp_path, account="a@x"):
    """A git repo whose HEAD holds the full record under harvest/ (frame a.html listed, thread t1)."""
    rec = _record()
    rec["harvest"]["account"] = account
    rec["harvest"]["frames"] = [{"path": "a.html", "sha256": "0" * 64}]
    out = _setup(tmp_path, rec)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "m")
    return out


def test_record_committed_true_when_head_matches(tmp_path):
    out = _committed_repo(tmp_path)
    assert _unreadable(out)["record_committed"] is True


def test_record_committed_ignores_prior_probe_key(tmp_path):
    out = _committed_repo(tmp_path)
    _unreadable(out)
    assert _unreadable(out)["record_committed"] is True


@pytest.mark.parametrize("rel", ["SETTLED.md", "frames/a.html", "threads/t1.json"])
def test_record_committed_false_when_listed_file_differs(tmp_path, rel):
    out = _committed_repo(tmp_path)
    (out / rel).write_bytes(b"changed\n")
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_listed_file_missing(tmp_path):
    out = _committed_repo(tmp_path)
    (out / "frames" / "a.html").unlink()
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_record_body_differs(tmp_path):
    out = _committed_repo(tmp_path)
    rec = json.loads((out / "harvest.json").read_text())
    rec["harvest"]["version"] = "v9"
    (out / "harvest.json").write_bytes(model.dump_json(rec))
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_false_when_file_untracked(tmp_path):
    out = _committed_repo(tmp_path)
    (out / "threads" / "t2.json").write_bytes(b"{}\n")
    rec = json.loads((out / "harvest.json").read_text())
    rec["harvest"]["thread_ids"] = ["t1", "t2"]
    (out / "harvest.json").write_bytes(model.dump_json(rec))
    assert _unreadable(out)["record_committed"] is False


def test_record_committed_spawns_no_process(tmp_path, monkeypatch):
    out = _committed_repo(tmp_path)

    def boom(*a, **k):
        raise AssertionError("record_committed must not spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _unreadable(out)["record_committed"] is True
