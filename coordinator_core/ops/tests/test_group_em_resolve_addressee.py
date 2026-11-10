"""groupem.resolve_addressee refuses a missing peer_session_id by name."""

from __future__ import annotations

import pytest

from coordinator_core.group_em import send_pass
from coordinator_core.ops import group_em_resolve_addressee as op


@pytest.mark.parametrize("params", [{}, None, {"peer_session_id": ""}, {"peer_session_id": 7}])
def test_missing_or_bad_peer_session_id_is_named_refusal(params):
    result = op._groupem_resolve_addressee(params)
    assert "peer_session_id" in result["error"]
    assert "name" not in result


def test_valid_call_unchanged(monkeypatch, tmp_path):
    seen = {}

    def fake(root, sid):
        seen["args"] = (root, sid)
        return "group-em-ad"

    monkeypatch.setattr(send_pass, "resolve_addressee", fake)
    result = op._groupem_resolve_addressee(
        {"peer_session_id": "abc", "repo_root": str(tmp_path)}
    )
    assert result == {"name": "group-em-ad"}
    assert seen["args"] == (str(tmp_path), "abc")
