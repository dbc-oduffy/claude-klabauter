"""Pins the roster supervisor's reconnect-after-far-reboot: no step ends the loop, failures back off to a cap."""
import importlib.util
import json
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_roster_rc", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Stop(Exception):
    pass


def drive(mod, monkeypatch, tmp_path, outcomes, reap=None):
    """Run main() through `outcomes` (True = far answers, False = rc=255); returns (log text, sleeps)."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "inbound_names", lambda: set())
    synced = []
    monkeypatch.setattr(mod, "sync", lambda *a, **k: synced.append(1))
    monkeypatch.setattr(mod, "reap_unbacked_agy_faces", lambda *a, **k: None)
    monkeypatch.setattr(mod, "reap_orphaned_mirrors", reap or (lambda *a, **k: None))
    seq = iter(outcomes)

    def fetch(*a, **k):
        if not next(seq):
            raise OSError("roster rc=255: Connection timed out")
        return []

    monkeypatch.setattr(mod, "fetch_roster", fetch)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= len(outcomes):
            raise Stop

    monkeypatch.setattr(mod.time, "sleep", sleep)
    log = tmp_path / "roster.log"
    with pytest.raises(Stop):
        mod.main([str(log), "--remote", "example-fleet-pro", "--key", "k"])
    return log.read_text(encoding="utf-8"), sleeps, synced


def test_far_down_for_n_cycles_then_back_relinks_without_a_trigger(monkeypatch, tmp_path):
    mod = load()
    text, sleeps, synced = drive(mod, monkeypatch, tmp_path, [True] + [False] * 6 + [True, True])
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1
    assert len(synced) == 3, "sync must run again once the far box answers"
    rec = json.loads((tmp_path / "home" / "unimatrix" / "link-state" / "example-fleet-pro.json").read_text("utf-8"))
    assert rec["state"] == "up"


def test_a_raise_in_the_failure_branch_reap_does_not_end_the_loop(monkeypatch, tmp_path):
    mod = load()

    def reap(remote, say, unbacked=False, keep=()):
        if unbacked:
            raise FileNotFoundError("session file already removed")

    text, _, synced = drive(mod, monkeypatch, tmp_path, [False, False, False, True, True], reap=reap)
    assert "reap failed: FileNotFoundError" in text
    assert text.count("LINK-UP") == 1 and len(synced) == 2


def test_a_raise_that_is_not_in_the_old_catch_list_is_retried(monkeypatch, tmp_path):
    mod = load()
    monkeypatch.setattr(mod, "mirrorable", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    text, sleeps, _ = drive(mod, monkeypatch, tmp_path, [True, True, True])
    assert text.count("roster failed: boom") == 3


def test_backoff_doubles_to_one_poll_multiple_cap_and_resets(monkeypatch, tmp_path):
    mod = load()
    assert [mod.backoff_seconds(n) for n in (1, 2, 3, 4, 9)] == [
        mod.POLL_SECONDS, 2 * mod.POLL_SECONDS, 4 * mod.POLL_SECONDS, 4 * mod.POLL_SECONDS, 4 * mod.POLL_SECONDS]
    assert mod.BACKOFF_CAP_SECONDS % mod.POLL_SECONDS == 0
    _, sleeps, _ = drive(mod, monkeypatch, tmp_path, [False, False, False, False, True, False])
    assert sleeps == [mod.POLL_SECONDS, 2 * mod.POLL_SECONDS, 4 * mod.POLL_SECONDS, 4 * mod.POLL_SECONDS,
                      mod.POLL_SECONDS, mod.POLL_SECONDS]


def test_drop_session_files_tolerates_a_file_the_bridge_already_removed(monkeypatch, tmp_path):
    mod = load()
    sess = tmp_path / "sess"
    sess.mkdir()
    (sess / "77.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "sessions_dir", lambda: str(sess))
    real = mod.os.remove

    def remove(p):
        real(p)
        raise FileNotFoundError(p)

    monkeypatch.setattr(mod.os, "remove", remove)
    mod.drop_session_files(77)
    assert not (sess / "77.json").exists()
