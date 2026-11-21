"""Pins the roster supervisor's reconnect-after-far-reboot: no step ends the loop, failures back off to a cap."""
import importlib.util
import json
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

BIN = Path(__file__).resolve().parents[1]


def load() -> "ModuleType":
    spec = importlib.util.spec_from_file_location("unimatrix_roster_rc", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Stop(Exception):
    pass


def drive(mod, monkeypatch, tmp_path, outcomes, reap=None):
    """Run main() through `outcomes` (True = the far box answers, False = probe and roster both fail).

    Returns (log text, sleeps, synced, fetch times on the fake clock)."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    links = tmp_path / "home" / "unimatrix" / "link-state"  # a known box: a missing record is a first link
    links.mkdir(parents=True)
    (links / "example-fleet-pro.json").write_text(json.dumps({"state": "up"}), encoding="utf-8")
    monkeypatch.setattr(mod, "inbound_names", lambda: set())
    synced = []
    monkeypatch.setattr(mod, "sync", lambda *a, **k: synced.append(1))
    monkeypatch.setattr(mod, "reap_unbacked_agy_faces", lambda *a, **k: None)
    monkeypatch.setattr(mod, "reap_orphaned_mirrors", reap or (lambda *a, **k: None))
    seq = iter(outcomes)
    cur = {"ok": True}
    clock = {"t": 0.0}
    fetched = []
    monkeypatch.setattr(mod, "_now", lambda: clock["t"])

    def probe(*a, **k):
        cur["ok"] = next(seq)
        return (True, "") if cur["ok"] else (False, "connect timed out")

    def fetch(*a, **k):
        fetched.append(clock["t"])
        if not cur["ok"]:
            raise OSError("roster rc=255: Connection timed out")
        return []

    monkeypatch.setattr(mod, "link_probe", probe)
    monkeypatch.setattr(mod, "fetch_roster", fetch)
    sleeps = []

    def sleep(s) -> None:
        sleeps.append(s)
        clock["t"] += s
        if len(sleeps) >= len(outcomes):
            raise Stop

    monkeypatch.setattr(mod.time, "sleep", sleep)
    # The loop waits in stop-poll slices; one fake sleep per whole poll keeps the cycle count and clock in poll units.
    monkeypatch.setattr(mod, "wait_or_stop", lambda seconds, own_start, sleep=None: bool(mod.time.sleep(seconds)))
    log = tmp_path / "roster.log"
    with pytest.raises(Stop):
        mod.main([str(log), "--remote", "example-fleet-pro", "--key", "k"])
    return log.read_text(encoding="utf-8"), sleeps, synced, fetched


def test_far_down_for_n_cycles_then_back_relinks_without_a_trigger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()
    text, sleeps, synced, _ = drive(mod, monkeypatch, tmp_path, [True] + [False] * 6 + [True, True])
    assert text.count("LINK-DOWN") == 1 and text.count("LINK-UP") == 1
    assert len(synced) == 2, "sync must run again once the far box answers"
    assert set(sleeps) == {mod.POLL_SECONDS}
    rec = json.loads((tmp_path / "home" / "unimatrix" / "link-state" / "example-fleet-pro.json").read_text("utf-8"))
    assert rec["state"] == "up"


def test_a_single_failed_poll_is_not_a_link_flap_and_syncing_continues(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()
    text, _, synced, _ = drive(mod, monkeypatch, tmp_path, [True, False, True, True])
    assert "LINK-" not in text and len(synced) == 3


def test_a_raise_in_the_failure_branch_reap_does_not_end_the_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()

    def reap(remote, say, unbacked: bool = False, keep=()) -> None:
        if unbacked:
            raise FileNotFoundError("session file already removed")

    text, _, synced, _ = drive(mod, monkeypatch, tmp_path, [False, False, False, True, True], reap=reap)
    assert "reap failed: FileNotFoundError" in text
    assert text.count("LINK-UP") == 1 and len(synced) == 2


def test_a_raise_that_is_not_in_the_old_catch_list_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()
    monkeypatch.setattr(mod, "mirrorable", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    text, _, _, fetched = drive(mod, monkeypatch, tmp_path, [True, True, True, True])
    assert text.count("ROSTER-STALE example-fleet-pro: boom") == 1 and "LINK-" not in text
    assert len(fetched) >= 2, "a failed roster fetch is retried after its backoff"


def test_backoff_doubles_to_one_poll_multiple_cap_and_resets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()
    P = mod.POLL_SECONDS
    assert [mod.backoff_seconds(n) for n in (1, 2, 3, 4, 9)] == [P, 2 * P, 4 * P, 4 * P, 4 * P]
    assert mod.BACKOFF_CAP_SECONDS % P == 0
    _, sleeps, _, fetched = drive(mod, monkeypatch, tmp_path, [False] * 4 + [True, False, True, True, True, True])
    assert set(sleeps) == {P}, "the link probe keeps its own cadence whatever the roster does"
    assert fetched == [0, P, 3 * P, 7 * P, 8 * P, 9 * P]


def test_drop_session_files_tolerates_a_file_the_bridge_already_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def test_lan_alias_that_answers_tcp_but_fails_identity_is_never_picked(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    checked = []

    def wrong_box(alias):
        checked.append(alias)
        return "not-this-box", "host key mismatch"

    assert mod.pick_remote("box-lan", "box-wg", reachable=lambda a: True, verify=wrong_box) == "box-wg"
    assert checked == ["box-lan"]
    assert mod.pick_remote("box-lan", "box-wg", reachable=lambda a: True, verify=lambda a: ("ok", "")) == "box-lan"
    assert mod.pick_remote("box-lan", "box-wg", reachable=lambda a: False, verify=lambda a: pytest.fail("verified a dead LAN")) == "box-wg"


def test_stable_remote_skips_a_lan_alias_whose_identity_fails_and_keeps_the_verified_address(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    far = mod._box_mux()._far_addresses()
    reads = {"unimatrix.addresses.box": "192.0.2.7"}
    seen = []

    def verify(target):
        seen.append(target)
        return (far.OK, "") if target == "kim@192.0.2.7" else (far.NOT_THIS_BOX, "mismatch")

    got = mod.stable_remote("kim@box", reachable=lambda a: True, fields=lambda a: {"port": "22"},
                            read_key=lambda k: reads.get(k, ""), verify=verify, tcp=lambda a: True, key="k")
    assert got == "kim@192.0.2.7"
    assert seen == ["kim@box-lan", "kim@192.0.2.7"]


def test_a_verified_lan_alias_is_remembered_for_the_ttl_and_a_failed_pinned_one_is_reprobed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()
    far = mod._box_mux()._far_addresses()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    mod._LAN_VERIFIED.clear()
    probes = []
    answers = iter([(far.OK, ""), (far.NOT_THIS_BOX, "mismatch"), (far.OK, "")])
    monkeypatch.setattr(far, "make_verify", lambda box, key=None, **k: lambda t: probes.append((box, key, t)) or next(answers))
    pinned = lambda k: "SHA256:abc" if k.startswith("unimatrix.hostkey.") else ""
    assert mod.lan_verified("box-lan", read_key=pinned, key="k")
    assert mod.lan_verified("box-lan", read_key=pinned, key="k")
    assert probes == [("box", "k", "box-lan")]
    assert not mod.lan_verified("other-lan", read_key=pinned, key="k")
    assert not mod._LAN_VERIFIED.get("other-lan")
    assert mod.lan_verified("other-lan", read_key=pinned, key="k") and len(probes) == 3


def test_a_peer_with_no_pinned_identity_stays_on_the_tcp_prefilter(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    far = mod._box_mux()._far_addresses()
    monkeypatch.setattr(far, "make_verify", lambda *a, **k: pytest.fail("spawned a probe with nothing pinned"))
    assert mod.lan_verified("box-lan", read_key=lambda k: "", key="k")


def test_roster_once_verifies_the_lan_alias_with_the_explicit_key(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load()
    seen = {}

    def pick(lan, tunnel=None, reachable=None, verify=None, key=None):
        seen["key"] = key
        raise OSError("stop")

    monkeypatch.setattr(mod, "pick_remote", pick)
    monkeypatch.setattr(mod, "ensure_key_loaded", lambda k: None)
    with pytest.raises(OSError):
        mod._roster_once("box-lan", "explicit-key", "box-wg")
    assert seen["key"] == "explicit-key"


def test_a_reset_that_reaps_a_mirror_redials_on_the_next_tick_not_after_the_backoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = load()
    P = mod.POLL_SECONDS
    calls = []

    def reap(remote, say, unbacked: bool = False, keep=(), bridges=None):
        if not unbacked:
            return []
        calls.append(1)
        return [(4242, "group-em-de")] if len(calls) == 3 else []  # the poll-2 failure (0 ok, P fail, 2P fail)

    mod._DIALS["example-fleet-pro"] = (0.0, None, "roster rc=255: Connection reset")
    _, _, _, fetched = drive(mod, monkeypatch, tmp_path, [True, False, False, False, False, True], reap=reap)
    # Plain backoff after the 2P failure idles to 4P; the reaped mirror must be re-dialled at 3P.
    assert fetched[:4] == [0, P, 2 * P, 3 * P]
    assert "example-fleet-pro" not in mod._DIALS, "the Windows replay cap must not replay the reset failure"
