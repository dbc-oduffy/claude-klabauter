"""Unit coverage for `plan_route.main`: the plain `--plan` emit.

Every fixture repo is a real `git init` under `tmp_path`, so the in-process write-set read and the
git-status fallback both run for real. The byte contract against the door lives in
`benchmarks/tests/test_emit_plan_route_byte_parity.py`; this file carries what those goldens cannot
see (box terms, cloud venue, resumed plans, evidence sidecar, a wide plan, a dirty write set).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import dirty_write_set, plan_route
from coordinator_core.ops.dispatch_emit.sibling_split import WidthOverCapError
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = (
    "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
    '- id: C1\n  title: Do a thing\n  writes: ["a.txt"]\n  body: |\n'
    "    Write a.txt.\n```\n"
)


@pytest.fixture(autouse=True)
def _write_set_reads_clean():
    """Overrides conftest's stub: these tests run the real write-set guard."""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, **no_console_creationflags()
    )


def _repo(tmp_path: Path, plan_text: str = _PLAN) -> "tuple[Path, Path]":
    repo = tmp_path / "repo"
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(plan_text, encoding="utf-8", newline="\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo, plan


def _run(repo: Path, plan: Path, *extra: str, out: "str | None" = None) -> int:
    out_path = out or str(repo / "out" / "p.workflow.mjs")
    return plan_route.main(["--plan", str(plan), "--out", out_path, "--repo-root", str(repo), *extra])


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_ADMISSION_DISABLE", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-4333-8444-555555555555")
    from coordinator_core import machine_resolver

    real = machine_resolver.registry_get
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: None if key == plan_route.BOX_TERMS_KEY else real(key)
    )


def test_emit_writes_script_and_receipt_and_prints_the_invocation(tmp_path, capsys):
    repo, plan = _repo(tmp_path)
    assert _run(repo, plan) == plan_route.EXIT_OK
    out = repo / "out" / "p.workflow.mjs"
    data = out.read_bytes()
    assert data and b"\r\n" not in data
    receipt = json.loads(out.with_name(out.name + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["plan"] == "p.md"
    assert receipt["session_id"] == "11111111-2222-4333-8444-555555555555"
    captured = capsys.readouterr()
    reply = json.loads(captured.out)
    assert reply["ok"] is True and reply["sha256"] == receipt["sha256"]
    assert reply["fire_args"] == {"repoRoot": repo.resolve().as_posix()}
    assert reply["admission"]["verdict"] == "disabled"
    assert "Workflow({" in captured.err


def test_out_defaults_beside_the_plan(tmp_path):
    repo, plan = _repo(tmp_path)
    assert plan_route.main(["--plan", str(plan), "--repo-root", str(repo)]) == plan_route.EXIT_OK
    assert (plan.parent / "p.workflow.mjs").is_file()


def test_inline_value_form_is_accepted(tmp_path):
    repo, plan = _repo(tmp_path)
    out = repo / "out" / "p.workflow.mjs"
    assert plan_route.main([f"--plan={plan}", f"--out={out}", f"--repo-root={repo}"]) == plan_route.EXIT_OK
    assert out.is_file()


@pytest.mark.parametrize(
    "argv",
    [
        ["--out", "x.workflow.mjs"],
        ["--plan"],
        ["--plan", "p.md", "--fire"],
        ["--plan", "p.md", "--force=1"],
        ["--plan", "p.md", "--hold", "C1"],
    ],
    ids=["no-plan", "plan-without-value", "unknown-flag", "force-with-value", "other-route-flag"],
)
def test_usage_refusals_exit_2_before_any_write(argv, capsys):
    assert plan_route.main(argv) == plan_route.EXIT_USAGE
    assert "ERROR" in capsys.readouterr().err


def test_out_must_end_workflow_mjs(tmp_path):
    repo, plan = _repo(tmp_path)
    victim = repo / "p.spine.md"
    victim.write_text("committed\n", encoding="utf-8")
    assert _run(repo, plan, out=str(victim)) == plan_route.EXIT_USAGE
    assert victim.read_text(encoding="utf-8") == "committed\n"


def test_out_outside_repo_root_is_a_data_error(tmp_path, capsys):
    repo, plan = _repo(tmp_path)
    assert _run(repo, plan, out=str(tmp_path / "elsewhere.workflow.mjs")) == plan_route.EXIT_DATA_ERROR
    assert "escapes target_root" in capsys.readouterr().err
    assert not (tmp_path / "elsewhere.workflow.mjs").exists()


def test_clean_write_set_is_answered_without_the_git_status_guard(tmp_path, monkeypatch):
    repo, plan = _repo(tmp_path)

    def refuse(*a, **k):
        raise AssertionError("the git-status guard ran on a provably clean write set")

    monkeypatch.setattr(dirty_write_set, "guard_against_dirty_write_set", refuse)
    assert plan_route._write_set_provably_clean(plan, repo) is True
    assert _run(repo, plan) == plan_route.EXIT_OK


def test_untracked_write_path_refuses_through_the_guard(tmp_path, capsys):
    repo, plan = _repo(tmp_path)
    (repo / "a.txt").write_text("peer work\n", encoding="utf-8")
    assert plan_route._write_set_provably_clean(plan, repo) is False
    assert _run(repo, plan) == plan_route.EXIT_DATA_ERROR
    err = capsys.readouterr().err
    assert "already modified, staged, or untracked" in err and "a.txt" in err
    assert not (repo / "out" / "p.workflow.mjs").exists()


def test_staged_change_to_a_tracked_write_path_refuses(tmp_path, capsys):
    repo, plan = _repo(tmp_path)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "track a")
    (repo / "a.txt").write_text("v2 staged\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    assert plan_route._write_set_provably_clean(plan, repo) is False
    assert _run(repo, plan) == plan_route.EXIT_DATA_ERROR
    assert "a.txt" in capsys.readouterr().err


def test_committed_clean_write_path_reads_clean_in_process(tmp_path):
    repo, plan = _repo(tmp_path)
    (repo / "a.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "track a")
    assert plan_route._write_set_provably_clean(plan, repo) is True


def test_a_prefix_write_set_falls_back_to_the_guard(tmp_path):
    text = _PLAN.replace('writes: ["a.txt"]', 'writes_under: ["gen/"]')
    repo, plan = _repo(tmp_path, text)
    assert plan_route._write_set_provably_clean(plan, repo) is False


def test_changed_approved_body_is_refused(tmp_path, capsys):
    text = _PLAN.replace("---\n---", "---\nstatus: approved\napproved_body_sha: 0123456789abcdef\n---", 1)
    repo, plan = _repo(tmp_path, text)
    assert _run(repo, plan) == plan_route.EXIT_DATA_ERROR
    assert "plan body changed since approval" in capsys.readouterr().err
    assert not (repo / "out" / "p.workflow.mjs").exists()


def test_unverifiable_approved_body_warns_and_emits(tmp_path, capsys):
    text = _PLAN.replace("---\n---", "---\nstatus: approved\n---", 1)
    repo, plan = _repo(tmp_path, text)
    assert _run(repo, plan) == plan_route.EXIT_OK
    assert "approved_body_sha absent" in capsys.readouterr().err


def test_foreign_emission_is_refused_unless_forced_or_the_same_session(tmp_path, capsys):
    repo, plan = _repo(tmp_path)
    out = repo / "out" / "p.workflow.mjs"
    out.parent.mkdir()
    out.write_bytes(b"another session's script\n")
    receipt = out.with_name(out.name + ".emitted.json")
    receipt.write_text(json.dumps({"session_id": "someone-else"}), encoding="utf-8")

    assert _run(repo, plan) == plan_route.EXIT_DATA_ERROR
    assert "already holds a DIFFERENT emission" in capsys.readouterr().err
    assert out.read_bytes() == b"another session's script\n"

    receipt.write_text(json.dumps({"session_id": "11111111-2222-4333-8444-555555555555"}), encoding="utf-8")
    assert _run(repo, plan) == plan_route.EXIT_OK

    out.write_bytes(b"another session's script\n")
    receipt.write_text(json.dumps({"session_id": "someone-else"}), encoding="utf-8")
    assert _run(repo, plan, "--force") == plan_route.EXIT_OK
    assert out.read_bytes() != b"another session's script\n"


def test_box_terms_bind_the_script_and_the_receipt(tmp_path, monkeypatch):
    repo, plan = _repo(tmp_path)
    terms = tmp_path / "terms.txt"
    terms.write_text("never touch the GPU\n\nsay hello first\n", encoding="utf-8")
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(terms))
    assert _run(repo, plan) == plan_route.EXIT_OK
    out = repo / "out" / "p.workflow.mjs"
    script = out.read_text(encoding="utf-8")
    assert "never touch the GPU" in script and "say hello first" in script
    receipt = json.loads(out.with_name(out.name + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["box_terms"] == ["never touch the GPU", "say hello first"]
    assert receipt["box_terms_path"] == str(terms)


@pytest.mark.parametrize("content", [None, "\n  \n"], ids=["unreadable", "no-term"])
def test_unusable_box_terms_are_a_usage_error(tmp_path, monkeypatch, content):
    repo, plan = _repo(tmp_path)
    terms = tmp_path / "terms.txt"
    if content is not None:
        terms.write_text(content, encoding="utf-8")
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(terms))
    assert _run(repo, plan) == plan_route.EXIT_USAGE
    assert not (repo / "out" / "p.workflow.mjs").exists()


def test_cloud_venue_and_credit_reach_the_composer(tmp_path, monkeypatch):
    repo, plan = _repo(tmp_path)
    from coordinator_core.ops.dispatch_emit import cross_repo_write_refusal

    monkeypatch.setattr(cross_repo_write_refusal, "is_remote_venue", lambda env=None: True)
    seen: dict = {}
    real = plan_route.emit_script

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(plan_route, "emit_script", spy)
    assert _run(repo, plan) == plan_route.EXIT_OK
    assert seen["cross_repo_approved"] is True
    assert seen["credit_rows"] is plan_route.rows_backed_before_base
    assert seen["auto_split"] is True


def test_recorded_evidence_sidecar_is_named_in_the_script(tmp_path):
    repo, plan = _repo(tmp_path)
    sidecar = plan.with_name("p.evidence.yaml")
    sidecar.write_text(
        yaml.safe_dump({"rows": {"C1": [{"recorded_at": "2026-10-14T00:00:00", "text": "ran it"}]}}),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evidence")
    assert _run(repo, plan) == plan_route.EXIT_OK
    assert "p.evidence.yaml" in (repo / "out" / "p.workflow.mjs").read_text(encoding="utf-8")


def test_a_plan_too_wide_for_one_workflow_is_handed_to_the_full_cli(tmp_path, monkeypatch):
    repo, plan = _repo(tmp_path)

    class _Wide(WidthOverCapError):
        def __init__(self):
            pass

    def wide(*a, **k):
        raise _Wide()

    handed: list = []
    from coordinator_core.ops.dispatch_emit import cli

    monkeypatch.setattr(plan_route, "emit_script", wide)
    monkeypatch.setattr(cli, "main", lambda argv=None: handed.append(argv) or 7)
    argv = ["--plan", str(plan), "--out", str(repo / "out" / "p.workflow.mjs"), "--repo-root", str(repo)]
    assert plan_route.main(argv) == 7
    assert handed == [argv]
    assert not (repo / "out" / "p.workflow.mjs").exists()


def test_error_finding_in_the_script_exits_1_after_writing(tmp_path, monkeypatch, capsys):
    repo, plan = _repo(tmp_path)
    from coordinator_core.ops._workflow_contract import Finding, Severity

    monkeypatch.setattr(
        plan_route, "run_checks", lambda script: [Finding(Severity.ERROR, "synthetic", "forced error", None)]
    )
    assert _run(repo, plan) == plan_route.EXIT_DATA_ERROR
    assert json.loads(capsys.readouterr().out)["error_count"] == 1
    assert (repo / "out" / "p.workflow.mjs").is_file()


def test_clear_gate_after_approval_still_emits(tmp_path):
    import asyncio

    from coordinator_core.frontmatter.primitives import stamp_approved_body_sha
    from coordinator_core.ops.plan_tasks_mutate import _handler

    text = (
        "---\nstatus: approved\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "# comment\n"
        "- id: C1\n  title: 'Do a thing'\n  change_kind: script-edit\n  surface: a.txt\n  queue_scope: project\n  writes: [\"a.txt\"]\n"
        "  external_gate:\n    - owner_repo: coordinator-content-repo\n      condition: \"flip\"\n"
        "      requires: commit-in-owner-repo\n      blocks: execution\n"
        "  body: |\n    Write a.txt.\n```\n"
    )
    repo, plan = _repo(tmp_path, stamp_approved_body_sha(text))
    params = {"verb": "clear-gate", "plan_path": str(plan), "id": "C1",
              "owner_repo": "coordinator-content-repo", "evidence": "DoE commit abc1234 landed"}
    result = asyncio.run(_handler(params, repo_root=repo / ".git"))
    assert result["exit_code"] == 0 and result["applied"] is True, result
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "clear")
    assert _run(repo, plan) == plan_route.EXIT_OK
