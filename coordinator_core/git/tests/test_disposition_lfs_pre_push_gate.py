"""The lfs pre-push gate entry, driven through `hook_dispositions` against tmp_path repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import hook_dispositions as hd
from coordinator_core.git.hook_disposition_entries import lfs_pre_push_gate as entry
from coordinator_core.ops.install_lfs_pre_push_hook import hook_body

STOCK = """#!/bin/sh
command -v git-lfs >/dev/null 2>&1 || { echo >&2 "git-lfs missing"; exit 2; }
git lfs pre-push "$@"
"""

OLDER_OURS = "#!/bin/sh\n# coordinator-lfs-gate (older rendering)\nexit 0\n"
FOREIGN = '#!/bin/sh\n# do not run git lfs pre-push here\necho "mine"\n'


@pytest.fixture(autouse=True)
def _table(monkeypatch):
    monkeypatch.setattr(hd, "DISPOSITIONS", (entry.ENTRY,), raising=False)


def _repo(tmp_path: Path, body: str | None, *, engine: bool = True) -> Path:
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    if body is not None:
        (hooks / "pre-push").write_bytes(body.encode("utf-8"))
    if engine:
        marker = tmp_path / "coordinator_core/ops/install_lfs_pre_push_hook.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")
    return tmp_path


def _hook(root: Path) -> Path:
    return root / ".git" / "hooks" / "pre-push"


def _verdict(root: Path, *, check_only: bool) -> str:
    [(eid, name, verdict)] = hd.apply_repo(root, check_only=check_only)
    assert (eid, name) == (entry.ENTRY.id, "pre-push")
    return verdict


@pytest.mark.parametrize("body", [STOCK, OLDER_OURS])
def test_stock_and_older_ours_are_replaced_and_backed_up(tmp_path, body):
    root = _repo(tmp_path, body)
    assert _verdict(root, check_only=False) == "replaced"
    assert _hook(root).read_text(encoding="utf-8") == hook_body()
    assert _hook(root).with_name("pre-push.retired").read_bytes() == body.encode("utf-8")


@pytest.mark.parametrize("body", [STOCK, OLDER_OURS])
def test_check_only_reports_stale_and_changes_nothing(tmp_path, body):
    root = _repo(tmp_path, body)
    assert _verdict(root, check_only=True) == "stale"
    assert _hook(root).read_text(encoding="utf-8") == body


def test_current_ours_is_current_and_untouched(tmp_path):
    root = _repo(tmp_path, hook_body())
    assert _verdict(root, check_only=False) == "current"
    assert _hook(root).read_text(encoding="utf-8") == hook_body()
    assert not _hook(root).with_name("pre-push.retired").exists()


def test_foreign_is_left_alone(tmp_path):
    root = _repo(tmp_path, FOREIGN)
    assert _verdict(root, check_only=False) == "unidentified-left-alone"
    assert _hook(root).read_text(encoding="utf-8") == FOREIGN
    assert not _hook(root).with_name("pre-push.retired").exists()


def test_absent_hook_is_not_created(tmp_path):
    root = _repo(tmp_path, None)
    assert _verdict(root, check_only=False) == "absent"
    assert not _hook(root).exists()


def test_non_engine_clone_stays_byte_identical(tmp_path):
    root = _repo(tmp_path, STOCK, engine=False)
    assert hd.apply_repo(root, check_only=False) == []
    assert _hook(root).read_bytes() == STOCK.encode("utf-8")
    assert not _hook(root).with_name("pre-push.retired").exists()
