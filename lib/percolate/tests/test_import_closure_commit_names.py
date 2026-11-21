"""`find_commit_from_import_name_violations` resolves from-import names against
the blobs of a commit, never the worktree. Fixtures commit a tiny repo under
tmp_path and then dirty the worktree, the shape of the three 2026-11-02
landing gaps."""

import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))
_BIN_DIR = str(Path(__file__).resolve().parents[3] / "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from percolate.import_closure import find_commit_from_import_name_violations  # noqa: E402

pytestmark = [pytest.mark.complete]


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


def test_warm_cache_parses_nothing_and_keeps_the_verdict(tmp_path, monkeypatch):
    from collections import Counter
    import percolate.import_closure as ic
    from coordinator_core.percolate.gate_result_cache import GateResultCache

    sources = {
        'coordinator_core/__init__.py': '',
        'coordinator_core/a.py': 'from coordinator_core.shared import present, absent\n',
        'coordinator_core/shared.py': 'present = 1\n',
    }
    monkeypatch.setattr(ic, '_read_commit_py_blobs', lambda *a: (sources, {'coordinator_core'}))
    calls = Counter()
    real = ic.ast.parse
    def counted(source, filename='<unknown>', **kwargs):
        calls[filename] += 1
        return real(source, filename=filename, **kwargs)
    monkeypatch.setattr(ic.ast, 'parse', counted)

    def run():
        cache = GateResultCache(tmp_path / 'cache', 'commit-from-import', 'v1')
        result = ic.find_commit_from_import_name_violations(tmp_path, 'c', cache=cache)
        cache.flush()
        return result

    expected = (1, [('coordinator_core/a.py', 'coordinator_core.shared.absent')])
    assert run() == expected
    cold = sum(calls.values())
    assert run() == expected
    assert sum(calls.values()) == cold
    # A changed blob is a new key: re-parsed, and the verdict follows the new bytes.
    sources['coordinator_core/shared.py'] += 'absent = 2\n'
    assert run() == (1, [])
    assert calls['coordinator_core/shared.py'] == 2
