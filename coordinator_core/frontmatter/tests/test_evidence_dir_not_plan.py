"""docs/plans/evidence/ files are not plans; docs/plans/<date>-x.md still is."""
from __future__ import annotations

import pytest

from coordinator_core.frontmatter import schema_validate as sv

_SCHEMAS = {
    '_byKind': {},
    '_byGlob': [{'glob': 'docs/plans/**/*.md', 'schemaName': 'plan'}],
    'plan': {},
}


@pytest.mark.parametrize('path', [
    'docs/plans/evidence/foo.md',
    'docs\\plans\\evidence\\foo.md',
    'docs/plans/evidence/sub/foo.md',
    'docs\\plans\\evidence\\sub\\foo.md',
])
def test_evidence_path_not_matched_as_plan(path):
    assert sv._is_plan_dir_index_routing_excluded(path.replace('\\', '/'), None)
    assert sv.match_schema(path, None, _SCHEMAS) is None
    assert sv.match_schema_for_path(path, _SCHEMAS) is None


@pytest.mark.parametrize('path', [
    'docs/plans/2026-11-05-x.md',
    'docs\\plans\\2026-11-05-x.md',
    'docs/plans/2026-11/2026-11-05-x.md',
    'docs/plans/evidence-notes.md',
])
def test_plan_path_still_matches_plan(path):
    assert not sv._is_plan_dir_index_routing_excluded(path.replace('\\', '/'), None)
    resolved = sv.match_schema(path, None, _SCHEMAS)
    assert resolved is not None and resolved['schemaName'] == 'plan'
