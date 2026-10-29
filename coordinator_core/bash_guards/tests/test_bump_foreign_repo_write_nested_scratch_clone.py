"""bump_foreign_repo_write treats the repo enclosing a session's scratch clone as
the session's own: a session anchored in <repo>/scratch/<clone> may write <repo>,
while a repo nested elsewhere, or an unrelated repo, stays foreign.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _git,
    _init_repo,
    _posix,
    _reset_git_root_cache,  # noqa: F401 -- autouse fixture reused
    _set_anchor,
    repos,  # noqa: F401 -- reused fixture
)
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture
)
from coordinator_core.guard_messages.testing import envelope_of

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "sess-nested-scratch-clone"


def _clone_into(parent, name, source):
    parent.mkdir(parents=True, exist_ok=True)
    clone = parent / name
    _git(str(parent), "clone", "-q", str(source), str(clone))
    return clone


def _run(anchor_repos, cmd):
    return envelope_of(guard.check_bump_foreign_repo_write(cmd, SID, str(anchor_repos["anchor"]), {}))


def _anchored_in(monkeypatch, repos, clone):  # noqa: F811
    nested = dict(repos, anchor=clone)
    _set_anchor(monkeypatch, nested, SID)
    return nested


def test_scratch_clone_session_may_commit_in_enclosing_repo(repos, monkeypatch):  # noqa: F811
    clone = _clone_into(repos["foreign"] / "scratch", "merge-clone", repos["foreign"])
    nested = _anchored_in(monkeypatch, repos, clone)

    assert _run(nested, f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x") is None
    assert _run(nested, f"cd {_posix(repos['foreign'])} && git add a.txt") is None


def test_clone_nested_outside_scratch_stays_foreign(repos, monkeypatch):  # noqa: F811
    clone = _clone_into(repos["foreign"] / "vendor", "other-clone", repos["foreign"])
    nested = _anchored_in(monkeypatch, repos, clone)

    result = _run(nested, f"git -C {_posix(repos['foreign'])} tag t1")

    assert result is not None


def test_unrelated_repo_stays_foreign_for_scratch_clone_session(repos, monkeypatch, tmp_path):  # noqa: F811
    clone = _clone_into(repos["foreign"] / "scratch", "merge-clone", repos["foreign"])
    nested = _anchored_in(monkeypatch, repos, clone)
    other = _init_repo(tmp_path, "other")

    result = _run(nested, f"git -C {_posix(other)} tag t1")

    assert result is not None


def test_cross_repo_commit_is_allowed_with_a_warning(repos, monkeypatch, capsys):  # noqa: F811
    _set_anchor(monkeypatch, repos, SID)
    foreign = _posix(repos["foreign"])

    assert _run(repos, f"git -C {foreign} commit --allow-empty -m x") is None
    warned = capsys.readouterr().err
    assert "advisory" in warned and "foreign" in warned

    assert _run(repos, f"git -C {foreign} add -A") is None
    assert "advisory" in capsys.readouterr().err

    assert _run(repos, f"git -C {foreign} tag t1") is not None


def test_anchor_repo_beside_scratch_is_not_enclosed(repos, monkeypatch):  # noqa: F811
    assert guard._anchor_is_scratch_clone_of(str(repos["anchor"]), str(repos["foreign"])) is False
    assert guard._anchor_is_scratch_clone_of(None, str(repos["foreign"])) is False
    assert guard._anchor_is_scratch_clone_of(str(repos["foreign"] / "scratch2" / "c"), str(repos["foreign"])) is False
    assert guard._anchor_is_scratch_clone_of(str(repos["foreign"] / "scratch" / "c"), str(repos["foreign"])) is True
