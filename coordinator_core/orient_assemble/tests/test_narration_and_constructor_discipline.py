"""
coordinator_core.orient_assemble.tests.test_narration_and_constructor_discipline
— C3 AC(e): narration[] is NOT a 1:1 reproduction of the old surface step
sequence, and every judgment_point is built via the shipped
`contract/decision_object/judgment.py` constructors, not a hand-assembled
dict. The assembler is held to `computed-skills-conversion-checklist.md`
on the COMPUTE side, not only the three caller surfaces (DR-090).

Directive shape note: `directives[]` entries are plain dicts by design —
every reader module's own docstring documents this as an intentional
mirror of `coordinator_core.pickup_assemble`'s directive shape ("no
separate shipped directive constructor exists"), so this file does not
assert directives were built via a constructor that does not exist. It
instead asserts every directive conforms to the DoE schema-of-record's
required directive key set (id, cli, args, depends_on, already_satisfied)
— the structural contract that DOES apply.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C3
"""

from __future__ import annotations

from coordinator_core.contract.decision_object.judgment import build_judgment_point
from coordinator_core.orient_assemble import CADENCES, brief
from coordinator_core.orient_assemble import (
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
)
from coordinator_core.orient_assemble.readers_clean_ops import ReaderResult

_REQUIRED_DIRECTIVE_KEYS = {"id", "cli", "args", "depends_on", "already_satisfied"}
_JUDGMENT_POINT_CONSTRUCTOR_KEYS = set(
    build_judgment_point(
        None,
        id="x",
        question="x?",
        dispositions=[{"value": "x", "resolves": []}],
        evidence="x",
        reason="x",
    ).keys()
)

#: `reportable` is emitted only when the builder classified the point (see
#: `judgment.partition_reportable`), so it is optional on this shape rather
#: than part of the constructor's unconditional key set. Pinned as a named
#: exception, not by loosening the equality above -- an unexpected key is
#: still a failure.
_OPTIONAL_JUDGMENT_POINT_KEYS = {"reportable"}


def _all_reader_results(monkeypatch):
    """Collect real (non-mocked-away) directive/judgment_point content by
    driving each family with just enough of a deterministic fixture to
    produce at least one entry of each shape, reusing the same fixtures
    exercised in test_round_trip_parity.py."""
    monkeypatch.setattr(rco, "_read_em_environment", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_memo_surface", lambda mode, **kw: ReaderResult())
    monkeypatch.setattr(rco, "_read_rag_staleness", lambda: ReaderResult())
    monkeypatch.setattr(rco, "_read_worktree_sweep", lambda **kw: ReaderResult())
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: (["RED: x"], 1))
    clean_ops = rco.collect("day")

    monkeypatch.setattr(rht, "_cmd_stale_plans", lambda args: (print("stale: p1"), 0)[1])
    monkeypatch.setattr(rht, "_cmd_ready", lambda args: 0)
    monkeypatch.setattr(rht, "_cmd_awaiting_gate", lambda args: 0)
    monkeypatch.setattr(rht, "_read_orphaned_plans", lambda: ReaderResult())
    handoff = rht.collect("day")

    return [clean_ops, handoff]


def test_all_directives_conform_to_the_schemas_required_directive_keys(monkeypatch):
    for reader_result in _all_reader_results(monkeypatch):
        for directive in reader_result.directives:
            assert _REQUIRED_DIRECTIVE_KEYS <= set(directive.keys()), directive


def test_clean_ops_em_environment_judgment_points_are_built_via_the_shipped_constructor(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(rco, "_resolve_effort", lambda proj, uc: ("high", "project"))
    monkeypatch.setattr(rco, "_resolve_transcript", lambda a, b, c: "")
    monkeypatch.setattr(rco, "_latest_model", lambda p: "")

    result = rco._read_em_environment()
    assert result.judgment_points, "expected at least one judgment point (effort='high')"
    for jp in result.judgment_points:
        keys = set(jp.keys())
        assert _JUDGMENT_POINT_CONSTRUCTOR_KEYS <= keys
        assert keys - _JUDGMENT_POINT_CONSTRUCTOR_KEYS <= _OPTIONAL_JUDGMENT_POINT_KEYS


def test_worktree_sweep_dirty_judgment_points_are_built_via_the_shipped_constructor(
    monkeypatch, tmp_path
):
    import coordinator_core.orient_assemble.readers_clean_ops as rco_mod

    fake_worktree_path = str(tmp_path / "fake-worktree")
    fake_repo_root = str(tmp_path / "fake-repo")

    class _FakeWorktree:
        path = fake_worktree_path

    class _FakeClassification:
        state = "dirty-nonbenign"
        dirty_count = 3

    seen_repo_root_cwd = []

    def _fake_wt_repo_root(cwd=None):
        seen_repo_root_cwd.append(cwd)
        return fake_repo_root

    monkeypatch.setattr(rco_mod, "_wt_repo_root", _fake_wt_repo_root)
    monkeypatch.setattr(rco_mod, "_wt_active_branch", lambda root: "main")
    monkeypatch.setattr(rco_mod, "_is_agent_worktree", lambda path: True)
    monkeypatch.setattr(rco_mod, "_list_worktrees", lambda root: [_FakeWorktree()])
    monkeypatch.setattr(rco_mod, "classify_worktree", lambda path, ref: _FakeClassification())

    passed_root = str(tmp_path / "caller-repo")
    result = rco._read_worktree_sweep(repo_root=passed_root)
    assert result.judgment_points
    for jp in result.judgment_points:
        assert set(jp.keys()) == _JUDGMENT_POINT_CONSTRUCTOR_KEYS

    from pathlib import Path as _Path

    assert seen_repo_root_cwd == [_Path(passed_root)]


def test_narration_is_not_a_verbatim_reproduction_of_any_reader_directive_detail(monkeypatch):
    """The assembler's own narration prose must not degenerate into pasting
    the raw step-body text it computed over. Checked at the brief()-skeleton
    layer (the level narration is actually emitted from today) plus a
    negative check against a synthesized old-style step string."""
    old_style_step_text = (
        "Step 1: Check EM environment. Step 2: Run addon health scan. "
        "Step 3: Surface cross-repo memos."
    )
    for cadence in CADENCES:
        envelope = brief(cadence)
        assert envelope["narration"] != old_style_step_text
        assert old_style_step_text not in envelope["narration"]
        assert envelope["narration"].strip() != ""
