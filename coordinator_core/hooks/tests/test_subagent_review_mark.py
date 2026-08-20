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
_HANDOFF_ID = "2026-08-20-the-refusal-dies-and-the-mark-falls-out"


def _run(coro):
    return asyncio.run(coro)


def _write_sidecar(worktree: Path, ranges, *, session_id: str = _SESSION_ID,
                   agent_id: str = _AGENT_ID, agent_type: str = _AGENT_TYPE) -> Path:
    """Author the run-report sidecar at the BY-CONSTRUCTION path (AC14):
    `state/subagent-share/<session_id>/<label>.<agent_id>.md`."""
    share = worktree / "state" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)
    body = "---\n"
    if ranges is not None:
        body += "reviewed_range:\n"
        for r in ranges:
            body += f"  - {r}\n"
    body += "verdict: OK\n---\n\nfindings\n"
    path = share / f"{agent_type}.{agent_id}.md"
    path.write_text(body, encoding="utf-8")
    return path


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
            handoff_id: Optional[str] = _HANDOFF_ID):
    """Drive the registered handler with every external seam stubbed."""
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
    assert kwargs["sidecar_path"] == (
        f"state/subagent-share/{_SESSION_ID}/{_AGENT_TYPE}.{_AGENT_ID}.md"
    ), "sidecar_path must be the forward-slash relative path, not an absolute one"


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
# AC14 — the sidecar resolves by construction, never by scan
# ---------------------------------------------------------------------------

def test_sidecar_path_is_derived_from_label_and_agent_id() -> None:
    """AC14: `<label>.<agent_id>.md`, computed — the nonce-named shape had no
    `agent_id` binding at all and was unresolvable by construction."""
    path = mod._resolve_sidecar_path(_SESSION_ID, _AGENT_TYPE, _AGENT_ID)

    assert path == Path("state") / "subagent-share" / _SESSION_ID / (
        f"{_AGENT_TYPE}.{_AGENT_ID}.md"
    )


def test_a_nonce_named_sidecar_is_not_found_by_a_glob(tmp_path: Path) -> None:
    """AC14 negative-spec: a pre-existing `<label>-<nonce>.md` sidecar stays
    permanently unresolvable. If this op ever grows the directory scan K-005
    condemned by name, this test goes green for the wrong reason and the
    no-backfill rule is quietly gone."""
    share = tmp_path / "state" / "subagent-share" / _SESSION_ID
    share.mkdir(parents=True)
    (share / f"{_AGENT_TYPE}-deadbeef.md").write_text(
        "---\nreviewed_range:\n  - aaa111..bbb222\n---\n", encoding="utf-8",
    )
    _write_pending_diff_record(tmp_path, "aaa111..bbb222")
    git = _GitStub({"aaa111..bbb222": ["sha_a"]})
    marks: list = []

    _invoke(tmp_path, git, marks)

    assert marks == [], "a nonce-named sidecar must not be reachable"


@pytest.mark.parametrize("agent_id", ["../escape", "has/slash", "has\\backslash"])
def test_a_malformed_agent_id_resolves_to_no_sidecar(agent_id: str) -> None:
    """AC14: an `agent_id` that does not survive sanitization unchanged
    resolves to nothing rather than to a guessed neighbour — the path is a
    filename component, never a path."""
    assert mod._resolve_sidecar_path(_SESSION_ID, _AGENT_TYPE, agent_id) is None


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
