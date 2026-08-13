"""
coordinator_core.subagent_sandbox.tests.test_provision_report -- decision-matrix
pytest harness for the spawn-time report-sidecar provisioner.

Purpose: drives coordinator_core.subagent_sandbox.provision_report's CLI
surface (main()) with synthetic spawn-payload dicts piped on stdin, a
fixture policy yaml (tmp_path), and a real `git init`'d tmp_path repo (so
resolve_git_root behaves like production) -- mirrors test_engine.py's
git_repo/policy_path fixture conventions and _write_backpointer helper for
the subagent_type back-pointer leg.

Spec backlink: pln-claude-klabauter-subagent-run-report-aut-f51428 (C4)
Module under test: coordinator_core/subagent_sandbox/provision_report.py
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

from coordinator_core.dispatch.provision import _build_sidecar_text
from coordinator_core.frontmatter.schema_validate import parse_yaml
from coordinator_core.subagent_sandbox.provision_report import _build_doc_text
from coordinator_core.subagent_sandbox.provision_report import _build_run_report_doc_text
from coordinator_core.subagent_sandbox.provision_report import _build_review_findings_doc_text
from coordinator_core.subagent_sandbox.provision_report import (
    _build_run_report_legacy_doc_text,
)
from coordinator_core.subagent_sandbox.provision_report import (
    _build_staff_eng_review_doc_text,
)
from coordinator_core.subagent_sandbox.provision_report import _PLAN_DERIVABLE_LENS
from coordinator_core.subagent_sandbox.provision_report import _provision
from coordinator_core.subagent_sandbox.provision_report import main as provision_main

# Real git repo is load-bearing: resolve_git_root() is asserted against a
# real `git init`'d tree per this file's own module docstring so it behaves
# exactly as it does against a production checkout -- mirrors
# test_engine.py's git_repo fixture convention.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REPORT_SIDECAR_TYPE = "coordinator:code-reviewer"
INELIGIBLE_TYPE = "coordinator:executor"

BARE_HEX_AGENT_ID = "abc123def4567890"
NAMED_AGENT_ID = "aReviewBot-0123456789abcdef"

_EMIT_RE = re.compile(
    r'^state/subagent-share/(?P<session>[^/]+)/(?P<label>[^/]+)-(?P<nonce>[0-9a-f]{8})\.md$'
)


def _sanitize_expected(seg: str) -> str:
    """Mirror provision_report._sanitize_segment's whitelist for test
    expectations -- the module strips anything outside [A-Za-z0-9._-]."""
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
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [REPORT_SIDECAR_TYPE],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


@pytest.fixture
def plan_derivable_policy_path(tmp_path: Path) -> Path:
    """A policy fixture eligible for BOTH a plan-derivable emitter
    (docs-checker) and the untouched session-keyed REPORT_SIDECAR_TYPE, so
    tests can prove the two homes coexist without one starving the other."""
    # Driven off _PLAN_DERIVABLE_LENS's own keys (not a hand-copied literal)
    # so a new plan-derivable emitter is automatically eligible here too --
    # this fixture's `report_sidecar` list must list every _PLAN_DERIVABLE_LENS
    # entry or test_all_registered_emitters_resolve_expected_lens's map-driven
    # parametrize (below) fails eligibility for the untested entry instead of
    # silently under-covering it.
    policy = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [REPORT_SIDECAR_TYPE, *_PLAN_DERIVABLE_LENS.keys()],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _write_backpointer(
    git_root: Path,
    agent_id: str,
    em_session_id: str,
    subagent_type: str,
) -> None:
    """Fake .git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt +
    .git/coordinator-sessions/<em_session_id>/dispatched-agents.txt back-pointer
    chain, mirroring engine.resolve_effective_types' on-disk layout (see
    test_engine.py's identical helper)."""
    agents_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "em-session-id.txt").write_text(em_session_id + "\n", encoding="utf-8")

    session_dir = git_root / ".git" / "coordinator-sessions" / em_session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    row = f"{agent_id}\t2026-07-13T00:00:00Z\t{subagent_type}\n"
    if dispatch_file.exists():
        with dispatch_file.open("a", encoding="utf-8") as fh:
            fh.write(row)
    else:
        dispatch_file.write_text(row, encoding="utf-8")


def _payload(
    *,
    agent_id: Optional[str] = None,
    agent_type: Optional[str] = None,
    session_id: Optional[str] = None,
    provision_key: Optional[str] = None,
    doc_type: Optional[str] = None,
    plan_path: Optional[str] = None,
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
    if doc_type is not None:
        payload["type"] = doc_type
    if plan_path is not None:
        payload["plan_path"] = plan_path
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
# Core matrix
# ---------------------------------------------------------------------------

def test_eligible_agent_type_creates_doc_and_emits_json(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-eligible-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id=session_id
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    match = _EMIT_RE.match(envelope["report_sidecar"])
    assert match is not None
    assert match.group("session") == session_id
    assert match.group("label") == _sanitize_expected(REPORT_SIDECAR_TYPE)

    doc_path = git_repo / envelope["report_sidecar"]
    assert doc_path.is_file()
    assert doc_path.parent == git_repo / "state" / "subagent-share" / session_id


def test_eligible_via_subagent_type_backpointer_leg(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    em_session_id = "em-session-backpointer-1"
    _write_backpointer(git_repo, NAMED_AGENT_ID, em_session_id, REPORT_SIDECAR_TYPE)
    payload = _payload(agent_id=NAMED_AGENT_ID, session_id=em_session_id)
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    match = _EMIT_RE.match(envelope["report_sidecar"])
    assert match is not None
    assert match.group("session") == em_session_id
    assert match.group("label") == _sanitize_expected(REPORT_SIDECAR_TYPE)

    doc_path = git_repo / envelope["report_sidecar"]
    assert doc_path.is_file()


def test_ineligible_type_no_doc_empty_stdout_exit_zero(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=INELIGIBLE_TYPE, session_id="sess-ineligible"
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    assert not (git_repo / "state" / "subagent-share").exists()


def test_fail_open_on_absent_policy(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    missing_policy = git_repo / "does-not-exist-policy.yaml"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id="sess-no-policy"
    )
    exit_code, out = _run(payload, missing_policy, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""


def test_fail_open_on_malformed_policy_non_dict(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    bad_policy_path = git_repo / "subagent-sandbox-policy.yaml"
    bad_policy_path.write_text(
        yaml.safe_dump(["confined", "exempt", "sanctioned_dirs"]), encoding="utf-8"
    )
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id="sess-bad-policy"
    )
    exit_code, out = _run(payload, bad_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""


def test_fail_open_on_malformed_policy_bad_yaml(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    bad_policy_path = git_repo / "subagent-sandbox-policy.yaml"
    bad_policy_path.write_text("confined: [unterminated\n  - foo\nbar: {", encoding="utf-8")
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id="sess-bad-yaml"
    )
    exit_code, out = _run(payload, bad_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""


def test_missing_session_id_emits_nothing(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    payload = _payload(agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE)
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    assert not (git_repo / "state" / "subagent-share").exists()


def test_unhashable_agent_type_fails_open(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """agent_type as an unhashable value (e.g. a list) raises TypeError on
    the `agent_type in policy.report_sidecar` membership test in
    _provision -- policy.report_sidecar is set-backed (Policy.__init__:
    `set(report_sidecar or ())`), so an unhashable element cannot even be
    tested for membership. Must fail open (exit 0, empty stdout, no doc)
    via main()'s blanket except, independently of the outer try/except
    moving in a future refactor. Review: the Staff Engineer -- pins the caught-TypeError
    fail-open leg he exercised manually."""
    payload = _payload(agent_id=BARE_HEX_AGENT_ID, session_id="sess-unhashable-agent-type")
    payload["agent_type"] = ["x"]
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    assert not (git_repo / "state" / "subagent-share").exists()


# ---------------------------------------------------------------------------
# Path-sanitization / traversal edges
# ---------------------------------------------------------------------------

def test_label_with_traversal_segments_sanitized_stays_inside_session_dir(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """label containing '../' or '/' must be sanitized down (character
    whitelist drops '/'), and the resulting doc must land ONLY under the
    intended session dir -- never escape it."""
    dangerous_label = "../../evil/type"
    policy_data = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [dangerous_label],
    }
    policy_path = tmp_path / "subagent-sandbox-policy.yaml"
    policy_path.write_text(yaml.safe_dump(policy_data), encoding="utf-8")

    session_id = "sess-traversal-label"
    payload = _payload(agent_id=BARE_HEX_AGENT_ID, agent_type=dangerous_label, session_id=session_id)
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    emitted_path = envelope["report_sidecar"]

    # No path separators survive anywhere in the emitted path's filename --
    # the label component is exactly the whitelist-sanitized label, dots and
    # all (the whitelist preserves '.', only '/' is what could smuggle a
    # directory separator, and that is what this test locks in as gone).
    assert "/" not in Path(emitted_path).name
    match = _EMIT_RE.match(emitted_path)
    assert match is not None
    assert match.group("session") == session_id

    doc_path = git_repo / emitted_path
    assert doc_path.is_file()
    assert doc_path.parent == git_repo / "state" / "subagent-share" / session_id
    # Nothing escaped state/subagent-share/ -- only the one session dir exists.
    share_root = git_repo / "state" / "subagent-share"
    all_files = list(share_root.rglob("*.md"))
    assert all_files == [doc_path]


def test_label_exactly_dotdot_emits_nothing(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """label == '..' exactly survives the character whitelist untouched and
    must be explicitly rejected -- must NOT produce a doc at
    subagent-share/../<session>/....md (i.e. one level ABOVE subagent-share)."""
    policy_data = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [".."],
    }
    policy_path = tmp_path / "subagent-sandbox-policy.yaml"
    policy_path.write_text(yaml.safe_dump(policy_data), encoding="utf-8")

    payload = _payload(agent_id=BARE_HEX_AGENT_ID, agent_type="..", session_id="sess-dotdot-label")
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    assert not (git_repo / "state" / "subagent-share").exists()
    # Nothing was written one level above subagent-share/ either.
    assert not (git_repo / "state" / "sess-dotdot-label").exists()


def test_malicious_session_id_traversal_confined_single_segment_no_escape(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """session_id='../escape' whitelist-sanitizes ('/' is dropped, dots
    survive) down to the single segment '..escape' -- NOT the degenerate
    '..' the module explicitly rejects, so a doc IS provisioned, but the
    key safety property holds: the sanitized session_id is one literal path
    segment with no '/' in it, so the doc can only land directly under
    state/subagent-share/, never escape upward or into a sibling tree."""
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id="../escape"
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    emitted_path = envelope["report_sidecar"]

    match = _EMIT_RE.match(emitted_path)
    assert match is not None
    assert match.group("session") == "..escape"
    assert "/" not in match.group("session")

    share_root = git_repo / "state" / "subagent-share"
    doc_path = git_repo / emitted_path
    assert doc_path.is_file()
    assert doc_path.parent == share_root / "..escape"
    # Confined to a direct child of subagent-share/ -- nothing escaped
    # upward past it (no writes outside share_root's own subtree).
    assert doc_path.resolve().is_relative_to(share_root.resolve())
    assert not (git_repo.parent / "escape").exists()


def test_session_id_sanitizes_to_empty_emits_nothing(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """session_id == '///' whitelist-sanitizes down to '' -- rejected."""
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id="///"
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    assert not (git_repo / "state" / "subagent-share").exists()


def test_label_sanitizes_to_empty_emits_nothing(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """label == '@@@' whitelist-sanitizes down to '' -- rejected."""
    dangerous_label = "@@@"
    policy_data = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [dangerous_label],
    }
    policy_path = tmp_path / "subagent-sandbox-policy.yaml"
    policy_path.write_text(yaml.safe_dump(policy_data), encoding="utf-8")

    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=dangerous_label, session_id="sess-empty-label"
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    assert not (git_repo / "state" / "subagent-share").exists()


# ---------------------------------------------------------------------------
# Nonce uniqueness
# ---------------------------------------------------------------------------

def test_two_provisions_same_type_session_distinct_nonces_both_exist(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-double-provision"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id=session_id
    )

    exit_code_1, out_1 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    exit_code_2, out_2 = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code_1 == 0 and exit_code_2 == 0
    envelope_1 = json.loads(out_1.splitlines()[0])
    envelope_2 = json.loads(out_2.splitlines()[0])
    path_1 = envelope_1["report_sidecar"]
    path_2 = envelope_2["report_sidecar"]

    assert path_1 != path_2
    nonce_1 = _EMIT_RE.match(path_1).group("nonce")
    nonce_2 = _EMIT_RE.match(path_2).group("nonce")
    assert nonce_1 != nonce_2

    assert (git_repo / path_1).is_file()
    assert (git_repo / path_2).is_file()


# ---------------------------------------------------------------------------
# SUBSUME amendment: provision_key deterministic + idempotent path mode
# ---------------------------------------------------------------------------

def test_provision_key_deterministic_path_not_nonce(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        provision_key="myplan.C1",
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope == {"report_sidecar": f"state/subagent-share/{session_id}/myplan.C1.md"}

    doc_path = git_repo / envelope["report_sidecar"]
    assert doc_path.is_file()


def test_provision_key_idempotent_reopen_same_path_preserves_content(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-idempotent"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        provision_key="myplan.C2",
    )

    exit_code_1, out_1 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_1 == 0
    envelope_1 = json.loads(out_1.splitlines()[0])
    path_1 = envelope_1["report_sidecar"]

    doc_path = git_repo / path_1
    assert doc_path.is_file()
    modified_content = "---\nstatus: modified-by-test\n---\n\nEDITED CONTENT\n"
    doc_path.write_text(modified_content, encoding="utf-8")

    exit_code_2, out_2 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_2 == 0
    envelope_2 = json.loads(out_2.splitlines()[0])
    path_2 = envelope_2["report_sidecar"]

    assert path_1 == path_2

    share_root = git_repo / "state" / "subagent-share"
    all_files = list(share_root.rglob("*.md"))
    assert all_files == [doc_path]

    assert doc_path.read_text(encoding="utf-8") == modified_content


def test_provision_key_traversal_sanitized_confined_single_segment(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-traversal"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        provision_key="../escape",
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    emitted_path = envelope["report_sidecar"]

    assert emitted_path == f"state/subagent-share/{session_id}/..escape.md"
    assert "/" not in Path(emitted_path).name

    doc_path = git_repo / emitted_path
    assert doc_path.is_file()
    share_root = git_repo / "state" / "subagent-share"
    assert doc_path.resolve().is_relative_to(share_root.resolve())
    all_files = list(share_root.rglob("*.md"))
    assert all_files == [doc_path]


def test_provision_key_exactly_dotdot_fails_open(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-dotdot"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        provision_key="..",
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    # The session dir itself is mkdir'd unconditionally upstream of the
    # provision_key branch (pre-existing D1 behavior) -- what matters here
    # is that NO doc got written for the rejected provision_key.
    share_root = git_repo / "state" / "subagent-share"
    if share_root.exists():
        assert list(share_root.rglob("*.md")) == []


@pytest.mark.parametrize("bad_provision_key", ["///", "@@@"])
def test_provision_key_sanitizes_to_empty_fails_open(
    git_repo: Path,
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    bad_provision_key: str,
) -> None:
    session_id = "sess-provkey-empty"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        provision_key=bad_provision_key,
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    assert out == ""
    # Same mkdir-happens-before-provision_key-check caveat as the
    # exactly-'..' case above -- assert no doc, not "no directory tree".
    share_root = git_repo / "state" / "subagent-share"
    if share_root.exists():
        assert list(share_root.rglob("*.md")) == []


def test_absent_provision_key_regresses_to_nonce_path(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    session_id = "sess-provkey-absent"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id=session_id
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    lines = out.splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    match = _EMIT_RE.match(envelope["report_sidecar"])
    assert match is not None
    assert match.group("session") == session_id
    assert match.group("label") == _sanitize_expected(REPORT_SIDECAR_TYPE)

    doc_path = git_repo / envelope["report_sidecar"]
    assert doc_path.is_file()


@pytest.mark.parametrize("use_provision_key", [False, True])
def test_provisioned_doc_contains_superset_scaffold_fields(
    git_repo: Path,
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    use_provision_key: bool,
) -> None:
    session_id = f"sess-superset-{use_provision_key}"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        provision_key="myplan.C3" if use_provision_key else None,
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    doc_path = git_repo / envelope["report_sidecar"]
    assert doc_path.is_file()

    text = doc_path.read_text(encoding="utf-8")
    assert "commits: []" in text
    assert "dispatch_feed:  # forward-declared, INERT until pcli-04 emitter" in text
    assert "  gate_kind: none" in text
    assert "## Run notes" in text
    assert "## Observations" in text
    assert "## Exit interview" in text
    assert "What did you have to work out that the brief could have told you?" in text
    assert (
        "What did you grep, read, or probe that turned out to be a dead end "
        "— and what were you actually looking for?"
    ) in text
    assert (
        "Where did your tool access, permissions, or output contract fight you? "
        "What was missing that isn't deliberately withheld from this role — a "
        "guard denial is not a gap."
    ) in text
    assert "Anything you wanted to say and had nowhere to put?" in text


def test_lead_session_id_stamped_and_distinct_from_agent_id(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """SUBSUME: `lead_session_id` frontmatter field carries the REQUESTING
    EM's session id (payload['session_id']) verbatim -- a distinct
    identity from the spawned agent's own `agent_id` (payload['agent_id'],
    resolve_effective_types' first return leg). The two must never
    collapse to the same value in the written doc."""
    session_id = "em-lead-session-42"
    assert session_id != BARE_HEX_AGENT_ID

    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id=session_id
    )
    exit_code, out = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code == 0

    envelope = json.loads(out.splitlines()[0])
    doc_path = git_repo / envelope["report_sidecar"]
    text = doc_path.read_text(encoding="utf-8")

    assert f"lead_session_id: {session_id}" in text
    assert f"lead_session_id: {BARE_HEX_AGENT_ID}" not in text


def test_build_doc_text_divergence_is_object_not_array() -> None:
    text = _build_doc_text(agent_type=REPORT_SIDECAR_TYPE, spawned_at="2026-07-13T00:00:00Z")
    frontmatter = text.split("---\n")[1]
    parsed = yaml.safe_load(frontmatter)

    assert isinstance(parsed["divergence"], dict)
    assert not isinstance(parsed["divergence"], list)
    assert parsed["divergence"] == {"diverged": False}


# ---------------------------------------------------------------------------
# divergence-field flow-style regression (cross-repo/inbox/
# 2026-07-25-coordinator-claude-em-provision-report-divergence-flow-style.md)
# ---------------------------------------------------------------------------

#: Each entry is (label, doc_text_producer, divergence_field_name). The
#: producer takes no args and returns the full frontmatter+body doc text --
#: closing the class (both provisioners that write a divergence-shaped
#: field) rather than just this module's own instance.
_DIVERGENCE_EMITTERS = [
    (
        "provision_report._build_doc_text",
        lambda: _build_doc_text(agent_type=REPORT_SIDECAR_TYPE, spawned_at="2026-07-13T00:00:00Z"),
        "divergence",
    ),
    (
        "dispatch.provision._build_sidecar_text",
        lambda: _build_sidecar_text(
            agent_type=REPORT_SIDECAR_TYPE,
            spawned_at="2026-07-13T00:00:00Z",
            plan_path="docs/plans/example.md",
            chunk_id="C1",
            dispatched_by="lead-session",
        ),
        "divergence_from_plan",
    ),
]


@pytest.mark.parametrize(
    ("label", "doc_text_fn", "field_name"),
    _DIVERGENCE_EMITTERS,
    ids=[e[0] for e in _DIVERGENCE_EMITTERS],
)
def test_divergence_field_parses_as_object_under_restricted_yaml_parser(
    label: str, doc_text_fn, field_name: str
) -> None:
    """Regression net for the flow-style ``divergence: {diverged: false}``
    defect (cross-repo/inbox/2026-07-25-coordinator-claude-em-provision-report-
    divergence-flow-style.md): this repo's restricted YAML parser
    (``coordinator_core.frontmatter.schema_validate.parse_yaml``) does NOT
    support flow-style mappings -- it parses ``{diverged: false}`` as a raw
    string rather than a dict, tripping coordinator-claude's run-report schema's
    object-shaped ``divergence``/``divergence_from_plan`` check
    (``type: object``, ``required: [diverged]``) on every spawn-provisioned
    sidecar. Block style (key on its own line, nested ``diverged:`` indented
    below) is the only shape that round-trips through ``parse_yaml`` as a
    dict -- see that module's docstring negative-spec. Parametrized over
    every emitter that writes a divergence-shaped field (``provision_report``
    and ``dispatch.provision``) so a future emitter reintroducing flow style
    fails here too, closing the class rather than just this one instance.

    NOTE: ``test_build_doc_text_divergence_is_object_not_array`` above uses
    ``yaml.safe_load`` (full-spec PyYAML), which DOES support flow-style
    mappings and would pass even on the pre-fix flow-style emission -- it was
    not exercising the restricted parser that actually gates coordinator-claude's schema
    validation. This test uses ``parse_yaml`` directly instead.
    """
    text = doc_text_fn()
    frontmatter = text.split("---\n")[1]
    parsed = parse_yaml(frontmatter)

    divergence = parsed[field_name]
    assert isinstance(divergence, dict), (
        f"{label}: {field_name!r} parsed as {type(divergence).__name__} "
        f"({divergence!r}), not dict -- flow-style mapping regression"
    )
    assert divergence["diverged"] is False


def test_dispatch_feed_field_is_block_style_and_parses_as_object() -> None:
    """AC7 companion to the divergence-flow-style regression net above:
    ``dispatch_feed`` is a real, block-style YAML object placeholder now
    (no longer the literal ``dispatch_feed: null`` scalar), and must round
    -trip through the SAME restricted parser (``schema_validate.parse_yaml``,
    which does not support flow-style mappings). Assert block style
    explicitly (no ``{`` on the ``dispatch_feed:`` line) rather than merely
    that the field parses -- a flow-style ``dispatch_feed: {...}`` would
    still happen to parse under full-spec ``yaml.safe_load`` but silently
    fail the restricted parser, exactly the defect class recorded at
    cross-repo/archive/2026-07-25-coordinator-claude-em-provision-report-
    divergence-flow-style.md.
    """
    text = _build_doc_text(agent_type=REPORT_SIDECAR_TYPE, spawned_at="2026-07-13T00:00:00Z")

    dispatch_feed_line = next(
        line for line in text.splitlines() if line.startswith("dispatch_feed:")
    )
    assert "{" not in dispatch_feed_line, (
        f"dispatch_feed header line is flow-style, not block-style: {dispatch_feed_line!r}"
    )

    frontmatter = text.split("---\n")[1]
    parsed = parse_yaml(frontmatter)
    dispatch_feed = parsed["dispatch_feed"]
    assert isinstance(dispatch_feed, dict), (
        f"dispatch_feed parsed as {type(dispatch_feed).__name__} ({dispatch_feed!r}), "
        "not dict -- flow-style mapping regression"
    )
    assert dispatch_feed["gate_kind"] == "none"
    assert dispatch_feed["write_files"] == []


def test_dispatch_feed_frontmatter_validates_against_run_report_schema() -> None:
    """AC7's own out-of-band gap: a shape assertion (block-style, parses as
    a dict, gate_kind/write_files present) passes even when the emitted
    object is schema-INVALID -- exactly how a prior revision of this
    function shipped ``label: null``/``agent_type: null``/``model: null``/
    ``effort: null``/``schema_ref: null``/``brief_ref: null``/
    ``est_min: null`` (seven fields none of which admit null in
    run-report.schema.json's declared sub-property types) with every shape
    test above still green. This test runs the actual emitted frontmatter
    through the real validator (``coordinator_core.frontmatter.
    schema_validate``) against the claude-klabauter-owned
    ``coordinator_core/frontmatter/schemas/run-report.schema.json`` and
    asserts it is schema-VALID, not merely shape-plausible.

    Spec backlink: C6 (commit 8571f7f22273) shipped the schema-invalid
    all-null-subfields shape; corrected in C6b after a staff review caught
    it downstream in coordinator/tests/test_flight_recorder_scaffolder.py.
    """
    from coordinator_core.frontmatter.schema_validate import (
        load_schemas,
        match_schema_for_path,
        parse_frontmatter,
        validate_frontmatter_obj,
    )

    schemas_dir = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas"
    text = _build_doc_text(agent_type=REPORT_SIDECAR_TYPE, spawned_at="2026-07-13T00:00:00Z")
    frontmatter = parse_frontmatter(text)["frontmatter"]

    schemas = load_schemas(schemas_dir)
    match = match_schema_for_path("state/subagent-share/probe-session/probe.md", schemas)
    assert match is not None, "no schema matched state/subagent-share/*/*.md"

    result = validate_frontmatter_obj(frontmatter, match["schema"])
    assert result["ok"], (
        f"provision_report._frontmatter's emitted dispatch_feed failed schema "
        f"validation: {result.get('errors')!r}"
    )


# ---------------------------------------------------------------------------
# --type axis + template registry
# ---------------------------------------------------------------------------

#: The ORIGINAL (pre-``--type``) run-report shape, frozen verbatim -- the
#: byte-for-byte back-compat target for a payload with no ``type`` key.
_LEGACY_RUN_REPORT_TEMPLATE = (
    "---\n"
    "status: open\n"
    "agent_type: {agent_type}\n"
    "spawned_at: {spawned_at}\n"
    "lead_session_id: {lead_session_id}\n"
    "divergence:\n"
    "  diverged: false\n"
    "commits: []\n"
    "dispatch_feed:  # forward-declared, INERT until pcli-04 emitter\n"
    "  gate_kind: none\n"
    "  write_files: []\n"
    "---\n\n"
    "## Run notes\n\n"
    "## Observations\n\n"
    "## Exit interview\n\n"
    "- What did you have to work out that the brief could have told you?\n\n"
    "- What did you grep, read, or probe that turned out to be a dead end — and what were you actually looking for?\n\n"
    "- Where did your tool access, permissions, or output contract fight you? What was missing that isn't deliberately withheld from this role — a guard denial is not a gap.\n\n"
    "- Anything you wanted to say and had nowhere to put?\n\n"
)


def test_build_doc_text_no_doc_type_matches_frozen_legacy_shape_byte_for_byte() -> None:
    """``_build_doc_text(agent_type, spawned_at)`` -- called with the OLD
    2-positional-arg shape, exactly as any pre-existing caller (like
    fan-out-dispatch.py's indirect path via _provision) would -- must
    still produce the exact pre-refactor bytes for the BODY (no
    'Divergence from plan' or 'Completion' additions leak into the
    no-type-key path). The frontmatter now carries the SUBSUME
    `lead_session_id` field too -- omitted-arg here renders as the
    literal `null`, since this 2-arg call predates and doesn't
    supply that field."""
    agent_type = REPORT_SIDECAR_TYPE
    spawned_at = "2026-07-13T00:00:00Z"

    text = _build_doc_text(agent_type, spawned_at)

    assert text == _LEGACY_RUN_REPORT_TEMPLATE.format(
        agent_type=agent_type, spawned_at=spawned_at, lead_session_id="null"
    )
    assert "## Divergence from plan" not in text
    assert "## Completion" not in text


def test_provision_direct_call_no_type_key_in_payload_matches_legacy_shape(
    git_repo: Path, policy_path: Path
) -> None:
    """Mirrors fan-out-dispatch.py's real call shape: _provision() invoked
    directly (never through main()/argparse), with a payload that has no
    'type' key at all. The written doc must byte-match the frozen legacy
    run-report template (spawned_at substituted for the actual value).
    Via _provision(), `lead_session_id` IS always available (it's the
    payload's own `session_id`, required for eligibility in the first
    place) -- unlike the direct 2-arg `_build_doc_text` call above, so
    this byte-match substitutes the real session_id, not `null`."""
    session_id = "sess-direct-no-type"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id=session_id
    )
    assert "type" not in payload

    report_sidecar = _provision(payload, str(policy_path), str(git_repo))
    assert report_sidecar is not None

    doc_path = git_repo / report_sidecar
    text = doc_path.read_text(encoding="utf-8")

    # spawned_at is a live UTC timestamp we don't control -- strip it via
    # the same template, substituting the actual emitted value.
    spawned_at_line = next(line for line in text.splitlines() if line.startswith("spawned_at: "))
    spawned_at = spawned_at_line[len("spawned_at: "):]
    assert text == _LEGACY_RUN_REPORT_TEMPLATE.format(
        agent_type=REPORT_SIDECAR_TYPE, spawned_at=spawned_at, lead_session_id=session_id
    )
    assert "## Divergence from plan" not in text
    assert "## Completion" not in text


@pytest.mark.parametrize(
    ("doc_type", "expected_sections"),
    [
        ("run-report", ["## Divergence from plan", "## Completion"]),
        ("review-findings", ["## Execution capability", "## Findings"]),
        ("assessment", ["## Questions"]),
        ("staff-eng-review", ["## Verdict", "## Rationale", "## Execution capability", "## Findings"]),
    ],
)
def test_type_argument_selects_expected_template_shape(
    git_repo: Path,
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    doc_type: str,
    expected_sections: list,
) -> None:
    session_id = f"sess-type-{doc_type}"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID, agent_type=REPORT_SIDECAR_TYPE, session_id=session_id
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    exit_code = provision_main(
        ["--policy", str(policy_path), "--cwd", str(git_repo), "--type", doc_type]
    )
    captured = capsys.readouterr()
    assert exit_code == 0

    envelope = json.loads(captured.out.splitlines()[0])
    doc_path = git_repo / envelope["report_sidecar"]
    text = doc_path.read_text(encoding="utf-8")

    for section in expected_sections:
        assert section in text
    # Every template inherits the universal Exit interview (c50cf8ac).
    assert "## Exit interview" in text
    assert "What did you have to work out that the brief could have told you?" in text
    # No new frontmatter fields -- same seven-field superset scaffold as run-report.
    assert "commits: []" in text
    assert "dispatch_feed:  # forward-declared, INERT until pcli-04 emitter" in text
    assert "  gate_kind: none" in text
    assert "divergence:\n  diverged: false" in text
    assert f"lead_session_id: {session_id}" in text


def test_staff_eng_review_emits_findings_last_so_the_extractor_scopes_correctly() -> None:
    """Section ORDER, not just presence — `## Findings` must come last.

    `append_integrator_dispositions._extract_findings_section` carves from
    `## Findings` to the exit-interview heading and deliberately does not stop
    at an intervening `## ` heading, so Verdict/Rationale emitted after Findings
    would be folded into the findings body — a reviewer writing only a verdict
    would then read as having filled in findings. Presence assertions alone
    cannot catch that; this one can.
    """
    text = _build_staff_eng_review_doc_text(
        "coordinator:staff-eng", "2026-08-10T00:00:00Z", "sess-order"
    )
    assert (
        text.index("## Verdict")
        < text.index("## Rationale")
        < text.index("## Findings")
        < text.index("## Exit interview")
    )


def test_review_findings_emits_execution_capability_before_findings() -> None:
    """`## Execution capability` must precede `## Findings` -- the same
    ordering hazard as staff-eng-review's Verdict/Rationale (C6, extending
    the review-trail-carries-execution-basis convention)."""
    text = _build_review_findings_doc_text(
        "coordinator:code-reviewer", "2026-08-11T00:00:00Z", "sess-order-rf"
    )
    assert text.index("## Execution capability") < text.index("## Findings")
    assert "none — this verdict rests on reading only" in text


def test_staff_eng_review_emits_execution_capability_before_findings() -> None:
    text = _build_staff_eng_review_doc_text(
        "coordinator:staff-eng", "2026-08-11T00:00:00Z", "sess-order-ser"
    )
    assert (
        text.index("## Verdict")
        < text.index("## Rationale")
        < text.index("## Execution capability")
        < text.index("## Findings")
    )
    assert "none — this verdict rests on reading only" in text


def test_run_report_emits_execution_capability_after_observations() -> None:
    text = _build_run_report_doc_text(
        "coordinator:executor", "2026-08-11T00:00:00Z", "sess-order-rr"
    )
    assert (
        text.index("## Observations")
        < text.index("## Execution capability")
        < text.index("## Divergence from plan")
    )
    assert "none — this verdict rests on reading only" in text


def test_run_report_legacy_shape_excludes_execution_capability() -> None:
    """The frozen legacy back-compat shape must NOT gain the new heading."""
    text = _build_run_report_legacy_doc_text(
        "coordinator:executor", "2026-08-11T00:00:00Z", "sess-legacy-untouched"
    )
    assert "## Execution capability" not in text


def test_run_report_templates_are_untouched_by_the_findings_convergence() -> None:
    """Negative-spec guard (inbound memo item 3, adopted): `## Observations` is
    right for a run report. The defect was never that run-report says
    Observations — only that reviewers could reach it by default."""
    for builder in (_build_run_report_doc_text, _build_run_report_legacy_doc_text):
        text = builder("coordinator:executor", "2026-08-10T00:00:00Z", "sess-untouched")
        assert "## Observations" in text
        assert "## Findings" not in text


def test_type_key_already_in_payload_wins_over_cli_default(
    git_repo: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A payload that already stamps its own 'type' field keeps it -- the
    CLI --type default ('run-report') must not clobber it."""
    session_id = "sess-payload-type-wins"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        doc_type="assessment",
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    exit_code = provision_main(["--policy", str(policy_path), "--cwd", str(git_repo)])
    captured = capsys.readouterr()
    assert exit_code == 0

    envelope = json.loads(captured.out.splitlines()[0])
    doc_path = git_repo / envelope["report_sidecar"]
    text = doc_path.read_text(encoding="utf-8")
    assert "## Questions" in text
    assert "## Divergence from plan" not in text


def test_unknown_type_falls_back_to_run_report_template(
    git_repo: Path, policy_path: Path
) -> None:
    """A direct _provision() call (bypassing argparse's choices=) with an
    unrecognized 'type' value must fail open into the run-report template,
    never raise -- consistent with this module's fail-open posture."""
    session_id = "sess-unknown-type"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        doc_type="not-a-real-type",
    )
    report_sidecar = _provision(payload, str(policy_path), str(git_repo))
    assert report_sidecar is not None

    doc_path = git_repo / report_sidecar
    text = doc_path.read_text(encoding="utf-8")
    assert "## Divergence from plan" in text
    assert "## Completion" in text


# ---------------------------------------------------------------------------
# Plan-derivable report_sidecar for the five plan-scoped-durable emitters
# (canonical spec § 2.7)
# ---------------------------------------------------------------------------

def test_plan_derivable_emitter_with_plan_path_routes_to_plan_sidecars(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    session_id = "sess-plan-derivable-1"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type="coordinator:prior-art-checker",
        session_id=session_id,
        plan_path="docs/plans/2026-07-24-some-plan.md",
        doc_type="assessment",
    )
    exit_code, out = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    assert envelope["report_sidecar"] == (
        "state/plan-sidecars/2026-07-24-some-plan.prior-art-check.md"
    )

    doc_path = git_repo / envelope["report_sidecar"]
    assert doc_path.is_file()
    assert doc_path.parent == git_repo / "state" / "plan-sidecars"
    text = doc_path.read_text(encoding="utf-8")
    assert "## Exit interview" in text
    assert "## Questions" in text  # assessment template


@pytest.mark.parametrize(
    ("agent_type", "lens"),
    sorted(_PLAN_DERIVABLE_LENS.items()),
)
def test_all_registered_emitters_resolve_expected_lens(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    agent_type: str,
    lens: str,
) -> None:
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=agent_type,
        session_id="sess-lens-matrix",
        plan_path="my-plan.md",
    )
    exit_code, out = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    assert envelope["report_sidecar"] == f"state/plan-sidecars/my-plan.{lens}.md"


def test_docs_checker_without_plan_path_keeps_session_keyed_home(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """docs-checker's code-review dispatch shape carries no plan_path (D4
    split-brain reconciliation, canonical spec § 2.7) -- it must be
    completely unaffected by the plan-derivable leg."""
    session_id = "sess-docs-checker-code-review"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type="coordinator:docs-checker",
        session_id=session_id,
    )
    exit_code, out = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    match = _EMIT_RE.match(envelope["report_sidecar"])
    assert match is not None
    assert match.group("session") == session_id


def test_reviewer_persona_with_plan_path_still_session_keyed(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A non-plan-derivable subagent_type must ignore plan_path entirely
    even if a caller mistakenly sends one -- only the named emitters
    are keyed off _PLAN_DERIVABLE_LENS."""
    session_id = "sess-persona-with-plan-path"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type=REPORT_SIDECAR_TYPE,
        session_id=session_id,
        plan_path="docs/plans/irrelevant.md",
    )
    exit_code, out = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    match = _EMIT_RE.match(envelope["report_sidecar"])
    assert match is not None
    assert match.group("session") == session_id


def test_plan_derivable_idempotent_reopen_preserves_content(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type="coordinator:plan-coverage-checker",
        session_id="sess-plan-idempotent",
        plan_path="docs/plans/idempotent-plan.md",
    )

    exit_code_1, out_1 = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_1 == 0
    path_1 = json.loads(out_1.splitlines()[0])["report_sidecar"]

    doc_path = git_repo / path_1
    modified_content = "---\nstatus: modified-by-test\n---\n\nEDITED CONTENT\n"
    doc_path.write_text(modified_content, encoding="utf-8")

    exit_code_2, out_2 = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_2 == 0
    path_2 = json.loads(out_2.splitlines()[0])["report_sidecar"]

    assert path_1 == path_2
    assert doc_path.read_text(encoding="utf-8") == modified_content

    plan_sidecars_root = git_repo / "state" / "plan-sidecars"
    assert list(plan_sidecars_root.glob("*.md")) == [doc_path]


def test_plan_path_traversal_stem_confined_single_segment(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type="coordinator:external-pattern-checker",
        session_id="sess-plan-traversal",
        plan_path="../../etc/passwd",
    )
    exit_code, out = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    emitted_path = envelope["report_sidecar"]
    assert emitted_path == "state/plan-sidecars/passwd.external-pattern.md"

    doc_path = git_repo / emitted_path
    plan_sidecars_root = git_repo / "state" / "plan-sidecars"
    assert doc_path.resolve().is_relative_to(plan_sidecars_root.resolve())


def test_plan_path_stem_sanitizes_to_empty_falls_back_to_session_keyed(
    git_repo: Path,
    plan_derivable_policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A plan_path whose stem sanitizes to a rejected segment (here,
    literally '..') fails open to the ordinary session-keyed path rather
    than dropping the sidecar."""
    session_id = "sess-plan-path-empty-stem"
    payload = _payload(
        agent_id=BARE_HEX_AGENT_ID,
        agent_type="coordinator:docs-checker",
        session_id=session_id,
        plan_path="..",
    )
    exit_code, out = _run(payload, plan_derivable_policy_path, git_repo, monkeypatch, capsys)

    assert exit_code == 0
    envelope = json.loads(out.splitlines()[0])
    match = _EMIT_RE.match(envelope["report_sidecar"])
    assert match is not None
    assert match.group("session") == session_id
