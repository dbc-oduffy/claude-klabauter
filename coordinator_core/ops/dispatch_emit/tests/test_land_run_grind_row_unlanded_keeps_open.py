"""A grind row with a path dropped from the landing is not reported committed and keeps its ledger."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run_grind
from coordinator_core.testing.git_write import write_seen_by_git
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261015T000000Z"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    for name in ("a.py", "b.py", "gone.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    for name in ("a.py", "b.py"):
        write_seen_by_git(root / name, "x = 2\n")
    return root


def _call(root: Path, rows) -> dict:
    params = {"script_path": "run.mjs", "run_id": RUN, "grind": {"profile": "bug", "rows": rows}}
    return land_run_grind.land(root, root / ".git", params)


def _row(row_id, paths):
    return {"row_id": row_id, "outcome": "fixed", "paths": list(paths), "deleted_paths": []}


def test_row_with_dropped_path_is_not_reported_committed(repo) -> None:
    (repo / "gone.py").unlink()
    out = _call(repo, [_row("r1", ["a.py", "gone.py"]), _row("r2", ["b.py"])])
    assert out["committed"] is True
    assert [r["row_id"] for r in out["rows"]] == ["r2"]
    assert [u["row_id"] for u in out["unlanded"]] == ["r1"]
    body = _git(["log", "-1", "--format=%B"], repo)
    assert "Grind-Row: r2 fixed" in body
    assert "Grind-Row: r1" not in body
    assert "Row-Unlanded: r1 gone.py" in body
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"b.py"}


def test_fully_landed_rows_carry_no_unlanded_entry(repo) -> None:
    out = _call(repo, [_row("r1", ["a.py"]), _row("r2", ["b.py"])])
    assert [r["row_id"] for r in out["rows"]] == ["r1", "r2"]
    assert "Row-Unlanded" not in _git(["log", "-1", "--format=%B"], repo)


PIN_BASE = "".join(f"l{i} = {i}\n" for i in range(1, 6))
PIN_EDIT = PIN_BASE.replace("l2 = 2\n", "l2 = 'x'\n")


@pytest.fixture
def pinned_repo(tmp_path: Path) -> Path:
    """A `text=auto` repo whose tracked file is LF in HEAD and CRLF in the worktree after the row's edit."""
    root = tmp_path / "pinned"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "true"], root)
    (root / ".gitattributes").write_bytes(b"* text=auto\n")
    (root / "p.ps1").write_bytes(PIN_BASE.encode())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _blob(root: Path, data: bytes) -> str:
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import write_object

    return write_object(resolve_git_common_dir(root), b"blob", data)


def _record_images(root: Path, pre: bytes, post: bytes) -> None:
    import json

    from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract

    f = root / contract.row_images_rel(RUN, "r1") / contract.IMAGE_RECORD_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"pre": {"p.ps1": _blob(root, pre)}, "post": {"p.ps1": _blob(root, post)}}), encoding="utf-8")


def test_solely_owned_crlf_row_under_a_text_pin_lands(pinned_repo) -> None:
    crlf = lambda s: s.replace("\n", "\r\n").encode()  # noqa: E731
    _record_images(pinned_repo, crlf(PIN_BASE), crlf(PIN_EDIT))
    (pinned_repo / "p.ps1").write_bytes(crlf(PIN_EDIT))
    out = _call(pinned_repo, [_row("r1", ["p.ps1"])])
    assert out["dropped"] == []
    assert out["unlanded"] == []
    assert [r["row_id"] for r in out["rows"]] == ["r1"]
    assert _git(["show", "HEAD:p.ps1"], pinned_repo) == PIN_EDIT


def test_lone_cr_image_under_a_text_pin_still_holds_the_row(pinned_repo) -> None:
    lone = PIN_EDIT.replace("l2 = 'x'\n", "l2 = 'x'\r").encode()
    _record_images(pinned_repo, PIN_BASE.encode(), lone)
    (pinned_repo / "p.ps1").write_bytes(lone)
    out = _call(pinned_repo, [_row("r1", ["p.ps1"])])
    assert [d["reason"] for d in out["dropped"]] == ["unattributed-shared"]
    assert [u["row_id"] for u in out["unlanded"]] == ["r1"]
    assert out["committed"] is False
