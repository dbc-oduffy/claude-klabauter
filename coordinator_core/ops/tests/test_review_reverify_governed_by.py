"""review.reverify list mode raises a major provenance row for agent code commits without a resolvable Governed-By."""

from __future__ import annotations

from coordinator_core.commenting import authorship
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.ceremony import governed_by
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401
    PREFIX,
    SESSION,
    _set_base,
    world,
)

FULL_BASE = "a" * 40
PLAN_SHA = "b" * 40
GOOD = f"Governed-By: {PLAN_SHA} 2026-11-26T20:00:00+00:00 docs/plans/p.md"


def _commit(sha: str, message: str, *paths: str) -> str:
    diffs = "".join(f"\ndiff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1 @@\n+x = 1\n" for p in paths)
    return f"\x1e{sha}\x1f{message}\x1f{diffs}"


def _log(monkeypatch, text: str, spawns: list) -> None:
    def fake(args, cwd, stdin):
        spawns.append(args[0])
        return text.encode("utf-8") if args[0] == "log" else None

    monkeypatch.setattr(authorship, "default_runner", fake)


def _list(root):
    return rr.review_reverify(root, base=FULL_BASE, slice_prefix=PREFIX, session_id=SESSION)


def test_agent_code_commit_without_governed_by_is_a_major_row(world, monkeypatch):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    spawns: list = []
    _log(monkeypatch, _commit("c" * 40, "edit\n\nSession-Id: sess-1\n", "pkg/mod.py"), spawns)
    monkeypatch.setattr(governed_by, "unresolved_shas", lambda _r, _s: [])
    out = _list(root)
    assert out["provenance_findings"] == [
        {"commit": "c" * 40, "paths": ["pkg/mod.py"], "shas": [], "severity": "major", "scope": "default-path", "reason": "missing"}
    ]
    assert spawns == ["log", "cat-file"]


def test_resolvable_governed_by_clears_and_unresolvable_raises(world, monkeypatch):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    log = _commit("c" * 40, f"edit\n\nSession-Id: sess-1\n{GOOD}\n", "pkg/mod.py")
    _log(monkeypatch, log, [])
    monkeypatch.setattr(governed_by, "unresolved_shas", lambda _r, _s: [])
    assert _list(root)["provenance_findings"] == []
    monkeypatch.setattr(governed_by, "unresolved_shas", lambda _r, shas: sorted(shas))
    rows = _list(root)["provenance_findings"]
    assert [(r["reason"], r["shas"]) for r in rows] == [("unresolved", [PLAN_SHA])]


def test_human_engine_and_non_code_commits_are_exempt(world, monkeypatch):
    root, _orig, _ = world
    _set_base(root, FULL_BASE)
    log = (
        _commit("1" * 40, "human edit\n", "pkg/mod.py")
        + _commit("2" * 40, "engine\n\nSession-Id: claude-klabauter-engine\n", "pkg/mod.py")
        + _commit("3" * 40, "docs only\n\nSession-Id: sess-1\n", "docs/notes.md")
    )
    _log(monkeypatch, log, [])
    monkeypatch.setattr(governed_by, "unresolved_shas", lambda _r, _s: [])
    assert _list(root)["provenance_findings"] == []


def test_revert_mode_and_non_sha_base_carry_no_provenance_rows(world, monkeypatch):
    root, _orig, _ = world
    spawns: list = []
    _log(monkeypatch, _commit("c" * 40, "e\n\nSession-Id: s\n", "pkg/mod.py"), spawns)
    assert rr.review_reverify(root, base="abc1234", slice_prefix=PREFIX, session_id=SESSION)["provenance_findings"] == []
    assert "provenance_findings" not in rr.review_reverify(root, base="abc1234", slice_prefix=PREFIX, session_id=SESSION, failing=[])
    assert spawns == []
