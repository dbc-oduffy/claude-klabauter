"""Pins the path-level peer-claim contract module."""
from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.ops.dispatch_emit import peer_claim_contract as pcc

HOLDS = [("b.py", "s2"), ("a.py", "s1"), ("c.py", "s1"), ("a.py", "s2")]


def test_constants():
    assert pcc.CONTRACT == "path-level"
    assert pcc.ROUTES == ("withhold", "peer-commits", "release")


def test_group_two_sessions_two_paths():
    assert pcc.group(HOLDS) == [
        {"session": "s1", "paths": ["a.py", "c.py"]},
        {"session": "s2", "paths": ["a.py", "b.py"]},
    ]


def test_refusal_fields_and_message():
    msg, fields = pcc.refusal(HOLDS, "run-42")
    for path, sid in HOLDS:
        assert f"{path} held by {sid}" in msg
    assert pcc.CONTRACT in msg
    assert "Wait for the peer" in msg
    assert "session-claim-cli release-artifact artifact <path>" in msg
    assert "run_id=run-42" in msg
    assert fields["peer_claims"] == [{"path": p, "session": s} for p, s in HOLDS]
    assert fields["peer_claims_by_session"] == pcc.group(HOLDS)
    assert fields["peer_claim_contract"] == "path-level"
    routes = fields["peer_claim_routes"]
    assert [r["route"] for r in routes] == list(pcc.ROUTES)
    assert "run_id=run-42" in routes[0]["call"]
    assert "release-artifact" in routes[2]["call"]


def test_left_out_sorted():
    out = pcc.left_out({"z.py": ["s2", "s1"], "a.py": ["s3"]})
    assert out == [
        {"path": "a.py", "sessions": ["s3"]},
        {"path": "z.py", "sessions": ["s1", "s2"]},
    ]
    assert pcc.left_out({}) == []


def test_module_imports_no_subprocess():
    tree = ast.parse(Path(pcc.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert "subprocess" not in names
