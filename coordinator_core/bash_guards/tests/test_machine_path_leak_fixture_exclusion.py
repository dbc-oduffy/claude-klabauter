"""Check 11 (machine-path-leak) scans live settings surfaces, not test fixtures.

Regression pin: ``coordinator/tests/fixtures/stranded-claude/F-truncated-json/
settings.json`` is deliberately truncated JSON (fixture for
``coordinator/bin/tests/test_break_glass.py``). Check 11's staged-file selector
matched it on filename alone, and
``commit_tripwires.check_machine_path_leak``'s JSONDecodeError branch reports
unparseable JSON as a hard violation -- so staging that fixture hard-blocked
every Bash-tool commit in the repo, on a path with nothing to do with the
committer's own scope.

Pure Python -- no git repo, no subprocess.

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py ::
_is_live_settings_json
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.dispatch_checks import _is_live_settings_json


@pytest.mark.parametrize(
    "rel_path",
    [
        "settings.json",
        ".claude/settings.json",
        "coordinator/settings.json",
        "some/deep/nested/settings.json",
    ],
)
def test_live_settings_surfaces_are_scanned(rel_path):
    assert _is_live_settings_json(rel_path) is True


@pytest.mark.parametrize(
    "rel_path",
    [
        "coordinator/tests/fixtures/stranded-claude/F-truncated-json/settings.json",
        "tests/fixtures/settings.json",
        "coordinator_core/bash_guards/tests/fixtures/x/settings.json",
    ],
)
def test_fixture_settings_json_is_excluded(rel_path):
    assert _is_live_settings_json(rel_path) is False


@pytest.mark.parametrize(
    "rel_path",
    [
        "settings.local.json",
        "coordinator/settings.json.bak",
        "my-settings.json.tmpl",
    ],
)
def test_non_settings_json_paths_are_not_selected(rel_path):
    assert _is_live_settings_json(rel_path) is False
