"""`grind-row settle-run`: lands an ended run's finished rows through the grind terminal commit only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows, grind_settle_run
from coordinator_core.backlog_grind_assemble.tests.test_grind_rows import _write_profile
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261015T000000Z"
PROFILE = "bug"
ARCHIVE = "archive/bug-backlog/2026-10"
EVIDENCE = f"state/queue-grind/{PROFILE}/evidence/{RUN}"
QUEUE = "state/bug-backlog"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for name in ("a.py", "b.py", "other.py"):
        _write(root, name, "x = 1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    _write(root, "a.py", "x = 2\n")
    _write(root, "b.py", "x = 2\n")
    _write(root, "other.py", "x = 2\n")
    _write_profile(tmp_path / "profiles", archive_path="archive/bug-backlog", schema_rel="schema/bug.schema.json")
    return root


def _finish_row(root: Path, row_id: str) -> None:
    """An ended row: archived copy and done evidence on disk, source row gone (never tracked)."""
    _write(root, f"{ARCHIVE}/{row_id}.yaml", f"id: {row_id}\n")
    _write(root, f"{EVIDENCE}/{row_id}.done.md", "done\n")
    _write(root, f"state/queue-grind/{PROFILE}/{row_id}.jsonl", "{}\n")


def _triage(root: Path, declared: dict) -> Path:
    run_dir = root / "scratch" / "warp" / RUN
    rows = [{"row": rid, "declared_files": files} for rid, files in declared.items()]
    _write(run_dir, "records/b0.json", json.dumps({"rows": rows}))
    return run_dir


def _argv(tmp_path: Path, root: Path, *extra: str) -> list:
    return [
        "--profile-dir", str(tmp_path / "profiles"), "--profile", PROFILE, "--run-id", RUN,
        "--queue", QUEUE, "--repo-root", str(root), *extra,
    ]


def _run(capsys, argv) -> tuple:
    code = grind_rows.main(["settle-run", *argv])
    out = capsys.readouterr().out.strip()
    return code, (json.loads(out) if out else None)


def _status(root: Path) -> dict:
    return {l[3:]: l[:2] for l in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def test_verb_is_registered():
    assert grind_rows._VERBS["settle-run"] is grind_rows._cmd_settle_run


def test_evidence_reconstruction_lands_declared_and_archive_and_drops_unlandable(repo, tmp_path, capsys):
    _finish_row(repo, "r1")
    _triage(repo, {"r1": ["a.py"]})
    code, reply = _run(capsys, _argv(tmp_path, repo))
    assert code == 0 and reply["committed"] is True and reply["source"] == "evidence"
    assert [r["row_id"] for r in reply["rows"]] == ["r1"]
    assert {d["reason"] for d in reply["dropped"]} == {"deletion-git-cannot-see"}
    assert "Grind-Row: r1 settled" in _git(["log", "-1", "--format=%B"], repo)
    assert "settled-after-run" in _git(["log", "-1", "--format=%B"], repo)
    status = _status(repo)
    assert f"{ARCHIVE}/r1.yaml" not in status and "a.py" not in status
    assert "b.py" in status and "other.py" in status
    assert not (repo / f"state/queue-grind/{PROFILE}/r1.jsonl").exists()
    assert f"{EVIDENCE}/r1.done.md" in status


def test_reconstruction_picks_only_rows_with_done_evidence_and_on_disk_archive(repo, tmp_path):
    _finish_row(repo, "r1")
    _write(repo, f"{ARCHIVE}/no-evidence.yaml")
    _write(repo, f"{EVIDENCE}/no-archive.done.md")
    rows = grind_settle_run.reconstruct_rows(
        repo, grind_settle_run.grind_profile.load_profile(PROFILE, tmp_path / "profiles"), RUN, [QUEUE],
        repo / "scratch" / "warp" / RUN,
    )
    assert [r["row_id"] for r in rows] == ["r1"]
    assert rows[0]["paths"] == [f"{ARCHIVE}/r1.yaml"]
    assert rows[0]["deleted_paths"] == [f"{QUEUE}/r1.yaml"]


def test_params_file_source_lands_its_rows(repo, tmp_path, capsys):
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    persisted = params_path(repo, RUN)
    persisted.parent.mkdir(parents=True)
    persisted.write_text(
        json.dumps({"grind": {"profile": PROFILE, "rows": [{"row_id": "r2", "outcome": "fixed", "paths": ["b.py"]}]}}),
        encoding="utf-8",
    )
    code, reply = _run(capsys, _argv(tmp_path, repo))
    assert code == 0 and reply["source"] == "params-file" and reply["committed"] is True
    assert "b.py" not in _status(repo)


def test_rows_file_overrides_params_file(repo, tmp_path, capsys):
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import params_path

    persisted = params_path(repo, RUN)
    persisted.parent.mkdir(parents=True)
    persisted.write_text(
        json.dumps({"grind": {"profile": PROFILE, "rows": [{"row_id": "r2", "outcome": "fixed", "paths": ["b.py"]}]}}),
        encoding="utf-8",
    )
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps([{"row_id": "r3", "outcome": "fixed", "paths": ["other.py"]}]), encoding="utf-8")
    code, reply = _run(capsys, _argv(tmp_path, repo, "--rows-file", str(rows_file)))
    assert code == 0 and reply["source"] == "rows-file"
    status = _status(repo)
    assert "other.py" not in status and "b.py" in status


def test_ledger_root_path_is_dropped_not_refused(repo, tmp_path, capsys):
    _finish_row(repo, "r1")
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(
        json.dumps([{"row_id": "r1", "outcome": "fixed", "paths": ["a.py", f"{EVIDENCE}/r1.done.md"]}]),
        encoding="utf-8",
    )
    code, reply = _run(capsys, _argv(tmp_path, repo, "--rows-file", str(rows_file)))
    assert code == 0 and reply["committed"] is True
    assert [d["reason"] for d in reply["dropped"]] == ["ledger-root"]
    assert f"{EVIDENCE}/r1.done.md" in _status(repo)


def test_a_row_whose_archive_is_already_committed_lands_nothing(repo, tmp_path, capsys):
    _finish_row(repo, "r1")
    _git(["add", f"{ARCHIVE}/r1.yaml"], repo)
    _git(["commit", "-q", "-m", "archived"], repo)
    code, reply = _run(capsys, _argv(tmp_path, repo))
    assert code == 0 and reply["committed"] is False and reply["rows"] == []
    assert "a.py" in _status(repo)


def test_dry_run_lands_nothing_and_lists_unclaimed_dirty(repo, tmp_path, capsys):
    _finish_row(repo, "r1")
    _triage(repo, {"r1": ["a.py"]})
    head = _git(["rev-parse", "HEAD"], repo)
    code, reply = _run(capsys, _argv(tmp_path, repo, "--dry-run"))
    assert code == 0 and reply["source"] == "evidence"
    assert [r["row_id"] for r in reply["rows"]] == ["r1"]
    assert "b.py" in reply["unclaimed_dirty"] and "other.py" in reply["unclaimed_dirty"]
    assert "a.py" not in reply["unclaimed_dirty"]
    assert not any(p.startswith("state/queue-grind/") for p in reply["unclaimed_dirty"])
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert (repo / f"state/queue-grind/{PROFILE}/r1.jsonl").exists()


def test_usage_error_without_a_queue(repo, tmp_path, capsys):
    code = grind_rows.main(
        ["settle-run", "--profile-dir", str(tmp_path / "profiles"), "--profile", PROFILE,
         "--run-id", RUN, "--repo-root", str(repo)]
    )
    assert code == grind_rows.EXIT_USAGE
