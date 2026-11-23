"""A reply to a delivered far sender resolves to exactly one local row that reaches it, for agy, grok and codex alike.

Drives the real chain on fixtures: the far relay's roster export, this box's mirrorable() and mirror(),
the roster listing and agy-face reap that decide whether the mirror row survives, then the receiving
relay's local_reply_address on a frame from each far sender.
"""
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
FAR_REMOTE = "me@Machine-b.local"
FAR_IP = "192.168.0.50"
FAR_BOX = "machine-b"


def far_sock(ch):
    return f"/tmp/cc-socks/cc-msg-{ch * 32}.sock"


def load(name):
    spec = importlib.util.spec_from_file_location("reply_route_" + name, BIN / f"unimatrix-{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD_MS = int((time.time() - 3600) * 1000)

FAR_SESSIONS = {
    "agy": {"pid": 4101, "name": "agy-0b1c2d3e-1111-2222-3333-444455556666", "vendor": "agy",
            "sessionId": "peer-agy-0b1c2d3e", "cwd": "/far/box/coordinator-content-repo", "box": FAR_BOX,
            "unimatrixRouterPid": 4101, "messagingSocketPath": far_sock("a"), "startedAt": OLD_MS},
    "grok": {"pid": 4102, "name": "grok-coordinator-content-repo", "vendor": "grok", "sessionId": "sid-grok",
             "cwd": "/far/box/coordinator-content-repo", "box": FAR_BOX, "messagingSocketPath": far_sock("b"),
             "startedAt": OLD_MS},
    "codex": {"pid": 4103, "name": "codex-coordinator-content-repo", "vendor": "codex", "sessionId": "peer-codex-1",
              "cwd": "/far/box/coordinator-content-repo", "box": FAR_BOX, "codexHolder": "live",
              "messagingSocketPath": far_sock("c"), "startedAt": OLD_MS},
}


@pytest.fixture
def chain(tmp_path, monkeypatch):
    """Far export -> near mirror files, with process liveness fixed alive so only routing logic decides."""
    near_sessions = tmp_path / "near" / "sessions"
    near_sessions.mkdir(parents=True)
    settings = tmp_path / "settings"
    (settings / "unimatrix").mkdir(parents=True)
    # The near box lists real peers of its own, so peers.json exists and holds no row for a far name.
    (settings / "unimatrix" / "peers.json").write_text(
        json.dumps([{"name": "agy-local-peer", "target": "local-conv", "vendor": "agy"}]), encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "near"))

    relay, roster = load("relay"), load("roster")
    far_rows = [(d["pid"], dict(d), f"{d['pid']}.key") for d in FAR_SESSIONS.values()]
    monkeypatch.setattr(relay, "entries", lambda: iter(far_rows))
    monkeypatch.setattr(relay, "codex_export_refused", lambda d, cache: False)
    return SimpleNamespace(relay=relay, roster=roster, sessions=near_sessions, monkeypatch=monkeypatch,
                           tmp=tmp_path)


def export_far_rows(chain, capsys):
    capsys.readouterr()
    chain.relay.roster()
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def mirror_far_rows(chain, rows):
    """Run this box's pull filter and mirror() on the exported rows; returns {family: written mirror entry}."""
    roster, mp = chain.roster, chain.monkeypatch
    pids = iter(range(31337, 31400))
    mp.setattr(roster, "sessions_dir", lambda: str(chain.sessions))
    mp.setattr(roster, "spawn", lambda cmd: SimpleNamespace(pid=next(pids)))
    mp.setattr(roster, "proc_start", lambda pid: "local-start")
    mp.setattr(roster, "remote_ip", lambda remote: FAR_IP)
    mp.setattr(roster.time, "sleep", lambda seconds: None)
    mp.setattr(roster, "_is_win", lambda platform=None: True)
    mp.setattr(roster._liveness(), "entry_live", lambda d: True)
    mp.setattr(roster._liveness(), "supervisor_live", lambda d: True)
    wanted = roster.mirrorable(rows, set(), set())
    cfg = SimpleNamespace(remote=FAR_REMOTE, key="key", tunnel_remote=None, log=str(chain.tmp / "roster.log"))
    for row in wanted.values():
        roster.mirror(row, cfg, lambda *a: None, scan=[])
    written = {}
    for f in sorted(chain.sessions.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        written[d["vendor"]] = d
    return written


def reply_frame(family):
    sender = FAR_SESSIONS[family]
    src = sender["messagingSocketPath"]
    content = f'<cross-session-message from="uds:{src}" from-name="{sender["name"]}">\nhi\n</cross-session-message>'
    return json.dumps({"from": "uds:" + src, "message": {"role": "user", "content": content}}).encode() + b"\n"


def point_relay_at_mirrors(chain, client):
    mirrors = [(d["pid"], d, f"{d['pid']}.key")
               for d in (json.loads(f.read_text(encoding="utf-8")) for f in sorted(chain.sessions.glob("*.json")))]
    chain.monkeypatch.setattr(chain.relay, "entries", lambda: iter(mirrors))
    chain.monkeypatch.setattr(chain.relay, "client_address", lambda: client)


@pytest.mark.parametrize("family", ["agy", "grok", "codex"])
def test_far_sender_is_exported_and_survives_this_boxs_mirror_filter(chain, capsys, family):
    rows = export_far_rows(chain, capsys)
    assert FAR_SESSIONS[family]["messagingSocketPath"] in [r["entry"]["messagingSocketPath"] for r in rows]
    wanted = chain.roster.mirrorable(rows, set(), set())
    assert FAR_SESSIONS[family]["messagingSocketPath"] in wanted


@pytest.mark.parametrize("family", ["agy", "grok", "codex"])
def test_the_far_senders_mirror_row_is_listed(chain, capsys, family):
    """The roster listing is what SendMessage resolves a reply's name against: an unlisted row is no route."""
    written = mirror_far_rows(chain, export_far_rows(chain, capsys))
    assert family in written
    rows = chain.roster.session_entries()
    mirror_row = next(d for d in rows if d.get("vendor") == family)
    assert chain.roster.listed_row(mirror_row, rows), f"{family} mirror row dropped from the listing"


@pytest.mark.parametrize("family", ["agy", "grok", "codex"])
def test_the_far_senders_mirror_row_survives_the_agy_face_reap(chain, capsys, family):
    mirror_far_rows(chain, export_far_rows(chain, capsys))
    roster = chain.roster
    mirror_row = next(d for d in roster.session_entries() if d.get("vendor") == family)
    chain.monkeypatch.setattr(roster, "tree_kill_many", lambda pids: None)
    chain.monkeypatch.setattr(roster, "drop_session_files",
                              lambda pid: (chain.sessions / f"{pid}.json").unlink(missing_ok=True))
    roster.reap_unbacked_agy_faces(lambda *a: None)
    assert (chain.sessions / f"{mirror_row['pid']}.json").exists(), f"{family} mirror row reaped"


@pytest.mark.parametrize("family", ["agy", "grok", "codex"])
def test_reply_to_a_delivered_far_sender_resolves_to_exactly_one_listed_local_row(chain, capsys, family):
    written = mirror_far_rows(chain, export_far_rows(chain, capsys))
    assert family in written
    rows = chain.roster.session_entries()
    assert chain.roster.listed_row(next(d for d in rows if d.get("vendor") == family), rows)
    point_relay_at_mirrors(chain, FAR_IP)
    frame = json.loads(chain.relay.local_reply_address(reply_frame(family)))
    assert frame["from"] == "uds:" + written[family]["messagingSocketPath"]
    assert frame["from"] != "uds:" + FAR_SESSIONS[family]["messagingSocketPath"]


@pytest.mark.parametrize("family", ["agy", "grok", "codex"])
def test_reply_resolution_does_not_depend_on_which_family_sent(chain, capsys, family):
    """Each family's candidate set is exactly its own row: a sibling mirror never answers for it."""
    written = mirror_far_rows(chain, export_far_rows(chain, capsys))
    point_relay_at_mirrors(chain, FAR_IP)
    out = json.loads(chain.relay.local_reply_address(reply_frame(family)))["from"]
    others = {"uds:" + d["messagingSocketPath"] for fam, d in written.items() if fam != family}
    assert out not in others



def test_a_reply_that_cannot_resolve_is_left_unchanged_not_misrouted(chain, capsys):
    mirror_far_rows(chain, export_far_rows(chain, capsys))
    point_relay_at_mirrors(chain, "10.9.9.9")
    line = reply_frame("agy")
    assert chain.relay.local_reply_address(line) == line
