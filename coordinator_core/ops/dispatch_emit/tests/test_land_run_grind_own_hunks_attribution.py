"""The grind landing attributes each row's own hunks, untracked files and satisfied deletions through commit_grind_rows."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import write_object
from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract
from coordinator_core.ops.dispatch_emit import land_run_grind
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261026T000000Z"
BASE = "".join(f"line{i} = {i}\n" for i in range(1, 21))
EDIT_A = BASE.replace("line2 = 2\n", "line2 = 'a'\n")
EDIT_B = BASE.replace("line19 = 19\n", "line19 = 'b'\n")
EDIT_AB = EDIT_A.replace("line19 = 19\n", "line19 = 'b'\n")
NEVER_TRACKED = "state/bug-backlog/r1.yaml"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _show(root: Path, rev_path: str) -> bytes:
    return subprocess.run(
        ["git", "show", rev_path], cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "shared.py").write_bytes(BASE.encode("utf-8"))
    (root / "a.py").write_bytes(b"x = 1\n")
    (root / "tracked.yaml").write_bytes(b"id: tracked\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _blob(root: Path, data: bytes) -> str:
    return write_object(resolve_git_common_dir(root), b"blob", data)


def _images(root: Path, row_id: str, path: str, pre: bytes | None, post: bytes) -> None:
    f = root / contract.row_images_rel(RUN, row_id) / contract.IMAGE_RECORD_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pre": {path: None if pre is None else _blob(root, pre)},
        "post": {path: _blob(root, post)},
    }
    f.write_text(json.dumps(record), encoding="utf-8")


def _row(row_id: str, paths: list[str], deleted: list[str] | None = None) -> dict:
    return {"row_id": row_id, "outcome": "fixed", "paths": paths, "deleted_paths": deleted or []}


def _land(root: Path, rows: list[dict]) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, "grind": {"profile": "bug", "rows": rows}}
    return land_run_grind.commit_grind_rows(root, root / ".git", params)


def _message(root: Path) -> str:
    return _git(["log", "-1", "--format=%B"], root)


def _assert_both_rows_landed(root: Path, out: dict) -> None:
    assert out["committed"] is True, out
    assert _show(root, "HEAD:shared.py") == EDIT_AB.encode("utf-8")
    assert not [d for d in out["dropped"] if d["reason"] == "peer-hunks-conflict"], out["dropped"]
    assert {r["row_id"] for r in out["rows"]} == {"r1", "r2"}
    body = _message(root)
    assert "Grind-Row: r1 fixed" in body and "Grind-Row: r2 fixed" in body


def test_two_rows_sequential_disjoint_hunks_both_land(repo):
    (repo / "shared.py").write_bytes(EDIT_AB.encode("utf-8"))
    _images(repo, "r1", "shared.py", BASE.encode(), EDIT_A.encode())
    _images(repo, "r2", "shared.py", EDIT_A.encode(), EDIT_AB.encode())

    out = _land(repo, [_row("r1", ["shared.py"]), _row("r2", ["shared.py"])])

    _assert_both_rows_landed(repo, out)


def test_two_rows_concurrent_disjoint_hunks_both_land(repo):
    (repo / "shared.py").write_bytes(EDIT_AB.encode("utf-8"))
    _images(repo, "r1", "shared.py", BASE.encode(), EDIT_A.encode())
    _images(repo, "r2", "shared.py", BASE.encode(), EDIT_B.encode())

    out = _land(repo, [_row("r1", ["shared.py"]), _row("r2", ["shared.py"])])

    _assert_both_rows_landed(repo, out)


def test_crlf_worktree_over_lf_head_lands_own_hunks(repo):
    _git(["config", "core.autocrlf", "true"], repo)
    crlf = BASE.replace("\n", "\r\n")
    (repo / "shared.py").write_bytes(crlf.encode("utf-8"))
    image = ["--run-id", RUN, "--row-id", "r1", "--repo-root", str(repo), "--path", "shared.py"]
    assert grind_rows.main(["image", *image[:4], "--phase", "pre", *image[4:]]) == 0
    edited = crlf.replace("line2 = 2\r\n", "line2 = 'a'\r\n")
    (repo / "shared.py").write_bytes(edited.encode("utf-8"))
    assert grind_rows.main(["image", *image[:4], "--phase", "post", *image[4:]]) == 0

    out = _land(repo, [_row("r1", ["shared.py"])])

    assert out["committed"] is True, out
    assert not [d for d in out["dropped"] if d["reason"] == "peer-hunks-conflict"], out["dropped"]
    assert _show(repo, "HEAD:shared.py") == EDIT_A.encode("utf-8")
    assert _git(["status", "--porcelain", "--", "shared.py"], repo) == ""


def test_untracked_file_with_prior_pre_image_lands(repo):
    first = b"def test_one():\n    pass\n"
    final = first + b"\n\ndef test_two():\n    pass\n"
    (repo / "new_test.py").write_bytes(final)
    _images(repo, "r1", "new_test.py", first, final)

    out = _land(repo, [_row("r1", ["new_test.py"])])

    assert out["committed"] is True, out
    assert _show(repo, "HEAD:new_test.py") == final
    assert not [d for d in out["dropped"] if d["path"] == "new_test.py"], out["dropped"]


def test_untracked_new_file_without_pre_lands(repo):
    final = b"def test_one():\n    pass\n"
    (repo / "new_test.py").write_bytes(final)
    _images(repo, "r1", "new_test.py", None, final)

    out = _land(repo, [_row("r1", ["new_test.py"])])

    assert out["committed"] is True, out
    assert _show(repo, "HEAD:new_test.py") == final
    assert out["dropped"] == []


def test_never_tracked_deletion_is_not_unlanded(repo):
    (repo / "a.py").write_bytes(b"x = 2\n")

    out = _land(repo, [_row("r1", ["a.py"], [NEVER_TRACKED])])

    assert out["committed"] is True, out
    assert not [d for d in out["dropped"] + out["stranded"] if d["reason"] == "deletion-git-cannot-see"]
    assert "Row-Unlanded" not in _message(repo)


def test_tracked_clean_undeleted_path_still_drops(repo):
    (repo / "a.py").write_bytes(b"x = 2\n")

    out = _land(repo, [_row("r1", ["a.py"], ["tracked.yaml"])])

    assert out["committed"] is True, out
    drop = {"row_id": "r1", "path": "tracked.yaml", "reason": "deletion-git-cannot-see"}
    assert drop in out["dropped"]
    assert "Row-Unlanded: r1 tracked.yaml deletion-git-cannot-see" in _message(repo)
