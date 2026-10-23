"""bump_foreign_repo_write under `coordinator.feature.out_of_repo_write_block`:
a marker and a lowered level open nothing, sanctioned destinations and the
engine routes (cross-repo-memo, Group EM scoped commit) still pass, and the
flag off keeps consumer behaviour. Everything lives under tmp_path.
"""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards._write_bump_marker import marker_basename, resolve_gitdir
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _posix,
    _reset_git_root_cache,  # noqa: F401 -- autouse fixture reused
    _set_anchor,
    repos,  # noqa: F401 -- reused fixture
)
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture
)
from coordinator_core.group_em import nomination
from coordinator_core.session import machinery_paths
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FLAG_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK"
SID = "sess-c7-hard"


def _flag(monkeypatch, value: str) -> None:
    monkeypatch.setenv(FLAG_ENV, value)
    machine_profile.reset_cache()


def _level_warn(monkeypatch) -> None:
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
    machine_profile.reset_cache()


def _marker(repos, sid: str = SID) -> None:  # noqa: F811
    (resolve_gitdir(str(repos["foreign"])) / marker_basename(sid)).touch()


def _run(repos, cmd, sid: str = SID, payload=None):  # noqa: F811
    return _enveloped(guard.check_bump_foreign_repo_write)(cmd, sid, str(repos["anchor"]), payload or {})


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _armed(monkeypatch, repos):  # noqa: F811
    _set_anchor(monkeypatch, repos, SID)
    _flag(monkeypatch, "on")
    _level_warn(monkeypatch)
    _marker(repos)


@pytest.mark.parametrize(
    "template",
    [
        "cp {src} {dst}",
        "echo hi > {dst}",
        "python - <<'PY'\nopen({dst!r}, 'w').write('x')\nPY",
    ],
)
def test_peer_repo_write_denied_despite_marker_and_warn(repos, monkeypatch, template):  # noqa: F811
    _armed(monkeypatch, repos)
    cmd = template.format(src=_posix(repos["anchor"] / "README.md"), dst=_posix(repos["foreign"] / "out.txt"))

    result = _run(repos, cmd)

    assert result is not None
    reason = _reason(result)
    assert "out-of-repo writes are blocked" in reason
    flat = reason.replace("\\", "/")
    assert _posix(repos["anchor"]) in flat and "/scratch/" in flat
    assert "touch" not in reason
    assert "cross-repo-memo" in reason


def test_read_only_peer_gitdir_is_still_denied(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    monkeypatch.setattr(guard, "marker_gitdir_is_writable", lambda _g: False)

    result = _run(repos, f"echo hi > {_posix(repos['foreign'] / 'out.txt')}")

    assert result is not None
    assert "out-of-repo writes are blocked" in _reason(result)


def test_read_only_peer_gitdir_passes_with_flag_off(repos, monkeypatch):  # noqa: F811
    _set_anchor(monkeypatch, repos, SID)
    _flag(monkeypatch, "off")
    monkeypatch.setattr(guard, "marker_gitdir_is_writable", lambda _g: False)

    assert _run(repos, f"echo hi > {_posix(repos['foreign'] / 'out.txt')}") is None


def test_subagent_is_denied_with_its_share_dir_named(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    cmd = f"echo hi > {_posix(repos['foreign'] / 'out.txt')}"

    result = _run(repos, cmd, payload={"agent_id": "a1b2c3d4e5f6a7b8"})

    assert result is not None
    reason = _reason(result)
    share = machinery_paths.share_dir(guard.resolve_git_root(str(repos["anchor"])), SID)
    assert share.replace("\\", "/") in reason.replace("\\", "/")
    assert "dispatching EM" in reason


def test_cross_repo_memo_invocation_passes(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)

    assert _run(repos, f"cross-repo-memo send x > {_posix(repos['foreign'] / 'out.txt')}") is None


def test_group_em_scoped_commit_passes(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_GEM_CROSS_REPO_COMMIT", "on")
    machine_profile.reset_cache()
    monkeypatch.setattr(
        nomination, "identify", lambda root=None: {"state": "live", "holder": {"session_id": SID}}
    )

    cmd = f"git -C {_posix(repos['foreign'])} commit -m x -- f.txt"

    assert _run(repos, cmd) is None


def test_sanctioned_destination_inside_a_peer_repo_passes(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    target = repos["foreign"] / "state" / "lessons-outbox" / "l.yaml"

    assert _run(repos, f"echo hi > {_posix(target)}") is None


def test_flag_off_with_marker_passes(repos, monkeypatch):  # noqa: F811
    _set_anchor(monkeypatch, repos, SID)
    _flag(monkeypatch, "off")
    _marker(repos)

    assert _run(repos, f"echo hi > {_posix(repos['foreign'] / 'out.txt')}") is None


def test_own_repo_write_passes_under_flag(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)

    assert _run(repos, f"echo hi > {_posix(repos['anchor'] / 'own.txt')}") is None


def _mirror(monkeypatch, session_em: str) -> None:
    """Make `foreign` the publish mirror owned by `owner-em`; the session is `session_em`."""
    from coordinator_core import machine_resolver
    from coordinator_core.ops.fleet import _memo_resolver

    monkeypatch.setattr(guard, "target_is_publish_destination", lambda *_a, **_k: True)
    monkeypatch.setattr(guard, "publish_destination_owner", lambda *_a, **_k: "owner-em")
    monkeypatch.setattr(_memo_resolver, "read_registry_repos", lambda: {})
    monkeypatch.setattr(machine_resolver, "em_id_for_root", lambda _r, _p: session_em)


def _publish_flow(repos) -> str:  # noqa: F811
    m = _posix(repos["foreign"])
    return (
        f"git -C {m} pull -q --no-edit origin candidate 2>&1 | tail -2; "
        f"git -C {m} push origin candidate 2>&1 | tail -1; "
        f"git -C {m} merge --ff-only origin/candidate; "
        f"git -C {m} log -1 --format=%h; git -C {m} status -sb | head -1"
    )


def test_owner_publish_flow_against_its_mirror_is_admitted(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    _mirror(monkeypatch, "owner-em")

    assert _run(repos, _publish_flow(repos)) is None


def test_owner_hand_edit_in_its_mirror_stays_denied(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    _mirror(monkeypatch, "owner-em")

    result = _run(repos, f"echo x > {_posix(repos['foreign'] / 'foo.py')}")

    assert result is not None
    assert "out-of-repo writes are blocked" in _reason(result)


@pytest.mark.parametrize("verb", ["push origin candidate", "pull origin candidate", "merge origin/candidate"])
def test_non_owner_mirror_sync_and_owner_non_ff_merge_are_denied(repos, monkeypatch, verb):  # noqa: F811
    _armed(monkeypatch, repos)
    _mirror(monkeypatch, "someone-else-em" if not verb.startswith("merge") else "owner-em")

    result = _run(repos, f"git -C {_posix(repos['foreign'])} {verb}")

    assert result is not None
    assert "out-of-repo writes are blocked" in _reason(result)


def test_mirror_reads_are_admitted_for_a_non_owner(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)
    _mirror(monkeypatch, "someone-else-em")
    m = _posix(repos["foreign"])

    assert _run(repos, f"git -C {m} log -1; git -C {m} status -sb") is None


def test_owner_sync_verb_is_not_exempt_for_a_non_mirror_peer(repos, monkeypatch):  # noqa: F811
    _armed(monkeypatch, repos)

    result = _run(repos, f"git -C {_posix(repos['foreign'])} push origin x")

    assert result is not None


def test_flag_off_owner_sync_keeps_the_marker_message(repos, monkeypatch):  # noqa: F811
    _set_anchor(monkeypatch, repos, SID)
    _flag(monkeypatch, "off")
    _mirror(monkeypatch, "owner-em")

    result = _run(repos, f"git -C {_posix(repos['foreign'])} push origin candidate")

    assert result is not None
    assert "out-of-repo writes are blocked" not in _reason(result)


def test_flag_off_message_is_the_marker_message(repos, monkeypatch):  # noqa: F811
    _set_anchor(monkeypatch, repos, SID)
    _flag(monkeypatch, "off")

    result = _run(repos, f"echo hi > {_posix(repos['foreign'] / 'out.txt')}")

    assert result is not None
    assert "out-of-repo writes are blocked" not in _reason(result)
