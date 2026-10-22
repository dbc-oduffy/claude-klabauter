"""Tests for the repo.provision_local op; every path lives under tmp_path."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops import sibling_repo_provision as op
from coordinator_core.ops.fleet import _memo_resolver
from coordinator_core.ops.sibling_repo_provision_contract import MARKER_NAME, has_remote, read_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

NAME = "demo-vault-guide"
KEY = "repos.demo_vault_guide"
SIZING = "state/sizings/s.yaml"
ASSENT = f"{SIZING}#R1"
_IDENT = ["-c", "user.name=T", "-c", "user.email=t@t"]


def _manifest(extra: str = "", source: str = "templates/README.md") -> str:
    return (
        "schema: sibling-repo-manifest\n"
        f"name: {NAME}\n"
        "files:\n"
        "  - path: README.md\n"
        f"    source: {source}\n"
        "  - path: tests/__init__.py\n"
        '    content: ""\n'
        'commit_message: "chore: provision"\n' + extra
    )


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout


def _commit_home(home: Path) -> None:
    _run(home, "add", "-A", "-f")
    _run(home, *_IDENT, "commit", "-q", "-m", "x")


_HOME_TEMPLATE: list = []


def _build_home_template() -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="sibprov-tpl-"))
    home = root / "home"
    (home / "templates").mkdir(parents=True)
    (home / "state" / "sizings").mkdir(parents=True)
    (home / "templates" / "README.md").write_bytes(b"hello\r\nworld\n")
    (home / "m.yaml").write_text(_manifest(), encoding="utf-8")
    (home / "state" / "sizings" / "s.yaml").write_text(
        f"requirement_register:\n  rows:\n    - id: R1\n      source_text: make {NAME} locally\n",
        encoding="utf-8",
    )
    # Pinned config keeps the CRLF fixture byte-stable under a machine-level autocrlf.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q", str(home)], ["-C", str(home), "add", "-A", "-f"],
                 ["-C", str(home), *_IDENT, "commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], env=env, capture_output=True, check=True,
                       **no_console_creationflags())
    return home


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not _HOME_TEMPLATE:
        _HOME_TEMPLATE.append(_build_home_template())
    home = tmp_path / "home"
    shutil.copytree(_HOME_TEMPLATE[0], home)
    registry: dict[str, str] = {}
    monkeypatch.setattr(machine_resolver, "registry_get", registry.get)
    monkeypatch.setattr(machine_resolver, "registry_set", registry.__setitem__)
    monkeypatch.setattr(_memo_resolver, "read_receiver_aliases", lambda: {})
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    spawns: list[list[str]] = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        spawns.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(op.subprocess, "run", spy)
    return home, tmp_path / NAME, registry, spawns


def _params(**over):
    p = {"manifest": "m.yaml", "assent": ASSENT}
    p.update(over)
    return p


def test_create(env):
    home, target, registry, spawns = env
    out = op._provision_local_op(_params(), home)
    op_spawns = len(spawns)
    assert out["created"] is True and out["committed"] is True and out["adopted"] is False
    assert sorted(out["changed"]) == ["README.md", "tests/__init__.py"]
    assert (target / ".git").is_dir() and not has_remote(target)
    assert _run(target, "remote").strip() == ""
    assert (target / "README.md").read_bytes() == b"hello\r\nworld\n"
    assert read_marker(target)["name"] == NAME
    assert (target / ".git" / MARKER_NAME).is_file()
    assert registry[KEY] == target.resolve().as_posix() and out["registered_key"] == KEY
    assert out["commit_sha"] == _run(target, "rev-parse", "HEAD").strip()
    assert sorted(_run(target, "show", "--name-only", "--format=", "HEAD").split()) == [
        "README.md",
        "tests/__init__.py",
    ]
    assert _run(target, "status", "--porcelain") == ""
    assert op_spawns == 4


def test_commit_identity_fallback_and_global(env, tmp_path):
    home, target, *_ = env
    op._provision_local_op(_params(), home)
    assert _run(target, "log", "-1", "--format=%an <%ae>").strip() == "Coordinator <coordinator@coordinator>"
    (tmp_path / "gitconfig").write_text("[user]\n\tname = Pat Q\n\temail = pat@example.invalid\n")
    (home / "templates" / "README.md").write_bytes(b"v2\n")
    _commit_home(home)
    op._provision_local_op(_params(), home)
    assert _run(target, "log", "-1", "--format=%an <%ae>").strip() == "Pat Q <pat@example.invalid>"


def test_identical_rerun_is_two_spawns(env):
    home, target, _registry, spawns = env
    op._provision_local_op(_params(), home)
    spawns.clear()
    out = op._provision_local_op(_params(), home)
    assert out["changed"] == [] and out["committed"] is False and out["commit_sha"] is None
    assert out["created"] is False
    assert len(spawns) == 2
    assert _run(target, "rev-list", "--count", "HEAD").strip() == "1"


def test_update_commits_only_changed_file(env):
    home, target, *_ = env
    op._provision_local_op(_params(), home)
    (home / "templates" / "README.md").write_bytes(b"v2\n")
    _commit_home(home)
    out = op._provision_local_op(_params(), home)
    assert out["changed"] == ["README.md"] and out["committed"] is True
    assert _run(target, "show", "--name-only", "--format=", "HEAD").split() == ["README.md"]
    assert _run(target, "rev-list", "--count", "HEAD").strip() == "2"


def test_never_deletes_unnamed_files(env):
    home, target, *_ = env
    op._provision_local_op(_params(), home)
    (target / "extra.txt").write_text("keep")
    op._provision_local_op(_params(), home)
    assert (target / "extra.txt").read_text() == "keep"


def _adopt_manifest(home: Path) -> None:
    (home / "m.yaml").write_text(_manifest("adopt: true\n"), encoding="utf-8")
    _commit_home(home)


def test_adopt_unmarked_repo(env):
    home, target, registry, _ = env
    _run(target.parent, "init", "-q", str(target))
    _adopt_manifest(home)
    out = op._provision_local_op(_params(), home)
    assert out["adopted"] is True and out["created"] is False and out["committed"] is True
    assert read_marker(target)["name"] == NAME
    assert registry[KEY] == target.resolve().as_posix()
    assert op._provision_local_op(_params(), home)["adopted"] is False


def test_refuses_unmarked_repo_without_adopt(env):
    home, target, registry, _ = env
    _run(target.parent, "init", "-q", str(target))
    with pytest.raises(ValueError, match="did not provision"):
        op._provision_local_op(_params(), home)
    assert registry == {} and not (target / "README.md").exists()
    assert read_marker(target) is None


def test_refuses_remote_on_adopt_candidate(env):
    home, target, registry, _ = env
    _run(target.parent, "init", "-q", str(target))
    _run(target, "remote", "add", "origin", "https://example.invalid/x.git")
    _adopt_manifest(home)
    with pytest.raises(ValueError, match="remote"):
        op._provision_local_op(_params(), home)
    assert registry == {} and read_marker(target) is None and not (target / "README.md").exists()


def test_refuses_nonempty_plain_dir(env):
    home, target, registry, _ = env
    target.mkdir()
    (target / "f").write_text("x")
    with pytest.raises(ValueError, match="non-empty"):
        op._provision_local_op(_params(), home)
    assert registry == {} and not (target / ".git").exists()


def test_refuses_remote_added_after_provisioning(env):
    home, target, registry, _ = env
    op._provision_local_op(_params(), home)
    _run(target, "remote", "add", "origin", "https://example.invalid/x.git")
    (home / "templates" / "README.md").write_bytes(b"v2\n")
    _commit_home(home)
    with pytest.raises(ValueError, match="remote"):
        op._provision_local_op(_params(), home)
    assert (target / "README.md").read_bytes() == b"hello\r\nworld\n"


def test_refuses_uncommitted_manifest(env):
    home, target, registry, _ = env
    (home / "m.yaml").write_text(_manifest("# edit\n"), encoding="utf-8")
    with pytest.raises(ValueError, match="tracked and unmodified"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {}


def test_refuses_modified_source(env):
    home, target, registry, _ = env
    (home / "templates" / "README.md").write_bytes(b"dirty\n")
    with pytest.raises(ValueError, match="tracked and unmodified"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {}


def test_refuses_gitignored_source(env):
    home, target, registry, _ = env
    (home / ".gitignore").write_text("templates/secret.md\n")
    (home / "templates" / "secret.md").write_text("s")
    (home / "m.yaml").write_text(_manifest(source="templates/secret.md"), encoding="utf-8")
    _run(home, "add", ".gitignore", "m.yaml")
    _run(home, *_IDENT, "commit", "-q", "-m", "y")
    with pytest.raises(ValueError, match="tracked and unmodified"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {}


def test_refuses_dirty_manifest_path_in_target(env):
    home, target, *_ = env
    op._provision_local_op(_params(), home)
    (target / "README.md").write_text("local edit")
    with pytest.raises(ValueError, match="uncommitted"):
        op._provision_local_op(_params(), home)
    assert (target / "README.md").read_text() == "local edit"


def test_dirty_path_already_matching_manifest_is_recommitted(env):
    home, target, *_ = env
    op._provision_local_op(_params(), home)
    (home / "templates" / "README.md").write_bytes(b"v2\n")
    _commit_home(home)
    (target / "README.md").write_bytes(b"v2\n")
    out = op._provision_local_op(_params(), home)
    assert out["changed"] == ["README.md"] and out["committed"] is True


@pytest.mark.parametrize(
    "assent",
    [None, "", "  ", "no-hash", f"{SIZING}#R9", "state/sizings/missing.yaml#R1", "../x.yaml#R1"],
)
def test_refuses_bad_assent(env, assent):
    home, target, registry, _ = env
    with pytest.raises(ValueError, match="assent|sizing"):
        op._provision_local_op(_params(assent=assent), home)
    assert not target.exists() and registry == {}


def test_refuses_assent_row_not_naming_target(env):
    home, target, registry, _ = env
    (home / "state" / "sizings" / "s.yaml").write_text(
        "requirement_register:\n  rows:\n    - id: R1\n      source_text: something else\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not name"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {}


def test_refuses_aliased_name(env, monkeypatch):
    home, target, registry, _ = env
    monkeypatch.setattr(_memo_resolver, "read_receiver_aliases", lambda: {NAME: "other"})
    with pytest.raises(ValueError, match="alias"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {}


def test_refuses_key_bound_elsewhere(env, tmp_path):
    home, target, registry, _ = env
    registry[KEY] = str(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="already bound"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {KEY: str(tmp_path / "elsewhere")}


def test_refuses_missing_source(env):
    home, target, registry, _ = env
    (home / "templates" / "README.md").unlink()
    with pytest.raises(ValueError, match="source"):
        op._provision_local_op(_params(), home)
    assert not target.exists() and registry == {}


def test_refuses_missing_params(env):
    home, *_ = env
    with pytest.raises(ValueError, match="manifest"):
        op._provision_local_op({"assent": ASSENT}, home)
    with pytest.raises(ValueError, match="repo_root"):
        op._provision_local_op(_params(), None)
