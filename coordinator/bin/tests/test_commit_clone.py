"""commit_clone: the publisher-code closure of one commit written from the commit tree, warm hits
with zero spawns, a refusal naming a runnable retry when the clone cannot be built, and an entry
run from the clone rather than the caller's tree. All trees live under tmp_path."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ._git_template import copy_repo, template_repo

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_MODULE = Path(__file__).resolve().parents[1] / "lib" / "commit_clone.py"


@pytest.fixture()
def cc() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_commit_clone_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def _no_real_cache() -> Path:
        raise AssertionError("a test reached the real managed commit-clone cache; pass a tmp_path cache")

    module.default_cache_root = _no_real_cache
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_repo(root: Path) -> None:
    _write(root, "coordinator/bin/entry.py", "import sys\nprint('ENTRY=' + __file__)\nsys.exit(3)\n")
    _write(root, "coordinator/bin/publish.py", "SEED = 'committed'\n")
    _write(root, "coordinator/bin/lib/helper.py", "VALUE = 'committed'\n")
    _write(root, "coordinator/bin/lib/tests/test_helper.py", "def test_x(): pass\n")
    _write(root, "coordinator_core/__init__.py", "")
    _write(root, "coordinator_core/percolate/__init__.py", "")
    _write(root, "coordinator_core/other/__init__.py", "")
    _write(root, "coordinator_core/other/lazy.py", "WHERE = 'committed'\n")
    _write(root, "coordinator/bin/lazy_entry.py", "import coordinator_core.other.lazy as m\nprint('LAZY=' + m.WHERE + ' ' + m.__file__)\n")
    _write(root, "coordinator_core/other/schemas/shape.json", "{}\n")
    _write(root, "coordinator_core/other/tests/fixture.json", "{}\n")
    _write(root, "coordinator_core/percolate/schemas/store.json", "{}\n")
    _write(root, "coordinator/bin/data_entry.py", "import coordinator_core.other.lazy as m, pathlib\nprint('DATA=' + (pathlib.Path(m.__file__).parent / 'schemas' / 'shape.json').read_text().strip())\n")
    _write(root, "coordinator/bin/child_entry.py", "import subprocess, sys\nr = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from coordinator_core.other import lazy; print(\\'CHILD=\\' + lazy.WHERE)', sys.argv[1]], capture_output=True, text=True)\nprint(r.stdout + r.stderr)\n")
    _write(root, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(root, "docs/note.md", "payload\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")


_ORPHAN: dict[str, str] = {}


def _build_orphan_repo(root: Path) -> None:
    """The repo plus a commit on no ref (parent: the first commit), its tree holding SEED = 'orphan'."""
    _build_repo(root)
    _ORPHAN["first"] = _git(root, "rev-parse", "HEAD")
    _write(root, "coordinator/bin/publish.py", "SEED = 'orphan'\n")
    _git(root, "add", "-A")
    _ORPHAN["sha"] = _git(root, "commit-tree", _git(root, "write-tree"), "-m", "orphan")
    _git(root, "reset", "-q", "--hard", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return copy_repo(template_repo(tmp_path_factory, "commit-clone:repo", _build_repo), tmp_path / "live")


@pytest.fixture()
def orphan_repo(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    template = template_repo(tmp_path_factory, "commit-clone:orphan", _build_orphan_repo)
    return copy_repo(template, tmp_path / "live")


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD")


class _SpawnCounter:
    def __init__(self, monkeypatch) -> None:
        self.calls: list[list[str]] = []
        real = subprocess.run

        def counting(cmd, *a, **kw):
            self.calls.append(list(cmd))
            return real(cmd, *a, **kw)

        monkeypatch.setattr(subprocess, "run", counting)


@pytest.fixture()
def import_state(monkeypatch: pytest.MonkeyPatch):
    """Restore `sys.modules` in place (the interpreter holds its own reference to the dict)."""
    saved = dict(sys.modules)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    yield
    sys.modules.clear()
    sys.modules.update(saved)


def test_cold_clone_writes_the_seed_closure_with_two_spawns(cc, repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = _head(repo)
    counter = _SpawnCounter(monkeypatch)
    dest = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    assert dest == tmp_path / "cache" / sha
    assert [c[c.index("-C") + 2] for c in counter.calls] == ["ls-tree", "cat-file"]
    assert cc._read_marker(dest)["sha"] == sha
    assert _git(dest, "rev-parse", "HEAD") == sha
    assert (dest / "coordinator" / "bin" / "publish.py").read_text(encoding="utf-8") == "SEED = 'committed'\n"
    assert (dest / "coordinator" / "bin" / "lib" / "helper.py").is_file()
    assert (dest / "coordinator_core" / "__init__.py").is_file()
    assert (dest / "pyproject.toml").is_file()  # the engine-root probe's second marker
    assert not (dest / "docs").exists()
    assert not (dest / "coordinator" / "bin" / "lib" / "tests").exists()
    assert not (dest / "coordinator_core" / "other").exists()


def test_git_reads_against_the_clone_resolve_the_commit(cc, repo, tmp_path: Path) -> None:
    """The doors resolve `--commit <sha>` and read the commit against the clone itself."""
    sha = _head(repo)
    dest = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    assert _git(dest, "cat-file", "-t", sha) == "commit"
    assert _git(dest, "cat-file", "-t", "HEAD") == "commit"
    assert _git(dest, "rev-parse", "--verify", f"{sha}^{{commit}}") == sha
    assert _git(dest, "log", "-1", "--format=%s", sha) == "one"


def test_an_objectless_shell_is_rebuilt_not_taken_as_a_warm_hit(cc, repo, tmp_path: Path) -> None:
    """A shell with no alternates (the earlier in-tree format, or a legacy marker) must not poison the cache."""
    sha = _head(repo)
    cache = tmp_path / "cache"
    for marker_name in (cc._MARKER, cc._LEGACY_MARKER):
        shell = cache / sha
        (shell / ".git" / "objects").mkdir(parents=True, exist_ok=True)
        (shell / ".git" / marker_name).write_text(f'{{"sha": "{sha}", "source_root": "{repo.as_posix()}"}}', encoding="utf-8")
        assert cc._usable_marker(shell, sha) is None
        dest = cc.materialise_commit_clone(repo, sha, cache)
        assert dest == shell
        assert _git(dest, "cat-file", "-t", sha) == "commit"
        assert (dest / "coordinator" / "bin" / "publish.py").is_file()
        cc.shutil.rmtree(shell)


def test_a_dead_alternates_target_is_not_a_warm_hit(cc, repo, tmp_path: Path) -> None:
    sha = _head(repo)
    cache = tmp_path / "cache"
    dest = cc.materialise_commit_clone(repo, sha, cache)
    (dest / ".git" / "objects" / "info" / "alternates").write_text(str(tmp_path / "gone") + "\n", encoding="utf-8")
    again = cc.materialise_commit_clone(repo, sha, cache)
    assert _git(again, "cat-file", "-t", sha) == "commit"


def test_a_legacy_marker_still_names_the_source(cc, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / cc._LEGACY_MARKER).write_text(f'{{"sha": "{"a" * 40}", "source_root": "{(tmp_path / "src").as_posix()}"}}', encoding="utf-8")
    assert cc.is_commit_clone(clone)
    assert cc.source_repo_root(clone) == tmp_path / "src"


def test_objects_dir_follows_a_linked_worktree_to_the_common_store(cc, tmp_path: Path) -> None:
    common = tmp_path / "main" / ".git"
    (common / "objects").mkdir(parents=True)
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
    assert cc._objects_dir(wt) == (common / "objects").resolve()


def test_warm_hit_spawns_nothing(cc, repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = _head(repo)
    first = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    counter = _SpawnCounter(monkeypatch)
    assert cc.materialise_commit_clone(repo, sha, tmp_path / "cache") == first
    assert counter.calls == []


def test_clone_holds_committed_bytes_not_the_live_edit(cc, repo, tmp_path: Path) -> None:
    sha = _head(repo)
    _write(repo, "coordinator/bin/publish.py", "SEED = 'dirty'\n")
    _write(repo, "coordinator/bin/lib/untracked.py", "X = 1\n")
    dest = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    assert (dest / "coordinator" / "bin" / "publish.py").read_text(encoding="utf-8") == "SEED = 'committed'\n"
    assert not (dest / "coordinator" / "bin" / "lib" / "untracked.py").exists()


def test_sha_on_no_ref_materialises(cc, orphan_repo, tmp_path: Path) -> None:
    repo = orphan_repo
    orphan = _ORPHAN["sha"]
    assert orphan != _ORPHAN["first"]
    dest = cc.materialise_commit_clone(repo, orphan, tmp_path / "cache")
    assert (dest / "coordinator" / "bin" / "publish.py").read_text(encoding="utf-8") == "SEED = 'orphan'\n"


def test_unknown_sha_refuses_with_a_runnable_retry_and_leaves_no_staging(cc, repo, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    with pytest.raises(cc.CommitCloneError) as raised:
        cc.materialise_commit_clone(repo, "0" * 40, cache)
    assert f'python "{_MODULE}" materialise {"0" * 40}' in str(raised.value)
    assert not cache.exists() or list(cache.iterdir()) == []


def test_a_ref_name_is_not_a_sha_and_is_refused(cc, repo, tmp_path: Path) -> None:
    with pytest.raises(cc.CommitCloneError):
        cc.materialise_commit_clone(repo, "HEAD", tmp_path / "cache")


def test_a_materialise_failure_refuses_and_runs_nothing(cc, repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The door's contract: a failed clone raises (never returns a live path), so no live-tree code runs."""
    sha = _head(repo)

    def boom(*_a, **_k):
        raise RuntimeError("git cat-file failed: injected")

    monkeypatch.setattr(cc, "_read_blobs", boom)
    with pytest.raises(cc.CommitCloneError, match="nothing ran from the live tree.*injected.*Retry: python"):
        cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    assert not (tmp_path / "cache" / sha).exists()


def test_run_entry_refuses_a_tree_that_is_not_a_commit_clone(cc, repo, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(cc.CommitCloneError, match="not a commit clone"):
        cc.run_entry_from_clone(repo, "coordinator/bin/entry.py", ["entry.py"])
    assert "ENTRY=" not in capsys.readouterr().out


def test_retry_script_builds_the_clone_and_names_the_failure(repo, tmp_path: Path) -> None:
    sha = _head(repo)
    cache = tmp_path / "cache"
    ok = subprocess.run(
        [sys.executable, str(_MODULE), "materialise", sha, "--repo", str(repo), "--cache", str(cache)],
        capture_output=True, text=True,
    )
    assert ok.returncode == 0 and ok.stdout.strip() == str(cache / sha)
    bad = subprocess.run(
        [sys.executable, str(_MODULE), "materialise", "0" * 40, "--repo", str(repo), "--cache", str(cache)],
        capture_output=True, text=True,
    )
    assert bad.returncode == 1 and "Retry: python" in bad.stderr


def test_preexisting_dest_is_returned_and_staging_discarded(cc, repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = _head(repo)
    cache = tmp_path / "cache"
    cc.materialise_commit_clone(repo, sha, cache)
    # A concurrent winner: force the cold path again while dest already holds a marker.
    real_read = cc._read_marker
    seen = {"n": 0}

    def miss_once(root):
        seen["n"] += 1
        return None if seen["n"] == 1 else real_read(root)

    monkeypatch.setattr(cc, "_read_marker", miss_once)
    assert cc.materialise_commit_clone(repo, sha, cache) == cache / sha
    assert [p.name for p in cache.iterdir()] == [sha]


def test_prune_keeps_dest_and_newest(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 6)]
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        os.utime(d, (1000 + i, 1000 + i))
    (cache / "abc123-staging").mkdir()
    keep = cache / names[0]  # oldest, still kept
    cc._prune(cache, keep=keep)
    left = {p.name for p in cache.iterdir()}
    assert names[0] in left
    assert "abc123-staging" in left
    assert {names[4], names[3]} <= left
    assert names[1] not in left and names[2] not in left


def test_prune_spares_a_recently_used_clone_beyond_retention(cc, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    names = [f"{i:040x}" for i in range(1, 7)]
    now = time.time()
    for i, name in enumerate(names):
        d = cache / name
        d.mkdir()
        os.utime(d, (now - 10 + i, now - 10 + i))
    cc._prune(cache, keep=cache / names[5])
    assert {p.name for p in cache.iterdir()} == set(names)


def test_source_repo_root_round_trips(cc, repo, tmp_path: Path) -> None:
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    assert cc.is_commit_clone(dest)
    assert cc.source_repo_root(dest) == repo
    assert not cc.is_commit_clone(repo)
    assert cc.source_repo_root(repo) == repo


def test_run_entry_from_clone_runs_clone_file_not_callers(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str]) -> None:
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    # Live tree is dirty: the clone must still run the committed entry (filled from the commit).
    _write(repo, "coordinator/bin/entry.py", "raise SystemExit(9)\n")
    code = cc.run_entry_from_clone(dest, "coordinator/bin/entry.py", ["entry.py"])
    out = capsys.readouterr()
    assert code == 3
    assert f"ENTRY={dest / 'coordinator' / 'bin' / 'entry.py'}" in out.out
    assert f"running from commit clone {dest}" in out.err


def test_run_entry_from_clone_restores_the_callers_import_state(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str]) -> None:
    # An in-process caller (a test, the coordinator-publish alias under pytest) keeps running after
    # the entry returns: it must get back its own sys.path and coordinator_core binding, or every
    # later `import coordinator_core.<x>` resolves against a clone with no filler and fails.
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    import coordinator_core

    path_before = list(sys.path)
    meta_before = list(sys.meta_path)
    bound_before = {m: sys.modules[m] for m in sys.modules if m.split(".")[0] == "coordinator_core"}
    assert cc.run_entry_from_clone(dest, "coordinator/bin/lazy_entry.py", ["lazy_entry.py"]) == 0
    assert "LAZY=committed" in capsys.readouterr().out
    assert sys.path == path_before
    assert sys.meta_path == meta_before
    assert {m: sys.modules[m] for m in sys.modules if m.split(".")[0] == "coordinator_core"} == bound_before
    assert sys.modules["coordinator_core"] is coordinator_core


def test_a_module_outside_the_seed_is_filled_from_the_commit_and_recorded(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str]) -> None:
    sha = _head(repo)
    cache = tmp_path / "cache"
    dest = cc.materialise_commit_clone(repo, sha, cache)
    _write(repo, "coordinator_core/other/lazy.py", "WHERE = 'dirty'\n")
    assert cc.run_entry_from_clone(dest, "coordinator/bin/lazy_entry.py", ["lazy_entry.py"]) == 0
    assert f"LAZY=committed {dest / 'coordinator_core' / 'other' / 'lazy.py'}" in capsys.readouterr().out
    assert "coordinator_core/other/lazy.py" in cc._read_extra(cache)
    _write(repo, "coordinator/bin/publish.py", "SEED = 'second'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    second = cc.materialise_commit_clone(repo, _head(repo), cache)
    assert (second / "coordinator_core" / "other" / "lazy.py").is_file()


def test_a_seeded_package_carries_its_data_files(cc, repo, tmp_path: Path) -> None:
    """A module reading `Path(__file__).parent / "schemas"` at import needs the data with it."""
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    assert (dest / "coordinator_core" / "percolate" / "schemas" / "store.json").is_file()
    assert not (dest / "coordinator_core" / "other").exists()


def test_a_lazily_filled_package_brings_its_data_files_but_not_its_tests(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str]) -> None:
    cache = tmp_path / "cache"
    dest = cc.materialise_commit_clone(repo, _head(repo), cache)
    assert cc.run_entry_from_clone(dest, "coordinator/bin/data_entry.py", ["data_entry.py"]) == 0
    assert "DATA={}" in capsys.readouterr().out
    assert not (dest / "coordinator_core" / "other" / "tests").exists()
    _write(repo, "coordinator/bin/publish.py", "SEED = 'second'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    second = cc.materialise_commit_clone(repo, _head(repo), cache)
    assert (second / "coordinator_core" / "other" / "schemas" / "shape.json").is_file()


def test_a_clone_of_an_older_format_is_rebuilt(cc, repo, tmp_path: Path) -> None:
    sha = _head(repo)
    dest = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    marker = dest / ".git" / cc._MARKER
    marker.write_text(f'{{"sha": "{sha}", "source_root": "{repo.as_posix()}"}}', encoding="utf-8")
    (dest / "coordinator_core" / "percolate" / "schemas" / "store.json").unlink()
    again = cc.materialise_commit_clone(repo, sha, tmp_path / "cache")
    assert (again / "coordinator_core" / "percolate" / "schemas" / "store.json").is_file()


def test_a_child_python_of_the_entry_fills_from_the_commit_and_records(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """publish.py runs as a child of the round: `from coordinator_core import <mod>` must resolve."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    cache = tmp_path / "cache"
    dest = cc.materialise_commit_clone(repo, _head(repo), cache)
    assert cc.run_entry_from_clone(dest, "coordinator/bin/child_entry.py", ["child_entry.py", str(dest)]) == 0
    assert "CHILD=committed" in capsys.readouterr().out
    assert "coordinator_core/other/lazy.py" in cc._read_extra(cache)
    assert "PYTHONPATH" not in os.environ


def test_the_child_filler_chains_to_the_sitecustomize_it_shadows(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Homebrew Python's own sitecustomize adds its site-packages; shadowing it loses pyyaml."""
    other = tmp_path / "other-site"
    _write(other, "sitecustomize.py", "import os\nos.environ['OTHER_SITE'] = 'ran'\n")
    monkeypatch.setenv("PYTHONPATH", str(other))
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    _write(dest, "coordinator/bin/site_entry.py", "import subprocess, sys\nprint(subprocess.run([sys.executable, '-c', 'import os; print(\\'SITE=\\' + os.environ.get(\\'OTHER_SITE\\', \\'missing\\'))'], capture_output=True, text=True).stdout)\n")
    assert cc.run_entry_from_clone(dest, "coordinator/bin/site_entry.py", ["site_entry.py"]) == 0
    assert "SITE=ran" in capsys.readouterr().out
    assert os.environ["PYTHONPATH"] == str(other)


def test_a_child_python_with_no_shadowed_sitecustomize_prints_no_keyerror(cc, repo, tmp_path: Path, import_state, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    _write(dest, "coordinator/bin/bare_entry.py", "import subprocess, sys\nr = subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True, text=True)\nprint('ERR=' + r.stderr.strip())\nprint('OUT=' + r.stdout.strip())\n")
    assert cc.run_entry_from_clone(dest, "coordinator/bin/bare_entry.py", ["bare_entry.py"]) == 0
    out = capsys.readouterr().out
    assert "KeyError" not in out and "Error in sitecustomize" not in out
    assert "OUT=1" in out


def test_fill_tree_writes_the_committed_non_test_files_under_its_roots(cc, repo, tmp_path: Path) -> None:
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    _write(repo, "coordinator_core/other/lazy.py", "WHERE = 'dirty'\n")
    cc.fill_tree(dest, ("coordinator_core",))
    assert (dest / "coordinator_core" / "other" / "lazy.py").read_text(encoding="utf-8") == "WHERE = 'committed'\n"
    assert (dest / "coordinator_core" / "other" / "schemas" / "shape.json").is_file()
    assert not (dest / "coordinator_core" / "other" / "tests").exists()


def test_fill_paths_reports_what_the_commit_lacks(cc, repo, tmp_path: Path) -> None:
    dest = cc.materialise_commit_clone(repo, _head(repo), tmp_path / "cache")
    assert cc.fill_paths(dest, ["coordinator_core/other/lazy.py", "coordinator_core/nope.py"]) == ["coordinator_core/nope.py"]
    assert (dest / "coordinator_core" / "other" / "lazy.py").read_text(encoding="utf-8") == "WHERE = 'committed'\n"


@pytest.mark.cadence
def test_cold_clone_of_the_real_repo_meets_the_process_time_bar(tmp_path: Path) -> None:
    """The brightline bar (500ms process time, children included) on a cold clone of this repo's HEAD."""
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    repo_root = _MODULE.parents[3]
    sha = _git(repo_root, "rev-parse", "HEAD")
    script = tmp_path / "cold.py"
    script.write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('cc', {str(_MODULE)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"m.materialise_commit_clone(Path({str(repo_root)!r}), {sha!r}, Path(sys.argv[1]))\n",
        encoding="utf-8",
    )
    out = tmp_path / "cold.out"
    result = single_invocation_tree_process_time(
        [sys.executable, str(script), str(tmp_path / "cache")], cwd=str(tmp_path), stdout_path=str(out),
    )
    assert result["rc"] == 0
    assert result["process_time_ms"] <= 500.0, f"cold commit clone cost {result['process_time_ms']:.0f}ms process time"


def test_every_engine_file_the_publisher_loads_by_path_is_seeded(cc: ModuleType) -> None:
    """A by-path load bypasses the clone's import filler, so the file must be in the static seed."""
    import re

    bin_dir = _MODULE.parents[1]
    loaded = {
        f"coordinator_core/{name}"
        for entry in cc._SEED_FILES
        if entry.startswith("coordinator/bin/") and entry.endswith(".py")
        for name in re.findall(r'"coordinator_core" / "([A-Za-z_]+\.py)"', (bin_dir / entry.split("/")[-1]).read_text(encoding="utf-8"))
    }
    assert loaded, "the scan found no by-path load; the pattern no longer matches the publisher"
    assert sorted(p for p in loaded if not cc._is_wanted(p, frozenset())) == []
