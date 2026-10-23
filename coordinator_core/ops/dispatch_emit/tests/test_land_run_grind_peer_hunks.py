"""The grind landing commits only its own rows' bytes of a peer-dirtied file and refuses an import closure HEAD lacks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import write_object
from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract
from coordinator_core.ops.dispatch_emit import land_run_grind
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "20261022T000000Z"
BASE = "".join(f"line{i} = {i}\n" for i in range(1, 21))
ROW_EDIT = BASE.replace("line2 = 2\n", "line2 = 'row'\n")
PEER_EDIT = ROW_EDIT.replace("line19 = 19\n", "line19 = 'peer'\n")


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "shared.py").write_text(BASE, encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "probe.py").write_text("def _ready():\n    return 1\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _blob(root: Path, text: str) -> str:
    return write_object(resolve_git_common_dir(root), b"blob", text.encode("utf-8"))


def _images(root: Path, row_id: str, path: str, pre: str, post: str) -> None:
    f = root / contract.row_images_rel(RUN, row_id) / contract.IMAGE_RECORD_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"pre": {path: _blob(root, pre)}, "post": {path: _blob(root, post)}}), encoding="utf-8")


def _land(root: Path, *paths: str) -> dict:
    row = {"row_id": "r1", "outcome": "fixed", "paths": list(paths), "deleted_paths": []}
    params = {"script_path": "run.mjs", "run_id": RUN, "grind": {"profile": "bug", "rows": [row]}}
    return land_run_grind.commit_grind_rows(root, root / ".git", params)


def _head(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root).strip()


def test_shared_file_lands_the_row_hunk_only_and_keeps_the_peer_hunk_uncommitted(repo):
    (repo / "shared.py").write_text(PEER_EDIT, encoding="utf-8")
    _images(repo, "r1", "shared.py", BASE, ROW_EDIT)

    out = _land(repo, "shared.py")

    assert out["committed"] is True, out
    assert _git(["show", "HEAD:shared.py"], repo) == ROW_EDIT
    assert (repo / "shared.py").read_text(encoding="utf-8") == PEER_EDIT
    assert "shared.py" in _git(["status", "--porcelain"], repo)


def test_landing_that_imports_a_worktree_only_package_is_refused_and_head_stays(repo):
    (repo / "a.py").write_text("from wt_pkg.mod import X\n", encoding="utf-8")
    (repo / "wt_pkg").mkdir()
    (repo / "wt_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "wt_pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    head = _head(repo)

    out = _land(repo, "a.py")

    assert out["committed"] is False and out["refused"] == contract.REFUSED_IMPORT_CLOSURE, out
    assert out["integrity"]
    assert _head(repo) == head


def test_landing_that_imports_a_name_only_the_worktree_copy_binds_is_refused(repo):
    (repo / "pkg" / "probe.py").write_text(
        "def _ready():\n    return 1\n\n\ndef _offer():\n    return 2\n", encoding="utf-8"
    )
    (repo / "pkg" / "commit.py").write_text("from pkg.probe import _offer, _ready\n", encoding="utf-8")
    head = _head(repo)

    out = _land(repo, "pkg/commit.py")

    assert out["committed"] is False and out["refused"] == contract.REFUSED_IMPORT_CLOSURE, out
    assert _head(repo) == head


def test_import_of_a_name_head_binds_still_lands(repo):
    (repo / "pkg" / "commit.py").write_text("from pkg.probe import _ready\n", encoding="utf-8")

    out = _land(repo, "pkg/commit.py")

    assert out["committed"] is True, out
