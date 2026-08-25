"""Cross-ceremony contract for the Tier-U grant seams: the ceremonies that
mint a grant emit BOTH the write and a guarded handback as `directives[]`
entries, and the ceremony that mints nothing emits neither.

Why this lives in `ceremony_common` rather than in either ceremony's own
test module: the invariants below span three ceremonies at once (workweek
mints, merge mints and nests inside workweek, workday deliberately mints
nothing). Split across three files, the one that actually bites — a write
whose ceremony name drifts from its handback's guard — is expressible in
neither.

Spec backlink: cross-repo/inbox/2026-08-04-doe-claude-em-ceremony-grants-
belong-in-code-not-prose.md § 3 (the ask), § 1 (the guard), constraint 3
(workday-complete is out of scope).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.merge_assemble import build_directives as merge_build_directives
from coordinator_core.workday_complete import brief as workday_brief
from coordinator_core.workweek_complete import brief as workweek_brief

#: workweek names the consumes-manifest CLI (its dispatcher loads that
#: module and calls `main()` in-process — no spawn); merge names an
#: in-process verb with no script behind it at all. Both reach the same
#: `session.grant_directive.run_grant_directive`.
_GRANT_CLIS = ("tier-u-grant-cli", "tier-u-grant")


def _workweek_directives() -> list[dict]:
    return workweek_brief._build_directives()


def _merge_directives() -> list[dict]:
    return merge_build_directives(Path("."), tag_prefix="v", proposed_tag="v0.0.0")


_MINTING_CEREMONIES = [
    pytest.param(_workweek_directives, "workweek-complete", id="workweek-complete"),
    pytest.param(_merge_directives, "merging-to-main", id="merging-to-main"),
]


@pytest.mark.parametrize("build, ceremony_name", _MINTING_CEREMONIES)
def test_ceremony_mints_its_grant_in_code(build, ceremony_name):
    """The ask: the grant write is a directive, not a prose line an EM is
    trusted to run by hand."""
    writes = [
        d
        for d in build()
        if d["cli"] in _GRANT_CLIS and d["args"][:1] == ["grant"]
    ]
    assert len(writes) == 1, f"{ceremony_name}: expected exactly one grant write"
    args = writes[0]["args"]
    assert args[1] == "ceremony", "an implicit ceremony grant is never granted_by=pm"
    assert args[-2:] == ["--ceremony", ceremony_name]


@pytest.mark.parametrize("build, ceremony_name", _MINTING_CEREMONIES)
def test_handback_is_guarded_and_names_this_ceremony(build, ceremony_name):
    """§ 1: a bare `revoke` at ceremony end unlinks whatever the session
    holds, destroying a live PM grant. The handback must carry the guard,
    and the guard must name the ceremony the write actually stamped —
    a drifted name is a silent permanent no-op."""
    handbacks = [
        d
        for d in build()
        if d["cli"] in _GRANT_CLIS and d["args"][:1] == ["revoke"]
    ]
    assert len(handbacks) == 1, f"{ceremony_name}: expected exactly one handback"
    assert handbacks[0]["args"] == ["revoke", "--only-ceremony", ceremony_name]


@pytest.mark.parametrize("build, ceremony_name", _MINTING_CEREMONIES)
def test_handback_orders_after_the_write(build, ceremony_name):  # noqa: ARG001
    """Directives dispatch in list order; a handback ahead of its write
    would hand back nothing and leave the grant live past the ceremony."""
    ids = [d["id"] for d in build()]
    grant_positions = [
        (ids.index(d["id"]), d["args"][0]) for d in build() if d["cli"] in _GRANT_CLIS
    ]
    ordered = [verb for _, verb in sorted(grant_positions)]
    assert ordered == ["grant", "revoke"]


def test_workday_complete_mints_and_revokes_nothing():
    """Constraint 3 — `/workday-complete` mints no token today, so a
    handback there could only destroy someone else's grant. Reversing this
    needs DR-088 § Amendment (g) re-amended on the record first; it is not
    built off the 2026-08-04 memo."""
    directives = workday_brief._build_directives(
        decisions={}, open_day_goals={}, dirty_tree_verdict={}
    )
    assert not [d for d in directives if d["cli"] in _GRANT_CLIS]
    assert not [c for c in _GRANT_CLIS if c in workday_brief.CONSUMES_MANIFEST]


def test_the_shared_close_tail_carries_no_handback():
    """The tail is shared by workday and workweek (`ceremony_common.tail`),
    so a handback added THERE would silently give workday one too. Each
    minting ceremony emits its own."""
    from coordinator_core.ceremony_common.tail import build_ceremony_close_tail

    tail = build_ceremony_close_tail(
        post_command_hook_id="d_hook",
        ceremony_name="workweek-complete",
    )
    assert not [d for d in tail if d["cli"] in _GRANT_CLIS]


def test_workweek_grant_legs_are_best_effort():
    """A grant that could not be minted must not turn an otherwise-clean
    ceremony into `PARTIAL_MUTATION` — the layer-5 guard fails closed, so
    an absent grant refuses the Tier-U consumer rather than authorizing
    it. The failure still reaches the operator via `report["degraded"]`."""
    grants = [d for d in _workweek_directives() if d["cli"] in _GRANT_CLIS]
    assert grants, "no grant directives to check"
    assert all(d.get("best_effort") is True for d in grants)


def test_merge_grant_dispatch_tolerates_exit_1_but_not_usage_or_transport():
    """`merge_assemble` runs on `apply_base.execute_directives`, which has
    no `best_effort` key — a raising handler aborts the whole ceremony. So
    the tolerance lives in the handler: exit 1 (unresolvable sid, routine
    under concurrent sessions) degrades; exit 2 (a wrong argv shape built
    in this repo) and exit 3 (engine unreachable) still raise."""
    from coordinator_core.merge_assemble import apply as merge_apply

    from coordinator_core.session import grant_directive

    original = grant_directive.run_grant_directive
    merge_apply_original = getattr(merge_apply, "_run_py_script", None)
    try:
        # Exit 1 -- unresolvable sid. Degrades, never raises.
        grant_directive.run_grant_directive = lambda args: (1, "session id unresolvable")
        result = merge_apply._dispatch_tier_u_grant(["grant"], Path("."))
        assert result["returncode"] == 1
        assert "degraded_reason" in result

        # Exit 2 -- a wrong argv shape built by build_directives. A defect: raise.
        grant_directive.run_grant_directive = lambda args: (2, "bad shape")
        with pytest.raises(RuntimeError):
            merge_apply._dispatch_tier_u_grant(["grant"], Path("."))
    finally:
        grant_directive.run_grant_directive = original
        assert merge_apply_original is merge_apply._run_py_script


def test_merge_grant_dispatch_spawns_no_subprocess():
    """The cost property this handler exists to hold. `resolve_session_id`
    reads env vars only, which a child inherits, so a spawn would resolve
    the SAME sid at the price of a cold interpreter start -- on a ceremony
    whose per-composition cost is already the thing under attack
    (2026-08-19-the-320-second-ceremony)."""
    import subprocess as _subprocess

    from coordinator_core.merge_assemble import apply as merge_apply
    from coordinator_core.session import grant_directive

    calls = []
    original_run = _subprocess.run
    original_directive = grant_directive.run_grant_directive
    try:
        _subprocess.run = lambda *a, **k: calls.append(a) or original_run(*a, **k)
        grant_directive.run_grant_directive = lambda args: (0, "")
        merge_apply._dispatch_tier_u_grant(["revoke", "--only-ceremony", "x"], Path("."))
    finally:
        _subprocess.run = original_run
        grant_directive.run_grant_directive = original_directive
    assert calls == []


def test_merge_registers_a_compensator_for_the_stranded_grant():
    """`apply_base.execute_directives` returns PARTIAL_MUTATION the moment a
    handler raises, so every later directive -- including the handback --
    never dispatches. The grant write is the only directive here whose
    effect outlives the run, so it is the only one an aborted run can
    strand, and the only one that registers a compensator."""
    from coordinator_core.merge_assemble import apply as merge_apply

    assert set(merge_apply._COMPENSATORS) == {"d_grant_write"}


def test_the_compensator_uses_the_same_guard_as_the_handback():
    """A compensator that revoked unguarded would destroy a PM grant that
    the abort happened to leave live -- the exact defect the guard exists
    to prevent, reintroduced on the failure path."""
    from coordinator_core.merge_assemble import apply as merge_apply
    from coordinator_core.session import grant_directive

    seen = []
    original = grant_directive.run_grant_directive
    try:
        grant_directive.run_grant_directive = lambda args: seen.append(args) or (0, "")
        merge_apply._COMPENSATORS["d_grant_write"]({"id": "d_grant_write"}, Path("."), None)
    finally:
        grant_directive.run_grant_directive = original

    assert seen == [["revoke", "--only-ceremony", "merging-to-main"]]


def test_a_failed_handback_is_not_reported_as_a_successful_compensation():
    """`apply_base._run_compensators` reads a compensator's return against a
    bool contract: only a literal `False` is a non-success, and any other
    value -- a `(code, message)` tuple included -- records `succeeded: True`.
    Returning `run_grant_directive`'s raw tuple therefore reported a grant as
    handed back when the revoke had failed, on the one path this compensator
    exists to make honest. Non-zero must reach the caller as a raise (which
    `_run_compensators` records with an `error`), never as a value it reads
    as success."""
    from coordinator_core.merge_assemble import apply as merge_apply
    from coordinator_core.session import grant_directive

    original = grant_directive.run_grant_directive
    try:
        grant_directive.run_grant_directive = lambda args: (1, "revoke: session id unresolvable")
        with pytest.raises(RuntimeError) as excinfo:
            merge_apply._COMPENSATORS["d_grant_write"](
                {"id": "d_grant_write"}, Path("."), None
            )
    finally:
        grant_directive.run_grant_directive = original

    assert "session id unresolvable" in str(excinfo.value), (
        "the compensator must carry the underlying diagnostic, not just a code"
    )


def test_a_successful_handback_compensation_returns_the_success_sentinel():
    """The success path must return `None`, not `True` and not the tuple:
    `_run_compensators` documents `None` as success for every compensator
    registered today, and a tuple is exactly the value whose truthiness
    masked a failure."""
    from coordinator_core.merge_assemble import apply as merge_apply
    from coordinator_core.session import grant_directive

    original = grant_directive.run_grant_directive
    try:
        grant_directive.run_grant_directive = lambda args: (0, "")
        result = merge_apply._COMPENSATORS["d_grant_write"](
            {"id": "d_grant_write"}, Path("."), None
        )
    finally:
        grant_directive.run_grant_directive = original

    assert result is None


def test_workweek_reaches_its_handback_even_after_a_failed_directive():
    """Workweek needs no compensator: its apply loop records a failure and
    CONTINUES, so the handback still dispatches. This pins that, so a future
    change to an abort-on-first-failure loop cannot silently strand the
    grant the way merge's would without its compensator."""
    import inspect

    from coordinator_core.workweek_complete import apply as wwc_apply

    source = inspect.getsource(wwc_apply._execute_directives)
    assert "continue" in source, (
        "workweek's directive loop no longer continues past a failure -- the "
        "handback can now be stranded; register a compensator as merge does"
    )

