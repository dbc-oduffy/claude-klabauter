"""A state/sizings operand whose slug holds write-marker words is a path, not a write."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as g

IDS = g._SIZING_RECORD_IDENTIFIERS
SLUGS = ["patch-the-guard", "tee-off", "sed-i-fix", "ed-ex", "curl-o"]


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize(
    "template",
    [
        "coordinator-safe-commit -m x -- {p}",
        "git commit -m x -- {p}",
        "git add -- {p}",
        "git diff -- {p}",
    ],
)
def test_pathspec_with_marker_word_slug_is_allowed(slug, template):
    path = f"state/sizings/2026-10-15-{slug}.yaml"
    assert not g.is_denied_bash_write(template.format(p=path), IDS)


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize(
    "template",
    [
        "echo x > {p}",
        "echo x >> {p}",
        "echo x | tee {p}",
        "cp a.yaml {p}",
        "sed -i s/a/b/ {p}",
    ],
)
def test_real_shell_writes_into_home_still_deny(slug, template):
    path = f"state/sizings/2026-10-15-{slug}.yaml"
    assert g.is_denied_bash_write(template.format(p=path), IDS)
