"""test_close_ships_its_batons — C3 of
docs/plans/2026-08-30-the-close-ships-the-baton-it-closed.md.

Covers the seam C2 built: `directives_commit_tail.resolve_ship_stamp_candidates`/
`apply_ship_stamps`/`revert_ship_stamps`, and `apply.py::_run_close_commit_tail`'s
orchestration of them (ship-stamp BEFORE the commit call, folded into the SAME
`stage_paths` sequence, reverted rather than left standing when the commit that
was meant to carry the stamp fails or never lands).

Negative-spec (mirrors the plan's own Anti-scope): none of these tests drive a
real `git commit` or a real claim ledger — `_held_handoff_basenames` and
`run_close_commit_and_release_claims` are monkeypatched at the seam `apply.py`
itself calls through, so what is under test is the ORCHESTRATION (ordering,
fold, revert-on-failure, the empty-set signal), not `commit_paths`'/`handoff.
stamp`'s own already-tested internals.

Run: python -m pytest coordinator_core/workstream_complete/tests/test_close_ships_its_batons.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import time
from pathlib import Path

import pytest

from coordinator_core.workstream_complete import apply as _apply
from coordinator_core.workstream_complete import directives_commit_tail


def _commit_tail_outcome(*, committed_sha, commit_failed):
    return directives_commit_tail.CommitTailOutcome(
        committed_sha=committed_sha,
        pushed=None,
        push_status="not-attempted",
        commit_failed=commit_failed,
        integrity_breach=False,
        sha_unverified=False,
        diagnostics=[],
    )


# ---------------------------------------------------------------------------
# resolve_ship_stamp_candidates — the positive-membership rule
# ---------------------------------------------------------------------------


def test_delivered_baton_is_a_candidate(tmp_path, monkeypatch):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "foo.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == [("state/handoffs/foo.md", "deadbeef")]


@pytest.mark.parametrize("disposition", ["closed", "abandoned", "continued"])
def test_non_delivered_terminal_baton_is_not_a_candidate(tmp_path, monkeypatch, disposition):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "foo.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": disposition, "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


def test_already_archived_baton_is_left_alone(tmp_path, monkeypatch):
    # No file under state/handoffs/ at all -- it is already under archive/handoffs/,
    # i.e. already consumed per the PM's folder-fact ruling. The claim ledger still
    # names the basename (a stale/late-release claim), but there is nothing active
    # here for this close to stamp.
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


def test_held_claim_this_close_never_touched_is_not_a_candidate(tmp_path, monkeypatch):
    # The claim ledger says the session holds "bar.md", but this close's own
    # decisions never mention it -- a claim held merely to read (or on a baton
    # this close did not itself close) is excluded by the POSITIVE rule, not
    # because it matches a negative disposition.
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "bar.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["bar.md"]
    )
    decisions = {"handoff_dispositions": {}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


# ---------------------------------------------------------------------------
# apply.py::_run_close_commit_tail — orchestration: fold-in, no second
# commit object, empty-set signal, and the write-lands-then-commit-fails
# ordering.
# ---------------------------------------------------------------------------


def test_delivered_baton_folds_into_the_close_s_own_commit_with_no_second_commit(monkeypatch, tmp_path):
    commit_calls = []

    def _fake_run_close_commit_and_release_claims(worktree_root, **kwargs):
        commit_calls.append(kwargs)
        return _commit_tail_outcome(committed_sha="abc123", commit_failed=False)

    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates",
        lambda *_a, **_k: [("state/handoffs/foo.md", "deadbeef")],
    )
    monkeypatch.setattr(
        directives_commit_tail, "apply_ship_stamps",
        lambda *_a, **_k: (
            directives_commit_tail.ShipStampOutcome(
                stamped_paths=("state/handoffs/foo.md",),
                skipped_paths=(),
                attempted=1,
                diagnostics=(),
            ),
            {"state/handoffs/foo.md": "original text\n"},
        ),
    )
    monkeypatch.setattr(
        directives_commit_tail,
        "run_close_commit_and_release_claims",
        _fake_run_close_commit_and_release_claims,
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-1")

    assert len(commit_calls) == 1, "the stamp must fold into the SAME commit call, never a second one"
    assert sorted(commit_calls[0]["stage_paths"]) == sorted(["other.txt", "state/handoffs/foo.md"])
    assert report["ship_stamp"]["stamped"] == ["state/handoffs/foo.md"]
    assert report["ship_stamp"]["reverted"] == []
    assert report["committed_sha"] == "abc123"


def test_session_holding_no_batons_produces_a_read_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        directives_commit_tail,
        "run_close_commit_and_release_claims",
        lambda worktree_root, **kwargs: _commit_tail_outcome(committed_sha="abc123", commit_failed=False),
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-2")

    # RAN, FOUND NOTHING -- distinguishable from "never ran" (the retired
    # design's empty_consumed_set had no reader; this key does).
    assert report["ship_stamp"]["attempted"] == 0
    assert report["ship_stamp"]["stamped"] == []


def test_write_lands_then_commit_raises_reverts_the_stamp(monkeypatch, tmp_path):
    revert_calls = []
    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates",
        lambda *_a, **_k: [("state/handoffs/foo.md", "deadbeef")],
    )
    monkeypatch.setattr(
        directives_commit_tail, "apply_ship_stamps",
        lambda *_a, **_k: (
            directives_commit_tail.ShipStampOutcome(
                stamped_paths=("state/handoffs/foo.md",),
                skipped_paths=(),
                attempted=1,
                diagnostics=(),
            ),
            {"state/handoffs/foo.md": "original text\n"},
        ),
    )

    def _raise(worktree_root, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(directives_commit_tail, "run_close_commit_and_release_claims", _raise)
    monkeypatch.setattr(
        directives_commit_tail, "revert_ship_stamps",
        lambda root, relpaths, backups: revert_calls.append((tuple(relpaths), dict(backups))),
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-3")

    assert revert_calls == [
        (("state/handoffs/foo.md",), {"state/handoffs/foo.md": "original text\n"})
    ], "a raised commit call must revert the already-landed stamp write, per DR-358's finally-release ordering"
    assert report["commit_failed"] is True


@pytest.mark.parametrize(
    "committed_sha,commit_failed",
    [(None, False), ("abc123", True)],
    ids=["no-op-commit-no-sha", "commit-refused"],
)
def test_stamp_reverted_when_commit_did_not_land(monkeypatch, tmp_path, committed_sha, commit_failed):
    revert_calls = []
    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates",
        lambda *_a, **_k: [("state/handoffs/foo.md", "deadbeef")],
    )
    monkeypatch.setattr(
        directives_commit_tail, "apply_ship_stamps",
        lambda *_a, **_k: (
            directives_commit_tail.ShipStampOutcome(
                stamped_paths=("state/handoffs/foo.md",),
                skipped_paths=(),
                attempted=1,
                diagnostics=(),
            ),
            {"state/handoffs/foo.md": "original text\n"},
        ),
    )
    monkeypatch.setattr(
        directives_commit_tail, "run_close_commit_and_release_claims",
        lambda worktree_root, **kwargs: _commit_tail_outcome(
            committed_sha=committed_sha, commit_failed=commit_failed
        ),
    )
    monkeypatch.setattr(
        directives_commit_tail, "revert_ship_stamps",
        lambda root, relpaths, backups: revert_calls.append(tuple(relpaths)),
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-4")

    assert revert_calls == [("state/handoffs/foo.md",)]
    assert report["ship_stamp"]["stamped"] == []
    assert report["ship_stamp"]["reverted"] == ["state/handoffs/foo.md"]


# ---------------------------------------------------------------------------
# The permanent budget guard: no unbounded corpus walk anywhere in the two
# writer modules. This is leg (B) of
# docs/plans/2026-08-30-the-close-ships-the-baton-it-closed.falsifier.py's
# own `CallVisitor`, imported and reused rather than re-derived -- per the
# chunk body, this pytest guard is the authoritative, permanent form; the
# falsifier's own leg (B) is the throwaway half once this lands.
# ---------------------------------------------------------------------------

_FALSIFIER_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "plans"
    / "2026-08-30-the-close-ships-the-baton-it-closed.falsifier.py"
)


def _load_falsifier_module():
    spec = importlib.util.spec_from_file_location("_wsc_ship_stamp_falsifier", _FALSIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _enclosing_function_source(tree: ast.Module, text: str, lineno: int) -> str:
    """The source text of the `def` that CONTAINS `lineno`, narrowest match
    first -- used to tell "a scan call that happens to live in this module"
    from "a scan call reachable from the ship-stamp path", since the shared
    `CallVisitor` (deliberately, per its own module docstring) does not
    distinguish targets, only call SHAPE (`.glob`/`.rglob`)."""
    best: "ast.FunctionDef | None" = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.lineno <= lineno <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:
                best = node
    if best is None:
        return ""
    return ast.get_source_segment(text, best) or ""


def test_no_unbounded_corpus_walk_in_the_ship_stamp_writer_modules():
    """C3's own AC: no rglob/os.walk over `state/handoffs/` or
    `archive/handoffs/` anywhere reachable from the ship-stamp path. Reuses
    the falsifier's `CallVisitor.scan_calls` (leg B) as the shape-matcher --
    per the chunk body, this pytest guard is the authoritative, permanent
    form of that check -- then narrows to the ship-stamp's own concern by
    reading each hit's enclosing function: `directives_commit_tail.py` also
    hosts `_peer_subagent_share_paths`' pre-existing, unrelated
    `state/subagent-share/` walk (module docstring's own "unrelated to
    handoff stamping" note, baselined at this plan's own falsifier baseline)
    -- a real corpus walk over a DIFFERENT surface, not a handoffs scan
    reintroduced under another name. A scan call whose enclosing function
    body mentions "handoffs" fails this guard; one that doesn't is not this
    plan's concern to gravestone."""
    falsifier = _load_falsifier_module()

    offending: list[tuple[str, int, str]] = []
    benign: list[tuple[str, int, str]] = []
    for path in sorted(falsifier.C2_WRITE_FILES):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        visitor = falsifier.CallVisitor()
        visitor.visit(tree)
        for lineno, name in visitor.scan_calls:
            hit = (path.name, lineno, name)
            func_source = _enclosing_function_source(tree, text, lineno)
            if "handoffs" in func_source:
                offending.append(hit)
            else:
                benign.append(hit)

    assert offending == [], (
        "a glob/rglob corpus scan over a handoffs surface appeared in "
        "apply.py/directives_commit_tail.py -- the 250ms consumed-handoff "
        f"scan this plan retired coming back under another name: {offending}"
    )
    # Pinned, not silently grown: today's one known, unrelated hit is
    # `_peer_subagent_share_paths`' walk over `state/subagent-share/<sid>/`.
    # A NEW benign-looking hit is still worth a human look even though it
    # does not fail this guard on its own -- surfaced here rather than
    # swallowed by a bare `assert offending == []`.
    assert benign == [("directives_commit_tail.py", 237, "abs_dir.rglob")], benign


def test_falsifier_ship_candidates_module_writes_the_two_files_the_plan_scoped():
    falsifier = _load_falsifier_module()
    assert falsifier.C2_WRITE_FILES == {
        falsifier.PKG_DIR / "apply.py",
        falsifier.PKG_DIR / "directives_commit_tail.py",
    }


# ---------------------------------------------------------------------------
# Process-time budget for the candidate-resolution leg itself -- process
# time, never wall clock (a wall-clock read is dominated by peer load on a
# shared box, per this repo's own load-norm doctrine). Aggregated over
# N>=200 iterations since a single `time.process_time()` read sits below
# Windows' ~15.6ms clock granularity and cannot show a regression on its own.
# ---------------------------------------------------------------------------

_SHIP_CANDIDATE_RESOLUTION_CEILING_MS = 50.0
_ITERATIONS = 200


def test_resolve_ship_stamp_candidates_process_time_budget(tmp_path, monkeypatch):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "foo.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    start = time.process_time()
    for _ in range(_ITERATIONS):
        directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)
    elapsed_ms = (time.process_time() - start) * 1000.0

    assert elapsed_ms <= _SHIP_CANDIDATE_RESOLUTION_CEILING_MS, (
        f"resolve_ship_stamp_candidates: {elapsed_ms}ms process time over {_ITERATIONS} "
        f"iterations exceeds the {_SHIP_CANDIDATE_RESOLUTION_CEILING_MS}ms ceiling -- "
        "candidate resolution must stay bounded by claims held, never corpus size"
    )
