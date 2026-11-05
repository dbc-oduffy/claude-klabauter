"""A node on a consumer box (published plugin + klabauter only) resolves through the installed engine."""

import ast
import re
import sys
from pathlib import Path

import pytest

from coordinator_core.group_em import box_registry, node_transport, remote_identity, remote_roster

GROUP_EM = Path(node_transport.__file__).resolve().parent
SEAM_MODULES = ("node_transport.py", "remote_roster.py", "remote_identity.py")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")


def _allowed_ipv4(octets: tuple) -> bool:
    a, b, c, _ = octets
    if a == 127:
        return True
    return (a, b, c) in {(192, 0, 2), (198, 51, 100), (203, 0, 113)}


def _stray_ipv4(text: str) -> list:
    out = []
    for m in _IPV4.finditer(text):
        octets = tuple(int(g) for g in m.groups())
        if all(o <= 255 for o in octets) and not _allowed_ipv4(octets):
            out.append(m.group(0))
    return out


@pytest.fixture
def consumer_home(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    (home / "state" / "group-em").mkdir(parents=True)
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.chdir(work)
    return home


def test_seam_modules_import_only_stdlib_and_engine():
    stdlib = set(sys.stdlib_module_names)
    for name in SEAM_MODULES:
        tree = ast.parse((GROUP_EM / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] if node.level == 0 else ["<relative>"]
            else:
                continue
            for mod in mods:
                top = mod.split(".")[0]
                assert top in stdlib or top == "coordinator_core", (name, mod)


def test_seam_modules_hold_no_repo_relative_lookup():
    for name in SEAM_MODULES:
        source = (GROUP_EM / name).read_text(encoding="utf-8")
        assert "__file__" not in source, name
        assert "claude-klabauter" not in source.lower(), name
        assert "coordinator-content-repo" not in source.lower(), name


def test_select_refresh_and_identity_resolve_from_settings_home(consumer_home, tmp_path):
    near = box_registry._group_em_dir(consumer_home)
    far_home = tmp_path / "far-home"
    far_dir = far_home / "state" / "group-em"
    far_dir.mkdir(parents=True)
    box_registry.register(far_home, {}, directory=near, box_id="far")

    assert box_registry.local_box_id(consumer_home) == "local"
    boxes = box_registry.read_boxes(near)
    assert [b["box_id"] for b in boxes] == ["far"]
    assert node_transport.select(boxes[0], near).kind == "fs"

    assert remote_roster.publish_local(
        far_dir, [{"session_id": "s1", "name": "alpha"}], local_id="far"
    )
    done = remote_roster.refresh(near, local_sessions=[], local_id="near")
    assert done == {"far": True, "near": True}
    snap = remote_roster.read_snapshot(near, local_id="near")
    assert snap["far"]["reachable"] is True
    assert [s["session_id"] for s in snap["far"]["sessions"]] == ["s1"]

    listed = node_transport.select(boxes[0], near).list_sessions(boxes[0])
    assert listed["ok"] is True and listed["sessions"][0]["box_id"] == "far"

    key = remote_identity.peer_key("far", "s1")
    assert remote_identity.parse_key(key, local_id="near") == ("far", "s1")
    rewritten = remote_identity.rewrite_from(
        {"from_box": "far", "from_session": "s1"}, local_id="near"
    )
    assert rewritten["from"] == "far:s1"


def test_ssh_node_needs_no_engine_checkout_beyond_the_seam(consumer_home):
    box = {"box_id": "far", "transport": {"kind": "ssh", "host": "192.0.2.7", "invoke": "/x/inv"}}
    t = node_transport.select(box, runner=lambda argv, timeout: None)
    assert t.kind == "ssh"
    res = t.list_sessions(box)
    assert res["ok"] is False and "reason" in res


def test_no_literal_address_in_group_em_source():
    sources = [p for p in GROUP_EM.glob("*.py")]
    sources.append(Path(__file__).resolve())
    wiki = GROUP_EM.parent.parent / "docs" / "wiki" / "cross-box-sessions.md"
    if wiki.exists():
        sources.append(wiki)
    stray = {p.name: _stray_ipv4(p.read_text(encoding="utf-8")) for p in sources}
    assert {k: v for k, v in stray.items() if v} == {}


def test_no_literal_address_detector_flags_private_and_wan():
    private, wan = "10." + "8.0.2", "8.8." + "8.8"
    assert _stray_ipv4(f"host {private} and {wan}") == [private, wan]
    assert _stray_ipv4("192.0.2.2 198.51.100.4 203.0.113.9 127.0.0.1") == []
