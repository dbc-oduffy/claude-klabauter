"""Tests for coordinator_core.quick_wrap_assemble — the `/quick-wrap` computed skill.

Scope, post fl-core-02 C7 cutover: `quick_wrap_assemble` no longer owns any of the five
close-gate facts' own read logic — that lifted onto `coordinator_core.session.
session_facts` (DR-323). What remains here is the ceremony-level composition:
`_entry_test`'s four-condition verdict, `_judgment_points`' emission, `_directives`, the
degraded-fact fallbacks (`_closed_pickup_kind`/`_closed_governing_plan`/`_closed_diff`/
`_degraded_probe_judgment_point`), and `brief()`'s envelope assembly. Per-fact read
behaviour (frontmatter parsing, git-backed scans, degraded/collision posture) is tested
against the facade directly in `coordinator_core/session/tests/test_session_facts.py` —
DR-323 § (c)'s test-home split.

The load-bearing property under test is a CONTRACT one, per the governing baton's AC
("every producer added carries a contract test, so a silently-missing field fails
loudly"): `test_close_gate_emits_every_named_field` fails red if any of the five fields
stops being emitted, which is the failure mode that made quick-wrap's skill body carry
five `engine-gap` markers in the first place.

Spec backlink: state/handoffs/2026-08-14-fact-layer-library-sweep.md § Specification
Fold-in ask: cross-repo/archive/2026-08-14-doe-claude-em-quick-wrap-has-no-assembler-at-all.md
Cutover:      docs/plans/2026-08-18-five-close-gate-facts-onto-the-facade.md § C7
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coordinator_core import quick_wrap_assemble as qwa

_SID = "11111111-2222-3333-4444-555555555555"

#: The five fields the origin memo names. A field dropped from `brief()` must fail a
#: test, never degrade silently to an EM re-derivation.
_NAMED_FIELDS = (
    "pickup_kind",
    "governing_plan",
    "diff",
    "terminal_sizings",
    "fold_sidecars",
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A worktree skeleton with a session-shape record and no pickup."""
    common = tmp_path / ".git"
    _write(
        common / "coordinator-sessions" / _SID / "session-shape.json",
        json.dumps({"schema_version": 1, "session_id": _SID}),
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch):
    """Fail loudly on an unstubbed git call rather than silently shelling out to the
    developer's real repo — a test that reads the ambient tree passes for the wrong
    reason and stops catching regressions."""

    def _boom(args, cwd):
        raise AssertionError(f"unstubbed git call: {args}")

    monkeypatch.setattr(qwa, "_git_out", _boom)


def _computed_record(
    value: dict[str, Any], *, collision: bool | None = None, source: str = "test-source"
) -> dict[str, Any]:
    """A DR-319 Shape-1 (computed) record, for stubbing `qwa.session_facts`'s five
    served functions at the `brief()`-level contract tests below."""
    return {"degraded": False, "value": value, "source": source, "collision": collision}


def _degraded_record(evidence: str, *, source: str = "test-source") -> dict[str, Any]:
    """A DR-319 Shape-2 (degraded) record — no `value` key, per the facade's own
    contract."""
    return {"degraded": True, "evidence": evidence, "source": source}


def _stub_facts_all_computed(monkeypatch, repo: Path) -> None:
    """Common setup for `brief()`-level contract tests: every one of the five facts
    computed and clean. Stubs `qwa.session_facts`'s served functions directly — the
    seam `brief()` actually calls post-cutover — rather than the git-level `_git_out`
    stubbing this file used pre-cutover, since none of the five facts' own reads
    happen inside this module anymore."""
    monkeypatch.setattr(qwa, "_resolve_session", lambda root=None: (repo, repo / ".git", _SID))
    # The uncommitted-at-gate probe shells out to `git status` through
    # `baton_assemble._compute_dirty_tree_attribution`; the fixture worktree is a
    # skeleton, not a repo, so an unstubbed call would read as degraded and fail c2
    # closed in every test here. Stubbed to "committed and clean", the state these
    # brief()-level tests model.
    monkeypatch.setattr(qwa, "_uncommitted_at_gate", lambda root: None)
    monkeypatch.setattr(
        qwa.session_facts,
        "session_pickup_kind",
        lambda *a, **k: _computed_record(
            {
                "classification": "none",
                "artifact_path": None,
                "basename": None,
                "deliverable_id": None,
                "actioned_memos": [],
                "consumed_predecessor": False,
            }
        ),
    )
    monkeypatch.setattr(
        qwa.session_facts,
        "session_governing_plan",
        lambda *a, **k: _computed_record(
            {"present": False, "path": None, "status": None, "scope_mode": None, "slug": None},
            collision=False,
        ),
    )
    monkeypatch.setattr(
        qwa.session_facts,
        "session_diff_brightline",
        lambda *a, **k: _computed_record(
            {
                "scoping_method": "session_id_trailer",
                "trustworthy": True,
                "sha_range": None,
                "commit_count": 0,
                "surface_count": 0,
                "novel_surface_count": 0,
                "touched_paths": [],
                "gross_loc": 0,
                "doc_only_loc": 0,
                "relocated_files": 0,
                "novel_loc": 0,
                "novel_commit_count": 0,
                "brightline": {
                    "novel_loc": 500,
                    "novel_commit_count": 5,
                    "novel_surface_count": 4,
                },
                "breached": [],
                "under_brightline": True,
            }
        ),
    )
    monkeypatch.setattr(
        qwa.session_facts,
        "session_terminal_sizings",
        lambda *a, **k: _computed_record(
            {"scanned": 0, "terminal": [], "non_terminal_count": 0}, collision=False
        ),
    )
    monkeypatch.setattr(
        qwa.session_facts,
        "session_fold_sidecars",
        lambda *a, **k: _computed_record(
            {"present": False, "paths": [], "count": 0}, collision=False
        ),
    )


# ---------------------------------------------------------------------------
# Entry test
# ---------------------------------------------------------------------------


def _diff(**over):
    base = {
        "novel_loc": 0,
        "gross_loc": 0,
        "doc_only_loc": 0,
        "novel_commit_count": 0,
        "commit_count": 0,
        "surface_count": 0,
        "scoping_method": "session_id_trailer",
        "under_brightline": True,
        "breached": [],
    }
    base.update(over)
    return base


def test_entry_test_never_decides_condition_four(tmp_path: Path):
    """Condition 4 is genuine EM discretion — the carve-out the doctrine page preserves.
    An assembler that guesses it has overstepped from detection into deciding."""
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": False},
        _diff(),
        tmp_path,
    )

    c4 = [c for c in result["conditions"] if c["id"] == "c4"][0]
    assert c4["passed"] is None
    assert result["verdict"] == "computed-conditions-pass"


def test_chain_root_baton_passes_condition_three(tmp_path: Path):
    """SKILL.md's `‡` footnote: a chain-root baton (no predecessor, no
    additional_predecessors, no forked_from) carries no prior session's scope to
    account for — claiming and finishing one leaves no coverage gate owed."""
    artifact = tmp_path / "state" / "handoffs" / "b.md"
    _write(artifact, "---\npredecessor: none\n---\nbody\n")

    result = qwa._entry_test(
        {
            "consumed_predecessor": True,
            "classification": "spinoff",
            "artifact_path": "state/handoffs/b.md",
            "basename": "b.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
    assert c3["passed"] is True
    assert result["computed_failures"] == []
    assert result["route"] == "quick-wrap (pending condition 4)"


def test_predecessor_set_to_a_real_path_fails_condition_three(tmp_path: Path):
    artifact = tmp_path / "state" / "handoffs" / "b.md"
    _write(artifact, "---\npredecessor: state/handoffs/a.md\n---\nbody\n")

    result = qwa._entry_test(
        {
            "consumed_predecessor": True,
            "classification": "handoff",
            "artifact_path": "state/handoffs/b.md",
            "basename": "b.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    assert result["computed_failures"] == ["c3"]
    assert result["route"] == "/workstream-complete"


def test_additional_predecessors_non_empty_fails_condition_three(tmp_path: Path):
    artifact = tmp_path / "state" / "handoffs" / "b.md"
    _write(
        artifact,
        "---\npredecessor: none\nadditional_predecessors:\n  - state/handoffs/a.md\n---\nbody\n",
    )

    result = qwa._entry_test(
        {
            "consumed_predecessor": True,
            "classification": "handoff",
            "artifact_path": "state/handoffs/b.md",
            "basename": "b.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    assert result["computed_failures"] == ["c3"]
    assert result["route"] == "/workstream-complete"


def test_forked_from_set_fails_condition_three(tmp_path: Path):
    artifact = tmp_path / "state" / "handoffs" / "b.md"
    _write(artifact, "---\npredecessor: none\nforked_from: state/handoffs/a.md\n---\nbody\n")

    result = qwa._entry_test(
        {
            "consumed_predecessor": True,
            "classification": "spinoff",
            "artifact_path": "state/handoffs/b.md",
            "basename": "b.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    assert result["computed_failures"] == ["c3"]
    assert result["route"] == "/workstream-complete"


def test_nothing_claimed_passes_condition_three(tmp_path: Path):
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": False},
        _diff(),
        tmp_path,
    )

    c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
    assert c3["passed"] is True
    assert result["computed_failures"] == []


def test_unreadable_or_absent_claimed_artifact_fails_condition_three_closed(tmp_path: Path):
    """A claimed artifact that no longer resolves — archived mid-session to a
    location this session's pickup fact never found — fails CLOSED rather than
    passing on the absence of a read."""
    result = qwa._entry_test(
        {
            "consumed_predecessor": True,
            "classification": "handoff",
            "artifact_path": None,
            "basename": "b.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
    assert c3["passed"] is False
    assert "failing closed" in c3["evidence"]
    assert result["computed_failures"] == ["c3"]
    assert result["route"] == "/workstream-complete"


def test_memo_only_session_passes_condition_three(tmp_path: Path):
    """A memo-only session owes no coverage gate. `session_pickup_kind`'s memo
    branch records `actioned_memos[].basename` and never an `artifact_path`, so
    before this branch existed every such session fell into the no-`artifact_path`
    fail-closed case and was routed to `/workstream-complete` — the heavy close, for
    a session that consumed no chain at all. A memo has no predecessor/
    additional_predecessors/forked_from to read in the first place."""
    result = qwa._entry_test(
        {
            "consumed_predecessor": False,
            "classification": "memo",
            "artifact_path": None,
            "basename": "2026-08-19-doe-claude-em-some-memo.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
    assert c3["passed"] is True
    assert "not a baton" in c3["evidence"]
    assert result["computed_failures"] == []


def test_memo_branch_does_not_rescue_a_pathless_handoff_or_a_degraded_fact(tmp_path: Path):
    """Negative spec for the branch above: it keys on `classification == "memo"`
    ALONE and must never widen into "no artifact_path passes". A handoff whose path
    did not resolve, and the degraded-fact substitute, both keep failing CLOSED."""
    for classification in ("handoff", "spinoff", "degraded"):
        result = qwa._entry_test(
            {
                "consumed_predecessor": True,
                "classification": classification,
                "artifact_path": None,
                "basename": "b.md",
            },
            {"present": False},
            _diff(),
            tmp_path,
        )
        c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
        assert c3["passed"] is False, classification
        assert result["route"] == "/workstream-complete"


def test_claimed_artifact_path_present_but_file_missing_fails_condition_three_closed(
    tmp_path: Path,
):
    """`artifact_path` resolved at pickup time, but the file is gone by the time
    quick-wrap reads it — a genuinely unreadable claimed artifact, distinct from
    the no-`artifact_path`-at-all case above. Also fails CLOSED."""
    result = qwa._entry_test(
        {
            "consumed_predecessor": True,
            "classification": "handoff",
            "artifact_path": "state/handoffs/gone.md",
            "basename": "gone.md",
        },
        {"present": False},
        _diff(),
        tmp_path,
    )

    c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
    assert c3["passed"] is False
    assert "could not be read" in c3["evidence"]
    assert result["computed_failures"] == ["c3"]


def test_brightline_breach_alone_stays_in_quick_wrap(tmp_path: Path):
    """Condition 2 failing routes to scoped-review-then-wrap, NOT out of the ceremony —
    the skill's own table. Collapsing it into /workstream-complete would send every
    slightly-large light close down the heavy path."""
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": False},
        _diff(novel_loc=900, under_brightline=False, breached=["novel_loc"]),
        tmp_path,
    )

    assert result["verdict"] == "review-owed"
    assert result["route"] == "scoped review, then wrap"


def test_spec_dispatch_passes_condition_one_as_a_deferred_obligation(tmp_path: Path):
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": True, "scope_mode": "spec-dispatch", "path": "docs/plans/p.md"},
        _diff(),
        tmp_path,
    )

    c1 = [c for c in result["conditions"] if c["id"] == "c1"][0]
    assert c1["passed"] is True
    assert c1["deferred_obligation"] is True


def test_an_ordinary_governing_plan_routes_out(tmp_path: Path):
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": True, "scope_mode": "dispatch", "path": "docs/plans/p.md"},
        _diff(),
        tmp_path,
    )

    assert result["computed_failures"] == ["c1"]
    assert result["route"] == "/workstream-complete"


# ---------------------------------------------------------------------------
# Judgment points
# ---------------------------------------------------------------------------


def test_trustworthy_scoping_raises_no_ambiguity_judgment_point(repo: Path):
    point_ids = [
        p["id"]
        for p in qwa._judgment_points({"terminal": []}, {"trustworthy": True, "scoping_method": "x"})
    ]

    assert "j-scoping-ambiguous" not in point_ids


def test_judgment_points_conform_to_the_shipped_builder_shape(repo: Path):
    """Finding 1: hand-built points carried an invented `options` key, a dict `evidence`,
    and no `dispositions`/`reason`/`recommendation` — a shape no sibling producer emits,
    which slipped past `_emit` only because its one check keys on a `recommendation` these
    points never set."""
    diff = {"trustworthy": False, "scoping_method": qwa.SCOPING_METHOD_AMBIGUOUS}
    sizings = {"terminal": [{"path": "state/sizings/a.yaml", "status": "shipped", "dirty": True}]}

    points = qwa._judgment_points(sizings, diff)

    assert {p["id"] for p in points} == {
        "j-work-finished",
        "j-dirty-terminal-sizing",
        "j-scoping-ambiguous",
    }
    for point in points:
        assert "options" not in point, "the invented key must not come back"
        assert isinstance(point["evidence"], str), "evidence is a str in the canonical shape"
        assert point["dispositions"], "every point must offer dispositions"
        assert all("value" in d for d in point["dispositions"])
        assert point["reason"]
        # Untrusted gates carry no recommendation, but the key must be PRESENT and null —
        # an absent key is what let the demotion check pass vacuously.
        assert "recommendation" in point
        assert point["recommendation"] is None


# ---------------------------------------------------------------------------
# Envelope contract
# ---------------------------------------------------------------------------


def test_close_gate_emits_every_named_field(repo: Path, monkeypatch):
    """CONTRACT test. A field silently dropped from the envelope is the exact regression
    that put five `engine-gap` markers in quick-wrap's skill body; it must fail red here
    rather than degrade into an EM re-derivation nobody notices.

    Post-cutover, `close_gate[field]` is the raw DR-319 record (`degraded`/`value`/
    `source`/`collision`), not the bare payload the interim readers returned — so this
    test also pins that every field is a computed record with a `value` key, not merely
    a non-`None` dict."""
    _stub_facts_all_computed(monkeypatch, repo)

    envelope = qwa.brief()

    assert set(envelope) == {
        "artifact",
        "preflight",
        "gates",
        "directives",
        "judgment_points",
        "decisions",
        "narration",
        "next_move",
    }
    close_gate = envelope["gates"]["close_gate"]
    for field in _NAMED_FIELDS:
        assert field in close_gate, f"close_gate lost {field!r}"
        # Membership alone is not the contract. A field kept as a key but emitted as
        # `None` is a silently-missing field wearing a present key — the exact failure
        # this test exists to catch, and it passed a bare `in` check.
        assert close_gate[field] is not None, f"close_gate emitted {field!r} as None"
        assert isinstance(close_gate[field], dict), (
            f"close_gate[{field!r}] must be a fact object, got {type(close_gate[field])}"
        )
        assert close_gate[field]["degraded"] is False
        assert "value" in close_gate[field]
    assert "entry_test" in envelope["gates"]


def test_safe_commit_offer_is_a_directive_not_a_judgment_point(repo: Path, monkeypatch):
    """C5 (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-wrote.md
    § C5): being asked whether to commit was itself the defect, by explicit PM ruling —
    so this is now an in-process `commit_session_offer_async` call, never a directive an
    agent might not run and never a judgment point to decide."""
    _stub_facts_all_computed(monkeypatch, repo)

    calls: list[tuple[str, str, object]] = []

    async def _fake_auto_commit(session_id, cwd=None, groups=None):
        calls.append((session_id, cwd, groups))
        return {
            "session_id": session_id,
            "groups": [],
            "excluded": [],
            "failed_groups": [],
            "dropped_groups": [],
            "residue": {},
            "outcome": {
                "status": "empty",
                "detail": "no claimed path(s) to commit this call.",
                "committed_paths": [],
                "conflicted_paths": [],
            },
        }

    monkeypatch.setattr(qwa, "commit_session_offer_async", _fake_auto_commit)

    envelope = qwa.brief()

    # No framing argument: `invoker` was deleted 2026-08-27 (no producer could
    # substantiate the attended/unattended claim it selected). This stub's
    # signature is the guard -- it fails loud if the call re-grows one, which
    # `brief()`'s own fail-open would otherwise swallow into an error report.
    assert calls == [(_SID, str(repo), None)]
    assert "safe-commit-offer" not in [d["cli"] for d in envelope["directives"]]
    assert not any("commit" in p["question"].lower() for p in envelope["judgment_points"])
    assert envelope["gates"]["commit_outcome"]["status"] == "empty"
    assert envelope["gates"]["commit_outcome"]["residue"] == {}


def test_auto_commit_failure_does_not_block_the_ceremony(repo: Path, monkeypatch):
    """AC9: a failure inside the in-process auto-commit call must not prevent
    `brief()` from completing and returning a valid decision object."""
    _stub_facts_all_computed(monkeypatch, repo)

    async def _boom(session_id, cwd=None, groups=None, invoker=None):
        raise RuntimeError("simulated auto-commit failure")

    monkeypatch.setattr(qwa, "commit_session_offer_async", _boom)

    envelope = qwa.brief()

    assert envelope["gates"]["commit_outcome"]["status"] == "error"
    assert "simulated auto-commit failure" in envelope["gates"]["commit_outcome"]["detail"]
    assert "next_move" in envelope


def test_usage_error_exits_two_and_brief_is_the_only_subcommand():
    assert qwa.main([]) == int(qwa.QuickWrapExitCode.USAGE)
    assert qwa.main(["apply"]) == int(qwa.QuickWrapExitCode.USAGE)
    assert qwa.main(["brief", "--extra"]) == int(qwa.QuickWrapExitCode.USAGE)


def test_the_shim_registers_this_target():
    """A module with no entry in `ASSEMBLE_TARGETS` is unreachable from the bin
    trampoline — shipped and dead."""
    import importlib.util

    shim_path = (
        Path(__file__).resolve().parents[1]
        / "coordinator"
        / "bin"
        / "lib"
        / "entry_point_shim.py"
    )
    spec = importlib.util.spec_from_file_location("_eps", shim_path)
    assert spec is not None and spec.loader is not None, f"unloadable shim at {shim_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "quick-wrap-assemble" in module.ASSEMBLE_TARGETS
    assert "quick-wrap-assemble" in module._ENGINE_ENTRIES


# ---------------------------------------------------------------------------
# Degraded-fact posture (fl-core-02 C7) — the call-site conversions this
# chunk makes: a degraded DR-319 record from any of the five served facts
# must never be read via a silent `.get("value")` that resolves to `None`.
# ---------------------------------------------------------------------------


def test_degraded_pickup_kind_fails_condition_three_closed_and_visibly(tmp_path: Path):
    """A pickup-kind read that could not complete is treated as having consumed a
    predecessor — fails c3 closed, routes to `/workstream-complete` — rather than
    silently reading as `classification: None` and passing c3 by accident."""
    result = qwa._entry_test(
        qwa._closed_pickup_kind(),
        {"present": False},
        _diff(),
        tmp_path,
    )

    c3 = [c for c in result["conditions"] if c["id"] == "c3"][0]
    assert c3["passed"] is False
    assert "degraded" in c3["evidence"]
    assert result["computed_failures"] == ["c3"]
    assert result["route"] == "/workstream-complete"


def test_degraded_governing_plan_fails_condition_one_closed_and_visibly(tmp_path: Path):
    """A governing-plan read that could not complete is treated as plan-governed with a
    non-deferred scope_mode — fails c1 closed, routes to `/workstream-complete` — the
    same dangerous-direction bug DR-323 names for the original `_read_governing_plan`,
    refused here rather than repeated."""
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        qwa._closed_governing_plan(),
        _diff(),
        tmp_path,
    )

    c1 = [c for c in result["conditions"] if c["id"] == "c1"][0]
    assert c1["passed"] is False
    assert c1["deferred_obligation"] is False
    assert "degraded" in c1["evidence"]
    assert result["computed_failures"] == ["c1"]
    assert result["route"] == "/workstream-complete"


def test_degraded_diff_fails_condition_two_closed_and_visibly(tmp_path: Path):
    """A diff-brightline read that could not complete is treated as breaching it —
    fails c2 closed, routes to a scoped review — with `None`, never a fabricated `0`,
    carried into the evidence string (a `0` would read as "genuinely clean")."""
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": False},
        qwa._closed_diff(),
        tmp_path,
    )

    c2 = [c for c in result["conditions"] if c["id"] == "c2"][0]
    assert c2["passed"] is False
    assert "novel_loc=None" in c2["evidence"]
    assert result["computed_failures"] == ["c2"]
    assert result["verdict"] == "review-owed"


def test_uncommitted_work_at_gate_fails_condition_two_instead_of_passing_on_zeroes(
    repo: Path, monkeypatch
):
    """`session_diff_brightline` scopes by `Session-Id` trailer over COMMITS, and the
    checklist commits in step 1 — AFTER this gate. A session still holding its work in
    the tree reports a COMPUTED (never degraded) all-zero diff, so condition 2 could not
    fail: observed 2026-08-21 passing a session with ~516 novel LOC across 4 surfaces.
    The zeroes must not read as satisfied."""
    _stub_facts_all_computed(monkeypatch, repo)
    monkeypatch.setattr(
        qwa,
        "_uncommitted_at_gate",
        lambda root: "2 uncommitted path(s) claimed by this session: a.py, b.py",
    )

    envelope = qwa.brief()

    entry_test = envelope["gates"]["entry_test"]
    c2 = [c for c in entry_test["conditions"] if c["id"] == "c2"][0]
    assert c2["passed"] is False
    assert "novel_loc=None" in c2["evidence"]
    assert "uncommitted-at-gate" in c2["evidence"]
    assert "a.py, b.py" in c2["evidence"]
    assert entry_test["computed_failures"] == ["c2"]
    assert entry_test["verdict"] == "review-owed"
    # The underlying fact travels through close_gate untouched — the substitution is
    # this ceremony's own posture, not a rewrite of what the facade reported.
    assert envelope["gates"]["close_gate"]["diff"]["value"]["under_brightline"] is True


def test_uncommitted_probe_reads_this_sessions_paths_and_ignores_peer_residue(monkeypatch):
    """`mine` is the whole signal: on a shared worktree a peer's uncommitted file says
    nothing about THIS session's diff, and failing c2 on residue would fail it
    permanently for every session on the branch."""
    from coordinator_core import baton_assemble

    monkeypatch.setattr(
        baton_assemble,
        "_compute_dirty_tree_attribution",
        lambda root: {"degraded": False, "mine": [], "residue_count": 7},
    )
    assert qwa._uncommitted_at_gate(Path("/nowhere")) is None

    monkeypatch.setattr(
        baton_assemble,
        "_compute_dirty_tree_attribution",
        lambda root: {
            "degraded": False,
            "mine": ["state/handoffs/x.md", "coordinator_core/y.py"],
            "residue_count": 7,
        },
    )
    evidence = qwa._uncommitted_at_gate(Path("/nowhere"))
    assert evidence is not None
    assert "2 uncommitted path(s)" in evidence
    assert "state/handoffs/x.md" in evidence and "coordinator_core/y.py" in evidence


def test_a_degraded_attribution_probe_fails_condition_two_closed(monkeypatch):
    """"Could not establish that nothing is uncommitted" is not "established that
    nothing is" — the probe failing must not resolve condition 2 as satisfied."""
    from coordinator_core import baton_assemble

    monkeypatch.setattr(
        baton_assemble,
        "_compute_dirty_tree_attribution",
        lambda root: {"degraded": True, "evidence": "no resolvable session id"},
    )

    evidence = qwa._uncommitted_at_gate(Path("/nowhere"))

    assert evidence is not None
    assert "probe degraded" in evidence
    result = qwa._entry_test(
        {"consumed_predecessor": False, "classification": "none"},
        {"present": False},
        qwa._closed_diff_uncommitted(evidence),
        Path("/nowhere"),
    )
    c2 = [c for c in result["conditions"] if c["id"] == "c2"][0]
    assert c2["passed"] is False
    assert c2["breached"] == ["uncommitted-at-gate"]


def test_closed_diff_uncommitted_carries_none_numerics_never_zero():
    """A `0` reads as "genuinely clean" — the fail-open this substitute refuses. Same
    posture as `_closed_diff`, which it reuses rather than restates."""
    closed = qwa._closed_diff_uncommitted("2 uncommitted path(s) claimed by this session: a.py")

    assert closed["under_brightline"] is False
    for field in (
        "novel_loc",
        "gross_loc",
        "doc_only_loc",
        "novel_commit_count",
        "commit_count",
        "surface_count",
        "novel_surface_count",
    ):
        assert closed[field] is None, f"{field} must be None, never a trustworthy 0"
    assert closed["scoping_method"].startswith("uncommitted-at-gate: ")


def test_degraded_terminal_sizings_raises_an_explicit_judgment_point(repo: Path, monkeypatch):
    """Neither `terminal_sizings` nor `fold_sidecars` feeds an entry-test condition, so a
    degraded read has no condition to fail closed — it must surface as an explicit
    judgment point instead of silently reporting an empty scan."""
    _stub_facts_all_computed(monkeypatch, repo)
    monkeypatch.setattr(
        qwa.session_facts,
        "session_terminal_sizings",
        lambda *a, **k: _degraded_record("state/sizings glob raised OSError"),
    )

    envelope = qwa.brief()

    point_ids = [p["id"] for p in envelope["judgment_points"]]
    assert "j-terminal-sizings-degraded" in point_ids
    point = [p for p in envelope["judgment_points"] if p["id"] == "j-terminal-sizings-degraded"][0]
    assert point["evidence"] == "state/sizings glob raised OSError"
    assert point["recommendation"] is None
    # The degraded record itself still travels through close_gate, not silently dropped.
    assert envelope["gates"]["close_gate"]["terminal_sizings"]["degraded"] is True
    assert "value" not in envelope["gates"]["close_gate"]["terminal_sizings"]


def test_degraded_fold_sidecars_raises_an_explicit_judgment_point(repo: Path, monkeypatch):
    _stub_facts_all_computed(monkeypatch, repo)
    monkeypatch.setattr(
        qwa.session_facts,
        "session_fold_sidecars",
        lambda *a, **k: _degraded_record("state/execution-records raised PermissionError"),
    )

    envelope = qwa.brief()

    point_ids = [p["id"] for p in envelope["judgment_points"]]
    assert "j-fold-sidecars-degraded" in point_ids
    point = [p for p in envelope["judgment_points"] if p["id"] == "j-fold-sidecars-degraded"][0]
    assert point["evidence"] == "state/execution-records raised PermissionError"
    assert point["recommendation"] is None
    assert envelope["gates"]["close_gate"]["fold_sidecars"]["degraded"] is True


# ---------------------------------------------------------------------------
# C1(d) — pre-lift characterization pin (fl-core-02)
# ---------------------------------------------------------------------------
#
# These tests are a CHARACTERIZATION PIN, not a specification of desired
# behaviour. They exist per docs/decisions/DR-323 (fl-core-02 C1(d)) to fail
# loudly if `_entry_test`'s four-condition verdicts or `_judgment_points`'
# emission change shape, wording, or routing across the facade cutover.
#
# WHY THIS EXISTS SEPARATELY FROM C6's CONTRACT TESTS: `fl-core-02` lifts the
# five `Fact 1..5` readers onto `coordinator_core.session.session_facts`
# under DR-319's two-shape `{degraded, value/evidence, source, collision}`
# contract. C6's contract tests assert that FACADE record shape and never
# call `_entry_test` or `_judgment_points` — so "C6 is green" is not evidence
# that the live `/quick-wrap` ceremony's verdict survives C7's cutover, only
# that the facade itself is correct. This file's `_entry_test` and
# `_judgment_points` still consume bare bool/str/int fields today
# (`pickup_kind.get("consumed_predecessor")`, `diff.get("under_brightline")`,
# `sizings.get("terminal")`, ...), not DR-319 records — that is the CURRENT,
# pre-lift shape these tests pin.
#
# C7 (the cutover chunk) converts every call site that currently does
# `record["value"] if not record["degraded"] else 0` into a read off a
# DR-319 record — but per C1(a)'s cutover discipline, it MUST preserve these
# functions' externally-observed verdicts, routes, and judgment-point
# emissions rather than re-baselining them. If a future edit needs to change
# what is asserted below, that is itself the signal that the live ceremony's
# behaviour changed — update this pin only alongside an explicit statement
# of what changed and why, never as a drive-by "make it green" edit.


def _entry_test_diff(**over: object) -> dict[str, Any]:
    """The full bare-dict shape `_read_diff` returns today (pre-lift) — every
    key `_entry_test` or its evidence strings read, held constant here so the
    pin is legible without cross-referencing `_read_diff`."""
    base: dict[str, Any] = {
        "scoping_method": "session_id_trailer",
        "trustworthy": True,
        "sha_range": "aaa..bbb",
        "commit_count": 2,
        "surface_count": 1,
        "novel_surface_count": 1,
        "touched_paths": ["coordinator_core/x.py"],
        "gross_loc": 10,
        "doc_only_loc": 0,
        "relocated_files": 0,
        "novel_loc": 10,
        "novel_commit_count": 2,
        "brightline": {"novel_loc": 500, "novel_commit_count": 5, "novel_surface_count": 4},
        "breached": [],
        "under_brightline": True,
    }
    base.update(over)
    return base


def test_pin_entry_test_all_conditions_pass(tmp_path: Path):
    """PIN. A clean session — no plan, nothing claimed, diff under brightline —
    computes conditions 1-3 True, leaves condition 4 un-decided, and verdicts
    `computed-conditions-pass`. Full-record equality: any drift in field
    names, evidence wording, or routing on this exact input must fail here."""
    pickup_kind = {"consumed_predecessor": False, "classification": "none"}
    governing_plan = {"present": False}
    diff = _entry_test_diff()

    result = qwa._entry_test(pickup_kind, governing_plan, diff, tmp_path)

    assert result == {
        "conditions": [
            {
                "id": "c1",
                "condition": "No governing plan drove this session's work",
                "passed": True,
                "deferred_obligation": False,
                "evidence": "no plan claim held by this session",
                "route_on_fail": "/workstream-complete",
            },
            {
                "id": "c2",
                "condition": "Diff under the review brightline",
                "passed": True,
                "evidence": (
                    "novel_loc=10 of 10 gross (0 doc-only carved out), "
                    "novel_commits=2 of 2, novel_surfaces=1 of 1, "
                    "scoping=session_id_trailer"
                ),
                "breached": [],
                "route_on_fail": "scoped review, then wrap (stays in quick-wrap)",
            },
            {
                "id": "c3",
                "condition": "What this session claimed has no ancestors",
                "passed": True,
                "evidence": (
                    "classification='none' — nothing claimed this session, "
                    "no ancestry to read"
                ),
                "route_on_fail": "/workstream-complete — a consumed handoff owes a coverage gate",
            },
            {
                "id": "c4",
                "condition": "Work is finished — nothing in-flight for a successor",
                "passed": None,
                "evidence": "EM judgment — deliberately not computed",
                "route_on_fail": "/handoff, only if context pressure genuinely forces the stop",
            },
        ],
        "computed_failures": [],
        "verdict": "computed-conditions-pass",
        "route": "quick-wrap (pending condition 4)",
    }


def test_pin_entry_test_ordinary_plan_routes_out_with_full_evidence_string(tmp_path: Path):
    """PIN. `present: True` with a non-`spec-dispatch` `scope_mode` fails
    condition 1 and routes to `/workstream-complete` — verdict `routed-out`,
    not `review-owed`. Pins the exact c1 evidence interpolation
    (`plan {path!r} scope_mode={mode}`), the string format `_entry_test`'s
    caller (a live close ceremony) reads verbatim."""
    pickup_kind = {"consumed_predecessor": False, "classification": "none"}
    governing_plan = {"present": True, "scope_mode": "dispatch", "path": "docs/plans/p.md"}
    diff = _entry_test_diff()

    result = qwa._entry_test(pickup_kind, governing_plan, diff, tmp_path)

    c1 = result["conditions"][0]
    assert c1 == {
        "id": "c1",
        "condition": "No governing plan drove this session's work",
        "passed": False,
        "deferred_obligation": False,
        "evidence": "plan 'docs/plans/p.md' scope_mode=dispatch",
        "route_on_fail": "/workstream-complete",
    }
    assert result["computed_failures"] == ["c1"]
    assert result["verdict"] == "routed-out"
    assert result["route"] == "/workstream-complete"


def test_pin_entry_test_chain_root_baton_evidence_string(tmp_path: Path):
    """PIN. c3's evidence for a claimed chain-root artifact names the artifact
    path and states which three frontmatter fields were confirmed absent."""
    artifact = tmp_path / "state" / "handoffs" / "b.md"
    _write(artifact, "---\npredecessor: none\n---\nbody\n")
    pickup_kind = {
        "consumed_predecessor": True,
        "classification": "spinoff",
        "artifact_path": "state/handoffs/b.md",
        "basename": "b.md",
        "actioned_memos": [{"basename": "m.md"}],
    }
    governing_plan = {"present": False}
    diff = _entry_test_diff()

    result = qwa._entry_test(pickup_kind, governing_plan, diff, tmp_path)

    c3 = result["conditions"][2]
    assert c3["evidence"] == (
        "claimed artifact 'state/handoffs/b.md' is a chain root "
        "(predecessor/additional_predecessors/forked_from all absent)"
    )
    assert result["computed_failures"] == []
    assert result["verdict"] == "computed-conditions-pass"


def test_pin_entry_test_ancestor_present_evidence_string(tmp_path: Path):
    """PIN. c3's evidence for a claimed artifact with a real ancestor names which
    field(s) carried the ancestry, and the session routes out."""
    artifact = tmp_path / "state" / "handoffs" / "b.md"
    _write(artifact, "---\npredecessor: state/handoffs/a.md\n---\nbody\n")
    pickup_kind = {
        "consumed_predecessor": True,
        "classification": "handoff",
        "artifact_path": "state/handoffs/b.md",
        "basename": "b.md",
    }
    governing_plan = {"present": False}
    diff = _entry_test_diff()

    result = qwa._entry_test(pickup_kind, governing_plan, diff, tmp_path)

    c3 = result["conditions"][2]
    assert c3["evidence"] == (
        "claimed artifact 'state/handoffs/b.md' carries ancestry: "
        "predecessor='state/handoffs/a.md'"
    )
    assert result["computed_failures"] == ["c3"]
    assert result["verdict"] == "routed-out"


def test_pin_entry_test_brightline_breach_alone_is_review_owed_not_routed_out(tmp_path: Path):
    """PIN. Condition 2 failing alone stays inside quick-wrap
    (`review-owed`/`scoped review, then wrap`) — the one computed-failure
    combination that does NOT verdict `routed-out`. Any other single or
    combined computed failure does."""
    pickup_kind = {"consumed_predecessor": False, "classification": "none"}
    governing_plan = {"present": False}
    diff = _entry_test_diff(
        novel_loc=900, under_brightline=False, breached=["novel_loc"]
    )

    result = qwa._entry_test(pickup_kind, governing_plan, diff, tmp_path)

    assert result["computed_failures"] == ["c2"]
    assert result["verdict"] == "review-owed"
    assert result["route"] == "scoped review, then wrap"


def test_pin_entry_test_spec_dispatch_deferred_obligation(tmp_path: Path):
    """PIN. `scope_mode == 'spec-dispatch'` passes c1 as a DEFERRED
    obligation (`deferred_obligation: True`), not a clean pass — the two must
    stay distinguishable on the returned record."""
    pickup_kind = {"consumed_predecessor": False, "classification": "none"}
    governing_plan = {"present": True, "scope_mode": "spec-dispatch", "path": "docs/plans/p.md"}
    diff = _entry_test_diff()

    result = qwa._entry_test(pickup_kind, governing_plan, diff, tmp_path)

    c1 = result["conditions"][0]
    assert c1["passed"] is True
    assert c1["deferred_obligation"] is True
    assert result["verdict"] == "computed-conditions-pass"


def test_pin_judgment_points_baseline_emits_only_work_finished():
    """PIN. With no dirty terminal sizing and a trustworthy diff, exactly one
    judgment point is emitted, and its scalar fields (question/evidence/
    reason/recommendation) are pinned verbatim — these are the exact prose
    strings the live ceremony's EM reads at close."""
    points = qwa._judgment_points({"terminal": []}, {"trustworthy": True, "scoping_method": "x"})

    assert len(points) == 1
    point = points[0]
    assert point["id"] == "j-work-finished"
    assert point["question"] == (
        "Is the work finished, with nothing in-flight for a successor?"
    )
    assert point["evidence"] == (
        "Entry-test condition 4. No disk fact answers it: whether a successor "
        "is owed depends on intent this assembler cannot read."
    )
    assert point["reason"] == (
        "Genuine EM discretion — the carve-out the computed-fact doctrine "
        "preserves. An assembler that guesses this has crossed from detecting "
        "into deciding."
    )
    assert point["recommendation"] is None


def test_pin_judgment_points_dirty_terminal_sizing_evidence_string():
    """PIN. `j-dirty-terminal-sizing`'s evidence is a `"; "`-joined
    `path (status)` list over only the DIRTY records — pins both the join
    format and the dirty-only filter."""
    sizings = {
        "terminal": [
            {"path": "state/sizings/a.yaml", "status": "shipped", "dirty": True},
            {"path": "state/sizings/b.yaml", "status": "declined", "dirty": False},
            {"path": "state/sizings/c.yaml", "status": "superseded", "dirty": True},
        ]
    }
    diff = {"trustworthy": True, "scoping_method": "session_id_trailer"}

    points = qwa._judgment_points(sizings, diff)

    point = [p for p in points if p["id"] == "j-dirty-terminal-sizing"][0]
    assert point["evidence"] == "state/sizings/a.yaml (shipped); state/sizings/c.yaml (superseded)"
    assert point["question"] == (
        "A terminal sizing-object carries uncommitted edits — archive it or leave it?"
    )
    assert point["recommendation"] is None


def test_pin_judgment_points_ambiguous_scoping_evidence_string():
    """PIN. `j-scoping-ambiguous`'s evidence is `scoping_method=<value>`
    verbatim, emitted only when `diff["trustworthy"]` is falsy."""
    diff = {"trustworthy": False, "scoping_method": qwa.SCOPING_METHOD_AMBIGUOUS}

    points = qwa._judgment_points({"terminal": []}, diff)

    point = [p for p in points if p["id"] == "j-scoping-ambiguous"][0]
    assert point["evidence"] == f"scoping_method={qwa.SCOPING_METHOD_AMBIGUOUS}"
    assert point["question"] == (
        "Session-Id trailer scoping is ambiguous — are the diff counts this "
        "session's own?"
    )
    assert point["recommendation"] is None


# ---------------------------------------------------------------------------
# Slice-C review finding (WARN) — the `d2` directive's gating on a DEGRADED
# Fact 5 was unasserted in either direction. Pinned here, both ways.
# ---------------------------------------------------------------------------


def _d2_ids(directives: list[dict]) -> list[str]:
    return [d["id"] for d in directives]


def test_d2_is_emitted_when_the_fold_scan_ran_and_found_sidecars():
    """The ordinary case: a clean scan with sidecars present emits the disposal
    directive, so the EM's reconcile is mechanical rather than remembered."""
    fold = {"present": True, "paths": ["state/execution-records/a.md"], "count": 1}
    directives = qwa._directives(fold, fold_degraded=False)
    assert "d2" in _d2_ids(directives)


def test_d2_is_withheld_when_the_fold_scan_ran_and_found_nothing():
    """A genuinely empty scan withholds `d2` — there is nothing to dispose."""
    fold = {"present": False, "paths": [], "count": 0}
    directives = qwa._directives(fold, fold_degraded=False)
    assert "d2" not in _d2_ids(directives)


def test_d2_is_withheld_on_a_degraded_scan_but_not_because_present_is_false():
    """The finding this pins.

    Fact 5's degraded fallback carries the SAME literal `present: False` as a
    genuinely-empty scan, so `_directives` gating on `present` alone read "could
    not run" as "ran and found nothing" — the absent-vs-clean conflation the lift
    exists to retire, surviving at the one call site the DR-319 record never
    reached.

    `d2` is still withheld on a degraded read (a path-less
    `coordinator-fold-execution-record` invocation is not runnable), so the
    OBSERVABLE directive list matches the empty-scan case. What must not regress is
    that the two are decided by different inputs: pass a fold payload that WOULD
    emit `d2` on a clean read, and assert the degraded flag alone suppresses it.
    A future edit that drops the flag and trusts `present` passes the two tests
    above and fails this one.
    """
    fold_that_would_emit = {"present": True, "paths": ["state/execution-records/a.md"], "count": 1}
    directives = qwa._directives(fold_that_would_emit, fold_degraded=True)
    assert "d2" not in _d2_ids(directives), (
        "a degraded fold scan must not emit d2 off a payload it could not have computed"
    )


def test_d3_carries_the_invoker_the_cli_declares_required():
    """The emitted directive must be RUNNABLE, not merely well-shaped.

    `regenerate-orientation-cache` declares `--invoker` as `required=True` with a
    closed `choices` set (`coordinator/bin/regenerate-orientation-cache.py`'s
    parser; `orientation.regenerate_cache._tier_for_invoker` is the vocabulary).
    `d3` emitted `args: []`, so every quick-wrap run handed the EM a directive that
    exits on argparse.

    Why nothing caught it for so long: the module was absent from the klabauter
    engine row's field 7, so the published mirror never carried it and the
    end-of-run argv-parity gate had nothing to grade. It went red the first round
    after the allowlist entry landed. The sibling emitter in
    `workstream_complete/directives_session_hygiene.py` passes its invoker; this
    one did not, and no test compared the two.

    Asserting the VALUE, not just the flag's presence: an invoker outside
    `_tier_for_invoker`'s allowlist parses and then raises ValueError, which is the
    same broken directive one layer further in.
    """
    fold = {"present": False, "paths": [], "count": 0}
    directives = qwa._directives(fold, fold_degraded=False)

    d3 = next(d for d in directives if d["id"] == "d3")
    assert d3["cli"] == "regenerate-orientation-cache"
    assert d3["args"] == ["--invoker", "quick-wrap"], (
        "d3 must emit the required --invoker flag with a value the CLI accepts"
    )


def test_terminal_sweep_directives_are_last_handoffs_then_sizings():
    """Ordering is load-bearing (commit message for the C3 chunk that added
    `d5`): every stamp this ceremony performs must land before either sweep
    classifies, which only holds if both sweeps are LAST, and the sizings
    sweep is emitted immediately AFTER the handoffs sweep (never before it,
    never interleaved with anything else). Nothing pinned this before —
    the generic cross-consistency guard
    (`coordinator_core/authz/tests/test_assembler_dispatchable.py`) only
    catches a CLI missing from `ASSEMBLER_DISPATCHABLE`/`CONSUMES_MANIFEST`,
    never emission order. This gap predates the sizings sweep: `d4` (the
    handoffs sweep) was never order-pinned either, so this test closes both
    at once.
    """
    fold = {"present": False, "paths": [], "count": 0}
    directives = qwa._directives(fold, fold_degraded=False)

    ids = _d2_ids(directives)
    assert ids[-2:] == ["d4", "d5"], (
        f"expected the handoffs sweep (d4) immediately followed by the sizings "
        f"sweep (d5) as the final two directives; got {ids!r}"
    )

    d4 = next(d for d in directives if d["id"] == "d4")
    d5 = next(d for d in directives if d["id"] == "d5")
    assert d4["cli"] == "sweep-terminal-handoffs"
    assert d5["cli"] == "sweep-terminal-sizings"


def test_a_degraded_fold_scan_still_surfaces_its_own_judgment_point(repo: Path, monkeypatch):
    """`d2`'s absence is not the EM's only signal — the degradation must remain
    visible as a judgment point, since that is what makes the withheld directive a
    stated decision rather than a silent omission.

    Complements `test_degraded_fold_sidecars_raises_an_explicit_judgment_point`,
    which asserts the judgment point and `close_gate` but never the directive list.
    """
    _stub_facts_all_computed(monkeypatch, repo)
    monkeypatch.setattr(
        qwa.session_facts,
        "session_fold_sidecars",
        lambda *a, **k: _degraded_record("state/execution-records raised PermissionError"),
    )

    envelope = qwa.brief()

    assert "j-fold-sidecars-degraded" in [p["id"] for p in envelope["judgment_points"]]
    assert "d2" not in _d2_ids(envelope["directives"])


# ---------------------------------------------------------------------------
# terminal_write_owed -- cross-repo/archive/2026-08-21-example-retrieval-repo-em-quick-wrap-
# landed-plan-stalls-silently.md, the sender's own option (3) and first
# preference. Baton item 4, state/handoffs/2026-08-30-workstream-complete-and-
# close-out-cannot.md.
# ---------------------------------------------------------------------------


def test_landed_plan_is_reported_as_owing_a_terminal_write():
    """The exact state that stalled the sender: plan at `landed`, read out of the
    brief, and nothing in the payload saying `landed` is a state the reader is
    expected to ACT on. They concluded the deliverable cascade owned the flip.
    It does not.
    """
    assert qwa._terminal_write_owed({"present": True, "status": "landed"}) is True


def test_a_terminal_status_owes_nothing():
    assert qwa._terminal_write_owed({"present": True, "status": "implemented"}) is False


def test_pre_terminal_but_unlanded_statuses_owe_nothing():
    """`executing` is non-terminal too, and deliberately NOT reported: a plan
    there is owed WORK, not a write. Reporting it would point an operator at
    `stamp-plan-implemented` for a plan whose chunks have not landed -- a wrong
    remedy, which is worse than silence. Same for the pre-execution states and
    for the terminal-by-disposition ones.
    """
    for status in (
        "draft", "reviewed", "approved", "executing",
        "deferred", "abandoned", "superseded",
    ):
        assert qwa._terminal_write_owed({"present": True, "status": status}) is False, status


def test_a_degraded_governing_plan_read_never_claims_a_write_is_owed():
    """`_closed_governing_plan()` substitutes `status: None` when the fact could
    not be resolved. A `None` status is not evidence of an owed write, and
    claiming one would point an operator at a stamp for a plan this call could
    not even read.
    """
    assert qwa._terminal_write_owed(qwa._closed_governing_plan()) is False
    assert qwa._terminal_write_owed({"present": True, "status": None}) is False
    assert qwa._terminal_write_owed({"present": False, "status": "landed"}) is False


def test_status_comparison_is_case_and_whitespace_insensitive():
    assert qwa._terminal_write_owed({"present": True, "status": "  LANDED "}) is True


def test_every_status_in_the_schema_enum_is_classified():
    """Guard: the constant is derived from `plan.schema.json`'s own `status`
    enum, so a new lifecycle state added there must be considered here rather
    than silently defaulting to "owes nothing". Fails loud on an enum this
    module has never seen.
    """
    import json

    schema = json.loads(
        (
            Path(__file__).resolve().parent
            / "frontmatter" / "schemas" / "plan.schema.json"
        ).read_text(encoding="utf-8")
    )
    known = {
        "draft", "reviewed", "approved", "executing", "landed",
        "implemented", "deferred", "abandoned", "superseded",
    }
    enum = set(schema["properties"]["status"]["enum"])
    assert enum == known, (
        "plan.schema.json's status enum changed; classify the new value(s) in "
        f"_TERMINAL_WRITE_OWED_STATUS before widening this guard: {enum ^ known}"
    )
    assert qwa._TERMINAL_WRITE_OWED_STATUS in enum
