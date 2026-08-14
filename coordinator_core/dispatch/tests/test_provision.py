"""
coordinator_core.dispatch.tests.test_provision -- pytest harness for the
spawn-time subagent-sidecar decision-object provisioner (W2-B3).

Mirrors coordinator_core/subagent_sandbox/tests/test_provision_report.py's
fixture conventions (git_repo/policy_path, synthetic payload dicts) since
coordinator_core.dispatch.provision deliberately reuses the same
eligibility/session/path resolution primitives.

Spec backlink: DoE-claude:pln-canonical-resolution-engine-6eea37 § W2-B3
Module under test: coordinator_core/dispatch/provision.py
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml

from coordinator_core.dispatch.provision import (
    _build_sidecar_text,
    provision_subagent_sidecar,
    main as provision_main,
)

SIDECAR_ELIGIBLE_TYPE = "coordinator:executor"
INELIGIBLE_TYPE = "coordinator:some-other-agent"

BARE_HEX_AGENT_ID = "abc123def4567890"

_EMIT_RE = re.compile(
    r'^state/subagent-share/(?P<session>[^/]+)/(?P<label>[^/]+)-sidecar-(?P<nonce>[0-9a-f]{8})\.md$'
)


def _sanitize_expected(seg: str) -> str:
    """Mirror provision._sanitize_segment's (imported from provision_report)
    whitelist for test expectations."""
    return re.sub(r"[^A-Za-z0-9._-]", "", seg)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repo rooted at tmp_path (so resolve_git_root behaves
    exactly as it does against a production checkout)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = {
        "report_sidecar": [SIDECAR_ELIGIBLE_TYPE],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _payload(
    *,
    agent_id: Optional[str] = None,
    agent_type: Optional[str] = None,
    session_id: Optional[str] = None,
    provision_key: Optional[str] = None,
    plan: Optional[str] = None,
    chunk: Optional[str] = None,
) -> dict:
    payload: dict = {}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if session_id is not None:
        payload["session_id"] = session_id
    if provision_key is not None:
        payload["provision_key"] = provision_key
    if plan is not None:
        payload["plan"] = plan
    if chunk is not None:
        payload["chunk"] = chunk
    return payload


def _run(
    payload: dict,
    policy_path: Path,
    git_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    exit_code = provision_main(["--policy", str(policy_path), "--cwd", str(git_root)])
    captured = capsys.readouterr()
    return exit_code, captured.out


# ---------------------------------------------------------------------------
# Core eligibility + shape matrix
# ---------------------------------------------------------------------------

def test_eligible_agent_type_creates_sidecar_and_emits_json(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-eligible-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=SIDECAR_ELIGIBLE_TYPE, session_id=session_id
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    match = _EMIT_RE.match(envelope["subagent_sidecar"])
    assert match is not None
    assert match.group("session") == session_id
    assert match.group("label") == _sanitize_expected(SIDECAR_ELIGIBLE_TYPE)

    doc_path = git_repo / envelope["subagent_sidecar"]
    assert doc_path.is_file()


def test_ineligible_agent_type_provisions_nothing(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-ineligible-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=INELIGIBLE_TYPE, session_id=session_id
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    share_root = git_repo / "state" / "subagent-share"
    if share_root.exists():
        assert list(share_root.rglob("*.md")) == []


def test_missing_session_id_fails_open(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    payload = _payload(agent_id=BARE_HEX_AGENT_ID, agent_type=SIDECAR_ELIGIBLE_TYPE)
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


def test_malformed_stdin_fails_open(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json{"))
    exit_code = provision_main(["--policy", str(policy_path), "--cwd", str(git_repo)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""


# ---------------------------------------------------------------------------
# Sidecar shape: decision-object fields (schemas/decision-object.schema.json
# $defs/subagent_sidecar) + dispatch frontmatter parity with run-report.
# ---------------------------------------------------------------------------

def test_provisioned_doc_contains_decision_object_fields(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-decision-object-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=SIDECAR_ELIGIBLE_TYPE,
        session_id=session_id,
        plan="docs/plans/2026-07-24-canonical-resolution-engine.md",
        chunk="w2-b3",
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code == 0

    envelope = json.loads(out.splitlines()[0])
    doc_path = git_repo / envelope["subagent_sidecar"]
    text = doc_path.read_text(encoding="utf-8")

    frontmatter = text.split("---\n")[1]
    parsed = yaml.safe_load(frontmatter)

    assert parsed["completion_status"] == "pending"
    assert parsed["divergence_from_plan"] == {"diverged": False, "summary": "", "detail": ""}
    assert parsed["plan"] == "docs/plans/2026-07-24-canonical-resolution-engine.md"
    assert parsed["chunk"] == "w2-b3"
    assert parsed["status"] == "dispatched"
    assert parsed["commits"] == []
    assert parsed["sidecar_schema"] == "v1"
    assert "## tell_the_EM" in text


def test_provisioned_doc_omits_plan_chunk_gracefully_when_absent_from_payload(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A spawn payload that is eligible but never threaded plan/chunk through
    still gets a sidecar -- those fields render null, provisioning never
    silently refuses."""
    session_id = "sess-no-plan-chunk"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=SIDECAR_ELIGIBLE_TYPE, session_id=session_id
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code == 0

    envelope = json.loads(out.splitlines()[0])
    doc_path = git_repo / envelope["subagent_sidecar"]
    text = doc_path.read_text(encoding="utf-8")
    frontmatter = text.split("---\n")[1]
    parsed = yaml.safe_load(frontmatter)

    assert parsed["plan"] is None
    assert parsed["chunk"] is None
    assert parsed["completion_status"] == "pending"


def test_build_sidecar_text_divergence_is_object_not_array() -> None:
    text = _build_sidecar_text(
        agent_type=SIDECAR_ELIGIBLE_TYPE,
        spawned_at="2026-07-24T00:00:00Z",
        plan_path=None,
        chunk_id=None,
        dispatched_by=None,
    )
    frontmatter = text.split("---\n")[1]
    parsed = yaml.safe_load(frontmatter)

    assert isinstance(parsed["divergence_from_plan"], dict)
    assert not isinstance(parsed["divergence_from_plan"], list)
    assert parsed["divergence_from_plan"] == {"diverged": False, "summary": "", "detail": ""}
    assert parsed["completion_status"] == "pending"


# ---------------------------------------------------------------------------
# provision_key deterministic + idempotent path mode (mirrors run-report's
# equivalent SUBSUME coverage).
# ---------------------------------------------------------------------------

def test_provision_key_deterministic_path_not_nonce(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=SIDECAR_ELIGIBLE_TYPE,
        session_id=session_id,
        provision_key="myplan.C1",
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    assert envelope == {
        "subagent_sidecar": f"state/subagent-share/{session_id}/myplan.C1.subagent-sidecar.md"
    }
    assert (git_repo / envelope["subagent_sidecar"]).is_file()


def test_provision_key_idempotent_reopen_preserves_content(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-idempotent"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=SIDECAR_ELIGIBLE_TYPE,
        session_id=session_id,
        provision_key="myplan.C2",
    )
    exit_code_1, out_1 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_1 == 0
    path_1 = json.loads(out_1.splitlines()[0])["subagent_sidecar"]
    doc_path = git_repo / path_1
    modified_content = "---\ncompletion_status: modified-by-test\n---\n\nEDITED\n"
    doc_path.write_text(modified_content, encoding="utf-8")

    exit_code_2, out_2 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_2 == 0
    path_2 = json.loads(out_2.splitlines()[0])["subagent_sidecar"]

    assert path_1 == path_2
    assert doc_path.read_text(encoding="utf-8") == modified_content


def test_provision_key_traversal_sanitized_confined_single_segment(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-traversal"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=SIDECAR_ELIGIBLE_TYPE,
        session_id=session_id,
        provision_key="../escape",
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    emitted_path = envelope["subagent_sidecar"]
    assert "/" not in Path(emitted_path).name

    doc_path = git_repo / emitted_path
    assert doc_path.is_file()
    share_root = git_repo / "state" / "subagent-share"
    assert doc_path.resolve().is_relative_to(share_root.resolve())


def test_direct_call_no_argparse_path_still_provisions(
    git_repo: Path, policy_path: Path
) -> None:
    """Mirrors provision_report's direct _provision() call shape -- a caller
    invoking provision_subagent_sidecar() directly (never through main()/
    argparse) still resolves eligibility/session/path correctly."""
    session_id = "sess-direct-call"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=SIDECAR_ELIGIBLE_TYPE, session_id=session_id
    )
    sidecar_path = provision_subagent_sidecar(payload, str(policy_path), str(git_repo))
    assert sidecar_path is not None
    assert (git_repo / sidecar_path).is_file()


# ---------------------------------------------------------------------------
# AC-9 confinement preservation: the shipped code-reviewer Bash-command
# allowlist is a COMMAND allowlist, not a write-target allowlist, and this
# module's existence must not widen it. Assert the guard's constants are
# byte-identical to what it shipped with -- this module never imports,
# references, or mutates that guard.
# ---------------------------------------------------------------------------

def test_confinement_guard_allowlist_constants_unchanged_by_this_module() -> None:
    """Sidecar = write TARGET, never write CAPABILITY (AC-9). Proves this
    package did not widen the shipped code-reviewer Bash-command allowlist:
    the allowed binary is still exactly ``coordinator-doc-new`` and the
    required --type value is still exactly ``review-findings`` -- a
    read-only-confined agent's tool grant is untouched by the existence of
    ``--type subagent-sidecar`` or this provisioner package."""
    from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as guard

    assert guard._ALLOWED_BINARY_SUFFIX == "coordinator-doc-new"
    assert guard._REQUIRED_TYPE_ARG_END == "--type review-findings"
    assert guard._METACHARACTERS == (";", "&&", "||", "|", "`", "$(", ">", "<", "&")

    # This provisioner module imports nothing from the guard package -- a
    # confined agent's Bash grant is not expanded merely because a new
    # sidecar TYPE now exists; the guard's allowlist would need its own
    # independent edit to ever permit --type subagent-sidecar, and no such
    # edit is made here.
    import coordinator_core.dispatch.provision as provision_module

    assert not any(
        name.startswith("block_reviewer") or "bash_guards" in name
        for name in dir(provision_module)
    )
