"""percolate-round runs its code and publisher chain from a shared commit clone at the round's commit.

Driven through `main()` (default arguments) on a tmp_path repo whose publisher-chain file differs
from its HEAD; the clone cache lives under tmp_path.
"""
from __future__ import annotations

import faulthandler
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ._git_template import copy_repo, template_repo

pytestmark = pytest.mark.complete

_BIN_DIR = Path(__file__).resolve().parents[1]
_CHAIN_FILES = (
    "coordinator/bin/publish.py",
    "coordinator/bin/percolate-gate.py",
    "coordinator/bin/percolate-push.py",
    "coordinator/bin/percolate-parse-dryrun.py",
    "coordinator/bin/publish-allowlist-generate.py",
)
_ENTRY = "coordinator/bin/percolate-round.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rnd():
    return _load(_BIN_DIR / "percolate-round.py", "percolate_round_commit_clone")


@pytest.fixture(autouse=True)
def _managed_temp_under_tmp_path(tmp_path, monkeypatch):
    """The clone cache lands under the managed temp dir; keep it in tmp_path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def _build(root: Path) -> None:
    for rel in _CHAIN_FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"PINNED = {rel!r}\n", encoding="utf-8")
    (root / _ENTRY).write_bytes((_BIN_DIR / "percolate-round.py").read_bytes())
    for rel in ("coordinator/bin/lib/commit_clone.py", "coordinator_core/temp_layout.py"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes((_BIN_DIR.parents[1] / rel).read_bytes())
    for rel in ("coordinator_core/percolate/engine.py", "coordinator/lib/percolate/targets.py",
                "setup/publish-targets.portable"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"PINNED = {rel!r}\n")
    (root / "README.md").write_text("doc\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")


@pytest.fixture(scope="session")
def chain_template(tmp_path_factory):
    return template_repo(tmp_path_factory, "percolate-chain-pin:base", _build)


@pytest.fixture(scope="session")
def head_sha(chain_template):
    return _git(chain_template, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path, chain_template):
    return copy_repo(chain_template, tmp_path / "repo")


@pytest.fixture()
def delegations(rnd, repo, monkeypatch):
    """`main()` rooted at `repo`, with the clone's entry run replaced by a recorder."""
    commit_clone = rnd._commit_clone_module()
    seen: list = []

    def record(clone, entry_rel, argv):
        seen.append((Path(clone), entry_rel, list(argv)))
        return 7

    monkeypatch.setattr(rnd, "_REPO_ROOT", repo)
    monkeypatch.setattr(commit_clone, "run_entry_from_clone", record)
    monkeypatch.setattr(commit_clone, "default_cache_root", lambda: repo.parent / "cache")
    return seen


def _main(rnd, argv):
    try:
        return rnd.main(argv)
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.mark.parametrize("rel", _CHAIN_FILES)
def test_dirty_chain_file_runs_the_round_from_a_clone_at_head(rnd, repo, head_sha, delegations, rel):
    (repo / rel).write_text("PINNED = 'dirty'\n")
    sha = head_sha
    assert _main(rnd, ["alpha", "--no-publish"]) == 7
    clone, entry_rel, argv = delegations[0]
    assert clone == repo.parent / "cache" / sha
    assert entry_rel == _ENTRY and argv == ["percolate-round.py", "alpha", "--no-publish"]
    assert (clone / rel).read_text(encoding="utf-8") == f"PINNED = {rel!r}\n"
    assert _git(clone, "rev-parse", "HEAD") == sha
    assert (repo / rel).read_text() == "PINNED = 'dirty'\n"


def test_source_ref_names_the_commit_the_clone_is_cut_at(rnd, repo, head_sha, delegations):
    first = head_sha
    (repo / "README.md").write_text("later\n")
    _git(repo, "commit", "-qam", "later")
    assert _main(rnd, ["alpha", "--source-ref", first]) == 7
    assert delegations[0][0].name == first


def test_a_ref_this_repo_lacks_falls_back_to_head(rnd, repo, head_sha, delegations):
    head = head_sha
    assert _main(rnd, ["alpha", "--source-ref", "no-such-branch"]) == 7
    assert delegations[0][0].name == head


def test_clone_failure_refuses_before_any_step(rnd, repo, delegations, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rnd._commit_clone_module(), "materialise_commit_clone", boom)
    monkeypatch.setattr(rnd, "_bootstrap_engine", lambda: pytest.fail("a step ran after a refusal"))
    assert _main(rnd, ["alpha"]) == rnd._EXIT_FAIL
    err = capsys.readouterr().err
    assert "nothing staged" in err and "disk full" in err
    assert delegations == []


def test_a_materialise_failure_refuses_with_a_runnable_retry_and_runs_no_live_tree_code(
    rnd, repo, head_sha, delegations, monkeypatch, capsys
):
    commit_clone = rnd._commit_clone_module()

    def disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(commit_clone, "_write_files", disk_full)
    monkeypatch.setattr(rnd, "_bootstrap_engine", lambda: pytest.fail("live-tree code ran after a refusal"))
    monkeypatch.setattr(rnd, "_cmd_round", lambda args: pytest.fail("live-tree round ran after a refusal"))
    assert _main(rnd, ["alpha"]) == rnd._EXIT_FAIL
    err = capsys.readouterr().err
    sha = head_sha
    assert "commit clone" in err and "disk full" in err and "nothing ran from the live tree" in err
    assert f'commit_clone.py" materialise {sha}' in err
    assert delegations == []


def test_inside_the_clone_the_chain_is_that_checkout_and_the_live_repo_stays_the_source(rnd, repo, delegations):
    (repo / _CHAIN_FILES[0]).write_text("PINNED = 'dirty'\n")
    assert _main(rnd, ["alpha"]) == 7
    clone = delegations[0][0]
    in_clone = _load(clone / _ENTRY, "percolate_round_inside_clone")
    clone_bin = clone / "coordinator" / "bin"
    assert in_clone._PUBLISH == clone_bin / "publish.py"
    assert in_clone._PERCOLATE_GATE == clone_bin / "percolate-gate.py"
    assert in_clone._PERCOLATE_PUSH == clone_bin / "percolate-push.py"
    assert in_clone._PARSE_DRYRUN == clone_bin / "percolate-parse-dryrun.py"
    assert in_clone._COORDINATOR_LIB == clone / "coordinator" / "lib"
    assert in_clone._REPO_ROOT == clone
    assert in_clone._live_repo_root() == repo


def test_inside_a_clone_main_does_not_delegate_again(rnd, repo, head_sha, delegations, monkeypatch):
    commit_clone = rnd._commit_clone_module()
    clone = commit_clone.materialise_commit_clone(repo, head_sha, repo.parent / "cache")
    in_clone = _load(clone / _ENTRY, "percolate_round_inside_clone_main")
    ran: list = []
    monkeypatch.setattr(in_clone, "_bootstrap_engine", lambda: ran.append("bootstrap"))
    monkeypatch.setattr(in_clone, "_cmd_round", lambda args: ran.append("round") or 0)
    assert _main(in_clone, ["alpha"]) == 0
    assert ran == ["bootstrap", "round"] and delegations == []


def test_the_retired_git_archive_chain_export_is_gone(rnd):
    for name in ("_pin_publisher_chain", "_clean_export_publish_cmd", "_PINNED_ROOT", "_bind_chain"):
        assert not hasattr(rnd, name), name
