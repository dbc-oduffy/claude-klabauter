"""
coordinator_core.ops.dispatch_emit.tests.test_cli

Regression coverage for the CLI-leg fixes filed against
``emit-dispatch-workflow.py`` (issue #84 item 2, issue #89 K1/K2):

- #84.2a: the queue route resolves ``--repo-root`` from cwd's own ``.git``
  ancestor when omitted, rather than requiring it -- the documented
  no-``--repo-root`` call works.
- #84.2c: a successful emit prints the ``Workflow({...})`` invocation line
  to stderr on EVERY route.
- #89 K1: ``--out`` must end ``.workflow.mjs``, and must not equal the
  spine an ``--inventory`` emission itself mints.
- #89 K2: ``--preamble FILE`` is read, hashed, and recorded in the receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import cli as cli_module

_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def _queue_fixture(tmp_path: Path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    queue_dir = repo_root / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    for i in range(2):
        (queue_dir / f"row{i}.yaml").write_text(
            yaml.safe_dump({"id": f"row{i}", "title": f"Row {i}"}), encoding="utf-8"
        )
    (repo_root / "state" / "queue-grind").mkdir(parents=True, exist_ok=True)
    return repo_root, queue_dir


def test_queue_route_defaults_repo_root_from_cwd(tmp_path, monkeypatch):
    """#84.2a: the documented no-``--repo-root`` call works when cwd sits
    inside a git worktree."""
    repo_root, queue_dir = _queue_fixture(tmp_path)
    out_path = repo_root / "state" / "queue-grind" / "out.workflow.mjs"
    monkeypatch.chdir(repo_root)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(out_path),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    assert out_path.is_file()


def test_queue_route_without_repo_root_and_no_git_ancestor_still_refuses(tmp_path, monkeypatch):
    """No ``.git`` ancestor from cwd: the pre-existing ``QueueRootMissingError``
    refusal is unchanged -- the default never invents a root."""
    _, queue_dir = _queue_fixture(tmp_path)
    isolated_cwd = tmp_path / "no-git-here"
    isolated_cwd.mkdir()
    out_path = isolated_cwd / "out.workflow.mjs"
    monkeypatch.chdir(isolated_cwd)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(out_path),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_DATA_ERROR


def test_successful_emit_prints_workflow_invocation_queue_route(tmp_path, monkeypatch, capsys):
    """#84.2c: the queue route prints the Workflow(...) call, naming the
    required run_stamp/script_path/profile_dir args."""
    repo_root, queue_dir = _queue_fixture(tmp_path)
    out_path = repo_root / "state" / "queue-grind" / "out.workflow.mjs"
    monkeypatch.chdir(repo_root)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(out_path),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    captured = capsys.readouterr()
    assert "Workflow({" in captured.err
    assert "run_stamp" in captured.err
    assert "script_path" in captured.err
    assert "profile_dir" in captured.err


def test_successful_emit_prints_workflow_invocation_plan_route(tmp_path, monkeypatch, capsys):
    """#84.2c: the plan route also prints the invocation line (it already
    had fire_args to name; the assertion pins that it is never silent)."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    plan_path = repo_root / "docs" / "plans" / "p.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n",
        encoding="utf-8",
    )
    out_path = repo_root / "p.workflow.mjs"
    monkeypatch.chdir(repo_root)
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    captured = capsys.readouterr()
    assert "Workflow({" in captured.err


def test_plan_route_defaults_out_to_plan_basename_workflow_mjs(tmp_path, monkeypatch):
    """DoE parity (emit-dispatch-workflow.py ``--out`` help text): the plan
    route defaults an omitted ``--out`` to ``<plan-basename>.workflow.mjs``
    beside the plan, rather than refusing with "--out is required"."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    plan_path = repo_root / "docs" / "plans" / "p.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo_root)
    argv = ["--plan", str(plan_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    expected_out = plan_path.parent / "p.workflow.mjs"
    assert expected_out.is_file()


def test_queue_only_flags_without_queue_are_refused(tmp_path, monkeypatch):
    """DoE parity (review(slice A), 08fb23d21): --appetite/--where/etc given
    without --queue/--profile is refused rather than silently ignored by the
    plan route."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    plan_path = repo_root / "docs" / "plans" / "p.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo_root)
    out_path = repo_root / "p.workflow.mjs"
    argv = ["--plan", str(plan_path), "--out", str(out_path), "--limit", "5"]
    assert cli_module.main(argv) == cli_module.EXIT_USAGE
    assert not out_path.exists()


def test_out_must_end_workflow_mjs(tmp_path):
    """#89 K1: an --out not ending .workflow.mjs is refused before any write."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    plan_path = repo_root / "docs" / "plans" / "p.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n",
        encoding="utf-8",
    )
    out_path = repo_root / "p.spine.md"
    out_path.write_text("existing committed spine\n", encoding="utf-8")
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_USAGE
    # Refused before any write -- the committed file is untouched.
    assert out_path.read_text(encoding="utf-8") == "existing committed spine\n"


def test_preamble_recorded_in_receipt(tmp_path, monkeypatch):
    """#89 K2: --preamble FILE is read, hashed, and both are recorded in
    the emission receipt."""
    repo_root, queue_dir = _queue_fixture(tmp_path)
    preamble_path = tmp_path / "preamble.md"
    preamble_text = "RUN POSTURE: resume-safe, do not re-plan.\n"
    preamble_path.write_text(preamble_text, encoding="utf-8", newline="")
    out_path = repo_root / "state" / "queue-grind" / "out.workflow.mjs"
    monkeypatch.chdir(repo_root)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(out_path),
        "--preamble", str(preamble_path),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    receipt_path = out_path.with_name(out_path.name + ".emitted.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["preamble_path"] == str(preamble_path)
    assert receipt["preamble_sha256"] == hashlib.sha256(preamble_path.read_bytes()).hexdigest()
    script_text = out_path.read_text(encoding="utf-8")
    assert "RUN POSTURE: resume-safe, do not re-plan." in script_text
    # Emitted once as a shared const, never inlined per row -- see
    # grind_compose.py's own docstring on PREAMBLE.
    assert script_text.count("const PREAMBLE =") == 1


def test_fire_refuses_when_a_peer_overwrites_the_script_before_firing(tmp_path, monkeypatch):
    """coordinator-content-repo parity port, leg 3 (`emit-dispatch-workflow.py ::
    _guard_against_fired_drift`), CLI production caller: `--fire` must
    refuse rather than spawn a child when the on-disk script no longer
    matches what this same call's own emit just wrote -- simulating a peer
    overwriting the deterministic ``--out`` path in the window between this
    process's emit and its own ``--fire`` branch.

    `_dispatch_emit` is monkeypatched to overwrite the file it just wrote
    immediately after returning -- the only way to land a peer write inside
    that window from a single synchronous CLI call -- and `fire_workflow`
    is monkeypatched to fail the test if reached (the guard must refuse
    before any spawn).
    """
    repo_root, queue_dir = _queue_fixture(tmp_path)
    out_path = repo_root / "state" / "queue-grind" / "out.workflow.mjs"
    monkeypatch.chdir(repo_root)

    from coordinator_core.ops.dispatch_emit import op as op_module

    real_dispatch_emit = op_module._dispatch_emit

    def _dispatch_emit_then_peer_overwrites(*args, **kwargs):
        result = real_dispatch_emit(*args, **kwargs)
        Path(result["path"]).write_bytes(b"// a peer's different emission\n")
        return result

    monkeypatch.setattr(cli_module, "_dispatch_emit", _dispatch_emit_then_peer_overwrites)

    def _fire_workflow_must_not_be_called(*_args, **_kwargs):
        raise AssertionError("fire_workflow was called despite the drifted script")

    import coordinator_core.ops.workflow_fire.fire as fire_module

    monkeypatch.setattr(fire_module, "fire_workflow", _fire_workflow_must_not_be_called)

    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(out_path),
        "--fire",
    ]

    with pytest.raises(op_module.FiredDriftError):
        cli_module.main(argv)

    assert out_path.read_bytes() == b"// a peer's different emission\n"
