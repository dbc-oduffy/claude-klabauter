"""A lessons grind row whose fix edits an undeclared file lands that file and the archive in one commit.

Case A drives the commit stage's engine side: the prewrite verb, then `dispatch.land_run` on the
params file it wrote. Case B stops the stage before landing and settles with the default
`grind-row settle-run` (no `--rows-file`), whether or not the params file survived.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.backlog_grind_assemble.tests.test_grind_rows import _write_profile
from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

from .test_grind_compose import _compose

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261113T002730Z"
SCRIPT = "scratch/warp/lessons.workflow.mjs"
SESSION = "sess-lessons-fix"
PROFILE = "lessons"
ROW = "l1"
QUEUE = "state/lessons"
DECLARED = "docs/guide.md"
UNDECLARED = "naked-vs-coordinator/README.md"
PEER_DIRT = "peer/notes.md"
EVIDENCE = f"state/queue-grind/{PROFILE}/evidence/{RUN}/{ROW}.done.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _status(root: Path) -> dict:
    return {ln[3:]: ln[:2] for ln in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def _head_files(root: Path) -> set:
    return set(_git(["show", "--name-only", "--no-renames", "--format=", "HEAD"], root).split())


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A repo after a lessons row's fix: declared and undeclared edits, row archived, peer dirt beside."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    for rel in (DECLARED, UNDECLARED, PEER_DIRT):
        _write(repo, rel, "seed\n")
    _write(repo, f"{QUEUE}/{ROW}.yaml", f"id: {ROW}\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    profiles = tmp_path / "profiles"
    _write_profile(profiles, archive_path="archive/lessons", schema_rel="schema/lessons.schema.json")
    (profiles / "bug.yaml").rename(profiles / f"{PROFILE}.yaml")
    archived = grind_rows.archive_destination("archive/lessons", RUN, f"{ROW}.yaml")

    _write(repo, DECLARED, "fixed\n")
    _write(repo, UNDECLARED, "fixed\n")
    _write(repo, PEER_DIRT, "peer edit\n")
    (repo / QUEUE / f"{ROW}.yaml").unlink()
    _write(repo, archived, f"id: {ROW}\n")
    _write(repo, EVIDENCE, "done\n")
    run_dir = repo / RUN_DIR_ROOT / RUN
    _write(run_dir, "records/b0.json", json.dumps({"rows": [{"row": ROW, "declared_files": [DECLARED]}]}))

    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    params = {
        "run_id": RUN, "run_token": run_token.mint(repo, RUN, SCRIPT), "script_path": SCRIPT,
        "grind": {
            "profile": PROFILE, "halted": None,
            "rows": [{
                "row_id": ROW, "outcome": "fixed",
                "paths": [DECLARED, UNDECLARED, archived],
                "deleted_paths": [f"{QUEUE}/{ROW}.yaml"],
            }],
        },
    }
    spawn = home / "projects" / "p" / SESSION / "subagents" / "workflows" / "wf_1"
    spawn.mkdir(parents=True)
    prompt = "\n".join(["Commit.", *chan.render_params_block(params).split("\n")])
    (spawn / "agent-1.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8"
    )
    return repo, profiles, archived


def _prewrite(repo: Path) -> int:
    return grind_rows.main(["prewrite-commit-params", "--repo-root", str(repo), "--run-id", RUN])


def _settle_argv(profiles: Path, repo: Path, *extra: str) -> list:
    return [
        "settle-run", "--profile-dir", str(profiles), "--profile", PROFILE, "--run-id", RUN,
        "--queue", QUEUE, "--repo-root", str(repo), *extra,
    ]


def _assert_one_commit_holding_the_fix(repo: Path, archived: str, before: int) -> None:
    assert int(_git(["rev-list", "--count", "HEAD"], repo)) == before + 1
    files = _head_files(repo)
    assert {DECLARED, UNDECLARED, archived, f"{QUEUE}/{ROW}.yaml"} <= files
    assert PEER_DIRT not in files
    status = _status(repo)
    for path in (DECLARED, UNDECLARED, archived, f"{QUEUE}/{ROW}.yaml"):
        assert path not in status
    assert PEER_DIRT in status


def test_commit_stage_lands_the_undeclared_fix_and_the_archive_in_one_commit(run):
    repo, _profiles, archived = run
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    target = chan.params_path(repo, RUN)
    assert not target.exists()
    assert _prewrite(repo) == grind_rows.EXIT_OK
    params = json.loads(target.read_text(encoding="utf-8"))
    out = land_run._handler(params, repo_root=repo / ".git")
    assert out.get("committed") is True, out
    _assert_one_commit_holding_the_fix(repo, archived, before)


def test_composed_grind_script_calls_the_prewrite_before_the_land_op():
    script = _compose()
    start = script.index("async function _terminalCommitCall")
    call = script[start: script.index("function _counts()", start)]
    assert call.index("grind-row prewrite-commit-params") < call.index("coordinator-invoke dispatch.land_run")


@pytest.mark.parametrize("params_file_survives", [True, False], ids=["params-file", "evidence"])
def test_settle_run_without_rows_file_lands_the_undeclared_fix_and_the_archive(
    run, capsys, params_file_survives
):
    repo, profiles, archived = run
    before = int(_git(["rev-list", "--count", "HEAD"], repo))
    assert _prewrite(repo) == grind_rows.EXIT_OK
    if not params_file_survives:
        chan.params_path(repo, RUN).unlink()
    capsys.readouterr()
    code = grind_rows.main(_settle_argv(profiles, repo))
    reply = json.loads(capsys.readouterr().out.strip())
    assert code == grind_rows.EXIT_OK and reply["committed"] is True, reply
    assert reply["source"] == ("params-file" if params_file_survives else "evidence")
    _assert_one_commit_holding_the_fix(repo, archived, before)


def test_settle_run_never_stages_a_path_no_row_names(run, capsys):
    repo, profiles, _archived = run
    assert _prewrite(repo) == grind_rows.EXIT_OK
    chan.params_path(repo, RUN).unlink()
    capsys.readouterr()
    code = grind_rows.main(_settle_argv(profiles, repo, "--dry-run"))
    plan = json.loads(capsys.readouterr().out.strip())
    assert code == grind_rows.EXIT_OK
    assert UNDECLARED in plan["rows"][0]["paths"]
    assert PEER_DIRT in plan["unclaimed_dirty"] and PEER_DIRT not in plan["rows"][0]["paths"]


def test_without_a_fix_paths_record_the_undeclared_fix_stays_unclaimed(run, capsys):
    repo, profiles, _archived = run
    assert _prewrite(repo) == grind_rows.EXIT_OK
    chan.params_path(repo, RUN).unlink()
    (repo / RUN_DIR_ROOT / RUN / "fix-paths" / f"{ROW}.json").unlink()
    capsys.readouterr()
    code = grind_rows.main(_settle_argv(profiles, repo, "--dry-run"))
    plan = json.loads(capsys.readouterr().out.strip())
    assert code == grind_rows.EXIT_OK
    assert UNDECLARED not in plan["rows"][0]["paths"] and UNDECLARED in plan["unclaimed_dirty"]
