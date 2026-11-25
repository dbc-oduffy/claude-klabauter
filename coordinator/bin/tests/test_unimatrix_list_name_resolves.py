"""Every name `unimatrix-send.py --list` prints, and every roster box-alias form, resolves with --to to that session.

Default path: the router's roster_rows (what --list prints) against its resolve_send_target (what --to runs)
over a tmp_path registry of far mirror rows shaped like the roster writes them (name / box /
unimatrixFarName / unimatrixBox / unimatrixRemote). Pure string matching; no host name is read.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router = _load("unimatrix-peer-router.py", "list_name_router")
roster = _load("unimatrix-roster.py", "list_name_roster")

CODEX_STEM = "coordinator-content-repo-codex-machine-b-lo-25"
CODEX_SID = "01a1fab0-66ce-7073-b564-27f8c32fc2a7"


def mirror(name, far, remote, box_field, box_id, sock, ep, sid, **extra):
    return {"pid": os.getpid(), "name": name, "unimatrixFarName": far, "messagingSocketPath": ep,
            "macSocketPath": sock, "farPid": 4242, "unimatrixRemote": remote, "box": box_field,
            "unimatrixBox": box_id, "vendor": "claude", "sessionId": sid, "target": None,
            "startedAt": 1, **roster.supervisor_fields(), **roster.far_seen_fields(), **extra}


def codex_row(stem, box_label):
    pid = os.getpid()
    return mirror(f"{stem}.{box_label}", stem, "example-operator@Machine-b.local", "Machine-b.local", "machine-b",
                  "/tmp/cc-socks/codex.sock", "/tmp/cc-socks/mirror-codex.sock", f"peer-{stem}-1",
                  vendor="codex", target=CODEX_SID, session_id=CODEX_SID, provider="codex", schema_version=1,
                  address_version=1, aliases=[], codexHolder="live", farPidDomain="darwin",
                  procStart=roster.proc_start(pid), pidDomain=roster.pid_domain())


def make_router(tmp_path, rows):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for key, row in rows.items():
        (sessions / f"{key}.json").write_text(json.dumps(row), encoding="utf-8")
    return router.Router([], sessions_dir=sessions, state_dir=tmp_path / "state", endpoint_dir=tmp_path / "eps")


def listed(r):
    """The names `--list` prints for far rows, as the router's list reply carries them."""
    _, rows = r.roster_rows()
    return [row["name"] for row in rows]


def reaches(r, query, endpoint):
    got = r.resolve_send_target(query)
    return got["ok"] and got["endpoint"] == endpoint


@pytest.fixture(autouse=True)
def _endpoints_exist(monkeypatch):
    monkeypatch.setattr(router._transport(), "endpoint_exists", lambda ep: True)


def test_machine_b_listed_name_and_roster_alias_forms_reach_the_listed_session(tmp_path):
    row = codex_row(CODEX_STEM, "Machine-b")
    r = make_router(tmp_path, {"d": row})
    (name,) = listed(r)
    assert name == f"{CODEX_STEM}.Machine-b"
    for query in (name, CODEX_STEM, f"{CODEX_STEM}.Machine-b.local", f"{CODEX_STEM}.machine-b",
                  f"{CODEX_STEM}.machine-b-lan", f"{CODEX_STEM}.machine-b.local"):
        assert reaches(r, query, row["messagingSocketPath"]), query


def test_mac_mini_listed_name_reaches_the_live_session_past_a_hidden_lan_twin(tmp_path):
    """The `-lan` alias row of the same far session is hidden from the listing; both its name and the listed one reach the live row."""
    sock = "/tmp/cc-socks/388.sock"
    live = mirror("holo-7e.Mac-mini", "holo-7e", "example-operator@Mac-mini.local", "mac-mini", "mac-mini", sock,
                  "/tmp/cc-socks/live.sock", "sid-7e")
    stale = mirror("holo-7e.mac-mini-lan", "holo-7e", "mac-mini-lan", "mac-mini", "mac-mini", sock,
                   "/tmp/cc-socks/stale.sock", "sid-7e", unimatrixFarSeen=1)
    r = make_router(tmp_path, {"live": live, "stale": stale})
    assert listed(r) == ["holo-7e.Mac-mini"]
    for query in ("holo-7e.Mac-mini", "holo-7e.mac-mini-lan", "holo-7e.mac-mini", "holo-7e"):
        assert reaches(r, query, live["messagingSocketPath"]), query
        assert r.last_resolved_row[0] == "holo-7e.Mac-mini"


def test_mac_mini_lan_alias_form_reaches_a_row_listed_under_the_local_name(tmp_path):
    live = mirror("proj-f3.Mac-mini", "proj-f3", "example-operator@Mac-mini.local", "mac-mini", "mac-mini",
                  "/tmp/cc-socks/1.sock", "/tmp/cc-socks/f3.sock", "sid-f3")
    r = make_router(tmp_path, {"live": live})
    for query in (listed(r)[0], "proj-f3.mac-mini-lan", "proj-f3.Mac-mini.local", "proj-f3.mac-mini"):
        assert reaches(r, query, live["messagingSocketPath"]), query


def test_gipsy_linux_lan_label_resolves_by_its_listed_name_and_box_forms(tmp_path):
    row = mirror("proj-dc.gipsy-lan", "proj-dc", "gipsy-lan", "gipsy", "gipsy", "/tmp/cc-socks/2.sock",
                 "/tmp/cc-socks/dc.sock", "sid-dc")
    r = make_router(tmp_path, {"g": row})
    for query in (listed(r)[0], "proj-dc.gipsy", "proj-dc.gipsy.local", "proj-dc"):
        assert reaches(r, query, row["messagingSocketPath"]), query


def test_every_listed_far_name_resolves_to_its_own_session(tmp_path):
    rows = {
        "d": codex_row(CODEX_STEM, "Machine-b"),
        "m": mirror("a-1.Mac-mini", "a-1", "example-operator@Mac-mini.local", "mac-mini", "mac-mini", "/tmp/cc-socks/3.sock",
                    "/tmp/cc-socks/a.sock", "sid-a"),
        "g": mirror("b-2.gipsy-lan", "b-2", "gipsy-lan", "gipsy", "gipsy", "/tmp/cc-socks/4.sock",
                    "/tmp/cc-socks/b.sock", "sid-b"),
    }
    r = make_router(tmp_path, rows)
    by_name = {row["name"]: row["messagingSocketPath"] for row in rows.values()}
    assert sorted(listed(r)) == sorted(by_name)
    for name, endpoint in by_name.items():
        assert reaches(r, name, endpoint), name


def test_two_live_sessions_behind_one_name_still_refuse(tmp_path):
    one = mirror("dup-1.Mac-mini", "dup-1", "example-operator@Mac-mini.local", "mac-mini", "mac-mini", "/tmp/cc-socks/5.sock",
                 "/tmp/cc-socks/one.sock", "sid-one")
    two = mirror("dup-1.Mac-mini", "dup-1", "example-operator@Mac-mini.local", "mac-mini", "mac-mini", "/tmp/cc-socks/6.sock",
                 "/tmp/cc-socks/two.sock", "sid-two")
    r = make_router(tmp_path, {"one": one, "two": two})
    for query in ("dup-1.Mac-mini", "dup-1.mac-mini-lan"):
        got = r.resolve_send_target(query)
        assert not got["ok"] and "no unique live session" in got["error"], query


def test_a_hidden_row_of_a_different_session_is_never_redirected_to_a_listed_one(tmp_path):
    live = mirror("x-1.Mac-mini", "x-1", "example-operator@Mac-mini.local", "mac-mini", "mac-mini", "/tmp/cc-socks/7.sock",
                  "/tmp/cc-socks/live.sock", "sid-live")
    other = mirror("x-1.mac-mini-lan", "x-1", "mac-mini-lan", "mac-mini", "mac-mini", "/tmp/cc-socks/8.sock",
                   "/tmp/cc-socks/other.sock", "sid-other", unimatrixFarSeen=1)
    r = make_router(tmp_path, {"live": live, "other": other})
    got = r.resolve_send_target("x-1.mac-mini-lan")
    assert not got["ok"]


@pytest.mark.parametrize("query", ["a-1.otherbox", "a-1.Mac", "a-", "a-1.Mac-mini.extra"])
def test_unknown_or_partial_names_are_never_matched_loosely(tmp_path, query):
    row = mirror("a-1.Mac-mini", "a-1", "example-operator@Mac-mini.local", "mac-mini", "mac-mini", "/tmp/cc-socks/9.sock",
                 "/tmp/cc-socks/a.sock", "sid-a")
    r = make_router(tmp_path, {"a": row})
    assert not r.resolve_send_target(query)["ok"]
