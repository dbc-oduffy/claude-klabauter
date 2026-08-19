"""
coordinator_core.tests.test_commit_anchors — Unit tests for the "commit.anchors" op.

Coverage:
    (a) Nature derivation for each subject prefix + param override.
    (b) Staged-diff Plan/Plan-Id/Deliverable-Id extraction via a real tmp git repo.
    (c) Precision-over-recall omission (unresolvable → key absent from trailers).
    (d) COMPUTE_ONLY assertion — op performs no git writes and no state/ writes.

_handler is a sync def (F1 AC-3 Gap-3 fix — converted from async def). _run() handles
both sync results and legacy coroutines so tests remain usable across both shapes.
No pytest-asyncio dependency (engine is stdlib-only; prefer no test-infra additions).

Spec backlink: pln-claude-klabauter-commit-anchor-stamper-q-29b891 § C1-op
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path
from typing import List

import pytest

from coordinator_core.ops.deliverable_equivalence import _reset_equivalence_map_cache

# Declared, not excused: the "commit.anchors" op reads Plan/Plan-Id/Deliverable-Id
# trailers off real STAGED DIFF content and asserts it performs no git writes
# (COMPUTE_ONLY) -- both properties are of git's own staging/commit behaviour, not
# reproducible against a mock. Tests build/mutate their own repo per-test via
# `_init_repo`, so the fixture is not hoisted to module scope.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(result_or_coro):
    """Run an async coroutine or return a sync result directly.

    Review: code-reviewer — F1: _handler converted to sync def; _run now handles both
    shapes so test bodies need no update beyond the helper.
    """
    if asyncio.iscoroutine(result_or_coro):
        return asyncio.run(result_or_coro)
    return result_or_coro


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )


def _init_repo(path: Path) -> None:
    """Initialise a fresh git repo with required identity config."""
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    # Initial commit so the repo has a HEAD
    (path / ".gitkeep").write_text("")
    _git(["add", ".gitkeep"], path)
    _git(["commit", "-m", "initial"], path)


def _common_dir(worktree: Path) -> Path:
    """Return the git common dir (.git directory) for the given worktree."""
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(worktree),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return Path(result.stdout.strip())


def _parse_trailers(block: str) -> dict:
    """Parse a newline-joined 'Key: value' block into a dict.

    Returns {} for an empty block.
    """
    if not block:
        return {}
    result = {}
    for line in block.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# (a) Nature derivation — subject prefix taxonomy + param override
# ---------------------------------------------------------------------------

class TestNatureDerivation:
    """Nature: derives correctly from subject prefix taxonomy and param override."""

    def _call(self, subject: str = "", nature_param=None, repo_root=None) -> str:
        """Call _handler and return the trailers string."""
        from coordinator_core.ops.commit_anchors import _handler

        params = {"session_id": "", "nature": nature_param}
        result = _run(_handler(params, repo_root=repo_root))
        return result["trailers"]

    # --- prefix taxonomy ---

    def test_bugfix_param_override(self) -> None:
        # Review: code-reviewer — F8: removed dead first trailers= assignment (overwritten
        # immediately, result never used). Renamed to match what the test actually exercises:
        # the param override path for "bugfix", not the subject-prefix derivation path.
        trailers = _parse_trailers(self._call(nature_param="bugfix"))
        assert trailers.get("Nature") == "bugfix"

    def test_execute_prefix_maps_to_roadmap(self) -> None:
        trailers = _parse_trailers(self._call(nature_param="roadmap"))
        assert trailers.get("Nature") == "roadmap"

    def test_memo_prefix_maps_to_session_op(self) -> None:
        trailers = _parse_trailers(self._call(nature_param="session-op"))
        assert trailers.get("Nature") == "session-op"

    def test_refactor_prefix_maps_to_refactor(self) -> None:
        trailers = _parse_trailers(self._call(nature_param="refactor"))
        assert trailers.get("Nature") == "refactor"

    def test_docs_prefix_maps_to_docs(self) -> None:
        trailers = _parse_trailers(self._call(nature_param="docs"))
        assert trailers.get("Nature") == "docs"

    def test_chore_prefix_maps_to_chore(self) -> None:
        trailers = _parse_trailers(self._call(nature_param="chore"))
        assert trailers.get("Nature") == "chore"

    def test_infra_enum_value_accepted(self) -> None:
        trailers = _parse_trailers(self._call(nature_param="infra"))
        assert trailers.get("Nature") == "infra"


class TestNatureSubjectDerivation:
    """Nature derivation via _derive_nature_from_subject() — unit tests on the helper."""

    def _derive(self, subject: str):
        from coordinator_core.ops.commit_anchors import _derive_nature_from_subject
        return _derive_nature_from_subject(subject)

    def test_fix_colon(self) -> None:
        assert self._derive("fix: correct typo") == "bugfix"

    def test_execute_colon(self) -> None:
        assert self._derive("execute: chunk C1-op") == "roadmap"

    def test_execute_plan_colon(self) -> None:
        assert self._derive("execute-plan: pcore-03") == "roadmap"

    def test_memo_colon(self) -> None:
        assert self._derive("memo: auto-sweep inbox") == "session-op"

    def test_session_init_colon(self) -> None:
        assert self._derive("session-init: archived orphaned handoff") == "session-op"

    def test_pickup_colon(self) -> None:
        assert self._derive("pickup: strang-01 tc3 emission") == "session-op"

    def test_refactor_colon(self) -> None:
        assert self._derive("refactor: extract helper") == "refactor"

    def test_docs_colon(self) -> None:
        assert self._derive("docs: update wiki entry") == "docs"

    def test_chore_colon(self) -> None:
        assert self._derive("chore: bump version") == "chore"

    def test_unknown_prefix_returns_chore(self) -> None:
        # Unknown prefix → "chore" (subject HAS prefix shape but unmapped)
        assert self._derive("wip: something random") == "chore"

    def test_no_prefix_returns_none(self) -> None:
        # No conventional-commit prefix shape at all → None (unresolvable → omit)
        assert self._derive("Initial commit") is None
        assert self._derive("") is None

    def test_scope_annotation_stripped(self) -> None:
        # "fix(parser): ..." → "fix" prefix
        assert self._derive("fix(parser): correct off-by-one") == "bugfix"

    def test_case_insensitive(self) -> None:
        assert self._derive("FIX: uppercase prefix") == "bugfix"
        assert self._derive("Execute: mixed case") == "roadmap"

    def test_handler_commit_editmsg_to_nature_integration(self, tmp_path) -> None:
        """Integration: _handler reads COMMIT_EDITMSG → subject → Nature: end-to-end.

        Review: code-reviewer — F6: closes the untested seam where _read_commit_subject
        and the common_dir/COMMIT_EDITMSG path derivation could silently regress.
        Tests the primary Nature derivation route in hook usage (no nature param supplied).
        """
        _init_repo(tmp_path)
        common = _common_dir(tmp_path)
        # Write a known commit subject into COMMIT_EDITMSG (git writes this before
        # prepare-commit-msg fires; we simulate it here).
        (common / "COMMIT_EDITMSG").write_text(
            "fix: correct off-by-one in partition key derivation\n\n# Comments are ignored\n",
            encoding="utf-8",
        )

        from coordinator_core.ops.commit_anchors import _handler
        result = _run(_handler({"session_id": "", "nature": None}, repo_root=common))
        trailers = _parse_trailers(result["trailers"])
        assert trailers.get("Nature") == "bugfix", (
            f"Expected Nature=bugfix from 'fix:' subject in COMMIT_EDITMSG; got {trailers!r}"
        )


class TestNatureParamOverride:
    """Nature param override takes priority over subject-prefix derivation."""

    def _call(self, nature_param=None) -> dict:
        from coordinator_core.ops.commit_anchors import _handler
        result = _run(_handler({"session_id": "", "nature": nature_param}))
        return _parse_trailers(result["trailers"])

    def test_valid_override_used_verbatim(self) -> None:
        for value in ("bugfix", "infra", "roadmap", "refactor", "docs", "chore", "session-op"):
            trailers = self._call(nature_param=value)
            assert trailers.get("Nature") == value, f"Nature override failed for {value!r}"

    def test_invalid_override_becomes_chore(self) -> None:
        # Invalid enum token → "chore" (not omit: caller tried to supply a nature)
        trailers = self._call(nature_param="unknown-value")
        assert trailers.get("Nature") == "chore"

    def test_null_override_omits_nature_without_repo(self) -> None:
        # null + no repo_root → no COMMIT_EDITMSG → Nature not derivable → omitted
        trailers = self._call(nature_param=None)
        assert "Nature" not in trailers


# ---------------------------------------------------------------------------
# (b) Staged-diff Plan/Plan-Id/Deliverable-Id extraction (real tmp git repo)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path):
    """Create a real git repo with an initial commit, return its Path."""
    _init_repo(tmp_path)
    return tmp_path


class TestStagedDiffPlanExtraction:
    """Plan/Plan-Id/Deliverable-Id extracted from staged diff via a real git repo."""

    _PLAN_FRONTMATTER = textwrap.dedent("""\
        ---
        title: "Test Plan"
        created: 2026-07-04
        author: test
        status: draft
        plan_id: "pln-test-plan-abc123"
        deliverable_id: "dlv-test-plan-abc456"
        ---

        # Test Plan
        """)

    _PLAN_FRONTMATTER_NO_DELIVERABLE = textwrap.dedent("""\
        ---
        title: "Test Plan No Deliverable"
        created: 2026-07-04
        author: test
        status: draft
        plan_id: "pln-test-plan-nodelv"
        ---

        # Test Plan
        """)

    def _call(self, repo: Path, session_id: str = "", nature: str = None) -> dict:
        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(repo)
        result = _run(_handler(
            {"session_id": session_id, "nature": nature},
            repo_root=common,
        ))
        return _parse_trailers(result["trailers"])

    def test_staged_plan_emits_plan_and_ids(self, tmp_repo) -> None:
        """A staged docs/plans/*.md emits Plan:, Plan-Id:, and Deliverable-Id:."""
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)

        _git(["add", str(plan_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert trailers.get("Plan") == "docs/plans/2026-07-04-test-plan.md"
        assert trailers.get("Plan-Id") == "pln-test-plan-abc123"
        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"

    def test_deliverable_key_is_exactly_deliverable_id(self, tmp_repo) -> None:
        """The deliverable FK is spelled `Deliverable-Id:`, never bare `Deliverable:`.

        Regression: this op and `coordinator/bin/coordinator-prepare-commit-msg` are two
        independent producers of the same FK and spelled it two ways until 2026-07-28.
        `%(trailers:key=X)` is an exact match, so the bare spelling read as empty for
        every consumer (claude-klabauter's coverage DAG, example-retrieval-repo's ingest) and errored nowhere.
        """
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)

        _git(["add", str(plan_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert "Deliverable" not in trailers, (
            "bare `Deliverable:` is the pre-2026-07-28 spelling no consumer reads; "
            f"got trailer keys {sorted(trailers)}"
        )
        assert "Deliverable-Id" in trailers

    _GOVERNING_PLAN_FRONTMATTER = textwrap.dedent("""\
        ---
        title: "Governing Plan"
        created: 2026-08-18
        author: test
        status: approved
        plan_id: "pln-governing-plan-real"
        deliverable_id: "dlv-governing-real"
        ---

        # Governing Plan
        """)

    def test_governing_plan_slug_wins_over_foreign_staged_plan(self, tmp_repo) -> None:
        """A supplied `governing_plan_slug` is authoritative over the staged-diff
        scan, even when a PEER's unrelated plan is the only `docs/plans/*.md`
        staged in the (shared) index.

        Regression: 2026-08-18-wsc-tail-commit-trailers-name-a-foreign-
        deliverable-3f7ac1d20e94.yaml — a wsc-tail commit invoked with
        `--governing-plan-slug 2026-08-18-sat-07-tier-a-wiring` landed
        `Deliverable-Id: dlv-fl-core-03` (a concurrent peer's plan) instead of
        `dlv-sat-07`, because the staged-diff scan found the peer's plan as the
        sole `docs/plans/*.md` candidate and nothing cross-checked it against
        the explicitly supplied slug. This pins the fix: the governing slug's
        own plan file (read straight off disk, not the staged diff) wins.
        """
        from coordinator_core.ops.commit_anchors import _handler

        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)

        # The peer's plan -- present in the shared index (staged), unrelated
        # to this commit, and NOT the governing plan.
        peer_plan = plan_dir / "2026-08-18-peer-plan.md"
        peer_plan.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(peer_plan)], tmp_repo)

        # The governing plan -- on disk (this session's own workstream), but
        # NOT staged as part of this commit's own diff at all.
        governing_slug = "2026-08-18-sat-07-tier-a-wiring"
        governing_plan = plan_dir / f"{governing_slug}.md"
        governing_plan.write_text(self._GOVERNING_PLAN_FRONTMATTER)

        common = _common_dir(tmp_repo)
        result = _run(_handler(
            {
                "session_id": "",
                "nature": None,
                "governing_plan_slug": governing_slug,
                # No "paths" scope -- reproduces the whole-shared-index read
                # that let the peer's plan win before this fix.
            },
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])

        assert trailers.get("Plan") == f"docs/plans/{governing_slug}.md"
        assert trailers.get("Plan-Id") == "pln-governing-plan-real"
        assert trailers.get("Deliverable-Id") == "dlv-governing-real"

    _GOVERNING_PLAN_NO_IDS_FRONTMATTER = textwrap.dedent("""\
        ---
        title: "Governing Plan Stub"
        created: 2026-08-18
        author: test
        status: draft
        ---

        # Governing Plan Stub (mid-enrichment, no ids yet)
        """)

    def test_governing_plan_slug_resolves_but_no_valid_ids_blocks_foreign_fallback(
        self, tmp_repo
    ) -> None:
        """Regression (finding 1, trailer-fix-review.md): the governing plan
        file EXISTS but carries neither a valid `pln-` plan_id nor `dlv-`
        deliverable_id (e.g. a plan stub mid-enrichment). A conflicting plan
        is visible to the staged-diff scan. The staged-diff result must NOT
        win by default -- that is exactly the foreign-peer-plan class this
        fix exists to stop. No foreign Plan/Plan-Id/Deliverable-Id trailer
        is emitted; at most `Plan:` (the governing path, no ids) is."""
        from coordinator_core.ops.commit_anchors import _handler

        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)

        # A conflicting plan, staged and visible to the staged-diff scan --
        # NOT the governing plan.
        conflicting_plan = plan_dir / "2026-08-18-conflicting-plan.md"
        conflicting_plan.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(conflicting_plan)], tmp_repo)

        # The governing plan -- exists on disk, but frontmatter carries no
        # valid ids yet (mid-enrichment stub).
        governing_slug = "2026-08-18-governing-plan-stub"
        governing_plan = plan_dir / f"{governing_slug}.md"
        governing_plan.write_text(self._GOVERNING_PLAN_NO_IDS_FRONTMATTER)

        common = _common_dir(tmp_repo)
        result = _run(_handler(
            {
                "session_id": "",
                "nature": None,
                "governing_plan_slug": governing_slug,
            },
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])

        assert trailers.get("Plan") != "docs/plans/2026-08-18-conflicting-plan.md"
        assert trailers.get("Plan-Id") != "pln-test-plan-abc123"
        assert trailers.get("Deliverable-Id") != "dlv-test-plan-abc456"
        assert "Plan-Id" not in trailers
        assert "Deliverable-Id" not in trailers

    def test_governing_plan_slug_unresolvable_falls_back_to_staged_diff(self, tmp_repo) -> None:
        """`governing_plan_slug` naming a plan file that does not exist on disk
        (nothing to be authoritative WITH) falls back to the existing
        staged-diff scan rather than omitting Plan/Plan-Id/Deliverable-Id
        outright."""
        from coordinator_core.ops.commit_anchors import _handler

        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(plan_file)], tmp_repo)

        common = _common_dir(tmp_repo)
        result = _run(_handler(
            {
                "session_id": "",
                "nature": None,
                "governing_plan_slug": "2026-08-18-does-not-exist",
            },
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])

        assert trailers.get("Plan") == "docs/plans/2026-07-04-test-plan.md"
        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"

    def test_staged_plan_without_deliverable_omits_deliverable_key(self, tmp_repo) -> None:
        """Plan file with no deliverable_id → Plan and Plan-Id emitted, Deliverable-Id omitted."""
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-no-deliverable.md"
        plan_file.write_text(self._PLAN_FRONTMATTER_NO_DELIVERABLE)

        _git(["add", str(plan_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert trailers.get("Plan") == "docs/plans/2026-07-04-no-deliverable.md"
        assert trailers.get("Plan-Id") == "pln-test-plan-nodelv"
        assert "Deliverable-Id" not in trailers

    def test_no_staged_plan_omits_plan_keys(self, tmp_repo) -> None:
        """No plan file staged → Plan/Plan-Id/Deliverable-Id all absent (precision over recall)."""
        # Stage a non-plan file
        non_plan = tmp_repo / "README.md"
        non_plan.write_text("# README\n")
        _git(["add", str(non_plan)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert "Plan" not in trailers
        assert "Plan-Id" not in trailers
        assert "Deliverable-Id" not in trailers

    def test_multiple_staged_plans_omits_plan_keys(self, tmp_repo) -> None:
        """Multiple docs/plans/*.md in staged set → ambiguous → Plan keys omitted."""
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)

        for name in ("plan-a.md", "plan-b.md"):
            (plan_dir / name).write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(plan_dir)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert "Plan" not in trailers
        assert "Plan-Id" not in trailers

    def test_plan_with_invalid_plan_id_omits_plan_id(self, tmp_repo) -> None:
        """Plan file with plan_id not starting with 'pln-' → Plan-Id omitted."""
        bad_frontmatter = textwrap.dedent("""\
            ---
            title: "Bad ID Plan"
            created: 2026-07-04
            author: test
            status: draft
            plan_id: "not-a-pln-id"
            deliverable_id: "dlv-valid-abc123"
            ---
            """)
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "bad-id-plan.md"
        plan_file.write_text(bad_frontmatter)
        _git(["add", str(plan_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert trailers.get("Plan") == "docs/plans/bad-id-plan.md"
        assert "Plan-Id" not in trailers
        assert trailers.get("Deliverable-Id") == "dlv-valid-abc123"


# ---------------------------------------------------------------------------
# (b2) `Resolves:` — the missing completion-grain producer
# (docs/plans/2026-08-01-baton-spine-information-integrity.md § A1)
# ---------------------------------------------------------------------------

class TestResolvesCompletionTrailer:
    """`Resolves:` is stamped ONLY at the completion event (a staged
    `archive/completed/*.md` entry alongside the plan), never on an ordinary
    mid-flight commit that carries `Deliverable-Id:` alone. This is the
    regression guard for the plan's Anti-scope entry 1 — a future "fix" that
    widens the ship-oracle's join onto `Deliverable-Id:` instead of building
    this producer would flip every one of these assertions.
    """

    _PLAN_FRONTMATTER = TestStagedDiffPlanExtraction._PLAN_FRONTMATTER

    def _call(self, repo: Path, session_id: str = "", nature: str = None) -> dict:
        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(repo)
        result = _run(_handler(
            {"session_id": session_id, "nature": nature},
            repo_root=common,
        ))
        return _parse_trailers(result["trailers"])

    def test_completion_event_emits_resolves(self, tmp_repo) -> None:
        """(a) A completion-event commit (plan + staged archive/completed/*.md
        whose `chain:` names that SAME plan's slug) carries `Resolves: <dlv-id>`
        — the same id as `Deliverable-Id:`."""
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(plan_file)], tmp_repo)

        completed_dir = tmp_repo / "archive" / "completed" / "2026-08"
        completed_dir.mkdir(parents=True)
        entry_file = completed_dir / "2026-08-01-test-plan-abc123.md"
        entry_file.write_text(
            "---\ntitle: \"Done\"\ncreated: 2026-08-01\n"
            "chain: \"2026-07-04-test-plan\"\n---\n\nDone.\n"
        )
        _git(["add", str(entry_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"
        assert trailers.get("Resolves") == "dlv-test-plan-abc456"

    def test_cross_deliverable_completion_entry_omits_resolves(self, tmp_repo) -> None:
        """Regression (F1): a commit staging plan A's frontmatter alongside an
        UNRELATED completion entry for plan B must NOT stamp `Resolves: dlv-A`.
        `_has_staged_completion_entry` used to return True for ANY staged
        completion entry regardless of which deliverable it actually names —
        this pins the fix that scopes the gate to the resolved deliverable."""
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)

        # Plan B — already committed (not part of this commit's staged diff),
        # named by the completion entry's `chain:` field.
        other_plan_frontmatter = textwrap.dedent("""\
            ---
            title: "Other Plan"
            created: 2026-07-05
            author: test
            status: draft
            plan_id: "pln-other-plan-def789"
            deliverable_id: "dlv-other-plan-xyz999"
            ---

            # Other Plan
            """)
        other_plan_file = plan_dir / "2026-07-05-other-plan.md"
        other_plan_file.write_text(other_plan_frontmatter)
        _git(["add", str(other_plan_file)], tmp_repo)
        _git(["commit", "-m", "add other plan"], tmp_repo)

        # Plan A — staged in THIS commit (the deliverable actually being resolved).
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(plan_file)], tmp_repo)

        # Completion entry staged alongside plan A, but its `chain:` names plan B.
        completed_dir = tmp_repo / "archive" / "completed" / "2026-08"
        completed_dir.mkdir(parents=True)
        entry_file = completed_dir / "2026-08-01-other-plan-abc123.md"
        entry_file.write_text(
            "---\ntitle: \"Done\"\ncreated: 2026-08-01\n"
            "chain: \"2026-07-05-other-plan\"\n---\n\nDone.\n"
        )
        _git(["add", str(entry_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"
        assert "Resolves" not in trailers, (
            "cross-deliverable false-positive: completion entry names plan B's "
            "deliverable, must never stamp Resolves for plan A's"
        )

    def test_declared_fork_pair_still_emits_resolves_raw(self, tmp_repo) -> None:
        """C13 (AC16/AC17) — manifest row #2, `_has_staged_completion_entry`.

        A completion entry resolving to plan B's raw deliverable_id must still
        gate `Resolves:` for plan A's commit when the pair is a DECLARED fork
        (state/deliverable-equivalence.yaml). Un-canonicalized raw equality
        would silently miss this and omit Resolves at the real completion
        event. The stamped value itself must be plan A's RAW id, never the
        canonicalized one — canonicalize() is confined to the boolean gate's
        comparison, the trailer write path stays untouched (AC17's writer
        pin: canonicalization must never leak into what lands on disk).
        """
        state_dir = tmp_repo / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "deliverable-equivalence.yaml").write_text(
            "entries:\n"
            "  - loser: dlv-other-plan-xyz999\n"
            "    winner: dlv-test-plan-abc456\n"
            "    evidence: test\n",
            encoding="utf-8",
        )
        _git(["add", "state/deliverable-equivalence.yaml"], tmp_repo)
        _reset_equivalence_map_cache()

        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)

        # Plan B — already committed, the LOSER leg of the declared pair.
        other_plan_frontmatter = textwrap.dedent("""\
            ---
            title: "Other Plan"
            created: 2026-07-05
            author: test
            status: draft
            plan_id: "pln-other-plan-def789"
            deliverable_id: "dlv-other-plan-xyz999"
            ---

            # Other Plan
            """)
        other_plan_file = plan_dir / "2026-07-05-other-plan.md"
        other_plan_file.write_text(other_plan_frontmatter)
        _git(["add", str(other_plan_file)], tmp_repo)
        _git(["commit", "-m", "add other plan"], tmp_repo)

        # Plan A — staged in THIS commit, the WINNER leg.
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(plan_file)], tmp_repo)

        # Completion entry staged alongside plan A, but its `chain:` names
        # plan B — the loser leg of the declared pair.
        completed_dir = tmp_repo / "archive" / "completed" / "2026-08"
        completed_dir.mkdir(parents=True)
        entry_file = completed_dir / "2026-08-01-other-plan-abc123.md"
        entry_file.write_text(
            "---\ntitle: \"Done\"\ncreated: 2026-08-01\n"
            "chain: \"2026-07-05-other-plan\"\n---\n\nDone.\n"
        )
        _git(["add", str(entry_file)], tmp_repo)

        try:
            trailers = self._call(tmp_repo)
        finally:
            _reset_equivalence_map_cache()

        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"
        assert trailers.get("Resolves") == "dlv-test-plan-abc456", (
            "declared fork pair must still gate Resolves, stamped with plan "
            "A's RAW id — never a canonicalized alias"
        )

    def test_mid_flight_commit_omits_resolves(self, tmp_repo) -> None:
        """(b) A mid-flight commit — plan staged, Deliverable-Id: resolvable,
        but NO completion entry staged — does NOT carry `Resolves:`. This is
        the regression guard: `Deliverable-Id:` alone must never read as
        completion (Anti-scope entry 1)."""
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(self._PLAN_FRONTMATTER)
        _git(["add", str(plan_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"
        assert "Resolves" not in trailers

    def test_completion_entry_without_deliverable_omits_resolves(self, tmp_repo) -> None:
        """A staged completion entry with no resolvable Deliverable-Id (no
        plan staged at all) omits Resolves: — precision over recall."""
        completed_dir = tmp_repo / "archive" / "completed" / "2026-08"
        completed_dir.mkdir(parents=True)
        entry_file = completed_dir / "2026-08-01-adhoc-abc123.md"
        entry_file.write_text("---\ntitle: \"Done\"\ncreated: 2026-08-01\n---\n\nDone.\n")
        _git(["add", str(entry_file)], tmp_repo)

        trailers = self._call(tmp_repo)
        assert "Deliverable-Id" not in trailers
        assert "Resolves" not in trailers


# ---------------------------------------------------------------------------
# (c) Precision-over-recall omission
# ---------------------------------------------------------------------------

class TestPrecisionOverRecall:
    """Unresolvable keys are absent from the trailer block (never fabricated)."""

    def test_no_repo_root_returns_empty_trailers(self) -> None:
        """With no repo_root and no nature param → empty trailer block."""
        from coordinator_core.ops.commit_anchors import _handler
        result = _run(_handler({"session_id": "s-abc", "nature": None}, repo_root=None))
        assert result["trailers"] == ""

    def test_anchor_absent_when_no_matching_handoff(self, tmp_path) -> None:
        """session_id that matches no handoff → Anchor absent."""
        _init_repo(tmp_path)
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)

        # Write a live handoff for a DIFFERENT session
        (handoff_dir / "2026-07-04-other-session.md").write_text(textwrap.dedent("""\
            ---
            title: "Other session handoff"
            status: open
            picked_up_by: "s-other-session"
            deployment_state: deployed
            created: 2026-07-04
            ---
            """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": "s-nomatch", "nature": "chore"},
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])
        assert "Anchor" not in trailers

    def test_anchor_absent_when_multiple_handoffs_match(self, tmp_path) -> None:
        """Multiple handoffs matching session_id → ambiguous → Anchor absent."""
        _init_repo(tmp_path)
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)

        sid = "s-ambiguous-session"
        for name in ("handoff-a.md", "handoff-b.md"):
            (handoff_dir / name).write_text(textwrap.dedent(f"""\
                ---
                title: "Handoff {name}"
                status: open
                picked_up_by: "{sid}"
                deployment_state: deployed
                created: 2026-07-04
                ---
                """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": sid, "nature": "chore"},
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])
        assert "Anchor" not in trailers

    def test_anchor_absent_when_matching_handoff_is_terminal(self, tmp_path) -> None:
        """A claimed/archived/abandoned handoff is not a live anchor → Anchor absent."""
        _init_repo(tmp_path)
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)

        sid = "s-consumed"
        (handoff_dir / "consumed-handoff.md").write_text(textwrap.dedent(f"""\
            ---
            title: "Consumed Handoff"
            status: claimed
            claimed_by: "{sid}"
            deployment_state: deployed
            created: 2026-07-04
            ---
            """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": sid, "nature": "chore"},
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])
        assert "Anchor" not in trailers

    def test_session_id_not_stamped(self) -> None:
        """Session-Id: is NOT emitted (prepare-commit-msg already stamps it separately)."""
        from coordinator_core.ops.commit_anchors import _handler
        result = _run(_handler(
            {"session_id": "s-some-session", "nature": "chore"},
        ))
        assert "Session-Id" not in result["trailers"]


# ---------------------------------------------------------------------------
# (c-cont) Anchor positive case
# ---------------------------------------------------------------------------

class TestAnchorResolution:
    """Anchor emitted when exactly one live handoff matches session_id."""

    def test_anchor_emitted_for_single_match(self, tmp_path) -> None:
        """Exactly one live handoff matching session_id → Anchor emitted."""
        _init_repo(tmp_path)
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)

        sid = "s-unique-session"
        handoff_name = "2026-07-04-my-handoff"
        (handoff_dir / f"{handoff_name}.md").write_text(textwrap.dedent(f"""\
            ---
            title: "My Handoff"
            status: open
            picked_up_by: "{sid}"
            deployment_state: deployed
            created: 2026-07-04
            ---
            """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": sid, "nature": "chore"},
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])
        assert trailers.get("Anchor") == f"handoff/{handoff_name}"

    def test_anchor_matches_consumed_by_field(self, tmp_path) -> None:
        """Session_id in old-vocabulary consumed_by on a non-terminal handoff → Anchor
        emitted (DR-084 fallback tolerance: _handler reads claimed_by with a consumed_by
        fallback — deliberately kept on old vocabulary to exercise that fallback path)."""
        _init_repo(tmp_path)
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)

        sid = "s-consumer-session"
        handoff_name = "2026-07-04-active-handoff"
        # open status + old-vocabulary consumed_by set — exercises fallback tolerance
        (handoff_dir / f"{handoff_name}.md").write_text(textwrap.dedent(f"""\
            ---
            title: "Active Handoff"
            status: open
            consumed_by: "{sid}"
            deployment_state: deployed
            created: 2026-07-04
            ---
            """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": sid, "nature": "chore"},
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])
        assert trailers.get("Anchor") == f"handoff/{handoff_name}"


# ---------------------------------------------------------------------------
# (b-cont) Full integration: Nature + Plan + Anchor in one call
# ---------------------------------------------------------------------------

class TestFullTrailerBlock:
    """Integration: Nature + Plan + Anchor all resolvable in a single call."""

    _PLAN_FM = textwrap.dedent("""\
        ---
        title: "Integration Plan"
        created: 2026-07-04
        author: test
        status: draft
        plan_id: "pln-integration-abc001"
        deliverable_id: "dlv-integration-abc002"
        ---
        # Integration Plan
        """)

    def test_full_trailer_block(self, tmp_path) -> None:
        """Nature + Plan + Plan-Id + Deliverable-Id + Anchor all emitted."""
        _init_repo(tmp_path)

        # Stage a plan file
        plan_dir = tmp_path / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-integration-plan.md"
        plan_file.write_text(self._PLAN_FM)
        _git(["add", str(plan_file)], tmp_path)

        # Create a live handoff for this session
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)
        sid = "s-integration-session"
        handoff_name = "2026-07-04-integration-handoff"
        (handoff_dir / f"{handoff_name}.md").write_text(textwrap.dedent(f"""\
            ---
            title: "Integration Handoff"
            status: open
            picked_up_by: "{sid}"
            deployment_state: deployed
            created: 2026-07-04
            ---
            """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": sid, "nature": "roadmap"},
            repo_root=common,
        ))
        trailers = _parse_trailers(result["trailers"])

        assert trailers.get("Nature") == "roadmap"
        assert trailers.get("Plan") == "docs/plans/2026-07-04-integration-plan.md"
        assert trailers.get("Plan-Id") == "pln-integration-abc001"
        assert trailers.get("Deliverable-Id") == "dlv-integration-abc002"
        assert trailers.get("Anchor") == f"handoff/{handoff_name}"

    def test_trailer_order(self, tmp_path) -> None:
        """Trailer order: Nature first, then Plan/Plan-Id/Deliverable-Id, then Anchor."""
        _init_repo(tmp_path)

        plan_dir = tmp_path / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-order-test.md"
        plan_file.write_text(self._PLAN_FM)
        _git(["add", str(plan_file)], tmp_path)

        sid = "s-order-session"
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)
        (handoff_dir / "2026-07-04-order-handoff.md").write_text(textwrap.dedent(f"""\
            ---
            title: "Order Handoff"
            status: open
            picked_up_by: "{sid}"
            deployment_state: deployed
            created: 2026-07-04
            ---
            """))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": sid, "nature": "infra"},
            repo_root=common,
        ))
        lines = [ln for ln in result["trailers"].splitlines() if ln.strip()]

        # First trailer must be Nature
        assert lines[0].startswith("Nature:")
        # Plan comes before Plan-Id comes before Deliverable-Id
        keys = [ln.split(":")[0] for ln in lines]
        plan_idx = keys.index("Plan") if "Plan" in keys else -1
        plan_id_idx = keys.index("Plan-Id") if "Plan-Id" in keys else -1
        deliverable_idx = keys.index("Deliverable-Id") if "Deliverable-Id" in keys else -1
        anchor_idx = keys.index("Anchor") if "Anchor" in keys else -1

        assert plan_idx < plan_id_idx
        assert plan_id_idx < deliverable_idx
        if anchor_idx >= 0:
            assert deliverable_idx < anchor_idx


# ---------------------------------------------------------------------------
# (d) COMPUTE_ONLY assertion
# ---------------------------------------------------------------------------

class TestComputeOnly:
    """Op performs zero git writes and zero state/ writes (COMPUTE_ONLY invariant)."""

    def test_no_git_writes_no_state_writes(self, tmp_path) -> None:
        """Running the op leaves the git index and state/ unchanged.

        Verification:
        - git status --porcelain before and after are identical (no staged mutations).
        - state/ mtime unchanged (no files written there by the op).
        """
        _init_repo(tmp_path)

        # Capture git status before
        before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path), capture_output=True, encoding="utf-8", check=True,
        ).stdout

        # Capture state/ mtime snapshot (if it exists)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        state_before_entries = set(state_dir.rglob("*"))

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_path)
        result = _run(_handler(
            {"session_id": "s-compute-only", "nature": "chore"},
            repo_root=common,
        ))

        # Result must be a dict with "trailers" key
        assert "trailers" in result
        assert isinstance(result["trailers"], str)

        # Git index unchanged
        after = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path), capture_output=True, encoding="utf-8", check=True,
        ).stdout
        assert before == after, (
            f"git status changed after op (COMPUTE_ONLY violation):\n"
            f"before: {before!r}\nafter:  {after!r}"
        )

        # state/ contents unchanged
        state_after_entries = set(state_dir.rglob("*"))
        assert state_before_entries == state_after_entries, (
            f"state/ entries changed after op (COMPUTE_ONLY violation):\n"
            f"new: {state_after_entries - state_before_entries}"
        )

    def test_op_returns_dict_with_trailers_key(self) -> None:
        """Op always returns a dict with a 'trailers' key (wire contract shape)."""
        from coordinator_core.ops.commit_anchors import _handler
        result = _run(_handler({"session_id": "", "nature": None}))
        assert isinstance(result, dict)
        assert "trailers" in result
        assert isinstance(result["trailers"], str)

    def test_op_registered_in_registry(self) -> None:
        """Importing the module registers 'commit.anchors' in the op-registry."""
        # Import the ops package so registration side-effects fire
        import coordinator_core.ops  # noqa: F401
        from coordinator_core.ipc import _REGISTRY
        assert "commit.anchors" in _REGISTRY, (
            "'commit.anchors' not found in op-registry — check register_op() call "
            "and ops/__init__.py import"
        )


# ---------------------------------------------------------------------------
# (e) Shared-index scoping — the `paths` param narrows the staged set
# ---------------------------------------------------------------------------

class TestSharedIndexScoping:
    """`paths` scopes the staged-diff read to THIS commit's own pathspec.

    Regression pin for the 2026-08-18 misattribution: on a shared worktree the
    index carries every concurrent session's staged work, so an unscoped
    `git diff --cached --name-only` answers "what is staged in the repo", not
    "what is in this commit". Ship commit 582c7b510 was stamped
    `Deliverable-Id: dlv-fl-core-03` off a PEER's staged plan while committing
    a different deliverable's artifacts. The single-plan ambiguity guard cannot
    catch this — a set of exactly one foreign plan looks unambiguous.
    """

    def _setup(self, tmp_path: Path):
        _reset_equivalence_map_cache()
        _init_repo(tmp_path)
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        (tmp_path / "docs" / "plans" / "peer-plan.md").write_text(
            textwrap.dedent(
                """\
                ---
                plan_id: "pln-peer-plan-aaaaaa"
                deliverable_id: "dlv-peer-deliverable"
                ---
                peer body
                """
            ),
            encoding="utf-8",
        )
        (tmp_path / "mine.md").write_text("my own artifact\n", encoding="utf-8")
        # BOTH staged, as on a shared tree: the peer's plan and my file.
        _git(["add", "docs/plans/peer-plan.md", "mine.md"], tmp_path)

    def _call(self, tmp_path: Path, paths):
        from coordinator_core.ops.commit_anchors import _handler

        params = {"session_id": "", "nature": None}
        if paths is not None:
            params["paths"] = paths
        result = _run(_handler(params, _common_dir(tmp_path)))
        return _parse_trailers(result["trailers"])

    def test_unscoped_read_picks_up_the_peers_staged_plan(self, tmp_path):
        """Pre-fix behaviour, pinned deliberately: with no `paths` scope the op
        still reads the whole index. That is correct for a sole-occupant tree
        and is what every not-yet-updated caller relies on — it must not change
        silently underneath them."""
        self._setup(tmp_path)
        trailers = self._call(tmp_path, paths=None)
        assert trailers.get("Deliverable-Id") == "dlv-peer-deliverable"

    def test_scoping_to_own_paths_omits_the_foreign_plan(self, tmp_path):
        """THE assertion: scoped to a pathspec that excludes the peer's plan,
        no Plan/Plan-Id/Deliverable-Id is emitted at all — omit rather than
        stamp a foreign identity."""
        self._setup(tmp_path)
        trailers = self._call(tmp_path, paths=["mine.md"])
        assert "Plan" not in trailers
        assert "Plan-Id" not in trailers
        assert "Deliverable-Id" not in trailers

    def test_scope_including_own_plan_still_resolves_it(self, tmp_path):
        """The narrowing must not break the case it exists to preserve: a plan
        genuinely inside this commit's pathspec still stamps."""
        self._setup(tmp_path)
        trailers = self._call(tmp_path, paths=["docs/plans/peer-plan.md", "mine.md"])
        assert trailers.get("Plan") == "docs/plans/peer-plan.md"
        assert trailers.get("Deliverable-Id") == "dlv-peer-deliverable"

    def test_backslash_pathspec_still_matches(self, tmp_path):
        """Windows is first-class: a caller handing over OS-native separators
        scopes identically to one handing over forward slashes."""
        self._setup(tmp_path)
        trailers = self._call(tmp_path, paths=[r"docs\plans\peer-plan.md"])
        assert trailers.get("Deliverable-Id") == "dlv-peer-deliverable"

    def test_empty_scope_is_treated_as_no_scope(self, tmp_path):
        """An empty list tells us nothing about scope, so it must NOT be read
        as "this commit touches nothing" and suppress every anchor."""
        self._setup(tmp_path)
        trailers = self._call(tmp_path, paths=[])
        assert trailers.get("Deliverable-Id") == "dlv-peer-deliverable"


# ---------------------------------------------------------------------------
# (f) Confident-wrong-edge guard — divergence across ALL staged artifacts,
# not just staged docs/plans/*.md files (staff-eng review finding 1, C7B).
# ---------------------------------------------------------------------------

class TestStagedArtifactDivergenceOmitsIds:
    """AC14 — a routine pathspec (one plan file plus the handoffs it spawned)
    must not stamp a single plan's `Deliverable-Id:` onto a commit whose
    contents actually belong to several deliverables. Distinct from AC11's
    `compute_missing_trailer_args`-only pin — this exercises `commit.anchors`
    end-to-end via `_handler` against a real staged mise-shaped fixture.
    """

    def _handoff(self, idx: int, deliverable_id: str) -> str:
        return textwrap.dedent(f"""\
            ---
            title: "Handoff {idx}"
            deliverable_id: "{deliverable_id}"
            ---
            handoff body {idx}
            """)

    def test_ten_foreign_handoffs_plus_one_plan_omits_deliverable_id(self, tmp_repo) -> None:
        """One staged plan file plus ten staged handoffs carrying FOREIGN
        `deliverable_id`s (the PM's headline case) → ZERO `Deliverable-Id:`
        lines on the resulting commit — Plan-Id: and Resolves: omitted too,
        since all three ride the same resolver."""
        _reset_equivalence_map_cache()
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(
            textwrap.dedent("""\
                ---
                title: "Test Plan"
                plan_id: "pln-test-plan-abc123"
                deliverable_id: "dlv-test-plan-abc456"
                ---

                # Test Plan
                """)
        )

        handoff_dir = tmp_repo / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)
        handoff_paths = []
        for i in range(10):
            hpath = handoff_dir / f"handoff-{i}.md"
            hpath.write_text(self._handoff(i, f"dlv-foreign-{i}"))
            handoff_paths.append(str(hpath))

        _git(["add", str(plan_file)] + handoff_paths, tmp_repo)

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_repo)
        result = _run(_handler({"session_id": "", "nature": None}, repo_root=common))
        trailers = _parse_trailers(result["trailers"])

        assert "Deliverable-Id" not in trailers, trailers
        assert "Plan-Id" not in trailers, trailers
        assert "Resolves" not in trailers, trailers
        # Plan: (the path, not a join key) is unaffected by the divergence guard.
        assert trailers.get("Plan") == "docs/plans/2026-07-04-test-plan.md"

    def test_staged_handoffs_agreeing_with_plan_still_emit_deliverable_id(self, tmp_repo) -> None:
        """The guard must not over-fire: staged handoffs that all agree with
        the plan's own `deliverable_id` (the ordinary case) still stamp."""
        _reset_equivalence_map_cache()
        plan_dir = tmp_repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_file = plan_dir / "2026-07-04-test-plan.md"
        plan_file.write_text(
            textwrap.dedent("""\
                ---
                title: "Test Plan"
                plan_id: "pln-test-plan-abc123"
                deliverable_id: "dlv-test-plan-abc456"
                ---

                # Test Plan
                """)
        )

        handoff_dir = tmp_repo / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)
        hpath = handoff_dir / "handoff-0.md"
        hpath.write_text(self._handoff(0, "dlv-test-plan-abc456"))

        _git(["add", str(plan_file), str(hpath)], tmp_repo)

        from coordinator_core.ops.commit_anchors import _handler
        common = _common_dir(tmp_repo)
        result = _run(_handler({"session_id": "", "nature": None}, repo_root=common))
        trailers = _parse_trailers(result["trailers"])

        assert trailers.get("Deliverable-Id") == "dlv-test-plan-abc456"
