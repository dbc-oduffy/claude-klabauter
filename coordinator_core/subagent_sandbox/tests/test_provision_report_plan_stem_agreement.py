"""
coordinator_core.subagent_sandbox.tests.test_provision_report_plan_stem_agreement
-- regression harness for the plan-sidecar stem-agreement guard.

Backlink: state/bug-backlog/2026-08-07-lens-sidecar-provisioning-clobbers-a-
peer-plans-sidecar.yaml. A lens dispatch resolved a STALE plan stem, and the
sidecar it provisioned was a different plan's artifact: the file's ``plan:``
frontmatter said one plan while its filename said another. These tests pin the
write-time half of that fix -- the provisioner refuses to write, or to hand
back, a plan-derived sidecar whose declared plan identity disagrees with the
filename stem it sits under, falling open to the session-keyed home instead of
dropping the sidecar.

Module under test: coordinator_core/subagent_sandbox/provision_report.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_passthrough_kwargs
from coordinator_core.subagent_sandbox.provision_report import (
    _PLAN_DERIVABLE_LENS,
    _declared_plan_disagrees_with_stem,
    _plan_frontmatter_value,
)
from coordinator_core.subagent_sandbox.provision_report import main as provision_main

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

AGENT_ID = "abc123def4567890"
PLAN_LENS_TYPE = "coordinator:plan-coverage-checker"
PLAN_LENS_SUFFIX = _PLAN_DERIVABLE_LENS[PLAN_LENS_TYPE]


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, empty git repo rooted at tmp_path (so resolve_git_root behaves
    exactly as it does against a production checkout) -- same convention as
    test_provision_report.py's own fixture."""
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = {
        "confined": [],
        "exempt": [],
        "sanctioned_dirs": [],
        "report_sidecar": list(_PLAN_DERIVABLE_LENS.keys()),
    }
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


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


def _payload(*, session_id: str, plan_path: str, agent_type: str = PLAN_LENS_TYPE) -> dict:
    return {
        "agent_id": AGENT_ID,
        "agent_type": agent_type,
        "session_id": session_id,
        "plan_path": plan_path,
    }


def test_peer_sidecar_with_foreign_plan_frontmatter_is_not_handed_back(
    git_repo: Path,
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The 2026-08-07 shape, verbatim: a sidecar already on disk under one
    plan's stem whose ``plan:`` frontmatter names ANOTHER plan. Provisioning
    for the stem must refuse it rather than hand a peer plan's artifact to a
    second emitter to append to -- the compounding write the incident's
    hand-repair had to undo."""
    stem = "2026-07-31-exec-cli-posix-leg-convergence"
    clobbered = git_repo / "state" / "plan-sidecars" / f"{stem}.{PLAN_LENS_SUFFIX}.md"
    clobbered.parent.mkdir(parents=True, exist_ok=True)
    clobbered.write_text(
        "---\n"
        "status: open\n"
        "plan: docs/plans/2026-08-07-argv-fidelity-at-the-windows-launcher-seam.md\n"
        "---\n\n"
        "## Findings\n\nfindings computed against the OTHER plan\n",
        encoding="utf-8",
    )
    before = clobbered.read_text(encoding="utf-8")

    exit_code, out = _run(
        _payload(session_id="sess-foreign-plan", plan_path=f"docs/plans/{stem}.md"),
        policy_path,
        git_repo,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    emitted = json.loads(out.splitlines()[0])["report_sidecar"]
    assert emitted.startswith("state/subagent-share/")
    assert not emitted.startswith("state/plan-sidecars/")
    assert clobbered.read_text(encoding="utf-8") == before


def test_agreeing_sidecar_is_still_reused_idempotently(
    git_repo: Path,
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Negative half of the guard: an existing sidecar whose ``plan:`` AGREES
    with its stem is the ordinary idempotent hit and must keep resolving to
    the plan-derivable home."""
    stem = "2026-08-21-example-plan"
    plan_path = f"docs/plans/{stem}.md"
    payload = _payload(session_id="sess-agreeing", plan_path=plan_path)

    exit_code_1, out_1 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_1 == 0
    first = json.loads(out_1.splitlines()[0])["report_sidecar"]
    assert first == f"state/plan-sidecars/{stem}.{PLAN_LENS_SUFFIX}.md"
    assert _plan_frontmatter_value((git_repo / first).read_text(encoding="utf-8")) == plan_path

    exit_code_2, out_2 = _run(payload, policy_path, git_repo, monkeypatch, capsys)
    assert exit_code_2 == 0
    assert json.loads(out_2.splitlines()[0])["report_sidecar"] == first


def test_plan_path_rewritten_by_sanitization_falls_open(
    git_repo: Path,
    policy_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A plan filename the segment whitelist REWRITES rather than rejects
    would land under a stem naming a different plan than the payload asked
    for. That is the same disagreement seen from the write side, so it takes
    the same fail-open to the session-keyed home."""
    exit_code, out = _run(
        _payload(session_id="sess-rewritten", plan_path="docs/plans/2026-08-07 argv fidelity.md"),
        policy_path,
        git_repo,
        monkeypatch,
        capsys,
    )

    assert exit_code == 0
    emitted = json.loads(out.splitlines()[0])["report_sidecar"]
    assert emitted.startswith("state/subagent-share/")
    assert not (git_repo / "state" / "plan-sidecars").exists()


def test_frontmatter_reader_ignores_body_plan_lines() -> None:
    """A ``plan:`` line in a filled-in sidecar's BODY is not an identity
    claim, and reading it as one would refuse healthy sidecars."""
    doc = (
        "---\n"
        "status: open\n"
        "divergence:\n"
        "  diverged: false\n"
        "plan: docs/plans/real-plan.md\n"
        "---\n\n"
        "## Findings\n\nplan: docs/plans/quoted-in-a-finding.md\n"
    )
    assert _plan_frontmatter_value(doc) == "docs/plans/real-plan.md"
    assert _plan_frontmatter_value("no frontmatter here\n") is None


@pytest.mark.parametrize(
    ("declared", "stem", "expected"),
    [
        ("docs/plans/a-plan.md", "a-plan", False),
        ("docs/plans/a-plan.md", "b-plan", True),
        ("a-plan.md", "a-plan", False),
        (None, "a-plan", False),
        ("", "a-plan", False),
    ],
)
def test_declared_plan_disagreement_predicate(
    declared: str | None, stem: str, expected: bool
) -> None:
    assert _declared_plan_disagrees_with_stem(declared, stem) is expected


def test_case_difference_follows_the_filesystem_not_the_bytes() -> None:
    """Two stems differing only in case agree iff the platform's paths do.

    Reachable through the reuse call site, which compares a `plan:` line an
    earlier dispatch wrote against a stem this one derived -- two independent
    origins for one plan. On NTFS `A-Plan.md` and `a-plan` are the same file,
    so refusing the reuse is spurious; on ext4 they are two plans, so agreeing
    would blind the guard to a real clobber. `os.path.normcase` is the axis
    that tracks whichever this is, so the expectation is derived from it here
    rather than hardcoded to one platform.
    """
    same_file_here = os.path.normcase("A-Plan") == os.path.normcase("a-plan")

    assert _declared_plan_disagrees_with_stem("docs/plans/A-Plan.md", "a-plan") is (
        not same_file_here
    )
    assert _declared_plan_disagrees_with_stem("docs/plans/a-plan.md", "A-Plan") is (
        not same_file_here
    )


def test_case_folding_does_not_swallow_a_real_disagreement() -> None:
    """Whatever the platform, two genuinely different plans still disagree."""
    assert _declared_plan_disagrees_with_stem("docs/plans/A-Plan.md", "b-plan") is True
    assert _declared_plan_disagrees_with_stem("docs/plans/a-plan.md", "B-PLAN") is True
