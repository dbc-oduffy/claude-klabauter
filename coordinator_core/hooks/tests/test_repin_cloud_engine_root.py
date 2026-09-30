
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coordinator_core.hooks import repin_cloud_engine_root as mod
from coordinator_core.ipc import _REGISTRY


_T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _write_stamp(
    root: Path, sha: str = "sha:abc123", published_at: "datetime | str | None" = _T0
) -> Path:
    stamp = root / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(sha, encoding="utf-8")
    if published_at is not None:
        text = published_at if isinstance(published_at, str) else published_at.isoformat()
        (stamp.parent / "_engine_published_at").write_text(text + "\n", encoding="utf-8")
    return stamp


def test_op_registered():
    assert "hooks.session_start_repin_cloud_engine_root" in _REGISTRY


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
    now = _T0 + timedelta(seconds=100)
    _write_stamp(frozen, published_at=now - timedelta(seconds=100))
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, published_at=now)

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
    now = _T0 + timedelta(seconds=100)
    _write_stamp(frozen, published_at=now - timedelta(seconds=100))
    checkout = parent / "Claude-Klabauter"
    _write_stamp(checkout, published_at=now)

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
    now = _T0 + timedelta(seconds=100)
    _write_stamp(frozen, published_at=now)
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, published_at=now - timedelta(seconds=100))

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
    now = _T0 + timedelta(seconds=100)
    _write_stamp(frozen, published_at=now - timedelta(seconds=100))
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, published_at=now)

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
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("engine-current.") and p.name.endswith(".tmp")]
    assert leftovers == []


def test_no_frozen_stamp_leaves_link_untouched(tmp_path):
    link = tmp_path / "engine-current"
    frozen = tmp_path / "klabauter"
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


_REMOTE = {"CLAUDE_CODE_REMOTE": "true"}


def _setup(tmp_path, *, frozen_at, checkout_at):
    frozen = tmp_path / "klabauter"
    parent = tmp_path / "home_user"
    parent.mkdir()
    _write_stamp(frozen, published_at=frozen_at)
    checkout = parent / "claude-klabauter"
    _write_stamp(checkout, published_at=checkout_at)
    return tmp_path / "engine-current", frozen, parent


def test_mtime_does_not_order_builds(tmp_path):
    """Checkout-time mtime (newer) on an older published build must not repoint."""
    link, frozen, parent = _setup(
        tmp_path, frozen_at=_T0, checkout_at=_T0 - timedelta(days=3)
    )
    now = _T0.timestamp() + 10_000
    os.utime(parent / "claude-klabauter" / "coordinator_core" / "_engine_stamp", (now, now))
    os.utime(frozen / "coordinator_core" / "_engine_stamp", (now - 9_000, now - 9_000))

    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env=_REMOTE
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_equal_publish_instant_repoints(tmp_path):
    link, frozen, parent = _setup(tmp_path, frozen_at=_T0, checkout_at=_T0)
    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env=_REMOTE
    )
    assert verdict["repinned"] is True


def test_mixed_offsets_and_naive_compare_as_instants(tmp_path):
    # 14:00+02:00 == 12:00Z (frozen); naive checkout 12:00 is read as UTC.
    link, frozen, parent = _setup(
        tmp_path, frozen_at="2026-09-29T14:00:00+02:00", checkout_at="2026-09-29T12:00:00"
    )
    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env=_REMOTE
    )
    assert verdict["repinned"] is True


def test_checkout_without_publish_instant_is_unknown_order(tmp_path):
    link, frozen, parent = _setup(tmp_path, frozen_at=_T0, checkout_at=None)
    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env=_REMOTE
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_frozen_without_publish_instant_is_unknown_order(tmp_path):
    link, frozen, parent = _setup(tmp_path, frozen_at=None, checkout_at=_T0)
    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env=_REMOTE
    )
    assert verdict["repinned"] is False
    assert not link.exists()


def test_unparsable_publish_instant_is_unknown_order(tmp_path):
    link, frozen, parent = _setup(tmp_path, frozen_at=_T0, checkout_at="not-a-date")
    verdict = mod.repin_cloud_engine_root(
        link_path=link, frozen_root=frozen, search_parent=parent, env=_REMOTE
    )
    assert verdict["repinned"] is False
