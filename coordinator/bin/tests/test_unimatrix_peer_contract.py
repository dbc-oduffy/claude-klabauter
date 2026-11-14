"""Behavior pins for the unimatrix peer contract module."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_contract", BIN / "unimatrix-peer-contract.py")
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def write(tmp_path, data):
    p = tmp_path / "peers.json"
    p.write_text(json.dumps(data))
    return p


def test_load_peers_accepts_wrapped_and_bare_forms(tmp_path):
    rows = [{"name": "codex-a", "vendor": "codex", "target": "thr-1"},
            {"name": "agy-a", "vendor": "agy", "target": "conv-1"}]
    assert pc.load_peers(write(tmp_path, {"peers": rows})) == rows
    assert pc.load_peers(write(tmp_path, rows)) == rows


def test_load_peers_round_trips_codex_holder_and_drops_malformed(tmp_path):
    rows = [{"name": "a", "vendor": "codex", "target": "t1", "codexPid": 5, "codexProcStart": "s"},
            {"name": "b", "vendor": "codex", "target": "t2", "codexPid": True, "codexProcStart": "s"},
            {"name": "c", "vendor": "codex", "target": "t3", "codexPid": "5"}]
    got = pc.load_peers(write(tmp_path, rows))
    assert got[0] == rows[0]
    assert "codexPid" not in got[1] and "codexProcStart" not in got[1]
    assert "codexPid" not in got[2]


def test_load_peers_accepts_grok_vendor(tmp_path):
    rows = [{"name": "grok-a", "vendor": "grok", "target": "sess-1"}]
    assert "grok" in pc.VENDORS
    assert pc.load_peers(write(tmp_path, rows)) == rows


def test_load_peers_refuses_duplicate_name(tmp_path):
    row = {"name": "x", "vendor": "codex", "target": "t"}
    with pytest.raises(pc.PeerConfigError, match="duplicate"):
        pc.load_peers(write(tmp_path, [row, dict(row, vendor="agy")]))


def test_load_peers_refuses_unknown_vendor(tmp_path):
    with pytest.raises(pc.PeerConfigError, match="vendor"):
        pc.load_peers(write(tmp_path, [{"name": "x", "vendor": "gemini", "target": "t"}]))


@pytest.mark.parametrize("missing", ["name", "vendor", "target"])
def test_load_peers_refuses_missing_or_blank_field(tmp_path, missing):
    row = {"name": "x", "vendor": "codex", "target": "t"}
    row[missing] = " "
    with pytest.raises(pc.PeerConfigError):
        pc.load_peers(write(tmp_path, [row]))
    del row[missing]
    with pytest.raises(pc.PeerConfigError):
        pc.load_peers(write(tmp_path, [row]))


def test_load_peers_refuses_non_list(tmp_path):
    with pytest.raises(pc.PeerConfigError):
        pc.load_peers(write(tmp_path, {"peers": "nope"}))


def test_build_frame_matches_prototype_shape():
    f = pc.build_frame("/tmp/a.sock", "router", "hello", msg_id="m1")
    assert f == {
        "msgV": 1, "msg_id": "m1", "type": "user", "priority": "next", "from": "uds:/tmp/a.sock",
        "message": {"role": "user", "content":
                    '<cross-session-message from="uds:/tmp/a.sock" from-name="router">\nhello\n</cross-session-message>'},
    }
    assert pc.build_frame("/s", "n", "t")["msg_id"]


def test_parse_inbound_roundtrips_build_frame():
    f = pc.build_frame("/tmp/a.sock", "em-one", "line1\nline2")
    assert pc.parse_inbound(f) == ("/tmp/a.sock", "em-one", "line1\nline2")


def test_parse_inbound_unwrapped_content_falls_back_to_endpoint():
    f = {"from": "uds:/tmp/b.sock", "message": {"content": "plain"}}
    assert pc.parse_inbound(f) == ("/tmp/b.sock", "/tmp/b.sock", "plain")


def test_parse_inbound_tolerates_empty_frame():
    assert pc.parse_inbound({}) == ("", "", "")


def test_label():
    assert pc.label("codex-a", "hi") == "[codex-a] hi"


def test_stamp_adds_provenance_and_from_name_without_mutating_input():
    f = pc.build_frame("/s", "router", "x", msg_id="m9")
    out = pc.stamp(f, "codex-a", "codex", "thr-1", 4242)
    assert out["provenance"] == {"peer": "codex-a", "vendor": "codex", "target": "thr-1",
                                 "router_pid": 4242, "msg_id": "m9"}
    assert set(out["provenance"]) == set(pc.PROVENANCE_FIELDS)
    assert out["from_name"] == "codex-a"
    assert "provenance" not in f and "from_name" not in f


def test_main_usage_error_and_invalid_file_exit_nonzero(tmp_path, capsys):
    assert pc.main([]) == 2
    assert "usage" in capsys.readouterr().err
    assert pc.main([str(tmp_path / "missing.json")]) == 1


@pytest.mark.parametrize("script", ["unimatrix-peer-contract.py", "unimatrix-peer-router.py"])
def test_import_runs_no_compile_io_or_subprocess(script, monkeypatch):
    import builtins
    import re
    import subprocess

    def trip(name):
        def _raise(*a, **k):
            raise AssertionError(f"import-time {name}")
        return _raise

    monkeypatch.setattr(re, "compile", trip("re.compile"))
    monkeypatch.setattr(builtins, "open", trip("open"))
    monkeypatch.setattr(subprocess, "Popen", trip("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "run", trip("subprocess.run"))
    monkeypatch.setattr(Path, "resolve", trip("Path.resolve"))
    spec = importlib.util.spec_from_file_location("inert_probe", BIN / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert callable(mod.main)


def test_agent_row_plain_claude_entry_is_claude_on_local_box():
    row = pc.agent_row({"name": "em-1", "sessionId": "abc"}, "mac")
    assert row == {"name": "em-1", "box": "mac", "vendor": "claude", "this_box": True}


def test_agent_row_this_box_flag_both_sides():
    assert pc.agent_row({"name": "a", pc.BOX_FIELD: "mac"}, "mac")["this_box"] is True
    assert pc.agent_row({"name": "b", pc.BOX_FIELD: "machine-a"}, "mac")["this_box"] is False


def test_self_row_matches_session_id_exactly_never_prefix():
    entries = [{"name": "claude-klabauter-a1", "sessionId": "8776ca63-188a", pc.BOX_FIELD: "machine-a"},
               {"name": "other", "sessionId": "zzz"}]
    row = pc.self_row(entries, "8776ca63-188a", "machine-a")
    assert row["name"] == "claude-klabauter-a1" and row["box"] == "machine-a" and row["this_box"] is True
    assert pc.self_row(entries, "8776ca63", "machine-a") is None
    assert pc.self_row(entries, "", "machine-a") is None
    assert pc.self_row([], "zzz", "machine-a") is None


def test_split_by_box_partitions_and_excludes_self():
    rows = [pc.agent_row({"name": n, pc.BOX_FIELD: b}, "machine-a")
            for n, b in [("me", "machine-a"), ("a", "machine-a"), ("m", "macmini"), ("d", "machine-b")]]
    same, other = pc.split_by_box(rows, "machine-a", self_name="me")
    assert [r["name"] for r in same] == ["a"]
    assert [r["name"] for r in other] == ["m", "d"]
    same, other = pc.split_by_box(rows, "machine-a")
    assert [r["name"] for r in same] == ["me", "a"]


def test_build_frame_from_box_emits_attribute_and_parses_back():
    f = pc.build_frame("/s", "me", "hi", msg_id="m1", from_box="machine-a")
    assert 'from-name="me" from-box="machine-a">' in f["message"]["content"]
    assert pc.parse_inbound(f) == ("/s", "me", "hi")
    assert "from-box" not in pc.build_frame("/s", "me", "hi")["message"]["content"]


def test_stamp_from_box_adds_provenance_box_only_when_given():
    f = pc.build_frame("/s", "r", "x", msg_id="m9")
    assert pc.stamp(f, "p", "codex", "t", 1, from_box="machine-a")["provenance"]["box"] == "machine-a"
    assert "box" not in pc.stamp(f, "p", "codex", "t", 1)["provenance"]


def test_agent_row_face_stamp_gives_vendor():
    row = pc.agent_row({"name": "cx", "sessionId": "peer-cx", pc.VENDOR_FIELD: "codex"}, "mac")
    assert row["vendor"] == "codex"


def test_agent_row_mirrored_entry_keeps_its_box():
    row = pc.agent_row({"name": "cx", pc.BOX_FIELD: "machine-b"}, "mac")
    assert row["box"] == "machine-b"


def test_agent_row_unstamped_peer_face_is_peer():
    assert pc.agent_row({"name": "x", "sessionId": "peer-x"}, "mac")["vendor"] == "peer"


@pytest.mark.parametrize("raw,want", [("example-operator@Machine-b.local", "machine-b"), ("Mac-mini-lan", "mac-mini"),
                                      ("machine-a", "machine-a"), ("10.0.0.5", "10.0.0.5")])
def test_box_id_normalises(raw, want):
    assert pc.box_id(raw) == want


def test_local_box_is_box_id_of_hostname(monkeypatch):
    monkeypatch.setattr(pc.socket, "gethostname", lambda: "Mac-Mini-lan.local")
    assert pc.local_box() == "mac-mini"
