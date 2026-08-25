"""Check 11 (machine-path-leak) scans every settings.json; a fixture path
suppresses only the unparseable-JSON finding.

Regression pin: ``coordinator/tests/fixtures/stranded-claude/F-truncated-json/
settings.json`` is deliberately truncated JSON (fixture for
``coordinator/bin/tests/test_break_glass.py``). Check 11's staged-file selector
matched it on filename alone, and
``commit_tripwires.check_machine_path_leak``'s JSONDecodeError branch reports
unparseable JSON as a hard violation -- so staging that fixture hard-blocked
every Bash-tool commit in the repo, on a path with nothing to do with the
committer's own scope.

The first fix excluded fixture settings.json from the scan ENTIRELY. Chain
review of `abbbac67d` (code-reviewer, 2026-08-18) found that grant wider than
its justification: a genuine machine-path leak in a file that happened to sit
under a fixtures path would silently skip the scan, which is the failure mode a
leak guard exists to prevent. The exclusion is now scoped to the finding rather
than the file -- pathname decides only whether the PARSE failure is tolerable.
The second class of test below is the half that would have caught the
over-grant.

Pure Python -- no git repo, no subprocess.

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py ::
_is_settings_json, _fixture_suppressible_detail
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _fixture_suppressible_detail,
    _is_settings_json,
)

_PARSE_ERROR = "ERROR — failed to parse x/settings.json as JSON: Expecting value"
_LEAK = (
    "VIOLATION: x/settings.json: machine-specific path in JSON leaf\n"
    "  Leaf path : env.COORDINATOR_HOME\n"
    "  Value     : C:\\Users\\someone\\dev\\repo"
)


@pytest.mark.parametrize(
    "rel_path",
    [
        "settings.json",
        ".claude/settings.json",
        "coordinator/settings.json",
        "some/deep/nested/settings.json",
        # Fixtures are SELECTED now -- they were the whole prior exclusion.
        "coordinator/tests/fixtures/stranded-claude/F-truncated-json/settings.json",
        "tests/fixtures/settings.json",
        "coordinator_core/bash_guards/tests/fixtures/x/settings.json",
    ],
)
def test_every_settings_json_is_scanned(rel_path):
    assert _is_settings_json(rel_path) is True


@pytest.mark.parametrize(
    "rel_path",
    [
        "settings.local.json",
        "coordinator/settings.json.bak",
        "my-settings.json.tmpl",
    ],
)
def test_non_settings_json_paths_are_not_selected(rel_path):
    assert _is_settings_json(rel_path) is False


@pytest.mark.parametrize(
    "rel_path",
    [
        "coordinator/tests/fixtures/stranded-claude/F-truncated-json/settings.json",
        "tests/fixtures/settings.json",
        "coordinator_core/bash_guards/tests/fixtures/x/settings.json",
    ],
)
def test_fixture_path_suppresses_the_parse_error(rel_path):
    """The original incident: a truncated fixture must not hard-block the repo."""
    assert _fixture_suppressible_detail(rel_path, _PARSE_ERROR) is True


@pytest.mark.parametrize(
    "rel_path",
    [
        "coordinator/tests/fixtures/stranded-claude/F-truncated-json/settings.json",
        "tests/fixtures/settings.json",
        "coordinator_core/bash_guards/tests/fixtures/x/settings.json",
    ],
)
def test_fixture_path_does_not_suppress_a_real_leak(rel_path):
    """The over-grant the first fix shipped: a machine path under a fixtures
    tree still blocks. Without this, the guard can be dodged by filing the
    leaking file one directory deeper."""
    assert _fixture_suppressible_detail(rel_path, _LEAK) is False


@pytest.mark.parametrize("detail", [_PARSE_ERROR, _LEAK])
def test_non_fixture_path_suppresses_nothing(detail):
    """A live settings surface gets no suppression of any kind -- including the
    parse error, which on a real settings.json is a finding, not noise."""
    assert _fixture_suppressible_detail(".claude/settings.json", detail) is False
