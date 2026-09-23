"""coordinator_core/hooks/tests/test_repin_cloud_engine_root.py — tests for
`coordinator_core.hooks.repin_cloud_engine_root`, the SessionStart op that
re-points `/root/engine-current` onto a fresher stamped per-session
checkout. Spec backlink: claude-klabauter#67 (comments 5785027514, 5785078234).

Every test injects `link_path`/`frozen_root`/`search_parent`/`env` — none
touch `/root` or `/home/user`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from coordinator_core.hooks import repin_cloud_engine_root as mod
from coordinator_core.ipc import _REGISTRY


def _write_stamp(root: Path, sha: str = "sha:abc123", mtime: "float | None" = None) -> Path:
    stamp = root / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(sha, encoding="utf-8")
    if mtime is not None:
        os.utime(stamp, (mtime, mtime))
    return stamp


def test_op_registered():
    assert "hooks.repin_cloud_engine_root" in _REGISTRY


def test_handler_returns_no_advisory():
    result = mod._handler({})
    assert result == {"hookSpecificOutput": {"hookEventName": "SessionStart"}} or result.get(
        "hookSpecificOutput", {}
    ).get("additionalContext") is None


def test_inert_when_remote_env_unset(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    _write_stamp(frozen)
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout)

    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env={}
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_inert_when_remote_env_not_true(tmp_path):
    link = tmp_path / "engine-current"
    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=tmp_path / "klabauter",
        search_parent=tmp_path / "home_user",
        env={"CLAUDE_CODE_REMOTE": "1"},
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_repoints_when_checkout_is_fresher(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    now = time.time()
    _write_stamp(frozen, mtime=now - 100)
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, mtime=now)

    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=frozen,
        search_parent=parent,
        env={"CLAUDE_CODE_REMOTE": "true"},
    )
    assert verdict["repinned"] is True
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(checkout)


def test_case_insensitive_checkout_match(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    now = time.time()
    _write_stamp(frozen, mtime=now - 100)
    checkout = parent / "Claude-Klabauter"
    _write_stamp(checkout, mtime=now)

    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=frozen,
        search_parent=parent,
        env={"CLAUDE_CODE_REMOTE": "true"},
    )
    assert verdict["repinned"] is True


def test_does_not_repoint_to_older_checkout(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    now = time.time()
    _write_stamp(frozen, mtime=now)
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, mtime=now - 100)

    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=frozen,
        search_parent=parent,
        env={"CLAUDE_CODE_REMOTE": "true"},
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_does_not_repoint_to_unstamped_checkout(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    _write_stamp(frozen)
    checkout = parent / "claude-klabauter"
    checkout.mkdir(parents=True)
    # No _engine_stamp written under checkout.

    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=frozen,
        search_parent=parent,
        env={"CLAUDE_CODE_REMOTE": "true"},
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_atomic_replace_leaves_valid_link(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    now = time.time()
    _write_stamp(frozen, mtime=now - 100)
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, mtime=now)

    # Pre-existing link pointed elsewhere.
    old_target = tmp_path / "old-target"
    old_target.mkdir()
    link.symlink_to(old_target)

    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=frozen,
        search_parent=parent,
        env={"CLAUDE_CODE_REMOTE": "true"},
    )
    assert verdict["repinned"] is True
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(checkout)
    # No stray temp files left beside the link.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("engine-current.") and p.name.endswith(".tmp")]
    assert leftovers == []


def test_no_frozen_stamp_leaves_link_untouched(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"  # no stamp written
    parent = tmp_path / "home_user"
    parent.mkdir()
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout)

    verdict = mod.repin_cloud_engine_root(
        link_path=link,
        frozen_root=frozen,
        search_parent=parent,
        env={"CLAUDE_CODE_REMOTE": "true"},
    )
    assert verdict["repinned"] is False
    assert not link.exists()
