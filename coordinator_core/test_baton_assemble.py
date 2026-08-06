"""
coordinator_core.test_baton_assemble -- co-located pytest for
coordinator_core.baton_assemble (compute half) + coordinator_core.baton_assemble.apply
(mutating half).

Mirrors the test_sizing_assemble.py / test_pickup_apply.py idiom: import the
module directly, exercise it in-process against tmp_path fixtures (no
subprocess round-trip to a real CLI). Covers:

  (a) decision-object key shapes -- the 8-key envelope, directive
      id/cli/args/depends_on fields, judgment_point dispositions/resolves/
      recommendation/reason fields -- come from the Tier-B constructors, not
      a hand-rolled dict literal (module's own negative-spec).
  (b) brief() is read-only: mutates no input, writes no disk.
  (c) the kind-parametrized cascade -- handoff vs spinoff parent-discovery
      order and companion-id fields.
  (d) a CLI smoke per subcommand (brief/apply) + the usage-error path.
  (e) a spy proving brief() calls resolve_operator_config() (B0's shared
      resolver) rather than re-deriving its own roots.
  (f) apply_base runner tests -- closed-dispatch rejection, per-directive
      halt, no-op, partial-mutation reporting -- asserted once against
      baton_assemble.apply's OWN composition of the factored runner
      (coordinator_core.contract.apply_base), per this chunk's own
      instruction. apply_base's domain-agnostic behavior against a
      synthetic dispatch table is already covered directly by
      coordinator_core/contract/test_apply_base.py (C2) -- this file proves
      baton's *wiring* to that runner, not the runner's own internals a
      second time.

Spec backlink: example-doctrine-repo docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md,
chunk C3 (depends C1-C2).

Run: python -m pytest coordinator_core/test_baton_assemble.py -q
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.ops.deliverable_carry import DroppedDeliverableJoinError
from coordinator_core.ops.fleet._common import plan_claim_dir
from coordinator_core.session import claims as session_claims
from coordinator_core.session import shape as session_shape

_FAKE_OPERATOR_CONFIG = {
    "settings_home": "/fake/settings-home",
    "claude_klabauter_bin": "/fake/settings-home/bin",
    "doe_root": "/fake/doe-root",
}


#: The generator `coordinator-doc-new` lives in THIS repo, beside
#: `coordinator_core` -- resolved off the test file's own location, never off a
#: settings-home lookup, because this suite runs under a HOME quarantine that
#: makes `resolve_operator_config()` fail loud by design.
_REPO_CLAUDE_KLABAUTER_BIN = Path(__file__).resolve().parents[1] / "coordinator" / "bin"


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    """Every test gets a fixed, machine-independent operator config unless a
    test explicitly monkeypatches its own spy -- brief()'s own call to
    resolve_operator_config() must never depend on THIS dev machine's real
    settings-home layout.

    `_resolve_claude_klabauter_bin` is pinned to the REAL in-repo `coordinator/bin`
    rather than the fake, and that difference is load-bearing:
    `_is_pristine_generator_scaffold` decides whether to DELETE a file by
    re-rendering `coordinator-doc-new`'s own template, so pointing it at a
    non-existent bin would make every such check decline for the wrong reason
    (generator unavailable) and quietly assert nothing. Machine-independent
    either way -- the path is derived from this file's location, not HOME."""
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def _write_artifact(path: Path, fm_lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = "".join(f"{line}\n" for line in fm_lines)
    path.write_text(f"---\n{fm}---\n\n# Artifact\n\nBody.\n", encoding="utf-8")
    return path


def _seed_claimed_predecessor(
    repo_root: Path, rel: str = "state/handoffs/predecessor.md"
) -> Path:
    """C5/AC8, DR-242 (`docs/decisions/DR-242-successor-named-child-is-not-
    evidence-of-succ.md`): seed a predecessor handoff's frontmatter with
    `claimed_at`/`claimed_by` so it honestly satisfies
    `coordinator_core.archival.claimed_or_shipped_at_path` --
    a supersede-dispatch test exercises a legitimate succession target this
    way, rather than the bare never-claimed shape DR-242 exists to refuse.
    Shared by `TestDispatchHandoffSupersedePredecessor` so there is exactly
    one place in this file that knows what a legitimate supersede target
    looks like (mirrors `_StubHarness._seed_claimed_frontmatter` in
    `coordinator/bin/tests/test_handoff_archive_transition.py`'s CLI-layer
    sibling)."""
    return _write_artifact(
        repo_root / rel,
        ["claimed_at: 2026-07-20T10:00:00Z", "claimed_by: test-session"],
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    """Real git repo (not merely a directory) -- needed by the
    `_invoke_op_in_process` regression tests below because
    `coordinator_core.lifecycle.git_common_dir` shells out to
    `git rev-parse --path-format=absolute --git-common-dir`, which requires
    an actual `.git` to resolve against."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


# ---------------------------------------------------------------------------
# (a) decision-object key shapes
# ---------------------------------------------------------------------------


class TestDecisionObjectKeyShapes:
    def test_envelope_has_exactly_the_8_canonical_keys(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', "initiative: init-1", 'predecessor: "none"'],
        )
        result = ba.brief("handoff", str(artifact), repo_root=tmp_path)
        decision = result.decision_object
        assert set(decision.keys()) == {
            "artifact",
            "preflight",
            "gates",
            "directives",
            "judgment_points",
            "decisions",
            "narration",
            "next_move",
        }

    def test_directive_shape_has_id_cli_args_depends_on(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        assert decision["directives"], "expected at least one directive"
        for directive in decision["directives"]:
            assert {"id", "cli", "args", "depends_on", "already_satisfied"} <= set(directive.keys())
            assert isinstance(directive["id"], str)
            assert isinstance(directive["cli"], str)
            assert isinstance(directive["args"], list)

    def test_judgment_point_shape_has_dispositions_resolves_recommendation_reason(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        assert decision["judgment_points"], "expected at least one judgment point"
        for jp in decision["judgment_points"]:
            assert "dispositions" in jp
            assert "recommendation" in jp
            assert "reason" in jp
            assert jp["recommendation"] is None  # untrusted-gate constructor: structurally None
            for disposition in jp["dispositions"]:
                assert "value" in disposition
                assert "resolves" in disposition


# ---------------------------------------------------------------------------
# (b) brief() is read-only
# ---------------------------------------------------------------------------


class TestBriefIsReadOnly:
    def test_brief_mutates_no_input_and_writes_no_disk(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        original_text = artifact.read_text(encoding="utf-8")
        decisions = {"jcc": {"disposition": "proceed"}}
        decisions_copy = dict(decisions)

        files_before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
        ba.brief("handoff", str(artifact), decisions=decisions, repo_root=tmp_path)
        files_after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())

        assert artifact.read_text(encoding="utf-8") == original_text
        assert decisions == decisions_copy
        assert files_before == files_after


# ---------------------------------------------------------------------------
# (c) kind-parametrized cascade
# ---------------------------------------------------------------------------


class TestKindParametrizedCascade:
    def test_handoff_kind_resolves_predecessor_order_and_predecessor_id_companion(self, tmp_path):
        """`artifact` (h1.md) carries no `handoff_id` of its own -- the
        non-handoff (plan->execute) lineage-source tier, per
        `resolve_lineage`'s `own_handoff_id` discriminator -- so its OWN
        `predecessor:` field is what gets carried. See
        `TestHandoffInputBecomesItsOwnPredecessor` below for the sibling
        (and far more common) case where `artifact_path` IS itself a real
        handoff record."""
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["kind"] == "handoff"
        assert lineage["predecessor_id"] == "hnd-1-1a2b3c"

        clis = {d["cli"] for d in decision["directives"]}
        # 2026-07-25: handoff.stamp_phase is deliberately NOT emitted -- d1's
        # scaffold already stamps handoff_phase:continuation unconditionally,
        # so a dedicated stamp directive here would always be a no-op (see
        # coordinator_core/baton_assemble/__init__.py's module docstring).
        assert "handoff.stamp_phase" not in clis
        assert "render-project-tracker" in clis
        assert "handoff.author_fork" not in clis

        render_tracker_directive = next(d for d in decision["directives"] if d["cli"] == "render-project-tracker")
        assert render_tracker_directive["depends_on"] == ["d1"]

        jp_ids = {jp["id"] for jp in decision["judgment_points"]}
        assert "j-continuation-vs-fork" in jp_ids

    def test_spinoff_kind_resolves_origin_companion_fields(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            [
                "deliverable_id: DEL-2",
                "handoff_id: hnd-2-1a2b3d",
                "claimed_by: sid-origin",
                "plan_id: PLAN-2",
                "goal_id: GOAL-2",
            ],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["kind"] == "spinoff"
        assert lineage["origin_handoff_id"] == "hnd-2-1a2b3d"
        assert lineage["origin_session"] == "sid-origin"
        assert lineage["origin_plan_id"] == "PLAN-2"
        assert lineage["origin_goal_id"] == ["GOAL-2"]

        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.author_fork" in clis
        assert "handoff.stamp_phase" not in clis
        assert "render-project-tracker" not in clis

        jp_ids = {jp["id"] for jp in decision["judgment_points"]}
        assert "j-continuation-vs-fork" not in jp_ids

    def test_spinoff_kind_no_longer_inherits_stub_hit_mints_fresh(self, tmp_path):
        """2026-08-05 PM ruling (widening the roadmap-baton-kind-skip fix):
        a spinoff does NOT inherit `deliverable_id` from its progenitor
        (`artifact_path`) by default, even when the progenitor is a
        handoff-schema-shaped hit (carries a `kind` field) that DOES resolve
        one. Before this ruling, that hit was silently carried and labeled
        `discovery == "stub"` (this test used to pin
        `test_spinoff_kind_reports_stub_tier_on_hit`'s exact assertion,
        `lineage["discovery"] == "stub"`) -- the `"stub"` tier is retired
        for the inherit path it used to name (see `resolve_lineage`'s own
        docstring). The default path now mints fresh regardless of the
        progenitor's own `deliverable_id` field, reported as
        `discovery == "mint"`, and the progenitor's id (`DEL-STUB-1`) must
        NOT appear on the fork."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin-stub-hit.md",
            ["deliverable_id: DEL-STUB-1", "handoff_id: hnd-stub-1-1a2b54", "kind: session-handoff"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["discovery"] == "mint"
        assert lineage["deliverable_id"] != "DEL-STUB-1"
        assert lineage["deliverable_id"] is None

    def test_spinoff_kind_no_longer_inherits_plan_hit_mints_fresh(self, tmp_path):
        """2026-08-05 PM ruling, sibling of the stub-hit case above: a
        progenitor with NO `kind` field (a `docs/plans/*.md` plan document
        never carries one) that DOES resolve a `deliverable_id` used to be
        carried and labeled `discovery == "plan"` (this test used to pin
        `test_spinoff_kind_reports_plan_tier_on_non_stub_hit`'s exact
        assertion, `lineage["discovery"] == "plan"`) -- also retired. The
        default path mints fresh here too, and the progenitor's id
        (`DEL-PLAN-1`) must not appear on the fork."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin-plan-hit.md",
            ["deliverable_id: DEL-PLAN-1"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["discovery"] == "mint"
        assert lineage["deliverable_id"] != "DEL-PLAN-1"
        assert lineage["deliverable_id"] is None

    def test_spinoff_kind_initiative_still_inherits_from_progenitor(self, tmp_path):
        """2026-08-05 PM ruling named `deliverable_id` ONLY -- `initiative`
        keeps inheriting from the progenitor's frontmatter exactly as
        before this ruling, unchanged."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin-initiative-hit.md",
            ["deliverable_id: DEL-INIT-1", "initiative: INIT-CARRIED", "kind: session-handoff"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["discovery"] == "mint"
        assert lineage["deliverable_id"] is None
        assert lineage["initiative"] == "INIT-CARRIED"

    def test_spinoff_explicit_deliverable_id_carried_unchanged(self, tmp_path):
        """The one opt-in this ruling sanctions for `resolve_lineage` itself
        (distinct from `coordinator-doc-new`'s own separate `--deliverable-
        id` flag, which never routes through this function -- see
        `resolve_lineage`'s docstring): a caller-supplied
        `explicit_deliverable_id` is carried through UNCHANGED, never
        re-minted, and reports `discovery == "explicit"` even though the
        progenitor's own frontmatter resolves a DIFFERENT id that must not
        win."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin-explicit-hit.md",
            ["deliverable_id: DEL-PROGENITOR-SHOULD-NOT-WIN", "kind: session-handoff"],
        )
        lineage = ba.resolve_lineage(
            "spinoff",
            str(artifact),
            tmp_path,
            explicit_deliverable_id="DEL-EXPLICIT-CARRY",
        )
        assert lineage["deliverable_id"] == "DEL-EXPLICIT-CARRY"
        assert lineage["discovery"] == "explicit"

    def test_handoff_kind_cascade_unaffected_by_spinoff_no_inherit_ruling(self, tmp_path):
        """The 2026-08-05 PM ruling is spinoff-only -- `kind == "handoff"`'s
        claimed-plan -> predecessor -> mint cascade must resolve the SAME
        tier and id it always has: a predecessor artifact's own
        `deliverable_id` is still carried (never re-minted) and labeled
        `discovery == "artifact"`, exactly as
        `test_handoff_kind_resolves_predecessor_order_and_predecessor_id_
        companion` already establishes for the predecessor_id companion
        field -- this test pins the `deliverable_id`/`discovery` pair that
        sibling test does not assert."""
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] == "DEL-1"
        assert lineage["discovery"] == "artifact"

    def test_spinoff_kind_bare_slug_mint_reports_mint_tier(self, tmp_path):
        """C8 (AC14): the bare-slug mint-fallback case (no `deliverable_id`
        resolvable from `artifact_path`) reports `discovery == "mint"` --
        the spinoff branch's own second tier, matching the handoff branch's
        `"mint"` fallback value."""
        decision = ba.brief("spinoff", "a-fresh-mint-slug-c8", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["discovery"] == "mint"
        assert lineage["standalone_no_predecessor_reason"] is None

    def test_spinoff_kind_bare_slug_mint_does_not_self_reference_origin_handoff(self, tmp_path):
        """Regression for the 2026-07-27 live break: the sanctioned
        `/spinoff <slug>` invocation (coordinator/skills/spinoff/SKILL.md)
        passes the NEW artifact's own mint slug as `artifact_path` -- there
        is no distinct existing origin file for `resolve_lineage` to read.
        Before this fix, `lineage["origin_handoff"]` unconditionally echoed
        `artifact_path` (the about-to-be-scaffolded output itself), so a
        live spinoff landed with `origin_handoff` pointing at its OWN path
        and `origin_handoff_id` null (the origin-handoff file never having
        existed for its `handoff_id` to be read from).

        `origin_handoff`/`origin_handoff_id` must come out None here -- NOT
        the mint slug's normalized path -- so the dispatcher passes them
        through unset and `handoff.author_fork`'s own self-resolution
        (`_resolve_origin_handoff`, tested end-to-end in
        `test_handoff_author_fork.py::TestStampMode::
        test_stamp_mode_self_resolves_when_caller_supplies_nothing`) can
        supply the session's ACTUAL held baton instead of a self-reference.
        A bare `is not None`-only assertion would have passed against the
        original bug (the self-referential value IS non-None) -- this
        asserts the specific wrong value is absent, not mere presence.
        """
        decision = ba.brief("spinoff", "a-fresh-mint-slug", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["origin_handoff"] is None
        assert lineage["origin_handoff_id"] is None
        # The output path IS the normalized mint slug (bare-slug mint
        # convention) -- confirming the bug's exact failure mode (echoing
        # this same value into origin_handoff) is what's being guarded.
        assert lineage["output_path"].endswith("-a-fresh-mint-slug.md")
        assert lineage["origin_handoff"] != lineage["output_path"]

        d3 = next(d for d in decision["directives"] if d["id"] == "d3")
        assert d3["args"][0] == ""  # origin_handoff slot -- empty, not self-referential
        assert d3["args"][1] == ""  # origin_handoff_id slot

    def test_spinoff_d3_args_widened_with_goal_id_and_d1_output_path(self, tmp_path):
        """d3 (2026-07-27 stamping rewrite, Option A): args widen to 6 slots
        -- origin_goal_id (";"-joined) is threaded through where it was
        previously dropped entirely, and slot 5 carries d1's OWN `--out`
        target (the file d3 will STAMP), never `lineage["artifact_path"]`
        (the origin this spinoff forks FROM)."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            [
                "deliverable_id: DEL-3",
                "handoff_id: hnd-3-1a2b3e",
                "claimed_by: sid-origin-3",
                "plan_id: PLAN-3",
                "goal_id: GOAL-3",
            ],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        d3 = next(d for d in decision["directives"] if d["id"] == "d3")

        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        d1_out = out_arg[len("--out="):]

        assert len(d3["args"]) == 6
        assert d3["args"] == [str(artifact), "hnd-3-1a2b3e", "sid-origin-3", "PLAN-3", "GOAL-3", d1_out]

    def test_spinoff_d3_args_null_goal_id_joins_to_empty_string(self, tmp_path):
        """No `goal_id` on the origin artifact -- `lineage["origin_goal_id"]`
        is None, and the ';'.join(...) convention must degrade to the empty
        string (not the literal 'None'), so the dispatcher's split-back-apart
        sees "no goal ids", not a one-element ["None"] list."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin-no-goal.md",
            ["deliverable_id: DEL-4", "handoff_id: hnd-4-1a2b3f"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        d3 = next(d for d in decision["directives"] if d["id"] == "d3")
        assert d3["args"][4] == ""

class TestHandoffInputBecomesItsOwnPredecessor:
    """2026-07-27 break-class fix regression: when `artifact_path` fed to
    `brief handoff` is ITSELF a real handoff record (carries its own
    `handoff_id` -- the common case, since d1's `coordinator-doc-new` mints
    one unconditionally), it must become the new successor's `predecessor`
    DIRECTLY -- per `coordinator/CLAUDE.md § Handoff Lineage` / `handoff/
    SKILL.md § Predecessor identification`: "the predecessor is whatever
    handoff this session was opened with -- period." Before this fix,
    `resolve_lineage` instead read a `predecessor:` field OFF of
    `artifact_path` -- walking one generation too far to artifact_path's own
    PARENT -- which meant `d6` superseded the grandparent while the true
    parent (`artifact_path` itself) was left claimed-but-never-continued.
    Reproduces the exact shape from the live incident: `artifact_path` is
    `state/handoffs/2026-07-27-execute-....md`, carrying its own
    `predecessor: state/handoffs/2026-07-26-....md` (its grandparent-to-be)
    and its own `handoff_id`."""

    def test_predecessor_is_the_passed_artifact_not_its_grandparent(self, tmp_path):
        grandparent = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-26-workstream-complete.md",
            ["handoff_id: hnd-grandparent-1a2b47"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-27-execute-workstream-complete.md",
            [
                "deliverable_id: DEL-1",
                "handoff_id: hnd-session-opened-with-1a2b51",
                f"predecessor: {grandparent.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == str(artifact)
        assert lineage["predecessor_id"] == "hnd-session-opened-with-1a2b51"
        assert lineage["predecessor"] != str(grandparent)
        assert lineage["predecessor_id"] != "hnd-grandparent-1a2b47"

        d6 = next(d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor")
        assert d6["args"][0] == str(artifact)
        assert d6["args"][0] != str(grandparent)

    def test_no_own_handoff_id_falls_back_to_predecessor_handoff_field(self, tmp_path):
        """A plan (no `handoff_id` of its own) carries its lineage pointer
        as `predecessor_handoff:` (the plan-authoring field name, e.g. this
        repo's own `docs/plans/2026-07-24-computed-skills-b4-baton-branch-
        lifecycle.md` frontmatter) rather than the handoff-record field
        `predecessor:` -- confirmed read here too."""
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor_handoff: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == str(predecessor.relative_to(tmp_path))
        assert lineage["predecessor_id"] == "hnd-1-1a2b3c"


    def test_predecessor_is_the_passed_artifact_even_without_own_handoff_id(self, tmp_path):
        """2026-08-06 break-class fix regression: `artifact_path` is a real
        handoff record (carries `kind:`) but -- the common case, since
        `handoff_id` is an optional-omit field on d1's own scaffold -- has
        NO `handoff_id` of its own. It must still become the new successor's
        `predecessor` DIRECTLY, not fall through to its own `predecessor:`
        field (the grandparent). Live reproduction: a consumed, in-flight
        handoff whose own `predecessor:` names an already-archived
        grandparent."""
        grandparent = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-26-workstream-complete.md",
            ["kind: session-handoff"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-27-execute-workstream-complete.md",
            [
                "deliverable_id: DEL-1",
                "kind: session-handoff",
                f"predecessor: {grandparent.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == str(artifact)
        assert lineage["predecessor"] != str(grandparent)
        assert lineage["predecessor_id"] is None

        d6 = next(d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor")
        assert d6["args"][0] == str(artifact)
        assert d6["args"][0] != str(grandparent)


class TestArchiveAwareResolution:
    """2026-07-28 break-class fix: `resolve_lineage(kind="handoff", ...)`
    used to silently degrade a caller-named `artifact_path` that no longer
    exists at its live location (typically the predecessor handoff this
    session was opened with, already swept to `archive/handoffs/` by the
    boot sweep) to empty frontmatter -- indistinguishable from the bare-slug
    mint convention. Covers the archive-search resolution and its fail-loud
    not-found counterpart, plus the bare-slug regression guard."""

    def test_archived_predecessor_resolves_and_d6_is_emitted(self, tmp_path):
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        predecessor = _write_artifact(
            archived_dir / "2026-07-20-earlier-session.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-archived-1a2b41", 'predecessor: "none"'],
        )
        live_path = tmp_path / "state" / "handoffs" / "2026-07-20-earlier-session.md"

        decision = ba.brief("handoff", str(live_path), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "DEL-1"
        assert lineage["predecessor_id"] == "hnd-archived-1a2b41"
        assert lineage["predecessor"] is not None
        assert lineage["predecessor_is_live"] is False

        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" in clis
        d6 = next(d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor")
        assert d6["args"][0] == lineage["predecessor"]

    def test_predecessor_named_via_bare_field_but_only_archived_is_not_reported_live(self, tmp_path):
        """2026-08-06 break-class fix: `lineage["predecessor_is_live"]` used
        to be a pure STRING-containment check (`does the path spell
        state/handoffs/...?`), true even when the named file does not
        actually exist there -- exactly the shape a `predecessor:`/
        `predecessor_handoff:` frontmatter field produces when it was
        written before the boot sweep archived that predecessor and the
        field itself was never rewritten (the `else`-branch field-walk below
        never routes through the archive-aware resolver, unlike the
        artifact_path-is-itself-a-handoff branch above)."""
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        _write_artifact(archived_dir / "predecessor.md", ["handoff_id: hnd-archived-1a2b41"])
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-1",
                "predecessor: state/handoffs/predecessor.md",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == "state/handoffs/predecessor.md"
        assert lineage["predecessor_is_live"] is False

    def test_qualified_path_resolving_nowhere_raises(self, tmp_path):
        (tmp_path / "state" / "handoffs").mkdir(parents=True)
        missing = tmp_path / "state" / "handoffs" / "2026-07-20-never-existed.md"
        with pytest.raises(ValueError, match="never-existed"):
            ba.brief("handoff", str(missing), repo_root=tmp_path)

    def test_bare_slug_mint_still_unaffected_by_archive_search(self, tmp_path):
        """Regression guard: the bare-slug mint convention must not attempt
        an archive search (nothing to find, nothing to raise about) --
        `was_bare_slug` routes around `_resolve_qualified_path_or_raise`
        entirely.

        C3 shift (docs/plans/2026-08-01-deliverable-id-carry-onto-executing-
        handoff.md): `deliverable_id` used to come out `None` here (the old
        `_fm_field(fm, ...)` read against empty frontmatter) -- C1 routes
        EVERY `kind="handoff"` resolution through `resolve_deliverable_and_
        initiative`, which always resolves to a CONCRETE id (mint-from-slug
        when, as here, no claimed plan/predecessor supplies one) rather than
        leaving the field null for a downstream `coordinator-doc-new` call to
        fill in later. The archive-search-skipped assertion this test exists
        for is unchanged; only the incidental `deliverable_id` value shifted.
        """
        decision = ba.brief("handoff", "a-fresh-mint-slug", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"].startswith("dlv-")
        assert lineage["discovery"] == "mint"
        assert lineage["predecessor"] is None


class TestSelfResolutionFromClaimLedger:
    """2026-07-28: kind="handoff" self-resolves its predecessor from the
    current session's own claim ledger when `artifact_path` is omitted --
    the kind=handoff analogue of kind=spinoff's pre-existing self-resolution.
    """

    def _seed_handoff_claim(
        self,
        repo_root: Path,
        session_id: str,
        basename: str,
        claimed_at: str | None = None,
    ) -> None:
        claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
        if claimed_at is not None:
            (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")

    def test_omitted_artifact_path_self_resolves_single_held_claim(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-held.md",
            ["deliverable_id: DEL-9", "handoff_id: hnd-held-1a2b48"],
        )
        self._seed_handoff_claim(tmp_path, "sid-held", predecessor.name)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-held")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor_id"] == "hnd-held-1a2b48"
        assert lineage["deliverable_id"] == "DEL-9"
        # AC-2: a single held claim stays byte-identical to pre-fix behavior --
        # no `additional_predecessors` populated, exactly one d6 directive
        # with the UNCHANGED bare id "d6" (never "d6-1" or similarly suffixed).
        assert lineage.get("additional_predecessors") is None
        d6_ids = [
            d["id"] for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"
        ]
        assert d6_ids == ["d6"]

    def test_zero_held_claims_produces_standalone_brief(self, tmp_path, monkeypatch):
        """2026-08-03 break-class fix: a memo-pickup session (resolvable
        session id, ZERO held handoff claims, no artifact-path) must NOT
        hard-fail -- the `/handoff` skill explicitly sanctions a standalone,
        no-predecessor brief for exactly this shape ("Neither? This handoff
        has no predecessor -- write standalone."). Asserts the brief
        succeeds, carries no predecessor, and names the standalone reason
        explicitly rather than merely leaving `predecessor` unexplained-None
        (which also happens for unrelated reasons elsewhere in this
        module)."""
        _init_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-nothing-held")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] is None
        assert lineage["predecessor_id"] is None
        assert lineage.get("additional_predecessors") is None
        assert lineage["standalone_no_predecessor_reason"] is not None
        assert "ZERO handoff claims" in lineage["standalone_no_predecessor_reason"]
        # No d6 (`handoff.supersede_predecessor`) directive -- nothing to
        # supersede when there is no predecessor.
        d6_ids = [
            d["id"] for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"
        ]
        assert d6_ids == []
        # No `plan_ledger_no_claim` judgment point either -- this is the
        # top-level self-resolution path (`allow_standalone=True`), not the
        # `is_plan_input` ledger read inside `resolve_lineage`, which keeps
        # its own separate fail-loud-then-judgment-point contract.
        assert lineage["plan_ledger_no_claim"] is None

    def test_single_and_multi_claim_paths_carry_standalone_reason_none(self, tmp_path, monkeypatch):
        """Regression: the single-held-claim and multi-held-claim paths are
        UNCHANGED by the standalone fix -- both still resolve a real
        predecessor and both now also carry the new
        `standalone_no_predecessor_reason` key, always `None` when a
        predecessor genuinely resolved."""
        _init_repo(tmp_path)
        single = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-single-held.md",
            ["deliverable_id: DEL-9", "handoff_id: hnd-single-1a2b52"],
        )
        self._seed_handoff_claim(tmp_path, "sid-single-held", single.name)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-single-held")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == str(single.relative_to(tmp_path))
        assert lineage["standalone_no_predecessor_reason"] is None

    def test_two_held_claims_produce_primary_and_additional_predecessor(self, tmp_path, monkeypatch):
        """AC-1 (2026-07-29 break-class fix): a session holding 2 held
        handoff claims produces a brief whose lineage names one primary
        `predecessor` and one entry in `additional_predecessors`, and whose
        directive list contains 2 d6 directives -- no ValueError (this used
        to hard-fail here as "ambiguous"). `claimed_at` is seeded
        DISAGREEING with basename sort order, to prove the earliest-CLAIMED
        (not earliest-basename) entry becomes primary."""
        _init_repo(tmp_path)
        first_claimed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-21-second-alpha.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-first-claimed-1a2b46"],
        )
        second_claimed = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-first-alpha.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-second-claimed-1a2b50"],
        )
        self._seed_handoff_claim(
            tmp_path, "sid-multi", first_claimed.name, claimed_at="2026-07-20T09:00:00Z"
        )
        self._seed_handoff_claim(
            tmp_path, "sid-multi", second_claimed.name, claimed_at="2026-07-20T10:00:00Z"
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-multi")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] == str(first_claimed.relative_to(tmp_path))
        assert lineage["additional_predecessors"] == [str(second_claimed.relative_to(tmp_path))]

        d6s = [
            d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"
        ]
        assert {d["id"] for d in d6s} == {"d6", "d6-2"}
        args_by_id = {d["id"]: d["args"][0] for d in d6s}
        assert args_by_id["d6"] == lineage["predecessor"]
        assert args_by_id["d6-2"] == lineage["additional_predecessors"][0]

    def test_additional_predecessor_resolves_through_archive_fallback(self, tmp_path, monkeypatch):
        """AC-4: an ADDITIONAL predecessor already swept to
        `archive/handoffs/` resolves through the SAME archive-aware fallback
        the primary predecessor gets (`_resolve_qualified_path_or_raise`) --
        never dropped, never raised."""
        _init_repo(tmp_path)
        primary = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-primary.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-primary-1a2b4e"],
        )
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        archived_extra = _write_artifact(
            archived_dir / "2026-07-19-archived-extra.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-extra-1a2b45"],
        )
        self._seed_handoff_claim(
            tmp_path, "sid-multi", primary.name, claimed_at="2026-07-20T09:00:00Z"
        )
        self._seed_handoff_claim(
            tmp_path, "sid-multi", archived_extra.name, claimed_at="2026-07-20T10:00:00Z"
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-multi")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] == str(primary.relative_to(tmp_path))
        assert lineage["additional_predecessors"] == [str(archived_extra.relative_to(tmp_path))]

        d6s = [
            d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"
        ]
        assert len(d6s) == 2

    def test_missing_claimed_at_falls_back_to_basename_sort(self, tmp_path, monkeypatch):
        """No durable `claimed_at` ordering signal for at least one held
        claim (a legacy/hand-seeded claim dir) -- falls back to the
        pre-existing `sorted()`-by-basename order rather than raising or
        picking arbitrarily from an unordered set."""
        _init_repo(tmp_path)
        earlier_by_name = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-aaa.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-a-1a2b40"],
        )
        later_by_name = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-bbb.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-b-1a2b43"],
        )
        self._seed_handoff_claim(tmp_path, "sid-multi", later_by_name.name)
        self._seed_handoff_claim(tmp_path, "sid-multi", earlier_by_name.name)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-multi")

        decision = ba.brief("handoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["predecessor"] == str(earlier_by_name.relative_to(tmp_path))
        assert lineage["additional_predecessors"] == [str(later_by_name.relative_to(tmp_path))]

    def test_no_resolvable_session_id_raises(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        with pytest.raises(ValueError, match="no current session id"):
            ba.brief("handoff", "", repo_root=tmp_path)

    def test_spinoff_kind_does_not_self_resolve_and_stays_mandatory(self, tmp_path):
        """kind=spinoff keeps requiring a truthy artifact_path -- self-
        resolution is a handoff-only addition; an empty artifact_path for
        spinoff must NOT attempt claim-ledger self-resolution and must
        instead behave exactly like the existing bare/empty-path convention
        (empty lineage, no exception raised by this chunk's own addition)."""
        decision = ba.brief("spinoff", "", repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["origin_handoff"] is None


class TestClaimedPlanDeliverableIdCarry:
    """C3 (docs/plans/2026-08-01-deliverable-id-carry-onto-executing-handoff.md):
    pins the end-to-end join `resolve_lineage` (C1) wires between a session's
    claimed plan and its executing handoff -- AC7 (the load-bearing join),
    AC8 (the `plan-claims/` fallback tier specifically), and AC4 (the
    `DroppedDeliverableJoinError` fail-loud guard survives relocation into
    `coordinator_core.ops.deliverable_carry`)."""

    @staticmethod
    def _seed_plan_claim(repo_root: Path, session_id: str, plan_slug: str) -> None:
        """Seed ONLY the `plan-claims/<slug>/session_id` ledger entry -- no
        `session-shape.json` write at all -- via the SAME `plan_claim_dir`
        helper production uses (`coordinator_core.ops.fleet._common`), so an
        AC8 fixture can never hand-roll a claim-dir layout that drifts from
        the real one. Mirrors `TestSelfResolutionFromClaimLedger.
        _seed_handoff_claim`'s pattern for the handoff-claims sibling ledger.
        """
        common_dir = repo_root / ".git"
        claim_dir = plan_claim_dir(common_dir, Path(f"{plan_slug}.md"))
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / "session_id").write_text(session_id, encoding="utf-8")

    def test_ac7_authored_handoff_carries_claimed_plans_deliverable_id_value(
        self, tmp_path, monkeypatch
    ):
        """AC7, the load-bearing assertion: a session holding a plan claim
        for a plan whose frontmatter carries a KNOWN deliverable_id produces
        an authored handoff carrying THAT id -- not one derived from the
        handoff's own mint-slug. Asserts on the id VALUE (not merely on
        `discovery`), which is the regression this test must catch if the
        wire from `resolve_claimed_plan_path` into `resolve_lineage` is ever
        cut."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-01-ac7-claimed-plan"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: dlv-ac7-claimed-plan-abc123"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac7")
        assert session_claims.claim_plan(plan_slug, cwd=str(tmp_path)) is True

        decision = ba.brief(
            "handoff", "fresh-handoff-ac7", repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "dlv-ac7-claimed-plan-abc123"
        assert lineage["discovery"] == "plan"

    def test_ac8_fallback_tier_resolves_when_session_shape_json_is_absent(
        self, tmp_path, monkeypatch
    ):
        """AC8: with `session-shape.json` entirely absent (tier (a) never
        wrote, e.g. the documented best-effort write raised/skipped), the
        `plan-claims/*/session_id` ledger scan (tier (b)) still resolves the
        plan and the carry still happens."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-01-ac8-shape-absent"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: dlv-ac8-shape-absent-xyz789"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac8-absent")
        self._seed_plan_claim(tmp_path, "sid-ac8-absent", plan_slug)
        assert not (tmp_path / ".git" / "coordinator-sessions" / "sid-ac8-absent").exists()

        decision = ba.brief(
            "handoff", "fresh-handoff-ac8-absent", repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "dlv-ac8-shape-absent-xyz789"
        assert lineage["discovery"] == "plan"

    def test_ac8_fallback_tier_resolves_when_session_shape_json_lacks_plan_key(
        self, tmp_path, monkeypatch
    ):
        """AC8, second half: `session-shape.json` PRESENT but missing its
        `plan` key (e.g. only `pickup` was ever written) -- the ledger scan
        still resolves the plan rather than trusting the shape file's
        silence as "nothing claimed"."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-01-ac8-shape-no-plan-key"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: dlv-ac8-no-plan-key-def456"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac8-no-plan-key")
        self._seed_plan_claim(tmp_path, "sid-ac8-no-plan-key", plan_slug)
        assert session_shape.session_shape_set(
            "sid-ac8-no-plan-key", {"pickup": {"deliverable_id": "dlv-decoy-should-not-be-used"}}, str(tmp_path)
        )

        decision = ba.brief(
            "handoff", "fresh-handoff-ac8-no-plan-key", repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "dlv-ac8-no-plan-key-def456"
        assert lineage["discovery"] == "plan"

    def test_ac4_dropped_join_survives_relocation_absent_field(self, tmp_path, monkeypatch):
        """AC4: under an active claimed plan whose `deliverable_id` field is
        ABSENT entirely, authoring raises `DroppedDeliverableJoinError`
        rather than silently minting from slug -- proving the guard was
        carried across the new `resolve_lineage` call path (C1) rather than
        bypassed."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-01-ac4-absent-field"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md", ["title: AC4 Plan"]
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac4-absent")
        assert session_claims.claim_plan(plan_slug, cwd=str(tmp_path)) is True

        with pytest.raises(DroppedDeliverableJoinError):
            ba.brief("handoff", "fresh-handoff-ac4-absent", repo_root=tmp_path)

    def test_ac4_dropped_join_survives_relocation_literal_null(self, tmp_path, monkeypatch):
        """AC4, the literal-`null` variant: `read_frontmatter_field` normalizes
        a literal `null` scalar to the empty string (indistinguishable from
        absent at that layer, per `deliverable_carry`'s own docstring) -- the
        guard must fire identically."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-01-ac4-literal-null"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md", ["deliverable_id: null"]
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-ac4-null")
        assert session_claims.claim_plan(plan_slug, cwd=str(tmp_path)) is True

        with pytest.raises(DroppedDeliverableJoinError):
            ba.brief("handoff", "fresh-handoff-ac4-null", repo_root=tmp_path)


class TestUnrecognizedKind:
    def test_unrecognized_kind_raises_value_error(self, tmp_path):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        with pytest.raises(ValueError):
            ba.brief("bogus-kind", str(artifact), repo_root=tmp_path)

    def test_d2_lint_frontmatter_args_use_file_flag_not_bare_positional(self, tmp_path):
        """Regression for the live failure: `lint-frontmatter.py`'s CLI
        trampoline (schema_validate._parse_argv) rejects a bare positional
        path with "unknown argument" (rc=2) -- it requires `--file <path>`.
        `_build_directives` must emit the flagged shape, and (2026-08-03
        break-class fix) must name d1's COMPUTED `--out` -- the artifact this
        run authors -- not `artifact_path`, the INPUT it was handed. Linting
        the input made d2 validate a file the run never wrote, so a
        pre-existing defect in a predecessor baton rolled the whole assembly
        back. d1's `--out` is computed statically at brief() time, so no
        runtime result-threading is involved."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        d2 = next(d for d in decision["directives"] if d["id"] == "d2")
        d1_out = next(a.split("=", 1)[1] for a in d1["args"] if a.startswith("--out="))
        assert d2["cli"] == "lint-frontmatter"
        assert d2["args"] == ["--file", d1_out]
        assert d2["args"][1] != str(artifact)


# ---------------------------------------------------------------------------
# (g) d1's --out/--title threading -- 2026-07-26 break-class fix regression.
# Live failure: d1 omitted both --out and --title, so `coordinator-doc-new`
# scaffolded a placeholder-titled file at a placeholder-derived path instead
# of the caller-supplied artifact_path, and d2's lint then failed with
# "file not found" against the path d1 never created.
#
# 2026-07-27: for kind="handoff" specifically, d1's `--out` is no longer the
# caller-supplied `artifact_path` verbatim -- `artifact_path` is the INPUT
# lineage source (plan or predecessor handoff), and echoing it into `--out`
# destroyed it the moment d1 fired (`coordinator-doc-new`'s `--out` write is
# an unconditional overwrite). See `TestD1OutNeverEqualsInputArtifact` below
# for the destructive-collision regression and
# `TestNoDirectiveWritesOverInputBackstop` for the general guard. This
# class now asserts d1's `--out` is a COMPUTED, DIFFERENT path following
# the `state/handoffs/<date>-<slug>.md` convention.
# ---------------------------------------------------------------------------


class TestD1OutAndTitleThreading:
    def test_d1_args_contain_computed_out_different_from_artifact_path(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        assert out_value != str(artifact)
        import re

        assert re.search(r"state[/\\]handoffs[/\\]\d{4}-\d{2}-\d{2}-h1\.md$", out_value)

    def test_d1_args_contain_title_when_supplied(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief(
            "handoff", str(artifact), repo_root=tmp_path, title="my one-line title"
        ).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert "--title=my one-line title" in d1["args"]

    def test_d1_args_omit_title_entirely_when_not_supplied(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert not any(arg.startswith("--title") for arg in d1["args"])

    def test_d1_existing_type_and_deliverable_id_args_unchanged(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert "--type=handoff" in d1["args"]
        assert "--deliverable-id=DEL-1" in d1["args"]


# ---------------------------------------------------------------------------
# (g2) 2026-08-05 -- `brief()`'s `explicit_deliverable_id` param, the missing
# caller for `resolve_lineage`'s own `explicit_deliverable_id` kwarg. Spinoff
# accepts and carries it (never re-minted); kind="handoff" rejects it loud
# rather than silently ignoring it (its own claimed-plan -> predecessor ->
# mint cascade already owns that tier).
# ---------------------------------------------------------------------------


class TestBriefExplicitDeliverableIdThreading:
    def test_spinoff_explicit_deliverable_id_lands_in_lineage_and_d1_args(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["deliverable_id: DEL-PROGENITOR", "kind: session-handoff"],
        )
        decision = ba.brief(
            "spinoff",
            str(artifact),
            repo_root=tmp_path,
            explicit_deliverable_id="DEL-EM-SUPPLIED",
        ).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] == "DEL-EM-SUPPLIED"
        assert lineage["discovery"] == "explicit"
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert "--deliverable-id=DEL-EM-SUPPLIED" in d1["args"]

    def test_spinoff_without_flag_still_mints_and_does_not_inherit(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["deliverable_id: DEL-PROGENITOR", "kind: session-handoff"],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["discovery"] == "mint"
        assert lineage["deliverable_id"] is None
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        assert "--deliverable-id=" in d1["args"]
        assert not any(
            arg.startswith("--deliverable-id=DEL-PROGENITOR") for arg in d1["args"]
        )

    def test_handoff_kind_rejects_explicit_deliverable_id_loudly(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        with pytest.raises(ValueError, match="spinoff-only"):
            ba.brief(
                "handoff",
                str(artifact),
                repo_root=tmp_path,
                explicit_deliverable_id="DEL-SHOULD-NOT-LAND",
            )

    def test_handoff_kind_unaffected_when_flag_absent(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["deliverable_id"] == "DEL-1"
        assert lineage["discovery"] == "artifact"


# ---------------------------------------------------------------------------
# (h) 2026-07-27 destructive-collision fix regression -- bug backlog
# `2026-07-27-baton-assemble-handoff-brief-computes-a-fe36a5dea88e.yaml`.
# Live incident: `baton-assemble brief handoff docs/plans/2026-07-26-priority-
# ledger.md` (the plan->execute execution-handoff trigger) came back with
# d1's `--out` set to that SAME plan path -- firing d1 verbatim would have
# scaffolded a blank handoff over a just-PM-authorized plan carrying
# `execution_authorized_*` stamps.
# ---------------------------------------------------------------------------


class TestD1OutNeverEqualsInputArtifact:
    def test_plan_path_input_produces_d1_out_different_from_the_plan(self, tmp_path):
        """Reproduces the exact live incident shape: a PLAN artifact (not a
        handoff) fed to `brief("handoff", ...)`, as the plan->execute
        execution-handoff trigger legitimately does (mirrors
        TestD5ClaimPlanArgs's own plan-path fixture convention)."""
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-priority-ledger.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        assert out_value != str(plan)
        assert out_value != "docs/plans/2026-07-26-priority-ledger.md"

    def test_predecessor_handoff_input_produces_d1_out_different_from_predecessor(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-earlier-session.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        assert out_value != str(artifact)

    def test_lineage_carries_output_path_distinct_from_artifact_path(self, tmp_path):
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-priority-ledger.md",
            ['deliverable_id: DEL-1'],
        )
        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["output_path"] != lineage["artifact_path"]
        assert lineage["artifact_path"] == str(plan)


class TestNoDirectiveWritesOverInputBackstop:
    """Unit-level coverage of `_assert_no_directive_writes_over_input` --
    the general backstop, independent of `_compute_fresh_output_path`'s
    own correctness. Proves the guard actually fires against an EXISTING
    input, stays silent against a not-yet-existing one (the bare-slug mint
    convention's legitimate `output_path == artifact_path` shape), and
    does not false-positive on legitimate same-path READ directives (d2's
    `--file <input>` lint target)."""

    def test_raises_when_a_directive_out_flag_equals_an_existing_input_path(self, tmp_path):
        real = tmp_path / "same" / "path.md"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("existing content\n", encoding="utf-8")
        directives = [
            {"id": "dX", "cli": "some-cli", "args": [f"--out={real}"], "depends_on": None},
        ]
        with pytest.raises(ValueError, match=re.escape(str(real))):
            ba._assert_no_directive_writes_over_input(directives, str(real), tmp_path)

    def test_no_raise_when_input_path_does_not_exist_on_disk(self, tmp_path):
        """The bare-slug mint case: `output_path` legitimately equals
        `artifact_path` because there is no PRE-EXISTING file at that path
        to destroy -- the guard must stay silent here."""
        not_yet_real = str(tmp_path / "state" / "handoffs" / "2026-07-27-fresh-slug.md")
        directives = [
            {"id": "dX", "cli": "some-cli", "args": [f"--out={not_yet_real}"], "depends_on": None},
        ]
        ba._assert_no_directive_writes_over_input(directives, not_yet_real, tmp_path)

    def test_no_raise_when_out_flag_differs_from_input_path(self, tmp_path):
        real = tmp_path / "same" / "path.md"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("existing content\n", encoding="utf-8")
        directives = [
            {"id": "dX", "cli": "some-cli", "args": ["--out=different/path.md"], "depends_on": None},
        ]
        ba._assert_no_directive_writes_over_input(directives, str(real), tmp_path)

    def test_no_raise_on_bare_positional_match_read_only_directive(self, tmp_path):
        """d2's `--file <input>` shape: a bare positional arg equal to the
        input path is a legitimate READ target, not a write collision --
        the guard must not false-positive on it, even against an existing
        input."""
        real = tmp_path / "same" / "path.md"
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("existing content\n", encoding="utf-8")
        directives = [
            {"id": "d2", "cli": "lint-frontmatter", "args": ["--file", str(real)], "depends_on": ["d1"]},
        ]
        ba._assert_no_directive_writes_over_input(directives, str(real), tmp_path)

    def test_no_raise_when_input_path_is_empty(self, tmp_path):
        directives = [
            {"id": "dX", "cli": "some-cli", "args": ["--out="], "depends_on": None},
        ]
        ba._assert_no_directive_writes_over_input(directives, "", tmp_path)

    def test_real_brief_call_never_raises_for_a_plan_input(self, tmp_path):
        """End-to-end: the fixed `brief()` composes `_build_directives` +
        the backstop guard without tripping it for the exact live-incident
        input shape."""
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-priority-ledger.md",
            ['deliverable_id: DEL-1'],
        )
        # Must not raise.
        ba.brief("handoff", str(plan), repo_root=tmp_path)


# ---------------------------------------------------------------------------
# 2026-07-27 follow-up regression: same-day handoff chains. `f560a361`'s
# `_compute_fresh_output_path` re-dates to TODAY, which is a no-op (and
# therefore a collision with the input itself) when the input is ALREADY
# dated today -- routine for this fleet's multi-handoff-per-day chains, not
# an edge case. Disambiguation follows the `<date>_<HHMMSS>_<slug>.md`
# convention already used fleet-wide for same-day chains (see
# `handoff_author_fork._fork_handoff_filename` and the many
# `state/handoffs/<date>_<HHMMSS>_*.md` files already on disk) rather than
# inventing a new scheme.
# ---------------------------------------------------------------------------


class TestSameDayChainOutputPathDisambiguation:
    def test_today_dated_input_produces_a_disambiguated_out_not_a_collision(self, tmp_path):
        """Reproduces the exact regression shape: `brief handoff
        state/handoffs/<today>-<slug>.md` where the input already exists on
        disk, today-dated. Must NOT raise (the plain re-date collides with
        the input), and the computed `--out` must be neither the input's
        own path nor any other existing file."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / f"{today}-priority-ledger-execute.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        # Must not raise -- this is the exact collision the backstop
        # correctly refused before the derivation was fixed.
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        assert out_value != str(artifact)
        assert not (tmp_path / out_value).exists()
        # Disambiguated via the established `<date>_<HHMMSS>_<slug>.md`
        # same-day-chain convention.
        assert re.search(
            rf"state[/\\]handoffs[/\\]{today}_\d{{6}}_priority-ledger-execute\.md$",
            out_value,
        )

    def test_disambiguated_candidate_also_checked_for_a_different_existing_file(self, tmp_path):
        """The disambiguation must not silently overwrite a DIFFERENT
        existing handoff either -- if even the HHMMSS-qualified candidate
        happens to already exist on disk, a further numeric suffix is
        appended and re-checked."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / f"{today}-chain-slug.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        time_str = now.strftime("%H%M%S")
        # Pre-seed the FIRST disambiguation candidate so the function must
        # keep looking.
        _write_artifact(
            tmp_path / "state" / "handoffs" / f"{today}_{time_str}_chain-slug.md",
            ["deliverable_id: DEL-9"],
        )
        out_value = ba._compute_fresh_output_path(str(artifact), tmp_path)
        assert out_value != str(artifact)
        assert not (tmp_path / out_value).exists()

    def test_root_none_skips_the_existence_check_and_stays_idempotent(self, tmp_path):
        """`root=None` (the `_build_directives` defensive-fallback shape,
        and direct unit tests constructing a `lineage` dict by hand) must
        keep the pre-collision-check idempotent behaviour unchanged -- no
        disk access at all."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        out_value = ba._compute_fresh_output_path(f"state/handoffs/{today}-x.md")
        assert out_value == f"state/handoffs/{today}-x.md" or out_value == str(
            Path("state") / "handoffs" / f"{today}-x.md"
        )

    def test_brief_end_to_end_same_day_chain_succeeds(self, tmp_path):
        """Brief-level reproduction matching the dispatch brief's literal
        repro command shape: a same-day chain must work end to end."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / f"{today}-priority-ledger-execute.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        # Must not raise.
        ba.brief("handoff", str(artifact), repo_root=tmp_path)


class TestStandaloneHandoffSlugFromTitle:
    """2026-08-04 break-class fix, Defect B: with no `artifact_path` to
    derive a slug from (the standalone-handoff mint case),
    `_compute_fresh_output_path` used to compute a dangling
    `state/handoffs/<date>-.md` (`Path("").stem == ""`). It now derives the
    slug from the caller-supplied `title`, via the SAME house slugifier
    `coordinator-doc-new` itself uses (ported, importable copy at
    `coordinator_core.ops.ceremony.completion_entry._slug_from_title`)."""

    def test_empty_artifact_path_with_title_derives_slug_from_title(self, tmp_path):
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        out_value = ba._compute_fresh_output_path(
            "", tmp_path, title="Some Standalone Title!"
        )
        assert out_value == str(
            Path("state") / "handoffs" / f"{today}-some-standalone-title.md"
        )

    def test_empty_artifact_path_and_no_title_falls_back_to_untitled(self, tmp_path):
        """Defect B's 'also handle' case: no title AND no artifact path must
        not produce a dangling `<date>-.md` -- falls back to a deterministic,
        non-empty slug instead."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        out_value = ba._compute_fresh_output_path("", tmp_path)
        assert out_value == str(Path("state") / "handoffs" / f"{today}-untitled.md")
        assert not out_value.endswith("-.md")

    def test_nonempty_artifact_path_ignores_title(self, tmp_path):
        """Non-regression: when `artifact_path` DOES resolve to a stem, the
        slug still derives from it alone -- `title` is only ever consulted
        in the empty-artifact_path case."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        out_value = ba._compute_fresh_output_path(
            str(artifact), tmp_path, title="Totally Different Title"
        )
        assert out_value == str(Path("state") / "handoffs" / f"{today}-some-plan.md")

    def test_brief_end_to_end_standalone_handoff_succeeds_with_slugified_title(
        self, tmp_path, monkeypatch
    ):
        """Brief-level reproduction of the live break: a standalone handoff
        (no artifact_path, title supplied) must produce a decision object
        whose d1 `--out` is a slugified, date-prefixed path -- never a
        dangling `<date>-.md`."""
        import datetime

        _init_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-standalone-slug")
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        decision = ba.brief(
            "handoff", "", repo_root=tmp_path, title="stamp the scope guard"
        ).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        assert out_value == f"state/handoffs/{today}-stamp-the-scope-guard.md"


# ---------------------------------------------------------------------------
# 2026-07-29 follow-up: successor-derivation archive-collision fix. Evidence:
# example-doctrine-repo state/handoffs/2026-07-29_175200_confinement-band-split-plan-
# awaiting-review.md § Session Ledger -- `baton-assemble apply handoff`'s
# first brief timestamped the predecessor's basename (a live same-day
# collision, correctly disambiguated); a concurrent session then archived
# the predecessor mid-flight, and the RE-BRIEF stopped timestamping because
# `_compute_fresh_output_path`'s existence check only looked at the LIVE
# `state/handoffs/` directory -- producing a plain candidate that collided
# with the now-archived record. `handoff_creation_guard.assert_no_archived_twin`
# correctly refused it (twice), but only after the assembler had already
# handed it an unusable path.
# ---------------------------------------------------------------------------


# Review: coordinator:code-reviewer (Finding 4) — this class's 5 fixture
# `handoff_id` sites were remapped from the pre-sweep `HID-{slug}` shape to
# `hnd-{slug}-<hex>` for consistency with the rest of this file, even though
# these particular fixtures never reach `validate_frontmatter`.
class TestSuccessorDerivationSurvivesPredecessorArchival:
    def test_archived_only_same_day_collision_is_disambiguated(self, tmp_path):
        """The predecessor exists ONLY under archive/handoffs/ (same basename,
        dated today) -- nothing live. Before the fix, `_exists` never looked
        there, so the plain `<date>-<slug>.md` candidate was returned
        unchanged and collided with the archived record the moment
        `coordinator-doc-new` fired. After the fix, this must disambiguate
        exactly like the live-collision case."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        slug = "earlier-session"
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        _write_artifact(
            archived_dir / f"{today}-{slug}.md",
            ["deliverable_id: DEL-1", f"handoff_id: hnd-{slug}-1a2b3c", 'predecessor: "none"'],
        )
        # Nothing exists at the live location -- reproduces "predecessor was
        # archived mid-flight" exactly (the caller still names the live path).
        live_path = tmp_path / "state" / "handoffs" / f"{today}-{slug}.md"

        out_value = ba._compute_fresh_output_path(str(live_path), tmp_path)
        assert not (tmp_path / out_value).exists()
        assert out_value != f"state/handoffs/{today}-{slug}.md"
        assert re.search(
            rf"state[/\\]handoffs[/\\]{today}_\d{{6}}_{slug}\.md$", out_value
        )

    def test_derivation_rule_is_identical_whether_predecessor_is_live_or_archived(
        self, tmp_path
    ):
        """Stability requirement: swap the SAME predecessor between a live
        location and an archived one and confirm both derivations follow the
        identical disambiguation rule (a `<date>_<HHMMSS>_<slug>.md` shape),
        not two different rules that happen to diverge only because one
        input shape was missed by the existence check."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        slug = "chain-slug"

        live_repo = tmp_path / "live-case"
        live_predecessor = _write_artifact(
            live_repo / "state" / "handoffs" / f"{today}-{slug}.md",
            ["deliverable_id: DEL-1", f"handoff_id: hnd-{slug}-1a2b3c", 'predecessor: "none"'],
        )
        live_out = ba._compute_fresh_output_path(str(live_predecessor), live_repo)

        archived_repo = tmp_path / "archived-case"
        archived_dir = archived_repo / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        _write_artifact(
            archived_dir / f"{today}-{slug}.md",
            ["deliverable_id: DEL-1", f"handoff_id: hnd-{slug}-1a2b3c", 'predecessor: "none"'],
        )
        archived_live_named_path = archived_repo / "state" / "handoffs" / f"{today}-{slug}.md"
        archived_out = ba._compute_fresh_output_path(
            str(archived_live_named_path), archived_repo
        )

        pattern = rf"state[/\\]handoffs[/\\]{today}_\d{{6}}_{slug}\.md$"
        assert re.search(pattern, live_out), live_out
        assert re.search(pattern, archived_out), archived_out

    def test_brief_end_to_end_archived_predecessor_same_day_does_not_collide(self, tmp_path):
        """Full `brief("handoff", ...)` reproduction of the reported
        scenario: the predecessor this session opened with has since been
        swept to archive/handoffs/, same-day-dated. Must not raise, and the
        computed d1 `--out` must not equal the archived predecessor's own
        basename."""
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        slug = "confinement-band-split"
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        _write_artifact(
            archived_dir / f"{today}-{slug}.md",
            ["deliverable_id: DEL-1", f"handoff_id: hnd-{slug}-1a2b3c", 'predecessor: "none"'],
        )
        live_named_path = tmp_path / "state" / "handoffs" / f"{today}-{slug}.md"

        decision = ba.brief("handoff", str(live_named_path), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        assert out_value != f"state/handoffs/{today}-{slug}.md"
        assert not (tmp_path / out_value).exists()

    def test_archived_collision_end_to_end_via_coordinator_doc_new_does_not_trip_guard(
        self, tmp_path
    ):
        """Integration proof: firing the ACTUAL `coordinator-doc-new` CLI
        with brief()'s computed `--out` for an archived, same-day-dated
        predecessor succeeds -- `handoff_creation_guard.assert_no_archived_twin`
        (coordinator/tests/test_coordinator_doc_new_handoff_archived_twin_guard.py)
        is never tripped, because the derivation no longer hands it a
        colliding path. The guard itself is untouched and stays covered by
        its own dedicated test file -- this only proves this module's output
        is now guard-safe."""
        import datetime
        import os
        import subprocess as sp
        import sys

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        slug = "guard-safe-check"
        _init_repo(tmp_path)
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        _write_artifact(
            archived_dir / f"{today}-{slug}.md",
            ["deliverable_id: DEL-1", f"handoff_id: hnd-{slug}-1a2b3c", 'predecessor: "none"'],
        )
        live_named_path = tmp_path / "state" / "handoffs" / f"{today}-{slug}.md"

        decision = ba.brief("handoff", str(live_named_path), repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]

        repo_root_here = Path(__file__).resolve().parents[1]
        bin_dir = repo_root_here / "coordinator" / "bin"
        cli = bin_dir / "coordinator-doc-new"
        env = dict(os.environ)
        env.pop("COORDINATOR_SESSION_ID", None)
        env.pop("CLAUDE_SESSION_ID", None)
        env["CLAUDE_CODE_SESSION_ID"] = "test-session-successor-derivation"
        # Rung-1-pin CLAUDE_KLABAUTER_ROOT (cc_invoke._resolve_claude_klabauter_root's cheapest,
        # highest-precedence rung): this module's own autouse
        # `_quarantine_real_home` fixture (conftest.py) redirects HOME to a
        # per-test throwaway dir for every test here, so the CLI subprocess's
        # own machine-local/settings-home ladder can never resolve
        # repos.claude_klabauter on its own -- it would otherwise fail loud
        # with "repos.claude_klabauter is not set" regardless of the real
        # machine's actual registration.
        env["CLAUDE_KLABAUTER_ROOT"] = str(repo_root_here)
        no_console = {"creationflags": getattr(sp, "CREATE_NO_WINDOW", 0)}
        result = sp.run(
            [sys.executable, str(cli), "--type", "handoff", "--out", out_value],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            **no_console,
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / out_value).is_file()


class TestArchiveScanIsMemoizedAndDegradesOnUnreadableSubdir:
    """2026-07-29 review finding 2 (P2): the non-colliding common case (the
    live `_compute_fresh_output_path` candidate is fresh) is exactly the
    case where `Path.exists()` returns False and the archive fallback used
    to run a full recursive `rglob` over all three archive subtrees, up to
    3x per call via the disambiguation ladder. Fixed by memoizing the
    archive basename index for the lifetime of one
    `_compute_fresh_output_path` invocation and by degrading an unreadable
    archive subdir to "treat as clear" instead of propagating an OSError."""

    def test_walk_count_is_one_not_per_probe(self, tmp_path, monkeypatch):
        # Populate all three archive subdirs so a naive per-probe walk would
        # visit all of them repeatedly; assert `rglob` is invoked exactly
        # once per archive subdir (one walk total), not once per `_exists`
        # probe in the disambiguation ladder.
        for subdir in ("cross-repo/archive", "archive/handoffs", "archive/completed"):
            (tmp_path / subdir).mkdir(parents=True)

        call_count = {"n": 0}
        real_rglob = Path.rglob

        def counting_rglob(self, pattern):
            call_count["n"] += 1
            return real_rglob(self, pattern)

        monkeypatch.setattr(Path, "rglob", counting_rglob)

        # No live candidate exists anywhere, so every probe in the ladder
        # falls through to the archive fallback -- the shape that used to
        # pay the walk cost repeatedly.
        out_value = ba._compute_fresh_output_path(
            str(tmp_path / "state" / "handoffs" / "2026-01-01-never-collides.md"),
            tmp_path,
        )
        assert out_value  # sanity: derivation still succeeds

        # One `rglob("*")` call per archive subdir (3), regardless of how
        # many `_exists` probes the ladder ran -- NOT 3x that.
        assert call_count["n"] == 3, call_count["n"]

    def test_unreadable_archive_subdir_degrades_to_treat_as_clear(self, tmp_path, monkeypatch):
        # Simulate a permission-denied archive subdirectory: `is_dir()`
        # reports True (so the fallback attempts to walk it) but `rglob`
        # raises OSError. Pre-fix behaviour was a bare `Path.exists()`
        # check that never raised on this shape at all; post-fix must match
        # that fail-open posture rather than crashing the derivation.
        import datetime

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        archive_dir = tmp_path / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)

        def raising_rglob(self, pattern):
            raise OSError("permission denied (simulated)")

        monkeypatch.setattr(Path, "rglob", raising_rglob)

        out_value = ba._compute_fresh_output_path(
            str(tmp_path / "state" / "handoffs" / "2026-01-01-unreadable-archive.md"),
            tmp_path,
        )
        assert out_value == f"state/handoffs/{today}-unreadable-archive.md"


class TestBareSlugArtifactPathNormalization:
    """Regression for the reproduced live break: `baton-assemble apply
    spinoff windows-host-validation-review-assemble-seam` scaffolded an
    extensionless file literally named `windows-host-validation-review-
    assemble-seam` at the repo ROOT and committed it there, instead of
    `state/handoffs/<date>-<slug>.md`. Covers `_normalize_artifact_path`
    both directly and through `brief()`'s full directive-emission surface,
    for BOTH kinds (kind-agnostic normalization point)."""

    def test_bare_slug_normalizes_to_state_handoffs_dated_md(self):
        normalized = ba._normalize_artifact_path("some-bare-slug")
        assert normalized.startswith("state" + "/" + "handoffs" + "/") or normalized.startswith(
            "state" + "\\" + "handoffs" + "\\"
        )
        assert normalized.endswith("-some-bare-slug.md")
        # date component is a real YYYY-MM-DD stamp, not a placeholder
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2}-some-bare-slug\.md$", normalized)

    def test_already_qualified_path_passes_through_byte_identical(self):
        qualified = "state/handoffs/2026-07-26-foo.md"
        assert ba._normalize_artifact_path(qualified) == qualified

    def test_path_shaped_but_no_extension_passes_through_unchanged(self):
        # Contains a separator -- ALREADY looks like a path (per the
        # normalization contract: "contains a separator, or ends `.md`"),
        # even though it lacks the `.md` suffix. Treating this as a bare
        # slug would double-nest an already-directed path under
        # state/handoffs/, which is wrong for any caller that already
        # named a directory.
        already_directed = "docs/plans/some-slug"
        assert ba._normalize_artifact_path(already_directed) == already_directed

    def test_md_suffix_with_no_directory_passes_through_unchanged(self):
        # No separator, but DOES end `.md` -- per the same contract this
        # also counts as "already looks like a path" (a bare filename the
        # caller explicitly extensioned) and must not be re-homed under
        # state/handoffs/ or re-dated.
        bare_filename = "some-slug.md"
        assert ba._normalize_artifact_path(bare_filename) == bare_filename

    def test_empty_string_passes_through_unchanged(self):
        assert ba._normalize_artifact_path("") == ""

    @pytest.mark.parametrize("kind", ["handoff", "spinoff"])
    def test_brief_d1_out_and_d2_file_use_the_same_normalized_value(self, kind, tmp_path):
        """The desync guard: d1's `--out` and the path d2 lints must be the
        SAME value when brief() is handed a bare slug -- both are sourced
        from `lineage["artifact_path"]`, so normalizing at that single point
        keeps them synchronized."""
        decision = ba.brief(kind, "a-bare-slug", repo_root=tmp_path).decision_object
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        d2 = next(d for d in decision["directives"] if d["id"] == "d2")

        out_arg = next(a for a in d1["args"] if a.startswith("--out="))
        out_value = out_arg[len("--out="):]
        file_flag_index = d2["args"].index("--file")
        file_value = d2["args"][file_flag_index + 1]

        assert out_value == file_value
        assert out_value != "a-bare-slug"  # actually normalized, not a passthrough
        assert out_value.endswith("-a-bare-slug.md")

    def test_brief_envelope_artifact_path_matches_lineage_artifact_path(self, tmp_path):
        decision = ba.brief("handoff", "a-bare-slug", repo_root=tmp_path).decision_object
        assert decision["artifact"]["path"] == decision["artifact"]["lineage"]["artifact_path"]

    def test_brief_with_already_qualified_path_is_unaffected(self, tmp_path):
        """Non-regression: a fully-qualified path (the pre-existing calling
        convention every other test in this file uses) must be completely
        unaffected by the normalization added for the bare-slug case."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        assert decision["artifact"]["path"] == str(artifact)
        assert decision["artifact"]["lineage"]["artifact_path"] == str(artifact)


class TestD5ClaimPlanArgs:
    """Authoring a handoff/spinoff is a RELINQUISHMENT of the plan claim, not
    an acquisition: d5 must name the ``release-artifact`` subcommand (class
    ``plan``) and hand it a bare slug (no directory, no ``.md``), never the
    raw ``artifact_path``. See coordinator_core.session.claims.release_artifact's
    own boundary/no-op-when-not-holder contract for the receiving side."""

    def test_d5_releases_the_plan_claim_rather_than_claiming_it(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d5 = next(d for d in decision["directives"] if d["id"] == "d5")
        assert d5["args"][0] == "release-artifact"
        assert d5["args"][1] == "plan"

    def test_d5_third_arg_is_bare_slug_not_path(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d5 = next(d for d in decision["directives"] if d["id"] == "d5")
        assert d5["args"] == ["release-artifact", "plan", "2026-07-26-some-plan"]


class TestD5EmissionDiscriminator:
    """d5 (`session-claim-cli release-artifact plan <slug>`) must fire ONLY
    on the handoff path, never on the spinoff path -- same asymmetry as d6's
    own discriminator (TestD6EmissionDiscriminator above). A handoff is a
    RELINQUISHMENT of the plan claim (the successor must find it unclaimed);
    a spinoff is a FORK -- the authoring session keeps executing its own
    plan, and releasing that claim would drop its own live execution lock,
    letting a concurrent session claim the same plan out from under it."""

    def test_d5_emitted_for_handoff(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        ids = {d["id"] for d in decision["directives"]}
        assert "d5" in ids

    def test_d5_not_emitted_for_spinoff_fork_kind(self, tmp_path):
        """A fork must not dispose of its origin's plan claim: the session
        authoring a spinoff is not relinquishing anything -- it forks a
        side-topic while continuing to execute the plan it already holds.
        Releasing that claim here would let a concurrent session claim the
        same plan out from under the still-executing spinoff author."""
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        ids = {d["id"] for d in decision["directives"]}
        clis = {d["cli"] for d in decision["directives"]}
        assert "d5" not in ids
        assert "session-claim-cli" not in clis

    def test_d5_not_emitted_for_standalone_handoff_with_no_plan_artifact(
        self, tmp_path, monkeypatch
    ):
        """2026-08-04 break-class fix: a STANDALONE handoff (no predecessor,
        no plan -- `lineage["artifact_path"]` empty) has no plan claim to
        relinquish, so d5 must be ABSENT entirely, not emitted with an empty
        basename (which `session-claim-cli` correctly rejects, aborting the
        whole mint -- the reproduced live break)."""
        _init_repo(tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-standalone-d5")

        decision = ba.brief(
            "handoff", "", repo_root=tmp_path, title="some standalone title"
        ).decision_object
        lineage = decision["artifact"]["lineage"]
        assert lineage["standalone_no_predecessor_reason"] is not None
        ids = {d["id"] for d in decision["directives"]}
        clis = {d["cli"] for d in decision["directives"]}
        assert "d5" not in ids
        assert "session-claim-cli" not in clis

    def test_d5_still_emitted_with_correct_basename_when_plan_artifact_present(
        self, tmp_path
    ):
        """Non-regression: d5 must still fire, unchanged, whenever there IS a
        plan artifact -- the plan->execute case this directive exists for."""
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d5 = next(d for d in decision["directives"] if d["id"] == "d5")
        assert d5["args"] == ["release-artifact", "plan", "2026-07-26-some-plan"]


class TestBatonNeverClaims:
    """PM-facing contract: authoring a baton (handoff or spinoff) must never
    emit a claim-acquiring subcommand -- a successor session picking up the
    baton and running /execute-plan must find the plan unclaimed. Guards
    against a future re-introduction of a claiming directive anywhere in the
    assembled directive set, not just at d5.

    Two checks, deliberately not merged into one:
      1. A cli-agnostic DENYLIST match on args[0] -- catches a re-introduced
         "claim-plan"/"claim-artifact" call issued through ANY cli name
         (survives a future rename of "session-claim-cli" itself, since it
         never inspects the cli field).
      2. A session-claim-cli-SCOPED ALLOWLIST match -- for any directive
         whose cli IS "session-claim-cli", args[0] must be one of that CLI's
         own documented NON-acquiring subcommands (see session-claim-cli's
         own `_SUBCOMMANDS` docstring list, minus claim-plan/claim-artifact).
         This is the fail-closed half check 1 alone cannot provide: a FUTURE
         subcommand rename (e.g. session-claim-cli grows an "acquire-plan"
         alias for claim-plan) would silently pass check 1 forever, because
         check 1 can only enumerate names it already knows are dangerous.
         Check 2 instead enumerates names already known SAFE, so anything
         new/unrecognized on this specific CLI fails loud until someone
         explicitly re-vets it and adds it to the allowlist -- the guard's
         own stated purpose (surviving future refactors) requires the
         fail-closed direction, not just fail-on-known-bad."""

    CLAIM_ACQUIRING_SUBCOMMANDS = {"claim-plan", "claim-artifact"}

    # session-claim-cli's own subcommand list (its module header comment),
    # minus the two acquiring ones above -- anything on this CLI outside
    # this set is presumptively unvetted, not presumptively safe.
    SESSION_CLAIM_CLI_NON_ACQUIRING_SUBCOMMANDS = {
        "release-artifact",
        "clear-claim-if-dead",
        "is-session-live",
        "list-stale-claim-handoffs",
    }

    def _assert_no_claim_acquiring_directive(self, decision):
        for directive in decision["directives"]:
            args = directive.get("args") or []
            cli = directive.get("cli")
            if args and args[0] in self.CLAIM_ACQUIRING_SUBCOMMANDS:
                raise AssertionError(
                    f"directive {directive['id']!r} emits claim-acquiring "
                    f"subcommand {args[0]!r} -- authoring a baton must "
                    f"release claims, never acquire them"
                )
            if cli == "session-claim-cli" and args:
                if args[0] not in self.SESSION_CLAIM_CLI_NON_ACQUIRING_SUBCOMMANDS:
                    raise AssertionError(
                        f"directive {directive['id']!r} calls session-claim-cli "
                        f"subcommand {args[0]!r}, not on the allowlist of known-"
                        f"non-acquiring subcommands "
                        f"{sorted(self.SESSION_CLAIM_CLI_NON_ACQUIRING_SUBCOMMANDS)} "
                        f"-- an unrecognized session-claim-cli subcommand emitted "
                        f"from baton authoring must be vetted (and, if genuinely "
                        f"non-acquiring, added to the allowlist) before this "
                        f"guard can pass, not silently allowed through"
                    )

    def test_handoff_brief_never_emits_a_claim_acquiring_subcommand(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        self._assert_no_claim_acquiring_directive(decision)

    def test_spinoff_brief_never_emits_a_claim_acquiring_subcommand(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("spinoff", str(artifact), repo_root=tmp_path).decision_object
        self._assert_no_claim_acquiring_directive(decision)


# ---------------------------------------------------------------------------
# (d) CLI smoke + usage-error path
# ---------------------------------------------------------------------------


class TestCliSmoke:
    def test_brief_subcommand_smoke(self, tmp_path, capsys):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        # main() resolves repo_root internally via resolve_repo_root(); patch
        # brief's positional call surface indirectly by cwd'ing is overkill
        # here -- call brief() directly for the CLI's own arg-parse/dispatch
        # smoke via the module-level main() entrypoint, exercised against a
        # real git worktree root (this claude-klabauter checkout itself is one)
        # so resolve_repo_root() succeeds without extra fixture machinery.
        import os

        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = ba.main(["brief", "handoff", str(artifact)])
        finally:
            os.chdir(old_cwd)
        assert exit_code == ba.EXIT_USAGE or exit_code == ba.EXIT_TRANSPORT_FAIL
        # tmp_path is not itself a git worktree, so resolve_repo_root() may
        # legitimately fail transport here -- the smoke asserts the CLI
        # reaches dispatch and returns SOME defined exit code, not a crash.
        out = capsys.readouterr()
        assert exit_code in (ba.EXIT_OK, ba.EXIT_TRANSPORT_FAIL, ba.EXIT_USAGE)

    def test_no_subcommand_is_usage_error(self, capsys):
        exit_code = ba.main([])
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "usage" in err

    def test_unknown_subcommand_is_usage_error(self, capsys):
        exit_code = ba.main(["bogus"])
        assert exit_code == ba.EXIT_USAGE

    def test_brief_missing_args_is_usage_error(self, capsys):
        exit_code = ba.main(["brief"])
        assert exit_code == ba.EXIT_USAGE

    def test_apply_subcommand_missing_args_is_usage_error(self, capsys):
        exit_code = ba.main(["apply"])
        assert exit_code == ba_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "usage" in err

    def test_malformed_decisions_json_is_usage_error(self, tmp_path, capsys):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        exit_code = ba.main(["brief", "handoff", str(artifact), "--decisions", "{not json"])
        assert exit_code == ba.EXIT_USAGE


# ---------------------------------------------------------------------------
# (g) --decisions VALUE-shape validation (twin of pickup_assemble's
# 072ae91c fix -- "Known twin, not fixed here" in that commit's own
# message). Well-formed JSON with the wrong value shape ({"j1": "proceed"}
# instead of {"j1": {"disposition": "proceed"}}) used to be silently
# ignored by baton_assemble too: exit 0/2 with no shape error, the
# judgment point reported unresolved -- indistinguishable from legitimate
# gating. validate_decisions_shape() is duplicated (not imported) from
# pickup_assemble's own copy -- see that function's docstring for why.
# Both CLI parse sites are covered, mirroring 072ae91c exactly: EXIT_USAGE
# for `main()`'s brief dispatch arm, APPLY_EXIT_TRANSPORT_FAIL for
# `main_apply` (its own malformed-JSON path already used that code).
# ---------------------------------------------------------------------------


class TestDecisionsShapeValidation:
    def test_bare_string_decision_value_is_usage_error_on_brief(self, tmp_path, capsys):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        exit_code = ba.main(["brief", "handoff", str(artifact), "--decisions", '{"j1": "proceed"}'])
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "j1" in err
        assert '{"j1": {"disposition": "<value>"}}' in err

    def test_list_decision_value_is_usage_error_on_brief(self, tmp_path, capsys):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        exit_code = ba.main(["brief", "handoff", str(artifact), "--decisions", '{"j1": ["proceed"]}'])
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "j1" in err

    def test_null_decision_value_is_usage_error_on_brief(self, tmp_path, capsys):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        exit_code = ba.main(["brief", "handoff", str(artifact), "--decisions", '{"j1": null}'])
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "j1" in err

    def test_non_object_decisions_is_usage_error_on_brief(self, tmp_path, capsys):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        exit_code = ba.main(["brief", "handoff", str(artifact), "--decisions", "[1, 2]"])
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "must be a JSON object" in err

    def test_valid_shaped_decisions_still_works_on_brief(self, tmp_path, capsys):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', "initiative: init-1", 'predecessor: "none"'],
        )
        # ba.main() has no --repo-root flag -- brief()'s `root` comes from
        # resolve_repo_root(), which shells out to `git rev-parse` against
        # the process cwd (see test_brief_subcommand_smoke's own chdir
        # dance above). Without isolating cwd into a real, isolated repo
        # here, `root` resolves to whatever repo the test RUNNER happens to
        # be invoked from -- letting `resolve_claimed_plan_path`'s ambient
        # claim-ledger lookup leak a real claimed plan's deliverable_id into
        # this artifact's isolated predecessor comparison
        # (DivergentDeliverableIdError, spurious). `_init_repo(tmp_path)` +
        # chdir gives resolve_repo_root() a real, claim-free repo instead.
        _init_repo(tmp_path)
        import os

        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = ba.main(
                ["brief", "handoff", str(artifact), "--decisions", '{"jcc": {"disposition": "proceed"}}']
            )
        finally:
            os.chdir(old_cwd)
        assert exit_code == ba.EXIT_OK

    def test_bare_string_decision_value_is_transport_fail_on_apply(self, capsys):
        exit_code = ba_apply.main_apply(
            ["handoff", "state/handoffs/h1.md", "--decisions", '{"j1": "proceed"}']
        )
        assert exit_code == ba_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "j1" in err
        assert '{"j1": {"disposition": "<value>"}}' in err

    def test_list_decision_value_is_transport_fail_on_apply(self, capsys):
        exit_code = ba_apply.main_apply(
            ["handoff", "state/handoffs/h1.md", "--decisions", '{"j1": ["proceed"]}']
        )
        assert exit_code == ba_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "j1" in err

    def test_null_decision_value_is_transport_fail_on_apply(self, capsys):
        exit_code = ba_apply.main_apply(
            ["handoff", "state/handoffs/h1.md", "--decisions", '{"j1": null}']
        )
        assert exit_code == ba_apply.APPLY_EXIT_TRANSPORT_FAIL
        err = capsys.readouterr().err
        assert "j1" in err

    def test_valid_shaped_decisions_reaches_apply(self, tmp_path, monkeypatch):
        captured = {}

        def _fake_apply(kind, artifact_path, *, session_id=None, repo_root=None, decisions=None, title=None):
            captured["decisions"] = decisions
            return ba_apply.APPLY_EXIT_OK, {"landed": []}

        monkeypatch.setattr(ba_apply, "apply", _fake_apply)

        exit_code = ba_apply.main_apply(
            ["handoff", "state/handoffs/h1.md", "--decisions", '{"j1": {"disposition": "proceed"}}']
        )

        assert exit_code == ba_apply.APPLY_EXIT_OK
        assert captured["decisions"] == {"j1": {"disposition": "proceed"}}


# ---------------------------------------------------------------------------
# (h) --decisions "value" key acceptance -- brief's own OUTPUT vocabulary
# names the choice-key "value" (`_build_judgment_points` emits
# `dispositions=[{"value": "proceed", ...}]`); an operator round-tripping
# that straight back into --decisions was rejected for using the engine's
# own word. See the live-failure writeup this closes (cross-repo dispatch,
# 2026-07-29): step 3->4 hand-translated `value` into a payload keyed
# `{"value": "proceed"}` and `apply` rejected it.
# ---------------------------------------------------------------------------


class TestDecisionsValueKeyEquivalence:
    def test_value_keyed_decisions_accepted_on_brief(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', "initiative: init-1", 'predecessor: "none"'],
        )
        # See test_valid_shaped_decisions_still_works_on_brief's comment
        # (same class of test, same ambient-repo-root leak via ba.main()'s
        # resolve_repo_root() -- isolate cwd into a real, claim-free repo).
        _init_repo(tmp_path)
        import os

        old_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            exit_code = ba.main(
                ["brief", "handoff", str(artifact), "--decisions", '{"jcc": {"value": "proceed"}}']
            )
        finally:
            os.chdir(old_cwd)
        assert exit_code == ba.EXIT_OK

    def test_value_keyed_decisions_reaches_apply_normalized_to_disposition(self, monkeypatch):
        captured = {}

        def _fake_apply(kind, artifact_path, *, session_id=None, repo_root=None, decisions=None, title=None):
            captured["decisions"] = decisions
            return ba_apply.APPLY_EXIT_OK, {"landed": []}

        monkeypatch.setattr(ba_apply, "apply", _fake_apply)

        exit_code = ba_apply.main_apply(
            ["handoff", "state/handoffs/h1.md", "--decisions", '{"j1": {"value": "proceed"}}']
        )

        assert exit_code == ba_apply.APPLY_EXIT_OK
        # Normalized in place -- downstream consumers only ever read "disposition".
        assert captured["decisions"] == {"j1": {"disposition": "proceed"}}

    def test_extra_sibling_keys_alongside_value_are_tolerated(self):
        decisions = {"j1": {"value": "proceed", "decision_note": "looks fine"}}
        assert ba.validate_decisions_shape(decisions) is None
        assert decisions == {"j1": {"disposition": "proceed", "decision_note": "looks fine"}}

    def test_disposition_and_value_agreeing_both_present_is_accepted(self):
        decisions = {"j1": {"disposition": "proceed", "value": "proceed"}}
        assert ba.validate_decisions_shape(decisions) is None
        assert decisions == {"j1": {"disposition": "proceed"}}

    def test_disposition_and_value_disagreeing_fails_loud_naming_both(self):
        decisions = {"j1": {"disposition": "proceed", "value": "decline"}}
        error = ba.validate_decisions_shape(decisions)
        assert error is not None
        assert "proceed" in error
        assert "decline" in error

    def test_neither_disposition_nor_value_still_fails_loud(self):
        decisions = {"j1": {"decision_note": "no choice supplied"}}
        error = ba.validate_decisions_shape(decisions)
        assert error is not None
        assert "j1" in error

    def test_bare_string_still_fails_loud_not_coerced(self):
        # Negative-spec preserved: a bare string is never silently coerced
        # into {"disposition": <string>}, even though "value" is now accepted.
        decisions = {"j1": "proceed"}
        error = ba.validate_decisions_shape(decisions)
        assert error is not None


# ---------------------------------------------------------------------------
# (h2) 2026-08-05 -- `main()`'s `--deliverable-id` CLI flag, the last hop
# threading an EM-supplied id down to `brief()`'s own `explicit_deliverable_id`
# parameter. Only passed through to `brief()` when actually supplied, so an
# invocation that never names the flag calls `brief()` exactly as before
# (no signature churn at that call site for existing callers/fakes).
# ---------------------------------------------------------------------------


class TestMainDeliverableIdFlagThreading:
    def test_flag_supplied_threads_through_to_brief_as_explicit_deliverable_id(
        self, tmp_path, monkeypatch
    ):
        captured = {}

        def _fake_brief(kind, artifact_path, decisions, *, title=None, explicit_deliverable_id=None):
            captured["explicit_deliverable_id"] = explicit_deliverable_id
            return ba.BriefResult({"artifact": {}, "directives": [], "judgment_points": []}, ba.EXIT_OK)

        monkeypatch.setattr(ba, "brief", _fake_brief)

        exit_code = ba.main(
            ["brief", "spinoff", "my-slug", "--deliverable-id", "DEL-EM-SUPPLIED"]
        )

        assert exit_code == ba.EXIT_OK
        assert captured["explicit_deliverable_id"] == "DEL-EM-SUPPLIED"

    def test_flag_absent_calls_brief_with_no_signature_churn(self, tmp_path, monkeypatch):
        # Mirrors TestTrailingPositionalBecomesTitle's fake -- a caller that
        # never learned about `explicit_deliverable_id` must keep working
        # unchanged when the flag is not supplied.
        captured = {}

        def _fake_brief(kind, artifact_path, decisions, *, title=None):
            captured["called"] = True
            return ba.BriefResult({"artifact": {}, "directives": [], "judgment_points": []}, ba.EXIT_OK)

        monkeypatch.setattr(ba, "brief", _fake_brief)

        exit_code = ba.main(["brief", "spinoff", "my-slug"])

        assert exit_code == ba.EXIT_OK
        assert captured["called"] is True

    def test_flag_missing_value_is_usage_error(self):
        exit_code = ba.main(["brief", "spinoff", "my-slug", "--deliverable-id"])
        assert exit_code == ba.EXIT_USAGE


# ---------------------------------------------------------------------------
# (i) trailing-positional-becomes-title + offer-shaped unrecognized-argument
# error (design-as-offers, project CLAUDE.md). Closes the round-trip where
# `baton-assemble brief spinoff <slug> "Some Title"` errored with a bare
# "unrecognized argument" and forced a --help re-read to discover --title.
# ---------------------------------------------------------------------------


class TestTrailingPositionalBecomesTitle:
    def test_trailing_bare_token_after_artifact_path_becomes_title_on_brief(self, tmp_path, monkeypatch):
        captured = {}

        def _fake_brief(kind, artifact_path, decisions, *, title=None):
            captured["title"] = title
            return ba.BriefResult({"artifact": {}, "directives": [], "judgment_points": []}, ba.EXIT_OK)

        monkeypatch.setattr(ba, "brief", _fake_brief)

        exit_code = ba.main(["brief", "spinoff", "my-slug", "Some Title"])

        assert exit_code == ba.EXIT_OK
        assert captured["title"] == "Some Title"

    def test_trailing_bare_token_after_artifact_path_becomes_title_on_apply(self, monkeypatch):
        captured = {}

        def _fake_apply(kind, artifact_path, *, session_id=None, repo_root=None, decisions=None, title=None):
            captured["title"] = title
            return ba_apply.APPLY_EXIT_OK, {"landed": []}

        monkeypatch.setattr(ba_apply, "apply", _fake_apply)

        exit_code = ba_apply.main_apply(["spinoff", "state/handoffs/h1.md", "Some Title"])

        assert exit_code == ba_apply.APPLY_EXIT_OK
        assert captured["title"] == "Some Title"

    def test_second_bare_token_after_title_already_bound_is_still_an_error(self, tmp_path):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        exit_code = ba.main(["brief", "spinoff", str(artifact), "Title One", "Title Two"])
        assert exit_code == ba.EXIT_USAGE

    def test_ambiguous_handoff_bare_token_without_artifact_path_offers_title_flag(self, capsys):
        # kind=handoff with no positional consumed as artifact-path -- a
        # bare token here is genuinely ambiguous (could be a mistyped
        # artifact-path), so it stays an error, but the message offers
        # --title as the likely fix rather than a bare scold.
        exit_code = ba.main(["brief", "handoff", "--decisions", "{}", "Some Title"])
        assert exit_code == ba.EXIT_USAGE
        err = capsys.readouterr().err
        assert "--title" in err
        assert "Some Title" in err


class TestValidateDecisionsShapeIsPickupsOwnCopy:
    """Both engines' `validate_decisions_shape` are independent copies of the
    same predicate (module docstrings on both sides), not one shared
    function -- assert they agree on every case rather than trusting the
    docstrings alone to keep them in sync."""

    @pytest.mark.parametrize(
        "decisions",
        [
            {},
            {"j1": {"disposition": "proceed"}},
            {"j1": {"value": "proceed"}},
            {"j1": {"disposition": "proceed", "value": "proceed"}},
            {"j1": {"disposition": "proceed", "value": "decline"}},
            {"j1": {}},
            {"j1": "proceed"},
            {"j1": ["proceed"]},
            {"j1": None},
            [1, 2],
            "not-a-dict",
            None,
        ],
    )
    def test_baton_and_pickup_validators_agree(self, decisions):
        from coordinator_core.pickup_assemble import validate_decisions_shape as pickup_validate

        baton_result = ba.validate_decisions_shape(decisions)
        pickup_result = pickup_validate(decisions)
        assert (baton_result is None) == (pickup_result is None)


# ---------------------------------------------------------------------------
# (e) resolve_operator_config spy -- brief() calls the shared B0 resolver,
# never re-derives its own settings_home/claude_klabauter_root/doe_root.
# ---------------------------------------------------------------------------


class TestResolveOperatorConfigSpy:
    def test_brief_calls_resolve_operator_config_exactly_once(self, tmp_path, monkeypatch):
        artifact = _write_artifact(tmp_path / "state" / "handoffs" / "h1.md", ["deliverable_id: DEL-1"])
        calls: list[int] = []

        def _spy():
            calls.append(1)
            return dict(_FAKE_OPERATOR_CONFIG)

        monkeypatch.setattr(ba, "resolve_operator_config", _spy)
        ba.brief("handoff", str(artifact), repo_root=tmp_path)

        assert len(calls) == 1

    def test_brief_never_defines_its_own_settings_home_helper(self):
        # Module-docstring negative-spec: no local `_settings_home()`-shaped
        # symbol exists on this module -- brief() has exactly one resolver
        # seam, the imported `resolve_operator_config`.
        assert not hasattr(ba, "_settings_home")
        assert not hasattr(ba, "_resolve_settings_home")


# ---------------------------------------------------------------------------
# (f) apply_base runner tests -- against baton_assemble.apply's own
# composition (_execute_directives / _CLI_DISPATCH / _resolve_cli), per this
# chunk's instruction to assert "once against the FACTORED runner."
# ---------------------------------------------------------------------------


class TestApplyBaseClosedDispatchRejection:
    def test_unrecognized_cli_raises_before_any_directive_dispatches(self):
        with pytest.raises(ba_apply.UnrecognizedDirective):
            ba_apply._resolve_cli("rm")

    def test_every_real_baton_directive_cli_resolves(self):
        for name in (
            "coordinator-doc-new",
            "lint-frontmatter",
            "session-claim-cli",
            "handoff.stamp_phase",
            "handoff.author_fork",
            "render-project-tracker",
            "handoff.supersede_predecessor",
        ):
            assert callable(ba_apply._resolve_cli(name))

    def test_dispatch_table_is_closed_over_exactly_the_real_set(self):
        assert set(ba_apply._CLI_DISPATCH) == {
            "coordinator-doc-new",
            "lint-frontmatter",
            "session-claim-cli",
            "handoff.stamp_phase",
            "handoff.author_fork",
            "render-project-tracker",
            "handoff.supersede_predecessor",
        }


class TestEveryEmittedDirectiveActuallyDispatches:
    """Closes the test gap that let ``handoff.stamp_phase`` ship DECLARED
    (op_scopes.py scope entry, ipc.py docs, a directive this module used to
    emit) but never actually REGISTERED on the live dispatch path -- the
    pre-existing tests (``test_every_real_baton_directive_cli_resolves``
    above) only proved a `cli` name is a KEY in `_CLI_DISPATCH`, which
    ``handoff.stamp_phase`` always was; the break was one layer deeper
    (`_invoke_op_in_process`'s `get_op_handler()` call had no import trigger,
    so the op-registry was empty for it in a fresh process) and nothing
    asserted that layer. General over `brief()`'s CURRENT emitted directive
    set for both kinds -- not a special case for the one op that shipped
    broken -- so the next op-invoking directive that forgets its trigger
    import fails a test instead of failing live.
    """

    #: cli name -> the op name it dispatches to via `_invoke_op_in_process`,
    #: or None for a standalone bin/ CLI reached via subprocess (no op
    #: registry involved). Kept as an explicit map (not derived by
    #: introspecting the handler bodies) so this test fails loud, not
    #: silently, if a handler's dispatch shape ever changes underneath it.
    _CLI_OP_NAMES = {
        "coordinator-doc-new": None,
        "lint-frontmatter": None,
        "session-claim-cli": None,
        "handoff.stamp_phase": "handoff.stamp_phase",
        "handoff.author_fork": "handoff.author_fork",
        # 2026-07-28 break-class fix: was "project.render_tracker" -- no
        # module anywhere registered that op name (confirmed by repo-wide
        # grep), so `apply handoff <slug>` hard-aborted at d4 with
        # "unrecognized op 'project.render_tracker'" every time it fired.
        # `render-project-tracker` was never an `ipc` op at all: it is a
        # standalone `coordinator/bin/` CLI (the SOLE writer of
        # `docs/project-tracker.md`), reached by subprocess exactly like
        # `coordinator-doc-new`/`lint-frontmatter`/`session-claim-cli`
        # above -- so it belongs in the `None` (standalone subprocess CLI,
        # no op registry involved) bucket, not the op-dispatch bucket. See
        # `apply._dispatch_render_project_tracker`'s own docstring.
        "render-project-tracker": None,
        "handoff.supersede_predecessor": "handoff.archive_transition",
    }

    def test_every_emitted_directive_cli_is_in_the_dispatch_table(self, tmp_path):
        """General shape: every `cli` name `brief()` emits, for every kind,
        must resolve in `_CLI_DISPATCH` -- the shallow layer the pre-existing
        tests already covered, re-asserted here against brief()'s ACTUAL
        emission (not a hand-maintained literal list) so a future directive
        add/rename can't silently drift from the dispatch table."""
        handoff_artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ["deliverable_id: DEL-1", 'predecessor: "none"'],
        )
        spinoff_artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-2-1a2b3d"],
        )
        for kind, artifact in (("handoff", handoff_artifact), ("spinoff", spinoff_artifact)):
            decision = ba.brief(kind, str(artifact), repo_root=tmp_path).decision_object
            for directive in decision["directives"]:
                cli = directive["cli"]
                assert callable(ba_apply._resolve_cli(cli)), (
                    f"kind={kind} directive {directive['id']} names cli {cli!r} "
                    "which does not resolve in _CLI_DISPATCH"
                )

    def test_op_invoking_cli_names_resolve_in_the_op_registry(self):
        """The deep layer the live break actually lived in: for every `cli`
        in the dispatch table that routes to `_invoke_op_in_process` (per
        `_CLI_OP_NAMES` above), the op name it passes must be a REGISTERED
        JSON-RPC op after a full eager import -- not merely a key that
        happens to exist in `_CLI_DISPATCH`.

        `project.render_tracker` (the `render-project-tracker` directive's
        target) is a SEPARATE, pre-existing, not-this-task's-scope defect:
        no module anywhere registers that op name (confirmed by repo-wide
        grep, 2026-07-25) -- `_dispatch_render_project_tracker` dispatches to
        an op that was never implemented, independent of the
        handoff.stamp_phase import-trigger bug this task fixes. Marked
        xfail (not skipped/removed) so it stays visible in test output as an
        open, tracked break rather than being silently dropped from
        coverage.
        """
        import coordinator_core.ops as ops_pkg
        from coordinator_core.ipc import get_op_handler

        ops_pkg._eager_import_all()

        for cli, op_name in self._CLI_OP_NAMES.items():
            if op_name is None:
                continue
            if op_name == "project.render_tracker":
                # Would fail today: no module registers this op name.
                # Tracked separately -- see docstring above.
                continue
            assert get_op_handler(op_name) is not None, (
                f"cli {cli!r} dispatches to op {op_name!r} via "
                "_invoke_op_in_process, but no module registers that op name"
            )

    @pytest.mark.xfail(
        reason=(
            "pre-existing, out-of-this-task's-scope defect: 'project.render_tracker' "
            "is never registered by any op module -- the render-project-tracker "
            "directive's _dispatch_render_project_tracker dispatches to a "
            "nonexistent op. Historically masked because handoff.stamp_phase's "
            "directive (removed 2026-07-25) always aborted the run before this "
            "directive could ever be reached. Flagged in the fixing session's "
            "run report for a follow-up bounded task; not fixed here (requires "
            "a design decision -- implement the op, or redirect the dispatch to "
            "a subprocess CLI like d1/d2 -- outside this fix's bounded scope)."
        ),
        strict=True,
    )
    def test_render_project_tracker_op_is_registered(self):
        import coordinator_core.ops as ops_pkg
        from coordinator_core.ipc import get_op_handler

        ops_pkg._eager_import_all()
        assert get_op_handler("project.render_tracker") is not None


class TestDispatchRenderProjectTrackerFailPosture:
    """d4 degrades, never raises -- the 2026-07-29 break-class fix.

    The live defect: on any repo whose `docs/project-tracker.md` is
    hand-curated and whose `state/workstreams/` store is empty, the renderer's
    zero-workstream truncation guard correctly declined, this handler raised,
    `execute_directives` reported APPLY_EXIT_PARTIAL_MUTATION, and d1's
    compensator DELETED the freshly-minted successor -- so `/handoff` could
    not mint a baton at all there, and the operator hand-ran the remaining
    directives one at a time. `docs/project-tracker.md` is a derived view and
    nothing depends on d4, so a raise trades the save-state for a stale copy
    of a regenerable file. See the handler's own FAIL POSTURE docstring.
    """

    @staticmethod
    def _run(monkeypatch, tmp_path, returncode: int, stderr: str = "") -> dict[str, Any]:
        monkeypatch.setattr(
            "coordinator_core.resolution.facade.resolve_operator_config",
            lambda: dict(_FAKE_OPERATOR_CONFIG),
        )
        monkeypatch.setattr(
            ba_apply.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0] if a else [], returncode, stdout="", stderr=stderr
            ),
        )
        return ba_apply._dispatch_render_project_tracker([], tmp_path)

    def test_not_queue_backed_decline_degrades_quietly_and_is_labelled(
        self, monkeypatch, tmp_path, capsys
    ):
        """2026-08-05 fix: a hand-curated repo's tracker is not queue-backed
        by design and this decline fires on EVERY `/handoff` there -- a
        warning that always fires trains its reader to stop reading warnings,
        including the genuinely-suspect `tracker-render-regression` case
        below. Demoted from a printed degrade to a quiet, structured-only
        one: still fully present in the returned `degraded` dict for a
        `--json` consumer, but no longer shouted on stderr."""
        from coordinator_core.ops.render_project_tracker import EXIT_NOT_APPLICABLE

        detail = self._run(
            monkeypatch, tmp_path, EXIT_NOT_APPLICABLE, stderr="refusing to overwrite"
        )

        assert detail["degraded"]["reason"] == "tracker-not-queue-backed"
        assert detail["degraded"]["returncode"] == EXIT_NOT_APPLICABLE
        assert detail["degraded"]["stderr"] == "refusing to overwrite"
        # No longer printed -- this is the steady-state, information-free
        # outcome for every hand-curated-tracker repo on every `/handoff`.
        err = capsys.readouterr().err
        assert err == ""

    def test_genuine_renderer_fault_also_degrades_but_is_labelled_differently(
        self, monkeypatch, tmp_path
    ):
        """A broken renderer is still not worth destroying a baton over -- but
        it must be distinguishable in the report from the steady-state decline,
        so only the former gets chased as a defect."""
        detail = self._run(monkeypatch, tmp_path, 1, stderr="boom")

        assert detail["degraded"]["reason"] == "render-failed"
        assert detail["degraded"]["stderr"] == "boom"

    def test_render_regression_degrades_and_is_labelled_distinctly(
        self, monkeypatch, tmp_path, capsys
    ):
        """`EXIT_RENDER_REGRESSION` (the renderer's own truncation guard
        catching a collapse-to-zero over a previously-populated tracker) must
        be distinguishable from both the benign not-queue-backed decline and
        a generic renderer fault -- the DATA axis, not the FAULT axis."""
        from coordinator_core.ops.render_project_tracker import EXIT_RENDER_REGRESSION

        detail = self._run(
            monkeypatch, tmp_path, EXIT_RENDER_REGRESSION, stderr="truncation guard fired"
        )

        assert detail["degraded"]["reason"] == "tracker-render-regression"
        assert detail["degraded"]["returncode"] == EXIT_RENDER_REGRESSION
        assert detail["degraded"]["stderr"] == "truncation guard fired"
        err = capsys.readouterr().err
        assert "degraded" in err
        assert "truncation guard fired" in err

    def test_success_reports_no_degrade(self, monkeypatch, tmp_path):
        assert self._run(monkeypatch, tmp_path, 0)["degraded"] is None


class TestApplyBasePerDirectiveHalt:
    def test_directive_blocked_by_unresolved_judgment_point_halts_and_mutates_nothing(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "session-claim-cli", lambda a, r: called.append(1))

        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": [], "depends_on": "j1", "already_satisfied": False}
        ]
        judgment_points = [{"id": "j1", "question": "?", "dispositions": []}]

        exit_code, report = ba_apply._execute_directives(directives, judgment_points, tmp_path)

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == []
        assert "j1" in report["unresolved_judgment_points"]
        assert called == []


class TestApplyBaseNoOp:
    def test_empty_directives_and_judgment_points_is_a_clean_no_op(self, tmp_path):
        exit_code, report = ba_apply._execute_directives([], [], tmp_path)
        assert exit_code == ba_apply.APPLY_EXIT_OK
        assert report["landed"] == []


class TestApplyBasePartialMutation:
    def test_a_raising_handler_reports_partial_mutation_and_names_the_failed_directive(self, tmp_path, monkeypatch):
        def _boom(args: list[str], repo_root: Path) -> dict[str, Any]:
            raise RuntimeError("simulated handler failure")

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _boom)

        directives = [
            {"id": "d1", "cli": "coordinator-doc-new", "args": [], "depends_on": None, "already_satisfied": True},
            {"id": "d2", "cli": "lint-frontmatter", "args": [], "depends_on": "d1", "already_satisfied": False},
        ]

        exit_code, report = ba_apply._execute_directives(directives, [], tmp_path)

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["landed"] == ["d1"]
        assert report["failed_directive"] == "d2"


# ---------------------------------------------------------------------------
# (g) `_invoke_op_in_process` root-tier conversion -- reproduces and fixes the
# handoff-writes-outside-the-repo defect: `apply()` resolves `repo_root` as
# the WORKTREE root (`resolve_repo_root()` runs `git rev-parse
# --show-toplevel`), but `common_dir`-scoped ops (per
# `coordinator_core.ipc.OP_KEY_SCOPE`, the SAME table `ipc.py`'s own
# `resolve_op_repo_key` consults for the UDS transport path) contractually
# expect `git_common_dir(worktree)`, not the worktree root itself --
# `main_worktree_root()` (`ops/fleet/_common.py`) takes `.parent` of whatever
# it is handed, so an unconverted worktree root lands one directory ABOVE the
# repo. `_invoke_op_in_process` now reuses `OP_KEY_SCOPE` (never a second,
# hand-maintained scope list) to convert per-op before dispatch.
# ---------------------------------------------------------------------------


class TestInvokeOpInProcessRootScopeConversion:
    def test_common_dir_scoped_op_receives_git_common_dir_not_the_worktree_root(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        captured: dict[str, Any] = {}

        async def _spy(params: dict[str, Any], repo_root):
            captured["repo_root"] = repo_root
            return {"ok": True}

        import coordinator_core.ipc as ipc_mod

        # handoff.stamp_phase is OP_KEY_SCOPE-classified "common_dir" (op_scopes.py).
        monkeypatch.setattr(ipc_mod, "get_op_handler", lambda name: _spy)

        ba_apply._invoke_op_in_process("handoff.stamp_phase", {}, tmp_path)

        from coordinator_core.lifecycle import git_common_dir

        expected = git_common_dir(tmp_path)
        assert captured["repo_root"] == expected
        assert captured["repo_root"] != tmp_path

    def test_show_top_scoped_op_receives_the_worktree_root_unconverted(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        captured: dict[str, Any] = {}

        async def _spy(params: dict[str, Any], repo_root):
            captured["repo_root"] = repo_root
            return {"ok": True}

        import coordinator_core.ipc as ipc_mod

        # coverage.gate is OP_KEY_SCOPE-classified "show_top" (op_scopes.py).
        monkeypatch.setattr(ipc_mod, "get_op_handler", lambda name: _spy)

        ba_apply._invoke_op_in_process("coverage.gate", {}, tmp_path)

        assert captured["repo_root"] == tmp_path

    def test_none_scoped_op_receives_none(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        captured: dict[str, Any] = {"called": False}

        async def _spy(params: dict[str, Any], repo_root):
            captured["called"] = True
            captured["repo_root"] = repo_root
            return {"ok": True}

        import coordinator_core.ipc as ipc_mod

        # ping is OP_KEY_SCOPE-classified "none" (op_scopes.py).
        monkeypatch.setattr(ipc_mod, "get_op_handler", lambda name: _spy)

        ba_apply._invoke_op_in_process("ping", {}, tmp_path)

        assert captured["called"] is True
        assert captured["repo_root"] is None

    def test_unclassified_op_defaults_to_none_scope(self, tmp_path, monkeypatch):
        """An op absent from OP_KEY_SCOPE is treated as scope "none" -- the
        documented default (op_scopes.py module docstring) -- never silently
        promoted to a working-tree/common-dir key."""
        _init_repo(tmp_path)
        captured: dict[str, Any] = {}

        async def _spy(params: dict[str, Any], repo_root):
            captured["repo_root"] = repo_root
            return {"ok": True}

        import coordinator_core.ipc as ipc_mod

        assert "totally.unclassified.op" not in ipc_mod.OP_KEY_SCOPE
        monkeypatch.setattr(ipc_mod, "get_op_handler", lambda name: _spy)

        ba_apply._invoke_op_in_process("totally.unclassified.op", {}, tmp_path)

        assert captured["repo_root"] is None


class TestHandoffAuthorForkRegressionLandsInsideTheRepo:
    """Regression pin for the reproduced live bug: `baton-assemble apply
    spinoff ...` was creating `state/handoffs/` one directory ABOVE the repo
    (in the repo's PARENT), because `handoff.author_fork` is `common_dir`-
    scoped but was receiving the unconverted worktree root. Exercises the
    REAL op handler (registered via apply.py's own top-of-module import) end
    to end against a real git repo -- not a mock -- so a regression in either
    `_invoke_op_in_process`'s conversion OR in `main_worktree_root`'s own
    contract fails this test."""

    def test_author_fork_writes_handoffs_dir_inside_the_repo_not_its_parent(self, tmp_path, monkeypatch):
        for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)

        repo = tmp_path / "repo"
        _init_repo(repo)

        result = ba_apply._invoke_op_in_process(
            "handoff.author_fork", {"title": "regression spinoff"}, repo
        )

        assert result.get("status") != "error", result

        expected_handoffs_dir = repo / "state" / "handoffs"
        wrong_handoffs_dir = repo.parent / "state" / "handoffs"

        assert expected_handoffs_dir.is_dir()
        created = list(expected_handoffs_dir.glob("*.md"))
        assert created, f"expected a new handoff under {expected_handoffs_dir}"
        assert not wrong_handoffs_dir.exists(), (
            f"handoff.author_fork wrote outside the repo at {wrong_handoffs_dir}"
        )


# ---------------------------------------------------------------------------
# (g) apply() must commit/name the NORMALIZED artifact path, not the raw
# `artifact_path` parameter -- regression for a bare-slug invocation
# scaffolding at the normalized path (d1, via brief()) while `apply()`'s own
# basename/`_scoped_commit` pathspec computation still read the un-normalized
# slug. See FIX-C dispatch brief.
# ---------------------------------------------------------------------------


class TestApplyReportAccountsForEveryCommit:
    """`report["commits"]` names every commit the run produced, not only
    `apply()`'s own `_scoped_commit` -- the 2026-07-29 legibility fix.

    d6 (`handoff.supersede_predecessor`) git-mv's the predecessor into
    `archive/handoffs/` and commits that itself, so a report carrying a single
    `commit_sha` after a two-commit run left the operator hand-walking
    `git log` over both paths to find out whether the rename's deletion side
    was stranded (observed live, 2026-07-29).
    """

    @staticmethod
    def _apply_with(monkeypatch, tmp_path, results, *, sha="deadbeefcafe"):
        monkeypatch.setattr(
            ba_apply,
            "_execute_directives",
            lambda *a, **k: (ba_apply.APPLY_EXIT_OK, {"landed": ["d1", "d6"], "results": results}),
        )
        monkeypatch.setattr(ba_apply, "_scoped_commit", lambda *a, **k: sha)
        return ba_apply.apply(
            "handoff", "some-slug", session_id="test-session", repo_root=tmp_path
        )[1]

    @staticmethod
    def _d6_result_row(*, moved: bool) -> dict[str, Any]:
        """Builds the row through the REAL `DirectiveResult.to_report()` rather
        than hand-writing a dict literal.

        These tests originally hand-built the row with a `"directive_id"` key.
        `to_report()` emits `"id"`, so the fixture and the code under test
        shared one wrong assumption and the suite agreed with the bug it was
        meant to catch (`commits[]` reported `None` for the directive it exists
        to name). Sourcing the row from the producing type is what makes that
        class of agreement impossible, so do not inline a literal here again.
        """
        return ba_apply.apply_base.DirectiveResult(
            "d6",
            already_satisfied=False,
            detail={"cli": "handoff.supersede_predecessor", "result": {"moved": moved}},
        ).to_report()

    def test_a_self_committing_directive_is_accounted_for(self, monkeypatch, tmp_path):
        report = self._apply_with(monkeypatch, tmp_path, [self._d6_result_row(moved=True)])

        by_directive = {c["directive_id"]: c for c in report["commits"]}
        # Names the directive, never None -- the regression this row's
        # `to_report()`-sourced fixture exists to pin.
        assert "d6" in by_directive
        assert by_directive["d6"]["committed_by"] == "handoff.archive_transition"
        # The op returns `moved: bool` and no sha -- reported as such, never
        # a fabricated one.
        assert by_directive["d6"]["sha"] is None
        assert by_directive[None]["sha"] == "deadbeefcafe"

    def test_stamp_in_place_supersede_contributes_no_commit_entry(self, monkeypatch, tmp_path):
        """An already-archived predecessor is stamped in place with no git-mv,
        so there is no second commit to account for."""
        report = self._apply_with(monkeypatch, tmp_path, [self._d6_result_row(moved=False)])

        assert [c["directive_id"] for c in report["commits"]] == [None]

    def test_commits_present_as_empty_list_when_nothing_committed(self, monkeypatch, tmp_path):
        """Absent-vs-empty must not be the ambiguity: "committed nothing" and
        "the report does not say" have to read differently."""
        report = self._apply_with(monkeypatch, tmp_path, [], sha=None)

        assert report["commits"] == []


class TestApplyUsesNormalizedArtifactPathForCommitAndBasename:
    def _run_apply(self, tmp_path, monkeypatch, artifact_path, kind="handoff"):
        captured: dict[str, Any] = {}

        def _fake_execute_directives(directives, judgment_points, repo_root, *, decisions=None):
            return ba_apply.APPLY_EXIT_OK, {"landed": ["d1"]}

        def _fake_scoped_commit(repo_root, artifact_rel_path, kind_, basename, landed):
            captured["artifact_rel_path"] = artifact_rel_path
            captured["basename"] = basename
            return "deadbeefcafe"

        monkeypatch.setattr(ba_apply, "_execute_directives", _fake_execute_directives)
        monkeypatch.setattr(ba_apply, "_scoped_commit", _fake_scoped_commit)

        exit_code, report = ba_apply.apply(
            kind, artifact_path, session_id="test-session", repo_root=tmp_path
        )
        return exit_code, report, captured

    def test_bare_slug_apply_stages_the_normalized_path_not_the_bare_slug(self, tmp_path, monkeypatch):
        """The actual regression test: made genuinely RED against pre-fix
        `apply()` (which passed the raw `artifact_path` positional straight
        through to `_scoped_commit`) before the fix landed."""
        import datetime

        exit_code, report, captured = self._run_apply(tmp_path, monkeypatch, "my-bare-slug")

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        expected_path = f"state/handoffs/{today}-my-bare-slug.md"

        assert exit_code == ba_apply.APPLY_EXIT_OK
        assert captured["artifact_rel_path"] == expected_path
        assert captured["artifact_rel_path"] != "my-bare-slug"

    def test_bare_slug_apply_basename_names_the_real_file_not_the_bare_slug(self, tmp_path, monkeypatch):
        """`basename` feeds `_compute_commit_message` -- a bare-slug apply
        must produce a commit message naming the real scaffolded file, not
        the bare slug it was invoked with."""
        import datetime

        exit_code, report, captured = self._run_apply(tmp_path, monkeypatch, "another-slug")

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        expected_basename = f"{today}-another-slug.md"

        assert captured["basename"] == expected_basename
        assert captured["basename"] != "another-slug"

    def test_qualified_input_path_apply_commits_the_minted_successor_not_the_input(
        self, tmp_path, monkeypatch
    ):
        """SUPERSEDES `test_already_qualified_path_apply_is_byte_identical_to_
        pre_fix_behaviour` (2026-07-29). That test asserted the pathspec equalled
        the qualified INPUT path -- i.e. it pinned, as a non-regression
        invariant, a defect: `apply()` staged the path it READ instead of the
        path it WROTE. Bare-slug inputs hid it (there `output_path ==
        artifact_path`), which is why it survived.

        Two live consequences, both fixed with the pathspec: the minted
        successor -- the run's ONE load-bearing artifact -- was never committed,
        and `/handoff`'s default shape (qualified predecessor path, supplied by
        `_resolve_held_handoff_for_session`) ended in an uncaught RuntimeError
        because d6 had already `git mv`'d that very path into `archive/handoffs/`
        before `git add -- <it>` ran. See `apply()`'s own comment block.

        The two bare-slug assertions above are unchanged and still pass -- this
        is a correction to the qualified case only, not a re-derivation of the
        normalization fix they pin.

        The referenced file must actually exist on disk: 2026-07-28's
        archive-aware resolution fix makes a qualified path that resolves
        NOWHERE fail loud rather than silently degrading to empty frontmatter."""
        import datetime

        qualified = "docs/plans/2026-01-01-existing-plan.md"
        _write_artifact(tmp_path / qualified, ["deliverable_id: DEL-1"])

        exit_code, report, captured = self._run_apply(tmp_path, monkeypatch, qualified)

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        assert captured["artifact_rel_path"] == f"state/handoffs/{today}-existing-plan.md"
        assert captured["artifact_rel_path"] != qualified
        assert captured["basename"] == f"{today}-existing-plan.md"

    def test_apply_with_malformed_artifact_key_falls_back_to_raw_artifact_path(self, tmp_path, monkeypatch):
        """Guard for a missing/malformed `artifact` key in the decision
        object -- `apply()` must not crash, and must fall back to the raw
        `artifact_path` parameter rather than raising."""
        captured: dict[str, Any] = {}

        def _fake_brief(kind, artifact_path, *, decisions=None, repo_root=None, title=None):
            class _FakeBriefResult:
                decision_object = {"directives": [], "judgment_points": [], "artifact": "not-a-dict"}

            return _FakeBriefResult()

        def _fake_execute_directives(directives, judgment_points, repo_root, *, decisions=None):
            return ba_apply.APPLY_EXIT_OK, {"landed": []}

        def _fake_scoped_commit(repo_root, artifact_rel_path, kind_, basename, landed):
            captured["artifact_rel_path"] = artifact_rel_path
            captured["basename"] = basename
            return None

        monkeypatch.setattr(ba, "brief", _fake_brief)
        monkeypatch.setattr(ba_apply, "_execute_directives", _fake_execute_directives)
        monkeypatch.setattr(ba_apply, "_scoped_commit", _fake_scoped_commit)

        exit_code, report = ba_apply.apply(
            "handoff", "fallback-path.md", session_id="test-session", repo_root=tmp_path
        )

        assert exit_code == ba_apply.APPLY_EXIT_OK
        assert captured["artifact_rel_path"] == "fallback-path.md"
        assert captured["basename"] == "fallback-path.md"


class TestHandoffStampPhaseSiteReceivesArgsVerbatim:
    """Investigation per FIX-C dispatch brief: `_dispatch_handoff_stamp_phase`'s
    `artifact_path = args[0] if args else ""` reads only the directive's OWN
    `args` -- never `apply()`'s raw `artifact_path` parameter directly. Those
    `args` are only ever populated (when/if a directive named
    `"handoff.stamp_phase"` is emitted) by `_build_directives`, which reads
    `lineage["artifact_path"]` -- already normalized by
    `resolve_lineage`/`_normalize_artifact_path` before `_build_directives`
    ever sees it. This site was ALREADY correct; these tests pin that fact
    (now load-bearing) rather than changing anything.

    Separately: `_build_directives` currently emits NO `"handoff.stamp_phase"`
    directive for `kind == "handoff"` at all (removed 2026-07-25, see this
    module's own docstring) -- the dispatch-table entry is dormant, kept only
    for a future/other caller. Both facts are pinned below.
    """

    def test_handler_passes_through_its_own_args_not_apply_s_raw_parameter(self, tmp_path, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_invoke(op_name, params, repo_root):
            captured["op_name"] = op_name
            captured["params"] = params
            # Mirrors handoff.stamp_phase's real `_ok(...)` reply shape --
            # the handler's fail posture keys on exit_code, so a stub that
            # omits it would be testing against a contract the op never emits.
            return {"exit_code": 0, "applied": True, "message": "stamped"}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        normalized = "state/handoffs/2026-07-27-some-slug.md"
        result = ba_apply._dispatch_handoff_stamp_phase([normalized], tmp_path)

        assert captured["op_name"] == "handoff.stamp_phase"
        assert captured["params"] == {"handoff_path": normalized}
        assert result["args"] == [normalized]

    def test_nonzero_exit_code_raises_rather_than_returning_a_failed_result(self, tmp_path, monkeypatch):
        """The swallow example-doctrine-repo-em reported 2026-07-29: `_invoke_op_in_process`
        returns the op's `_err(...)` dict as an ordinary value, and
        `apply_base.execute_directives` treats only a RAISED exception as
        failure -- so without this raise a failed stamp landed in `landed`
        under a green `APPLY_EXIT_OK`. Same third-generation shape already
        fixed for `_dispatch_handoff_author_fork`."""

        def _fake_invoke(op_name, params, repo_root):
            return {
                "exit_code": 1,
                "applied": False,
                "error": "handoff not found on disk: state/handoffs/gone.md",
            }

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        with pytest.raises(RuntimeError, match="handoff.stamp_phase failed to stamp"):
            ba_apply._dispatch_handoff_stamp_phase(["state/handoffs/gone.md"], tmp_path)

    def test_converged_no_op_does_not_raise(self, tmp_path, monkeypatch):
        """`applied:False` at `exit_code:0` is a legitimate already-converged
        no-op (byte-identical rewrite skipped by `locked_rmw`). Keying the
        fail posture on `applied` instead of `exit_code` would fail every
        re-run over an already-stamped handoff -- pinned so a later
        "tighten the predicate" edit cannot silently make that mistake."""

        def _fake_invoke(op_name, params, repo_root):
            return {"exit_code": 0, "applied": False, "message": "already converged"}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        result = ba_apply._dispatch_handoff_stamp_phase(
            ["state/handoffs/2026-07-27-some-slug.md"], tmp_path
        )
        assert result["result"]["applied"] is False

    def test_build_directives_for_kind_handoff_does_not_currently_emit_handoff_stamp_phase(self):
        lineage = {
            "kind": "handoff",
            "artifact_path": "state/handoffs/2026-07-27-x.md",
            "deliverable_id": None,
            "predecessor": None,
            "predecessor_id": None,
        }
        directives = ba._build_directives("handoff", lineage)
        clis = [d["cli"] for d in directives]
        assert "handoff.stamp_phase" not in clis


# ---------------------------------------------------------------------------
# C1 -- the push-side succession writer (d6, "handoff.supersede_predecessor").
# Spec backlink: docs/plans/2026-07-26-push-side-write-discipline.md, chunk C1.
#
# Three required assertions:
#   (a) continuation -> predecessor stamped `continued` + `continued_into`
#       AND archived, in the same transaction;
#   (b) fork via author_fork -> origin UNTOUCHED;
#   (c) superseded=False -> the mint FAILS and no successor file is left on
#       disk.
# ---------------------------------------------------------------------------


class TestD6EmissionDiscriminator:
    """The MECHANICAL discriminator per the dispatch brief: `kind ==
    "handoff" and lineage["predecessor"] is not None` -- never the
    `j-continuation-vs-fork` judgment point, never fired for a fork."""

    def test_d6_emitted_for_handoff_with_named_predecessor(self, tmp_path):
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" in clis
        d6 = next(d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor")
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        d1_out = next(a for a in d1["args"] if a.startswith("--out="))[len("--out="):]
        assert d6["depends_on"] == ["d1"]
        assert d6["args"][0] == str(predecessor.relative_to(tmp_path))
        # continued_into and exclude are the SAME value: this successor's
        # own FRESH output_path (what d1 actually scaffolds it at) -- NOT
        # `decision["artifact"]["path"]`, which is the INPUT `artifact_path`
        # (2026-07-27 follow-up fix; threading the input here silently
        # corrupted `continued_into`, see `_build_directives`'s d6 block).
        assert d6["args"][1] == d6["args"][2] == decision["artifact"]["lineage"]["output_path"]
        assert d6["args"][1] == d1_out
        assert d6["args"][1] != decision["artifact"]["path"]

    def test_d6_successor_path_is_never_the_plan_input_artifact_path(self, tmp_path):
        """2026-07-27 follow-up regression: the reproduced live-incident
        shape from `TestD1OutNeverEqualsInputArtifact` (a PLAN, not a
        handoff, fed as `artifact_path` for kind="handoff" -- the
        plan->execute execution-handoff trigger), but asserting d6's
        threaded successor path specifically. Before the fix, `successor_path
        = lineage.get("artifact_path")` would have stamped the predecessor's
        `continued_into` with the PLAN's own path -- silent-corruption-
        shaped: passes schema validation, rots lineage silently."""
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-priority-ledger.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        d6 = next(d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor")
        assert d6["args"][1] != str(plan)
        assert d6["args"][1] != "docs/plans/2026-07-26-priority-ledger.md"
        assert d6["args"][1] == decision["artifact"]["lineage"]["output_path"]

    def test_d6_not_emitted_for_handoff_with_predecessor_none(self, tmp_path):
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis

    def test_d6_not_emitted_for_spinoff_fork_kind(self, tmp_path):
        """(b) fork via author_fork -> origin UNTOUCHED. Since kind=="spinoff"
        never reaches the d6-emitting branch at all, the origin handoff's
        `handoff.author_fork` directive is the ONLY artifact-authoring
        directive spinoff ever gets -- there is no mechanism here that could
        touch the origin's own disposition."""
        origin = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-2-1a2b3d"],
        )
        decision = ba.brief("spinoff", str(origin), repo_root=tmp_path).decision_object
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis
        assert "handoff.author_fork" in clis


class TestD6IsLastEmittedForSafeStranding:
    """2026-07-29, break-class fix: d6 (`handoff.supersede_predecessor`) is
    the ONLY directive in a continuation-handoff brief that mutates a
    DIFFERENT, pre-existing artifact -- the predecessor. `order_by_depends_on`
    (`coordinator_core/contract/apply_base.py`) is a STABLE topological sort
    that breaks ties on the directives' ORIGINAL LIST ORDER, so emission
    order here dictates execution order. `apply_base.execute_directives` has
    NO rollback: a raised handler exception mid-run returns
    APPLY_EXIT_PARTIAL_MUTATION with whatever already landed. With d6 emitted
    before d5 (the old order), a later directive's failure (e.g. d5) left the
    predecessor already stamped deployment_state:continued +
    continued_into:<successor> pointing at a successor a subsequent failure
    could leave never fully populated -- a corrupted succession graph, not a
    merely-partial mint (observed live, 2026-07-29).

    d6 must be LAST: this asserts it by POSITION, not just membership, and
    is written to fail loudly (not silently mis-order) if someone moves d6
    earlier again -- the position is load-bearing safety, not incidental
    list order."""

    def test_d6_is_the_final_directive_for_a_continuation_handoff(self, tmp_path):
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        directive_ids = [d["id"] for d in decision["directives"]]
        assert directive_ids[-1] == "d6", (
            "d6 (handoff.supersede_predecessor) MUST be the last emitted "
            "directive -- it is the only one that mutates a pre-existing "
            "artifact (the predecessor), and apply_base.execute_directives "
            "has no rollback. Moving d6 earlier re-opens the exact stranding "
            "defect (predecessor stamped continued_into a successor a later "
            "directive's failure could leave incomplete) this ordering was "
            "fixed to close on 2026-07-29 -- see _build_directives's own "
            "'ORDERING' comment before reordering this again."
        )
        # d5 (plan-claim release) must precede d6, not the reverse -- this is
        # the actual reorder under test, not merely "d6 exists somewhere".
        assert directive_ids.index("d5") < directive_ids.index("d6")

    def test_d6_depends_on_d1_unchanged_by_the_reorder(self, tmp_path):
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-1-1a2b3c"],
        )
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        d6 = next(d for d in decision["directives"] if d["id"] == "d6")
        assert d6["depends_on"] == ["d1"]

    def test_fork_kind_emits_no_d6_and_the_reorder_is_a_no_op(self, tmp_path):
        origin = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-2-1a2b3d"],
        )
        decision = ba.brief("spinoff", str(origin), repo_root=tmp_path).decision_object
        ids = {d["id"] for d in decision["directives"]}
        assert "d6" not in ids


class TestDispatchHandoffAuthorFork:
    """Unit-level coverage of `_dispatch_handoff_author_fork` against a
    stubbed `_invoke_op_in_process` -- isolates this handler's OWN
    param-shaping from the real op's stamping behavior (which
    TestHandoffAuthorForkStampEndToEnd below exercises separately).

    2026-07-27 rewrite (Option A): d3 STAMPS d1's already-minted artifact
    instead of authoring a second file. Two previously-silent defects this
    closes: `origin_handoff` no longer gets bound into `title` (the
    from-scratch author path's own field -- irrelevant to stamping); and
    `origin_session` is no longer unpacked into a local and discarded --
    it now reaches `params["origin_session"]`.
    """

    def _capture(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_invoke(op_name, params, repo_root):
            captured["op_name"] = op_name
            captured["params"] = params
            return {"status": "ok"}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        return captured

    def test_builds_stamp_mode_params_with_handoff_path_and_all_five_origin_fields(
        self, tmp_path, monkeypatch
    ):
        captured = self._capture(monkeypatch)

        ba_apply._dispatch_handoff_author_fork(
            [
                "state/handoffs/origin.md",  # origin_handoff
                "hnd-origin-abc123",  # origin_handoff_id
                "sess-origin-1",  # origin_session
                "pln-origin-1",  # origin_plan_id
                "gol-a;gol-b",  # origin_goal_id (joined)
                "state/handoffs/2026-07-27-spun-off.md",  # handoff_path (d1's --out)
            ],
            tmp_path,
        )

        assert captured["op_name"] == "handoff.author_fork"
        assert captured["params"] == {
            "handoff_path": "state/handoffs/2026-07-27-spun-off.md",
            "origin_handoff": "state/handoffs/origin.md",
            "origin_handoff_id": "hnd-origin-abc123",
            "origin_session": "sess-origin-1",
            "origin_plan_id": "pln-origin-1",
            "origin_goal_id": ["gol-a", "gol-b"],
        }

    def test_origin_session_reaches_params_instead_of_being_discarded(self, tmp_path, monkeypatch):
        """Regression: previously `origin_session` was unpacked into a local
        and never placed into `params`, so it was silently dropped."""
        captured = self._capture(monkeypatch)

        ba_apply._dispatch_handoff_author_fork(
            ["", "", "sess-must-survive", "", "", "state/handoffs/x.md"], tmp_path
        )
        assert captured["params"]["origin_session"] == "sess-must-survive"

    def test_empty_args_degrade_to_none_goal_id_and_no_title_field(self, tmp_path, monkeypatch):
        """All-empty args (e.g. an origin artifact with no resolvable
        provenance) -> every origin_* field is None, origin_goal_id is None
        (not `[]`), and `title` is never a key in params (stamp mode has no
        use for it -- the target file already has its own title)."""
        captured = self._capture(monkeypatch)

        ba_apply._dispatch_handoff_author_fork(
            ["", "", "", "", "", "state/handoffs/y.md"], tmp_path
        )
        params = captured["params"]
        assert "title" not in params
        assert params["origin_handoff"] is None
        assert params["origin_handoff_id"] is None
        assert params["origin_session"] is None
        assert params["origin_plan_id"] is None
        assert params["origin_goal_id"] is None
        assert params["handoff_path"] == "state/handoffs/y.md"

    # -------------------------------------------------------------------
    # THIRD-GENERATION regression: a non-"ok" stamp result must raise, not
    # return normally -- see this handler's own docstring "THIRD-GENERATION
    # defect fixed here" paragraph and the author-fork-seam-repair spinoff.
    # Mirrors TestDispatchHandoffSupersedePredecessor's
    # test_superseded_false_raises_and_does_not_key_on_exit_code_zero below,
    # same FAIL POSTURE convention applied to this sibling directive.
    # -------------------------------------------------------------------

    def test_non_ok_stamp_result_raises_instead_of_returning_success(self, tmp_path, monkeypatch):
        """The exact defect this fix closes: `_handle_stamp` returning
        `{"exit_code": 1, "error": ...}` (e.g. `handoff_path not found on
        disk`) used to flow straight back to `apply_base.execute_directives`
        as an ordinary `detail` dict -- which only treats a RAISED exception
        as directive failure -- so the whole `apply()` run reported
        `APPLY_EXIT_OK` with the target's `origin_*` fields never written and
        no signal anywhere. A non-"ok" `status` must now raise."""

        def _fake_invoke(op_name, params, repo_root):
            return {"exit_code": 1, "error": "handoff_path not found on disk: state/handoffs/x.md"}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        with pytest.raises(RuntimeError, match="failed to stamp origin"):
            ba_apply._dispatch_handoff_author_fork(
                ["", "", "sess-x", "", "", "state/handoffs/x.md"], tmp_path
            )

    def test_ok_stamp_result_returns_normally_and_does_not_raise(self, tmp_path, monkeypatch):
        """Negative-control: a genuinely successful stamp (status: ok) must
        NOT raise -- the fix only tightens the failure path, it must not
        turn a correct stamp into a false-positive failure."""

        def _fake_invoke(op_name, params, repo_root):
            return {"status": "ok", "handoff_path": "state/handoffs/x.md"}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        detail = ba_apply._dispatch_handoff_author_fork(
            ["", "", "sess-x", "", "", "state/handoffs/x.md"], tmp_path
        )
        assert detail["result"]["status"] == "ok"
        assert detail["degraded"] is None

    def test_degraded_stamp_is_visible_at_top_level_of_returned_detail(
        self, tmp_path, monkeypatch, capsys
    ):
        """Review: code-reviewer (P1) -- a degrade still reports `status:
        "ok"`, so the RuntimeError branch above never fires for it, and
        prior to this fix `degraded` was nested two levels inside
        `result["result"]` with NO non-test consumer anywhere in the tree
        (confirmed by the reviewer's full-repo grep). A test that only
        checks the file's stamped fields would pass against that bug --
        this asserts the degrade is actually visible to the CALLER: at the
        top level of this directive's own returned dict (the same level
        `cli`/`args`/`result` already live, so `report["results"][i]
        ["detail"]["degraded"]` is a direct read in the printed apply()
        report), AND printed as an operator-visible advisory."""

        def _fake_invoke(op_name, params, repo_root):
            return {
                "status": "ok",
                "handoff_path": "state/handoffs/x.md",
                "degraded": [
                    {
                        "field": "origin_plan_id",
                        "reason": "ambiguous match (2 candidates) -- stamped null",
                        "candidates": [
                            {"plan_id": "pln-a", "title": "A", "score": 1.0},
                            {"plan_id": "pln-b", "title": "B", "score": 1.0},
                        ],
                    }
                ],
            }

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        detail = ba_apply._dispatch_handoff_author_fork(
            ["", "", "sess-x", "", "", "state/handoffs/x.md"], tmp_path
        )
        assert detail["degraded"] is not None
        assert detail["degraded"][0]["field"] == "origin_plan_id"
        # Same object the op returned -- not merely re-derived/duplicated.
        assert detail["degraded"] is detail["result"]["degraded"]

        stderr = capsys.readouterr().err
        assert "degraded" in stderr
        assert "origin_plan_id" in stderr

    def test_degraded_stderr_surfaces_engine_reason_not_stale_ambiguous_wording(
        self, tmp_path, monkeypatch, capsys
    ):
        """The printer must carry the ENGINE's own `reason` string (which
        distinguishes `below-threshold` -- nothing scored high enough, no
        judgment call happened -- from `too-close` -- a genuine tie)
        rather than a hardcoded "(ambiguous match, ...)" label that is now
        wrong for both new match_core.ResolutionReason cases."""

        def _fake_invoke(op_name, params, repo_root):
            return {
                "status": "ok",
                "handoff_path": "state/handoffs/x.md",
                "degraded": [
                    {
                        "field": "origin_plan_id",
                        "reason": (
                            "below-threshold: no candidate scored high enough "
                            "to auto-resolve (1 candidate(s) ranked, top score "
                            "below min_score=0.5) -- stamped null"
                        ),
                        "candidates": [
                            {"plan_id": "pln-a", "title": "A", "score": 0.1},
                        ],
                    }
                ],
            }

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        ba_apply._dispatch_handoff_author_fork(
            ["", "", "sess-x", "", "", "state/handoffs/x.md"], tmp_path
        )

        stderr = capsys.readouterr().err
        assert "below-threshold" in stderr
        assert "ambiguous match" not in stderr

    def test_failed_stamp_surfaces_as_partial_mutation_through_apply_base(
        self, tmp_path, monkeypatch
    ):
        """Integration-level: the failure must reach the operator through
        the REAL `apply_base.execute_directives` seam, not merely raise in
        isolation -- proving the caller-visible contract (`APPLY_EXIT_
        PARTIAL_MUTATION`, `failed_directive == "d3"`) that would have
        caught all three generations of this bug (see the handler's own
        docstring history)."""

        def _fake_invoke(op_name, params, repo_root):
            if op_name == "handoff.author_fork":
                return {"exit_code": 1, "error": "handoff_path not found on disk"}
            return {"status": "ok"}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        directives = [
            {
                "id": "d3",
                "cli": "handoff.author_fork",
                "args": ["", "", "sess-x", "", "", "state/handoffs/x.md"],
                "depends_on": None,
                "already_satisfied": False,
            }
        ]
        exit_code, report = ba_apply._execute_directives(directives, [], tmp_path)
        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["failed_directive"] == "d3"
        assert "d3" not in report["landed"]

    # -------------------------------------------------------------------
    # Fail-loud args-length guard: the old `(list(args) + [...6 blanks])[:6]`
    # shape silently padded a too-short `args` and silently truncated a
    # too-long one -- the same silent-positional-shape-mismatch class that
    # already bit this handler twice (see the handler's own docstring
    # "Previous defects fixed here"). A future edit to d3's emission that
    # adds/removes a lineage field without updating this unpack in lockstep
    # must now fail loudly instead of a third time silently.
    # -------------------------------------------------------------------

    def test_too_few_args_raises_instead_of_silently_padding(self, tmp_path, monkeypatch):
        captured = self._capture(monkeypatch)

        with pytest.raises(ValueError, match="expects exactly 6"):
            ba_apply._dispatch_handoff_author_fork(
                ["state/handoffs/origin.md", "hnd-origin-abc123"], tmp_path
            )
        assert "params" not in captured

    def test_too_many_args_raises_instead_of_silently_truncating(self, tmp_path, monkeypatch):
        captured = self._capture(monkeypatch)

        with pytest.raises(ValueError, match="expects exactly 6"):
            ba_apply._dispatch_handoff_author_fork(
                ["", "", "sess-x", "", "", "state/handoffs/x.md", "unexpected-seventh"],
                tmp_path,
            )
        assert "params" not in captured


class TestDispatchHandoffSupersedePredecessor:
    """Unit-level coverage of `_dispatch_handoff_supersede_predecessor`
    against a stubbed `_invoke_op_in_process` -- isolates this handler's OWN
    param-shaping and fail-posture logic from the real op's behavior (which
    the end-to-end test below exercises separately)."""

    def test_passes_mode_supersede_and_matching_continued_into_and_exclude(self, tmp_path, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_invoke(op_name, params, repo_root):
            captured["op_name"] = op_name
            captured["params"] = params
            return {"exit_code": 0, "superseded": True, "moved": True}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        _seed_claimed_predecessor(tmp_path)

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            ["state/handoffs/predecessor.md", "state/handoffs/successor.md", "state/handoffs/successor.md"],
            tmp_path,
        )

        assert captured["op_name"] == "handoff.archive_transition"
        assert captured["params"] == {
            "handoff_path": "state/handoffs/predecessor.md",
            "mode": "supersede",
            # `continued_into` stays repo-relative (it is frontmatter);
            # `exclude` is ABSOLUTE because `dag.referenced_by` resolves a
            # relative exclude against the process CWD, not the worktree root
            # -- see the handler's own comment at the params dict.
            "continued_into": "state/handoffs/successor.md",
            "exclude": [str(tmp_path / "state" / "handoffs" / "successor.md")],
        }
        assert result["result"]["superseded"] is True

    def test_superseded_false_raises_and_does_not_key_on_exit_code_zero(self, tmp_path, monkeypatch):
        """(c) part 1: a graceful-retain outcome (op returns exit_code:0,
        superseded:False -- e.g. an unrelated live child) must FAIL this
        directive, not silently succeed because rc looked clean."""

        def _fake_invoke(op_name, params, repo_root):
            return {
                "exit_code": 0,
                "superseded": False,
                "retained": True,
                "retain_reason": "unrelated live child",
            }

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        _seed_claimed_predecessor(tmp_path)

        with pytest.raises(RuntimeError, match="did not supersede"):
            ba_apply._dispatch_handoff_supersede_predecessor(
                ["state/handoffs/predecessor.md", "state/handoffs/successor.md", "state/handoffs/successor.md"],
                tmp_path,
            )

    def test_superseded_false_deletes_the_already_scaffolded_successor_file(self, tmp_path, monkeypatch):
        """(c) part 2: no successor file is left on disk. d1 (scaffold) runs
        BEFORE d6 in dependency order, so by the time this handler fires the
        successor already exists on disk -- a failed supersede must remove
        it rather than leaving a stranded, uncommitted file behind (apply_base
        performs no automatic rollback of its own)."""
        successor_rel = "state/handoffs/2026-07-27-successor.md"
        successor_abs = _render_real_scaffold(tmp_path / successor_rel)

        def _fake_invoke(op_name, params, repo_root):
            return {"exit_code": 0, "superseded": False, "retained": True}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        # REQUIRED, not incidental: without a claimed predecessor this test
        # never reached the `superseded is False` path it names -- it stopped at
        # the DR-242 gate, whose own (then-)cleanup deleted the file and made
        # the assertions below pass for the wrong reason. That gate degrades
        # rather than raising as of 2026-08-03, which is what surfaced the
        # mis-seeded fixture.
        _seed_claimed_predecessor(tmp_path)

        assert successor_abs.exists()
        with pytest.raises(RuntimeError):
            ba_apply._dispatch_handoff_supersede_predecessor(
                ["state/handoffs/predecessor.md", successor_rel, successor_rel], tmp_path
            )
        assert not successor_abs.exists()

    def test_superseded_false_with_missing_successor_file_does_not_crash(self, tmp_path, monkeypatch):
        """Defensive: the successor file may not exist yet (e.g. d1 itself
        failed upstream in a way that still let d6 be attempted in a
        hand-constructed directive list) -- unlink is best-effort, not a
        second failure mode."""

        def _fake_invoke(op_name, params, repo_root):
            return {"exit_code": 0, "superseded": False}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)
        # See the sibling above: a claimed predecessor is what makes this test
        # reach the `superseded is False` raise it is named for.
        _seed_claimed_predecessor(tmp_path)

        with pytest.raises(RuntimeError):
            ba_apply._dispatch_handoff_supersede_predecessor(
                ["state/handoffs/predecessor.md", "state/handoffs/nonexistent.md", "state/handoffs/nonexistent.md"],
                tmp_path,
            )

    def test_exception_from_invoke_op_still_deletes_scaffolded_successor_and_reraises(
        self, tmp_path, monkeypatch
    ):
        """(2026-07-27 review Finding 2) The cleanup-then-raise contract this
        handler's docstring promises ("Also removes the successor artifact
        ... before raising") must hold when `_invoke_op_in_process` itself
        raises -- not only when it returns a graceful `superseded: False`
        dict. Before this fix, an exception here propagated straight out,
        skipping the successor-cleanup entirely and stranding the
        already-scaffolded successor file on disk."""
        successor_rel = "state/handoffs/2026-07-27-successor-exc.md"
        successor_abs = _render_real_scaffold(tmp_path / successor_rel)

        def _fake_invoke_raises(op_name, params, repo_root):
            raise RuntimeError("boom")

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke_raises)
        _seed_claimed_predecessor(tmp_path)

        assert successor_abs.exists()
        with pytest.raises(RuntimeError, match="boom"):
            ba_apply._dispatch_handoff_supersede_predecessor(
                ["state/handoffs/predecessor.md", successor_rel, successor_rel], tmp_path
            )
        assert not successor_abs.exists()

    def test_never_claimed_predecessor_refuses_before_dispatch_and_names_dr242(
        self, tmp_path, monkeypatch
    ):
        """C5/AC8 negative case -- the entire point of the DR-242 gate: a
        predecessor that was never claimed or shipped is refused BEFORE
        `handoff.archive_transition` is ever dispatched, even though it is
        named as this successor's `continued_into`/predecessor -- exactly
        the successor-named-child evidence DR-242 (`docs/decisions/DR-242-
        successor-named-child-is-not-evidence-of-succ.md`) forbids treating
        as sufficient on its own.

        The refusal DEGRADES as of 2026-08-03 -- the op is still never
        dispatched, and the already-scaffolded successor is now left where d1
        put it. Why deleting it was break-class:
        `baton_assemble/tests/test_apply_degrade_no_compensation.py`."""
        successor_rel = "state/handoffs/2026-07-28-successor-refused.md"
        successor_abs = _render_real_scaffold(tmp_path / successor_rel)

        _write_artifact(tmp_path / "state/handoffs/predecessor.md", ["title: never claimed"])

        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(op_name, params, repo_root):
            calls.append((op_name, params))
            return {"exit_code": 0, "superseded": True, "moved": True}

        monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _fake_invoke)

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            ["state/handoffs/predecessor.md", successor_rel, successor_rel], tmp_path
        )

        assert calls == []
        assert successor_abs.exists()
        assert result["degraded"]["reason"] == "predecessor-not-claimed-or-shipped"


class TestHandoffSupersedePredecessorEndToEnd:
    """(a) + (b), end to end against the REAL `handoff.archive_transition`
    op (registered via apply.py's own top-of-module import) and a real git
    repo -- not a mock. Proves the full transaction: predecessor stamped
    `continued`/`continued_into` AND archived (git mv'd out of
    state/handoffs/), all from ONE `apply()` call minting the successor."""

    def _init_repo(self, repo: Path) -> None:
        _init_repo(repo)

    def test_continuation_supersedes_and_archives_predecessor_in_one_transaction(self, tmp_path, monkeypatch):
        for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))

        repo = tmp_path / "repo"
        self._init_repo(repo)

        predecessor = _write_artifact(
            repo / "state" / "handoffs" / "predecessor.md",
            [
                "handoff_id: hnd-pred-1a2b4c",
                "deployment_state: in_flight",
                "title: Predecessor handoff",
                "created: 2026-07-27",
                "branch: work/test/2026-01-01",
                'predecessor: "none"',
                "category: infra",
                "summary: predecessor handoff for the d6 end-to-end test",
                "claimed_at: 2026-07-27T09:00:00Z",
                "claimed_by: test-session",
            ],
        )
        _git(repo, "add", "state/handoffs/predecessor.md")
        _git(repo, "commit", "-m", "add predecessor")

        successor_rel = "state/handoffs/successor.md"
        successor_abs = repo / successor_rel
        successor_abs.parent.mkdir(parents=True, exist_ok=True)
        successor_abs.write_text("scaffolded-by-d1\n", encoding="utf-8")

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            ["state/handoffs/predecessor.md", successor_rel, successor_rel], repo
        )

        assert result["result"]["superseded"] is True
        assert result["result"]["moved"] is True

        # Archived: no longer in state/handoffs/.
        assert not predecessor.exists()

        # Landed under archive/handoffs/<YYYY-MM>/ with the supersede stamp.
        archive_root = repo / "archive" / "handoffs"
        archived_files = list(archive_root.rglob("predecessor.md"))
        assert archived_files, f"expected predecessor.md archived under {archive_root}"
        archived_text = archived_files[0].read_text(encoding="utf-8")
        assert "deployment_state: continued" in archived_text
        assert "continued_into:" in archived_text
        assert successor_rel in archived_text

    def test_predecessor_already_archived_by_boot_sweep_is_stamped_in_place(
        self, tmp_path, monkeypatch
    ):
        """2026-07-28 d6-archived-predecessor fix: the NORMAL `/handoff` shape
        is that the session boot sweep already archived the predecessor
        BEFORE d6 ever runs -- `predecessor_path` names an archive/handoffs/
        path from the start, not a state/handoffs/ one. Before the fix this
        was a hard usage-error refusal inside `handoff.archive_transition`
        (`handoff_path escapes state/handoffs/`); the fix teaches that op to
        stamp an already-archived target IN PLACE instead. This proves the
        full `_dispatch_handoff_supersede_predecessor` composition against a
        `predecessor_path` that already lives under archive/handoffs/."""
        for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))

        repo = tmp_path / "repo"
        self._init_repo(repo)

        archived_rel = "archive/handoffs/2026-07/predecessor.md"
        predecessor = _write_artifact(
            repo / archived_rel,
            [
                "handoff_id: hnd-pred-archived-1a2b4d",
                "deployment_state: shipped",
                "shipped_in: deadbeef",
                "title: Already-archived predecessor",
                "created: 2026-07-20",
                "branch: work/test/2026-01-01",
                'predecessor: "none"',
                "category: infra",
                "summary: predecessor archived by the boot sweep before d6 runs",
                "claimed_at: 2026-07-20T09:00:00Z",
                "claimed_by: test-session",
            ],
        )
        _git(repo, "add", archived_rel)
        _git(repo, "commit", "-m", "boot sweep: archive predecessor")

        successor_rel = "state/handoffs/successor.md"
        successor_abs = repo / successor_rel
        successor_abs.parent.mkdir(parents=True, exist_ok=True)
        successor_abs.write_text("scaffolded-by-d1\n", encoding="utf-8")

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [archived_rel, successor_rel, successor_rel], repo
        )

        assert result["result"]["superseded"] is True, result
        assert result["result"]["moved"] is False, result

        # Stamped in place -- still at the SAME archive path, never moved.
        assert predecessor.exists()
        archived_text = predecessor.read_text(encoding="utf-8")
        assert "deployment_state: continued" in archived_text
        assert "continued_into:" in archived_text
        assert successor_rel in archived_text
        # The successor scaffold (d1's own work) is untouched by this directive.
        assert successor_abs.read_text(encoding="utf-8") == "scaffolded-by-d1\n"

    def test_fork_path_leaves_origin_completely_untouched(self, tmp_path, monkeypatch):
        """(b): the fork path never calls `_dispatch_handoff_supersede_predecessor`
        at all -- brief()'s own directive emission for kind=="spinoff" never
        includes "handoff.supersede_predecessor" (see
        TestD6EmissionDiscriminator.test_d6_not_emitted_for_spinoff_fork_kind).
        This test pins the disk-level consequence: the origin handoff a
        spinoff forks from is byte-identical before and after `brief()`+the
        directive set is computed for kind="spinoff"."""
        for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))

        repo = tmp_path / "repo"
        self._init_repo(repo)

        origin = _write_artifact(
            repo / "state" / "handoffs" / "origin.md",
            ["deliverable_id: DEL-2", "handoff_id: hnd-2-1a2b3d"],
        )
        _git(repo, "add", "state/handoffs/origin.md")
        _git(repo, "commit", "-m", "add origin")
        before = origin.read_text(encoding="utf-8")

        decision = ba.brief("spinoff", str(origin), repo_root=repo).decision_object
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis

        after = origin.read_text(encoding="utf-8")
        assert before == after



# ---------------------------------------------------------------------------
# 2026-07-29 finding: a partially-applied run (d1 scaffolds, a LATER
# directive fails) used to leave the scaffold on disk still carrying its
# untouched placeholder title/handoff_id while ADVERTISING pickup_ready:
# true. A first fix (`_degrade_placeholder_scaffold_after_partial_failure`,
# retired by this same change) flipped `pickup_ready` to `false` but left
# the empty file behind forever -- one per failed run, since a re-run
# computes a FRESH `_compute_fresh_output_path` path rather than reusing a
# prior failed run's. This suite now covers the compensating-action seam
# (`apply_base.execute_directives`'s optional `compensators` parameter) and
# baton_assemble's own `_compensate_d1_scaffold`, which deletes the orphaned
# scaffold outright rather than merely flagging it.
# ---------------------------------------------------------------------------


def _render_real_scaffold(
    path: Path,
    *,
    doc_type: str = "handoff",
    title: str | None = None,
    predecessor: str | None = None,
    predecessor_id: str | None = None,
    deliverable_id: str | None = None,
    handoff_id: str = "hnd-fixture-000000",
    branch: str = "work/test/2026-01-01",
    body: str | None = None,
) -> Path:
    """Writes a scaffold byte-identically to what `coordinator-doc-new --out`
    would have written, by calling the generator's OWN scaffolder.

    Load-bearing, not fixture polish: `_is_pristine_generator_scaffold` (the
    Chunk B predicate) answers "does this file carry operator content beyond
    what the generator produced" by re-rendering the generator's template and
    comparing bytes. A hand-rolled approximation of a scaffold is, correctly,
    NOT pristine -- so a fixture that approximates one cannot exercise the
    delete branch at all. Passing `body` substitutes operator prose for the
    generator's section skeleton, which is what a preserved (non-pristine)
    scaffold looks like.
    """
    module = ba_apply._load_doc_new_module()
    kwargs: dict[str, Any] = {
        "title": title
        or (
            "PLACEHOLDER — replace with one-line handoff title"
            if doc_type == "handoff"
            else "PLACEHOLDER — replace with one-line spinoff title"
        ),
        "branch": branch,
        "deliverable_id": deliverable_id,
        "initiative": None,
        "handoff_id": handoff_id,
        "predecessor_id": predecessor_id,
        "category": None,
    }
    if doc_type == "handoff":
        kwargs["predecessor"] = predecessor
        content = module._scaffold_handoff(**kwargs)
    else:
        content = module._scaffold_spinoff(**kwargs)
    if body is not None:
        split = content.split("---\n", 2)
        content = f"---\n{split[1]}---\n\n{body}"
    if not content.endswith("\n"):
        content += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_placeholder_scaffold(path: Path) -> None:
    _render_real_scaffold(path)


class TestCompensateD1Scaffold:
    """Unit tests for `_compensate_d1_scaffold` in isolation -- the
    compensator function itself, called directly with a synthetic
    directive dict, mirroring the shape `apply_base.execute_directives`
    would pass it."""

    def _d1_directive(self, out_path: str, doc_type: str = "handoff") -> dict:
        """Mirrors `_build_directives`'s own d1 arg shape: `--type` is what tells
        the compensator WHICH scaffolder to re-render through, so a directive
        without it is not a d1 this compensator can reason about."""
        return {
            "id": "d1",
            "cli": "coordinator-doc-new",
            "args": [f"--type={doc_type}", f"--out={out_path}"],
        }

    def test_deletes_an_untouched_placeholder_scaffold(self, tmp_path):
        rel = "state/handoffs/2026-07-29-fresh.md"
        _write_placeholder_scaffold(tmp_path / rel)

        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        assert not (tmp_path / rel).exists()

    def test_deletes_a_titled_but_otherwise_untouched_scaffold(self, tmp_path):
        """The Chunk B correction, stated as a test. This case USED to survive:
        the retired predicate keyed on `coordinator-doc-new`'s no-`--title`
        template defaults, so supplying `--title` alone flipped the scaffold to
        "operator-customized" even when the operator had written nothing at all.
        A title is not content."""
        rel = "state/handoffs/2026-07-29-real-title.md"
        target = _render_real_scaffold(tmp_path / rel, title="a real, populated title")

        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        assert not target.exists()

    def test_leaves_a_scaffold_carrying_operator_body_content_alone(self, tmp_path):
        """The half that must NOT change: anything the generator did not write
        is operator work, and deleting it is the unrecoverable direction."""
        rel = "state/handoffs/2026-07-29-real-body.md"
        target = _render_real_scaffold(
            tmp_path / rel,
            title="a real, populated title",
            body="## What Was Accomplished\n\nReal operator prose.\n",
        )

        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        assert target.exists()
        assert "Real operator prose." in target.read_text(encoding="utf-8")

    def test_a_single_edited_frontmatter_field_is_enough_to_preserve(self, tmp_path):
        """The predicate is byte-exact against the re-render, not a
        body-only diff: replacing the placeholder `summary` -- a field the
        generator emits from its own constant, not from a parameter -- is
        operator content."""
        rel = "state/handoffs/2026-07-29-edited-summary.md"
        target = _render_real_scaffold(tmp_path / rel)
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "PLACEHOLDER — replace with one-line session summary (≤140 chars)",
                "a real summary the operator wrote",
            ),
            encoding="utf-8",
        )

        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        assert target.exists()

    def test_a_spinoff_scaffold_is_re_rendered_through_the_spinoff_scaffolder(
        self, tmp_path
    ):
        """`--type` selects the scaffolder, so kind=spinoff's own template is
        compared against `_scaffold_spinoff`, not the handoff one."""
        rel = "state/handoffs/2026-07-29-fork.md"
        pristine = _render_real_scaffold(tmp_path / rel, doc_type="spinoff")

        ba_apply._compensate_d1_scaffold(
            self._d1_directive(rel, doc_type="spinoff"), tmp_path, None
        )
        assert not pristine.exists()

        edited = _render_real_scaffold(
            tmp_path / rel, doc_type="spinoff", body="## What this covers\n\nMine.\n"
        )
        ba_apply._compensate_d1_scaffold(
            self._d1_directive(rel, doc_type="spinoff"), tmp_path, None
        )
        assert edited.exists()

    def test_an_unreadable_file_declines_to_delete(self, tmp_path, monkeypatch):
        """AC-2: uncertainty fails SAFE. An I/O error reading the candidate is
        not evidence the file is disposable."""
        rel = "state/handoffs/2026-07-29-unreadable.md"
        target = _render_real_scaffold(tmp_path / rel)

        def _boom(*a, **k):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", _boom)
        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        monkeypatch.undo()
        assert target.exists()

    def test_an_unavailable_generator_declines_to_delete(self, tmp_path, monkeypatch):
        """AC-2: the other uncertainty axis -- `claude_klabauter_bin` unresolvable (a
        corrupt settings-home fails `resolve_operator_config()` loud). Without
        the generator there is no re-render to compare against, so there is no
        proof the file is disposable."""
        rel = "state/handoffs/2026-07-29-no-generator.md"
        target = _render_real_scaffold(tmp_path / rel)
        # The loaded-module cache is process-wide, so it has to be emptied for
        # the duration of this test or the resolution below is never consulted.
        monkeypatch.setattr(ba_apply, "_DOC_NEW_MODULE", {})
        monkeypatch.setattr(
            ba_apply, "_resolve_claude_klabauter_bin", lambda: tmp_path / "does-not-exist"
        )

        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        assert target.exists()

    def test_an_unexpected_predicate_failure_declines_and_emits_advisory(
        self, tmp_path, monkeypatch, capsys
    ):
        """2026-07-30 (review: code-reviewer, Finding 2). A genuine bug in the
        predicate itself -- here simulated as an `AttributeError` out of
        `_render_pristine_scaffold` -- must be indistinguishable from a
        legitimate decline in NEITHER direction it matters: it still declines
        to delete (fail-safe direction unchanged), but unlike an expected
        refusal (`SystemExit`, generator-unavailable) it is no longer silent --
        it is surfaced on stderr so a silently-growing orphan count is
        diagnosable."""
        rel = "state/handoffs/2026-07-29-predicate-bug.md"
        target = _render_real_scaffold(tmp_path / rel)

        def _boom(*a, **k):
            raise AttributeError("simulated signature/field drift")

        monkeypatch.setattr(ba_apply, "_render_pristine_scaffold", _boom)

        ba_apply._compensate_d1_scaffold(self._d1_directive(rel), tmp_path, None)

        assert target.exists()
        stderr = capsys.readouterr().err
        assert "_is_pristine_generator_scaffold declined" in stderr
        assert str(target) in stderr
        assert "AttributeError" in stderr
        assert "simulated signature/field drift" in stderr

    def test_an_unknown_doc_type_declines_to_delete(self, tmp_path):
        """`_GENERATOR_SCAFFOLDERS` is a closed table; a doc_type outside it has
        no re-render path and therefore no deletion warrant."""
        rel = "state/handoffs/2026-07-29-odd-type.md"
        target = _render_real_scaffold(tmp_path / rel)

        ba_apply._compensate_d1_scaffold(
            self._d1_directive(rel, doc_type="recovery"), tmp_path, None
        )

        assert target.exists()

    def test_missing_type_flag_is_a_silent_noop(self, tmp_path):
        rel = "state/handoffs/2026-07-29-no-type.md"
        target = _render_real_scaffold(tmp_path / rel)

        ba_apply._compensate_d1_scaffold(
            {"id": "d1", "cli": "coordinator-doc-new", "args": [f"--out={rel}"]},
            tmp_path,
            None,
        )

        assert target.exists()

    def test_missing_file_is_a_silent_noop(self, tmp_path):
        # Must not raise -- best-effort cleanup over an already-failed run.
        ba_apply._compensate_d1_scaffold(
            self._d1_directive("state/handoffs/does-not-exist.md"), tmp_path, None
        )

    def test_missing_out_flag_is_a_silent_noop(self, tmp_path):
        ba_apply._compensate_d1_scaffold(
            {"id": "d1", "cli": "coordinator-doc-new", "args": []}, tmp_path, None
        )


class TestD1CompensatorEndToEnd:
    """End-to-end: `apply()` composes `_execute_directives`, which wires
    `_D1_COMPENSATORS` into `apply_base.execute_directives`'s `compensators`
    seam. These tests drive real directive dispatch (monkeypatched CLI
    handlers standing in for the actual `coordinator-doc-new`/subprocess
    calls) through `apply()` itself, proving the wiring end-to-end rather
    than the compensator function in isolation."""

    def _fake_brief_with_directives(self, rel: str, directives: list[dict]):
        def _fake_brief(kind, artifact_path, *, decisions=None, repo_root=None, title=None):
            class _FakeBriefResult:
                decision_object = {
                    "directives": directives,
                    "judgment_points": [],
                    "artifact": {"path": rel, "lineage": {"output_path": rel}},
                }

            return _FakeBriefResult()

        return _fake_brief

    def test_partial_mutation_deletes_d1_scaffold_and_reports_compensation(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        rel = "state/handoffs/2026-07-29-partial.md"

        def _fake_d1(args, repo_root):
            _write_placeholder_scaffold(repo_root / rel)
            return {"cli": "coordinator-doc-new", "args": args}

        def _fake_failing_lint(args, repo_root):
            raise RuntimeError("boom")

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _fake_failing_lint)
        monkeypatch.setattr(
            ba,
            "brief",
            self._fake_brief_with_directives(
                rel,
                [
                    {
                        "id": "d1",
                        "cli": "coordinator-doc-new",
                        "args": ["--type=handoff", f"--out={rel}"],
                        "already_satisfied": False,
                    },
                    {
                        "id": "d2",
                        "cli": "lint-frontmatter",
                        "args": [],
                        "already_satisfied": False,
                        "depends_on": ["d1"],
                    },
                ],
            ),
        )

        exit_code, report = ba_apply.apply(
            "handoff", rel, session_id="test-session", repo_root=tmp_path
        )

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert not (tmp_path / rel).exists()
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": True}
        ]
        # the original failure signal is unchanged/unmasked
        assert report["failed_directive"] == "d2"

    def test_partial_mutation_leaves_customised_scaffold_and_still_reports_compensation(
        self, tmp_path, monkeypatch
    ):
        """The compensator runs (nothing masks the seam firing) but its own
        guard leaves a scaffold carrying real operator content on disk --
        `succeeded` stays True (no exception), the file survives. The scaffold
        here is generator output with an EDITED BODY: `--title` alone no longer
        earns preservation (see
        `TestCompensateD1Scaffold::test_deletes_a_titled_but_otherwise_untouched_scaffold`)."""
        _init_repo(tmp_path)
        rel = "state/handoffs/2026-07-29-customised.md"

        def _fake_d1(args, repo_root):
            _render_real_scaffold(
                repo_root / rel,
                title="a real, populated title",
                body="## What Was Accomplished\n\nReal operator prose.\n",
            )
            return {"cli": "coordinator-doc-new", "args": args}

        def _fake_failing_lint(args, repo_root):
            raise RuntimeError("boom")

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _fake_failing_lint)
        monkeypatch.setattr(
            ba,
            "brief",
            self._fake_brief_with_directives(
                rel,
                [
                    {
                        "id": "d1",
                        "cli": "coordinator-doc-new",
                        "args": ["--type=handoff", f"--out={rel}"],
                        "already_satisfied": False,
                    },
                    {
                        "id": "d2",
                        "cli": "lint-frontmatter",
                        "args": [],
                        "already_satisfied": False,
                        "depends_on": ["d1"],
                    },
                ],
            ),
        )

        exit_code, report = ba_apply.apply(
            "handoff", rel, session_id="test-session", repo_root=tmp_path
        )

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert (tmp_path / rel).exists()
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": True}
        ]

    def test_no_compensation_key_when_d1_never_landed(self, tmp_path, monkeypatch):
        """d1 itself is the directive that failed -- nothing landed, so
        there is nothing to compensate and the compensation pass is a
        no-op list (the key is still present once compensators are
        supplied, but empty)."""
        _init_repo(tmp_path)
        rel = "state/handoffs/x.md"

        def _fake_failing_d1(args, repo_root):
            raise RuntimeError("d1 failed")

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_failing_d1)
        monkeypatch.setattr(
            ba,
            "brief",
            self._fake_brief_with_directives(
                rel,
                [
                    {
                        "id": "d1",
                        "cli": "coordinator-doc-new",
                        "args": ["--type=handoff", f"--out={rel}"],
                        "already_satisfied": False,
                    },
                ],
            ),
        )

        exit_code, report = ba_apply.apply(
            "handoff", rel, session_id="test-session", repo_root=tmp_path
        )

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["compensation"] == []

    def test_no_compensation_key_on_a_clean_run(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        rel = "state/handoffs/ok.md"

        def _fake_d1(args, repo_root):
            target = repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("---\ntitle: ok\n---\n\nBody.\n", encoding="utf-8")
            return {"cli": "coordinator-doc-new", "args": args}

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        monkeypatch.setattr(
            ba,
            "brief",
            self._fake_brief_with_directives(
                rel,
                [
                    {
                        "id": "d1",
                        "cli": "coordinator-doc-new",
                        "args": ["--type=handoff", f"--out={rel}"],
                        "already_satisfied": False,
                    },
                ],
            ),
        )

        exit_code, report = ba_apply.apply(
            "handoff", rel, session_id="test-session", repo_root=tmp_path
        )

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_OK
        assert "compensation" not in report

    def test_raising_compensator_is_recorded_and_does_not_mask_original_error(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        rel = "state/handoffs/2026-07-29-compensator-raises.md"

        def _fake_d1(args, repo_root):
            return {"cli": "coordinator-doc-new", "args": args}

        def _fake_failing_lint(args, repo_root):
            raise RuntimeError("d2 boom")

        def _raising_compensator(directive, repo_root, detail):
            raise OSError("compensator boom")

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _fake_failing_lint)
        monkeypatch.setitem(ba_apply._D1_COMPENSATORS, "d1", _raising_compensator)
        monkeypatch.setattr(
            ba,
            "brief",
            self._fake_brief_with_directives(
                rel,
                [
                    {
                        "id": "d1",
                        "cli": "coordinator-doc-new",
                        "args": ["--type=handoff", f"--out={rel}"],
                        "already_satisfied": False,
                    },
                    {
                        "id": "d2",
                        "cli": "lint-frontmatter",
                        "args": [],
                        "already_satisfied": False,
                        "depends_on": ["d1"],
                    },
                ],
            ),
        )

        exit_code, report = ba_apply.apply(
            "handoff", rel, session_id="test-session", repo_root=tmp_path
        )

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["error"] == "d2 boom"
        assert report["failed_directive"] == "d2"
        assert report["compensation"] == [
            {"directive_id": "d1", "attempted": True, "succeeded": False, "error": "compensator boom"}
        ]


class TestD1PredecessorThreading:
    """The PULL side of the succession edge: d1 must stamp the resolved
    predecessor ONTO the successor it scaffolds.

    d6 (TestD6EmissionDiscriminator above) covers the push side -- stamping
    `continued_into` onto the predecessor. Until 2026-07-29 only that half
    existed: `resolve_lineage` resolved `predecessor`/`predecessor_id`
    correctly and `_build_directives` dropped both, so every continuation
    baton this engine minted fell through to `coordinator-doc-new`'s
    hardcoded `predecessor: none` and an operator had to hand-supply the id
    the engine had already computed. These tests exist so that regression is
    caught at the directive layer rather than in a live handoff.
    """

    def _d1_predecessor_args(self, decision):
        d1 = next(d for d in decision["directives"] if d["id"] == "d1")
        return [a for a in d1["args"] if a.startswith("--predecessor")]

    def test_continuation_threads_both_predecessor_flags(self, tmp_path):
        """`lineage["predecessor"]` (kind="handoff", own_handoff_id branch)
        carries `artifact_path` VERBATIM -- absolute in this fixture, since
        `_write_artifact` returns an absolute `tmp_path`-rooted `Path` (see
        TestHandoffInputBecomesItsOwnPredecessor above). d1's own
        `--predecessor=` flag must NOT thread that absolute value verbatim,
        though: `_repo_relative_posix` renders it repo-relative first, since
        the `predecessor:` FRONTMATTER field is contractually repo-relative
        (schema_validate.py Rule C2-1b / dag.py's resolve_target) -- an
        absolute value would author an unwalkable, machine-specific edge."""
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md",
            ["handoff_id: hnd-pred-1a2b4c"],
        )
        decision = ba.brief(
            "handoff", str(predecessor), repo_root=tmp_path
        ).decision_object
        args = self._d1_predecessor_args(decision)
        assert "--predecessor=state/handoffs/predecessor.md" in args
        assert "--predecessor-id=hnd-pred-1a2b4c" in args

    def test_archived_predecessor_threads_the_archive_relative_path(self, tmp_path):
        """The archive-aware resolution fix (`_resolve_qualified_path_or_
        raise`) resolves a swept predecessor and reassigns
        `lineage["artifact_path"]`/`lineage["predecessor"]` to the archive-
        relative path it actually found -- d1 must thread THAT resolved
        value, not the original (now-nonexistent) live-looking input."""
        archived_dir = tmp_path / "archive" / "handoffs" / "2026-07"
        archived_dir.mkdir(parents=True)
        _write_artifact(
            archived_dir / "2026-07-20-earlier-session.md",
            ["deliverable_id: DEL-1", "handoff_id: hnd-archived-1a2b41", 'predecessor: "none"'],
        )
        live_path = tmp_path / "state" / "handoffs" / "2026-07-20-earlier-session.md"

        decision = ba.brief("handoff", str(live_path), repo_root=tmp_path).decision_object
        args = self._d1_predecessor_args(decision)
        assert (
            "--predecessor=archive/handoffs/2026-07/2026-07-20-earlier-session.md" in args
        )
        assert "--predecessor-id=hnd-archived-1a2b41" in args

    def test_predecessor_without_own_handoff_id_still_threads_the_path(self, tmp_path):
        """Mirrors `TestKindParametrizedCascade`'s plan->execute fixture: no
        `predecessor_id` resolves when the predecessor lacks its own
        `handoff_id` -- `--predecessor` still threads (the path IS known),
        `--predecessor-id` is correctly omitted (never sent empty)."""
        predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "predecessor.md", ["handoff_id: hnd-1-1a2b3c"]
        )
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-07-26-some-plan.md",
            [
                "deliverable_id: DEL-1",
                f"predecessor_handoff: {predecessor.relative_to(tmp_path)}",
            ],
        )
        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        args = self._d1_predecessor_args(decision)
        assert any(a.startswith("--predecessor=") for a in args)
        assert "--predecessor-id=hnd-1-1a2b3c" in args

    def test_fork_threads_neither_flag(self, tmp_path):
        """A fork (`predecessor` unresolved) must scaffold byte-identically
        to before this threading existed -- neither flag passed, not even
        an empty one, so `coordinator-doc-new` keeps its `none` default."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ["deliverable_id: DEL-1", 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        assert self._d1_predecessor_args(decision) == []

    def test_spinoff_never_threads_a_predecessor(self, tmp_path):
        """Spinoff kinds are predecessor:none-by-design (schema_validate.py
        Rule A3a-3 `_cf_spinoff_predecessor_none`); threading one would
        author a guaranteed validation failure."""
        origin = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["handoff_id: hnd-origin-1a2b4a"],
        )
        decision = ba.brief("spinoff", str(origin), repo_root=tmp_path).decision_object
        assert self._d1_predecessor_args(decision) == []

    def test_predecessor_id_is_never_passed_without_its_path(self, tmp_path):
        """`coordinator-doc-new` refuses `--predecessor-id` without
        `--predecessor` (they are the id and path halves of one edge, and
        the referential-integrity checker skips the comparison when the path
        is unset). This engine must never emit that refused combination."""
        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ["deliverable_id: DEL-1", 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        args = self._d1_predecessor_args(decision)
        has_id = any(a.startswith("--predecessor-id=") for a in args)
        has_path = any(
            a.startswith("--predecessor=") for a in args
        )
        assert not (has_id and not has_path)


# ---------------------------------------------------------------------------
# 2026-07-29 break-class fix -- IDEMPOTENT REPLAY.
#
# `apply_base.execute_directives` has no rollback and had no resume: a handler
# that raised mid-run returned APPLY_EXIT_PARTIAL_MUTATION and the operator was
# left hand-running the remaining directives one at a time (observed live,
# 2026-07-29, aborting at d4). The sanctioned resume is RE-RUNNING THE IDENTICAL
# COMMAND, made to converge by deriving `directives[].already_satisfied` from
# disk at brief() time -- no flag, no run-state file.
#
# The wedge these tests exist to pin: `_compute_fresh_output_path` deliberately
# disambiguates AWAY from any existing file, so a re-run used to mint a SECOND
# successor and hand d6 that new path as `continued_into`. `_supersede_continued`
# correctly refuses to overwrite one real succession edge with a different one,
# so d6 raised, the fresh scaffold was deleted, and EVERY subsequent attempt
# failed identically -- a permanent wedge, with the predecessor's
# `continued_into` pointing at the abandoned attempt's successor forever.
# ---------------------------------------------------------------------------

_PREDECESSOR_FM = [
    "handoff_id: hnd-pred-1a2b4c",
    "deployment_state: in_flight",
    "title: Predecessor handoff",
    "created: 2026-07-27",
    "branch: work/test/2026-01-01",
    'predecessor: "none"',
    "category: infra",
    "summary: predecessor handoff for the idempotent-replay suite",
    "claimed_at: 2026-07-27T09:00:00Z",
    "claimed_by: test-session",
]

_PRED_REL = "state/handoffs/predecessor.md"


class _ReplayHarness:
    """One place that knows how to drive a WHOLE `apply()` run for kind=handoff
    against a real git repo and the REAL `handoff.archive_transition` op, with
    only the four subprocess-shaped directives (d1/d2/d4/d5) faked.

    d6 is deliberately never faked -- it is the directive whose replay behaviour
    is under test, and its convergence comes from `_supersede_continued`'s own
    idempotency branch, which a mock would simply assert away.
    """

    def __init__(self, tmp_path: Path, monkeypatch, *, predecessor_fm=None):
        self.repo = tmp_path / "repo"
        _init_repo(self.repo)
        self.monkeypatch = monkeypatch
        for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))

        _write_artifact(
            self.repo / _PRED_REL,
            list(_PREDECESSOR_FM if predecessor_fm is None else predecessor_fm),
        )
        _git(self.repo, "add", _PRED_REL)
        _git(self.repo, "commit", "-m", "add predecessor")

        self.d1_calls: list[str] = []
        self.fail_at: str | None = None
        self.d1_title: str | None = None
        self.d1_body: str | None = None
        self._install_fakes()

    def _install_fakes(self) -> None:
        def _fake_d1(args, repo_root):
            """Stands in for the `coordinator-doc-new` SUBPROCESS only -- the
            bytes it writes come from the generator's own scaffolder, threading
            through the very `--title`/`--predecessor`/`--predecessor-id`/
            `--deliverable-id` values `_build_directives` emitted. That
            fidelity is required, not cosmetic: both Chunk B's pristine-render
            predicate and Chunk A's adoption candidate-match read fields this
            engine asked d1 to stamp, so a fake writing approximate frontmatter
            would silently exercise neither."""
            out = next(a[len("--out="):] for a in args if a.startswith("--out="))
            self.d1_calls.append(out)
            if self.fail_at == "d1":
                raise RuntimeError("fake d1 failure")

            def _flag(name: str) -> str | None:
                prefix = f"--{name}="
                return next(
                    (a[len(prefix):] or None for a in args if a.startswith(prefix)), None
                )

            _render_real_scaffold(
                repo_root / out,
                doc_type=_flag("type") or "handoff",
                title=_flag("title") or self.d1_title,
                predecessor=_flag("predecessor"),
                predecessor_id=_flag("predecessor-id"),
                deliverable_id=_flag("deliverable-id"),
                body=self.d1_body,
            )
            return {"cli": "coordinator-doc-new", "args": args}

        def _noop(name):
            def _fake(args, repo_root):
                if self.fail_at == name:
                    raise RuntimeError(f"fake {name} failure")
                return {"cli": name, "args": args}

            return _fake

        self.monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        self.monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _noop("d2"))
        self.monkeypatch.setitem(ba_apply._CLI_DISPATCH, "render-project-tracker", _noop("d4"))
        self.monkeypatch.setitem(ba_apply._CLI_DISPATCH, "session-claim-cli", _noop("d5"))

    def run(self) -> tuple[int, dict]:
        """Re-runs the IDENTICAL invocation every time -- that identity is the
        resume contract, so the harness offers no way to vary it."""
        return ba_apply.apply(
            "handoff", _PRED_REL, session_id="test-session", repo_root=self.repo
        )

    # -- disk oracles ----------------------------------------------------
    def live_handoffs(self) -> list[str]:
        live = self.repo / "state" / "handoffs"
        return sorted(p.name for p in live.glob("*.md")) if live.is_dir() else []

    def archived_predecessor(self) -> Path | None:
        archive_root = self.repo / "archive" / "handoffs"
        if not archive_root.is_dir():
            return None
        found = list(archive_root.rglob("predecessor.md"))
        return found[0] if found else None

    def predecessor_text(self) -> str:
        archived = self.archived_predecessor()
        target = archived if archived is not None else self.repo / _PRED_REL
        return target.read_text(encoding="utf-8")

    def continued_into(self) -> str | None:
        from coordinator_core.frontmatter.primitives import (
            read_fm_field_unquoted,
            split_frontmatter,
        )

        split = split_frontmatter(self.predecessor_text())
        return read_fm_field_unquoted(split.fm_text, "continued_into") if split else None

    def head_shas(self) -> list[str]:
        proc = _git(self.repo, "log", "--format=%H")
        return proc.stdout.split()


class TestCleanFirstRunIsUnchanged:
    """AC-3 non-regression: a first run over a never-superseded predecessor is
    behaviourally identical to its pre-replay shape -- same directives, same
    order, nothing skipped, one commit for the artifact plus d6's own."""

    def test_clean_run_lands_every_directive_in_order_and_skips_nothing(
        self, tmp_path, monkeypatch
    ):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert report["landed"] == ["d1", "d2", "d4", "d5", "d6"]
        assert report["replayed"] == []
        assert all(not r["already_satisfied"] for r in report["results"])
        assert report["commit_sha"]
        assert harness.archived_predecessor() is not None
        assert "deployment_state: continued" in harness.predecessor_text()

    def test_clean_run_directives_carry_no_already_satisfied_reason_key(
        self, tmp_path, monkeypatch
    ):
        """The reason key is emitted ONLY for a satisfied directive, so a clean
        run's directive dicts stay byte-identical to their pre-replay shape."""
        harness = _ReplayHarness(tmp_path, monkeypatch)
        decision = ba.brief("handoff", _PRED_REL, repo_root=harness.repo).decision_object
        for directive in decision["directives"]:
            assert directive["already_satisfied"] is False
            assert "already_satisfied_reason" not in directive
        assert decision["artifact"]["lineage"]["resumed_successor"] is None

    def test_clean_run_narration_carries_no_replay_suffix(self, tmp_path, monkeypatch):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        decision = ba.brief("handoff", _PRED_REL, repo_root=harness.repo).decision_object
        assert "REPLAY" not in decision["narration"]


class TestCleanRunCommitsTheMintedSuccessor:
    """The `_scoped_commit` half of the same 2026-07-29 incident, found by the
    suite above: `apply()` staged the path it READ (`artifact.path`) rather than
    the path it WROTE (`lineage["output_path"]`). In `/handoff`'s default shape
    that path is the predecessor -- which d6 has just `git mv`'d into
    `archive/handoffs/` and committed -- so `git add -- <it>` returned rc=128 and
    `apply()` died on an uncaught RuntimeError with every directive landed and
    the successor sitting uncommitted. That is the "hand-commit the rest
    yourself" residue, and AC-1's "same COMMITTED end-state" is unstatable
    without it."""

    def test_apply_does_not_crash_and_the_successor_is_committed(self, tmp_path, monkeypatch):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        successor = harness.continued_into()
        assert successor

        # Committed, not merely written: the path has its own git history, and
        # `git status --porcelain` shows nothing outstanding for it.
        log = _git(harness.repo, "log", "--format=%H", "--", successor)
        assert log.stdout.split(), f"{successor} has no commit history"
        status = _git(harness.repo, "status", "--porcelain", "--", successor)
        assert status.stdout.strip() == ""
        assert report["commit_sha"] in log.stdout.split()

    def test_the_report_names_the_successor_not_the_input_as_what_it_committed(
        self, tmp_path, monkeypatch
    ):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        exit_code, report = harness.run()

        successor = harness.continued_into()
        own = [c for c in report["commits"] if c["sha"] == report["commit_sha"]]
        assert len(own) == 1
        assert successor in own[0]["what"]
        assert _PRED_REL not in own[0]["what"]


class TestD6AlreadySupersededWedge:
    """AC-2. The core of the fix. `test_..._wedges_when_resumption_is_disabled`
    is the RED half, pinned permanently rather than merely observed once: it
    neuters `_resume_recorded_successor_path` to reproduce pre-fix behaviour and
    asserts the wedge, so a future change that silently removes resumption fails
    here instead of shipping."""

    def test_rerun_after_a_completed_succession_wedges_when_resumption_is_disabled(
        self, tmp_path, monkeypatch
    ):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        assert harness.run()[0] == ba_apply.APPLY_EXIT_OK
        first_successor = harness.continued_into()

        monkeypatch.setattr(ba, "_resume_recorded_successor_path", lambda *a, **k: None)
        exit_code, report = harness.run()

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION, report
        assert report["failed_directive"] == "d6"
        assert "supersede conflict" in report["error"]
        # The abandoned attempt's successor is still what the predecessor names,
        # and no re-run can ever repoint it -- the wedge.
        assert harness.continued_into() == first_successor
        # d6's own inline cleanup removed the second scaffold it could not use.
        assert harness.live_handoffs() == [Path(first_successor).name]

    def test_rerun_after_a_completed_succession_converges(self, tmp_path, monkeypatch):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        assert harness.run()[0] == ba_apply.APPLY_EXIT_OK
        successor = harness.continued_into()
        successor_text = (harness.repo / successor).read_text(encoding="utf-8")
        shas_after_first = harness.head_shas()

        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert report["landed"] == ["d1", "d2", "d4", "d5", "d6"]
        assert [e["directive_id"] for e in report["replayed"]] == ["d1"]
        assert successor in report["replayed"][0]["reason"]
        # No duplicate mutation: one successor, same bytes, same edge.
        assert harness.live_handoffs() == [Path(successor).name]
        assert (harness.repo / successor).read_text(encoding="utf-8") == successor_text
        assert harness.continued_into() == successor
        # No duplicate commit -- nothing changed for either path to commit.
        assert report["commit_sha"] is None
        assert report["commits"] == []
        assert harness.head_shas() == shas_after_first

    def test_third_run_is_still_idempotent_and_does_not_oscillate(
        self, tmp_path, monkeypatch
    ):
        """AC-7."""
        harness = _ReplayHarness(tmp_path, monkeypatch)
        assert harness.run()[0] == ba_apply.APPLY_EXIT_OK
        successor = harness.continued_into()
        shas = harness.head_shas()

        second = harness.run()
        third = harness.run()

        assert second[0] == ba_apply.APPLY_EXIT_OK, second[1]
        assert third[0] == ba_apply.APPLY_EXIT_OK, third[1]
        assert third[1]["landed"] == second[1]["landed"]
        assert third[1]["replayed"] == second[1]["replayed"]
        assert harness.continued_into() == successor
        assert harness.live_handoffs() == [Path(successor).name]
        assert harness.head_shas() == shas
        # d1 dispatched exactly once across all three runs.
        assert len(harness.d1_calls) == 1


class TestReplayAfterPartialAbortBeforeD6:
    """AC-1 for the pre-d6 residue rows of the matrix. d6 is emitted LAST
    precisely so an earlier failure leaves the predecessor untouched; these
    tests pin that the re-run is then a clean run, not a second mint."""

    @pytest.mark.parametrize("fail_at", ["d2", "d4", "d5"])
    def test_placeholder_scaffold_is_compensated_and_the_rerun_converges(
        self, tmp_path, monkeypatch, fail_at
    ):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        harness.fail_at = fail_at
        exit_code, report = harness.run()

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION, report
        assert report["failed_directive"] == fail_at
        # d1's compensator removed the untouched placeholder, and d6 never ran,
        # so the predecessor carries no succession edge at all.
        assert harness.live_handoffs() == ["predecessor.md"]
        assert harness.continued_into() is None

        harness.fail_at = None
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert report["landed"] == ["d1", "d2", "d4", "d5", "d6"]
        # A clean re-run, not a replay -- there was no residue to resume.
        assert report["replayed"] == []
        assert harness.archived_predecessor() is not None
        successor = harness.continued_into()
        assert successor and (harness.repo / successor).is_file()
        assert harness.live_handoffs() == [Path(successor).name]

    def test_preserved_scaffold_is_adopted_on_rerun_and_the_open_edge_is_closed(
        self, tmp_path, monkeypatch
    ):
        """The residue row 76ee96ee left OPEN, now closed -- this test is the
        successor of `test_customised_scaffold_the_compensator_declines_is_the_
        known_open_edge`, which pinned the same scenario as an orphan-minting
        boundary.

        The scaffold here carries real operator body content, so
        `_compensate_d1_scaffold` correctly preserves it (Chunk B's pristine-
        render predicate declines), and an abort before d6 leaves NO
        predecessor-side evidence. Resumption therefore has nothing to read on
        the predecessor -- the ONLY disk fact identifying the survivor as this
        run's own prior attempt is the survivor's own `predecessor:` pointer,
        which DR-242's Amendment A1 carve-out admits for exactly this ONE
        decision: which path d1 writes. The re-run must ADOPT the survivor
        rather than mint beside it and orphan it."""
        harness = _ReplayHarness(tmp_path, monkeypatch)
        harness.d1_body = "## What Was Accomplished\n\nReal operator prose.\n"
        harness.fail_at = "d5"
        assert harness.run()[0] == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        stranded = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert len(stranded) == 1

        harness.fail_at = None
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        # d1 was skipped: its `--out` is the SURVIVING file, already on disk.
        assert [e["directive_id"] for e in report["replayed"]] == ["d1"]
        successor = harness.continued_into()
        assert successor is not None
        # The adopted file IS the successor -- no orphan beside it.
        assert Path(successor).name == stranded[0]
        assert harness.live_handoffs() == [stranded[0]]
        # The operator's own prose survived the adoption untouched.
        assert "Real operator prose." in (harness.repo / successor).read_text(
            encoding="utf-8"
        )


class TestDr242IsNotReachableViaAlreadySatisfied:
    """AC-5. Two independent teeth: resumption itself refuses a predecessor that
    was never claimed or shipped, and d6 carries no `already_satisfied` at all,
    so its own DR-242 gate cannot be skipped past."""

    def test_never_claimed_predecessor_is_still_refused_end_to_end(
        self, tmp_path, monkeypatch
    ):
        never_claimed = [
            line
            for line in _PREDECESSOR_FM
            if not line.startswith(("claimed_at:", "claimed_by:"))
        ]
        harness = _ReplayHarness(tmp_path, monkeypatch, predecessor_fm=never_claimed)
        exit_code, report = harness.run()

        # The REFUSAL is what AC-5 pins and it is intact: no succession edge is
        # written. As of 2026-08-03 it degrades rather than aborting the mint --
        # `baton_assemble/tests/test_apply_degrade_no_compensation.py`.
        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert harness.continued_into() is None
        assert harness.archived_predecessor() is None
        assert [d["directive_id"] for d in report["degraded"]] == ["d6"]
        assert report["replayed"] == []

    def test_d6_never_carries_already_satisfied_for_any_predecessor_state(
        self, tmp_path, monkeypatch
    ):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        assert harness.run()[0] == ba_apply.APPLY_EXIT_OK

        decision = ba.brief("handoff", _PRED_REL, repo_root=harness.repo).decision_object
        d6 = next(d for d in decision["directives"] if d["id"] == "d6")
        assert d6["already_satisfied"] is False
        assert "already_satisfied_reason" not in d6

    def test_successor_naming_its_predecessor_is_not_resumption_evidence(self, tmp_path):
        """DR-242's negative-spec, at the resumption seam: a predecessor whose
        ONLY evidence is a child pointing back at it resumes nothing."""
        _write_artifact(tmp_path / _PRED_REL, ["handoff_id: hnd-pred-1a2b4c", "deployment_state: in_flight"])
        _write_artifact(
            tmp_path / "state" / "handoffs" / "successor.md",
            ["handoff_id: hnd-succ-1a2b55", "predecessor: state/handoffs/predecessor.md"],
        )
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_continued_into_on_a_never_claimed_predecessor_resumes_nothing(self, tmp_path):
        """A hand-authored `continued_into` on a record that was never claimed
        and never reached a terminal state is not succession evidence either --
        `claimed_or_shipped_at_path` is the composed gate, and it fails closed."""
        _write_artifact(
            tmp_path / _PRED_REL,
            [
                "handoff_id: hnd-pred-1a2b4c",
                "deployment_state: in_flight",
                "continued_into: state/handoffs/successor.md",
            ],
        )
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None


class TestBriefStaysSideEffectFreeOnAReplay:
    """AC-6: the resumption read is a READ. `TestBriefIsReadOnly` already covers
    the first-run case; this covers the branch that actually parses a
    predecessor's succession stamp."""

    def test_replay_brief_writes_nothing_and_mutates_no_input(self, tmp_path, monkeypatch):
        harness = _ReplayHarness(tmp_path, monkeypatch)
        assert harness.run()[0] == ba_apply.APPLY_EXIT_OK

        files_before = {
            p.relative_to(harness.repo): p.read_bytes()
            for p in harness.repo.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }
        decision = ba.brief("handoff", _PRED_REL, repo_root=harness.repo).decision_object
        files_after = {
            p.relative_to(harness.repo): p.read_bytes()
            for p in harness.repo.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }

        assert files_before == files_after
        assert decision["artifact"]["lineage"]["resumed_successor"] is not None
        assert "REPLAY" in decision["narration"]


class TestResumeRecordedSuccessorPathUnit:
    """`_resume_recorded_successor_path` in isolation -- the value-shape
    tolerances and refusals its own negative-spec claims."""

    def _pred(self, root: Path, extra: list[str]) -> None:
        _write_artifact(
            root / _PRED_REL,
            ["handoff_id: hnd-pred-1a2b4c", "claimed_by: test-session", "deployment_state: continued"]
            + extra,
        )

    def test_returns_the_recorded_value_verbatim(self, tmp_path):
        self._pred(tmp_path, ["continued_into: state/handoffs/2026-07-29-succ.md"])
        assert (
            ba._resume_recorded_successor_path(_PRED_REL, tmp_path)
            == "state/handoffs/2026-07-29-succ.md"
        )

    def test_a_backslash_written_value_is_returned_unchanged_not_reserialized(self, tmp_path):
        """Byte-identity with disk is what keeps `_supersede_continued`'s
        string comparison converging across platforms."""
        self._pred(tmp_path, [r"continued_into: state\handoffs\2026-07-29-succ.md"])
        assert (
            ba._resume_recorded_successor_path(_PRED_REL, tmp_path)
            == r"state\handoffs\2026-07-29-succ.md"
        )

    def test_absolute_in_repo_value_is_admitted(self, tmp_path):
        target = tmp_path / "state" / "handoffs" / "abs-succ.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
        self._pred(tmp_path, [f'continued_into: "{target.as_posix()}"'])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) == target.as_posix()

    def test_out_of_repo_absolute_value_is_refused(self, tmp_path):
        outside = (tmp_path.parent / "elsewhere" / "succ.md").as_posix()
        self._pred(tmp_path, [f'continued_into: "{outside}"'])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_dot_dot_traversal_is_refused(self, tmp_path):
        self._pred(tmp_path, ["continued_into: ../outside/succ.md"])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_bare_handoff_id_value_is_refused(self, tmp_path):
        self._pred(tmp_path, ["continued_into: hnd-some-successor-abc123"])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    @pytest.mark.parametrize("value", ['"none"', "none", "null", "~", '""'])
    def test_empty_sentinel_values_are_refused(self, tmp_path, value):
        self._pred(tmp_path, [f"continued_into: {value}"])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_missing_continued_into_is_refused(self, tmp_path):
        self._pred(tmp_path, [])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_non_continued_deployment_state_is_refused(self, tmp_path):
        _write_artifact(
            tmp_path / _PRED_REL,
            [
                "handoff_id: hnd-pred-1a2b4c",
                "claimed_by: test-session",
                "deployment_state: in_flight",
                "continued_into: state/handoffs/succ.md",
            ],
        )
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_existing_archived_successor_is_admitted(self, tmp_path):
        archived = tmp_path / "archive" / "handoffs" / "2026-07" / "succ.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text("x\n", encoding="utf-8")
        self._pred(tmp_path, ["continued_into: archive/handoffs/2026-07/succ.md"])
        assert (
            ba._resume_recorded_successor_path(_PRED_REL, tmp_path)
            == "archive/handoffs/2026-07/succ.md"
        )

    def test_missing_archived_successor_is_refused_never_a_scaffold_target(self, tmp_path):
        """d1 must never be pointed at an `archive/` path that does not exist --
        scaffolding into the archive would author a record outside the live
        corpus every reader walks."""
        self._pred(tmp_path, ["continued_into: archive/handoffs/2026-07/gone.md"])
        assert ba._resume_recorded_successor_path(_PRED_REL, tmp_path) is None

    def test_missing_state_handoffs_successor_is_admitted_as_a_remint_target(self, tmp_path):
        """A `continued_into` whose target was deleted is STALE -- re-minting at
        that exact path is what makes it stop being stale, so it is admitted."""
        self._pred(tmp_path, ["continued_into: state/handoffs/2026-07-29-gone.md"])
        assert (
            ba._resume_recorded_successor_path(_PRED_REL, tmp_path)
            == "state/handoffs/2026-07-29-gone.md"
        )

    def test_missing_predecessor_file_is_refused(self, tmp_path):
        assert ba._resume_recorded_successor_path("state/handoffs/nope.md", tmp_path) is None

    def test_empty_predecessor_is_refused(self, tmp_path):
        assert ba._resume_recorded_successor_path("", tmp_path) is None


_THIS_RUN_SESSION = "sid-this-run"


class TestAdoptPriorAttemptScaffoldPathUnit:
    """`_adopt_prior_attempt_scaffold_path` in isolation -- the DR-242
    Amendment A1 carve-out's identification teeth. Every refusal here is a
    case where adopting would be a GUESS; the pre-existing fresh-mint
    behaviour stands instead.

    Every test in this class runs under a fixed, matching `CLAUDE_SESSION_ID`
    (`_THIS_RUN_SESSION`) via the autouse fixture below, and every candidate
    written by `_child` carries a matching `authoring_session` by default --
    so a pre-existing test asserting adoption keeps asserting adoption, and
    each authorship-specific case below overrides ONLY the one field it means
    to test. This mirrors the fix itself: the identification teeth already
    covered here are unchanged, authorship is one more required condition
    alongside them, not a replacement for them.
    """

    @pytest.fixture(autouse=True)
    def _this_run_session(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", _THIS_RUN_SESSION)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _pred(self, root: Path, extra: list[str] | None = None) -> None:
        _write_artifact(
            root / _PRED_REL,
            [
                "handoff_id: hnd-pred-1a2b4c",
                "claimed_by: test-session",
                "deployment_state: in_flight",
            ]
            + (extra or []),
        )

    def _child(self, root: Path, name: str, extra: list[str]) -> Path:
        """Writes a candidate with a matching `authoring_session` by default
        -- pass `authoring_session: ...` (or omit it) in `extra` explicitly to
        override, since a later duplicate key in the written frontmatter is
        harmless for `_read_frontmatter`'s last-value-wins read but callers
        that need "no authoring_session at all" must instead pass a sentinel
        the authorship-specific tests below use directly (they bypass this
        default via their own literal `extra` list)."""
        if not any(line.startswith("authoring_session:") for line in extra):
            extra = extra + [f"authoring_session: {_THIS_RUN_SESSION}"]
        return _write_artifact(root / "state" / "handoffs" / name, extra)

    def test_the_one_child_naming_this_predecessor_is_adopted(self, tmp_path):
        self._pred(tmp_path)
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            ["handoff_id: hnd-succ-1a2b55", f"predecessor: {_PRED_REL}", "predecessor_id: hnd-pred-1a2b4c"],
        )
        assert ba._adopt_prior_attempt_scaffold_path(
            _PRED_REL, "hnd-pred-1a2b4c", tmp_path
        ) == str(Path("state") / "handoffs" / "2026-07-29-succ.md")

    def test_a_child_naming_a_different_predecessor_is_not_adopted(self, tmp_path):
        """AC-4. The whole corpus of live handoffs is in scan range; only a
        child of THIS predecessor is a candidate."""
        self._pred(tmp_path)
        _write_artifact(tmp_path / "state" / "handoffs" / "other-parent.md", ["handoff_id: hnd-other-1a2b4b"])
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            ["handoff_id: hnd-succ-1a2b55", "predecessor: state/handoffs/other-parent.md"],
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_a_scaffold_from_an_unrelated_run_is_not_adopted(self, tmp_path):
        """AC-4. `predecessor: none` is the shape of every fork and every
        chain-head baton sitting in the same directory."""
        self._pred(tmp_path)
        self._child(
            tmp_path, "2026-07-29-unrelated.md", ["handoff_id: hnd-unrel-1a2b56", 'predecessor: "none"']
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_a_child_whose_predecessor_id_contradicts_the_path_is_not_adopted(
        self, tmp_path
    ):
        """AC-4, second tooth: the id and path halves of the edge must agree. A
        child pointing at this path while naming a DIFFERENT parent id is
        internally inconsistent and is not adopted on the path match alone."""
        self._pred(tmp_path)
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            ["handoff_id: hnd-succ-1a2b55", f"predecessor: {_PRED_REL}", "predecessor_id: hnd-someone-else-1a2b53"],
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_two_candidates_are_ambiguous_and_refused(self, tmp_path):
        """Adopting either would orphan the other -- the very failure this
        function exists to remove. Ambiguity declines rather than ordering its
        way to an answer."""
        self._pred(tmp_path)
        for name in ("2026-07-29-a.md", "2026-07-29-b.md"):
            self._child(tmp_path, name, [f"predecessor: {_PRED_REL}"])
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_a_never_claimed_predecessor_adopts_nothing(self, tmp_path):
        """`claimed_or_shipped_at_path` is COMPOSED here, not re-derived -- d6
        refuses a never-claimed predecessor outright, so the carve-out has no
        business reaching one."""
        _write_artifact(
            tmp_path / _PRED_REL, ["handoff_id: hnd-pred-1a2b4c", "deployment_state: in_flight"]
        )
        self._child(tmp_path, "2026-07-29-succ.md", [f"predecessor: {_PRED_REL}"])
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_predecessor_side_evidence_takes_precedence_and_adopts_nothing(self, tmp_path):
        """A predecessor that already carries a succession edge is
        `_resume_recorded_successor_path`'s case. The two evidence classes must
        never compete for `output_path`."""
        self._pred(
            tmp_path,
            ["continued_into: state/handoffs/2026-07-29-succ.md"],
        )
        self._child(tmp_path, "2026-07-29-succ.md", [f"predecessor: {_PRED_REL}"])
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_a_child_that_has_itself_been_continued_is_not_a_scaffold(self, tmp_path):
        self._pred(tmp_path)
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            [
                f"predecessor: {_PRED_REL}",
                "deployment_state: continued",
                "continued_into: state/handoffs/2026-07-29-grandchild.md",
            ],
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_an_archived_predecessor_adopts_nothing(self, tmp_path):
        """An archived predecessor means d6 already ran -- not a pre-d6 abort."""
        archived = tmp_path / "archive" / "handoffs" / "2026-07" / "predecessor.md"
        _write_artifact(archived, ["handoff_id: hnd-pred-1a2b4c", "claimed_by: s"])
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            ["predecessor: archive/handoffs/2026-07/predecessor.md"],
        )
        assert (
            ba._adopt_prior_attempt_scaffold_path(
                "archive/handoffs/2026-07/predecessor.md", "hnd-pred-1a2b4c", tmp_path
            )
            is None
        )

    def test_the_predecessor_itself_is_never_its_own_candidate(self, tmp_path):
        self._pred(tmp_path, [f"predecessor: {_PRED_REL}"])
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_empty_and_missing_predecessor_are_refused(self, tmp_path):
        assert ba._adopt_prior_attempt_scaffold_path("", None, tmp_path) is None
        assert (
            ba._adopt_prior_attempt_scaffold_path("state/handoffs/nope.md", None, tmp_path)
            is None
        )


class TestAdoptPriorAttemptScaffoldPathAuthorship:
    """Cross-authorship adoption gap (closed 2026-07-30) -- `authoring_session`
    identification teeth specifically. Deliberately NOT a subclass of
    `TestAdoptPriorAttemptScaffoldPathUnit`: pytest collects inherited test
    methods too, and this class exists to add four new cases, not silently
    re-run the whole parent suite under a second name. `_pred`/`_child`/the
    matching-session autouse fixture are duplicated from that class rather
    than shared, matching its own already-established per-class-helper
    convention (see `_pred`/`_child` there)."""

    @pytest.fixture(autouse=True)
    def _this_run_session(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", _THIS_RUN_SESSION)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _pred(self, root: Path, extra: list[str] | None = None) -> None:
        _write_artifact(
            root / _PRED_REL,
            [
                "handoff_id: hnd-pred-1a2b4c",
                "claimed_by: test-session",
                "deployment_state: in_flight",
            ]
            + (extra or []),
        )

    def _child(self, root: Path, name: str, extra: list[str]) -> Path:
        if not any(line.startswith("authoring_session:") for line in extra):
            extra = extra + [f"authoring_session: {_THIS_RUN_SESSION}"]
        return _write_artifact(root / "state" / "handoffs" / name, extra)

    def test_matching_session_adopts(self, tmp_path):
        """The base class's own default (`_child`'s matching
        `authoring_session`) already exercises this on every other test in
        the parent class; asserted once more here, explicitly, as the
        positive case this subclass exists to anchor."""
        self._pred(tmp_path)
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            [f"predecessor: {_PRED_REL}", "predecessor_id: hnd-pred-1a2b4c"],
        )
        assert ba._adopt_prior_attempt_scaffold_path(
            _PRED_REL, "hnd-pred-1a2b4c", tmp_path
        ) == str(Path("state") / "handoffs" / "2026-07-29-succ.md")

    def test_mismatched_session_declines(self, tmp_path):
        self._pred(tmp_path)
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            [
                f"predecessor: {_PRED_REL}",
                "predecessor_id: hnd-pred-1a2b4c",
                "authoring_session: sid-some-other-run",
            ],
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_absent_authoring_session_on_candidate_declines(self, tmp_path):
        """A handoff scaffolded before this change, or by a scaffolder that
        still emits no such field, is indistinguishable from an unrelated
        session's file -- never adopted."""
        self._pred(tmp_path)
        _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-29-succ.md",
            [f"predecessor: {_PRED_REL}", "predecessor_id: hnd-pred-1a2b4c"],
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None

    def test_absent_env_for_this_run_declines(self, tmp_path, monkeypatch):
        """No resolvable session id for THIS run must fail-safe to "no
        match," never coerce into a value that could coincidentally equal a
        candidate's own unset/placeholder field."""
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        self._pred(tmp_path)
        self._child(
            tmp_path,
            "2026-07-29-succ.md",
            [f"predecessor: {_PRED_REL}", "predecessor_id: hnd-pred-1a2b4c"],
        )
        assert ba._adopt_prior_attempt_scaffold_path(_PRED_REL, "hnd-pred-1a2b4c", tmp_path) is None


class TestAdoptionIsNotASuccessionConclusion:
    """DR-242 Amendment A1's anti-loophole teeth, asserted rather than
    asserted-in-prose: the carve-out buys ONE path decision and nothing else."""

    def test_adoption_does_not_flip_any_status_on_the_predecessor(self, tmp_path, monkeypatch):
        """`brief()` is the surface that consumes the carve-out, and it writes
        nothing -- the predecessor is byte-identical after the read."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", _THIS_RUN_SESSION)
        _write_artifact(
            tmp_path / _PRED_REL,
            ["handoff_id: hnd-pred-1a2b4c", "claimed_by: s", "deployment_state: in_flight"],
        )
        _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-29-succ.md",
            [
                f"predecessor: {_PRED_REL}",
                "predecessor_id: hnd-pred-1a2b4c",
                f"authoring_session: {_THIS_RUN_SESSION}",
            ],
        )
        before = {
            p.relative_to(tmp_path): p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file()
        }

        decision = ba.brief("handoff", _PRED_REL, repo_root=tmp_path).decision_object

        assert {
            p.relative_to(tmp_path): p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file()
        } == before
        lineage = decision["artifact"]["lineage"]
        assert lineage["adopted_scaffold"] == str(
            Path("state") / "handoffs" / "2026-07-29-succ.md"
        )
        # The carve-out grants no succession conclusion: predecessor-side
        # evidence is still absent, and the field that records it stays None.
        assert lineage["resumed_successor"] is None

    def test_d6_still_carries_no_already_satisfied_under_an_adoption(self, tmp_path, monkeypatch):
        """d6's own DR-242 gate is untouched -- it re-checks the predecessor
        independently and can never be skipped past by an adoption."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", _THIS_RUN_SESSION)
        _write_artifact(
            tmp_path / _PRED_REL,
            ["handoff_id: hnd-pred-1a2b4c", "claimed_by: s", "deployment_state: in_flight"],
        )
        _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-29-succ.md",
            [
                f"predecessor: {_PRED_REL}",
                "predecessor_id: hnd-pred-1a2b4c",
                f"authoring_session: {_THIS_RUN_SESSION}",
            ],
        )

        decision = ba.brief("handoff", _PRED_REL, repo_root=tmp_path).decision_object

        d6 = next(d for d in decision["directives"] if d["id"] == "d6")
        assert d6["already_satisfied"] is False
        assert "already_satisfied_reason" not in d6

    def test_a_never_claimed_predecessor_is_still_refused_end_to_end_under_adoption(
        self, tmp_path, monkeypatch
    ):
        """The carve-out cannot launder a DR-242 refusal: an adoption for a
        never-claimed predecessor is not even offered, and d6 refuses regardless.
        """
        never_claimed = [
            line
            for line in _PREDECESSOR_FM
            if not line.startswith(("claimed_at:", "claimed_by:"))
        ]
        harness = _ReplayHarness(tmp_path, monkeypatch, predecessor_fm=never_claimed)
        harness.d1_body = "## What Was Accomplished\n\nReal operator prose.\n"
        harness.fail_at = "d5"
        assert harness.run()[0] == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        stranded = [n for n in harness.live_handoffs() if n != "predecessor.md"]
        assert len(stranded) == 1

        harness.fail_at = None
        exit_code, report = harness.run()

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert [d["directive_id"] for d in report["degraded"]] == ["d6"]
        # The refusal's substance: no succession edge, no archival.
        assert harness.continued_into() is None
        assert harness.archived_predecessor() is None
        # d6's own cleanup destroyed neither the operator's prose (it is
        # pristine-gated, and this file is not pristine) nor this run's mint.
        # KNOWN RESIDUE, asserted rather than hidden: adoption is not offered
        # for a never-claimed predecessor -- this class's whole subject -- so
        # the re-run mints a FRESH successor beside the stranded one and both
        # survive. Pre-2026-08-03 the fresh mint vanished, but only as a side
        # effect of d6 destroying it: that is the defect, not a cleanup policy.
        assert stranded[0] in harness.live_handoffs()
        assert len(harness.live_handoffs()) == 3


class TestD6CleanupNeverDeletesOperatorContent:
    """d6's inline `_cleanup_successor` was an unconditional unlink of its
    `--out` target. On a replay -- resumed OR adopted -- that target is a file
    d1 never wrote this run, so an unlink on any d6 failure destroyed operator
    content this run had no claim to."""

    def test_a_pristine_scaffold_is_still_cleaned_up(self, tmp_path, monkeypatch):
        successor_rel = "state/handoffs/2026-07-29-pristine.md"
        successor_abs = _render_real_scaffold(tmp_path / successor_rel)
        monkeypatch.setattr(
            ba_apply,
            "_invoke_op_in_process",
            lambda *a, **k: {"exit_code": 0, "superseded": False},
        )
        _seed_claimed_predecessor(tmp_path)

        with pytest.raises(RuntimeError):
            ba_apply._dispatch_handoff_supersede_predecessor(
                ["state/handoffs/predecessor.md", successor_rel, successor_rel], tmp_path
            )

        assert not successor_abs.exists()

    def test_a_scaffold_with_operator_content_survives_a_d6_failure(
        self, tmp_path, monkeypatch
    ):
        successor_rel = "state/handoffs/2026-07-29-authored.md"
        successor_abs = _render_real_scaffold(
            tmp_path / successor_rel,
            body="## What Was Accomplished\n\nReal operator prose.\n",
        )
        monkeypatch.setattr(
            ba_apply,
            "_invoke_op_in_process",
            lambda *a, **k: {"exit_code": 0, "superseded": False},
        )
        _seed_claimed_predecessor(tmp_path)

        with pytest.raises(RuntimeError):
            ba_apply._dispatch_handoff_supersede_predecessor(
                ["state/handoffs/predecessor.md", successor_rel, successor_rel], tmp_path
            )

        assert successor_abs.exists()
        assert "Real operator prose." in successor_abs.read_text(encoding="utf-8")


class TestSpinoffKindNeverReplays:
    """A fork has no predecessor, so `output_path` is always a genuinely fresh
    path and every spinoff directive stays unsatisfied -- kind=spinoff's
    behaviour is untouched by this fix."""

    def test_spinoff_directives_are_never_already_satisfied(self, tmp_path):
        origin = _write_artifact(
            tmp_path / "state" / "handoffs" / "origin.md",
            ["handoff_id: hnd-origin-1a2b4a", "deliverable_id: DEL-1"],
        )
        decision = ba.brief("spinoff", str(origin), repo_root=tmp_path).decision_object
        assert all(d["already_satisfied"] is False for d in decision["directives"])


# ---------------------------------------------------------------------------
# j-dirty-tree-case-c conditional emission (2026-07-31 fix): computed from
# disk (`_compute_dirty_tree_attribution`) rather than asked unconditionally
# with a single "mine" disposition. Two layers of coverage:
#   (1) `_build_judgment_points`'s pure emission predicate over a caller-
#       built `dirty_tree_attribution` dict (mirrors the sweep provider's own
#       calling convention) -- non-empty/empty `mine`, path-cap truncation,
#       residue-count evidence.
#   (2) `_compute_dirty_tree_attribution`'s real disk/git probe against a
#       real git repo -- each of the three degradation paths, plus the
#       Windows backslash/forward-slash path-normalization case.
# ---------------------------------------------------------------------------


class TestDirtyTreeCaseCConditionalEmission:
    def _jp(self, judgment_points: list[dict[str, Any]]):
        matches = [jp for jp in judgment_points if jp["id"] == "j-dirty-tree-case-c"]
        return matches[0] if matches else None

    def test_emitted_when_intersection_nonempty_with_paths_in_evidence(self):
        attribution = {
            "degraded": False,
            "mine": ["a.txt", "b.txt"],
            "residue_count": 3,
        }
        jp = self._jp(ba._build_judgment_points("handoff", attribution))
        assert jp is not None
        assert jp["dispositions"] == [{"value": "mine", "resolves": ["d1"]}]
        assert "a.txt" in jp["evidence"]
        assert "b.txt" in jp["evidence"]

    def test_not_emitted_when_intersection_empty(self):
        attribution = {"degraded": False, "mine": [], "residue_count": 5}
        judgment_points = ba._build_judgment_points("handoff", attribution)
        assert self._jp(judgment_points) is None
        # d1 has depends_on=None always -- confirmed unaffected by the
        # judgment point's absence -- but the OTHER judgment points (self-
        # honesty/pm-auth) still fire; this is not a wholesale emission bug.
        assert any(jp["id"] == "j-self-honesty" for jp in judgment_points)

    def test_evidence_truncates_at_ten_paths_with_and_n_more_tail(self):
        mine = [f"f{i}.txt" for i in range(15)]
        attribution = {"degraded": False, "mine": mine, "residue_count": 0}
        jp = self._jp(ba._build_judgment_points("handoff", attribution))
        assert jp is not None
        for path in mine[:10]:
            assert path in jp["evidence"]
        for path in mine[10:]:
            assert path not in jp["evidence"]
        assert "and 5 more" in jp["evidence"]

    def test_evidence_states_residue_count(self):
        attribution = {"degraded": False, "mine": ["a.txt"], "residue_count": 7}
        jp = self._jp(ba._build_judgment_points("handoff", attribution))
        assert jp is not None
        assert "7" in jp["evidence"]

    def test_degraded_true_falls_back_to_unconditional_emission(self):
        attribution = {"degraded": True, "evidence": "probe unavailable"}
        jp = self._jp(ba._build_judgment_points("handoff", attribution))
        assert jp is not None
        assert jp["evidence"] == "probe unavailable"

    def test_no_attribution_arg_falls_back_to_unconditional_emission(self):
        """`_build_judgment_points(kind)` with no second arg (the sweep-
        provider-independent default) never silently drops the judgment
        point -- matches the module's own `dirty_tree_attribution=None`
        default-degrade branch."""
        jp = self._jp(ba._build_judgment_points("handoff"))
        assert jp is not None


class TestComputeDirtyTreeAttribution:
    """Real git-repo/disk coverage of `_compute_dirty_tree_attribution` --
    the three degradation paths plus the Windows path-normalization case."""

    def _clear_session_env(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _touched_path(self, repo: Path, session_id: str) -> Path:
        common_dir = ba.git_common_dir(repo)
        touched = common_dir / "coordinator-sessions" / session_id / "touched.txt"
        touched.parent.mkdir(parents=True, exist_ok=True)
        return touched

    def test_no_resolvable_session_id_degrades(self, tmp_path, monkeypatch):
        self._clear_session_env(monkeypatch)
        _init_repo(tmp_path)
        result = ba._compute_dirty_tree_attribution(tmp_path)
        assert result["degraded"] is True
        assert "session id" in result["evidence"]

    def test_missing_touched_file_degrades(self, tmp_path, monkeypatch):
        self._clear_session_env(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-missing-touched")
        _init_repo(tmp_path)
        # No touched.txt ever written for this session id.
        result = ba._compute_dirty_tree_attribution(tmp_path)
        assert result["degraded"] is True
        assert "touched.txt" in result["evidence"]

    def test_git_status_failure_degrades(self, tmp_path, monkeypatch):
        self._clear_session_env(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-git-fail")
        _init_repo(tmp_path)
        touched = self._touched_path(tmp_path, "sess-git-fail")
        touched.write_text("some/file.txt\n", encoding="utf-8")

        real_run = ba.subprocess.run

        def _fake_run(argv, *args, **kwargs):
            if "status" in argv and "--porcelain" in argv:
                return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="boom")
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(ba.subprocess, "run", _fake_run)
        result = ba._compute_dirty_tree_attribution(tmp_path)
        assert result["degraded"] is True
        assert "exited" in result["evidence"]

    def test_non_git_root_degrades_via_runtime_error(self, tmp_path, monkeypatch):
        self._clear_session_env(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-non-git")
        # tmp_path is a plain directory -- never `git init`'d.
        result = ba._compute_dirty_tree_attribution(tmp_path)
        assert result["degraded"] is True

    def test_intersection_and_residue_computed_from_real_dirty_tree(self, tmp_path, monkeypatch):
        self._clear_session_env(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-real")
        _init_repo(tmp_path)
        touched = self._touched_path(tmp_path, "sess-real")
        touched.write_text("mine.txt\n", encoding="utf-8")
        (tmp_path / "mine.txt").write_text("this session's own edit\n", encoding="utf-8")
        (tmp_path / "peer.txt").write_text("a sibling session's edit\n", encoding="utf-8")

        result = ba._compute_dirty_tree_attribution(tmp_path)
        assert result["degraded"] is False
        assert result["mine"] == ["mine.txt"]
        assert result["residue_count"] == 1

    def test_windows_backslash_touched_entries_intersect_forward_slash_porcelain(
        self, tmp_path, monkeypatch
    ):
        """Case-teeth for the Windows path-normalization requirement: a
        `touched.txt` entry written with backslash separators must still
        intersect against `git status --porcelain`'s forward-slash path for
        the SAME file -- a naive string-set intersection silently produces
        an EMPTY `mine` set here, which is the exact false-negative this
        change must not introduce."""
        self._clear_session_env(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-windows")
        _init_repo(tmp_path)
        touched = self._touched_path(tmp_path, "sess-windows")
        # Backslash-separated entry -- as a Windows-authored touched.txt line
        # could carry, defensively, even though track_touched_files already
        # normalizes to forward slashes on write. Built via str.join (not a
        # literal backslash-separated path string) so this fixture never
        # reads as a hardcoded machine path to the concrete-path-citation
        # guard.
        backslash = chr(92)
        touched.write_text(backslash.join(["sub", "dir", "file.txt"]) + "\n", encoding="utf-8")
        (tmp_path / "sub" / "dir").mkdir(parents=True)
        (tmp_path / "sub" / "dir" / "file.txt").write_text("content\n", encoding="utf-8")

        result = ba._compute_dirty_tree_attribution(tmp_path)
        assert result["degraded"] is False
        assert result["mine"] == ["sub/dir/file.txt"]
        assert result["residue_count"] == 0


class TestDirtyTreeCaseCEndToEndViaBrief:
    """`brief()` itself threads `_compute_dirty_tree_attribution` into
    `_build_judgment_points` -- covered end-to-end against a real git repo
    (existing suite's `tmp_path` fixtures are plain directories, so this is
    the only place the real wiring is exercised)."""

    def test_brief_emits_case_c_only_when_this_session_owns_dirty_paths(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-brief")
        _init_repo(tmp_path)
        touched = ba.git_common_dir(tmp_path) / "coordinator-sessions" / "sess-brief" / "touched.txt"
        touched.parent.mkdir(parents=True, exist_ok=True)
        touched.write_text("state/handoffs/h1.md\n", encoding="utf-8")

        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        jp_ids = {jp["id"] for jp in decision["judgment_points"]}
        assert "j-dirty-tree-case-c" in jp_ids

    def test_brief_omits_case_c_when_dirty_tree_is_not_this_sessions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-brief-clean")
        _init_repo(tmp_path)
        touched = ba.git_common_dir(tmp_path) / "coordinator-sessions" / "sess-brief-clean" / "touched.txt"
        touched.parent.mkdir(parents=True, exist_ok=True)
        touched.write_text("some/unrelated/path.txt\n", encoding="utf-8")

        artifact = _write_artifact(
            tmp_path / "state" / "handoffs" / "h1.md",
            ['deliverable_id: DEL-1', 'predecessor: "none"'],
        )
        # h1.md itself is untracked/dirty here but NOT in touched.txt --
        # this session did not (per its own bookkeeping) author it.
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        jp_ids = {jp["id"] for jp in decision["judgment_points"]}
        assert "j-dirty-tree-case-c" not in jp_ids
        assert decision["artifact"]["lineage"]["resumed_successor"] is None


# ---------------------------------------------------------------------------
# C6c -- red-before-green regression tests for C3/C4/C5.
# Spec backlink: docs/plans/2026-08-02-roadmap-baton-supersession-hazard.md,
# chunk C6c (depends C3/C4/C5, which are PEER chunks -- NOT implemented by
# this chunk). Every test in this section is expected to FAIL against
# current HEAD; the fixes land in C3 (`_build_directives` gains a
# `roadmap-baton` predecessor discriminator), C4 (`resolve_lineage`'s plan
# tier resolves its supersession target from the durable claim ledger), and
# C5 (a d5 compensator or gate-evaluation hoist closes the plan-claim
# compensator gap). See § Layering / F1-F5 in the plan's own Problem section
# for the hazard these three chunks close.
# ---------------------------------------------------------------------------


class TestC3RoadmapBatonPredecessorDeclinesD6:
    """C3: `_build_directives` currently gates d6 on the MECHANICAL
    predicate `lineage.get("predecessor") is not None` alone -- it never
    reads the predecessor's own `kind` frontmatter. When the resolved
    predecessor's `kind` canonicalizes to `roadmap-baton` (via the shared
    `coordinator_core.frontmatter.baton_class.canonical_kind` helper), d6
    must NOT arm as an unconditional directive -- it must instead surface a
    judgment point (PIN-3 id `d6-roadmap-baton-decline`) naming the baton,
    with two legal decision values: `leave-baton` (default -- d6 stays
    unarmed, the mint proceeds) and `force-supersede` (operator override --
    d6 arms exactly as it does today). This is the PRIMARY closure for the
    ordinary `/handoff` path (§ Layering) -- C2 is only the backstop for
    direct-invoke callers that bypass `brief()`/`_build_directives` entirely.

    Every assertion below FAILS against current HEAD: HEAD has no
    `d6-roadmap-baton-decline` judgment point at all, and d6 arms
    unconditionally for ANY named predecessor regardless of its `kind`."""

    @staticmethod
    def _roadmap_baton_predecessor(tmp_path: Path) -> Path:
        """The artifact IS its own predecessor (own_handoff_id branch --
        mirrors `TestHandoffInputBecomesItsOwnPredecessor`), carrying
        `kind: roadmap-baton` on the SAME frontmatter `_build_directives`
        must read to discriminate -- the shortest fixture that puts a
        roadmap-baton `kind` on the resolved predecessor's own frontmatter."""
        return _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-02-roadmap-baton-predecessor.md",
            [
                "deliverable_id: DEL-C3-BATON",
                "handoff_id: hnd-baton-1a2b44",
                "kind: roadmap-baton",
                'predecessor: "none"',
            ],
        )

    def test_no_decision_supplied_does_not_arm_d6(self, tmp_path):
        artifact = self._roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis, (
            "C3: d6 must not arm unconditionally for a roadmap-baton "
            "predecessor -- it must surface a judgment point instead"
        )

    def test_judgment_point_emitted_naming_the_baton(self, tmp_path):
        artifact = self._roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        jp = next(
            (jp for jp in decision["judgment_points"] if jp["id"] == "d6-roadmap-baton-decline"),
            None,
        )
        assert jp is not None, (
            "C3/PIN-3: expected a judgment point with id "
            "'d6-roadmap-baton-decline' when the resolved predecessor's "
            "kind canonicalizes to roadmap-baton"
        )
        jp_text = " ".join(str(jp.get(field, "")) for field in ("question", "reason", "evidence"))
        assert "roadmap-baton" in jp_text.lower() or "baton" in jp_text.lower(), (
            "C3: the judgment point must name the baton, not decline bare -- "
            f"got jp text {jp_text!r}"
        )

    def test_both_legal_decision_values_are_present(self, tmp_path):
        artifact = self._roadmap_baton_predecessor(tmp_path)
        decision = ba.brief("handoff", str(artifact), repo_root=tmp_path).decision_object
        jp = next(
            (jp for jp in decision["judgment_points"] if jp["id"] == "d6-roadmap-baton-decline"),
            None,
        )
        assert jp is not None
        values = {d["value"] for d in jp["dispositions"]}
        assert values == {"leave-baton", "force-supersede"}, (
            "C3/PIN-3: legal decision values must be exactly "
            f"{{'leave-baton', 'force-supersede'}} -- got {values!r}"
        )

    def test_resolving_leave_baton_yields_no_d6_directive(self, tmp_path):
        artifact = self._roadmap_baton_predecessor(tmp_path)
        decision = ba.brief(
            "handoff",
            str(artifact),
            decisions={"d6-roadmap-baton-decline": {"disposition": "leave-baton"}},
            repo_root=tmp_path,
        ).decision_object
        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis

    def test_resolving_force_supersede_yields_the_d6_directive_as_today(self, tmp_path):
        artifact = self._roadmap_baton_predecessor(tmp_path)
        decision = ba.brief(
            "handoff",
            str(artifact),
            decisions={"d6-roadmap-baton-decline": {"disposition": "force-supersede"}},
            repo_root=tmp_path,
        ).decision_object
        d6 = next(
            (d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"),
            None,
        )
        assert d6 is not None, (
            "C3: an explicit 'force-supersede' override must arm d6 exactly "
            "as an ungated predecessor does today"
        )
        assert d6["args"][0] == str(artifact)


class TestC4PlanTierSupersessionTargetFromLedger:
    """C4: `resolve_lineage`'s `kind == "handoff"` branch currently
    discriminates a plan input only by the ABSENCE of `handoff_id` -- the
    same `else` branch also covers a legacy/hand-authored/corrupted handoff
    record missing `handoff_id`
    (`test_handoff_kind_resolves_predecessor_order_and_predecessor_id_companion`,
    preserved untouched below -- AC5). C4 adds an explicit, EARLIER
    plan-ness discriminator (`_fm_field(fm, "plan_id") is not None`) and,
    when true, resolves the ACTUAL supersession target from the session's
    own durable handoff-claims ledger -- the SAME authoritative source
    `_resolve_held_handoff_for_session` already reads for the empty-
    artifact-path self-resolution case -- rather than from the plan's
    `predecessor_handoff`/`predecessor` fields, which name PROVENANCE (the
    handoff that SPAWNED this plan per example-doctrine-repo's plan.schema.json), not a
    termination target (F3). `predecessor_handoff` must still be carried on
    `lineage` for lineage-carry purposes (assumed key name
    `lineage["predecessor_handoff"]`, mirroring this module's existing
    `predecessor_id`/`predecessor` naming convention -- not itself pinned by
    the plan; flag to the coordinator if C4 lands a different key). When the
    ledger read finds ZERO held claims, a judgment point (PIN-3 id
    `d6-plan-no-ledger-claim`) must be emitted naming the plan and the
    absent claim -- NOT a silent non-arm -- whose message mentions BOTH
    `replay` (a legitimate post-d5-release re-run) and `never` (a genuine
    stranding), so an operator can tell the two apart.

    Every assertion below FAILS against current HEAD: HEAD has no
    `plan_id`-based discriminator at all (a plan always falls to
    `predecessor_handoff or predecessor`), and a zero-claim ledger read is
    never even attempted for a plan input."""

    @staticmethod
    def _seed_handoff_claim(repo_root: Path, session_id: str, basename: str) -> None:
        """Mirrors `TestSelfResolutionFromClaimLedger._seed_handoff_claim` --
        duplicated locally (rather than reaching into a sibling test class)
        so this class's fixtures stay self-contained."""
        claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
        claims_dir.mkdir(parents=True, exist_ok=True)
        (claims_dir / "session_id").write_text(session_id, encoding="utf-8")

    def test_plan_input_resolves_predecessor_from_ledger_not_predecessor_handoff_field(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        provenance_handoff = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-07-20-provenance.md",
            ["handoff_id: hnd-provenance-1a2b4f"],
        )
        claimed_predecessor = _write_artifact(
            tmp_path / "state" / "handoffs" / "2026-08-01-claimed-predecessor.md",
            ["handoff_id: hnd-ledger-claimed-1a2b49"],
        )
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-02-c4-plan.md",
            [
                "plan_id: PLAN-C4",
                "deliverable_id: DEL-C4",
                f"predecessor_handoff: {provenance_handoff.relative_to(tmp_path)}",
            ],
        )
        self._seed_handoff_claim(tmp_path, "sid-c4", claimed_predecessor.name)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c4")

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor"] == str(claimed_predecessor.relative_to(tmp_path)), (
            "C4/AC5: a plan input's supersession target must come from the "
            "durable handoff-claims ledger, not predecessor_handoff/predecessor "
            f"fm fields -- got predecessor={lineage['predecessor']!r}"
        )
        assert lineage["predecessor"] != str(provenance_handoff.relative_to(tmp_path))
        assert lineage.get("predecessor_handoff") == str(
            provenance_handoff.relative_to(tmp_path)
        ), "C4/AC5: predecessor_handoff must still be carried on lineage for lineage-carry"

        d6 = next(
            (d for d in decision["directives"] if d["cli"] == "handoff.supersede_predecessor"),
            None,
        )
        assert d6 is not None
        assert d6["args"][0] == lineage["predecessor"]

    def test_zero_ledger_claims_emits_judgment_point_naming_plan_replay_and_never(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-02-c4-no-claim-plan.md",
            ["plan_id: PLAN-C4-NOCLAIM", "deliverable_id: DEL-C4-NOCLAIM"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c4-no-claim")

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object

        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis, (
            "C4: a plan input with zero ledger claims must not silently "
            "arm/skip d6 -- it must surface a judgment point"
        )

        jp = next(
            (jp for jp in decision["judgment_points"] if jp["id"] == "d6-plan-no-ledger-claim"),
            None,
        )
        assert jp is not None, (
            "C4/PIN-3: expected a judgment point with id "
            "'d6-plan-no-ledger-claim' naming the plan and the absent claim "
            "-- never a silent non-arm"
        )
        jp_text_raw = " ".join(str(jp.get(field, "")) for field in ("question", "reason", "evidence"))
        jp_text = jp_text_raw.lower()
        assert "replay" in jp_text, (
            "C4: the judgment point's message must mention 'replay' (a "
            "legitimate post-d5-release re-run) -- "
            f"got {jp_text_raw!r}"
        )
        assert "never" in jp_text, (
            "C4: the judgment point's message must mention 'never' (a "
            f"genuine stranding, distinct from a replay) -- got {jp_text_raw!r}"
        )
        assert "PLAN-C4-NOCLAIM" in jp_text_raw or plan.name in jp_text_raw, (
            "C4: the judgment point must name the plan -- "
            f"got {jp_text_raw!r}"
        )

    def test_ledger_claimed_predecessor_already_archived_is_still_recognized_as_roadmap_baton(
        self, tmp_path, monkeypatch
    ):
        """Review: coordinatorcode-reviewer-c2d43fc7 Finding 1 regression.
        The ledger's returned basename may already have moved to
        `archive/handoffs/` (`_resolve_held_handoff_for_session`'s own reason
        for existing) -- that is not exotic, it is the case the ledger is
        FOR. Before the fix, `resolve_lineage`'s `elif is_plan_input:` branch
        read the ledger-resolved predecessor via a hand-rolled
        `_read_frontmatter` that silently returns `""` for a missing path,
        so `predecessor_id` came back `None` and
        `_resolved_predecessor_canonical_kind` (C3's own gate, fed the same
        un-resolved value) reported "not roadmap-baton" for a predecessor
        that genuinely is one -- arming d6 unconditionally for exactly the
        archived-baton shape this whole plan exists to close."""
        _init_repo(tmp_path)
        archived_predecessor = _write_artifact(
            tmp_path / "archive" / "handoffs" / "2026-08-01-archived-baton.md",
            [
                "handoff_id: hnd-archived-baton-1a2b42",
                "kind: roadmap-baton",
                "deployment_state: continued",
            ],
        )
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-02-c4-archived-baton-plan.md",
            ["plan_id: PLAN-C4-ARCHIVED", "deliverable_id: DEL-C4-ARCHIVED"],
        )
        # The ledger claims the predecessor by its LIVE basename -- the
        # basename has since moved to archive/handoffs/, mirroring the boot
        # sweep archiving a claimed handoff out from under a held claim.
        self._seed_handoff_claim(tmp_path, "sid-c4-archived", "2026-08-01-archived-baton.md")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c4-archived")

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["predecessor_id"] == "hnd-archived-baton-1a2b42", (
            "Finding 1: the archived ledger-claimed predecessor's own "
            "handoff_id must still resolve, not silently come back None -- "
            f"got {lineage['predecessor_id']!r}"
        )

        clis = {d["cli"] for d in decision["directives"]}
        assert "handoff.supersede_predecessor" not in clis, (
            "Finding 1: an archived roadmap-baton predecessor resolved via "
            "the ledger must still decline to arm d6 unconditionally -- C3's "
            "gate must not be defeated by the archived-path shape"
        )
        jp = next(
            (jp for jp in decision["judgment_points"] if jp["id"] == "d6-roadmap-baton-decline"),
            None,
        )
        assert jp is not None, (
            "Finding 1: the archived predecessor must still be recognized as "
            "a roadmap-baton and surface 'd6-roadmap-baton-decline', not "
            "silently arm d6"
        )


class TestC9DiscoveryLabelPlanInput:
    """C9 (docs/plans/2026-08-03-deliverable-id-carry-plan-handoff-agree.md,
    AC7): C4 relabels a plan->execute trigger's `deliverable_id` hit from
    the `_tracking_read_frontmatter_field` vocabulary's `"artifact"` tier to
    a THIRD value, `"plan-input"` -- deliberately distinct from `"plan"`,
    which stays reserved for the CLAIMED-plan tier
    (`TestClaimedPlanDeliverableIdCarry` above). Tests what C4 actually put
    on disk (`coordinator_core/baton_assemble/__init__.py`'s `is_plan_input`
    block, ~line 1406), not the plan prose."""

    def test_plan_as_artifact_path_invocation_reports_plan_input_discovery(
        self, tmp_path, monkeypatch
    ):
        """AC7: `brief("handoff", <plan path>, ...)` -- the plan->execute
        trigger's own invocation shape, no session claimed-plan involved --
        must report `discovery == "plan-input"`, not the borrowed
        `"artifact"` label C4 fixes nor the CLAIMED-plan tier's `"plan"`."""
        _init_repo(tmp_path)
        plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-02-c9-plan-input.md",
            ["plan_id: PLAN-C9", "deliverable_id: DEL-C9-PLAN-INPUT"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c9-plan-input")

        decision = ba.brief("handoff", str(plan), repo_root=tmp_path).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "DEL-C9-PLAN-INPUT"
        assert lineage["discovery"] == "plan-input", (
            "C4/AC7: a plan->execute trigger invocation must report "
            f"discovery == 'plan-input' -- got {lineage['discovery']!r}"
        )

    def test_plan_input_and_claimed_plan_tiers_stay_distinguishable(
        self, tmp_path, monkeypatch
    ):
        """AC7 negative pin: the CLAIMED-plan tier (a session holding a
        plan claim, `artifact_path` a FRESH mint slug, unrelated to the
        claimed plan) still reports `discovery == "plan"` -- proving C4's
        relabel is conditioned on `is_plan_input` and never leaks onto the
        pre-existing tier `_tracking_read_frontmatter_field` already names
        `"plan"`. Companion to
        `TestClaimedPlanDeliverableIdCarry.test_ac7_authored_handoff_carries_claimed_plans_deliverable_id_value`,
        which pins the same tier's id-VALUE carry; this test's job is only
        the label boundary between the two tiers C4 introduced a third
        value alongside."""
        _init_repo(tmp_path)
        plan_slug = "2026-08-02-c9-claimed-plan-not-input"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: DEL-C9-CLAIMED-TIER"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c9-claimed-tier")
        assert session_claims.claim_plan(plan_slug, cwd=str(tmp_path)) is True

        decision = ba.brief(
            "handoff", "fresh-handoff-c9-claimed-tier", repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "DEL-C9-CLAIMED-TIER"
        assert lineage["discovery"] == "plan", (
            "C4/AC7: the CLAIMED-plan tier must keep reporting 'plan', "
            f"never 'plan-input' -- got {lineage['discovery']!r}"
        )

    def test_claimed_plan_tier_wins_precedence_even_when_artifact_path_is_plan_shaped(
        self, tmp_path, monkeypatch
    ):
        """Review: coordinator:code-reviewer (Finding 3) -- the relabel
        guard is `if is_plan_input and lineage["discovery"] == "artifact"`,
        deliberately checking the DISCOVERY VALUE, not just `is_plan_input`.
        This pins the case that guard clause exists for: `artifact_path`
        ITSELF is plan-shaped (`is_plan_input` is True, per the same
        `docs/plans/*.md` + `plan_id`-no-`handoff_id` shape as
        `test_plan_as_artifact_path_invocation_reports_plan_input_discovery`
        above), but the session ALSO holds a claimed plan carrying its own
        `deliverable_id` -- the claimed-plan tier reads first in cascade
        order and wins, so `discovery` must stay `"plan"`, never fall to
        `"plan-input"`. A future edit weakening the guard to
        `if is_plan_input:` (dropping the `== "artifact"` check) would flip
        this to `"plan-input"` and this test would catch it."""
        _init_repo(tmp_path)
        claimed_plan_slug = "2026-08-02-c9-precedence-claimed-plan"
        _write_artifact(
            tmp_path / "docs" / "plans" / f"{claimed_plan_slug}.md",
            ["deliverable_id: DEL-C9-PRECEDENCE-CLAIMED"],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c9-precedence")
        assert session_claims.claim_plan(claimed_plan_slug, cwd=str(tmp_path)) is True

        artifact_plan = _write_artifact(
            tmp_path / "docs" / "plans" / "2026-08-02-c9-precedence-artifact-plan.md",
            ["plan_id: PLAN-C9-PRECEDENCE-ARTIFACT"],
        )

        decision = ba.brief(
            "handoff", str(artifact_plan), repo_root=tmp_path
        ).decision_object
        lineage = decision["artifact"]["lineage"]

        assert lineage["deliverable_id"] == "DEL-C9-PRECEDENCE-CLAIMED", (
            "the claimed-plan tier must supply the id, not the plan-shaped "
            f"artifact_path -- got {lineage['deliverable_id']!r}"
        )
        assert lineage["discovery"] == "plan", (
            "C4/AC7 precedence: a plan-shaped artifact_path (is_plan_input="
            "True) must NOT relabel a claimed-plan-tier ('plan') hit to "
            f"'plan-input' -- got {lineage['discovery']!r}"
        )


class TestC5AbortD6LeavesPlanClaimIntact:
    """C5 (AC6's demonstration test): d5 (`session-claim-cli release-artifact
    plan <stem>`) genuinely releases the session's plan claim into the
    UNVERSIONED `<git-common-dir>/coordinator-sessions/` store -- `git
    revert` cannot restore it (F5). When a LATER directive (d6, the
    succession write) aborts, the session has already lost its plan claim
    with nothing to show for it -- a dirty abort. C5 closes this gap either
    via a d5 compensator (re-acquiring the claim) or by hoisting the d6
    GATE EVALUATION ahead of d5's execution (never d6's DIRECTIVE POSITION,
    which the plan's own Anti-scope pins last). Either shape must leave the
    session's plan claim HELD after an aborted d6.

    FAILS against current HEAD: `_D1_COMPENSATORS` maps only directive id
    "d1" -- d5's real release is never compensated, so this test's final
    assertion (the plan claim is still held) currently fails."""

    def test_aborted_d6_leaves_the_plan_claim_held(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        plan_slug = "2026-08-02-c5-abort-plan"
        plan_path = _write_artifact(
            tmp_path / "docs" / "plans" / f"{plan_slug}.md",
            ["deliverable_id: DEL-C5", 'predecessor: "none"'],
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-c5-abort")
        assert session_claims.claim_plan(plan_slug, cwd=str(tmp_path)) is True

        rel = "state/handoffs/2026-08-02-c5-abort-successor.md"

        def _fake_d1(args, repo_root):
            _write_placeholder_scaffold(repo_root / rel)
            return {"cli": "coordinator-doc-new", "args": args}

        def _fake_noop(args, repo_root):
            return {"cli": "noop", "args": args}

        def _fake_d5(args, repo_root):
            # Performs the REAL release via `session_claims.release_artifact`
            # in-process -- this suite runs under a HOME quarantine
            # (`_quarantine_real_home`, coordinator_core/conftest.py) that
            # makes the real `session-claim-cli` subprocess dispatch's own
            # `resolve_operator_config()` fail loud by design (see this
            # file's module-level `_stub_operator_config` fixture docstring),
            # so d5 is faked at the dispatch-table seam like every other
            # directive here -- but its BODY still genuinely releases the
            # claim, which is the actual hazard (F5) this test exercises.
            assert args[:2] == ["release-artifact", "plan"]
            session_claims.release_artifact("plan", args[2], cwd=str(repo_root))
            return {"cli": "session-claim-cli", "args": args}

        def _fake_failing_d6(args, repo_root):
            raise RuntimeError("d6 aborted (simulated, C5 regression)")

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _fake_noop)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "render-project-tracker", _fake_noop)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "session-claim-cli", _fake_d5)
        monkeypatch.setitem(
            ba_apply._CLI_DISPATCH, "handoff.supersede_predecessor", _fake_failing_d6
        )

        directives = [
            {
                "id": "d1",
                "cli": "coordinator-doc-new",
                "args": ["--type=handoff", f"--out={rel}"],
                "depends_on": None,
                "already_satisfied": False,
            },
            {
                "id": "d2",
                "cli": "lint-frontmatter",
                "args": ["--file", str(plan_path)],
                "depends_on": ["d1"],
                "already_satisfied": False,
            },
            {
                "id": "d4",
                "cli": "render-project-tracker",
                "args": [],
                "depends_on": ["d1"],
                "already_satisfied": False,
            },
            {
                "id": "d5",
                "cli": "session-claim-cli",
                "args": ["release-artifact", "plan", plan_slug],
                "depends_on": ["d1"],
                "already_satisfied": False,
            },
            {
                "id": "d6",
                "cli": "handoff.supersede_predecessor",
                "args": ["state/handoffs/predecessor.md", rel, rel],
                "depends_on": ["d1"],
                "already_satisfied": False,
            },
        ]

        def _fake_brief(kind, artifact_path, *, decisions=None, repo_root=None, title=None):
            class _FakeBriefResult:
                decision_object = {
                    "directives": directives,
                    "judgment_points": [],
                    "artifact": {"path": rel, "lineage": {"output_path": rel}},
                }

            return _FakeBriefResult()

        monkeypatch.setattr(ba, "brief", _fake_brief)

        exit_code, report = ba_apply.apply(
            "handoff",
            str(plan_path),
            session_id="sid-c5-abort",
            repo_root=tmp_path,
        )

        assert exit_code == ba_apply.apply_base.APPLY_EXIT_PARTIAL_MUTATION
        assert report["failed_directive"] == "d6"

        held = session_claims.list_claims_by_session("sid-c5-abort", cwd=str(tmp_path))
        assert ("plan-claims", plan_slug) in held, (
            "C5/AC6: aborting d6 must leave the session's plan claim intact -- "
            f"got held claims {held!r} (expected ('plan-claims', {plan_slug!r}) present)"
        )


class TestC6cPinnedTestPassesUntouched:
    """AC5 pin: `test_handoff_kind_resolves_predecessor_order_and_
    predecessor_id_companion` (around line 220 of this file) covers the
    legacy/hand-authored/corrupted handoff record missing `handoff_id` --
    C4's plan-ness discriminator (`plan_id is not None`) must not disturb
    that field-walk path. This is not a NEW test -- it is a marker that the
    pinned test was verified passing at HEAD (and must stay passing after
    C4 lands). See the completion report for the actual pytest invocation
    and its current (HEAD) PASS status."""


class TestStandaloneHandoffApplyEndToEnd:
    """2026-08-04 break-class fix, primary pin: the LIVE reproduced break --
    `baton-assemble apply handoff --title "..."` with no predecessor and no
    plan claim (a standalone handoff) used to abort entirely at d5
    (`session-claim-cli: release-artifact: basename required`, rc=1),
    `apply`'s own d1 compensation then deleting the scaffold it had just
    minted, so the ceremony produced NOTHING. Drives a REAL `apply()` run
    (only the subprocess-shaped directives -- d1/d2/d4 -- faked, matching
    `_ReplayHarness`'s own established pattern) and asserts it now succeeds
    end to end and lands the successor at a slugified, date-prefixed path."""

    def test_standalone_handoff_apply_succeeds_and_lands_slugified_file(
        self, tmp_path, monkeypatch
    ):
        import datetime

        repo = tmp_path / "repo"
        _init_repo(repo)
        for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(key, raising=False)

        d1_calls: list[str] = []

        def _fake_d1(args, repo_root):
            out = next(a[len("--out="):] for a in args if a.startswith("--out="))
            d1_calls.append(out)

            def _flag(name: str) -> str | None:
                prefix = f"--{name}="
                return next(
                    (a[len(prefix):] or None for a in args if a.startswith(prefix)), None
                )

            _render_real_scaffold(
                repo_root / out,
                doc_type="handoff",
                title=_flag("title"),
                predecessor=_flag("predecessor"),
                predecessor_id=_flag("predecessor-id"),
                deliverable_id=_flag("deliverable-id"),
            )
            return {"cli": "coordinator-doc-new", "args": args}

        def _noop(name):
            def _fake(args, repo_root):
                return {"cli": name, "args": args}

            return _fake

        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "coordinator-doc-new", _fake_d1)
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "lint-frontmatter", _noop("d2"))
        monkeypatch.setitem(ba_apply._CLI_DISPATCH, "render-project-tracker", _noop("d4"))

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        exit_code, report = ba_apply.apply(
            "handoff",
            "",
            session_id="sid-standalone-e2e",
            repo_root=repo,
            title="some title here",
        )

        assert exit_code == ba_apply.APPLY_EXIT_OK, report
        assert report["landed"] == ["d1", "d2", "d4"]
        assert d1_calls == [f"state/handoffs/{today}-some-title-here.md"]
        assert (repo / "state" / "handoffs" / f"{today}-some-title-here.md").is_file()
