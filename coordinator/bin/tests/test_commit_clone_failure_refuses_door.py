"""A commit clone that cannot be materialised refuses each publisher door with a named error and a
runnable remediation; no live-tree publisher code runs. Drives the real door files in subprocesses
against tmp_path trees, injecting the failure inside `commit_clone`."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

from ._git_template import copy_repo, template_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_BIN = Path(__file__).resolve().parents[1]
_SHA = "a" * 40

_STUB_COMMIT_CLONE = textwrap.dedent(
    """
    from pathlib import Path

    class CommitCloneError(RuntimeError):
        pass

    def is_commit_clone(root):
        return False

    def default_cache_root():
        return Path("unused")

    def materialise_commit_clone(repo_root, sha, cache_root):
        raise CommitCloneError("injected materialise failure")
    """
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags(),
    )


_SENTINEL_PUBLISH = (
    "from pathlib import Path\n"
    "Path(__file__).resolve().parents[{up}].joinpath({name!r}).write_text('ran')\n"
)
_CLONE_STUB = _STUB_COMMIT_CLONE.replace(
    'raise CommitCloneError("injected materialise failure")',
    'return Path(__file__).resolve().parents[4] / "clone"',
)


def _build_door_tree(root: Path, *, commit: bool, clone_stub: bool = False) -> None:
    bin_dir = root / "coordinator" / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    (bin_dir / "coordinator-publish.py").write_text(
        (_BIN / "coordinator-publish.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (bin_dir / "lib" / "commit_clone.py").write_text(_STUB_COMMIT_CLONE, encoding="utf-8")
    (bin_dir / "publish.py").write_text(
        _SENTINEL_PUBLISH.format(up=2, name="LIVE_PUBLISH_RAN"), encoding="utf-8"
    )
    _git(root, "init", "-q")
    if commit:
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "seed")
    if clone_stub:
        (bin_dir / "lib" / "commit_clone.py").write_text(_CLONE_STUB, encoding="utf-8")


def _door_tree(tmp_path_factory, root: Path, *, commit: bool) -> tuple[Path, Path]:
    """A copy of the session door tree: real coordinator-publish.py, a failing commit_clone, a sentinel publish.py."""
    key = "door-refusal:commit" if commit else "door-refusal:nocommit"
    template = template_repo(tmp_path_factory, key, lambda p: _build_door_tree(p, commit=commit))
    copy_repo(template, root)
    return root / "coordinator" / "bin" / "coordinator-publish.py", root / "LIVE_PUBLISH_RAN"


def _run_door(door: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(door), *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
        **no_console_creationflags(),
    )


def test_coordinator_publish_refuses_when_materialise_fails(tmp_path: Path, tmp_path_factory) -> None:
    door, sentinel = _door_tree(tmp_path_factory, tmp_path / "tree", commit=True)
    proc = _run_door(door, door.parents[2])
    assert proc.returncode != 0, proc.stdout
    assert "coordinator-publish: refused -- commit clone failed:" in proc.stderr
    assert "injected materialise failure" in proc.stderr
    assert "commit_clone.py" in proc.stderr and "materialise" in proc.stderr
    assert "running this tree" not in proc.stderr
    assert not sentinel.exists()


def test_coordinator_publish_refuses_when_the_round_commit_does_not_resolve(
    tmp_path: Path, tmp_path_factory
) -> None:
    door, sentinel = _door_tree(tmp_path_factory, tmp_path / "tree", commit=False)
    proc = _run_door(door, door.parents[2])
    assert proc.returncode != 0, proc.stdout
    assert "coordinator-publish: refused -- commit clone failed:" in proc.stderr
    assert "does not resolve to a commit" in proc.stderr
    assert "commit_clone.py" in proc.stderr
    assert not sentinel.exists()


def test_coordinator_publish_refuses_an_unresolvable_explicit_commit(tmp_path: Path, tmp_path_factory) -> None:
    door, sentinel = _door_tree(tmp_path_factory, tmp_path / "tree", commit=True)
    proc = _run_door(door, door.parents[2], "klabauter", "--commit", _SHA)
    assert proc.returncode != 0, proc.stdout
    assert "coordinator-publish: refused -- commit clone failed:" in proc.stderr
    assert not sentinel.exists()


def _build_bare(path: Path) -> None:
    _git(path, "init", "-q", "--bare", "origin.git")


def _clone_door_tree(tmp_path_factory, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A door tree whose materialise succeeds into a sentinel-writing clone, with a bare origin."""
    template = template_repo(
        tmp_path_factory, "door-refusal:clone-stub",
        lambda p: _build_door_tree(p, commit=True, clone_stub=True),
    )
    root = copy_repo(template, tmp_path / "tree")
    clone_bin = tmp_path / "clone" / "coordinator" / "bin"
    clone_bin.mkdir(parents=True)
    (clone_bin / "publish.py").write_text(
        _SENTINEL_PUBLISH.format(up=3, name="CLONE_PUBLISH_RAN"), encoding="utf-8"
    )
    bare_tpl = template_repo(tmp_path_factory, "door-refusal:bare-origin", _build_bare)
    bare = copy_repo(bare_tpl, tmp_path) / "origin.git"
    with (root / ".git" / "config").open("a", encoding="utf-8") as cfg:
        cfg.write(f'[remote "origin"]\n\turl = {bare.as_posix()}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    return root / "coordinator" / "bin" / "coordinator-publish.py", bare, tmp_path / "CLONE_PUBLISH_RAN"


def _bare_refs(bare: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"], capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout


def test_the_door_hands_off_without_pushing_the_source_commit(tmp_path: Path, tmp_path_factory) -> None:
    """The source-commit push lives in publish.py (test_publish_pushes_source_commit_ref); the door only hands off."""
    door, bare, ran = _clone_door_tree(tmp_path_factory, tmp_path)
    proc = _run_door(door, door.parents[2], "klabauter", "--allow-source-rollback")
    assert proc.returncode == 0, proc.stderr
    assert "refs/heads/publish/" not in _bare_refs(bare)
    assert ran.exists()


_LOAD = textwrap.dedent(
    """
    import importlib.util, sys
    from pathlib import Path
    bin_dir = Path(sys.argv[1])
    sys.path[:0] = [str(bin_dir), str(bin_dir.parents[1])]
    spec = importlib.util.spec_from_file_location("door_under_test", bin_dir / sys.argv[2])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["door_under_test"] = mod
    spec.loader.exec_module(mod)
    cc = mod._commit_clone_module()

    def _fail(*a, **k):
        raise RuntimeError("injected seed failure")

    def _ran(*a, **k):
        raise AssertionError("live-tree publisher code ran after a clone failure")

    cc._seed_entries = _fail
    cc.run_entry_from_clone = _ran
    mod._bootstrap_engine = _ran
    """
)

_PUBLISH_DOOR = _LOAD + textwrap.dedent(
    """
    mod._resolve_round_commit = lambda root, ref: "a" * 40
    mod._dirty_publisher_paths = lambda root, sha: ["coordinator/bin/publish.py"]
    mod._BOOTSTRAP_DONE = False
    sys.exit(mod._main(["--dry-run"]))
    """
)

_PERCOLATE_DOOR = _LOAD + textwrap.dedent(
    """
    mod._resolve_clone_sha = lambda root, ref: "a" * 40
    sys.exit(mod.main(["--no-publish", "somewhere"]))
    """
)


@pytest.mark.parametrize(
    ("entry", "driver", "refusal"),
    [
        ("publish.py", _PUBLISH_DOOR, "publish: refused --"),
        ("percolate-round.py", _PERCOLATE_DOOR, "percolate-round: refused, nothing staged: commit clone failed:"),
    ],
)
def test_real_doors_refuse_and_name_the_remediation_script(
    tmp_path: Path, entry: str, driver: str, refusal: str
) -> None:
    script = tmp_path / "drive_door.py"
    script.write_text(driver, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), str(_BIN), entry], capture_output=True, text=True, timeout=120,
        **no_console_creationflags(),
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert refusal in proc.stderr, proc.stderr
    assert "injected seed failure" in proc.stderr
    assert "commit_clone.py" in proc.stderr and "materialise" in proc.stderr
    assert "ran after a clone failure" not in proc.stderr
