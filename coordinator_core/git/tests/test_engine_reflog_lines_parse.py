"""An engine ref move writes a reflog line git can read.

Machine-a 2026-11-06: 3,837 of 8,482 `.git/logs/HEAD` lines carried `Name <email>` with no
`<epoch> <tz>`, so `git reflog` skipped every engine-made commit and reflog recovery could
not see them.
"""

from __future__ import annotations


from coordinator_core.git import git_objects


def test_an_already_dated_identity_is_kept_verbatim() -> None:
    ident = "Engine <e@x> 1794007142 +0000"
    assert git_objects._reflog_identity(ident) == ident
