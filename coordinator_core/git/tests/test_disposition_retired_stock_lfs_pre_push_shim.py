"""The retired stock git-lfs pre-push shim entry, driven through `hook_dispositions` against tmp_path repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import hook_dispositions as hd
from coordinator_core.git.hook_disposition_entries import (
    load_entries,
    retired_stock_lfs_pre_push_shim as entry,
)

STOCK = (
    "#!/bin/sh\n"
    "command -v git-lfs >/dev/null 2>&1 || { printf >&2 \"\\n%s\\n\\n\" \"This repository is "
    "configured for Git LFS but 'git-lfs' was not found on your path.\"; exit 2; }\n"
    'git lfs pre-push "$@"\n'
)
URL_GUARD = "#!/bin/sh\nremote_url=$2\ncase $remote_url in *fifa*) exit 1;; esac\nexit 0\n"
STOCK_PLUS_EXTRA = STOCK + "echo extra\n"


@pytest.fixture(autouse=True)
def _table(monkeypatch):
    monkeypatch.setattr(hd, "DISPOSITIONS", (entry.ENTRY,), raising=False)


def _repo(tmp_path: Path, body: str) -> Path:
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-push").write_bytes(body.encode("utf-8"))
    return tmp_path


def _hook(root: Path) -> Path:
    return root / ".git" / "hooks" / "pre-push"


def test_entry_is_registered():
    assert entry.ENTRY in load_entries()


@pytest.mark.parametrize("body", [STOCK, STOCK.replace("\n", "\r\n")])
def test_stock_shim_lf_and_crlf_removed_with_backup(tmp_path, body):
    root = _repo(tmp_path, body)
    assert hd.apply_repo(root, check_only=False) == [(entry.ENTRY_ID, "pre-push", "removed")]
    assert not _hook(root).exists()
    assert _hook(root).with_name("pre-push.retired").read_bytes() == body.encode("utf-8")


def test_root_gitattributes_lfs_leaves_shim(tmp_path):
    root = _repo(tmp_path, STOCK)
    (root / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    assert hd.apply_repo(root, check_only=False) == []
    assert _hook(root).read_bytes() == STOCK.encode("utf-8")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_nested_tracked_gitattributes_lfs_leaves_shim(tmp_path):
    import subprocess

    root = _repo(tmp_path, STOCK)
    sub = root / "sub"
    sub.mkdir()
    (sub / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, creationflags=flags)
    subprocess.run(
        ["git", "add", "sub/.gitattributes"], cwd=root, check=True, creationflags=flags
    )
    assert hd.apply_repo(root, check_only=False) == []
    assert _hook(root).read_bytes() == STOCK.encode("utf-8")


def test_commented_lfs_mention_still_removes(tmp_path):
    root = _repo(tmp_path, STOCK)
    (root / ".gitattributes").write_text("# never filter=lfs here\n", encoding="utf-8")
    assert hd.apply_repo(root, check_only=False)[0][2] == "removed"


@pytest.mark.parametrize("body", [URL_GUARD, STOCK_PLUS_EXTRA])
def test_foreign_or_extended_hook_left_alone(tmp_path, body):
    root = _repo(tmp_path, body)
    assert hd.apply_repo(root, check_only=False) == [(entry.ENTRY_ID, "pre-push", "current")]
    assert _hook(root).read_bytes() == body.encode("utf-8")
