"""lint_tree: warm replay, dependency-keyed invalidation, store robustness.

Trees are built under tmp_path with a bare `.git/` directory (the store only
needs the git dir to resolve); history and ref tips are stubbed where a test
reaches them.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import dag
from coordinator_core.frontmatter import lint_tree
from coordinator_core.frontmatter import schema_validate as sv
from coordinator_core.git import git_state

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_HANDOFF_FM = "kind: session-handoff\nhandoff_id: {hid}\n{extra}"


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8', newline='\n')
    return p


def _record(fm: str) -> str:
    return f"---\n{fm}---\n\nbody\n"


def _handoff(root: Path, name: str, hid: str, extra: str = '') -> Path:
    return _write(root, f'state/handoffs/{name}', _record(_HANDOFF_FM.format(hid=hid, extra=extra)))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A repo-shaped tree whose validator/schema fingerprint inputs are private copies."""
    (tmp_path / 'tree' / '.git').mkdir(parents=True)
    (tmp_path / 'tree' / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    root = tmp_path / 'tree'

    core = tmp_path / 'core'
    for name in lint_tree._CODE_FILES:
        dst = core / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lint_tree._CORE / name, dst)
    schemas = tmp_path / 'schemas'
    shutil.copytree(lint_tree._SCHEMAS_DIR, schemas)
    monkeypatch.setattr(lint_tree, '_CORE', core)
    monkeypatch.setattr(lint_tree, '_SCHEMAS_DIR', schemas)

    monkeypatch.setattr(git_state, 'ref_tips_digest', lambda repo: 'tips-a')
    monkeypatch.setattr(dag, 'build_persisted_git_history_cache', lambda r, timeout_s=15.0: dag.GitHistoryCache(set(), True))
    return root


class _Counters:
    def __init__(self, monkeypatch):
        self.validated = 0
        self.ref_checks = 0
        real_validate, real_refs = sv.validate_frontmatter_obj, sv._check_handoff_refs

        def validate(*a, **k):
            self.validated += 1
            return real_validate(*a, **k)

        def refs(*a, **k):
            self.ref_checks += 1
            return real_refs(*a, **k)

        monkeypatch.setattr(sv, 'validate_frontmatter_obj', validate)
        monkeypatch.setattr(sv, '_check_handoff_refs', refs)

    def reset(self):
        self.validated = self.ref_checks = 0


@pytest.fixture
def counts(monkeypatch):
    return _Counters(monkeypatch)


def _sweep(root: Path, capsys, strict: bool = False) -> dict:
    capsys.readouterr()
    rc = lint_tree.run(str(root), as_json=True, strict_refs=strict)
    out = json.loads(capsys.readouterr().out)
    assert rc == (0 if out['ok'] else 1)
    return out


def _seed(root: Path) -> None:
    _write(root, 'docs/plans/p1.md', _record('title: "Plan"\nstatus: "bogué中"\n'))
    _write(root, 'docs/plans/p2.md', _record('title: two\n'))
    _write(root, 'docs/decisions/d1.md', _record('title: d\n'))
    _write(root, 'docs/plans/INDEX.md', 'no frontmatter\n')
    _handoff(root, 'a.md', 'id-a', 'predecessor: state/handoffs/missing.md\n')
    _handoff(root, 'b.md', 'id-b')


def _by_file(out: dict) -> dict:
    return {v['file']: v for v in out['violations']}


def test_warm_replay_reads_nothing_and_spawns_nothing(tree, capsys, monkeypatch):
    _seed(tree)
    cold = _sweep(tree, capsys)
    assert cold['violations'] and cold['refWarnings']
    store = tree / '.git' / lint_tree._STORE_NAME
    assert store.is_file()

    opened: list[str] = []
    real_open = builtins.open

    def spy_open(file, *a, **k):
        if os.path.abspath(str(file)) != os.path.abspath(str(store)):
            opened.append(str(file))
        return real_open(file, *a, **k)

    def no_spawn(*a, **k):
        raise AssertionError('spawned')

    monkeypatch.setattr(builtins, 'open', spy_open)
    monkeypatch.setattr(io, 'open', spy_open)
    monkeypatch.setattr(subprocess, 'Popen', no_spawn)
    warm = _sweep(tree, capsys)
    assert warm == cold
    assert opened == []


def test_file_edit_revalidates_only_that_file(tree, capsys, counts):
    _seed(tree)
    _sweep(tree, capsys)
    counts.reset()
    _write(tree, 'docs/plans/p2.md', _record('title: two\nstatus: also-bogus\n'))
    out = _sweep(tree, capsys)
    assert counts.validated == 1
    assert 'status' in json.dumps(_by_file(out)['docs/plans/p2.md'])


def test_schema_edit_invalidates_only_that_schemas_files(tree, capsys, counts):
    _seed(tree)
    _sweep(tree, capsys)
    schema_path = tree.parent / 'schemas' / 'decision.schema.json'
    doc = json.loads(schema_path.read_text(encoding='utf-8'))
    doc['x-test-marker'] = 1
    schema_path.write_text(json.dumps(doc), encoding='utf-8')
    counts.reset()
    _sweep(tree, capsys)
    assert counts.validated == 1  # docs/decisions/d1.md only


def test_schema_touch_without_content_change_keeps_every_verdict(tree, capsys, counts):
    _seed(tree)
    _sweep(tree, capsys)
    schema_path = tree.parent / 'schemas' / 'decision.schema.json'
    schema_path.write_bytes(schema_path.read_bytes())
    os.utime(schema_path, ns=(1, 1))
    counts.reset()
    _sweep(tree, capsys)
    assert counts.validated == 0


def test_validator_module_edit_revalidates_everything(tree, capsys, counts):
    _seed(tree)
    _sweep(tree, capsys)
    counts.reset()
    sv_copy = tree.parent / 'core' / 'frontmatter' / 'schema_validate.py'
    sv_copy.write_bytes(sv_copy.read_bytes() + b'\n# edited\n')
    _sweep(tree, capsys)
    assert counts.validated >= 5  # p1, p2, d1, a, b


def test_roadmap_override_env_change_revalidates_everything(tree, capsys, counts, monkeypatch):
    _seed(tree)
    _sweep(tree, capsys)
    counts.reset()
    monkeypatch.setenv(lint_tree._ROADMAP_ENV, '1')
    _sweep(tree, capsys)
    assert counts.validated >= 5


def test_handoff_ref_target_created_then_deleted(tree, capsys, counts):
    _seed(tree)
    first = _sweep(tree, capsys)
    assert any('missing.md' in w['warning']['error'] for w in first['refWarnings'])

    counts.reset()
    _handoff(tree, 'missing.md', 'id-m')
    second = _sweep(tree, capsys)
    assert not any('missing.md' in w['warning']['error'] for w in second['refWarnings'])
    assert counts.ref_checks >= 2  # a.md (dependency changed) and the new file itself

    counts.reset()
    (tree / 'state/handoffs/missing.md').unlink()
    third = _sweep(tree, capsys)
    assert any('missing.md' in w['warning']['error'] for w in third['refWarnings'])
    assert third['refWarnings'] == first['refWarnings']


def test_strict_refs_promotes_stored_warnings_without_revalidating(tree, capsys, counts):
    _seed(tree)
    loose = _sweep(tree, capsys)
    counts.reset()
    strict = _sweep(tree, capsys, strict=True)
    assert counts.validated == 0 and counts.ref_checks == 0
    assert strict['refWarnings'] == []
    a = _by_file(strict)['state/handoffs/a.md']
    assert any('missing.md' in e['error'] for e in a['errors'])
    assert loose['refWarnings'][0]['warning'] in a['errors']


def test_handoff_id_change_elsewhere_revalidates_id_dependents(tree, capsys, counts):
    _seed(tree)
    _handoff(tree, 'c.md', 'id-c', 'predecessor_id: id-b\npredecessor: state/handoffs/b.md\n')
    first = _sweep(tree, capsys)
    assert not any('id-b' in w['warning']['error'] for w in first['refWarnings'])

    counts.reset()
    _handoff(tree, 'b.md', 'id-b-renamed')
    second = _sweep(tree, capsys)
    assert any('predecessor_id "id-b"' in w['warning']['error'] for w in second['refWarnings'])
    assert counts.ref_checks >= 2  # b.md (edited) and c.md (id-index digest moved)


def test_unrelated_handoff_id_change_leaves_id_free_entries_alone(tree, capsys, counts):
    _seed(tree)
    _handoff(tree, 'c.md', 'id-c', 'predecessor_id: id-b\npredecessor: state/handoffs/b.md\n')
    _sweep(tree, capsys)
    counts.reset()
    _handoff(tree, 'b.md', 'id-b-renamed')
    _sweep(tree, capsys)
    assert counts.ref_checks == 2  # b.md (edited) and c.md (id ref); a.md carries no id ref


def test_ref_tips_change_rechecks_history_negative_entries_only(tree, capsys, counts, monkeypatch):
    _seed(tree)
    tips = {'v': 'tips-a'}
    monkeypatch.setattr(git_state, 'ref_tips_digest', lambda repo: tips['v'])
    _sweep(tree, capsys)
    counts.reset()
    _sweep(tree, capsys)
    assert counts.ref_checks == 0  # tips unchanged

    tips['v'] = 'tips-b'
    _sweep(tree, capsys)
    assert counts.ref_checks == 1  # a.md only; b.md rests on no negative history answer

    counts.reset()
    _sweep(tree, capsys)
    assert counts.ref_checks == 0


def test_unreadable_ref_tips_always_recheck_history_negative_entries(tree, capsys, counts, monkeypatch):
    _seed(tree)
    monkeypatch.setattr(git_state, 'ref_tips_digest', lambda repo: None)
    _sweep(tree, capsys)
    counts.reset()
    _sweep(tree, capsys)
    assert counts.ref_checks == 1


def test_non_ascii_and_nested_hints_round_trip_through_the_store(tree, capsys):
    _seed(tree)
    cold = _sweep(tree, capsys)
    stored = json.loads((tree / '.git' / lint_tree._STORE_NAME).read_text(encoding='utf-8'))
    assert 'docs/plans/p1.md' in stored['files']
    assert 'é' in json.dumps(cold, ensure_ascii=False)
    assert _sweep(tree, capsys) == cold
    for violation in cold['violations']:
        for err in violation['errors']:
            assert set(err) >= {'field', 'error'}


def test_outcome_that_does_not_round_trip_is_never_stored(tree, capsys, monkeypatch):
    _seed(tree)
    real = sv.validate_frontmatter_obj

    def odd(fm, schema):
        result = real(fm, schema)
        if fm.get('title') == 'two':
            return {'ok': False, 'errors': [{'field': 'f', 'error': 'e', 'hint': ('a', 'b')}]}
        return result

    monkeypatch.setattr(sv, 'validate_frontmatter_obj', odd)
    cold = _sweep(tree, capsys)
    stored = json.loads((tree / '.git' / lint_tree._STORE_NAME).read_text(encoding='utf-8'))
    assert 'docs/plans/p2.md' not in stored['files']
    assert _sweep(tree, capsys) == cold


@pytest.mark.parametrize('garbage', [
    'not json at all',
    '[]',
    '{"v": 99}',
    '{"v": 1, "env": 1, "code": {}, "sfiles": {}, "files": {}, "hids": {}}',
    '{"v": 1, "env": "0", "code": {}, "sfiles": {}, "files": {"docs/plans/p1.md": 7}, "hids": {}}',
])
def test_corrupt_or_foreign_store_rebuilds_silently(tree, capsys, garbage):
    _seed(tree)
    cold = _sweep(tree, capsys)
    (tree / '.git' / lint_tree._STORE_NAME).write_text(garbage, encoding='utf-8')
    rebuilt = _sweep(tree, capsys)
    assert rebuilt == cold
    assert capsys.readouterr().err == ''
    json.loads((tree / '.git' / lint_tree._STORE_NAME).read_text(encoding='utf-8'))


def test_foreign_code_fingerprint_rebuilds(tree, capsys, counts):
    _seed(tree)
    cold = _sweep(tree, capsys)
    store = tree / '.git' / lint_tree._STORE_NAME
    data = json.loads(store.read_text(encoding='utf-8'))
    first = next(iter(data['code']))
    data['code'][first] = [1, 1, 'foreign']
    store.write_text(json.dumps(data), encoding='utf-8')
    counts.reset()
    assert _sweep(tree, capsys) == cold
    assert counts.validated >= 5


def test_inert_without_a_git_dir(tmp_path, capsys):
    root = tmp_path / 'nogit'
    _seed(root)
    out = _sweep(root, capsys)
    assert out['violations']
    assert list(root.glob('**/claude-klabauter-lint-tree.json')) == []


def test_roadmap_env_name_matches_the_validator_constant():
    assert lint_tree._ROADMAP_ENV == sv._ROADMAP_GRAPH_OVERRIDE_ENV_VAR


def test_first_claim_wins_when_two_globs_match(tree, capsys):
    _seed(tree)
    out = _sweep(tree, capsys)
    files = [v['file'] for v in out['violations']]
    assert len(files) == len(set(files))


def test_sidecars_yaml_and_directory_index_are_skipped(tree, capsys):
    _write(tree, 'docs/plans/p1.prior-art-check.md', _record('x: 1\n'))
    _write(tree, 'docs/plans/data.yaml', 'a: 1\n')
    _write(tree, 'docs/plans/INDEX.md', 'no frontmatter\n')
    out = _sweep(tree, capsys)
    assert out['ok'] and out['violations'] == []


def test_human_output_and_exit_code(tree, capsys):
    _seed(tree)
    capsys.readouterr()
    rc = lint_tree.run(str(tree), as_json=False, strict_refs=False)
    text = capsys.readouterr().out
    assert rc == 1
    assert text.startswith('lint-frontmatter: ') and 'violation(s) (root: ' in text
    assert 'dangling handoff-DAG ref warning(s)' in text


def test_main_routes_non_tree_argv_to_schema_validate(monkeypatch):
    seen = {}
    monkeypatch.setattr(sv, 'main', lambda argv: seen.setdefault('argv', argv) and 7)
    assert lint_tree.main(['--file', 'x.md']) == 7
    assert seen['argv'] == ['--file', 'x.md']
    assert lint_tree.main(['--root']) == 7


def test_main_rejects_a_root_that_is_not_a_directory(tmp_path, capsys):
    assert lint_tree.main(['--root', str(tmp_path / 'absent'), '--json']) == 2
    assert 'does not resolve to a directory' in capsys.readouterr().err


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_warm_run_in_a_fresh_interpreter_never_imports_the_validator(tree, capsys):
    _seed(tree)
    cold = _sweep(tree, capsys)
    probe = (
        "import sys, json\n"
        "from coordinator_core.frontmatter import lint_tree\n"
        "rc = lint_tree.main(['--root', sys.argv[1], '--json'])\n"
        "sys.stderr.write(json.dumps({'sv': 'coordinator_core.frontmatter.schema_validate' in sys.modules,"
        " 'dag': 'coordinator_core.dag' in sys.modules, 'rc': rc}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(_CLAUDE_KLABAUTER_ROOT))
    # The fixture fingerprints private copies; the child fingerprints the real files. Content is
    # identical, so the first child run only restamps the store and the second replays warm.
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, '-c', probe, str(tree)], capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    flags = json.loads(proc.stderr.strip().splitlines()[-1])
    assert flags == {'sv': False, 'dag': False, 'rc': 1}
    assert json.loads(proc.stdout) == cold
