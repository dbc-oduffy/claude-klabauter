"""test_coordinator_registry.py — golden tests for bin/lib/coordinator_registry.py.

Asserts that the shared registry loader derives the expected frozensets and dicts
from schemas/coordinator-registry.manifest.json, byte-equal to the pre-refactor
literal tuples in coordinator-doc-new.

Converted from a hand-rolled runner (module-level assertion list + sys.exit)
to collectable pytest functions with plain `assert`.

Run: python3 -m pytest coordinator/bin/tests/test_coordinator_registry.py

Spec backlink: docs/plans/2026-07-05-central-identity-flip-completion.md § C1/C2
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Bootstrap sys.path so coordinator_registry is importable from bin/lib/.
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import coordinator_registry as reg  # noqa: E402

# ---------------------------------------------------------------------------
# AC-1: KNOWN_TYPES — exact 31-type set
#
# Golden pin derived from schemas/coordinator-registry.manifest.json (source
# of truth). Reconciled 2026-07-25: this pin had drifted silently because the
# hand-rolled fail_test() helper (fixed in 23f65fce) let these assertions run
# to completion without ever raising, so three real upstream manifest edits
# in example-doctrine-repo never got mirrored here:
#   - "flight-recorder" REMOVED — example-doctrine-repo commit 3aa9a79f ("C8(subsume): retire
#     flight-recorder — rm schema, repoint registry+artifact-shape to
#     run-report") deleted the flight-recorder schema and repointed the
#     registry to run-report; the type was subsumed, not merely renamed.
#   - "run-report" ADDED — the same 3aa9a79f repoint.
#   - "tier-u-grant" ADDED — example-doctrine-repo commit 58cdc600 ("Tier-U grant token schema +
#     manifest registration").
#   - "sizing-object" ADDED — example-doctrine-repo commit adf618d5 ("register sizing-object
#     doc-type — manifest row + drift-guard fixture").
#   - "subagent-sidecar" ADDED — manifest registration closing AC-10's unmet
#     half (agent-side decision-object container scaffolder, schemaName null
#     — schema-of-record is schemas/decision-object.schema.json $defs/
#     subagent_sidecar, not a standalone file); retires the coordinator-doc-
#     new / type_enum.py local shims that pre-dated this manifest row.
# Reconciled 2026-08-02 (stale-test cleanup, triage-F): example-doctrine-repo commit 410eae0d1
# ("manifest + skills: --type and kind now agree", deliverable
# dlv-baton-kind-vocabulary-one-axis-per-field-1be219) renamed the docTypes
# entries so the --type flag agrees with the kind value each scaffolds:
#   - "spinoff-roadmap" RENAMED to "roadmap-baton" (legacy spelling remains a
#     permanent CLI-side alias in coordinator-doc-new, not a second manifest
#     row).
#   - "spinoff-goal" RENAMED to "goal-seed" (same alias treatment).
#   - "spinoff-roadmap-creator" RENAMED to "roadmap-seed" (same alias
#     treatment).
# Prior reconciliation history (2026-07-12: spike-result, strategic-self-
# description, workflow; 2026-07-11: spinoff-goal, spinoff-roadmap-creator,
# goal, recovery) retained below for context. Do NOT weaken this to a
# subset/superset check — it is an exact-set pin; add new entries here in the
# same commit that adds a type to the manifest.
# ---------------------------------------------------------------------------
_EXPECTED_KNOWN_TYPES: frozenset[str] = frozenset({
    "handoff",
    "spinoff",
    "roadmap-baton",
    "goal-seed",
    "roadmap-seed",
    "recovery",
    "memo",
    "plan",
    "decision",
    "audit-record",
    "problem-set",
    "completion",
    "goal",
    "health-status",
    "run-report",
    "research-synthesis",
    "review-findings",
    "review",
    "prior-art-check",
    "plan-coverage-check",
    "docs-check",
    "improvement-queue",
    "bug-backlog",
    "debt-backlog",
    "lesson",
    "spike-result",
    "strategic-self-description",
    "workflow",
    "tier-u-grant",
    "sizing-object",
    "subagent-sidecar",
})


def test_known_types():
    assert reg.KNOWN_TYPES == _EXPECTED_KNOWN_TYPES


def test_known_types_count():
    # Sanity: explicit count check surfaces the delta more quickly when a new
    # type is added without updating the expected set above.
    assert len(reg.KNOWN_TYPES) == 31


def test_sidecar_types():
    assert reg.SIDECAR_TYPES == frozenset(
        {"review", "prior-art-check", "plan-coverage-check", "docs-check"}
    )


def test_queue_types():
    assert reg.QUEUE_TYPES == frozenset(
        {"improvement-queue", "bug-backlog", "debt-backlog"}
    )


def test_repo_aliases():
    assert reg.REPO_ALIASES == {"example_game_workbench_repo": "example-game-repo"}


def test_receiver_em_aliases():
    # Inverse of REPO_ALIASES.
    assert reg.RECEIVER_EM_ALIASES == {"example-game-repo": "example_game_workbench_repo"}


def test_central_receiver_ids():
    # Includes example-doctrine-repo-em as forgiving alias (C1).
    assert reg.CENTRAL_RECEIVER_IDS == frozenset(
        {"claude-central-em", "central-em", "central", "example-doctrine-repo-em"}
    )


# AC-7: CENTRAL_REPO_BASENAMES retired (C1 — basename anchor abandoned; the
# manifest key was removed; the Python constant is gone). No assertion here.
# The validate-frontmatter-schema.js consumer must be updated separately.


def test_sidecar_suffixes():
    # Review F2/F3 — new symbol replacing local _SIDECAR_SUFFIX.
    assert reg.SIDECAR_SUFFIXES == {
        "review": "review",
        "prior-art-check": "prior-art-check",
        "plan-coverage-check": "plan-coverage-check",
        "docs-check": "docs-check",
    }


# ---------------------------------------------------------------------------
# AC-9: repo_key_to_em_id — central anchor and normal cases (C1)
#
# repos.example_doctrine_repo resolves to the manifest-derived canonical central identity
# (identity.centralReceiverIds[0] == "example-doctrine-repo-em"), NOT the retired
# "claude-central-em" literal — see _central_canonical_id() in
# coordinator_registry.py. "claude-central-em" remains a valid receiver alias
# (see CENTRAL_RECEIVER_IDS) but is no longer the canonical return here.
# ---------------------------------------------------------------------------


def test_repo_key_to_em_id_example_doctrine_repo_canonical():
    assert reg.repo_key_to_em_id("repos.example_doctrine_repo") == "example-doctrine-repo-em"


def test_central_canonical_id():
    assert reg._central_canonical_id() == "example-doctrine-repo-em"


def test_repo_key_to_em_id_example_retrieval_repo():
    assert reg.repo_key_to_em_id("repos.example_retrieval_repo") == "example-retrieval-repo-em"


def test_repo_key_to_em_id_example_game_repo_alias():
    assert reg.repo_key_to_em_id("repos.example_game_workbench_repo") == "example-game-repo-em"


# ---------------------------------------------------------------------------
# AC-10: em_id_for_root — central, unregistered, None cases (C1)
#
# Uses the actual example-doctrine-repo repo root derived from __file__ as the repos.example_doctrine_repo path.
# ---------------------------------------------------------------------------
_EXAMPLE_DOCTRINE_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_em_id_for_root_example_doctrine_repo_canonical():
    assert reg.em_id_for_root(
        _EXAMPLE_DOCTRINE_REPO_ROOT, {"repos.example_doctrine_repo": _EXAMPLE_DOCTRINE_REPO_ROOT}
    ) == "example-doctrine-repo-em"


def test_em_id_for_root_none():
    assert reg.em_id_for_root(
        None, {"repos.example_doctrine_repo": _EXAMPLE_DOCTRINE_REPO_ROOT}
    ) == "unknown-sender-em"


def test_em_id_for_root_basename_fallback():
    # ~/.claude is NOT special-cased; resolves via basename fallback.
    fake_claude_home = os.path.expanduser("~/.claude")
    assert reg.em_id_for_root(fake_claude_home, {}) == ".claude-em"


def test_em_id_for_root_registered_non_central_loop_step_3():
    # Step 3 — registered non-central repo via the loop (F2: previously untested).
    # Uses a non-existent fake path; _same_path falls back to normcase+realpath
    # string comparison, which matches when root == registered path.
    fake_example_game_repo_root = "/nonexistent/fake-example-game-repo-for-test"
    assert reg.em_id_for_root(
        fake_example_game_repo_root, {"repos.example_game_workbench_repo": fake_example_game_repo_root}
    ) == "example-game-repo-em"


def test_example_doctrine_repo_em_alias_in_central_receiver_ids():
    assert "example-doctrine-repo-em" in reg.CENTRAL_RECEIVER_IDS
