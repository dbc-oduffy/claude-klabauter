"""The retired lfs pre-push gate entry, driven through `hook_dispositions` against tmp_path repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import hook_dispositions as hd
from coordinator_core.git.hook_disposition_entries import retired_lfs_pre_push_gate as entry

CURRENT_OURS = (
    "#!/bin/sh\n# coordinator-lfs-gate (installed by the retired installer)\n"
    'echo "coordinator-lfs-gate: not-tracked skipped" >&2\nexit 0\n'
)
OLDER_OURS = "#!/bin/sh\n# coordinator-lfs-gate (older rendering)\nexit 0\n"
STOCK = """#!/bin/sh
command -v git-lfs >/dev/null 2>&1 || { echo >&2 "git-lfs missing"; exit 2; }
git lfs pre-push "$@"
"""
MID_LINE = '#!/bin/sh\necho "see # coordinator-lfs-gate docs"\nexit 0\n'


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


@pytest.mark.parametrize("body", [CURRENT_OURS, OLDER_OURS])
def test_banner_bodies_are_removed_and_backed_up(tmp_path, body):
    root = _repo(tmp_path, body)
    assert hd.apply_repo(root, check_only=False) == [(entry.ENTRY_ID, "pre-push", "removed")]
    assert not _hook(root).exists()
    assert _hook(root).with_name("pre-push.retired").read_bytes() == body.encode("utf-8")


@pytest.mark.parametrize("body", [STOCK, MID_LINE])
def test_stock_shim_and_mid_line_mention_are_left_alone(tmp_path, body):
    root = _repo(tmp_path, body)
    assert hd.apply_repo(root, check_only=False) == [(entry.ENTRY_ID, "pre-push", "current")]
    assert _hook(root).read_bytes() == body.encode("utf-8")
    assert not _hook(root).with_name("pre-push.retired").exists()


def test_lfs_tracking_gitattributes_leaves_hook_alone(tmp_path):
    root = _repo(tmp_path, CURRENT_OURS)
    (root / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs\n", encoding="utf-8")
    assert hd.apply_repo(root, check_only=False) == []
    assert _hook(root).read_bytes() == CURRENT_OURS.encode("utf-8")


def test_lfs_tracking_info_attributes_leaves_hook_alone(tmp_path):
    root = _repo(tmp_path, CURRENT_OURS)
    info = root / ".git" / "info"
    info.mkdir()
    (info / "attributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
    assert hd.apply_repo(root, check_only=False) == []
    assert _hook(root).read_bytes() == CURRENT_OURS.encode("utf-8")
