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
