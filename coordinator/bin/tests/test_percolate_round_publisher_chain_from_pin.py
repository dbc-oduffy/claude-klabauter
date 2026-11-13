"""percolate-round runs publish.py and the gate/push CLIs from a per-sha export at the pin.

Driven through `main()` (default arguments) on a tmp_path repo whose publisher-chain file
differs from the pinned commit; the export caches (chain and engine) live under tmp_path.
"""
from __future__ import annotations

import faulthandler
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_BIN_DIR = Path(__file__).resolve().parents[1]
_CHAIN_FILES = (
    "coordinator/bin/publish.py",
    "coordinator/bin/percolate-gate.py",
    "coordinator/bin/percolate-push.py",
    "coordinator/bin/percolate-parse-dryrun.py",
    "coordinator/bin/publish-allowlist-generate.py",
)
_REAL_ENGINE_FILES = ("coordinator_core/temp_layout.py",)
_CHAIN_NAMES = ("_PUBLISH", "_PERCOLATE_GATE", "_PERCOLATE_PUSH", "_PARSE_DRYRUN", "_COORDINATOR_LIB", "_PINNED_ROOT")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def rnd(monkeypatch):
    spec = importlib.util.spec_from_file_location("percolate_round_chain_pin", _BIN_DIR / "percolate-round.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in _CHAIN_NAMES:
        monkeypatch.setattr(module, name, getattr(module, name))
    return module


@pytest.fixture(autouse=True)
def _managed_temp_under_tmp_path(tmp_path, monkeypatch):
    """The per-sha engine export lands under the managed temp dir; keep it in tmp_path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def _committed_text(rel: str) -> str:
    """publish.py is the real one (the export loads its `_export_engine_at_ref`); the rest are stubs."""
    return (_BIN_DIR / "publish.py").read_text(encoding="utf-8") if rel.endswith("/publish.py") else f"PINNED = {rel!r}\n"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel in _CHAIN_FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_committed_text(rel), encoding="utf-8")
    for rel in _REAL_ENGINE_FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes((_BIN_DIR.parents[1] / rel).read_bytes())
    for rel in ("coordinator_core/percolate/engine.py", "coordinator/lib/percolate/targets.py",
                "coordinator/bin/lib/helper.py", "setup/publish-targets.portable"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"PINNED = {rel!r}\n")
    (root / "README.md").write_text("doc\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _archive_spawns(rnd, monkeypatch) -> list:
    calls: list = []
    real = rnd._git_bytes

    def spy(repo_root, *args):
        calls.append(args)
        return real(repo_root, *args)

    monkeypatch.setattr(rnd, "_git_bytes", spy)
    return calls


def test_clean_chain_runs_from_this_checkout(rnd, repo, tmp_path):
    assert rnd._pin_publisher_chain(None, repo_root=repo, cache_root=tmp_path / "cache") is None
    assert rnd._PUBLISH == _BIN_DIR / "publish.py"
    assert rnd._PINNED_ROOT is None
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("rel", _CHAIN_FILES)
def test_dirty_chain_file_runs_the_whole_chain_from_the_pin(rnd, repo, tmp_path, rel):
    (repo / rel).write_text("PINNED = 'dirty'\n")
    cache = tmp_path / "cache"
    assert rnd._pin_publisher_chain(None, repo_root=repo, cache_root=cache) is None
    sha = _git(repo, "rev-parse", "HEAD")
    pinned_bin = cache / sha / "coordinator" / "bin"
    for name, script in (
        ("_PUBLISH", "publish.py"), ("_PERCOLATE_GATE", "percolate-gate.py"),
        ("_PERCOLATE_PUSH", "percolate-push.py"), ("_PARSE_DRYRUN", "percolate-parse-dryrun.py"),
    ):
        assert getattr(rnd, name) == pinned_bin / script
    assert (pinned_bin / Path(rel).name).read_text(encoding="utf-8") == _committed_text(rel)
    assert _git(cache / sha, "rev-parse", "HEAD") == sha


def test_second_round_at_the_same_sha_reuses_the_export(rnd, repo, tmp_path, monkeypatch):
    (repo / _CHAIN_FILES[0]).write_text("PINNED = 'dirty'\n")
    cache = tmp_path / "cache"
    assert rnd._pin_publisher_chain(None, repo_root=repo, cache_root=cache) is None
    calls = _archive_spawns(rnd, monkeypatch)
    assert rnd._pin_publisher_chain(None, repo_root=repo, cache_root=cache) is None
    assert [c[0] for c in calls] == ["diff", "rev-parse"]
    assert rnd._PUBLISH.is_relative_to(cache)


def test_full_sha_pin_costs_one_git_spawn_when_cached(rnd, repo, tmp_path, monkeypatch):
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / _CHAIN_FILES[1]).write_text("PINNED = 'dirty'\n")
    cache = tmp_path / "cache"
    assert rnd._pin_publisher_chain(sha, repo_root=repo, cache_root=cache) is None
    calls = _archive_spawns(rnd, monkeypatch)
    assert rnd._pin_publisher_chain(sha, repo_root=repo, cache_root=cache) is None
    assert [c[0] for c in calls] == ["diff"]


def test_failed_export_refuses_naming_the_divergent_files(rnd, repo, tmp_path, monkeypatch):
    (repo / _CHAIN_FILES[2]).write_text("PINNED = 'dirty'\n")

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rnd, "_export_chain_at_sha", boom)
    refusal = rnd._pin_publisher_chain(None, repo_root=repo, cache_root=tmp_path / "cache")
    assert _CHAIN_FILES[2] in refusal and "disk full" in refusal
    assert rnd._PUBLISH == _BIN_DIR / "publish.py"


def test_pin_resets_after_a_previous_round_pinned(rnd, repo, tmp_path):
    (repo / _CHAIN_FILES[0]).write_text("PINNED = 'dirty'\n")
    assert rnd._pin_publisher_chain(None, repo_root=repo, cache_root=tmp_path / "cache") is None
    assert rnd._PINNED_ROOT is not None
    _git(repo, "checkout", "--", ".")
    assert rnd._pin_publisher_chain(None, repo_root=repo, cache_root=tmp_path / "cache") is None
    assert rnd._PINNED_ROOT is None and rnd._PUBLISH == _BIN_DIR / "publish.py"


def test_main_default_entry_resolves_chain_from_pin_before_any_step(rnd, repo, tmp_path, monkeypatch):
    (repo / _CHAIN_FILES[0]).write_text("PINNED = 'dirty'\n")
    cache = tmp_path / "cache"
    monkeypatch.setattr(rnd, "_REPO_ROOT", repo)
    monkeypatch.setattr(rnd, "_chain_cache_root", lambda: cache)
    monkeypatch.setattr(rnd, "_bootstrap_engine", lambda: None)
    seen: dict = {}

    def first_step(override):
        seen["gate"] = rnd._PERCOLATE_GATE
        seen["publish"] = rnd._PUBLISH
        return None

    monkeypatch.setattr(rnd, "_resolve_percolate_root", first_step)
    try:
        rc = rnd.main(["alpha"])
    finally:
        faulthandler.cancel_dump_traceback_later()
    sha = _git(repo, "rev-parse", "HEAD")
    assert rc == rnd._EXIT_USAGE
    assert seen["gate"] == cache / sha / "coordinator" / "bin" / "percolate-gate.py"
    assert seen["publish"] == cache / sha / "coordinator" / "bin" / "publish.py"


def test_main_refuses_before_any_step_when_the_export_fails(rnd, repo, tmp_path, monkeypatch, capsys):
    (repo / _CHAIN_FILES[0]).write_text("PINNED = 'dirty'\n")
    monkeypatch.setattr(rnd, "_REPO_ROOT", repo)
    monkeypatch.setattr(rnd, "_chain_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(rnd, "_export_chain_at_sha", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setattr(rnd, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(rnd, "_resolve_percolate_root", lambda o: pytest.fail("a step ran after a refusal"))
    try:
        rc = rnd.main(["alpha"])
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert rc == rnd._EXIT_FAIL
    err = capsys.readouterr().err
    assert "nothing staged" in err and _CHAIN_FILES[0] in err


def test_publish_dirty_check_covers_every_chain_path(rnd):
    spec = importlib.util.spec_from_file_location("publish_chain_paths_under_test", _BIN_DIR / "publish.py")
    pub = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pub
    spec.loader.exec_module(pub)
    assert set(rnd._CHAIN_PATHS) == set(pub._PUBLISHER_CODE_PATHS)


def _commit_shas(repo: Path, count: int) -> list:
    shas = []
    for i in range(count):
        (repo / "README.md").write_text(f"doc {i}\n")
        _git(repo, "commit", "-qam", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return shas


def test_new_export_prunes_the_oldest_beyond_retention_and_keeps_the_returned_one(rnd, repo, tmp_path):
    cache = tmp_path / "cache"
    keep = rnd._CHAIN_EXPORT_RETENTION
    shas = _commit_shas(repo, keep + 2)
    for age, sha in enumerate(shas):
        dest = rnd._export_chain_at_sha(repo, sha, cache)
        stamp = 1_000_000 + age * 100
        os.utime(dest, (stamp, stamp))
    surviving = sorted(p.name for p in cache.iterdir())
    assert surviving == sorted(shas[-keep:])
    assert (cache / shas[-1] / ".git").is_file()


def test_pruning_never_removes_the_in_use_export_even_when_it_is_oldest(rnd, repo, tmp_path):
    cache = tmp_path / "cache"
    keep = rnd._CHAIN_EXPORT_RETENTION
    shas = _commit_shas(repo, keep + 1)
    for sha in shas[:keep]:
        dest = rnd._export_chain_at_sha(repo, sha, cache)
        os.utime(dest, (2_000_000, 2_000_000))
    old = cache / shas[0]
    os.utime(old, (1, 1))
    # The newest export is built last and is the one in use; the stale-looking `old` goes first.
    assert rnd._export_chain_at_sha(repo, shas[keep], cache) == cache / shas[keep]
    assert not old.exists()
    assert (cache / shas[keep] / ".git").is_file()
    assert len(list(cache.iterdir())) == keep


def test_cache_hit_prunes_nothing_and_staging_dirs_are_left_alone(rnd, repo, tmp_path):
    cache = tmp_path / "cache"
    shas = _commit_shas(repo, rnd._CHAIN_EXPORT_RETENTION)
    for sha in shas:
        rnd._export_chain_at_sha(repo, sha, cache)
    (cache / "abcdef012345-stage").mkdir()
    extra = cache / ("f" * 40)
    extra.mkdir()
    before = sorted(p.name for p in cache.iterdir())
    assert rnd._export_chain_at_sha(repo, shas[0], cache) == cache / shas[0]
    assert sorted(p.name for p in cache.iterdir()) == before
    rnd._prune_chain_exports(cache, keep=cache / shas[-1])
    assert (cache / "abcdef012345-stage").is_dir()
