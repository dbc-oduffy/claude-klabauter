"""Characterization pin for the whole-tree lint-frontmatter sweep.

Drives ``schema_validate.main`` over a mixed fixture tree and pins the parsed
``--json`` output as a literal, cold and then cached. The expected literals
were produced by the implementation that predates the lint_tree rebuild and
reviewed entry by entry; the rebuild must reproduce them without edits.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ['git', '-C', str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


_VALID_PLAN = """---
title: A valid plan
created: 2026-01-01
author: tester
status: draft
---
Body.
"""

_INVALID_PLAN = """---
title: Missing author and status
created: 2026-01-01
---
Body.
"""

_UNREGISTERED_KIND = """---
title: Odd record
kind: no-such-kind-anywhere
---
Body.
"""

_DUP_KEYS = """---
title: First title
title: Second title
created: 2026-01-01
author: tester
status: draft
---
Body.
"""

_BARE_DATE_IN_STRING_FIELD = """---
title: 2026-01-05
created: 2026-01-01
author: tester
status: draft
---
Body.
"""

_SKIPPED_NO_FRONTMATTER = "# Index\n\nGenerated; no frontmatter.\n"

_SIDECAR = "---\nnot: a plan\n---\nSidecar body.\n"

_YAML_FILE = "title: skipped\n"

_HANDOFF_VALID = """---
title: Live handoff, clean
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
handoff_id: "hnd-live-clean-aaa111"
---
Body.
"""

_HANDOFF_DANGLING = """---
title: Live handoff, dangling predecessor
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: state/handoffs/does-not-exist-anywhere.md
handoff_id: "hnd-live-dangling-bbb222"
---
Body.
"""

_HANDOFF_ID_DISAGREES = """---
title: Live handoff, predecessor_id disagrees with predecessor path
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: state/handoffs/live-clean.md
predecessor_id: "hnd-archived-target-ccc333"
handoff_id: "hnd-live-disagree-ddd444"
---
Body.
"""

_HANDOFF_ID_RESOLVES_ARCHIVED = """---
title: Live handoff, predecessor_id resolves into the archive
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: none
predecessor_id: "hnd-archived-target-ccc333"
handoff_id: "hnd-live-resolves-eee555"
---
Body.
"""

_ARCHIVED_HANDOFF = """---
title: Archived handoff
created: 2025-12-01
branch: work/test/2025-12-01
status: consumed
predecessor: none
handoff_id: "hnd-archived-target-ccc333"
---
Body.
"""

_KIND_REROUTED = """---
title: Walked under the decision glob, claimed by its kind as a handoff
kind: session-handoff
---
Body.
"""

_UNREACHED_TWO_LEVEL_GLOB = """---
title: Under a two-level non-recursive glob the walk never descends into
status: not-a-real-status
---
Body.
"""

_FILES: dict[str, str] = {
    'docs/plans/2026-01-01-valid.md': _VALID_PLAN,
    'docs/plans/2026-01-02-invalid.md': _INVALID_PLAN,
    'docs/plans/2026-01-03-unregistered-kind.md': _UNREGISTERED_KIND,
    'docs/plans/2026-01-04-dup-keys.md': _DUP_KEYS,
    'docs/plans/2026-01-05-bare-date.md': _BARE_DATE_IN_STRING_FIELD,
    'docs/plans/INDEX.md': _SKIPPED_NO_FRONTMATTER,
    'docs/plans/2026-01-01-valid.prior-art-check.md': _SIDECAR,
    'docs/plans/2026-01-06-notes.yaml': _YAML_FILE,
    'state/handoffs/live-clean.md': _HANDOFF_VALID,
    'state/handoffs/live-dangling.md': _HANDOFF_DANGLING,
    'state/handoffs/live-disagree.md': _HANDOFF_ID_DISAGREES,
    'state/handoffs/live-resolves.md': _HANDOFF_ID_RESOLVES_ARCHIVED,
    'archive/handoffs/2025-12/archived-target.md': _ARCHIVED_HANDOFF,
    'docs/decisions/2026-01-07-kind-rerouted.md': _KIND_REROUTED,
    '.coordinator-local/subagent-share/s1/unreached.md': _UNREACHED_TWO_LEVEL_GLOB,
}


def _build_tree(root: Path) -> Path:
    for rel, text in _FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    _git(root, 'init', '-q')
    _git(root, 'config', 'user.email', 'lint@example.invalid')
    _git(root, 'config', 'user.name', 'lint')
    _git(root, 'config', 'commit.gpgsign', 'false')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-q', '-m', 'fixture')
    return root


def _sweep(root: Path, capsys, *flags: str) -> tuple[int, dict]:
    capsys.readouterr()
    rc = main(['--root', str(root), '--json', *flags])
    return rc, json.loads(capsys.readouterr().out)


def _by_file(report: dict) -> dict:
    return {v['file']: v for v in report['violations']}


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    return _build_tree(tmp_path)


_V_KIND_REROUTED = {
    'file': 'docs/decisions/2026-01-07-kind-rerouted.md',
    'schema': 'handoff',
    'errors': [
        {'field': 'created', 'error': 'required field missing', 'hint': 'Add "created:" to the record'},
        {'field': 'branch', 'error': 'required field missing', 'hint': 'Add "branch:" to the record'},
        {'field': 'status', 'error': 'required field missing', 'hint': 'Add "status:" to the record'},
        {'field': 'predecessor', 'error': 'required field missing', 'hint': 'Add "predecessor:" to the record'},
        {
            'field': 'category',
            'error': 'required for handoffs created on or after 2026-05-29',
            'hint': 'Set category to one of: roadmap, infra, bug, docs, research, refactor, '
                    'uncategorized, queue-derived-baton',
        },
        {
            'field': 'summary',
            'error': 'required for handoffs created on or after 2026-05-29',
            'hint': 'Add a one-line summary (≤140 chars) describing the session work',
        },
    ],
}

_V_DISAGREE = {
    'file': 'state/handoffs/live-disagree.md',
    'schema': 'handoff',
    'errors': [{
        'field': 'predecessor_id',
        'error': 'predecessor_id "hnd-archived-target-ccc333" resolves to '
                 '"state/handoffs/archived-target.md", which does not match predecessor '
                 '"state/handoffs/live-clean.md" — never-silently-disagree invariant',
        'hint': 'predecessor and predecessor_id are two representations of the same edge and must '
                'name the same artifact when both are set. Either predecessor was hand-edited '
                'without updating predecessor_id, or predecessor_id is stale.',
    }],
}

_DANGLING_FIELD = {
    'field': 'predecessor',
    'error': 'dangling reference "state/handoffs/does-not-exist-anywhere.md": unresolvable in '
             'live ∪ archive-on-disk ∪ git-history (provably never-existed)',
    'hint': 'Point this field at an existing handoff, or clear it if the target was never authored.',
}

_V_DANGLING = {'file': 'state/handoffs/live-dangling.md', 'schema': 'handoff', 'errors': [_DANGLING_FIELD]}

_PLAN_INVALID_ERRORS = [
    {'field': 'author', 'error': 'required field missing', 'hint': 'Add "author:" to the record'},
    {'field': 'status', 'error': 'required field missing', 'hint': 'Add "status:" to the record'},
]

_V_INVALID_PLAN = {
    'file': 'docs/plans/2026-01-02-invalid.md',
    'schema': 'plan',
    'errors': _PLAN_INVALID_ERRORS,
}

_V_DUP_KEYS = {
    'file': 'docs/plans/2026-01-04-dup-keys.md',
    'schema': 'plan',
    'errors': [{
        'field': 'frontmatter',
        'error': "duplicate top-level key 'title' — YAML last-key-wins silently discards the earlier value",
        'hint': 'remove or merge the duplicate key before trusting either value',
    }],
}

EXPECTED_DEFAULT: dict = {
    'ok': False,
    'violations': [_V_KIND_REROUTED, _V_DISAGREE, _V_INVALID_PLAN, _V_DUP_KEYS],
    'refWarnings': [{
        'file': 'state/handoffs/live-dangling.md',
        'schema': 'handoff',
        'warning': _DANGLING_FIELD,
    }],
}

EXPECTED_STRICT: dict = {
    'ok': False,
    'violations': [_V_KIND_REROUTED, _V_DANGLING, _V_DISAGREE, _V_INVALID_PLAN, _V_DUP_KEYS],
    'refWarnings': [],
}


def test_default_sweep_cold_then_cached_matches_pinned_literal(tree, capsys):
    rc_cold, cold = _sweep(tree, capsys)
    rc_warm, warm = _sweep(tree, capsys)
    assert cold == EXPECTED_DEFAULT
    assert warm == cold
    assert rc_cold == rc_warm == 1


def test_strict_refs_sweep_cold_then_cached_matches_pinned_literal(tree, capsys):
    rc_cold, cold = _sweep(tree, capsys, '--strict-refs')
    rc_warm, warm = _sweep(tree, capsys, '--strict-refs')
    assert cold == EXPECTED_STRICT
    assert warm == cold
    assert rc_cold == rc_warm == 1


def test_bare_yaml_date_in_string_field_is_not_a_violation(tree, capsys):
    _, report = _sweep(tree, capsys)
    assert 'docs/plans/2026-01-05-bare-date.md' not in _by_file(report)


def test_skipped_files_never_appear(tree, capsys):
    _, report = _sweep(tree, capsys)
    files = {v['file'] for v in report['violations']} | {w['file'] for w in report['refWarnings']}
    assert 'docs/plans/INDEX.md' not in files
    assert 'docs/plans/2026-01-01-valid.prior-art-check.md' not in files
    assert 'docs/plans/2026-01-06-notes.yaml' not in files
    assert 'docs/plans/2026-01-03-unregistered-kind.md' not in files


def test_editing_one_file_changes_only_that_files_entry(tree, capsys):
    _, before = _sweep(tree, capsys)
    _sweep(tree, capsys)  # warm the verdict store

    target = tree / 'docs/plans/2026-01-01-valid.md'
    target.write_text(_INVALID_PLAN, encoding='utf-8')
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    _, after = _sweep(tree, capsys)
    old, new = _by_file(before), _by_file(after)

    assert 'docs/plans/2026-01-01-valid.md' not in old
    assert 'docs/plans/2026-01-01-valid.md' in new
    assert new['docs/plans/2026-01-01-valid.md']['errors'] == _PLAN_INVALID_ERRORS
    for rel in set(old) | set(new):
        if rel != 'docs/plans/2026-01-01-valid.md':
            assert new.get(rel) == old.get(rel), rel
    assert after['refWarnings'] == before['refWarnings']
