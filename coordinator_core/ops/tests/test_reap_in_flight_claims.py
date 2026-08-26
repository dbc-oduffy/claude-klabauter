"""
Tests for coordinator_core.ops.reap_in_flight_claims — the return-data
survey of orphaned in_flight handoff claims (C1 of
docs/plans/2026-08-26-two-callers-want-two-numbers-not-a-1301-line-cli.md).

Negative-spec: does not exercise `coordinator/bin/reap-orphaned-in-flight-
handoffs.py` (C3's rebuild) or `readers_health_reaper.py` (C2's caller) —
pure unit coverage of this module's own `survey()`/`apply_dispositions()`
surface, built from the plan's Problem section and from
`coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py`'s
predicates (DR-344 § 6 forbids opening the deleted implementation itself).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import reap_in_flight_claims as mod


def _write_handoff(handoffs_dir, name, *, status, deployment_state, consumed_by=None,
                    kind=None, deliverable_id=None, handoff_id=None):
    lines = ["---", 'title: "test handoff"', f"status: {status}",
             f"deployment_state: {deployment_state}"]
    if consumed_by is not None:
        lines.append(f"consumed_by: {consumed_by}")
    if kind is not None:
        lines.append(f"kind: {kind}")
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    if handoff_id is not None:
        lines.append(f"handoff_id: {handoff_id}")
    lines.append("---")
    lines.append("body")
    path = Path(handoffs_dir) / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ===========================================================================
# _batch_commit_timestamps
# ===========================================================================
def test_batch_commit_timestamps_makes_one_call_for_many_shas(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stdout = "sha1full 1000\nsha2full 2000\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    result = mod._batch_commit_timestamps(["sha1", "sha2"], "/some/repo")

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == ["git", "-C", "/some/repo"]
    assert "--no-walk=unsorted" in cmd
    assert "--ignore-missing" in cmd
    assert "--format=%H %ct" in cmd
    assert "sha1" in cmd and "sha2" in cmd
    assert not any(".." in tok for tok in cmd)
    assert result == {"sha1": 1000, "sha2": 2000}


def test_batch_commit_timestamps_empty_input_makes_no_call(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise AssertionError("must not spawn for an empty SHA list")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._batch_commit_timestamps([], "/some/repo") == {}


def test_batch_commit_timestamps_dropped_sha_absent_from_result(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "presentshafull 5000\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: FakeResult())
    result = mod._batch_commit_timestamps(["presentsha", "droppedsha"], "/some/repo")
    assert result.get("presentsha") == 5000
    assert "droppedsha" not in result


def test_batch_commit_timestamps_git_failure_returns_empty_map(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: FakeResult())
    assert mod._batch_commit_timestamps(["sha1"], "/some/repo") == {}


# ===========================================================================
# _best_shipped_sha
# ===========================================================================
def test_best_shipped_sha_picks_max_committer_timestamp():
    sha_ct = {"a": 100, "b": 300, "c": 200}
    assert mod._best_shipped_sha(["a", "b", "c"], sha_ct) == "b"


def test_dropped_sha_is_reconciled_as_unresolved_not_shipped():
    assert mod._best_shipped_sha(["droppedsha"], {}) == ""
    assert mod._best_shipped_sha(["droppedsha", "presentsha"], {"presentsha": 42}) == "presentsha"


def test_best_shipped_sha_no_resolvable_sha_fails_closed():
    assert mod._best_shipped_sha([], {}) == ""
    assert mod._best_shipped_sha(["x", "y"], {}) == ""


# ===========================================================================
# _shipped_orphan_candidate_shas — P2+P3, zero git spawns
# ===========================================================================
def test_shipped_orphan_candidate_p2_ambiguous_returns_none():
    result = mod._shipped_orphan_candidate_shas("s1", {"s1": 2}, {"s1": [{"frontmatter": {"commits": ["x"]}}]})
    assert result is None


def test_shipped_orphan_candidate_p3_zero_completions_returns_none():
    result = mod._shipped_orphan_candidate_shas("s1", {"s1": 1}, {})
    assert result is None


def test_shipped_orphan_candidate_success_returns_commits_list():
    index = {"s1": [{"frontmatter": {"commits": ["sha1", "sha2"]}}]}
    result = mod._shipped_orphan_candidate_shas("s1", {"s1": 1}, index)
    assert result == ["sha1", "sha2"]


# ===========================================================================
# AC3 — ONE open per corpus file
# ===========================================================================
def test_build_corpus_opens_each_file_at_most_once(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    for i in range(5):
        _write_handoff(handoffs_dir, f"h{i}.md", status="active", deployment_state="ready_to_fire")

    opens = []
    real = mod._read_text_once

    def counting(path):
        opens.append(path)
        return real(path)

    monkeypatch.setattr(mod, "_read_text_once", counting)
    corpus = mod._build_corpus(handoffs_dir)

    assert len(corpus) == 5
    assert len(opens) <= len(list(handoffs_dir.glob("*.md")))
    assert len(opens) == 5


def test_survey_over_whole_call_opens_at_most_once_per_corpus_file(tmp_path, monkeypatch):
    """AC3, end-to-end: `survey()` never re-opens a corpus file after
    `_build_corpus` — the census, live-children check, and ship-detection
    indexes all read from the in-memory table, never the disk again."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")
    for i in range(3):
        _write_handoff(handoffs_dir, f"other{i}.md", status="active", deployment_state="ready_to_fire")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(
        mod, "has_live_children_many",
        _async_return({str((handoffs_dir / "a.md").resolve()): 1}),
    )
    monkeypatch.setattr(mod, "_build_implemented_plan_index", lambda repo_root: {})
    monkeypatch.setattr(mod, "_build_completion_index", lambda repo_root: {})
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")

    opens = []
    real = mod._read_text_once

    def counting(path):
        opens.append(path)
        return real(path)

    monkeypatch.setattr(mod, "_read_text_once", counting)

    result = mod.survey(tmp_path)

    assert len(opens) == 4  # one per corpus file, never re-opened
    assert result.would_release == 1
    assert result.would_reclaim == 0


def _async_return(mapping):
    async def _fake(candidates, repo_root=None, **kwargs):
        return dict(mapping)

    return _fake


# ===========================================================================
# survey() — end-to-end disposition predicates
# ===========================================================================
def test_survey_no_in_flight_claims_returns_zero_zero(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir, "active.md", status="active", deployment_state="ready_to_fire")

    monkeypatch.setattr(mod, "_build_completion_index", lambda repo_root: (_ for _ in ()).throw(AssertionError("must not build index")))
    monkeypatch.setattr(mod, "_build_implemented_plan_index", lambda repo_root: (_ for _ in ()).throw(AssertionError("must not build index")))

    result = mod.survey(tmp_path)
    assert result.would_release == 0
    assert result.would_reclaim == 0
    assert result.dispositions == []


def test_survey_live_holder_is_not_a_candidate(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="alive1")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: True)

    result = mod.survey(tmp_path)
    assert result.would_release == 0
    assert result.would_reclaim == 0
    assert result.dispositions == []


def test_survey_dead_holder_with_live_children_is_skipped(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(
        mod, "has_live_children_many",
        _async_return({str(h1.resolve()): 0}),
    )

    result = mod.survey(tmp_path)
    assert result.would_release == 0
    assert result.would_reclaim == 0
    assert len(result.dispositions) == 1
    assert result.dispositions[0].verdict == mod._VERDICT_SKIP_LIVE_CHILDREN


def test_survey_indeterminate_live_children_fails_closed_to_skip(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(mod, "has_live_children_many", _async_return({}))

    result = mod.survey(tmp_path)
    assert result.dispositions[0].verdict == mod._VERDICT_SKIP_LIVE_CHILDREN
    assert "indeterminate" in result.dispositions[0].detail


def test_survey_governed_plan_skips_release(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(
        handoffs_dir, "a.md", status="consumed", deployment_state="in_flight",
        consumed_by="dead1", deliverable_id="dlv-abc",
    )

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(mod, "has_live_children_many", _async_return({str(h1.resolve()): 1}))
    monkeypatch.setattr(
        mod, "_build_implemented_plan_index",
        lambda repo_root: {"dlv-abc": {"path": "docs/plans/p.md", "title": "shipped"}},
    )

    result = mod.survey(tmp_path)
    assert result.would_release == 0
    assert result.would_reclaim == 0
    assert result.dispositions[0].verdict == mod._VERDICT_SKIP_GOVERNED_PLAN


def test_survey_spinoff_kind_is_exempt_from_governed_plan_precheck(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(
        handoffs_dir, "a.md", status="consumed", deployment_state="in_flight",
        consumed_by="dead1", kind="spinoff", deliverable_id="dlv-abc",
    )

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(mod, "has_live_children_many", _async_return({str(h1.resolve()): 1}))
    monkeypatch.setattr(
        mod, "_build_implemented_plan_index",
        lambda repo_root: {"dlv-abc": {"path": "docs/plans/p.md", "title": "shipped"}},
    )
    monkeypatch.setattr(mod, "_build_completion_index", lambda repo_root: {})

    result = mod.survey(tmp_path)
    assert result.would_release == 1
    assert result.dispositions[-1].verdict == mod._VERDICT_RELEASE


def test_survey_ships_when_completion_commits_resolve_in_git_log(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(mod, "has_live_children_many", _async_return({str(h1.resolve()): 1}))
    monkeypatch.setattr(mod, "_build_implemented_plan_index", lambda repo_root: {})
    monkeypatch.setattr(
        mod, "_build_completion_index",
        lambda repo_root: {"dead1": [{"frontmatter": {"commits": ["sha-a1"]}}]},
    )

    class FakeResult:
        returncode = 0
        stdout = "sha-a1-full 1000\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: FakeResult())

    result = mod.survey(tmp_path)
    assert result.would_release == 0
    assert result.would_reclaim == 1
    reclaim = [d for d in result.dispositions if d.verdict == mod._VERDICT_RECLAIM_SHIPPED][0]
    assert reclaim.sha == "sha-a1"


def test_survey_dropped_candidate_sha_falls_through_to_release(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(mod, "has_live_children_many", _async_return({str(h1.resolve()): 1}))
    monkeypatch.setattr(mod, "_build_implemented_plan_index", lambda repo_root: {})
    monkeypatch.setattr(
        mod, "_build_completion_index",
        lambda repo_root: {"dead1": [{"frontmatter": {"commits": ["sha-vanished"]}}]},
    )

    class FakeResult:
        returncode = 0
        stdout = ""  # sha-vanished never appears -- --ignore-missing dropped it

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: FakeResult())

    result = mod.survey(tmp_path)
    assert result.would_release == 1
    assert result.would_reclaim == 0


def test_survey_batches_across_multiple_orphans_in_one_git_log_call(tmp_path, monkeypatch):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")
    h2 = _write_handoff(handoffs_dir, "b.md", status="consumed", deployment_state="in_flight", consumed_by="dead2")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(
        mod, "has_live_children_many",
        _async_return({str(h1.resolve()): 1, str(h2.resolve()): 1}),
    )
    monkeypatch.setattr(mod, "_build_implemented_plan_index", lambda repo_root: {})
    monkeypatch.setattr(
        mod, "_build_completion_index",
        lambda repo_root: {
            "dead1": [{"frontmatter": {"commits": ["sha-a1"]}}],
            "dead2": [{"frontmatter": {"commits": ["sha-b1", "sha-b2"]}}],
        },
    )

    git_log_calls = []

    class FakeResult:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kwargs):
        git_log_calls.append(cmd)
        return FakeResult(0, "sha-a1-full 1000\nsha-b1-full 3000\nsha-b2-full 2000\n")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    result = mod.survey(tmp_path)
    assert len(git_log_calls) == 1
    for sha in ("sha-a1", "sha-b1", "sha-b2"):
        assert sha in git_log_calls[0]
    assert result.would_reclaim == 2

    b_reclaim = next(d for d in result.dispositions if d.path == str(h2))
    assert b_reclaim.sha == "sha-b1"  # MAX committer timestamp, not positional-first


def test_survey_ambiguous_holder_falls_through_to_release(tmp_path, monkeypatch):
    """P2: a holder claiming two in-flight handoffs at once cannot be
    disambiguated by a completion entry -- both fall through to release."""
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    h1 = _write_handoff(handoffs_dir, "a.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")
    h2 = _write_handoff(handoffs_dir, "b.md", status="consumed", deployment_state="in_flight", consumed_by="dead1")

    monkeypatch.setattr(mod, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(mod, "git_common_dir", lambda repo_root: repo_root / ".git")
    monkeypatch.setattr(
        mod, "has_live_children_many",
        _async_return({str(h1.resolve()): 1, str(h2.resolve()): 1}),
    )
    monkeypatch.setattr(mod, "_build_implemented_plan_index", lambda repo_root: {})
    monkeypatch.setattr(
        mod, "_build_completion_index",
        lambda repo_root: {"dead1": [{"frontmatter": {"commits": ["sha-a1"]}}]},
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: (_ for _ in ()).throw(
        AssertionError("ambiguous holders must never reach git-log")))

    result = mod.survey(tmp_path)
    assert result.would_release == 2
    assert result.would_reclaim == 0


# ===========================================================================
# apply_dispositions() — calls archive_stamp's verbs IN-PROCESS, never spawns
# ===========================================================================
class _Outcome:
    def __init__(self, exit_code=0, error=None):
        self.exit_code = exit_code
        self.error = error


def test_apply_release_calls_unclaim_handoff(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "cs_unclaim_handoff",
                        lambda path, reaped_from=None: (calls.append((path, reaped_from)), 0)[1])

    dispositions = [mod.Disposition("state/handoffs/a.md", "dead1", mod._VERDICT_RELEASE, "detail")]
    applied, failed = mod.apply_dispositions(dispositions)

    assert applied == ["state/handoffs/a.md"]
    assert failed == []
    assert calls == [("state/handoffs/a.md", "dead1")]


def test_apply_reclaim_shipped_calls_stamp_then_ship(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "stamp_shipped_in",
                        lambda path, kind=None, sha=None: (calls.append(("stamp", path, kind, sha)), _Outcome())[1])
    monkeypatch.setattr(mod, "cs_ship_handoff",
                        lambda path, sha=None: (calls.append(("ship", path, sha)), 0)[1])

    dispositions = [
        mod.Disposition("state/handoffs/a.md", "dead1", mod._VERDICT_RECLAIM_SHIPPED, "detail", sha="deadbeef")
    ]
    applied, failed = mod.apply_dispositions(dispositions)

    assert applied == ["state/handoffs/a.md"]
    assert failed == []
    assert [c[0] for c in calls] == ["stamp", "ship"]
    assert calls[0] == ("stamp", "state/handoffs/a.md", "ship-commit", "deadbeef")
    assert calls[1] == ("ship", "state/handoffs/a.md", "deadbeef")


def test_apply_never_spawns_a_subprocess(monkeypatch):
    """The standing amplification gate's own property, asserted locally: a reap of
    N dispositions must create ZERO processes on the write path. This is what a
    per-disposition archive-stamp-cli shell-out cost (N-2N interpreter starts)."""
    import subprocess as _sp
    monkeypatch.setattr(mod, "cs_unclaim_handoff", lambda path, reaped_from=None: 0)
    monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("apply_dispositions must not create a process")))
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("apply_dispositions must not create a process")))

    dispositions = [
        mod.Disposition(f"state/handoffs/{n}.md", "dead1", mod._VERDICT_RELEASE, "detail")
        for n in "abcde"
    ]
    applied, failed = mod.apply_dispositions(dispositions)
    assert applied == [f"state/handoffs/{n}.md" for n in "abcde"]
    assert failed == []


def test_apply_skip_verdicts_perform_no_write(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a skip verdict must never write")

    monkeypatch.setattr(mod, "cs_unclaim_handoff", _boom)
    monkeypatch.setattr(mod, "stamp_shipped_in", _boom)
    monkeypatch.setattr(mod, "cs_ship_handoff", _boom)

    dispositions = [
        mod.Disposition("state/handoffs/a.md", "dead1", mod._VERDICT_SKIP_LIVE_CHILDREN, "detail"),
        mod.Disposition("state/handoffs/b.md", "dead2", mod._VERDICT_SKIP_GOVERNED_PLAN, "detail"),
    ]
    applied, failed = mod.apply_dispositions(dispositions)
    assert applied == []
    assert failed == []


def test_apply_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(mod, "cs_unclaim_handoff", lambda path, reaped_from=None: 3)

    dispositions = [mod.Disposition("state/handoffs/a.md", "dead1", mod._VERDICT_RELEASE, "detail")]
    applied, failed = mod.apply_dispositions(dispositions)
    assert applied == []
    assert len(failed) == 1
    assert "rc=3" in failed[0]


def test_apply_reports_a_raising_verb_without_aborting_the_reap(monkeypatch):
    """One bad row must not strand the remaining dispositions."""
    def _raise_on_first(path, reaped_from=None):
        if path.endswith("a.md"):
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(mod, "cs_unclaim_handoff", _raise_on_first)
    dispositions = [
        mod.Disposition("state/handoffs/a.md", "dead1", mod._VERDICT_RELEASE, "detail"),
        mod.Disposition("state/handoffs/b.md", "dead1", mod._VERDICT_RELEASE, "detail"),
    ]
    applied, failed = mod.apply_dispositions(dispositions)
    assert applied == ["state/handoffs/b.md"]
    assert len(failed) == 1
    assert "boom" in failed[0]


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
