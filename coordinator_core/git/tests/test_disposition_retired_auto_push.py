"""The retired auto-push post-commit entries: every fixture case of the standalone
remover, driven through `hook_dispositions` against tmp_path repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import hook_dispositions as hd
from coordinator_core.git.hook_disposition_entries import retired_auto_push_post_commit as entry

FRESH = """#!/bin/sh
# coordinator coordinator-auto-push hook — installed by git_hook_install.
# coordinator-hook-gen: 2
_PY="$(command -v python3)"
SCRIPT="/somewhere/coordinator/bin/coordinator-auto-push"
exec "$_PY" "$SCRIPT" "$@"
"""

OLDER = """#!/bin/sh
# coordinator auto-push (crash insurance)
# installed by an earlier generation
#
# always exits 0
exec bash "$HOME/.claude/plugins/coordinator-claude/coordinator/bin/coordinator-auto-push" "$@"
"""

START = entry._START_MARKER
END = entry._END_MARKER
APPENDED = f'#!/bin/sh\necho "someone else owns this hook"\n{START}\n_PY=x\n{END}\necho tail\n'
APPENDED_REMAINDER = '#!/bin/sh\necho "someone else owns this hook"\necho tail\n'


@pytest.fixture(autouse=True)
def _table(monkeypatch):
    monkeypatch.setattr(hd, "DISPOSITIONS", entry.ENTRIES, raising=False)


def _repo(tmp_path: Path, body: str | None) -> Path:
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    if body is not None:
        (hooks / "post-commit").write_bytes(body.encode("utf-8"))
    return tmp_path


def _hook(root: Path) -> Path:
    return root / ".git" / "hooks" / "post-commit"


def _verdicts(root: Path, *, check_only: bool):
    return {eid: v for eid, _name, v in hd.apply_repo(root, check_only=check_only)}


@pytest.mark.parametrize("body", [FRESH, OLDER])
def test_whole_body_is_removed_and_backed_up(tmp_path, body):
    root = _repo(tmp_path, body)
    assert _verdicts(root, check_only=False)[entry.ENTRY.id] == "removed"
    assert not _hook(root).exists()
    assert _hook(root).with_name("post-commit.retired").read_bytes() == body.encode("utf-8")


@pytest.mark.parametrize("body", [FRESH, OLDER])
def test_check_only_reports_stale_and_changes_nothing(tmp_path, body):
    root = _repo(tmp_path, body)
    assert _verdicts(root, check_only=True)[entry.ENTRY.id] == "stale"
    assert _hook(root).read_text(encoding="utf-8") == body
    assert not _hook(root).with_name("post-commit.retired").exists()


@pytest.mark.parametrize(
    "body",
    [
        '#!/bin/sh\necho "we used to run coordinator-auto-push here"\n',
        "#!/bin/sh\n# coordinator coordinator-auto-push hook — installed by git_hook_install.\n"
        'echo "operator replaced the body"\n',
        '#!/bin/sh\nexec bash "$HOME/bin/my-hook"\n',
        "",
    ],
)
def test_unidentified_body_survives_byte_identical(tmp_path, body):
    root = _repo(tmp_path, body)
    verdicts = _verdicts(root, check_only=False)
    assert set(verdicts.values()) == {"current"}
    assert _hook(root).read_bytes() == body.encode("utf-8")
    assert not _hook(root).with_name("post-commit.retired").exists()


def test_appended_block_is_excised_and_foreign_content_kept(tmp_path):
    root = _repo(tmp_path, APPENDED)
    assert _verdicts(root, check_only=False)[entry.BLOCK_ENTRY.id] == "block-excised"
    assert _hook(root).read_text(encoding="utf-8") == APPENDED_REMAINDER
    assert _hook(root).with_name("post-commit.retired").read_text(encoding="utf-8") == APPENDED


@pytest.mark.parametrize(
    "body",
    [
        f"#!/bin/sh\n{START}\n_PY=x\n",
        f"#!/bin/sh\n{END}\n{START}\n",
        f"#!/bin/sh\n{START}\n{START}\n{END}\n",
    ],
)
def test_ambiguous_block_is_left_alone(tmp_path, body):
    root = _repo(tmp_path, body)
    assert set(_verdicts(root, check_only=False).values()) == {"current"}
    assert _hook(root).read_text(encoding="utf-8") == body


def test_absent_hook_is_reported_not_created(tmp_path):
    root = _repo(tmp_path, None)
    assert set(_verdicts(root, check_only=False).values()) == {"absent"}
    assert not _hook(root).exists()


def test_linked_worktree_resolves_to_the_common_hooks_dir(tmp_path):
    common = tmp_path / "main" / ".git"
    (common / "hooks").mkdir(parents=True)
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
    (common / "hooks" / "post-commit").write_text(FRESH, encoding="utf-8")
    assert _verdicts(wt, check_only=False)[entry.ENTRY.id] == "removed"
    assert not (common / "hooks" / "post-commit").exists()


def test_older_body_classifier():
    assert entry._identify_whole_body(OLDER) is not None
    assert entry._identify_whole_body('#!/bin/sh\nexec bash "$HOME/bin/my-hook"\n') is None
