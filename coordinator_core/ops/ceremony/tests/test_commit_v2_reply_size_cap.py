"""`ceremony.commit_v2` replies stay bounded however long a reply list grows."""

from __future__ import annotations

import json

from coordinator_core.ops.ceremony import commit_v2

REPLY_BYTE_CAP = 8 * 1024
MANY = 600


def test_cap_reply_keeps_a_sample_and_the_total():
    paths = [f"dir/file-{i:05d}.py" for i in range(MANY)]
    reply = {"committed": True, "warnings": paths, "short": ["a"]}

    capped = commit_v2.cap_reply(reply)

    assert capped["warnings"] == paths[: commit_v2.REPLY_LIST_CAP]
    assert capped["warnings_total"] == MANY
    assert capped["short"] == ["a"]
    assert "short_total" not in capped


def test_handler_reply_is_bounded_and_full_reply_keeps_the_whole_set(monkeypatch):
    warnings = [f"left/{'x' * 40}-{i:05d}.py" for i in range(MANY)]
    monkeypatch.setattr(
        commit_v2, "_handler_body", lambda params, repo_root, ledger: {"committed": True, "warnings": list(warnings)}
    )

    capped = commit_v2._handler({"paths": ["a.py"], "message": "m"})
    full = commit_v2._handler({"paths": ["a.py"], "message": "m", "full_reply": True})

    assert capped["warnings_total"] == MANY
    assert len(capped["warnings"]) == commit_v2.REPLY_LIST_CAP
    assert len(json.dumps(capped)) < REPLY_BYTE_CAP
    assert len(full["warnings"]) == MANY
    assert "warnings_total" not in full
