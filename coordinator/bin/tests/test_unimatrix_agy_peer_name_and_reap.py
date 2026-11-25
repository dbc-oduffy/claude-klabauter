"""Behavior pins for the agy SessionStart peer: named from agy's launch dir, dead or replaced legs' rows reaped."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
CONV = "0cf351a3-6538-4a9b-814c-52b1b75a596c"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sess = load("unimatrix-agy-hook-sessionstart")


@pytest.fixture
def user(tmp_path, monkeypatch):
    """A fake user home (so ~/.gemini resolves under tmp_path) and an isolated settings home."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    settings = tmp_path / "settings"
    (settings / "unimatrix" / "agy-legs").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    return home


def legs(tmp_path):
    return tmp_path / "settings" / "unimatrix" / "agy-legs"


def peers_file(tmp_path):
    return tmp_path / "settings" / "unimatrix" / "peers.json"


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def write_leg(tmp_path, conv, **rec):
    (legs(tmp_path) / f"{conv}.json").write_text(json.dumps({"conversation": conv, **rec}))


def test_payload_cwd_prefers_workspace_over_cwd(user):
    assert sess.payload_cwd({"cwd": "/c", "workspacePaths": ["/w"]}) == "/w"
    assert sess.payload_cwd({"cwd": "/c", "workspacePaths": []}) == "/c"
    assert sess.payload_cwd({"cwd": "/c"}) == "/c"


def test_payload_cwd_skips_a_dir_inside_the_agy_config_home(user):
    gemini = user / ".gemini" / "config"
    ws = user / "repo" / "scratch" / "agy-proof"
    assert sess.payload_cwd({"cwd": str(gemini), "workspacePaths": [str(ws)]}) == str(ws)
    assert sess.payload_cwd({"cwd": str(ws), "workspacePaths": [str(gemini)]}) == str(ws)
    assert sess.payload_cwd({"cwd": str(gemini), "workspacePaths": []}) is None
    assert sess.payload_cwd({"cwd": str(user / ".gemini")}) is None


def test_config_home_prefix_is_a_path_boundary(user):
    sibling = user / ".gemini-other" / "x"
    assert sess.payload_cwd({"cwd": str(sibling)}) == str(sibling)


def test_peer_is_named_from_the_workspace_repo_not_the_config_dir(user, tmp_path):
    repo = user / "projrepo"
    (repo / ".git").mkdir(parents=True)
    ws = repo / "scratch" / "agy-proof"
    ws.mkdir(parents=True)
    cwd = sess.payload_cwd({"cwd": str(user / ".gemini" / "config"), "workspacePaths": [str(ws)]})
    write_leg(tmp_path, CONV, pid=os.getpid(), cwd=cwd, mode="pending")
    assert sess.bind_peer(CONV, legs(tmp_path) / f"{CONV}.json") is not None
    rows = json.loads(peers_file(tmp_path).read_text())
    rows = rows["peers"] if isinstance(rows, dict) else rows
    names = [r["name"] for r in rows if r.get("target") == CONV]
    assert len(names) == 1 and names[0].startswith("agy-projrepo-") and "config" not in names[0]


def test_registration_reaps_dead_replaced_and_unbacked_agy_rows_and_keeps_the_live_one(user, tmp_path):
    gone, dead, reused, other = "gone-conv", "dead-conv", "reused-conv", "live-other"
    write_leg(tmp_path, dead, pid=dead_pid(), procStart="x")
    write_leg(tmp_path, reused, pid=os.getpid(), procStart="not-this-process-start")
    write_leg(tmp_path, other, pid=os.getpid())
    write_leg(tmp_path, CONV, pid=os.getpid(), cwd=str(user))
    peers_file(tmp_path).write_text(json.dumps([
        {"name": "agy-gone", "vendor": "agy", "target": gone},
        {"name": "agy-dead", "vendor": "agy", "target": dead},
        {"name": "agy-reused", "vendor": "agy", "target": reused},
        {"name": "agy-other", "vendor": "agy", "target": other},
        {"name": "codex-x", "vendor": "codex", "target": gone},
    ]))
    sess.bind_peer(CONV, legs(tmp_path) / f"{CONV}.json")
    rows = json.loads(peers_file(tmp_path).read_text())
    rows = rows["peers"] if isinstance(rows, dict) else rows
    kept = {r["name"] for r in rows}
    assert "agy-gone" not in kept and "agy-dead" not in kept and "agy-reused" not in kept
    assert {"agy-other", "codex-x"} <= kept
    assert any(r.get("target") == CONV for r in rows)


def test_the_row_just_bound_survives_even_when_its_leg_pid_is_dead(user, tmp_path):
    write_leg(tmp_path, CONV, pid=dead_pid(), cwd=str(user))
    sess.bind_peer(CONV, legs(tmp_path) / f"{CONV}.json")
    rows = json.loads(peers_file(tmp_path).read_text())
    rows = rows["peers"] if isinstance(rows, dict) else rows
    assert [r["target"] for r in rows] == [CONV]


def test_register_without_a_sidecar_module_starts_the_pending_leg(user, tmp_path, monkeypatch):
    real = sess.load_sibling
    launch = real("unimatrix-agy-hook-launch")
    started = []

    def load(name):
        if name == "unimatrix-agy-sidecar":
            raise FileNotFoundError(name)
        return launch if name == "unimatrix-agy-hook-launch" else real(name)

    monkeypatch.setattr(sess, "load_sibling", load)
    monkeypatch.setattr(launch, "start", lambda req, roster, state: started.append(req))
    sess.register({"conversationId": CONV, "cwd": str(user)}, 5)
    assert started and started[0]["conversationId"] == CONV


def test_a_record_with_no_pid_or_unreadable_is_not_proof_of_death(user, tmp_path):
    liveness = sess.load_sibling("unimatrix-liveness")
    (legs(tmp_path) / "nopid.json").write_text(json.dumps({"agy_pid": 1}))
    (legs(tmp_path) / "torn.json").write_text("{not json")
    peers_file(tmp_path).write_text(json.dumps([
        {"name": "a-nopid", "vendor": "agy", "target": "nopid"},
        {"name": "a-torn", "vendor": "agy", "target": "torn"},
        {"name": "a-absent", "vendor": "agy", "target": "absent"},
    ]))
    assert liveness.agy_peer_rows_orphaned() == [("a-absent", "absent")]


def test_reap_never_raises_when_the_peers_file_is_unreadable(user, tmp_path):
    peers_file(tmp_path).write_text("{not json")
    hook = sess.load_sibling("unimatrix-codex-hook-sessionstart")
    assert sess.reap_dead_peers(hook, peers_file(tmp_path)) == []


router = load("unimatrix-peer-router")


@pytest.fixture
def live_agy(monkeypatch):
    """Every leg record reads live, every agy session alive, every presence lock held."""
    class Liveness:
        @staticmethod
        def agy_session_state(conv, rec):
            return "alive"
    monkeypatch.setattr(router, "leg_record_live", lambda rec: True)
    monkeypatch.setattr(router, "lock_held", lambda path: True)
    monkeypatch.setattr(router, "_liveness", lambda: Liveness)


def discover(tmp_path):
    presence = tmp_path / "presence"
    presence.mkdir(exist_ok=True)
    r = router.Router([], sessions_dir=tmp_path / "s", state_dir=tmp_path / "st", endpoint_dir=tmp_path / "e",
                      adapters={"agy": None}, legs_dir=legs(tmp_path), presence_dir=presence)
    return r._live_discovered()


def agy_names(found):
    return sorted(n for n, p in found.items() if p.get("target") == CONV)


def test_a_leg_cwd_in_a_repo_names_exactly_one_face_stable_across_a_router_restart(user, tmp_path, live_agy):
    repo = user / "projrepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "scratch" / "agy-proof"
    sub.mkdir(parents=True)
    write_leg(tmp_path, CONV, cwd=str(sub))
    (tmp_path / "presence").mkdir()
    (tmp_path / "presence" / f"{CONV}.lock").write_text("")
    first, second = agy_names(discover(tmp_path)), agy_names(discover(tmp_path))
    assert len(first) == 1 and first[0].startswith("agy-projrepo-") and first[0].endswith("-on-" + router.readable_peer_name("agy", CONV).rsplit("-on-", 1)[1])
    assert first == second


@pytest.mark.parametrize("sub", ["config", ""])
def test_a_config_home_cwd_never_names_a_face_agy_config(user, tmp_path, live_agy, sub):
    cfg = user / ".gemini" / sub if sub else user / ".gemini"
    write_leg(tmp_path, CONV, cwd=str(cfg))
    found = discover(tmp_path)
    assert len(agy_names(found)) == 1
    name = agy_names(found)[0]
    assert "config" not in name and name == router.readable_peer_name("agy", CONV)
    assert "cwd" not in found[name]


def test_a_config_home_name_matches_the_no_cwd_name_so_no_second_face_appears(user, tmp_path, live_agy):
    write_leg(tmp_path, CONV, cwd=str(user / ".gemini" / "config"))
    (tmp_path / "presence").mkdir()
    (tmp_path / "presence" / f"{CONV}.lock").write_text("")
    assert len(agy_names(discover(tmp_path))) == 1


def test_the_config_home_rule_is_agy_only_and_a_path_boundary(user):
    cfg = str(user / ".gemini" / "config")
    assert router.readable_peer_name("codex", CONV, cfg).startswith("codex-config-")
    sibling = str(user / ".gemini-other" / "x")
    assert router.readable_peer_name("agy", CONV, sibling).startswith("agy-x-")
