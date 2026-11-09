"""`find_commit_from_import_name_violations` resolves from-import names against
the blobs of a commit, never the worktree. Fixtures commit a tiny repo under
tmp_path and then dirty the worktree, the shape of the three 2026-11-02
landing gaps."""

import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))
_BIN_DIR = str(Path(__file__).resolve().parents[3] / "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from percolate.import_closure import find_commit_from_import_name_violations  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]


def _write(root: Path, rel: str, src: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, importer_src: str = "from coordinator_core.helpers import present\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "coordinator_core/__init__.py", "")
    _write(repo, "coordinator_core/helpers.py", "def present():\n    pass\n")
    _write(repo, "coordinator_core/user.py", importer_src)
    _write(repo, "coordinator_core/warm/door/leaf.py", "VALUE = 1\n")
    _write(repo, "coordinator/bin/tool.py", "from coordinator_core.helpers import present\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def test_clean_commit_passes_and_reports_denominator(tmp_path):
    repo = _repo(tmp_path)
    examined, violations = find_commit_from_import_name_violations(repo, _head(repo))
    assert violations == []
    assert examined == 2


def test_blob_read_spans_chunks_and_still_resolves_every_importer(tmp_path, monkeypatch):
    import percolate.import_closure as ic

    repo = _repo(tmp_path, "from coordinator_core.helpers import present, absent_name\n")
    reads = []
    real = ic._git_bytes
    monkeypatch.setattr(ic, "_CAT_FILE_CHUNK", 2)
    monkeypatch.setattr(ic, "_git_bytes", lambda r, a, s=None: reads.append(a[0]) or real(r, a, s))
    examined, violations = find_commit_from_import_name_violations(repo, _head(repo))
    assert examined == 2
    assert violations == [("coordinator_core/user.py", "coordinator_core.helpers.absent_name")]
    assert reads.count("cat-file") == 3


def test_name_only_in_worktree_hunk_is_refused_naming_importer_and_name(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core.helpers import present, spawn_in_capped_job\n")
    _write(
        repo,
        "coordinator_core/helpers.py",
        "def present():\n    pass\n\ndef spawn_in_capped_job():\n    pass\n",
    )
    _, violations = find_commit_from_import_name_violations(repo, _head(repo))
    assert violations == [("coordinator_core/user.py", "coordinator_core.helpers.spawn_in_capped_job")]


def test_same_name_once_committed_passes(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core.helpers import present, extra\n")
    _write(repo, "coordinator_core/helpers.py", "def present():\n    pass\n\ndef extra():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "define extra")
    assert find_commit_from_import_name_violations(repo, _head(repo))[1] == []


def test_untracked_module_is_refused(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core.testing.guard import thing\n")
    _write(repo, "coordinator_core/testing/guard.py", "thing = 1\n")
    _, violations = find_commit_from_import_name_violations(repo, _head(repo))
    assert violations == [("coordinator_core/user.py", "coordinator_core.testing.guard")]


def test_namespace_package_without_init_never_flags(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core.warm.door import anything, leaf\n")
    assert find_commit_from_import_name_violations(repo, _head(repo))[1] == []


def test_module_defining_getattr_never_flags(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core.lazy import whatever\n")
    _write(repo, "coordinator_core/lazy.py", "def __getattr__(name):\n    return name\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "lazy")
    assert find_commit_from_import_name_violations(repo, _head(repo))[1] == []


def test_submodule_name_never_flags(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core import helpers\nfrom coordinator_core.warm import door\n")
    _write(repo, "coordinator_core/warm/__init__.py", "")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "warm")
    assert find_commit_from_import_name_violations(repo, _head(repo))[1] == []


def test_absent_name_in_package_init_is_flagged(tmp_path):
    repo = _repo(tmp_path, "from coordinator_core import nothing_here\n")
    _, violations = find_commit_from_import_name_violations(repo, _head(repo))
    assert violations == [("coordinator_core/user.py", "coordinator_core.nothing_here")]


def test_test_modules_relative_and_guarded_imports_are_ignored(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "coordinator_core/tests/test_x.py", "from coordinator_core.helpers import missing\n")
    _write(repo, "coordinator_core/rel.py", "from .helpers import missing\n")
    _write(
        repo,
        "coordinator_core/guarded.py",
        "try:\n    from coordinator_core.helpers import missing\nexcept ImportError:\n    missing = None\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignored shapes")
    assert find_commit_from_import_name_violations(repo, _head(repo))[1] == []


def test_publish_preflight_gate_refuses_and_names_importer_and_name(tmp_path):
    import publish

    repo = _repo(tmp_path, "from coordinator_core.helpers import ghost\n")
    target = SimpleNamespace(source_dir=repo / "coordinator_core")
    out, err = io.StringIO(), io.StringIO()
    assert publish._commit_import_name_gate(target, _head(repo), out=out, err=err) is False
    assert "coordinator_core/user.py: coordinator_core.helpers.ghost" in err.getvalue()
    assert "importer file(s) examined" in out.getvalue()


def test_publish_preflight_gate_passes_clean_commit(tmp_path):
    import publish

    repo = _repo(tmp_path)
    target = SimpleNamespace(source_dir=repo / "coordinator_core")
    out, err = io.StringIO(), io.StringIO()
    assert publish._commit_import_name_gate(target, _head(repo), out=out, err=err) is True
    assert err.getvalue() == ""


def test_commit_ast_is_shared_read_only_by_importer_and_namespace_checks(tmp_path, monkeypatch):
    import ast
    from collections import Counter
    import percolate.import_closure as ic

    sources = {
        'coordinator_core/__init__.py': '',
        'coordinator_core/a.py': (
            'from coordinator_core.shared import present as one, absent as missing_one\n'
            'from coordinator_core.shared import present as two, absent as missing_two\n'
        ),
        'coordinator_core/b.py': 'from coordinator_core.shared import present, another_missing\n',
        'coordinator_core/shared.py': 'from coordinator_core.leaf import value as present\n',
        'coordinator_core/leaf.py': 'value = 7\n',
    }
    monkeypatch.setattr(ic, '_read_commit_py_blobs', lambda *a: (sources, {'coordinator_core'}))
    parse_calls, snapshots = Counter(), {}
    original_parse = ast.parse
    def counted(source, filename='<unknown>', **kwargs):
        parse_calls[filename] += 1
        tree = original_parse(source, filename=filename, **kwargs)
        snapshots[filename] = (tree, ast.dump(tree, include_attributes=True))
        return tree
    monkeypatch.setattr(ic.ast, 'parse', counted)
    examined, violations = ic.find_commit_from_import_name_violations(tmp_path, 'immutable-commit')
    assert examined == 3
    assert violations == [
        ('coordinator_core/a.py', 'coordinator_core.shared.absent'),
        ('coordinator_core/a.py', 'coordinator_core.shared.absent'),
        ('coordinator_core/b.py', 'coordinator_core.shared.another_missing'),
    ]
    assert parse_calls == Counter({
        'coordinator_core/a.py': 1, 'coordinator_core/b.py': 1,
        'coordinator_core/shared.py': 1, 'coordinator_core/leaf.py': 1,
    })
    assert all(ast.dump(tree, include_attributes=True) == before for tree, before in snapshots.values())
    # Caches belong to this read, not a mutable ref string or prior invocation.
    sources['coordinator_core/shared.py'] += 'absent = another_missing = 1\n'
    _, later_violations = ic.find_commit_from_import_name_violations(tmp_path, 'different-commit')
    assert later_violations == []
    assert all(count == 2 for count in parse_calls.values())


def test_unparseable_committed_module_is_cached_without_changing_gate_semantics(tmp_path, monkeypatch):
    from collections import Counter
    import percolate.import_closure as ic
    sources = {
        'coordinator_core/a.py': 'from coordinator_core.b import x, y\n',
        'coordinator_core/b.py': 'from coordinator_core.a import x\ndef (:\n',
    }
    monkeypatch.setattr(ic, '_read_commit_py_blobs', lambda *a: (sources, {'coordinator_core'}))
    calls = Counter()
    real = ic.ast.parse
    def counted(source, filename='<unknown>', **kwargs):
        calls[filename] += 1
        return real(source, filename=filename, **kwargs)
    monkeypatch.setattr(ic.ast, 'parse', counted)
    assert ic.find_commit_from_import_name_violations(tmp_path, 'sealed') == (1, [])
    assert calls == Counter({'coordinator_core/a.py': 1, 'coordinator_core/b.py': 1})
