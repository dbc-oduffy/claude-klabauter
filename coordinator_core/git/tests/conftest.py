"""Shared, SPAWN-FREE constants for
`coordinator_core.git.tests.test_checkin_surface_fixtures`.

WHY THIS FILE HOLDS NO FIXTURE. A `conftest.py` spawn site is untierable by
construction -- `pytest.mark.cadence`/`pytest.mark.spawns_process` only
tiers the test item that declares it, and pytest INJECTS a fixture rather
than calling it, so no marker on a test function ever reaches a spawn
buried in a `conftest.py`-defined fixture (`coordinator_core/tests/
test_no_new_spawning_tests.py`'s own Rule 4 note: "a `conftest.py` is
exempt from Rule 4 and must instead hold NO spawn site at all"). The repo
factories that DO spawn `git` therefore live in the sibling test module
itself, under its own module-level `pytestmark`, and this file carries only
the data both the positive and negative fixtures need to stay in sync.

Spec backlink: docs/plans/2026-08-27-something-must-commit-ceremony-commit-
v2.md, chunk C2.
"""

from __future__ import annotations

#: Transcribed from this repo's own root `.gitattributes` -- the patterns
#: that govern this repo's real checkin surface. Kept to the directives
#: that matter for checkin conversion (`text`/`-text`/`eol=`); the prose
#: comments in the real file are not fixture-relevant.
REAL_GITATTRIBUTES = (
    "*.cmd text eol=crlf\n"
    "*.ps1 text eol=crlf\n"
    "*.sha text eol=lf\n"
    "*.diff text eol=lf\n"
    "*.patch text eol=lf\n"
    "*.sh text eol=lf\n"
    "**/_goldens/** -text\n"
)
