"""
coordinator_core.workstream_complete.tests.test_review_receipt_gates_delivered_close
— red-first suite for docs/plans/2026-08-27-the-review-gate-measures-the-whole-session.md,
chunk C1 (AC6/AC7/AC13).

Purpose: prove, against the REAL `workstream_complete.brief()` path (never a mock of
the gate under test), that a plan can reach `d-stamp-plan-implemented` today with zero
review logged and nothing observing it — and pin the interface C2/C3/C4 build against,
so the fix chunks have a fixed target rather than a fresh design choice each.

Pinned interface (not yet implemented — every test below is RED at authoring time,
AC13; each case's captured failure lives in this plan's C1 sidecar, not inline here,
per the run-report contract):

  - `workstream_complete.ReviewReceiptGate` — a `NamedTuple(applies, blocks, detail)`,
    the same shape family as `OpenSpineRowGate`/`LandedReconciliationGate`.
  - `workstream_complete._compute_review_receipt_gate(root, sid, target_status)` — the
    SOLE call site (AC7 leg ii) that decides whether `target_status` may be reached
    without a receipt. Reads `state/subagent-share/<sid>/*.md` sidecars; a sidecar
    counts as a receipt iff its frontmatter `agent_type` (the `coordinator:` prefix
    stripped, if present) names a member of
    `coordinator_core.ops.review_trail_write._DELEGATE_REVIEWERS`, AND its body (the
    text after the frontmatter fence) is non-blank (AC5 — a blank sidecar is an
    aborted review, not a pass). No boolean/optional parameter may exist on this
    function that skips the check (AC7 leg ii).
  - `workstream_complete._status_requires_review_receipt(status)` — constraint 2's
    total mapping: `False` for `superseded`/`abandoned` only; `True` for every other
    `_LEG_A_TERMINAL_PLAN_STATUS` member AND any unrecognised value (fail toward more
    review, constraint 4).
  - `brief()`'s envelope grows `gates["review_receipt"]` (mirroring
    `gates["open_spine_row_worklist"]`/`gates["landed_reconciliation"]`'s own shape:
    `{"applies": bool, "blocks": bool, "detail": str}`), and — when it blocks — a new
    judgment point `jp-review-receipt-block-stamp` gating `d-stamp-plan-implemented`
    via `depends_on`, mirroring `jp-open-spine-rows-block-stamp`'s wiring pattern
    exactly (see `test_workstream_complete.py::test_open_spine_row_gate_blocks_the_
    implemented_stamp` for the sibling this pattern is copied from).

Negative-spec: this suite does NOT exercise AC2b's claim-window join (baton
`claimed_at` vs. sidecar timestamp) — that join is C4's own scope (AC2b is discharged
by C4, not listed under C1's AC6). A session-id match plus a non-blank, correctly-typed
sidecar is sufficient for this suite's fixtures.

Run scoped only:
    python -m pytest coordinator_core/workstream_complete/tests/test_review_receipt_gates_delivered_close.py -q
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.workstream_complete as wsc
from coordinator_core.subagent_sandbox import provision_report
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "testsid123"


def _gate(disposition: str = "single-session", **overrides) -> wsc.SessionShapeGate:
    """Minimal `SessionShapeGate` fixture, same shape/defaults as
    `test_workstream_complete.py::_gate` — duplicated locally rather than imported,
    since that module is a test file (no public re-export) and this suite's own
    `writes:` scope does not include it."""
    fields = dict(
        sid=_SID,
        disposition=disposition,
        consumed_handoff="",
        diagnostics=[],
        consumed_handoff_paths=(),
        detection={},
    )
    fields.update(overrides)
    return wsc.SessionShapeGate(**fields)


def _patch_gate(monkeypatch: pytest.MonkeyPatch, gate: wsc.SessionShapeGate) -> None:
    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root: gate)


def _write_clean_plan(tmp_path: Path, slug: str, status: str = "draft") -> None:
    plan_path = tmp_path / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ntitle: \"a plan\"\nstatus: {status}\n---\n\n"
        "# a plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Shipped row\n  disposition: coded\n  disposition_ref: abc123\n"
        "\n```\n",
        encoding="utf-8",
    )


def _write_sidecar(
    tmp_path: Path,
    sid: str,
    agent_type: str = "coordinator:code-reviewer",
    body: str = "## Findings\n\nReal review content here.\n",
    stamped_at: str = "2026-08-27T13:00:00+00:00",
    with_receipt: bool = True,
    receipt_key: str = "review_receipt",
) -> Path:
    """Write a reviewer sidecar as the dispatch seam actually writes one.

    C11: the receipt block is produced by ``provision_report``'s OWN splice
    functions, not hand-rolled here. That coupling is the point — the gate's
    contract is with the bytes the dispatch seam emits, so a change to
    ``_splice_review_receipt``'s shape must break these tests rather than
    silently diverge from them. Hand-writing the block would let the writer and
    the reader drift apart, which is the class of defect C11 exists to close.

    ``with_receipt=False`` writes the sidecar WITHOUT any receipt block, leaving
    only the generic provisioning frontmatter every provisioned agent gets. That
    is the fixture proving the gate does not accept the header as a substitute.
    """
    from coordinator_core.subagent_sandbox import provision_report

    sidecar_dir = tmp_path / "state" / "subagent-share" / sid
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = sidecar_dir / f"{agent_type.replace(':', '')}.deadbeef01.md"
    doc = (
        f"---\nstatus: complete\nagent_type: {agent_type}\nlead_session_id: {sid}\n"
        "commits: []\n---\n\n" + body
    )
    if with_receipt:
        splice = (
            provision_report._splice_review_receipt
            if receipt_key == "review_receipt"
            else provision_report._splice_integrator_receipt
        )
        doc = splice(doc, sid, "deadbeef01", agent_type, stamped_at)
    sidecar_path.write_text(doc, encoding="utf-8")
    return sidecar_path


def _stamp_directive(decision_object) -> dict:
    return next(
        d for d in decision_object["directives"] if d["id"] == "d-stamp-plan-implemented"
    )


def _depends_on(directive: dict) -> list:
    depends_on = directive.get("depends_on")
    if depends_on is None:
        return []
    return [depends_on] if isinstance(depends_on, str) else list(depends_on)


# ---------------------------------------------------------------------------
# (a) no receipt, terminal status requiring one -> must REFUSE
#     today: closes clean -- gates["review_receipt"] does not exist at all.
# ---------------------------------------------------------------------------


def test_no_receipt_blocks_the_implemented_stamp(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate())
    _write_clean_plan(tmp_path, "no-receipt-plan")
    # Deliberately no `state/subagent-share/<sid>/` sidecar at all.

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "no-receipt-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["applies"] is True
    assert review_receipt_gate["blocks"] is True

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" in jp_ids
    assert "jp-review-receipt-block-stamp" in _depends_on(_stamp_directive(decision_object))


# ---------------------------------------------------------------------------
# (b) receipt linking a FILLED sidecar -> must SUCCEED
# ---------------------------------------------------------------------------


def test_filled_reviewer_sidecar_receipt_unblocks_the_implemented_stamp(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate())
    _write_clean_plan(tmp_path, "filled-receipt-plan")
    _write_sidecar(tmp_path, _SID, body="## Findings\n\nReal review content here.\n")

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "filled-receipt-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is False

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" not in jp_ids
    assert "jp-review-receipt-block-stamp" not in _depends_on(_stamp_directive(decision_object))


# ---------------------------------------------------------------------------
# (c) receipt linking a BLANK sidecar -> must REFUSE (AC5: abort is not a pass)
# ---------------------------------------------------------------------------


def test_blank_reviewer_sidecar_receipt_still_blocks_the_implemented_stamp(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate())
    _write_clean_plan(tmp_path, "blank-receipt-plan")
    _write_sidecar(tmp_path, _SID, body="")  # aborted review: frontmatter only, no body

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "blank-receipt-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is True

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" in jp_ids
    assert "jp-review-receipt-block-stamp" in _depends_on(_stamp_directive(decision_object))


# ---------------------------------------------------------------------------
# (d) AC4/AC7 leg i -- enum-exhaustive: every _LEG_A_TERMINAL_PLAN_STATUS member,
#     plus one unrecognised sentinel, no receipt.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    sorted(wsc._LEG_A_TERMINAL_PLAN_STATUS) + ["totally-unrecognised-sentinel-status"],
)
def test_status_requires_review_receipt_is_total_and_names_only_the_escape_hatch(status):
    requires_receipt = wsc._status_requires_review_receipt(status)
    if status in ("superseded", "abandoned"):
        assert requires_receipt is False, f"{status!r} is constraint 2's escape hatch"
    else:
        assert requires_receipt is True, (
            f"{status!r} is not superseded/abandoned and must require a receipt "
            "(constraint 2's mapping is total, not partial; an unrecognised status "
            "resolves toward requiring review per constraint 4)"
        )


# ---------------------------------------------------------------------------
# AC7 leg ii -- call-site cardinality: the receipt check has exactly one call
# site and no boolean/optional bypass parameter, via inspect.signature (not a
# grep for "override").
# ---------------------------------------------------------------------------


def test_receipt_check_has_exactly_one_call_site_and_no_bypass_parameter():
    gate_fn = wsc._compute_review_receipt_gate

    signature = inspect.signature(gate_fn)
    for parameter in signature.parameters.values():
        is_boolish_default = isinstance(parameter.default, bool)
        assert not is_boolish_default, (
            f"{gate_fn.__name__} carries a boolean-default parameter "
            f"{parameter.name!r} -- a bypassable receipt check is not a floor "
            "(constraint 2)"
        )
        assert parameter.default is not None or parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ) and parameter.default is inspect.Parameter.empty, (
            f"{gate_fn.__name__} carries an Optional-defaulted parameter "
            f"{parameter.name!r} -- no skip path may exist for the receipt check"
        )

    source = inspect.getsource(wsc)
    call_marker = f"{gate_fn.__name__}("
    def_marker = f"def {gate_fn.__name__}("
    total_occurrences = source.count(call_marker)
    def_occurrences = source.count(def_marker)
    call_site_count = total_occurrences - def_occurrences
    assert call_site_count == 1, (
        f"{gate_fn.__name__} must be wired at exactly one call site inside "
        f"workstream_complete/__init__.py (found {call_site_count}) -- a second "
        "call site is exactly the kind of override key constraint 2 forbids"
    )


# ---------------------------------------------------------------------------
# C3 / AC2 -- review-integrator finishing stamps its OWN receipt at the same
# provision_report._provision seam, distinguishable from the reviewer's
# (AC1/C2) receipt so "review ran" and "findings were applied" stay
# separately legible on the same sidecar shape family.
# ---------------------------------------------------------------------------

_INTEGRATOR_TYPE = "coordinator:review-integrator"
_REVIEWER_TYPE = "coordinator:code-reviewer"


@pytest.fixture
def integrator_git_repo(tmp_path: Path) -> Path:
    """Real, empty git repo -- mirrors test_provision_report.py's own
    ``git_repo`` fixture. ``resolve_git_root`` spawns ``git rev-parse
    --show-toplevel`` (not a bare ``.git``-entry walk — that cheaper walk is
    ``resolve_git_root_cheap``, a different, miss-mode-only resolver this
    seam does not use), so a synthetic ``.git`` marker directory resolves to
    ``None`` here and every ``_provision`` call in this section would
    silently no-op. ``no_console_creationflags()`` suppresses the console
    popup a leaf `git.exe` spawn otherwise triggers headless on Windows."""
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_creationflags()
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, **no_console_creationflags()
    )
    return tmp_path


@pytest.fixture
def integrator_policy_path(tmp_path: Path) -> Path:
    policy = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [_INTEGRATOR_TYPE, _REVIEWER_TYPE],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def test_is_review_integrator_matches_only_the_integrator_persona():
    assert provision_report._is_review_integrator("coordinator:review-integrator", "") is True
    assert provision_report._is_review_integrator("", "coordinator:review-integrator") is True
    assert provision_report._is_review_integrator("coordinator:code-reviewer", "") is False
    assert provision_report._is_review_integrator("", "") is False


def test_integrator_receipt_key_is_distinguishable_from_the_reviewer_receipt_key():
    """AC2's own wording -- the two receipts must be separately legible, not
    merely both present under one shared key."""
    review_doc = provision_report._splice_review_receipt(
        "---\n\n## Findings\n\n", "sid1", "agent1", "coordinator:code-reviewer", "2026-08-27T00:00:00Z"
    )
    integrator_doc = provision_report._splice_integrator_receipt(
        "---\n\n## Findings\n\n", "sid1", "agent1", "coordinator:review-integrator", "2026-08-27T00:00:00Z"
    )
    assert "review_receipt:" in review_doc
    assert "integrator_receipt:" not in review_doc
    assert "integrator_receipt:" in integrator_doc
    assert "review_receipt:" not in integrator_doc


def test_provision_stamps_integrator_receipt_on_review_integrator_dispatch(
    integrator_git_repo, integrator_policy_path
):
    payload = {
        "session_id": "leadsid123",
        "agent_type": _INTEGRATOR_TYPE,
        "provision_key": "review-integrator.deadbeef",
    }
    sidecar_path = provision_report._provision(payload, str(integrator_policy_path), str(integrator_git_repo))

    assert sidecar_path is not None
    text = (integrator_git_repo / sidecar_path).read_text(encoding="utf-8")
    assert "integrator_receipt:" in text
    assert "review_receipt:" not in text
    assert "session_id: leadsid123" in text
    assert f"agent_type: {_INTEGRATOR_TYPE}" in text


def test_provision_stamps_review_receipt_not_integrator_receipt_on_reviewer_dispatch(
    integrator_git_repo, integrator_policy_path
):
    payload = {
        "session_id": "leadsid456",
        "agent_type": _REVIEWER_TYPE,
        "provision_key": "code-reviewer.deadbeef",
    }
    sidecar_path = provision_report._provision(payload, str(integrator_policy_path), str(integrator_git_repo))

    assert sidecar_path is not None
    text = (integrator_git_repo / sidecar_path).read_text(encoding="utf-8")
    assert "review_receipt:" in text
    assert "integrator_receipt:" not in text


# ---------------------------------------------------------------------------
# C4 / AC2b — the claim-window join: a receipt's `spawned_at` must not fall
# strictly before the covering baton's own `claimed_at` (read off the
# consumed handoff's frontmatter, the same text `_read_consumed_handoff_
# text` already reads elsewhere on this path -- no new git spawn, no
# `baton_assemble` hop).
# ---------------------------------------------------------------------------


def _write_consumed_handoff(tmp_path: Path, claimed_at: str) -> str:
    handoff_dir = tmp_path / "state" / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / "2026-08-27-covering-baton.md"
    handoff_path.write_text(
        f"---\nkind: session-handoff\nclaimed_at: {claimed_at}\n---\n\nbody\n",
        encoding="utf-8",
    )
    return "state/handoffs/2026-08-27-covering-baton.md"


def test_receipt_stamped_before_the_claim_window_start_does_not_count(monkeypatch, tmp_path):
    consumed_handoff = _write_consumed_handoff(tmp_path, claimed_at="2026-08-27T12:00:00+00:00")
    _patch_gate(monkeypatch, _gate(consumed_handoff=consumed_handoff))
    _write_clean_plan(tmp_path, "stale-receipt-plan")
    # C11: the receipt block's own `stamped_at` predates this baton's claim --
    # a stale receipt from an EARLIER baton, exactly the hazard AC2b's claim
    # window exists to exclude. Set on the receipt the dispatch seam writes,
    # not on the sidecar header, because the header is not what the gate reads.
    _write_sidecar(tmp_path, _SID, stamped_at="2026-08-27T10:00:00+00:00")

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "stale-receipt-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is True

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" in jp_ids


def test_receipt_stamped_inside_the_claim_window_counts(monkeypatch, tmp_path):
    consumed_handoff = _write_consumed_handoff(tmp_path, claimed_at="2026-08-27T12:00:00+00:00")
    _patch_gate(monkeypatch, _gate(consumed_handoff=consumed_handoff))
    _write_clean_plan(tmp_path, "fresh-receipt-plan")
    _write_sidecar(tmp_path, _SID, stamped_at="2026-08-27T13:00:00+00:00")

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "fresh-receipt-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is False

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" not in jp_ids


def test_generic_provisioning_frontmatter_without_a_receipt_block_does_not_count(
    monkeypatch, tmp_path
):
    """C11 — THE TEST THAT GOES RED IF C2's SPLICE IS REVERTED (AC13).

    The gate's first implementation keyed on the sidecar's top-level
    `agent_type` / `lead_session_id` / `spawned_at`. Those are written for
    EVERY provisioned agent by `_provision`, so the gate passed identically
    with the entire receipt mechanism deleted: AC1 and AC2's stamp was
    decorative and nothing observed its absence.

    This fixture is precisely that state — a reviewer-typed sidecar with a
    non-blank body and correct session id, provisioned exactly as any agent
    is, but carrying NO `review_receipt:` block because no dispatch seam
    stamped one. It MUST block. If someone reverts `_splice_review_receipt`,
    or "simplifies" the gate back to reading the header, this test is what
    fails.
    """
    consumed_handoff = _write_consumed_handoff(tmp_path, claimed_at="2026-08-27T12:00:00+00:00")
    _patch_gate(monkeypatch, _gate(consumed_handoff=consumed_handoff))
    _write_clean_plan(tmp_path, "header-only-plan")
    sidecar = _write_sidecar(tmp_path, _SID, with_receipt=False)

    text = sidecar.read_text(encoding="utf-8")
    # Guard the fixture itself: it must genuinely look eligible on every
    # header field, or this test would pass for the wrong reason.
    assert "review_receipt:" not in text
    assert "agent_type: coordinator:code-reviewer" in text
    assert f"lead_session_id: {_SID}" in text
    assert "Real review content here." in text

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "header-only-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is True

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" in jp_ids


def test_prime_exit_criterion_falsifier_end_to_end(monkeypatch, tmp_path, integrator_git_repo):
    """PROMOTED FALSIFIER for this plan's prime exit criterion (Phase 4 step 3.5).

    Runs `falsifier.how` in full: a baton whose work has had NO review dispatched
    attempts the `d-stamp-plan-implemented` stamp; then a reviewer AND an
    integrator are dispatched over the same work and it attempts again.

    What this covers that no sibling test does: the receipts here are written by
    the REAL `provision_report._provision` — an actual simulated dispatch —
    rather than by `_write_sidecar`'s call to the splice helper. That is what
    discharges `expected_when_true`'s "a receipt ... that no one hand-wrote".

    Measured, not assumed: neuter provisioning's reviewer eligibility (so
    `_provision` stops stamping while the splice helpers stay intact) and FOUR
    tests in this file go red — the three `test_provision_stamps_*` /
    `test_is_review_integrator_*` siblings, and this one. The siblings catch it
    at the WRITER (they assert on the sidecar's bytes); this is the only one
    that catches it at the GATE, i.e. the only one that proves the close
    actually refuses when no receipt was stamped. That end-to-end reach is its
    reason to exist, not exclusivity.

    Arrived red-demonstrated at plan altitude: `baseline_output` at `da0cd0e74`
    records the stamp being reachable with zero review logged, unconditionally.
    """
    from coordinator_core.subagent_sandbox.provision_report import _provision

    consumed_handoff = _write_consumed_handoff(tmp_path, claimed_at="2026-08-27T12:00:00+00:00")
    _patch_gate(monkeypatch, _gate(consumed_handoff=consumed_handoff))
    _write_clean_plan(tmp_path, "falsifier-plan")

    policy = tmp_path / "subagent-sandbox-policy.yaml"
    policy.write_text(
        f"report_sidecar:\n  - coordinator:code-reviewer\n  - {_INTEGRATOR_TYPE}\n",
        encoding="utf-8",
    )

    def _brief():
        return wsc.brief(
            decisions={"governing_plan_slug": "falsifier-plan", "subject": "x"},
            repo_root=tmp_path,
        )

    # --- attempt 1: no review dispatched -> must REFUSE, naming what is missing
    first = _brief()
    assert first["gates"]["review_receipt"]["applies"] is True
    assert first["gates"]["review_receipt"]["blocks"] is True
    assert "dispatch a reviewer" in first["gates"]["review_receipt"]["detail"]
    assert "jp-review-receipt-block-stamp" in {jp["id"] for jp in first["judgment_points"]}
    stamp = next(
        (d for d in first["directives"] if d["id"] == "d-stamp-plan-implemented"), None
    )
    assert stamp is not None
    assert "jp-review-receipt-block-stamp" in (stamp.get("depends_on") or [])

    # --- dispatch a reviewer and an integrator, mechanically
    for agent_type, agent_id, findings in (
        ("coordinator:code-reviewer", "aaaa111122223333", "\n## Findings\n\nA real finding.\n"),
        (_INTEGRATOR_TYPE, "bbbb444455556666", "\n## Applied\n\nThe finding was applied.\n"),
    ):
        rel = _provision(
            {"agent_id": agent_id, "agent_type": agent_type, "session_id": _SID},
            str(policy),
            str(tmp_path),
        )
        assert rel, f"_provision declined to provision {agent_type}"
        sidecar = tmp_path / rel
        # The receipt must already be there, stamped at DISPATCH, before the
        # agent has written a single word of findings -- that is what makes
        # blank-vs-filled meaningful rather than a proxy for "did it finish".
        assert "receipt:" in sidecar.read_text(encoding="utf-8")
        sidecar.write_text(sidecar.read_text(encoding="utf-8") + findings, encoding="utf-8")

    # --- attempt 2: must SUCCEED, and both receipts must be separately legible
    second = _brief()
    gate_2 = second["gates"]["review_receipt"]
    assert gate_2["blocks"] is False
    assert "review receipt found" in gate_2["detail"]
    assert "integrator receipt" in gate_2["detail"]
    assert "jp-review-receipt-block-stamp" not in {jp["id"] for jp in second["judgment_points"]}


def test_stale_same_session_receipt_with_no_consumed_handoff_still_counts(monkeypatch, tmp_path):
    """F6 (docs/plans/2026-08-27-the-review-gate-measures-the-whole-session.md):
    pins the accepted-but-unglamorous behavior `_resolve_baton_claim_window_start`'s
    corrected docstring now names explicitly. No consumed handoff -> the claim
    window is unbounded below, so a receipt stamped EARLIER in the SAME session
    still counts -- no `claimed_at` bound could ever exclude it, since a
    same-session receipt matches `sid` by construction. Session-id + non-blank
    body (AC5) is the only floor in this case, and this test documents that as a
    product decision rather than an unnoticed hole."""
    _patch_gate(monkeypatch, _gate(consumed_handoff=""))
    _write_clean_plan(tmp_path, "stale-same-session-plan")
    # A receipt stamped well before "now", from no consumed handoff (single
    # session, no baton to bound against) -- this must still unblock the close.
    _write_sidecar(tmp_path, _SID, stamped_at="2020-01-01T00:00:00+00:00")

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "stale-same-session-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is False


def test_integrator_receipt_is_reported_but_never_required(monkeypatch, tmp_path):
    """AC2 at the READING end: "findings were applied" stays separately
    legible without becoming a second gate.

    Two halves, because either alone would let a defect through:
      (a) a reviewer receipt with no integrator receipt UNBLOCKS, and the
          detail says the integrator receipt is absent -- requiring one would
          block every close whose review found nothing to apply, which the
          plan never asked for;
      (b) an integrator receipt ALONE does not unblock -- it is not a
          substitute for review having happened (constraint 4: ambiguity
          resolves toward requiring review).
    """
    consumed_handoff = _write_consumed_handoff(tmp_path, claimed_at="2026-08-27T12:00:00+00:00")
    _patch_gate(monkeypatch, _gate(consumed_handoff=consumed_handoff))

    # (a) reviewer receipt only
    _write_clean_plan(tmp_path, "reviewer-only-plan")
    _write_sidecar(tmp_path, _SID, stamped_at="2026-08-27T13:00:00+00:00")
    gate_a = wsc.brief(
        decisions={"governing_plan_slug": "reviewer-only-plan", "subject": "x"}, repo_root=tmp_path
    )["gates"]["review_receipt"]
    assert gate_a["blocks"] is False
    assert "no integrator receipt" in gate_a["detail"]

    # (b) integrator receipt only -- wipe the reviewer sidecar first
    for stale in (tmp_path / "state" / "subagent-share" / _SID).glob("*.md"):
        stale.unlink()
    _write_clean_plan(tmp_path, "integrator-only-plan")
    _write_sidecar(
        tmp_path,
        _SID,
        agent_type=_INTEGRATOR_TYPE,
        stamped_at="2026-08-27T13:00:00+00:00",
        receipt_key="integrator_receipt",
    )
    gate_b = wsc.brief(
        decisions={"governing_plan_slug": "integrator-only-plan", "subject": "x"},
        repo_root=tmp_path,
    )["gates"]["review_receipt"]
    assert gate_b["blocks"] is True


# ---------------------------------------------------------------------------
# C3 (docs/plans/2026-08-30-the-close-time-review-floor-excludes-its-mandatory-
# reviewer.md): the gate reads `CLOSE_RECEIPT_REVIEWERS`, not
# `DELEGATE_REVIEWERS` — so Kira (`coordinator:overengineering-reviewer`), the
# one reviewer the PM mandates on EVERY close, is able to satisfy the floor
# she is stamped a receipt for. `DELEGATE_REVIEWERS` alone would leave this
# gate refusing the only reviewer a `single-session` close is allowed to
# dispatch (the plan's own problem statement) -- these three cases pin that
# fix, and only that fix: session-id and blank-body still block identically
# for a Kira sidecar as for any other.
# ---------------------------------------------------------------------------

_KIRA_TYPE = "coordinator:overengineering-reviewer"


def test_kira_only_sidecar_with_valid_receipt_clears_the_gate(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate())
    _write_clean_plan(tmp_path, "kira-only-plan")
    _write_sidecar(
        tmp_path,
        _SID,
        agent_type=_KIRA_TYPE,
        body="## Findings\n\nNo overengineering found.\n",
    )

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "kira-only-plan", "subject": "x"}, repo_root=tmp_path
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is False

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" not in jp_ids
    assert "jp-review-receipt-block-stamp" not in _depends_on(_stamp_directive(decision_object))


def test_kira_sidecar_with_foreign_session_id_still_blocks(monkeypatch, tmp_path):
    _patch_gate(monkeypatch, _gate())
    _write_clean_plan(tmp_path, "kira-foreign-sid-plan")
    # The sidecar lives under THIS session's own directory (as a real dispatch
    # would place it), but the receipt block's own `session_id` names a
    # foreign session -- (b)'s check, distinct from the directory-listing
    # miss the plain-`is_dir()` branch above already covers.
    sidecar_dir = tmp_path / "state" / "subagent-share" / _SID
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = sidecar_dir / f"{_KIRA_TYPE.replace(':', '')}.deadbeef02.md"
    doc = (
        f"---\nstatus: complete\nagent_type: {_KIRA_TYPE}\nlead_session_id: {_SID}\n"
        "commits: []\n---\n\n## Findings\n\nNo overengineering found.\n"
    )
    doc = provision_report._splice_review_receipt(
        doc, "some-other-session-id", "deadbeef02", _KIRA_TYPE, "2026-08-27T13:00:00+00:00"
    )
    sidecar_path.write_text(doc, encoding="utf-8")

    decision_object = wsc.brief(
        decisions={"governing_plan_slug": "kira-foreign-sid-plan", "subject": "x"},
        repo_root=tmp_path,
    )

    review_receipt_gate = decision_object["gates"]["review_receipt"]
    assert review_receipt_gate["blocks"] is True

    jp_ids = {jp["id"] for jp in decision_object["judgment_points"]}
    assert "jp-review-receipt-block-stamp" in jp_ids
    assert "jp-review-receipt-block-stamp" in _depends_on(_stamp_directive(decision_object))
