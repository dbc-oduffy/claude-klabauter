"""Pinned guard fates from the 2026-10-28 denial census: each shape is guided or allowed, and the
kept blocks still refuse and name a route that does not itself refuse.

Registered guards (``deny_registry``) deny only their degrading shape; every other guard resolves
to a log-only advisory through ``machine_profile.apply_guard_level``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import deny_registry, machine_profile
from coordinator_core.bash_guards import bump_foreign_repo_write as bump
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _posix,
    _repo_template,  # noqa: F401 -- module-scoped autouse fixture `repos` copies from
    _reset_git_root_cache,  # noqa: F401 -- autouse fixture reused
    _set_anchor,
    repos,  # noqa: F401 -- reused fixture
)
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture
)
from coordinator_core.testing.home_sandbox import sandbox_home

pytestmark = [pytest.mark.complete]

SUB_ID = "a1b2c3d4e5f6a7b8"


@pytest.fixture(autouse=True)
def _resolvable_home(_clean_bump_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`_clean_bump_env` deletes HOME and USERPROFILE; the dispatched guards resolve the plugin root from the home dir and crash without one."""
    sandbox_home(monkeypatch, tmp_path / "sandbox-home")


def _denies(result) -> bool:
    items = result if isinstance(result, list) else [result]
    return any(
        isinstance(r, dict) and r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        for r in items
    )


# --- block_subagent_destructive_action: regenerable build/cache dirs are guide-only ---------------


# --- guard_em_post_fire_shell_write: never denies the EM's own act ---------------------------------


def test_post_engine_ask_lock_is_unregistered_so_it_never_denies() -> None:
    assert deny_registry.guard_deny_class("block-em-post-fire-product-write") is None
    assert deny_registry.guard_deny_class("guard-em-post-fire-shell-write") is None


# --- block_subagent_destructive_action: the clone remedy must not refuse itself -------------------


# --- bump_foreign_repo_write: a ff-only mirror catch-up passes, a non-ff write still routes -------

SID = "sess-fates-bump"


def _bump_armed(monkeypatch, repos) -> None:  # noqa: F811
    from coordinator_core import machine_resolver
    from coordinator_core.ops.fleet import _memo_resolver

    _set_anchor(monkeypatch, repos, SID)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK", "on")
    machine_profile.reset_cache()
    monkeypatch.setattr(bump, "target_is_publish_destination", lambda *_a, **_k: True)
    monkeypatch.setattr(bump, "publish_destination_owner", lambda *_a, **_k: "owner-em")
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: {})
    monkeypatch.setattr(machine_resolver, "em_id_for_root", lambda _r, _p: "someone-else-em")


def _bump(repos, cmd):  # noqa: F811
    return bump.check_bump_foreign_repo_write(cmd, SID, str(repos["anchor"]), {})


@pytest.mark.parametrize(
    "verb",
    ["pull --ff-only origin candidate", "pull --ff-only", "fetch origin candidate", "fetch --all"],
)
def test_ff_only_mirror_catch_up_is_not_routed(repos, monkeypatch: pytest.MonkeyPatch, verb) -> None:  # noqa: F811
    _bump_armed(monkeypatch, repos)
    assert _bump(repos, f"git -C {_posix(repos['foreign'])} {verb}") is None


@pytest.mark.parametrize(
    "verb",
    [
        "pull origin candidate",
        "pull --ff-only /elsewhere/other.git candidate",
        "push origin candidate",
        "merge origin/candidate",
        "commit -m x",
    ],
)
def test_non_ff_mirror_write_still_routes(repos, monkeypatch: pytest.MonkeyPatch, verb) -> None:  # noqa: F811
    _bump_armed(monkeypatch, repos)
    assert _bump(repos, f"git -C {_posix(repos['foreign'])} {verb}") is not None


def test_ff_only_pull_of_a_non_mirror_peer_still_routes(repos, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _bump_armed(monkeypatch, repos)
    monkeypatch.setattr(bump, "target_is_publish_destination", lambda *_a, **_k: False)
    assert _bump(repos, f"git -C {_posix(repos['foreign'])} pull --ff-only origin candidate") is not None


def test_bump_guard_never_denies_at_any_level() -> None:
    assert deny_registry.guard_deny_class("bump-foreign-repo-write") is None
    envelope = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}
    assert not _denies(machine_profile.apply_guard_level("bump-foreign-repo-write", envelope))


# --- remote delete, variable-held pytest paths: shapes, not irreversible acts ----------------------


# --- destructive-git-orphan: an index-only unstage is not a subshell-resolved ref -----------------
