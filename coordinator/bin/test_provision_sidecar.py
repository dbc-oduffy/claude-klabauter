"""test_provision_sidecar.py — pytest suite for provision-sidecar.py.

Drives the CLI as a real subprocess (mirroring test_fan_out_dispatch.py's
conventions, since dashed filenames aren't importable as modules) against a
real `git init`'d tmp_path repo and a fixture `subagent-sandbox-policy.yaml`
(mirroring coordinator_core/subagent_sandbox/tests/test_provision_report.py's
fixture shape) — never the real example-doctrine-repo policy file or the developer's live
session, per the dispatch brief.

Covers the loudness property this CLI exists to guarantee: an ineligible
agent type (or any other unmet precondition) exits non-zero with a specific
diagnostic on stderr and prints NOTHING to stdout — the opposite of
provision_report's own fail-open contract, which this wrapper deliberately
does not weaken (see provision-sidecar.py's module docstring).

Spec backlink: dispatched via a coordinator:executor chunk, 2026-07-30
(provision-sidecar CLI for Workflow-spawned report_sidecar agents).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from coordinator_core.win_portability import no_console_creationflags

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(SCRIPT_DIR, "provision-sidecar.py")
PYTHON = sys.executable

ELIGIBLE_TYPE = "coordinator:fixture-eligible-reviewer"
INELIGIBLE_TYPE = "coordinator:fixture-ineligible-type"



@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repo rooted at tmp_path (so resolve_git_root behaves
    exactly as it does against a production checkout)."""
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        r = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, **no_console_creationflags())
        if r.returncode != 0:
            raise RuntimeError(f"git setup failed in {tmp_path}: {r.stderr}")
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": [ELIGIBLE_TYPE],
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def run_cli(git_root: Path, policy_path: Path, extra_args=None, extra_env=None):
    argv = [
        PYTHON,
        HELPER,
        "--policy",
        str(policy_path),
        "--cwd",
        str(git_root),
    ]
    if extra_args:
        argv += extra_args
    env = dict(os.environ)
    # Loudness / missing-session-id tests need a clean slate — never inherit
    # the developer's live session id from the environment this suite itself
    # runs under.
    for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(
        argv,
        cwd=git_root,
        capture_output=True,
        text=True,
        env=env,
        **no_console_creationflags(),
    )
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_returns_path_and_writes_file(git_repo: Path, policy_path: Path) -> None:
    rc, out, err = run_cli(
        git_repo,
        policy_path,
        extra_args=["--agent-type", ELIGIBLE_TYPE, "--session-id", "sess-happy-1"],
    )
    assert rc == 0, f"expected success, got rc={rc} stderr={err}"

    lines = out.splitlines()
    assert len(lines) == 1, f"stdout must be exactly one line, got: {out!r}"
    rel_path = lines[0]
    assert rel_path.startswith("state/subagent-share/sess-happy-1/")

    doc_path = git_repo / rel_path
    assert doc_path.is_file(), f"sidecar file must exist on disk at {doc_path}"
    assert doc_path.read_text(encoding="utf-8").startswith("---\n")


# ---------------------------------------------------------------------------
# The loudness property
# ---------------------------------------------------------------------------

def test_ineligible_agent_type_is_loud_not_silent(git_repo: Path, policy_path: Path) -> None:
    rc, out, err = run_cli(
        git_repo,
        policy_path,
        extra_args=["--agent-type", INELIGIBLE_TYPE, "--session-id", "sess-ineligible-1"],
    )
    assert rc != 0, "an ineligible agent type must exit non-zero"
    assert out == "", f"stdout must be empty on failure, got: {out!r}"
    assert INELIGIBLE_TYPE in err, "stderr diagnostic must name the specific ineligible type"
    assert "not" in err.lower() and "eligible" in err.lower()


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_same_provision_key_is_idempotent_not_clobbering(git_repo: Path, policy_path: Path) -> None:
    args = [
        "--agent-type",
        ELIGIBLE_TYPE,
        "--session-id",
        "sess-idempotent-1",
        "--provision-key",
        "plan-slug.chunk-A",
    ]
    rc1, out1, err1 = run_cli(git_repo, policy_path, extra_args=args)
    assert rc1 == 0, f"first run must succeed: {err1}"
    path1 = out1.strip()

    doc_path = git_repo / path1
    marker = "MARKER-FROM-FIRST-RUN\n"
    with doc_path.open("a", encoding="utf-8") as fh:
        fh.write(marker)

    rc2, out2, err2 = run_cli(git_repo, policy_path, extra_args=args)
    assert rc2 == 0, f"second run must succeed: {err2}"
    path2 = out2.strip()

    assert path1 == path2, "same --provision-key must resolve to the same path"
    assert marker in doc_path.read_text(encoding="utf-8"), (
        "second run with the same provision-key must NOT clobber the existing sidecar's content"
    )


# ---------------------------------------------------------------------------
# Missing session id
# ---------------------------------------------------------------------------

def test_missing_session_id_is_loud_not_silent(git_repo: Path, policy_path: Path) -> None:
    rc, out, err = run_cli(
        git_repo,
        policy_path,
        extra_args=["--agent-type", ELIGIBLE_TYPE],
    )
    assert rc != 0, "a missing session id must exit non-zero"
    assert out == "", f"stdout must be empty on failure, got: {out!r}"
    assert "session id" in err.lower()


# ---------------------------------------------------------------------------
# Template-type resolution (report_type_map)
#
# Regression anchor: before this leg, the CLI sent no `type` key at all, so a
# code-reviewer provisioned through it fell through to the frozen legacy
# run-report shape and got `## Observations` — the exact heading
# `ops.append_integrator_dispositions` must refuse. That made the sanctioned
# remedy for the Workflow-agent() hook bypass reproduce the very defect it
# exists to route around.
# ---------------------------------------------------------------------------

REVIEWER_TYPE = "coordinator:code-reviewer"


@pytest.fixture
def typed_policy_path(tmp_path: Path) -> Path:
    """A policy carrying report_type_map, as the real example-doctrine-repo-side file does."""
    policy = {
        "report_sidecar": [ELIGIBLE_TYPE, REVIEWER_TYPE],
        "report_type_map": {
            ELIGIBLE_TYPE: "run-report",
            REVIEWER_TYPE: "review-findings",
        },
    }
    path = tmp_path / "typed-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _provisioned_text(git_repo: Path, stdout: str) -> str:
    return (git_repo / stdout.strip()).read_text(encoding="utf-8")


def test_reviewer_resolves_to_findings_template_without_an_explicit_type(
    git_repo: Path, typed_policy_path: Path
) -> None:
    """The whole point: the caller passes no --type and still gets Findings."""
    rc, out, err = run_cli(
        git_repo,
        typed_policy_path,
        extra_args=["--agent-type", REVIEWER_TYPE, "--session-id", "sess-typed"],
    )
    assert rc == 0, err
    text = _provisioned_text(git_repo, out)
    assert "## Findings" in text
    assert "## Observations" not in text


def test_explicit_type_overrides_the_resolved_one(
    git_repo: Path, typed_policy_path: Path
) -> None:
    rc, out, err = run_cli(
        git_repo,
        typed_policy_path,
        extra_args=[
            "--agent-type", REVIEWER_TYPE,
            "--session-id", "sess-override",
            "--type", "assessment",
        ],
    )
    assert rc == 0, err
    text = _provisioned_text(git_repo, out)
    assert "## Questions" in text
    assert "## Findings" not in text


def test_unknown_explicit_type_is_loud_not_silent(
    git_repo: Path, typed_policy_path: Path
) -> None:
    rc, out, err = run_cli(
        git_repo,
        typed_policy_path,
        extra_args=["--agent-type", REVIEWER_TYPE, "--session-id", "s", "--type", "bogus"],
    )
    assert rc == 2
    assert "unknown --type" in err
    assert "review-findings" in err


def test_lookup_miss_still_provisions_with_the_legacy_shape(
    git_repo: Path, policy_path: Path
) -> None:
    """A policy with NO report_type_map at all must behave exactly as before.

    This is the fail-open arm, and it is deliberately not loud: whether a given
    agent_type has a report_type_map row is a fact about a policy file
    the engine repo does not own, so its absence is not a precondition failure.
    """
    rc, out, err = run_cli(
        git_repo,
        policy_path,
        extra_args=["--agent-type", ELIGIBLE_TYPE, "--session-id", "sess-miss"],
    )
    assert rc == 0, err
    text = _provisioned_text(git_repo, out)
    assert "## Observations" in text
    assert "## Findings" not in text


def test_malformed_report_type_map_does_not_break_provisioning(
    git_repo: Path, tmp_path: Path
) -> None:
    """Loader fail-open, end to end: a non-dict map must not brick a spawn."""
    policy = {"report_sidecar": [ELIGIBLE_TYPE], "report_type_map": ["not", "a", "dict"]}
    path = tmp_path / "malformed-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")

    rc, out, err = run_cli(
        git_repo,
        path,
        extra_args=["--agent-type", ELIGIBLE_TYPE, "--session-id", "sess-malformed"],
    )
    assert rc == 0, err
    assert "## Observations" in _provisioned_text(git_repo, out)
