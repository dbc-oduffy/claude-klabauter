"""A run that halts, lands, is re-composed or ends unrecorded leaves its record dir clean (tracked) or committed nowhere (ignored)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_resume_record, halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as landed_op
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workflow_watch import _write_unrecorded_end_halt

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261113T000000-aaaaaa"
SCRIPT = "run.mjs"
HALT = {"halted": "refusal", "reason": "sizing refused"}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(root), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _make_repo(root: Path, *, ignored: bool) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    exclude = ".coordinator-local/\n" + ("state/warp/\n" if ignored else "")
    (root / ".git" / "info" / "exclude").write_text(exclude, encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / SCRIPT).write_text("// script\n", encoding="utf-8")
    _git(root, "add", "--", "seed.txt")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture(params=["tracked", "ignored"])
def shape(request, tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    return request.param, _make_repo(tmp_path / "repo", ignored=request.param == "ignored")


def _record(root: Path) -> Path:
    rec = root / RECORD_DIR_ROOT / RUN
    (rec / "prompts" / "sub").mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": None}) + "\n", encoding="utf-8")
    (rec / "prompts" / "p1.md").write_text("p1\n", encoding="utf-8")
    (rec / "prompts" / "sub" / "p2.md").write_text("p2\n", encoding="utf-8")
    return rec


def _halt(root: Path) -> dict:
    return terminal_commit._handler(
        {"script_path": SCRIPT, "run_id": RUN, HALTED_RECORD_PARAM: dict(HALT)}, repo_root=root / ".git"
    )


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain", "--untracked-files=all", "--", f"{RECORD_DIR_ROOT}/{RUN}")


def _tracked_record_paths(root: Path) -> list[str]:
    return [p for p in _git(root, "ls-files", "--", f"{RECORD_DIR_ROOT}/{RUN}").splitlines() if p]


def _landed_run(root: Path) -> tuple[Path, dict]:
    """Halt the run through the halt commit, dirty its record dir, then land it through the landed op."""
    rec = _record(root)
    seed = _git(root, "rev-parse", "HEAD")
    assert _halt(root).get("committed") is True
    (rec / "resume.json").write_text('{"before": 1}\n', encoding="utf-8")
    (rec / "prompts" / "late.md").write_text("late\n", encoding="utf-8")
    out = landed_op._handler({"run_id": RUN, "sha": seed}, root)
    assert out["ok"] and out["written"] is True, out
    return rec, out


def _recompose(rec: Path, root: Path) -> None:
    halt_resume_record.write(rec, {"after": 2})
    halt_resume_record.write_refire_record(rec, root / SCRIPT, "stored", None, 0.0)


def test_halt_commit_writes_the_marker_and_names_ignored_prompts(shape):
    name, root = shape
    rec = _record(root)

    out = _halt(root)

    assert out.get("committed") is True, out
    assert (rec / halted_run.HALTED_FILE).is_file()
    if name == "ignored":
        assert f"{RECORD_DIR_ROOT}/{RUN}/prompts/p1.md" in out["run_artifacts"]["ignored_excluded"]
        assert _tracked_record_paths(root) == []


def test_halt_commit_leaves_a_tracked_record_dir_clean(tmp_path, monkeypatch):
    root = _make_repo(tmp_path / "repo", ignored=False)
    _record(root)

    assert _halt(root).get("committed") is True

    assert _porcelain(root) == ""


def test_landed_record_commit_takes_prompts_and_resume_json_when_tracked(tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    root = _make_repo(tmp_path / "repo", ignored=False)

    rec, out = _landed_run(root)

    marker = halted_run._read(rec)
    assert marker is not None and marker["status"] == halted_run.STATUS_LANDED
    assert "records_commit" in out and "records_commit_error" not in out
    assert _porcelain(root) == ""
    rel = f"{RECORD_DIR_ROOT}/{RUN}"
    assert {f"{rel}/prompts/late.md", f"{rel}/prompts/p1.md", f"{rel}/resume.json"} <= set(_tracked_record_paths(root))


def test_landed_record_commit_commits_no_ignored_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    root = _make_repo(tmp_path / "repo", ignored=True)

    rec, _ = _landed_run(root)

    assert (rec / halted_run.HALTED_FILE).is_file()
    rel = f"{RECORD_DIR_ROOT}/{RUN}"
    assert set(_tracked_record_paths(root)) <= {f"{rel}/halted.json", f"{rel}/run-stage.json"}


def test_recompose_after_landing_leaves_resume_json_unchanged(shape):
    _, root = shape
    rec, _ = _landed_run(root)
    before = (rec / "resume.json").read_bytes()
    mtime = (rec / "resume.json").stat().st_mtime_ns

    _recompose(rec, root)

    assert (rec / "resume.json").read_bytes() == before
    assert (rec / "resume.json").stat().st_mtime_ns == mtime


def test_recompose_after_landing_leaves_a_tracked_record_dir_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)
    root = _make_repo(tmp_path / "repo", ignored=False)
    rec, _ = _landed_run(root)

    _recompose(rec, root)

    assert _porcelain(root) == ""


def test_terminal_branch_with_no_halt_record_leaves_a_halt_record(shape):
    name, root = shape
    run_dir = root / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "submission.json").write_text(
        json.dumps({"run_id": RUN, "script_path": SCRIPT, "sizing_rel": None, "submitted_at": 1.0}),
        encoding="utf-8",
    )
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    assert not (rec / halted_run.HALTED_FILE).exists()

    _write_unrecorded_end_halt(str(root / "no-journal.jsonl"), "failed", SCRIPT, root=root)

    marker = halted_run._read(rec)
    assert marker is not None and marker["halted"] == "run-ended-unrecorded"
    assert (rec / halted_run.HALTED_FILE).is_file()


def test_successful_run_end_writes_no_run_ended_unrecorded_marker(shape):
    _, root = shape
    run_dir = root / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "submission.json").write_text(
        json.dumps({"run_id": RUN, "script_path": SCRIPT, "sizing_rel": None, "submitted_at": 1.0}), encoding="utf-8"
    )
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")

    _write_unrecorded_end_halt(str(root / "no-journal.jsonl"), "completed", SCRIPT, root=root)

    assert not (rec / halted_run.HALTED_FILE).exists()


def test_terminal_branch_for_a_landed_run_writes_no_halt_record(shape):
    _, root = shape
    run_dir = root / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "submission.json").write_text(
        json.dumps({"run_id": RUN, "script_path": SCRIPT, "sizing_rel": None, "submitted_at": 1.0}), encoding="utf-8"
    )
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "run-stage.json").write_text(json.dumps({"stage": "landed"}), encoding="utf-8")

    _write_unrecorded_end_halt(str(root / "no-journal.jsonl"), "failed", SCRIPT, root=root)

    assert not (rec / halted_run.HALTED_FILE).exists()
