"""Pins CommitRequest.review_carried: marker round-trip and landing_remedy.review_carried_withholds."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import landing_remedy as lr
from coordinator_core.ops.dispatch_emit.commit_request import (
    MARKER_PREFIX,
    ChunkCommit,
    CommitRequest,
    parse_marker,
    render_marker,
)

_CHUNKS = (ChunkCommit(id="C1", title="t", paths=("a.py",), prefixes=(), report="r.md"),)


def _req(**kw) -> CommitRequest:
    return CommitRequest(chunks=_CHUNKS, deliverable_id=None, session_id=None, repo_root=None,
                         plan_path="p.md", **kw)


def _payload(line: str) -> dict:
    return json.loads(line[len(MARKER_PREFIX):])


def test_review_carried_round_trips_sorted_and_forward_slash():
    req = _req(review_only=True, review_carried=("z/b.py", "a\\c.py"))
    line = render_marker(req)
    assert _payload(line)["review_carried"] == ["a/c.py", "z/b.py"]
    assert parse_marker(line).review_carried == ("a/c.py", "z/b.py")


def test_plain_and_non_review_only_markers_never_carry_the_key():
    assert "review_carried" not in _payload(render_marker(_req()))
    assert "review_carried" not in _payload(render_marker(_req(review_only=True)))
    assert "review_carried" not in _payload(render_marker(_req(review_carried=("a.py",))))


def test_older_marker_parses_to_empty_and_non_strings_drop():
    assert parse_marker(render_marker(_req(review_only=True))).review_carried == ()
    payload = _payload(render_marker(_req(review_only=True)))
    payload["review_carried"] = ["a.py", 3, None]
    line = MARKER_PREFIX + json.dumps(payload)
    assert parse_marker(line).review_carried == ("a.py",)


def test_withholds_names_carried_paths_for_review_only_only():
    assert lr.review_carried_withholds(_req(review_only=True, review_carried=("a.py",))) == {
        "a.py": lr.REVIEW_CARRIED_REASON
    }
    assert lr.review_carried_withholds(_req(review_carried=("a.py",))) == {}
    assert lr.review_carried_withholds(_req(review_only=True)) == {}
