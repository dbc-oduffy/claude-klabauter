"""``dispatch.land_credited``: commits exactly the credited paths differing from HEAD, names holds, refuses a lost CAS."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit
from coordinator_core.ops.dispatch_emit import land_credited as lane
from coordinator_core.ops.dispatch_emit import land_credited_contract as contract
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

RUN = "warp-test-run"
SCRIPT = "scratch/run.js"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    for rel in ("src/a.py", "src/b.py", "src/c.py", "other/z.py"):
        _write(root, rel, "v = 0\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    marker = render_marker(CommitRequest(chunks=(
        ChunkCommit(id="C1", title="one", paths=("src/a.py", "src/b.py")),
        ChunkCommit(id="C2", title="two", paths=("src/c.py",)),
    )))
    _write(root, SCRIPT, f"// script\n{marker}\n")
    return root


def _request() -> contract.LandCreditedRequest:
    return contract.LandCreditedRequest(run_id=RUN, plan_path="docs/plans/p.md", script_path=SCRIPT)


def _head(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root).strip()


def _committed(root: Path) -> list:
    return sorted(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def test_commits_exactly_the_credited_paths_that_differ_from_head(repo):
    _write(repo, "src/a.py", "v = 1\n")
    _write(repo, "src/c.py", "v = 2\n")
    _write(repo, "other/z.py", "v = 9\n")

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert reply["landed"] == ["src/a.py", "src/c.py"]
    assert reply["sha"] == _head(repo)
    assert _committed(repo) == ["src/a.py", "src/c.py"]
    assert _git(["show", "HEAD:other/z.py"], repo) == "v = 0\n"
    assert f"Run-Id: {RUN}" in _git(["log", "-1", "--format=%B"], repo)


def test_a_credited_path_equal_to_head_is_not_committed(repo):
    _write(repo, "src/a.py", "v = 1\n")

    reply = lane.land(repo, _request())

    assert _committed(repo) == ["src/a.py"]
    assert reply["landed"] == ["src/a.py"]


def test_nothing_differing_lands_nothing(repo):
    before = _head(repo)

    reply = lane.land(repo, _request())

    assert reply["committed"] is False
    assert reply["refused"] == contract.REFUSED_NOTHING_TO_LAND
    assert _head(repo) == before


def test_a_credited_deletion_is_committed(repo):
    (repo / "src/b.py").unlink()

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert reply["landed"] == ["src/b.py"]
    assert "src/b.py" not in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()


def test_the_run_record_dir_rides_with_the_credited_paths(repo):
    _write(repo, "src/a.py", "v = 1\n")
    _write(repo, f"state/warp/{RUN}/run.json", "{}\n")

    reply = lane.land(repo, _request())

    assert reply["landed"] == ["src/a.py", f"state/warp/{RUN}/run.json"]


def test_a_lost_cas_race_refuses_and_writes_nothing(repo, monkeypatch):
    _write(repo, "src/a.py", "v = 1\n")
    before = _head(repo)
    monkeypatch.setattr(git_commit, "cas_ref", lambda *a, **k: False)

    reply = lane.land(repo, _request())

    assert reply["committed"] is False
    assert reply["refused"] == contract.REFUSED_COMMIT
    assert "compare-and-swap" in reply["error"]
    assert _head(repo) == before


def test_bytes_changed_after_the_blobs_were_read_are_refused(repo, monkeypatch):
    _write(repo, "src/a.py", "v = 1\n")
    before = _head(repo)
    real = git_commit.checkin_blob_sha

    def stale(root, rel):
        sha = real(root, rel)
        _write(root, rel, "v = 777\n")
        return sha

    monkeypatch.setattr(git_commit, "checkin_blob_sha", stale)

    reply = lane.land(repo, _request())

    assert reply["committed"] is False
    assert reply["refused"] == contract.REFUSED_COMMIT
    assert _head(repo) == before


def _install_holds(monkeypatch, held: dict):
    from coordinator_core.ops.dispatch_emit import land_credited_holds

    monkeypatch.setattr(land_credited_holds, "holds", lambda root, request: dict(held))


def test_a_held_path_is_named_and_left_out_without_refusing(repo, monkeypatch):
    _write(repo, "src/a.py", "v = 1\n")
    _write(repo, "src/c.py", "v = 200\n")  # a different size, so a same-tick rewrite cannot read as clean in `git status`
    _install_holds(monkeypatch, {"src/c.py": "halted-run:other", "src/untouched.py": "peer"})

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert reply["landed"] == ["src/a.py"]
    assert reply["withheld"] == {"src/c.py": "halted-run:other"}
    assert _committed(repo) == ["src/a.py"]
    assert "src/c.py" in _git(["status", "--porcelain"], repo)


def test_every_path_held_lands_nothing_and_names_them(repo, monkeypatch):
    _write(repo, "src/a.py", "v = 1\n")
    _install_holds(monkeypatch, {"src/a.py": "never-commit"})
    before = _head(repo)

    reply = lane.land(repo, _request())

    assert reply["refused"] == contract.REFUSED_NOTHING_TO_LAND
    assert reply["withheld"] == {"src/a.py": "never-commit"}
    assert _head(repo) == before


def test_a_script_without_a_marker_is_a_bad_request(repo):
    _write(repo, SCRIPT, "// nothing\n")

    reply = lane.land(repo, _request())

    assert reply["refused"] == contract.REFUSED_BAD_REQUEST


def test_a_script_path_outside_the_repo_is_a_bad_request(repo):
    request = contract.LandCreditedRequest(run_id=RUN, plan_path="p.md", script_path="../outside.js")

    assert lane.land(repo, request)["refused"] == contract.REFUSED_BAD_REQUEST


def test_an_unsafe_run_id_is_a_bad_request(repo):
    request = contract.LandCreditedRequest(run_id="../x", plan_path="p.md", script_path=SCRIPT)

    assert lane.land(repo, request)["refused"] == contract.REFUSED_BAD_REQUEST


def test_handler_refuses_missing_params_and_is_registered():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP[contract.OP_NAME] == "coordinator_core.ops.dispatch_emit.land_credited"
    assert lane._handler({"run_id": RUN})["refused"] == contract.REFUSED_BAD_REQUEST


def test_emitted_workflow_output_in_the_record_dir_is_not_landed(repo):
    _write(repo, "src/a.py", "v = 1\n")
    _write(repo, f"state/warp/{RUN}/run.json", "{}\n")
    _write(repo, f"state/warp/{RUN}/resume-script.workflow.mjs", "// emitted\n")
    _write(repo, f"state/warp/{RUN}/resume-script.workflow.mjs.emitted.json", "{}\n")

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert reply["landed"] == ["src/a.py", f"state/warp/{RUN}/run.json"]


def test_op_modules_do_not_import_land_run():
    import ast

    for module in (lane, __import__("coordinator_core.ops.dispatch_emit.land_credited_holds", fromlist=["x"])):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            n.module if isinstance(n, ast.ImportFrom) else a.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
            for a in (n.names if isinstance(n, ast.Import) else [None])
        }
        assert not any(m and m.endswith("land_run") for m in imported), module.__name__
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "land_run" not in names, module.__name__


def test_a_credited_deletion_lands_when_the_scoped_status_is_unavailable(repo, monkeypatch):
    import coordinator_core.git.scoped_changed as sc

    monkeypatch.setattr(sc, "scoped_changed", lambda *a, **k: None)
    (repo / "src/b.py").unlink()

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert "src/b.py" in reply["landed"]
    assert "src/b.py" not in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()
