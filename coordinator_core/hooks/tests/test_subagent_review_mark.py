"""
Coverage for `coordinator_core.hooks.subagent_review_mark` — the SubagentStop
op that derives a commit-ledger review mark from a finishing reviewer's own
`reviewed_range` findings.

Spec backlink: docs/plans/2026-08-20-the-refusal-dies-and-the-mark-falls-out.md
§ C6 (AC4/AC5/AC7/AC8/AC13/AC14).

Fast tier by construction: every git call is stubbed at
`subagent_review_mark._git_rev_list`, so no test here spawns a process and
none needs the `spawns_process` + `cadence` marker pair the amplification
ratchet requires of a test that does. AC8's spawn-shape claim is asserted by
COUNTING stubbed calls and inspecting their argv, which is the property that
actually matters (one call carrying every declared range, never one per
range) — a real spawn would measure the same thing more expensively.

Negative-spec:
    - Does NOT assert `mark_reviewed`'s own ledger-file format. That is
      `commit_ledger.store`'s contract and its own tests' subject; these
      tests stub the call and assert what this op PASSES to it.
    - Does NOT exercise the SubagentStop shim. The shim lives in DoE-claude
      (C5, `writes: []` here) and is unreachable from this suite by
      construction — these tests enter at the registered op handler.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional
from unittest import mock

import pytest

from coordinator_core.hooks import subagent_review_mark as mod


_SESSION_ID = "a1b2c3d4-e5f6-4777-8888-99990000aaaa"
_AGENT_ID = "coordinatorcode-reviewer-fc901831"
_AGENT_TYPE = "code-reviewer"
#: The shape a real spawn writes: `<label>-<nonce>.md`. Deliberately NOT
#: `<label>.<agent_id>.md` -- see `_write_sidecar`.
_SIDECAR_NAME = "coordinatorcode-reviewer-36c5be9c.md"
_SIDECAR_REL = f"state/subagent-share/{_SESSION_ID}/{_SIDECAR_NAME}"
_HANDOFF_ID = "2026-08-20-the-refusal-dies-and-the-mark-falls-out"


#: Sentinel: `None` and `""` are both meaningful transcript values, so
#: neither can double as "the caller said nothing".
_UNSET = object()


def _run(coro):
    return asyncio.run(coro)


def _write_sidecar(worktree: Path, ranges, *, session_id: str = _SESSION_ID,
                   agent_type: str = _AGENT_TYPE,
                   filename: str = _SIDECAR_NAME) -> Path:
    """Author the run-report sidecar at the NONCE-named path a real spawn
    actually produces -- `<label>-<nonce>.md`, not the by-construction
    `<label>.<agent_id>.md` this suite used to assume.

    That assumption is the whole of the defect these tests now cover:
    `provision_report._provision` picks the name at PreToolUse, where
    `agent_id` does not exist, so no sidecar on disk has ever carried the
    derived name (0 of 6,503 measured). A fixture spelling a name no producer
    writes cannot fail when the op cannot reach it.
    """
    share = worktree / "state" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)
    body = "---\n"
    if ranges is not None:
        body += "reviewed_range:\n"
        for r in ranges:
            body += f"  - {r}\n"
    body += "verdict: OK\n---\n\nfindings\n"
    path = share / filename
    path.write_text(body, encoding="utf-8")
    return path


def _write_transcript(worktree: Path, sidecar_rel, *,
                      agent_id: str = _AGENT_ID) -> str:
    """Author the finishing agent's transcript at the harness's own
    `agent-<agent_id>.jsonl` shape, carrying the `sidecar_path:` marker
    `enforce-agent-dispatch-mode.py::_compose_sidecar_offer_text` appends to
    the child's prompt.

    The record is JSON-ENCODED, not raw prose, because that is what the op
    reads on the live path -- the marker sits inside a JSON string with its
    newlines escaped, and a fixture that skipped the encoding would not prove
    the terminator set survives it. `sidecar_rel=None` writes a transcript
    with no marker: a spawn that provisioned no sidecar.
    """
    d = worktree / ".transcripts"
    d.mkdir(parents=True, exist_ok=True)
    prompt = "Review the frozen slice.\n"
    if sidecar_rel is not None:
        prompt += (
            "\nYou have a run-report sidecar for this dispatch -- capture run "
            "notes and any divergence there.\nsidecar_path: " + sidecar_rel
        )
    prompt += "\n\nYou are acting as a reviewer.\n"
    path = d / f"agent-{agent_id}.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"content": []}})
        + "\n",
        encoding="utf-8",
    )
    return str(path)


def _write_pending_diff_record(worktree: Path, sha_range: str, *,
                               session_id: str = _SESSION_ID,
                               verdict: str = "pending",
                               scope_kind: str = "diff") -> Path:
    """A `verdict: pending`, `scope_kind: diff` review-trail record — the
    containment bound's on-disk source (AC13). `session_id` is stored in the
    TRUNCATED 8-char form the op compares against."""
    trail = worktree / "state" / "review-trail"
    trail.mkdir(parents=True, exist_ok=True)
    path = trail / f"{sha_range.replace('..', '-')}.json"
    path.write_text(json.dumps({
        "session_id": session_id[:8],
        "sha_range": sha_range,
        "verdict": verdict,
        "scope_kind": scope_kind,
    }), encoding="utf-8")
    return path


class _GitStub:
    """Stands in for `_git_rev_list`, recording every call's argv.

    `resolve` maps a joined-argv key to the SHA list that call returns;
    an unmapped key resolves to `None`, the op's own "resolution failed"
    signal.
    """

    def __init__(self, resolve: dict):
        self.resolve = resolve
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str], cwd: str) -> Optional[List[str]]:  # noqa: ARG002
        self.calls.append(list(args))
        return self.resolve.get(" ".join(args))


def _invoke(worktree: Path, git: _GitStub, marks: list, *,
            agent_type: str = _AGENT_TYPE, agent_id: str = _AGENT_ID,
            session_id: str = _SESSION_ID,
            transcript=_UNSET,
            handoff_id: Optional[str] = _HANDOFF_ID):
    """Drive the registered handler with every external seam stubbed.

    `transcript` defaults to one pointing at `_SIDECAR_REL` -- the REACHABLE
    case. Pass an explicit path, `None`, or `""` to vary it.
    """
    if transcript is _UNSET:
        transcript = _write_transcript(worktree, _SIDECAR_REL, agent_id=agent_id)

    def _fake_mark(*args, **kwargs):
        marks.append((args, kwargs))
        return True

    with mock.patch.object(mod, "_git_rev_list", git), \
            mock.patch.object(mod, "resolve_owner_handoff_id",
                              return_value=(handoff_id, False)), \
            mock.patch.object(mod.ledger_store, "mark_reviewed", _fake_mark):
        return _run(mod._handler({
            "session_id": session_id,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "agent_transcript_path": transcript or "",
            "cwd": str(worktree),
        }, repo_root=str(worktree)))


# ---------------------------------------------------------------------------
# AC4 — the op is registered, and registered by MEMBERSHIP in the eager list
# ---------------------------------------------------------------------------

def test_op_is_registered_under_its_own_name() -> None:
    """AC4: decorating the handler is not enough — the op must be reachable
    through the registry every caller dispatches by name."""
    import coordinator_core.ops  # noqa: F401 — populates the registry
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("hooks.subagent_review_mark") is not None


def test_module_is_in_the_eager_hook_module_list() -> None:
    """AC4 (staff-eng review N2): membership in `_EAGER_HOOK_MODULES` is the
    registration mechanism every sibling hook op uses. Omitting it leaves the
    op written, decorated, and unreachable — a state no other assertion in
    this module would catch, because importing it here registers it."""
    from coordinator_core.hooks import _EAGER_HOOK_MODULES

    assert "coordinator_core.hooks.subagent_review_mark" in _EAGER_HOOK_MODULES


# ---------------------------------------------------------------------------
# AC5 — the mark itself, over the happy path
# ---------------------------------------------------------------------------

def test_marks_the_reviewed_shas_with_agent_and_sidecar_provenance(tmp_path: Path) -> None:
    """AC5: a finishing reviewer whose sidecar declares a `reviewed_range`
    inside this session's frozen bound produces one `mark_reviewed` call
    carrying the resolved SHAs, the reviewer, and both provenance fields."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({
        "aaa111..bbb222": ["sha_a", "sha_b"],
    })
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert len(marks) == 1, f"expected exactly one ledger append, got {marks!r}"
    args, kwargs = marks[0]
    assert args[0] == _HANDOFF_ID
    assert sorted(args[1]) == ["sha_a", "sha_b"]
    assert args[2] == _AGENT_TYPE
    assert kwargs["agent_id"] == _AGENT_ID
    assert kwargs["sidecar_path"] == _SIDECAR_REL, (
        "sidecar_path must be the forward-slash relative path, not an absolute one"
    )


def test_handler_always_returns_no_advisory(tmp_path: Path) -> None:
    """AC5: this op never denies and never advises — its only observable
    effect is the append. A hook that starts advising on SubagentStop writes
    into the SUBAGENT's context, not the EM's."""
    from coordinator_core.hooks._envelope import no_advisory

    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    result = _invoke(tmp_path, git, marks)

    assert result == no_advisory()


# ---------------------------------------------------------------------------
# AC13 — the containment bound
# ---------------------------------------------------------------------------

def test_reviewed_range_exceeding_the_frozen_slice_does_not_expand_the_mark(
    tmp_path: Path,
) -> None:
    """AC13, the load-bearing case: a `reviewed_range` resolving to commits
    OUTSIDE every range this session actually froze must not carry those
    commits into the mark. A self-report is not a coverage claim."""
    _write_sidecar(tmp_path, ["aaa111..zzz999"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({
        "aaa111..zzz999": ["sha_a", "sha_b", "sha_outside", "sha_also_outside"],
        "aaa111..bbb222": ["sha_a", "sha_b"],
    })
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert len(marks) == 1
    admitted = sorted(marks[0][0][1])
    assert admitted == ["sha_a", "sha_b"], (
        "the mark must be the INTERSECTION with the frozen bound — "
        f"got {admitted!r}, which admits commits this session never froze"
    )


def test_reviewed_range_wholly_outside_the_bound_marks_nothing(tmp_path: Path) -> None:
    """AC13/AC7: an empty intersection is fail-quiet, not a degenerate
    zero-sha append."""
    _write_sidecar(tmp_path, ["ccc333..ddd444"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({
        "ccc333..ddd444": ["sha_far"],
        "aaa111..bbb222": ["sha_a"],
    })
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


def test_no_pending_diff_record_means_no_bound_and_no_mark(tmp_path: Path) -> None:
    """AC13: an unenumerable bound admits NOTHING rather than everything —
    the fail-closed direction. A session that froze no diff has no slice a
    reviewer could have been dispatched against."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


def test_bound_ignores_another_sessions_pending_record(tmp_path: Path) -> None:
    """AC13: the bound is THIS session's own frozen ranges. A peer's pending
    record on the shared branch must not widen it."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(
        tmp_path, "aaa111..bbb222", session_id="ffffffff-0000-4000-8000-000000000000",
    )
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


@pytest.mark.parametrize("verdict,scope_kind", [
    ("ok", "diff"),
    ("pending", "plan"),
    ("pending", "integration"),
])
def test_bound_ignores_records_that_are_not_pending_diff(
    tmp_path: Path, verdict: str, scope_kind: str,
) -> None:
    """AC13: only a `verdict: pending` + `scope_kind: diff` record represents
    a frozen review slice. A closed record or a plan review is not one."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(
        tmp_path, "aaa111..bbb222", verdict=verdict, scope_kind=scope_kind,
    )
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


# ---------------------------------------------------------------------------
# AC8 — spawn shape
# ---------------------------------------------------------------------------

def test_every_reviewed_range_resolves_in_one_call(tmp_path: Path) -> None:
    """AC8: N declared ranges cost ONE `git rev-list` call carrying all N in
    a single argv — never one call per range, the per-item shape the
    amplification gate polices."""
    _write_sidecar(tmp_path, ["r1..r2", "r3..r4", "r5..r6"])
    _write_pending_diff_record(tmp_path, "b1..b2")
    git = _GitStub({
        "r1..r2 r3..r4 r5..r6": ["sha_a"],
        "b1..b2": ["sha_a"],
    })
    marks: list = []

    _invoke(tmp_path, git, marks)

    reviewed_calls = [c for c in git.calls if "r1..r2" in c]
    assert len(reviewed_calls) == 1, (
        f"three ranges must resolve in one call, got {reviewed_calls!r}"
    )
    assert reviewed_calls[0] == ["r1..r2", "r3..r4", "r5..r6"]


def test_containment_ranges_are_resolved_one_call_each(tmp_path: Path) -> None:
    """AC8 negative-spec: the containment ranges are deliberately NOT batched,
    with each other or with `reviewed_range`. `git rev-list` applies exclusions
    GLOBALLY across its argv, so batching two ranges can silently narrow the
    result — the bound would then admit less than the session actually froze.
    This is the one place a per-range call is correct, and it is bounded by
    this session's own freeze count, never by a growing set."""
    _write_sidecar(tmp_path, ["r1..r2"])
    _write_pending_diff_record(tmp_path, "b1..b2")
    _write_pending_diff_record(tmp_path, "b3..b4")
    git = _GitStub({
        "r1..r2": ["sha_a"],
        "b1..b2": ["sha_a"],
        "b3..b4": ["sha_b"],
    })
    marks: list = []

    _invoke(tmp_path, git, marks)

    bound_calls = [c for c in git.calls if c and c[0].startswith("b")]
    assert bound_calls == [["b1..b2"], ["b3..b4"]], (
        "each containment range gets its own call; batching them risks a "
        f"globally-applied exclusion silently narrowing the bound — got {bound_calls!r}"
    )


# ---------------------------------------------------------------------------
# Sidecar resolution — read off the finishing agent's own transcript.
#
# This section SUPERSEDES the AC14 by-construction coverage it replaces. Those
# tests asserted that `<label>.<agent_id>.md` was COMPUTED correctly (it was)
# and that a nonce-named sidecar was unreachable (it was) -- and both passed
# while the op could not fire for a single real dispatch, because every real
# sidecar is nonce-named. An unreachability assertion needs a REACHABLE case
# beside it or it only measures its own precondition; `test_a_nonce_named_
# sidecar_is_reached_through_the_transcript` is that case, and it is the one
# test here that would have caught the original defect.
# ---------------------------------------------------------------------------

def test_a_nonce_named_sidecar_is_reached_through_the_transcript(tmp_path: Path) -> None:
    """THE REACHABLE CASE. A sidecar named the way a real spawn names it, found
    through the transcript the harness bound to this agent_id, produces a mark.

    Every other test in this section constrains that path; this one proves it
    exists at all."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert len(marks) == 1
    _args, kwargs = marks[0]
    assert kwargs["sidecar_path"] == _SIDECAR_REL
    assert kwargs["agent_id"] == _AGENT_ID


def test_the_marker_is_read_from_the_transcript_not_derived(tmp_path: Path) -> None:
    """The resolved path is whatever the marker SAYS, including a home the op
    could never have derived (the § 2.7 plan-sidecars route). A resolver that
    reconstructed a path from session_id/label/agent_id passes every other test
    in this file and fails this one."""
    plan_rel = "state/plan-sidecars/2026-08-20-some-plan.code-review.md"
    (tmp_path / "state" / "plan-sidecars").mkdir(parents=True)
    (tmp_path / plan_rel).write_text(
        "---\nreviewed_range:\n  - aaa111..bbb222\n---\n", encoding="utf-8",
    )
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks,
            transcript=_write_transcript(tmp_path, plan_rel))

    assert len(marks) == 1
    assert marks[0][1]["sidecar_path"] == plan_rel


def test_the_by_construction_name_is_no_longer_consulted(tmp_path: Path) -> None:
    """Regression pin on the defect itself. A sidecar sitting at the OLD
    derived name, with no marker naming it, marks nothing -- the derivation is
    gone, not merely demoted to a fallback that would resurrect the failure on
    every dispatch that has no marker."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"],
                   filename=f"{_AGENT_TYPE}.{_AGENT_ID}.md")
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, transcript=_write_transcript(tmp_path, None))

    assert marks == []


def test_a_transcript_with_no_marker_marks_nothing(tmp_path: Path) -> None:
    """Fail-quiet: a spawn that provisioned no sidecar leaves no marker, and an
    absent marker declines rather than guessing. K-005's negative-spec lives
    here -- the tempting repair for this branch is the directory glob."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, transcript=_write_transcript(tmp_path, None))

    assert marks == []


@pytest.mark.parametrize("transcript", ["", None])
def test_an_unforwarded_transcript_path_marks_nothing(tmp_path: Path, transcript) -> None:
    """The relay leg. `agent_transcript_path` reaches this op only because the
    SubagentStop shim forwards it; a shim that does not leaves the op inert
    rather than marking off a reconstructed path."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, transcript=transcript)

    assert marks == []


def test_an_unreadable_transcript_marks_nothing_and_does_not_raise(tmp_path: Path) -> None:
    """AC7: a transcript path that does not resolve to a readable file is the
    ordinary degraded case, never a raise on a subagent's exit."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, transcript=str(tmp_path / "absent.jsonl"))

    assert marks == []


@pytest.mark.parametrize("value", [
    "/etc/passwd",
    "../../escape.md",
    "state/../../escape.md",
    "X:/claude-klabauter/state/subagent-share/s/x.md",
])
def test_a_marker_that_escapes_the_worktree_resolves_to_nothing(
    tmp_path: Path, value: str,
) -> None:
    """The marker is authored by another repo's hook and read out of a file the
    agent itself can write -- so it is untrusted input, not a path this op may
    join onto the worktree unchecked. Absolute, traversing, and drive-qualified
    values all decline.

    The drive-qualified case is here because `PurePosixPath("X:/a").is_absolute()`
    is False on every platform: without its own check it would join onto the
    worktree and read outside it on Windows, which is first-class here."""
    assert mod._SIDECAR_MARKER_RE.search(f"sidecar_path: {value}") is not None, (
        "fixture must reach the resolver's path check, not die at the regex"
    )
    transcript = _write_transcript(tmp_path, value)

    assert mod._resolve_sidecar_from_transcript(transcript) is None


def test_only_the_first_marker_is_read(tmp_path: Path) -> None:
    """A reviewer's own findings can quote a sidecar path (this suite's
    fixtures do). The injected marker is in the FIRST user message, so the
    first match wins and a later quotation cannot redirect the mark."""
    d = tmp_path / ".transcripts"
    d.mkdir(parents=True)
    path = d / f"agent-{_AGENT_ID}.jsonl"
    path.write_text(
        json.dumps({"message": {"content": f"sidecar_path: {_SIDECAR_REL}"}}) + "\n"
        + json.dumps({"message": {"content": "sidecar_path: state/other/quoted.md"}})
        + "\n",
        encoding="utf-8",
    )

    assert mod._resolve_sidecar_from_transcript(str(path)) == Path(
        "state", "subagent-share", _SESSION_ID, _SIDECAR_NAME,
    )


def test_the_scan_is_bounded(tmp_path: Path, monkeypatch) -> None:
    """The read is capped: a marker beyond the limit is not found. Reviewer
    transcripts routinely run to hundreds of KB and this hook fires on every
    SubagentStop fleet-wide, so an unbounded read is a fleet-wide cost, not a
    local one."""
    monkeypatch.setattr(mod, "_TRANSCRIPT_SCAN_LIMIT_BYTES", 64)
    transcript = _write_transcript(tmp_path, _SIDECAR_REL)

    assert mod._resolve_sidecar_from_transcript(transcript) is None


# ---------------------------------------------------------------------------
# AC7 — fail-quiet on every absent or unresolvable input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent_type", [
    "executor",            # a real agent type, but not a reviewer
    "enricher",
    "waived",              # a justification value, deliberately not a delegate
    "em-verified",
    "not-an-agent-at-all",
])
def test_a_non_reviewer_agent_marks_nothing(tmp_path: Path, agent_type: str) -> None:
    """AC7/step 1: the gate is `review_trail_write._DELEGATE_REVIEWERS`, the
    same closed vocabulary `review_trail.write` enforces — reused, so the two
    surfaces cannot drift into disagreeing about who is a reviewer."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"], agent_type=agent_type)
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, agent_type=agent_type)

    assert marks == []
    assert git.calls == [], "a non-reviewer must not reach git at all"


def test_a_namespaced_reviewer_type_still_passes_the_gate() -> None:
    """Step 1: `coordinator:code-reviewer` is the same reviewer as
    `code-reviewer` — the vocabulary is spelled bare, and the harness spells
    it namespaced."""
    assert mod._is_reviewer("coordinator:code-reviewer") is True
    assert mod._is_reviewer("agent:staff-eng") is True
    assert mod._is_reviewer("coordinator:executor") is False


def test_a_missing_sidecar_marks_nothing(tmp_path: Path) -> None:
    """AC7: the ordinary case for a reviewer dispatched before the naming fix,
    or one that wrote no report at all."""
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


@pytest.mark.parametrize("ranges", [None, [], [""], [123, None]])
def test_an_absent_or_unusable_reviewed_range_marks_nothing(tmp_path: Path, ranges) -> None:
    """AC7: `reviewed_range` absent, empty, or not a list of non-empty strings.

    A SCALAR `reviewed_range` reads as absent here for the same reason it does
    on the trail-write side — the field is a YAML list by contract."""
    _write_sidecar(tmp_path, ranges)
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


def test_a_scalar_reviewed_range_reads_as_absent(tmp_path: Path) -> None:
    """AC7: the same scalar-vs-list trap `review_trail.write` already carries.
    Written explicitly because a scalar LOOKS like a declaration and silently
    is not."""
    share = tmp_path / "state" / "subagent-share" / _SESSION_ID
    share.mkdir(parents=True)
    (share / f"{_AGENT_TYPE}.{_AGENT_ID}.md").write_text(
        "---\nreviewed_range: aaa111..bbb222\n---\n", encoding="utf-8",
    )
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


def test_a_standalone_agent_with_no_held_baton_marks_nothing(tmp_path: Path) -> None:
    """AC7: `resolve_owner_handoff_id` returning None is a legitimate
    outcome (a dispatch outside any held baton), never a raise."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, handoff_id=None)

    assert marks == []


def test_an_owner_resolution_error_marks_nothing_and_does_not_raise(tmp_path: Path) -> None:
    """AC7: a hook that raises on a subagent's exit is worse than a missing
    mark."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    def _fake_mark(*args, **kwargs):
        marks.append((args, kwargs))
        return True

    with mock.patch.object(mod, "_git_rev_list", git), \
            mock.patch.object(mod, "resolve_owner_handoff_id",
                              side_effect=ValueError("no owner")), \
            mock.patch.object(mod.ledger_store, "mark_reviewed", _fake_mark):
        result = _run(mod._handler({
            "session_id": _SESSION_ID,
            "agent_id": _AGENT_ID,
            "agent_type": _AGENT_TYPE,
            "cwd": str(tmp_path),
        }, repo_root=str(tmp_path)))

    assert marks == []
    assert result is not None


def test_a_failed_reviewed_range_resolution_marks_nothing(tmp_path: Path) -> None:
    """AC7: `git rev-list` failing over the declared range (a bad ref, a
    range naming commits not in this clone) resolves to None."""
    _write_sidecar(tmp_path, ["bogus..range"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})  # "bogus..range" unmapped -> None
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == []


@pytest.mark.parametrize("missing", ["session_id", "agent_id", "agent_type"])
def test_a_missing_required_payload_field_marks_nothing(tmp_path: Path, missing: str) -> None:
    """AC7: `""` is treated as absent, per `_payload.field()`."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    payload = {
        "session_id": _SESSION_ID,
        "agent_id": _AGENT_ID,
        "agent_type": _AGENT_TYPE,
        "cwd": str(tmp_path),
    }
    payload[missing] = ""

    def _fake_mark(*args, **kwargs):
        marks.append((args, kwargs))
        return True

    with mock.patch.object(mod, "_git_rev_list", git), \
            mock.patch.object(mod, "resolve_owner_handoff_id",
                              return_value=(_HANDOFF_ID, False)), \
            mock.patch.object(mod.ledger_store, "mark_reviewed", _fake_mark):
        _run(mod._handler(payload, repo_root=str(tmp_path)))

    assert marks == []


def test_no_repo_root_marks_nothing(tmp_path: Path) -> None:
    """AC7: without a worktree there is no sidecar to read and no ledger to
    append to."""
    marks: list = []
    git = _GitStub({})

    def _fake_mark(*args, **kwargs):
        marks.append((args, kwargs))
        return True

    with mock.patch.object(mod, "_git_rev_list", git), \
            mock.patch.object(mod.ledger_store, "mark_reviewed", _fake_mark):
        _run(mod._handler({
            "session_id": _SESSION_ID,
            "agent_id": _AGENT_ID,
            "agent_type": _AGENT_TYPE,
            "cwd": str(tmp_path),
        }, repo_root=None))

    assert marks == []


# ---------------------------------------------------------------------------
# C2 — the COMPLETION leg: `review_completion:` stamped at SubagentStop,
# keyed (session, agent_id), independent of reviewed_range/handoff/git.
#
# Spec backlink: docs/plans/2026-09-11-review-receipt-records-completion-
# not-dispatch.md § C2.
# ---------------------------------------------------------------------------

def _receipt_yaml(session_id: str, agent_id: str, agent_type: str,
                   stamped_at: str = "2026-09-01T00:00:00Z") -> str:
    """The real ``review_receipt:`` block shape, built through C1's own
    ``_receipt_block`` rather than hand-rolled, so a fixture never drifts
    from what dispatch actually stamps."""
    from coordinator_core.subagent_sandbox.provision_report import _receipt_block

    return _receipt_block("review_receipt", session_id, agent_id, agent_type, stamped_at)


def _write_sidecar_with_receipt(
    worktree: Path, ranges, *,
    session_id: str = _SESSION_ID,
    agent_type: str = _AGENT_TYPE,
    filename: str = _SIDECAR_NAME,
    receipt_session_id: Optional[str] = None,
    receipt_agent_id: str = "",
    receipt_agent_type: Optional[str] = None,
    with_receipt: bool = True,
    with_completion: bool = False,
    body: str = "findings\n",
) -> Path:
    """A sidecar carrying both a `reviewed_range` (MARK leg input) and a
    `review_receipt` (COMPLETION leg input), independently controllable so a
    test can vary either without disturbing the other."""
    share = worktree / "state" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)
    receipt_session_id = session_id if receipt_session_id is None else receipt_session_id
    receipt_agent_type = agent_type if receipt_agent_type is None else receipt_agent_type

    doc = "---\n"
    if ranges is not None:
        doc += "reviewed_range:\n"
        for r in ranges:
            doc += f"  - {r}\n"
    doc += "verdict: OK\n"
    if with_receipt:
        doc += _receipt_yaml(receipt_session_id, receipt_agent_id, receipt_agent_type)
    if with_completion:
        from coordinator_core.subagent_sandbox.provision_report import _receipt_block

        doc += _receipt_block("review_completion", receipt_session_id, receipt_agent_id,
                               receipt_agent_type, "2026-08-01T00:00:00Z")
    doc += "---\n\n" + body
    path = share / filename
    path.write_text(doc, encoding="utf-8")
    return path


def _read_fm(path: Path) -> dict:
    import yaml as _yaml

    from coordinator_core.frontmatter.primitives import split_frontmatter

    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None, "sidecar must still carry parseable frontmatter"
    return _yaml.safe_load(split.fm_text) or {}


def test_completion_leg_stamps_a_filled_reviewer_with_the_payload_agent_id(
    tmp_path: Path,
) -> None:
    """AC: a fixture reviewer with a filled body and a session-matching
    receipt gains exactly one `review_completion:` block whose `agent_id` is
    the PAYLOAD's, and its frontmatter still parses."""
    sidecar = _write_sidecar_with_receipt(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    fm = _read_fm(sidecar)
    assert fm["review_completion"]["agent_id"] == _AGENT_ID
    assert fm["review_completion"]["session_id"] == _SESSION_ID
    assert fm["review_receipt"]["agent_id"] == ""  # untouched


def test_completion_leg_stamps_the_pipeline_run_report_and_leaves_the_findings_twin_untouched(
    tmp_path: Path,
) -> None:
    """AC: a pipeline-shaped fixture -- an unfilled SubagentStart run-report
    named by the transcript marker, beside a filled `agent_id: ''` findings
    sidecar no marker names -- stamps the run-report and leaves the findings
    sidecar byte-unchanged. Only the marker-named sidecar is ever resolved
    (census row 2's twin no marker names is unreachable by construction)."""
    run_report = _write_sidecar_with_receipt(
        tmp_path, None, receipt_agent_id="", body="",
    )
    findings_twin = _write_sidecar_with_receipt(
        tmp_path, None, filename="coordinatorcode-reviewer-findings-twin.md",
        receipt_agent_id="",
    )
    findings_bytes_before = findings_twin.read_bytes()
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks)

    fm = _read_fm(run_report)
    assert fm["review_completion"]["agent_id"] == _AGENT_ID
    assert findings_twin.read_bytes() == findings_bytes_before


def test_completion_leg_stamps_the_payload_agent_id_over_an_empty_receipt_agent_id(
    tmp_path: Path,
) -> None:
    """AC: a receipt carrying `agent_id: ''` on the marker-named sidecar is
    stamped with the payload agent_id."""
    sidecar = _write_sidecar_with_receipt(tmp_path, None, receipt_agent_id="")
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks)

    fm = _read_fm(sidecar)
    assert fm["review_completion"]["agent_id"] == _AGENT_ID


def test_no_receipt_stamps_nothing(tmp_path: Path) -> None:
    """AC (no-write case): no `review_receipt` at all."""
    sidecar = _write_sidecar_with_receipt(tmp_path, None, with_receipt=False)
    before = sidecar.read_bytes()
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert sidecar.read_bytes() == before


def test_a_session_mismatched_receipt_stamps_nothing(tmp_path: Path) -> None:
    """AC (no-write case): the sidecar's `review_receipt.session_id` does not
    match the payload's `session_id`."""
    sidecar = _write_sidecar_with_receipt(
        tmp_path, None, receipt_session_id="ffffffff-0000-4000-8000-000000000000",
    )
    before = sidecar.read_bytes()
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert sidecar.read_bytes() == before


def test_an_existing_completion_block_stamps_nothing(tmp_path: Path) -> None:
    """AC (no-write case): a `review_completion:` block already present --
    a SubagentStop re-fire or a resumed agent must not produce a duplicate
    YAML key."""
    sidecar = _write_sidecar_with_receipt(tmp_path, None, with_completion=True)
    before = sidecar.read_bytes()
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert sidecar.read_bytes() == before


def test_an_absent_transcript_stamps_nothing(tmp_path: Path) -> None:
    """AC (no-write case): an absent/markerless transcript -- the sidecar is
    never resolved at all."""
    _write_sidecar_with_receipt(tmp_path, None)
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks, transcript=_write_transcript(tmp_path, None))

    # No exception, no crash; nothing to assert on the sidecar since it was
    # never even opened -- the absence of a raise IS the assertion.


def test_a_mis_anchored_doc_stamps_nothing(tmp_path: Path) -> None:
    """AC (no-write case), covered end to end here per C1(b): the finishing
    reviewer authored an `---\\n\\n` divider in the BODY, so the first such
    sequence in the whole doc is not the frontmatter's own closing fence.
    Census row 3 finds 3 of 922 agent-rewritten sidecars in this shape."""
    session_id = _SESSION_ID
    share = tmp_path / "state" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)
    doc = (
        "---\n"
        + _receipt_yaml(session_id, "", _AGENT_TYPE)
        + "---\n"
        "Body starts immediately, no blank line after the real fence.\n"
        "\n---\n\nA rendered divider inside the body.\n"
    )
    sidecar = share / _SIDECAR_NAME
    sidecar.write_text(doc, encoding="utf-8")
    before = sidecar.read_bytes()
    git = _GitStub({})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert sidecar.read_bytes() == before


def test_overengineering_reviewer_is_stamped_but_produces_no_ledger_mark(
    tmp_path: Path,
) -> None:
    """AC: `overengineering-reviewer` is CLOSE-only (census row 8) -- the
    COMPLETION leg's broader gate stamps it, but the MARK leg's narrower
    `_is_reviewer` gate still excludes it."""
    sidecar = _write_sidecar_with_receipt(
        tmp_path, ["aaa111..bbb222"], agent_type="overengineering-reviewer",
    )
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, agent_type="overengineering-reviewer")

    fm = _read_fm(sidecar)
    assert fm["review_completion"]["agent_id"] == _AGENT_ID
    assert marks == []


def test_code_reviewer_is_stamped_and_still_marks_exactly_as_before(
    tmp_path: Path,
) -> None:
    """AC: a `code-reviewer` fixture is stamped AND still marks exactly as
    before -- the two products coexist for a DELEGATE reviewer."""
    sidecar = _write_sidecar_with_receipt(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a", "sha_b"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    fm = _read_fm(sidecar)
    assert fm["review_completion"]["agent_id"] == _AGENT_ID
    assert len(marks) == 1
    assert sorted(marks[0][0][1]) == ["sha_a", "sha_b"]


def test_completion_stamp_lands_when_resolve_owner_handoff_id_returns_none(
    tmp_path: Path,
) -> None:
    """AC: the completion stamp lands even for a standalone session with no
    held baton -- the leg does not depend on handoff resolution at all."""
    sidecar = _write_sidecar_with_receipt(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks, handoff_id=None)

    fm = _read_fm(sidecar)
    assert fm["review_completion"]["agent_id"] == _AGENT_ID
    assert marks == []


def test_completion_stamp_only_fires_for_the_payloads_own_session_id(
    tmp_path: Path,
) -> None:
    """AC (F5): pins which `session_id` the real SubagentStop relay payload
    carries -- the same key `_invoke` forwards on every other test in this
    file (`session_id`, `agent_id`, `agent_type`, `agent_transcript_path`,
    `cwd`), matching DoE's shim (`subagent-zero-tool-use-detect.py`'s own
    documented SubagentStop payload shape). A mismatch on that value must
    leave the stamp unwritten even though every other input is valid."""
    sidecar = _write_sidecar_with_receipt(
        tmp_path, ["aaa111..bbb222"],
        receipt_session_id="ffffffff-0000-4000-8000-000000000000",
    )
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    fm = _read_fm(sidecar)
    assert "review_completion" not in fm


def test_an_unreadable_review_trail_record_does_not_break_the_bound(tmp_path: Path) -> None:
    """AC7: one corrupt record is skipped; the remaining records still form
    the bound. A single bad file must not silently empty it."""
    _write_sidecar(tmp_path, ["aaa111..bbb222"])
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    (tmp_path / "state" / "review-trail" / "corrupt.json").write_text(
        "{not json", encoding="utf-8",
    )
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert len(marks) == 1
    assert sorted(marks[0][0][1]) == ["sha_a"]
