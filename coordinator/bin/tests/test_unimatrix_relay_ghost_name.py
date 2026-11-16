"""check_name_free against real registry files: roster mirrors and corpses never block an inbox."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_unimatrix_scripts import load  # noqa: E402


def _write(sess, pid, entry):
    (sess / f"{pid}.json").write_text(json.dumps(entry), encoding="utf-8")
    (sess / f"{pid}.k.key").write_text("k", encoding="utf-8")


@pytest.fixture
def relay(monkeypatch, tmp_path):
    mod = load("relay")
    monkeypatch.setattr(mod, "SESS", str(tmp_path))
    return mod


def _live_row(name, **extra):
    return {"pid": os.getpid(), "name": name, **extra}


def test_live_roster_mirror_named_name_lets_the_inbox_open(relay, tmp_path, monkeypatch):
    row = _live_row("group-em-ad", macSocketPath="/tmp/cc-socks/far.sock", unimatrixMirror=True,
                    messagingSocketPath="/tmp/cc-socks/cc-msg-abc.sock")
    _write(tmp_path, os.getpid(), row)
    monkeypatch.setattr(relay, "mirror_orphaned", lambda d: False)
    assert relay.check_name_free("group-em-ad") == []
    assert (tmp_path / f"{os.getpid()}.json").exists()


def test_live_roster_mirror_without_mirror_flag_lets_the_inbox_open(relay, tmp_path, monkeypatch):
    _write(tmp_path, os.getpid(), _live_row("group-em-ad", macSocketPath="/tmp/cc-socks/far.sock"))
    monkeypatch.setattr(relay, "mirror_orphaned", lambda d: False)
    assert relay.check_name_free("group-em-ad") == []
    assert (tmp_path / f"{os.getpid()}.json").exists()


def test_live_distinct_session_still_refuses(relay, tmp_path):
    _write(tmp_path, os.getpid(), _live_row("group-em-ad"))
    with pytest.raises(SystemExit, match="live local session"):
        relay.check_name_free("group-em-ad")


def test_live_pid_without_procstart_still_refuses(relay, tmp_path):
    row = _live_row("group-em-ad")
    assert "procStart" not in row
    _write(tmp_path, os.getpid(), row)
    with pytest.raises(SystemExit, match="live local session"):
        relay.check_name_free("group-em-ad")


def test_procstart_corpse_does_not_block_and_is_never_freed(relay, tmp_path, monkeypatch):
    pid = os.getpid()
    _write(tmp_path, pid, _live_row("group-em-ad", procStart="Fri Nov  6 09:00:00 2026"))
    liveness = relay._liveness()
    monkeypatch.setattr(liveness, "proc_start", lambda p: "Fri Nov  6 10:30:00 2026")
    freed = relay.check_name_free("group-em-ad")
    assert pid not in {f[0] for f in freed}
    assert (tmp_path / f"{pid}.json").exists()
