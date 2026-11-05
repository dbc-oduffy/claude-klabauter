"""docs/plans/evidence/ is carved out of plan routing in match_schema and lint_tree."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coordinator_core import dag
from coordinator_core.frontmatter import lint_tree
from coordinator_core.frontmatter import schema_validate as sv
from coordinator_core.git import git_state

_SCHEMAS = {
    '_byKind': {},
    '_byGlob': [{'glob': 'docs/plans/**/*.md', 'schemaName': 'plan'}],
    'plan': {},
}


@pytest.mark.parametrize('path', [
    'docs/plans/evidence/foo.md',
    'docs\\plans\\evidence\\foo.md',
    'docs/plans/evidence/sub/foo.md',
])
def test_match_schema_skips_evidence(path):
    assert sv.match_schema(path, None, _SCHEMAS) is None
    assert sv.match_schema(path, {'title': 'x'}, _SCHEMAS) is None


def test_match_schema_routes_evidence_that_declares_a_kind():
    schemas = {**_SCHEMAS, '_byKind': {'widget': 'widget'}, 'widget': {}}
    resolved = sv.match_schema('docs/plans/evidence/foo.md', {'kind': 'widget'}, schemas)
    assert resolved is not None and resolved['schemaName'] == 'widget'


@pytest.mark.parametrize('path', [
    'docs/plans/2026-11-05-x.md',
    'docs\\plans\\2026-11-05-x.md',
    'docs/plans/evidence-notes.md',
])
def test_match_schema_still_routes_plans(path):
    resolved = sv.match_schema(path, None, _SCHEMAS)
    assert resolved is not None and resolved['schemaName'] == 'plan'


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / 'tree'
    (root / '.git').mkdir(parents=True)
    (root / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
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
    monkeypatch.setattr(dag, 'build_persisted_git_history_cache',
                        lambda r, timeout_s=15.0: dag.GitHistoryCache(set(), True))
    return root


def _put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8', newline='\n')


def test_lint_tree_skips_evidence_but_still_lints_plans(tree, capsys):
    _put(tree, 'docs/plans/evidence/e.md', 'no frontmatter\n')
    _put(tree, 'docs/plans/2026-11-05-x.md', 'no frontmatter\n')
    capsys.readouterr()
    lint_tree.run(str(tree), as_json=True, strict_refs=False)
    files = {v['file'] for v in json.loads(capsys.readouterr().out)['violations']}
    assert not any('evidence' in f for f in files)
    assert any(f.endswith('2026-11-05-x.md') for f in files)
