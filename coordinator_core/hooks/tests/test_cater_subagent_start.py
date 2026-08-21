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

import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks.cater_subagent_start import (
    ADDITIONAL_CONTEXT_CHAR_CAP,
    BLOCKS_COMPANION_MARKER_PREFIX,
    SIDECAR_MISS_MARKER,
    SIDECAR_PATH_MARKER_PREFIX,
    compose_catering,
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
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "coordinator" / "snippets").mkdir(parents=True)
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
# Real-corpus tests (AC1/AC2/AC3) -- skipped without a sibling DoE-claude checkout
# ---------------------------------------------------------------------------

pytestmark_doe = pytest.mark.skipif(
    not DOE_ROOT_PRESENT,
    reason="sibling DoE-claude checkout not resolvable on this machine "
    "(see coordinator_core.testing.doe_root.resolve_doe_root)",
)


@pytestmark_doe
def test_real_code_reviewer_payload_carries_every_resolved_block(tmp_path: Path) -> None:
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

    policy_file = Path(DOE_ROOT) / "coordinator" / "subagent-sandbox-policy.yaml"
    policy_data = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    block_names = policy_data["contract_blocks"][ELIGIBLE_TYPE]

    payload = {
        "agent_type": ELIGIBLE_TYPE,
        "session_id": "ac1-real-code-reviewer",
        "contract_blocks": block_names,
    }
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


@pytestmark_doe
def test_real_staff_eng_payload_spills_blocks_to_companion_file(tmp_path: Path) -> None:
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
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)

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
