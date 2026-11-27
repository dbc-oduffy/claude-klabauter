"""review.reverify list mode reports agent-added comments still present in the worktree as major rows."""

from __future__ import annotations

import pytest

from coordinator_core.commenting.added_types import AddedComment
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    PREFIX,
    SESSION,
    _set_base,
    world,
)

FULL_BASE = "a" * 40


def _list(root, base):
    return rr.review_reverify(root, base=base, slice_prefix=PREFIX, session_id=SESSION)


def _found(monkeypatch, calls):
    def fake(root, base, head, **_k):
        calls.append((base, head))
        return [AddedComment("pkg/mod.py", 2, "# this is excused", "comment")]

    monkeypatch.setattr(rr, "agent_added_comments", fake)


def test_comment_still_in_the_file_is_a_major_default_path_row(world, monkeypatch):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    (root / "pkg/mod.py").write_text("VALUE = 1\n# this is excused\n", encoding="utf-8")
    calls: list = []
    _found(monkeypatch, calls)
    out = _list(root, FULL_BASE)
    assert out["comment_findings"] == [
        {"path": "pkg/mod.py", "line": 2, "text": "# this is excused", "severity": "major", "scope": "default-path"}
    ]
    assert calls == [(FULL_BASE, "HEAD")]


@pytest.mark.parametrize("body", ["VALUE = 1\n", "VALUE = 1\n# trap: rewritten\n"])
def test_deleted_or_rewritten_comment_clears_the_row(world, monkeypatch, body):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    (root / "pkg/mod.py").write_text(body, encoding="utf-8")
    _found(monkeypatch, [])
    assert _list(root, FULL_BASE)["comment_findings"] == []


def test_a_non_sha_base_never_reaches_the_detector(world, monkeypatch):
    root, _orig, _ = world
    calls: list = []
    _found(monkeypatch, calls)
    out = rr.review_reverify(root, base="abc1234", slice_prefix=PREFIX, session_id=SESSION)
    assert out["comment_findings"] == [] and calls == []


def _blob(content: str) -> bytes:
    data = content.encode()
    return b"%s blob %d\n%s\n" % (b"c" * 40, len(data), data)


def _canned_git(monkeypatch, head_text):
    log = (
        f"\x1e{'b' * 40}\x1f edit\n\nSession-Id: abc-123\n\x1f\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,0 +2 @@\n+# this is excused\n"
    )

    def fake_runner(args, cwd, stdin):
        if args[:1] == ["log"]:
            return log.encode()
        return _blob("VALUE = 1\n") + _blob(head_text)

    monkeypatch.setattr(rr.authorship, "default_runner", fake_runner)


@pytest.mark.parametrize(
    ("worktree", "expected"),
    [
        ("VALUE = 1\n# this is excused\n", ["# this is excused"]),
        ("VALUE = 1\n", []),
        ("VALUE = 1\n# trap: this is excused\n", []),
        ("VALUE = 1\n# a different excuse\n", ["# a different excuse"]),
    ],
)
def test_review_edit_over_the_flagged_comment_through_real_detector(world, monkeypatch, worktree, expected):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    (root / "pkg/mod.py").write_text(worktree, encoding="utf-8")
    _canned_git(monkeypatch, "VALUE = 1\n# this is excused\n")
    rows = _list(root, FULL_BASE)["comment_findings"]
    assert [r["text"] for r in rows] == expected
    assert all(r["severity"] == "major" and r["scope"] == "default-path" for r in rows)


def _canned_log(monkeypatch, message):
    log = f"\x1e{'b' * 40}\x1f{message}\x1f\ndiff --git a/pkg/mod.py b/pkg/mod.py\n+++ b/pkg/mod.py\n+x = 1\n"

    def fake_runner(args, cwd, stdin):
        return log.encode() if args[:1] == ["log"] else None

    monkeypatch.setattr(rr.authorship, "default_runner", fake_runner)

    def detect(root, base, head, runner=None, **_k):
        runner(["log"], root, None)
        return []

    monkeypatch.setattr(rr, "agent_added_comments", detect)


def test_agent_code_commit_without_governed_by_is_a_major_provenance_row(world, monkeypatch):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    _canned_log(monkeypatch, "edit\n\nSession-Id: abc-123\n")
    rows = _list(root, FULL_BASE)["provenance_findings"]
    assert [(r["commit"], r["paths"], r["severity"], r["reason"]) for r in rows] == [
        ("b" * 40, ["pkg/mod.py"], "major", "missing")
    ]


def test_human_commit_raises_no_provenance_row(world, monkeypatch):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    _canned_log(monkeypatch, "edit by hand\n")
    assert _list(root, FULL_BASE)["provenance_findings"] == []


def test_revert_mode_carries_no_comment_findings(world, monkeypatch):
    root, _orig, _ = world
    _found(monkeypatch, [])
    out = rr.review_reverify(root, base="abc1234", slice_prefix=PREFIX, session_id=SESSION, failing=[])
    assert "comment_findings" not in out
