"""bump_foreign_repo_write lets the publisher write into the tree its own
publish-targets row names: `git apply` and the engine stamp pass there, and
every other writer, repo and tree stays refused. Everything lives under tmp_path.
"""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
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

SID = "sess-publisher-own-tree"
FLAG_ENV = "MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK"


def _row(name: str, dest_sigil: str, source: str = "dist/klabauter-toplevel") -> str:
    return f"{name}|flat-mirror|{dest_sigil}|{source}|"


def _setup(monkeypatch, tmp_path, repos, *, rows, mirror_root=None, portable=True):  # noqa: F811
    """Anchor repo carries the portable rows; the registry maps `claude_klabauter` to `mirror_root`."""
    _set_anchor(monkeypatch, repos, SID)
    monkeypatch.setenv(FLAG_ENV, "on")
    machine_profile.reset_cache()
    if portable:
        setup_dir = repos["anchor"] / "setup"
        setup_dir.mkdir(exist_ok=True)
        (setup_dir / "publish-targets.portable").write_text("# rows\n" + "\n".join(rows) + "\n", encoding="utf-8")
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    target = mirror_root if mirror_root is not None else repos["foreign"]
    (reg_dir / "registry.local.toml").write_text(
        "[publish.mirrors.claude_klabauter]\npath = '%s'\n" % _posix(target), encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))


def _run(repos, cmd):  # noqa: F811
    return envelope_of(guard.check_bump_foreign_repo_write(cmd, SID, str(repos["anchor"]), {}))


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


OWN_ROW = [_row("klabauter-top", "publish-mirror:claude_klabauter")]


def test_git_apply_into_own_row_tree_is_allowed(repos, monkeypatch, tmp_path):  # noqa: F811
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW)

    assert _run(repos, f"git -C {_posix(repos['foreign'])} apply patch.diff") is None


def test_engine_stamp_write_into_own_row_tree_is_allowed(repos, monkeypatch, tmp_path):  # noqa: F811
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW)
    (repos["foreign"] / "coordinator_core").mkdir()
    stamp = _posix(repos["foreign"] / "coordinator_core" / "_engine_stamp")

    assert _run(repos, f"echo abc123 > {stamp}") is None


@pytest.mark.parametrize(
    "template",
    [
        "git -C {m} commit --allow-empty -m x",
        "git -C {m} checkout -- .",
        "echo x > {m}/foo.py",
        "cp {m}/a {m}/coordinator_core/other.py",
    ],
)
def test_same_tree_from_a_non_publisher_command_is_denied(repos, monkeypatch, tmp_path, template):  # noqa: F811
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW)
    (repos["foreign"] / "coordinator_core").mkdir()

    result = _run(repos, template.format(m=_posix(repos["foreign"])))

    assert result is not None
    assert "out-of-repo writes are blocked" in _reason(result)


@pytest.mark.parametrize(
    "template",
    ["git -C {o} apply patch.diff", "echo abc > {o}/coordinator_core/_engine_stamp"],
)
def test_another_foreign_repo_is_denied(repos, monkeypatch, tmp_path, template):  # noqa: F811
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW)
    other = _init_repo(tmp_path, "other")
    (other / "coordinator_core").mkdir()

    result = _run(repos, template.format(o=_posix(other)))

    assert result is not None
    assert "out-of-repo writes are blocked" in _reason(result)


@pytest.mark.parametrize(
    "template",
    ["git -C {m} apply patch.diff", "echo abc > {m}/coordinator_core/_engine_stamp"],
)
def test_tree_that_is_no_rows_destination_is_denied(repos, monkeypatch, tmp_path, template):  # noqa: F811
    other = _init_repo(tmp_path, "elsewhere")
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW, mirror_root=other)
    (repos["foreign"] / "coordinator_core").mkdir()

    result = _run(repos, template.format(m=_posix(repos["foreign"])))

    assert result is not None
    assert "out-of-repo writes are blocked" in _reason(result)


def test_row_whose_source_is_another_repo_is_denied(repos, monkeypatch, tmp_path):  # noqa: F811
    rows = [_row("klabauter-top", "publish-mirror:claude_klabauter", source="plugin-source:other/bin")]
    _setup(monkeypatch, tmp_path, repos, rows=rows)

    result = _run(repos, f"git -C {_posix(repos['foreign'])} apply patch.diff")

    assert result is not None


def test_session_without_a_portable_file_is_denied(repos, monkeypatch, tmp_path):  # noqa: F811
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW, portable=False)

    result = _run(repos, f"git -C {_posix(repos['foreign'])} apply patch.diff")

    assert result is not None


def test_deny_remedy_does_not_end_in_ask_the_operator(repos, monkeypatch, tmp_path):  # noqa: F811
    _setup(monkeypatch, tmp_path, repos, rows=OWN_ROW)

    result = _run(repos, f"echo x > {_posix(repos['foreign'])}/foo.py")

    assert result is not None
    assert not _reason(result).rstrip(" .\n").lower().endswith("ask the operator")
