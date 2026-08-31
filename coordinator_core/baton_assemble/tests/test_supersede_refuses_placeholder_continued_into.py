"""Regression coverage for guard (b),
state/bug-backlog/2026-08-31-pickup-after-a-close-supersedes-the-new-baton.yaml:
a `continued_into` target still carrying an unreplaced `coordinator-doc-new`
PLACEHOLDER title/summary must never be written onto a predecessor's
frontmatter -- it is not a successor, it is an empty stub, and stamping it
in removes the predecessor from the pickup queue with nothing real on the
far end of the edge.

THE CLAIM-HOLDING TRIGGER, NOT THE CLOSE TRIGGER. The bug's own `repro` key
is wrong -- it names a post-`/workstream-complete` pickup as the trigger.
`second_instance` (added 2026-08-31, session f2fdabbc) corrects it: the
sufficient condition is HOLDING A CLAIM on any other baton, no close
anywhere. `TestFanInSelfBriefNamesAPlaceholderSuccessor` below reproduces
that shape exactly -- two UNRELATED batons, both claimed via the durable
claim ledger (`_seed_handoff_claim`), no close, no `/workstream-complete` --
mirroring `_unify_into_successor`'s own multi-parent self-brief
(`kind="handoff"`, `artifact_path=""`) rather than the filed `repro`'s
close-then-pickup shape. A second, narrower case
(`TestDispatchRefusesAPlaceholderTarget`) covers the same guard at the
dispatcher-unit level, close-agnostic by construction.

Spec backlink: `coordinator_core.baton_assemble.apply._dispatch_handoff_
supersede_predecessor` and its new `_continued_into_target_is_placeholder`
guard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _init_repo,
    _seed_claimed_predecessor,
    _write_artifact,
)

# Spawns no external process -- every test here is a direct in-process call
# into `ba.brief`/`ba_apply._dispatch_handoff_supersede_predecessor` with
# `_invoke_op_in_process` stubbed. `_init_repo` DOES spawn real git (needed
# for `ba.brief`'s claim-ledger/lineage resolution), matching the sibling
# suite's own `pytestmark` for that reason.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def _seed_handoff_claim(repo_root: Path, session_id: str, basename: str, claimed_at: str) -> None:
    claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _write_placeholder_successor(path: Path, *, fan_in: bool = True) -> Path:
    """The exact shape `coordinator-doc-new`'s no-`--title` handoff scaffold
    leaves behind (`coordinator/bin/coordinator-doc-new.py` line ~5992):
    `title: "PLACEHOLDER --- replace with one-line handoff title"`. This is
    what every `_unify_into_successor` mint looks like -- `title=None` is
    passed unconditionally on that call path.

    `fan_in=True` (default) also carries `additional_predecessors:` -- the
    key `coordinator-doc-new.py` only ever writes on a genuine multi-parent
    mint (one or more `--additional-predecessor` flags), and the guard's
    own scoping signal (an ORDINARY single-predecessor `/handoff` mints the
    SAME placeholder title and must NOT be refused -- see the guard's
    docstring)."""
    lines = [
        'title: "PLACEHOLDER --- replace with one-line handoff title"',
        'summary: "PLACEHOLDER --- replace with one-line handoff summary"',
    ]
    if fan_in:
        lines += ["additional_predecessors:", "  - state/handoffs/some-other-parent.md"]
    return _write_artifact(path, lines)


class TestDispatchRefusesAPlaceholderTarget:
    """Unit-level coverage of the new guard, isolated from `brief()`'s own
    lineage resolution -- mirrors `TestDispatchHandoffSupersedePredecessor`'s
    existing pattern in `coordinator_core/test_baton_assemble.py`."""

    def test_op_is_never_invoked_when_continued_into_is_a_placeholder(self, tmp_path, monkeypatch):
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(op_name, params, repo_root):
            calls.append((op_name, params))
            return {"exit_code": 0, "transition": {"superseded": True, "moved": True}}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        _seed_claimed_predecessor(tmp_path)
        _write_placeholder_successor(tmp_path / "state" / "handoffs" / "successor.md")

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [
                "state/handoffs/predecessor.md",
                "state/handoffs/successor.md",
                "state/handoffs/successor.md",
            ],
            tmp_path,
        )

        assert calls == [], (
            "the supersede op must never be composed against a "
            f"PLACEHOLDER-carrying continued_into target -- got {calls!r}"
        )
        assert result["result"] is None
        assert result["degraded"]["reason"] == "continued-into-target-is-placeholder"
        # The predecessor's own frontmatter is untouched -- still readable
        # and still carrying the SAME claim stamp `_seed_claimed_predecessor`
        # wrote, never flipped `continued`.
        predecessor_text = (tmp_path / "state" / "handoffs" / "predecessor.md").read_text(
            encoding="utf-8"
        )
        assert "continued_into" not in predecessor_text
        assert "deployment_state: continued" not in predecessor_text

    def test_op_is_invoked_normally_when_continued_into_carries_a_real_title(
        self, tmp_path, monkeypatch
    ):
        """Negative control: the guard is scoped to PLACEHOLDER targets
        only -- an ordinary, operator-titled successor must supersede
        exactly as before."""
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(op_name, params, repo_root):
            calls.append((op_name, params))
            return {"exit_code": 0, "transition": {"superseded": True, "moved": True}}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        _seed_claimed_predecessor(tmp_path)
        _write_artifact(
            tmp_path / "state" / "handoffs" / "successor.md",
            ['title: "A genuine, operator-authored successor title"'],
        )

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [
                "state/handoffs/predecessor.md",
                "state/handoffs/successor.md",
                "state/handoffs/successor.md",
            ],
            tmp_path,
        )

        assert len(calls) == 1, "a real successor title must reach the supersede op"
        assert result["result"]["superseded"] is True

    def test_op_is_invoked_normally_for_an_ordinary_single_predecessor_placeholder_mint(
        self, tmp_path, monkeypatch
    ):
        """Negative control (the regression `test_predecessor_back_edge.py`
        caught on first pass): an ORDINARY single-predecessor `/handoff`
        mints its successor with the SAME default PLACEHOLDER title/summary
        d1 always leaves before the operator has a chance to edit it -- no
        `additional_predecessors:` key, because this is not a fan-in. The
        guard must not refuse this shape; only the fan-in-plus-placeholder
        combination is refused."""
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(op_name, params, repo_root):
            calls.append((op_name, params))
            return {"exit_code": 0, "transition": {"superseded": True, "moved": True}}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        _seed_claimed_predecessor(tmp_path)
        _write_placeholder_successor(
            tmp_path / "state" / "handoffs" / "successor.md", fan_in=False
        )

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [
                "state/handoffs/predecessor.md",
                "state/handoffs/successor.md",
                "state/handoffs/successor.md",
            ],
            tmp_path,
        )

        assert len(calls) == 1, (
            "an ordinary single-predecessor continuation must supersede "
            "normally even though its successor is still placeholder-titled "
            f"at supersede time -- got {calls!r}"
        )
        assert result["result"]["superseded"] is True


class TestFanInSelfBriefNamesAPlaceholderSuccessor:
    """End-to-end reproduction of the filed incident's `second_instance`
    trigger: TWO UNRELATED batons, BOTH claimed via the durable claim
    ledger, NO CLOSE anywhere -- the exact shape `/pickup`'s own C5
    unification routing (`route_baton_adoption` ->
    `_unify_into_successor` -> `baton_assemble.apply.apply("handoff", "",
    ...)`) reaches, reproduced here entirely inside `baton_assemble`'s own
    reach (no `pickup_assemble` import) since the routing decision itself
    lives outside this fix's file scope."""

    def test_self_brief_over_two_unrelated_claimed_batons_names_a_placeholder_d6_target(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        held = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-31-fleet-python-import-topology.md",
            ["deliverable_id: DEL-HELD-1", "handoff_id: hnd-held-1a2b3c"],
        )
        picked_up = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-30-the-install-chain.md",
            ["deliverable_id: DEL-PICKED-UP-1", "handoff_id: hnd-picked-up-4d5e6f"],
        )
        _seed_handoff_claim(tmp_path, "sid-second-instance", held.name, claimed_at="2026-08-31T10:47:12Z")
        _seed_handoff_claim(
            tmp_path, "sid-second-instance", picked_up.name, claimed_at="2026-08-31T11:35:15Z"
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-second-instance")

        # No `/workstream-complete`, no close, no ship stamp anywhere above
        # this line -- both batons are simply held, exactly as `second_
        # instance` describes. This is `_unify_into_successor`'s own call
        # shape: kind="handoff", artifact_path="".
        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["additional_predecessors"], (
            "fixture must reproduce a genuine fan-in -- got no additional "
            f"predecessors: {lineage!r}"
        )

        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        successor_out = next(a for a in d1["args"] if a.startswith("--out="))[len("--out="):]
        assert not any(a.startswith("--title=") for a in d1["args"]), (
            "a self-brief unification mint supplies no --title, exactly "
            "like _unify_into_successor's own apply() call -- the successor "
            "is a PLACEHOLDER scaffold by construction"
        )

        d6s = [d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"]
        assert len(d6s) == 2, "one d6 per held predecessor -- both legs must be gated"
        for d6 in d6s:
            assert d6["args"][1] == successor_out

        # The successor is not scaffolded by this test (no real
        # coordinator-doc-new spawn) -- write the SAME placeholder shape the
        # real generator would have left, then drive each d6 directive
        # through the actual dispatcher to prove neither predecessor is
        # superseded.
        _write_placeholder_successor(tmp_path / successor_out)

        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(op_name, params, repo_root):
            calls.append((op_name, params))
            return {"exit_code": 0, "transition": {"superseded": True, "moved": True}}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        for d6 in d6s:
            result = ba_apply._dispatch_handoff_supersede_predecessor(d6["args"], tmp_path)
            assert result["degraded"]["reason"] == "continued-into-target-is-placeholder"

        assert calls == [], (
            "neither the held nor the picked-up baton may be superseded into "
            f"a PLACEHOLDER successor -- the op was composed anyway: {calls!r}"
        )
