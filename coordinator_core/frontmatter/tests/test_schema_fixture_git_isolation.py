"""Proof that test_schema_validate.py's seed-repo git helpers cannot reach a repo outside their own.

Scenario: an export directory nested in a real repo, cwd inside it, GIT_DIR/GIT_WORK_TREE inherited
from the parent. An unscrubbed `git -C <fixture> commit` then lands in the parent repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.tests import test_schema_validate as tsv
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')


def _raw(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        check=True, capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    ).stdout


def _snapshot(parent: Path, global_cfg: Path) -> tuple[str, str, bytes, bytes]:
    return (
        _raw(parent, 'rev-parse', 'HEAD').strip(),
        _raw(parent, 'for-each-ref'),
        (parent / '.git' / 'config').read_bytes(),
        global_cfg.read_bytes(),
    )


def _seed(git, fixture: Path) -> None:
    fixture.mkdir()
    (fixture / 'widget.schema.json').write_text('{"title": "widget"}\n', encoding='utf-8')
    git(fixture, 'init', '-q')
    git(fixture, 'config', 'user.email', 'test@example.invalid')
    git(fixture, 'config', 'user.name', 'coordinator-test')
    git(fixture, 'add', '-A')
    git(fixture, 'commit', '-q', '-m', 'seed widget schema')


_HELPERS = {
    'advisory': lambda: tsv._advisory_git,
    'ahead': lambda: tsv._ahead_git,
    'batch': lambda: tsv.TestCheckSchemaDriftBatch._git,
}


@pytest.mark.parametrize('helper', sorted(_HELPERS))
def test_seed_helper_leaves_enclosing_repo_untouched(helper, tmp_path, monkeypatch):
    global_cfg = tmp_path / 'global.gitconfig'
    global_cfg.write_text('[core]\n\tautocrlf = false\n', encoding='utf-8')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_cfg))
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    parent = tmp_path / 'parent'
    parent.mkdir()
    _raw(parent, 'init', '-q')
    _raw(parent, 'config', 'user.email', 'test@example.invalid')
    _raw(parent, 'config', 'user.name', 'parent')
    (parent / 'real.txt').write_text('real\n', encoding='utf-8')
    _raw(parent, 'add', '-A')
    _raw(parent, 'commit', '-q', '-m', 'real work')
    before = _snapshot(parent, global_cfg)

    export = parent / 'export'
    export.mkdir()
    monkeypatch.chdir(export)
    monkeypatch.setenv('GIT_DIR', str(parent / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(parent))
    monkeypatch.delenv('GIT_CEILING_DIRECTORIES', raising=False)

    try:
        _seed(_HELPERS[helper](), export / 'fixture')
    except subprocess.CalledProcessError:
        pass

    assert _snapshot(parent, global_cfg) == before


def test_helpers_scrub_inherited_git_vars(tmp_path, monkeypatch):
    for var in _GIT_VARS:
        monkeypatch.setenv(var, str(tmp_path / 'elsewhere'))
    repo = tmp_path / 'repo'
    env = tsv._isolated_git_env(repo)
    assert not any(var in env for var in _GIT_VARS)
    assert env['GIT_CEILING_DIRECTORIES'] == str(tmp_path)
