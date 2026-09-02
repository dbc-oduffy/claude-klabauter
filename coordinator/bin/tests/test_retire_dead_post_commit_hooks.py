"""Guards for `coordinator/bin/retire-dead-post-commit-hooks.py`.

The script deletes files inside repos it does not own, so the tests that
matter are the refusals, not the happy path: an unrecognized `post-commit`
must survive byte-identical, and no removal may happen without the backup
that makes it undoable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "retire-dead-post-commit-hooks.py"


def _load():
    spec = importlib.util.spec_from_file_location("retire_dead_post_commit_hooks", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


_FRESH_BODY = """#!/bin/sh
# coordinator coordinator-auto-push hook — installed by git_hook_install.
# coordinator-hook-gen: 2
_PY="$(command -v python3)"
SCRIPT="/somewhere/coordinator/bin/coordinator-auto-push"
exec "$_PY" "$SCRIPT" "$@"
"""


def _repo(tmp_path: Path, body: str | None) -> Path:
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    if body is not None:
        (hooks / "post-commit").write_text(body, encoding="utf-8")
    return tmp_path


def test_fresh_body_is_removed_and_backed_up(tmp_path: Path) -> None:
    root = _repo(tmp_path, _FRESH_BODY)
    assert mod._retire_one(root, apply=True) == "removed"
    hooks = root / ".git" / "hooks"
    assert not (hooks / "post-commit").exists()
    assert (hooks / "post-commit.retired").read_text(encoding="utf-8") == _FRESH_BODY


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path, _FRESH_BODY)
    assert mod._retire_one(root, apply=False) == "would-remove"
    hooks = root / ".git" / "hooks"
    assert (hooks / "post-commit").read_text(encoding="utf-8") == _FRESH_BODY
    assert not (hooks / "post-commit.retired").exists()


@pytest.mark.parametrize(
    "body",
    [
        # Mentions the retired helper but is not our generated body — a
        # foreign hook is free to name it, and naming is not ownership.
        '#!/bin/sh\necho "we used to run coordinator-auto-push here"\n',
        # Our header, but the exec tail was hand-edited away: identification
        # needs both, so this is left alone rather than deleted on a comment.
        "#!/bin/sh\n# coordinator coordinator-auto-push hook — installed by git_hook_install.\n"
        'echo "operator replaced the body"\n',
    ],
)
def test_unidentified_body_survives_byte_identical(tmp_path: Path, body: str) -> None:
    root = _repo(tmp_path, body)
    assert mod._retire_one(root, apply=True) == "unidentified-left-alone"
    hook = root / ".git" / "hooks" / "post-commit"
    assert hook.read_text(encoding="utf-8") == body
    assert not hook.with_name("post-commit.retired").exists()


def test_appended_block_is_excised_and_foreign_content_kept(tmp_path: Path) -> None:
    start, end = mod.git_hook_install._append_markers(mod._RETIRED_HEADER)
    body = f'#!/bin/sh\necho "someone else owns this hook"\n{start}\n_PY=x\n{end}\necho tail\n'
    root = _repo(tmp_path, body)
    assert mod._retire_one(root, apply=True) == "block-excised"
    hook = root / ".git" / "hooks" / "post-commit"
    remaining = hook.read_text(encoding="utf-8")
    assert remaining == '#!/bin/sh\necho "someone else owns this hook"\necho tail\n'
    assert start not in remaining and end not in remaining
    assert hook.with_name("post-commit.retired").read_text(encoding="utf-8") == body


def test_absent_hook_is_reported_not_created(tmp_path: Path) -> None:
    root = _repo(tmp_path, None)
    assert mod._retire_one(root, apply=True) == "absent"
    assert not (root / ".git" / "hooks" / "post-commit").exists()


def test_non_worktree_is_skipped(tmp_path: Path) -> None:
    assert mod._retire_one(tmp_path, apply=True) == "skipped-not-a-worktree"


def test_linked_worktree_resolves_to_the_common_hooks_dir(tmp_path: Path) -> None:
    """A worktree's hooks live in the main repo's common dir; resolving to
    `<gitdir>/hooks` instead would silently miss every worktree."""
    main_git = tmp_path / "main" / ".git"
    (main_git / "hooks").mkdir(parents=True)
    wt_gitdir = main_git / "worktrees" / "wt"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n", encoding="utf-8")
    assert mod._hooks_dir(wt) == main_git / "hooks"
