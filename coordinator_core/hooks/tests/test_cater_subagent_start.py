"""
coordinator_core.hooks.tests.test_cater_subagent_start -- pytest harness for
the C2 engine-side SubagentStart catering composer.

Two families:

  * Synthetic-fixture tests (no DoE-claude checkout required) -- exercise
    `compose_catering`'s ORDER, marker shapes, and fail-open contract (AC5)
    against a hand-built `coordinator/snippets/` + policy fixture, mirroring
    `test_provision_report.py`'s `git_repo`/`policy_path` fixture
    conventions.
  * Real-corpus tests (skipped when the sibling DoE-claude checkout is not
    resolvable) -- AC1/AC2/AC3 against the actual `subagent-sandbox-
    policy.yaml` and `coordinator/snippets/` on disk, the only artifacts
    that prove this leg reaches the exact population the gating plan names.

Spec backlink: docs/plans/2026-08-21-catering-rides-subagentstart.md (C2)
Module under test: coordinator_core/hooks/cater_subagent_start.py
"""

from __future__ import annotations

import hashlib
import re
import statistics
import subprocess
import time
from pathlib import Path

import pytest

import coordinator_core.hooks.cater_subagent_start as cater_subagent_start
from coordinator_core.hooks.cater_subagent_start import (
    ADDITIONAL_CONTEXT_CHAR_CAP,
    BLOCKS_COMPANION_MARKER_PREFIX,
    SIDECAR_MISS_MARKER,
    SIDECAR_PATH_MARKER_PREFIX,
    _compose_blocks_pointer_text,
    _compose_sidecar_miss_text,
    _compose_sidecar_offer_text,
    _compute_sentinel_leaf,
    _resolve_contract_blocks_payload,
    compose_catering,
)
from coordinator_core.subagent_sandbox.detect_unfilled_sidecar import (
    scan_session_dir,
    split_frontmatter,
)
from coordinator_core.subagent_sandbox.provision_report import (
    _sanitize_segment,
    assemble_contract_blocks_for_payload,
)
from coordinator_core.testing.doe_root import doe_root_and_present

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

DOE_ROOT, DOE_ROOT_PRESENT = doe_root_and_present()

ELIGIBLE_TYPE = "coordinator:code-reviewer"
INELIGIBLE_ON_ROSTER_TYPE = "Explore"
CONTRACT_ONLY_TYPE = "coordinator:atlassian-worker"

SNIPPET_A = "quota-self-detect-preamble"
SNIPPET_A_BODY = "INJECTION-ONLY-CANARY-A: this sentence exists nowhere except snippet A."
SNIPPET_B = "provisioned-scaffold-precedence"
SNIPPET_B_BODY = "INJECTION-ONLY-CANARY-B: this sentence exists nowhere except snippet B."
ROLE_APPEND_CANARY = "INJECTION-ONLY-CANARY-ROLE: role framing text exists nowhere else."


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # `git init` only -- no test in this file ever commits, so a `user.email`/
    # `user.name` config would be dead subprocess weight even before
    # considering that the suite-root conftest (`coordinator_core/conftest.py
    # :: _quarantine_real_home`) already exports GIT_AUTHOR_NAME/EMAIL and
    # GIT_COMMITTER_NAME/EMAIL process-wide for every test. Each spawn here
    # contends with the real-corpus tests' own `resolve_git_root` call
    # against the sibling DoE-claude checkout (2.0s hard timeout,
    # `engine.py :: _resolve_git_root_uncached`) -- measured under load,
    # trimming this fixture's 3 spawns to 1 is what keeps that call clear of
    # the timeout rather than flaking into a false "sidecar_provisioning:
    # missed" read.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "coordinator" / "snippets").mkdir(parents=True)
    # `resolve_plugin_root()` (provision_report.py) resolves the
    # coordinator-claude plugin's CONTENT root independently of this
    # fixture's own git root -- point its `CLAUDE_PLUGIN_ROOT` rung (the
    # documented harness-injected override, and the intended seam for a
    # test to supply its own plugin content) at THIS fixture's
    # `coordinator/` dir so `_assemble_contract_blocks` resolves the
    # synthetic snippets built below rather than whatever plugin happens
    # to be installed on the machine running the suite.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "coordinator"))
    registry = tmp_path / "coordinator" / "snippets" / "registry.toml"
    registry.write_text(
        "schema_version = 1\n\n"
        f'[snippet.{SNIPPET_A}]\n'
        f'sentinel_begin = "<!-- BEGIN {SNIPPET_A} -->"\n'
        f'sentinel_end = "<!-- END {SNIPPET_A} -->"\n'
        'consumers = []\n\n'
        f'[snippet.{SNIPPET_B}]\n'
        f'sentinel_begin = "<!-- BEGIN {SNIPPET_B} -->"\n'
        f'sentinel_end = "<!-- END {SNIPPET_B} -->"\n'
        'consumers = []\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator" / "snippets" / f"{SNIPPET_A}.md").write_text(
        f"<!-- BEGIN {SNIPPET_A} -->\n{SNIPPET_A_BODY}\n<!-- END {SNIPPET_A} -->\n",
        encoding="utf-8",
    )
    (tmp_path / "coordinator" / "snippets" / f"{SNIPPET_B}.md").write_text(
        f"<!-- BEGIN {SNIPPET_B} -->\n{SNIPPET_B_BODY}\n<!-- END {SNIPPET_B} -->\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = tmp_path / "subagent-sandbox-policy.yaml"
    policy.write_text(
        "report_sidecar:\n"
        f"  - {ELIGIBLE_TYPE}\n",
        encoding="utf-8",
    )
    return policy


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch: pytest.MonkeyPatch, policy_path: Path) -> None:
    """`compose_catering` always resolves policy via `load_policy(None)`'s
    own cascade -- the env-var rung is this fixture's control point,
    matching `load_policy`'s own documented resolution order."""
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point `claude_config_dir()` at an empty scratch dir by default -- the
    role-framing leg then fails open to "" (no `plugins/coordinator-claude/
    .../snippets/agent-role-dispatched.md` on disk), isolating the
    synthetic-fixture tests from whatever happens to be installed on the
    machine running them. `test_role_framing_present_and_unconditional`
    below overrides this with its own fixture."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def _payload(agent_type: str, session_id: str, cwd: str, contract_blocks=None) -> dict:
    payload = {"agent_type": agent_type, "session_id": session_id, "cwd": cwd}
    if contract_blocks is not None:
        payload["contract_blocks"] = contract_blocks
    return payload


# ---------------------------------------------------------------------------
# AC1/AC2/AC3-shaped assertions against the synthetic fixture
# ---------------------------------------------------------------------------

def test_eligible_type_gets_sidecar_offer_and_blocks_in_order(git_repo: Path) -> None:
    payload = _payload(
        ELIGIBLE_TYPE, "session-order-1", str(git_repo), contract_blocks=[SNIPPET_A, SNIPPET_B]
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX in result
    assert "\n" + SIDECAR_PATH_MARKER_PREFIX in result, "marker must be on its own line"
    assert SNIPPET_A_BODY in result
    assert SNIPPET_B_BODY in result

    # Canonical order: sidecar offer -> injected blocks -> (role framing,
    # absent here per the autouse fixture above).
    offer_index = result.index(SIDECAR_PATH_MARKER_PREFIX)
    block_index = result.index(SNIPPET_A_BODY)
    assert offer_index < block_index

    # The provisioned file actually exists on disk (AC2).
    marker_line = next(
        line for line in result.splitlines() if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
    )
    rel_path = marker_line[len(SIDECAR_PATH_MARKER_PREFIX):]
    assert (git_repo / rel_path).is_file()


def test_eligible_type_with_missed_provisioning_emits_miss_marker(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5's partial-catering clause: eligible + blocks resolve, but
    provisioning itself comes back empty (simulated by clearing
    session_id, which `_provision` requires)."""
    import coordinator_core.hooks.cater_subagent_start as mod

    monkeypatch.setattr(mod, "_provision", lambda *a, **k: None)
    payload = _payload(ELIGIBLE_TYPE, "session-miss-1", str(git_repo), contract_blocks=[SNIPPET_A])
    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert SNIPPET_A_BODY in result  # blocks leg is independent of the sidecar leg


def test_ineligible_type_stays_silent_on_sidecar_but_still_gets_blocks(git_repo: Path) -> None:
    """A type absent from `report_sidecar:` gets no offer AND no miss
    notice (matches the Agent-path hook's `_is_report_sidecar_eligible`
    gate) -- contract_blocks stays decoupled (gating plan's `provision.py`
    docstring, DR-151) and still fires."""
    payload = _payload(
        CONTRACT_ONLY_TYPE, "session-decoupled-1", str(git_repo), contract_blocks=[SNIPPET_B]
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert SIDECAR_MISS_MARKER not in result
    assert SNIPPET_B_BODY in result


def test_role_framing_present_for_off_roster_and_built_in_types(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: role framing reaches the populations the roster lookup never
    reaches -- unconditional across `subagent_type`, no `contract_blocks`
    row required."""
    claude_dir = tmp_path / "role-claude-config"
    snippet_dir = claude_dir / "plugins" / "coordinator-claude" / "coordinator" / "snippets"
    snippet_dir.mkdir(parents=True)
    (snippet_dir / "agent-role-dispatched.md").write_text(ROLE_APPEND_CANARY, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    for agent_type in (INELIGIBLE_ON_ROSTER_TYPE, "coordinator:git-commit-agent"):
        payload = _payload(agent_type, f"session-role-{agent_type}", str(git_repo))
        result = compose_catering(payload, cwd=str(git_repo))
        assert ROLE_APPEND_CANARY in result, f"role framing missing for {agent_type!r}"


def test_role_framing_lands_last(git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    claude_dir = tmp_path / "role-claude-config-2"
    snippet_dir = claude_dir / "plugins" / "coordinator-claude" / "coordinator" / "snippets"
    snippet_dir.mkdir(parents=True)
    (snippet_dir / "agent-role-dispatched.md").write_text(ROLE_APPEND_CANARY, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    payload = _payload(
        ELIGIBLE_TYPE, "session-order-2", str(git_repo), contract_blocks=[SNIPPET_A]
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert result.rstrip().endswith(ROLE_APPEND_CANARY)
    assert result.index(SNIPPET_A_BODY) < result.index(ROLE_APPEND_CANARY)
    assert result.index(SIDECAR_PATH_MARKER_PREFIX) < result.index(SNIPPET_A_BODY)


def _reflow_tolerant_pattern(phrase: str) -> str:
    """Turn `phrase` into a regex that matches it across a cosmetic line
    reflow -- every run of whitespace in `phrase` becomes `\\s+`, every
    other character is escaped literally. Used so a pinned docstring phrase
    only goes red on an actual content change, never a rewrap."""
    return r"\s+".join(re.escape(word) for word in phrase.split())


def test_docstring_declares_exactly_three_legs_in_order_and_no_fourth() -> None:
    """Pins the module docstring's own declared contract, not prose the
    test re-derives -- a docstring edit that drops a leg, reorders the
    three, or removes the "no fourth" sentence goes red here even though
    `compose_catering` itself never changed. Every pinned phrase is matched
    reflow-tolerantly (`_reflow_tolerant_pattern`), not as a plain substring
    -- a cosmetic rewrap of any of them must never trip this test on its
    own; only an actual change to the declared contract may."""
    doc = cater_subagent_start.__doc__ or ""
    for phrase in (
        "This module composes three legs",
        "sidecar offer (report_sidecar) OR miss notice",
        "-> injected contract blocks",
        "-> role framing (LAST, unconditional, outside any roster lookup)",
    ):
        assert re.search(_reflow_tolerant_pattern(phrase), doc) is not None, phrase
    assert re.search(r"There is no fourth,\s+teammate-clause leg to port", doc) is not None


@pytest.mark.parametrize(
    "blocks_shape,sidecar_variant,blocks_spilled",
    [
        pytest.param("list", "offer", False, id="list-offer-inline"),
        pytest.param("mapping", "offer", False, id="mapping-offer-inline"),
        pytest.param("list", "miss", False, id="list-miss-inline"),
        pytest.param("mapping", "miss", False, id="mapping-miss-inline"),
        pytest.param("list", "offer", True, id="list-offer-spilled"),
        pytest.param("mapping", "offer", True, id="mapping-offer-spilled"),
        pytest.param("list", "miss", True, id="list-miss-spilled"),
        pytest.param("mapping", "miss", True, id="mapping-miss-spilled"),
    ],
)
def test_composed_output_is_wholly_accounted_for_by_the_three_declared_legs(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    blocks_shape: str,
    sidecar_variant: str,
    blocks_spilled: bool,
) -> None:
    """Exhaustiveness, not presence: drives `compose_catering` across the
    2x2x2 matrix of leg-shape combinations the module's own docstring
    admits -- `contract_blocks` LIST-shaped or MAPPING-shaped (routed
    through `_resolve_contract_blocks_payload`, the same transformation
    `compose_catering` itself applies, never the raw payload), the sidecar
    leg resolved to an OFFER or degraded to a MISS notice, and the blocks
    leg kept INLINE or SPILLED to a companion file -- and removes exactly
    the three declared legs, IN THE DECLARED ORDER, from the composed
    string in every combination. Built from the same leg-composing helpers
    `compose_catering` itself calls (`_compose_sidecar_offer_text` /
    `_compose_sidecar_miss_text`, `assemble_contract_blocks_for_payload` /
    `_compose_blocks_pointer_text`), never a re-implementation of
    `compose_catering`'s own joining logic. Nothing may remain in any of
    the eight arms: a fourth leg appended anywhere leaves residue and fails
    this test even though every leg-isolated test elsewhere in this file
    still passes.

    Scope: role framing is driven present in every arm (its own presence/
    absence is covered by `test_role_framing_present_for_off_roster_and_
    built_in_types`); this test's matrix is the sidecar x blocks axes only.

    One boundary, measured rather than assumed: in the OFFER arms the
    expected sidecar leg is rebuilt from the `sidecar_path:` marker parsed
    out of `compose_catering`'s own output, so text fused onto that marker
    line with no separator is absorbed into the expected leg and does not
    register as residue. A LEG carries its own `

` separator and is
    caught in every arm and every position; a bare suffix on the marker
    line is a path-parse concern, not a fourth leg, and belongs to
    `_marker_rel_path`'s own callers.
    """
    claude_dir = tmp_path / f"role-claude-config-exhaustive-{blocks_shape}-{sidecar_variant}-{blocks_spilled}"
    snippet_dir = claude_dir / "plugins" / "coordinator-claude" / "coordinator" / "snippets"
    snippet_dir.mkdir(parents=True)
    (snippet_dir / "agent-role-dispatched.md").write_text(ROLE_APPEND_CANARY, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    if blocks_spilled:
        # Force the spill arm deterministically regardless of the two
        # snippets' actual byte size, rather than authoring a third,
        # oversized snippet fixture just to clear the real cap.
        monkeypatch.setattr(cater_subagent_start, "ADDITIONAL_CONTEXT_CHAR_CAP", 10)

    if blocks_shape == "list":
        contract_blocks = [SNIPPET_A, SNIPPET_B]
    else:
        contract_blocks = {ELIGIBLE_TYPE: [SNIPPET_A, SNIPPET_B]}

    session_id = f"session-exhaustive-{blocks_shape}-{sidecar_variant}-{blocks_spilled}"
    payload = _payload(ELIGIBLE_TYPE, session_id, str(git_repo), contract_blocks=contract_blocks)

    if sidecar_variant == "miss":
        monkeypatch.setattr(cater_subagent_start, "_provision", lambda *a, **k: None)

    result = compose_catering(payload, cwd=str(git_repo))

    if sidecar_variant == "offer":
        sidecar_rel_path = _marker_rel_path(result, SIDECAR_PATH_MARKER_PREFIX)
        expected_sidecar_leg = _compose_sidecar_offer_text(sidecar_rel_path)
        resolved_sidecar_path = sidecar_rel_path
    else:
        assert SIDECAR_MISS_MARKER in result
        expected_sidecar_leg = _compose_sidecar_miss_text()
        resolved_sidecar_path = ""

    assert result.startswith(expected_sidecar_leg), "sidecar leg must lead the composition"
    remainder = result[len(expected_sidecar_leg):]

    expected_role_leg = "\n\n" + ROLE_APPEND_CANARY
    assert remainder.endswith(expected_role_leg), "role framing must trail the composition"
    blocks_leg = remainder[: -len(expected_role_leg)]

    # Route through the same payload-resolution helper `compose_catering`
    # itself calls before handing off to the assembler (Finding 1) -- for
    # the LIST shape this is a documented no-op; for the MAPPING shape it
    # is the row-selection rewrite that this arm exists to exercise.
    resolved_payload = _resolve_contract_blocks_payload(
        dict(payload), agent_type=ELIGIBLE_TYPE, subagent_type=""
    )
    injected_blocks = (
        assemble_contract_blocks_for_payload(
            resolved_payload, cwd=str(git_repo), report_sidecar_path=resolved_sidecar_path
        )
        or ""
    )
    assert injected_blocks, "fixture premise: blocks leg must resolve to non-empty content"

    if blocks_spilled:
        assert BLOCKS_COMPANION_MARKER_PREFIX in blocks_leg
        companion_rel_path = _marker_rel_path(blocks_leg, BLOCKS_COMPANION_MARKER_PREFIX)
        expected_blocks_leg = "\n\n" + _compose_blocks_pointer_text(companion_rel_path)
        assert (git_repo / companion_rel_path).is_file()
        assert injected_blocks in (git_repo / companion_rel_path).read_text(encoding="utf-8")
    else:
        expected_blocks_leg = "\n\n" + injected_blocks

    assert blocks_leg == expected_blocks_leg, (
        "residue between the sidecar and role legs must be exactly the "
        "blocks leg -- any extra text here is an unaccounted fourth leg"
    )


def test_under_cap_type_keeps_blocks_inline_no_companion_file(git_repo: Path) -> None:
    """AC9 amendment, threshold-not-a-switch: a composed total under the
    cap keeps its blocks inline, byte-identical to today (AC1) -- no
    pointer marker, no companion file written."""
    payload = _payload(
        ELIGIBLE_TYPE, "session-under-cap-1", str(git_repo), contract_blocks=[SNIPPET_A, SNIPPET_B]
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert len(result) <= ADDITIONAL_CONTEXT_CHAR_CAP
    assert BLOCKS_COMPANION_MARKER_PREFIX not in result
    assert SNIPPET_A_BODY in result
    assert SNIPPET_B_BODY in result

    session_dir = git_repo / "state" / "subagent-share" / "session-under-cap-1"
    companion_files = list(session_dir.glob("*.blocks.md")) if session_dir.is_dir() else []
    assert companion_files == []


def test_over_cap_write_failure_falls_back_to_inline_blocks(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9 fail-open: the companion-file write is its own try/except --
    a resolution/write failure falls back to today's inline blocks, never
    to silence and never to an exception."""
    import coordinator_core.hooks.cater_subagent_start as mod

    monkeypatch.setattr(mod, "ADDITIONAL_CONTEXT_CHAR_CAP", 1)

    def _boom(*a, **k):
        raise OSError("simulated companion-file write failure")

    monkeypatch.setattr(mod, "_spill_blocks_to_companion", _boom)

    payload = _payload(
        ELIGIBLE_TYPE, "session-spill-fail-1", str(git_repo), contract_blocks=[SNIPPET_A]
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SNIPPET_A_BODY in result
    assert BLOCKS_COMPANION_MARKER_PREFIX not in result


def test_no_teammate_clause_marker_present(git_repo: Path) -> None:
    """Anti-scope: the named-teammate clause is not ported -- its marker
    must never appear even when the payload happens to carry a `name`
    key (a SubagentStart payload never legitimately would, but the
    composer must not accidentally key off it either)."""
    payload = _payload(
        ELIGIBLE_TYPE, "session-no-teammate", str(git_repo), contract_blocks=[SNIPPET_A]
    )
    payload["name"] = "some-teammate"
    result = compose_catering(payload, cwd=str(git_repo))
    assert "teammate_delivery_channel" not in result


# ---------------------------------------------------------------------------
# AC5 -- fail-open on every arm
# ---------------------------------------------------------------------------

def test_malformed_payload_returns_empty_string() -> None:
    assert compose_catering(None) == ""  # type: ignore[arg-type]
    assert compose_catering([]) == ""  # type: ignore[arg-type]
    assert compose_catering("not-a-dict") == ""  # type: ignore[arg-type]


def test_unresolvable_git_root_fails_open(tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    payload = _payload(ELIGIBLE_TYPE, "session-no-root", str(outside), contract_blocks=["x"])
    result = compose_catering(payload, cwd=str(outside))
    assert result == "" or SIDECAR_PATH_MARKER_PREFIX not in result


def test_missing_policy_file_fails_open(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(tmp_path / "does-not-exist.yaml"))
    payload = _payload(ELIGIBLE_TYPE, "session-no-policy", str(git_repo))
    result = compose_catering(payload, cwd=str(git_repo))
    assert result == ""


def test_unenumerated_type_stays_silent(git_repo: Path) -> None:
    payload = _payload("coordinator:not-a-real-type", "session-unenum", str(git_repo))
    result = compose_catering(payload, cwd=str(git_repo))
    assert result == ""


def test_unhandled_exception_in_sidecar_leg_never_propagates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coordinator_core.hooks.cater_subagent_start as mod

    def _boom(*a, **k):
        raise RuntimeError("simulated provisioning failure")

    monkeypatch.setattr(mod, "_provision", _boom)
    payload = _payload(
        ELIGIBLE_TYPE, "session-boom", str(git_repo), contract_blocks=[SNIPPET_A]
    )
    result = compose_catering(payload, cwd=str(git_repo))
    # Sidecar leg blew up and degrades to nothing; the independent blocks
    # leg must still fire (AC5: one leg's failure never suppresses another).
    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert SNIPPET_A_BODY in result


def test_unhandled_exception_in_blocks_leg_never_propagates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coordinator_core.hooks.cater_subagent_start as mod

    def _boom(*a, **k):
        raise RuntimeError("simulated assembly failure")

    monkeypatch.setattr(mod, "assemble_contract_blocks_for_payload", _boom)
    payload = _payload(
        ELIGIBLE_TYPE, "session-boom-2", str(git_repo), contract_blocks=[SNIPPET_A]
    )
    result = compose_catering(payload, cwd=str(git_repo))
    assert SIDECAR_PATH_MARKER_PREFIX in result
    assert SNIPPET_A_BODY not in result


# ---------------------------------------------------------------------------
# C2 -- the missed-sidecar race: sentinel before the child, content states,
# unsanitizable-id fallback
# (docs/plans/2026-08-25-a-missed-sidecar-leaves-a-file-the-em-ca.md)
# ---------------------------------------------------------------------------

def _marker_rel_path(result: str, prefix: str) -> str:
    marker_line = next(line for line in result.splitlines() if line.startswith(prefix))
    return marker_line[len(prefix):]


def test_race_payload_writes_sentinel_before_child_and_names_it_in_marker(
    git_repo: Path,
) -> None:
    """AC1/AC3: a race-shaped named-dispatch payload (no back-pointer row,
    `subagent_type` never resolved) gets its sentinel written to disk by
    `compose_catering` itself -- before any child tool call exists to
    write one -- and the returned marker names that exact literal path."""
    agent_id_raw = "quality-guard@session-11111111"
    payload = _payload("quality-guard", "session-race-1", str(git_repo))
    payload["agent_id"] = agent_id_raw

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER not in result
    assert SIDECAR_PATH_MARKER_PREFIX in result
    rel_path = _marker_rel_path(result, SIDECAR_PATH_MARKER_PREFIX)
    assert (git_repo / rel_path).is_file()

    expected_leaf = _compute_sentinel_leaf(agent_id_raw)
    assert rel_path == f"state/subagent-share/session-race-1/{expected_leaf}.md"


def test_em_side_leaf_derivation_matches_write_side_derivation(git_repo: Path) -> None:
    """AC2: an EM computing the sentinel leaf from (teammate name, session
    short8) alone -- the exact inputs it has on its own side of the race,
    with no on-disk lookup -- gets the same leaf `_write_miss_sentinel`
    actually wrote."""
    teammate_name = "em-derivable"
    session_short8 = "abcdef12"
    agent_id_raw = f"{teammate_name}@session-{session_short8}"

    em_side_leaf = _compute_sentinel_leaf(agent_id_raw)

    payload = _payload(teammate_name, "session-em-derive-1", str(git_repo))
    payload["agent_id"] = agent_id_raw
    result = compose_catering(payload, cwd=str(git_repo))
    rel_path = _marker_rel_path(result, SIDECAR_PATH_MARKER_PREFIX)

    assert rel_path.endswith(f"{em_side_leaf}.md")
    assert (git_repo / rel_path).is_file()


def test_sentinel_content_states_via_frontmatter_key(git_repo: Path) -> None:
    """AC5: the three states are distinguishable purely from the sentinel's
    own declared `scaffold_sha256` frontmatter key, over the body BELOW
    frontmatter -- never a byte-compare against a shared template
    constant. Unmodified: declared hash matches the current body's hash.
    Filled: body diverges from the declared baseline (hash mismatch, not
    absence). Corrupt-and-rewritable: the declared key itself is gone."""
    agent_id_raw = "content-states@session-22222222"
    payload = _payload("content-states", "session-content-1", str(git_repo))
    payload["agent_id"] = agent_id_raw

    result = compose_catering(payload, cwd=str(git_repo))
    rel_path = _marker_rel_path(result, SIDECAR_PATH_MARKER_PREFIX)
    sentinel_path = git_repo / rel_path
    text = sentinel_path.read_text(encoding="utf-8")

    declared_match = re.search(r"^scaffold_sha256: (\S+)$", text, re.MULTILINE)
    assert declared_match is not None, "unmodified sentinel must declare scaffold_sha256"
    declared_hash = declared_match.group(1)

    # State 1: unmodified -- the declared hash matches the current body.
    # `split_frontmatter` leaves the fence's own trailing newline attached
    # (a parsing artefact of that shared helper, not part of the body the
    # write side hashed) -- stripped here so this is still a content
    # comparison, never a byte-compare against a shared template constant.
    body = split_frontmatter(text).lstrip("\n")
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == declared_hash

    # State 2: filled -- the child wrote real content, body no longer
    # matches the declared baseline (frontmatter untouched).
    filled_text = text.replace(body, body + "some real findings here\n")
    filled_body = split_frontmatter(filled_text).lstrip("\n")
    assert hashlib.sha256(filled_body.encode("utf-8")).hexdigest() != declared_hash

    # State 3: corrupt-and-rewritable -- the declared key itself is absent,
    # not a fourth silent state conflated with "unmodified".
    corrupt_text = re.sub(r"^scaffold_sha256: \S+\n", "", text, flags=re.MULTILINE)
    corrupt_match = re.search(r"^scaffold_sha256: (\S+)$", corrupt_text, re.MULTILINE)
    assert corrupt_match is None


def test_mangling_collision_pair_gets_distinct_leaves_no_reuse(git_repo: Path) -> None:
    """AC4 -- the report-misattribution case, and the most important test
    here (retracted-premise section, governing plan): the SUBAGENT-side raw
    `a<name>-<16hex>` shape delegates to `resolve_subagent_identity`, which
    extracts `name` via a permissive `(.+)` capture and embeds it verbatim
    in the canonical id -- unlike a directly-supplied canonical id, `name`
    here is NOT pre-validated against `_TEAMMATE_CANONICAL_RE`'s charset,
    so it can legitimately carry a `/`. Two distinct teammate names
    (`feature/auth-review` and `featureauth-review`) then canonicalize to
    two distinct agent_ids that `_sanitize_segment` mangles to the SAME
    stem. The digest (over the RAW agent_id, not the sanitized one) must
    still separate them into distinct leaves and distinct sentinel files --
    an idempotent-hit reuse across this pair would mean one agent reading
    another's findings."""
    session_id = "11111111"
    raw_a = "afeature/auth-review-0123456789abcdef"
    raw_b = "afeatureauth-review-0123456789abcdef"

    payload_a = _payload("collide-a", session_id, str(git_repo))
    payload_a["agent_id"] = raw_a
    payload_b = _payload("collide-b", session_id, str(git_repo))
    payload_b["agent_id"] = raw_b

    result_a = compose_catering(payload_a, cwd=str(git_repo))
    result_b = compose_catering(payload_b, cwd=str(git_repo))

    rel_path_a = _marker_rel_path(result_a, SIDECAR_PATH_MARKER_PREFIX)
    rel_path_b = _marker_rel_path(result_b, SIDECAR_PATH_MARKER_PREFIX)

    canon_a = f"feature/auth-review@session-{session_id}"
    canon_b = f"featureauth-review@session-{session_id}"
    assert canon_a != canon_b
    assert _sanitize_segment(canon_a) == _sanitize_segment(canon_b), (
        "fixture premise: both canonical ids must mangle to the same sanitized stem"
    )
    assert _compute_sentinel_leaf(canon_a) != _compute_sentinel_leaf(canon_b)

    assert rel_path_a != rel_path_b
    assert (git_repo / rel_path_a).is_file()
    assert (git_repo / rel_path_b).is_file()


def test_raw_fallback_shape_gets_no_sentinel(git_repo: Path) -> None:
    """AC2: the subagent-side raw `a<name>-<16hex>` fallback shape carries
    16 hex digits no EM can derive -- a sentinel keyed on it would be
    unpollable by construction. A short `session_id` (<8 chars) forces the
    the Staff Engineer F4 fallback in `_canonical_agent_id`, so the raw id survives
    unchanged rather than being delegated into the canonical shape."""
    raw_id = "aworker-0123456789abcdef"
    payload = _payload("worker", "abc", str(git_repo))
    payload["agent_id"] = raw_id

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result
    session_dir = git_repo / "state" / "subagent-share" / "abc"
    assert not session_dir.exists() or list(session_dir.glob("*.md")) == []


def test_resolver_exception_arm_gets_no_path_marker_body(git_repo: Path) -> None:
    """AC3: the third miss arm -- `agent_type` and `subagent_type` both
    falsy -- has an empty `agent_id`, so no sentinel is possible; the
    no-path body is unconditional, and the retired "the path your agent
    definition names" instruction is gone from it."""
    payload = {"agent_type": "", "session_id": "session-resolver-exc-1", "cwd": str(git_repo)}

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result
    assert "the path your agent definition names" not in result


def test_race_routes_to_sentinel_and_never_terminates_inline(git_repo: Path) -> None:
    """AC9: the rendered block a persona receives in the race routes to the
    sentinel path and never falls back to the inline "report your findings
    inline" body. Driven against the SYNTHETIC fixture (not the DOE-gated
    real corpus), so this assertion FAILS rather than silently skips when
    its own subject is unresolvable -- a skip-when-absent test passes when
    its own subject is missing, which is exactly the failure mode this
    plan exists to remove."""
    agent_id_raw = "route-check@session-33333333"
    payload = _payload("route-check", "session-route-1", str(git_repo))
    payload["agent_id"] = agent_id_raw

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_PATH_MARKER_PREFIX in result
    assert SIDECAR_MISS_MARKER not in result
    assert "Report your findings inline in your reply" not in result
    rel_path = _marker_rel_path(result, SIDECAR_PATH_MARKER_PREFIX)
    assert (git_repo / rel_path).is_file()


def test_sentinel_write_failure_falls_back_to_miss_marker_not_dropped(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: the try/except lives INSIDE `_write_miss_sentinel`, around the
    write alone -- monkeypatching the underlying atomic rename (not the
    whole function) to raise proves the marker survives a write failure
    rather than being dropped by `compose_catering`'s outer wrap, which
    would zero BOTH `_resolve_sidecar_leg` return values and lose the miss
    marker entirely (strictly worse than today).

    Patches `pathlib.Path.replace`, not `mod.os.replace`: the module stopped
    importing `os` (it used it for exactly this rename, and the bare import
    tripped `test_module_source_never_spawns_a_process`, whose
    `_SPAWN_SIGNATURES` cannot statically tell `import os` from
    `os.system`). Same seam, same single call, one layer down."""

    def _boom(*a, **k):
        raise OSError("simulated atomic-rename failure")

    monkeypatch.setattr(Path, "replace", _boom)

    agent_id_raw = "write-fail@session-44444444"
    payload = _payload("write-fail", "session-writefail-1", str(git_repo))
    payload["agent_id"] = agent_id_raw

    result = compose_catering(payload, cwd=str(git_repo))

    assert SIDECAR_MISS_MARKER in result
    assert SIDECAR_PATH_MARKER_PREFIX not in result


def test_sentinel_write_cost_and_zero_spawns(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6: cost pinned by `perf_counter` (never `process_time` -- this
    box's granularity is 15.625ms, unable to resolve a sub-millisecond
    write), N>=1000, median AND p99, warm (idempotent-hit reuse) and cold
    (fresh session dir per iteration) measured separately. The zero-spawn
    assertion is a REGRESSION GUARD, not a measurement -- `_write_miss_
    sentinel` resolves its git root via `_show_toplevel_no_spawn`
    (walk-only), never `resolve_git_root` (which shells out to `git
    rev-parse`), so this leg must carry zero subprocess spawns."""
    n_iterations = 1000

    spawn_calls: list = []
    orig_run = subprocess.run

    def _tracking_run(*args, **kwargs):
        spawn_calls.append(args)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _tracking_run)

    # Warm: one session dir, repeated calls hit the idempotent-hit branch
    # after the first write.
    warm_payload = _payload("cost-warm", "session-cost-warm-1", str(git_repo))
    warm_payload["agent_id"] = "cost-warm@session-55555555"

    warm_samples = []
    for _ in range(n_iterations):
        start = time.perf_counter()
        compose_catering(warm_payload, cwd=str(git_repo))
        warm_samples.append(time.perf_counter() - start)

    # Cold: a fresh session dir (and fresh sentinel leaf) every iteration,
    # so every call pays the actual write, never the idempotent-hit path.
    cold_samples = []
    for i in range(n_iterations):
        cold_payload = _payload(f"cost-cold-{i}", f"session-cost-cold-{i}", str(git_repo))
        cold_payload["agent_id"] = f"cost-cold-{i}@session-66666{i:03d}"
        start = time.perf_counter()
        compose_catering(cold_payload, cwd=str(git_repo))
        cold_samples.append(time.perf_counter() - start)

    assert spawn_calls == [], "the race arm must carry zero subprocess spawns"

    warm_samples.sort()
    cold_samples.sort()
    median_warm = statistics.median(warm_samples)
    p99_warm = warm_samples[int(len(warm_samples) * 0.99) - 1]
    median_cold = statistics.median(cold_samples)
    p99_cold = cold_samples[int(len(cold_samples) * 0.99) - 1]

    # Sanity bounds only -- this box's own absolute figures are recorded in
    # the plan's AC6 (warm median 0.211ms / p99 0.549ms; cold median
    # 0.244ms / p99 0.617ms / max 41.371ms); a hard-pinned bound here would
    # flake on a slower peer box rather than catch a real regression.
    assert median_warm < 0.1
    assert p99_warm < 0.5
    assert median_cold < 0.1
    assert p99_cold < 0.5


def test_sentinel_is_flagged_by_existing_unfilled_detector(git_repo: Path) -> None:
    """AC12: the sentinel is flagged by the EXISTING idle-detector with no
    new mechanism -- `detect_unfilled_sidecar.scan_session_dir` globs
    `*.md` under the session dir and flags `status: open` + unfilled body.
    This is the interlock that closes the root spinoff's loudness AC: a
    sentinel now exists where the race previously left nothing at all for
    that detector to find."""
    agent_id_raw = "detector-check@session-77777777"
    payload = _payload("detector-check", "session-detector-1", str(git_repo))
    payload["agent_id"] = agent_id_raw

    result = compose_catering(payload, cwd=str(git_repo))
    rel_path = _marker_rel_path(result, SIDECAR_PATH_MARKER_PREFIX)
    assert (git_repo / rel_path).is_file()

    verdicts = scan_session_dir(str(git_repo), "session-detector-1")

    assert len(verdicts) == 1
    assert verdicts[0].status == "open"
    assert verdicts[0].unfilled is True
    assert verdicts[0].flagged is True


# ---------------------------------------------------------------------------
# Real-corpus tests (AC1/AC2/AC3) -- skipped without a sibling DoE-claude checkout
# ---------------------------------------------------------------------------

pytestmark_doe = pytest.mark.skipif(
    not DOE_ROOT_PRESENT,
    reason="sibling DoE-claude checkout not resolvable on this machine "
    "(see coordinator_core.testing.doe_root.resolve_doe_root)",
)


@pytestmark_doe
def test_real_code_reviewer_payload_carries_every_resolved_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: real `coordinator:code-reviewer` payload against the real
    `subagent-sandbox-policy.yaml` + `coordinator/snippets/` -- every
    resolved `contract_blocks` entry must be present, matched on the
    canonical spec's own injection-only substring convention (each block's
    body, not a semantic presence check -- see the gating plan's
    Anti-scope)."""
    import yaml

    from coordinator_core.snippet_sync.registry import get_snippet_entry, load_registry
    from coordinator_core.subagent_sandbox.provision_report import (
        _extract_contract_block_body,
    )

    # The suite-root quarantine (`coordinator_core/conftest.py ::
    # _quarantine_real_home`) deliberately seeds `.doe-root` with a
    # throwaway stub, not the real sibling checkout, so
    # `resolve_plugin_root()`'s rungs 2/3 cannot see the real corpus this
    # test exists to exercise. Point its rung-1 `CLAUDE_PLUGIN_ROOT`
    # override straight at DOE_ROOT's own content root instead.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(Path(DOE_ROOT) / "coordinator"))

    policy_file = Path(DOE_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    policy_data = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    block_names = policy_data["contract_blocks"][ELIGIBLE_TYPE]

    session_id = "ac1-real-code-reviewer"
    payload = {
        "agent_type": ELIGIBLE_TYPE,
        "session_id": session_id,
        "contract_blocks": block_names,
    }
    # Resolving against the real corpus means `cwd` is the sibling checkout,
    # so provisioning writes into a PEER's working tree -- which is tracked
    # there, not ignored. Anything this test leaves behind is reconciliation
    # work for whoever runs `git status` in that repo next, so the session
    # directory comes back out however this test exits.
    session_dir = Path(DOE_ROOT) / "state" / "subagent-share" / session_id
    try:
        result = compose_catering(payload, cwd=DOE_ROOT)

        snippets_dir = Path(DOE_ROOT) / "coordinator" / "snippets"
        registry_data = load_registry(snippets_dir / "registry.toml")
        for name in block_names:
            entry = get_snippet_entry(registry_data, name)
            header_style = entry.get("header_style", "sentinel-embedded")
            snippet_text = (snippets_dir / f"{name}.md").read_text(encoding="utf-8")
            body = _extract_contract_block_body(
                snippet_text, header_style, entry["sentinel_begin"], entry["sentinel_end"]
            )
            assert body is not None, f"could not extract real block {name!r}"
            # Placeholder-bearing blocks resolve {{...}}; check a substring that
            # survives placeholder substitution rather than the raw body.
            probe = body.strip().splitlines()[0][:40]
            assert probe in result, f"block {name!r} not present in composed catering text"
    finally:
        import shutil

        shutil.rmtree(session_dir, ignore_errors=True)


@pytestmark_doe
def test_real_staff_eng_payload_spills_blocks_to_companion_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9 amendment: `coordinator:staff-eng` (the widest `contract_blocks`
    row on disk, measured ~31,913 composed chars) must spill its blocks leg
    to a companion file rather than blow the `additionalContext` cap --
    injection-only substring checks, not a semantic presence check
    (Anti-scope)."""
    import os

    import yaml

    from coordinator_core.snippet_sync.registry import get_snippet_entry, load_registry
    from coordinator_core.subagent_sandbox.provision_report import (
        _extract_contract_block_body,
    )

    OVER_CAP_TYPE = "coordinator:staff-eng"

    policy_file = Path(DOE_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    policy_data = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    block_names = policy_data["contract_blocks"][OVER_CAP_TYPE]

    snippets_dir = Path(DOE_ROOT) / "coordinator" / "snippets"
    registry_data = load_registry(snippets_dir / "registry.toml")
    probes = []
    for name in block_names:
        entry = get_snippet_entry(registry_data, name)
        header_style = entry.get("header_style", "sentinel-embedded")
        snippet_text = (snippets_dir / f"{name}.md").read_text(encoding="utf-8")
        body = _extract_contract_block_body(
            snippet_text, header_style, entry["sentinel_begin"], entry["sentinel_end"]
        )
        assert body is not None, f"could not extract real block {name!r}"
        probes.append(body.strip().splitlines()[0][:40])

    import shutil

    # See the sibling AC1 test above for why this override is required:
    # the suite-root quarantine stubs `.doe-root` so `resolve_plugin_root()`
    # cannot otherwise see the real corpus.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(Path(DOE_ROOT) / "coordinator"))

    session_dir = Path(DOE_ROOT) / "state" / "subagent-share" / "ac9-real-staff-eng"
    os.environ["SUBAGENT_SANDBOX_POLICY"] = str(policy_file)
    try:
        payload = {
            "agent_type": OVER_CAP_TYPE,
            "session_id": "ac9-real-staff-eng",
            "contract_blocks": block_names,
        }
        result = compose_catering(payload, cwd=DOE_ROOT)

        assert len(result) <= ADDITIONAL_CONTEXT_CHAR_CAP
        assert result.count(BLOCKS_COMPANION_MARKER_PREFIX) == 1
        for probe in probes:
            assert probe not in result, f"block probe {probe!r} leaked into additionalContext"

        marker_line = next(
            line for line in result.splitlines() if line.startswith(BLOCKS_COMPANION_MARKER_PREFIX)
        )
        companion_rel_path = marker_line[len(BLOCKS_COMPANION_MARKER_PREFIX):]
        companion_file = Path(DOE_ROOT) / companion_rel_path
        assert companion_file.is_file()
        companion_text = companion_file.read_text(encoding="utf-8")
        for probe in probes:
            assert probe in companion_text, f"block probe {probe!r} missing from companion file"

        # Sidecar offer and role framing (if present) keep their canonical
        # relative order around the pointer -- same order as today.
        if SIDECAR_PATH_MARKER_PREFIX in result:
            assert result.index(SIDECAR_PATH_MARKER_PREFIX) < result.index(
                BLOCKS_COMPANION_MARKER_PREFIX
            )
    finally:
        os.environ.pop("SUBAGENT_SANDBOX_POLICY", None)
        shutil.rmtree(session_dir, ignore_errors=True)


@pytestmark_doe
def test_real_run_report_sidecar_provisioned_and_marker_present(tmp_path: Path) -> None:
    """AC2: real policy/eligibility check against the real
    `subagent-sandbox-policy.yaml`, in a scratch git repo (the sidecar file
    itself must land under `state/subagent-share/<session>/`, which must
    not be DoE-claude's own tree)."""
    # `git init` only -- see `git_repo` fixture's docstring-equivalent
    # comment above for why the `user.email`/`user.name` config calls this
    # test never needed are cut: no commit happens here, and trimming
    # subprocess spawns keeps `resolve_git_root`'s own 2.0s-timeout spawn
    # (against the sibling DoE-claude checkout, right below) clear of the
    # load-driven contention that flaked it into a false
    # "sidecar_provisioning: missed" read.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    policy_file = Path(DOE_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    import os

    os.environ["SUBAGENT_SANDBOX_POLICY"] = str(policy_file)
    try:
        payload = {"agent_type": ELIGIBLE_TYPE, "session_id": "ac2-real-session"}
        result = compose_catering(payload, cwd=str(tmp_path))
    finally:
        os.environ.pop("SUBAGENT_SANDBOX_POLICY", None)

    assert SIDECAR_PATH_MARKER_PREFIX in result
    marker_line = next(
        line for line in result.splitlines() if line.startswith(SIDECAR_PATH_MARKER_PREFIX)
    )
    rel_path = marker_line[len(SIDECAR_PATH_MARKER_PREFIX):]
    assert (tmp_path / rel_path).is_file()


def test_named_dispatch_miss_notice_names_a_channel_that_delivers():
    """The no-path miss body must not tell a NAMED teammate to reply inline.

    A named teammate's final assistant text is never returned to the
    dispatcher -- SendMessage's own contract says so ("Your plain text output
    is NOT visible to other agents"). The unnamed body's "report your findings
    inline in your reply" is correct for an ordinary subagent and names the one
    dead channel for this population.

    Not hypothetical: on 2026-08-26 six named `general-purpose` agents each
    wrote a complete 7-13K diagnosis, ended `stop_reason: end_turn`, reached
    the dispatching EM as a bare idle notification, and were recovered only by
    reading their transcript jsonl off disk. They had followed this
    instruction exactly. Silence was indistinguishable from having done
    nothing, which is the expensive part -- the EM re-did all six by hand.
    """
    named = _compose_sidecar_miss_text("", is_named=True)
    assert "SendMessage" in named
    assert "inline in your reply" not in named
    assert SIDECAR_MISS_MARKER in named

    unnamed = _compose_sidecar_miss_text("")
    assert "inline in your reply" in unnamed
    assert "SendMessage" not in unnamed

    # A sentinel path outranks both: there IS a file, so the channel question
    # does not arise and the body is shared.
    assert _compose_sidecar_miss_text("/x/y.md", is_named=True) == (
        _compose_sidecar_miss_text("/x/y.md")
    )
