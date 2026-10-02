"""coordinator_core.testing.content_root -- shared content-root resolver for tests.

One `resolve_content_root()` for every test needing the coordinator content
checkout, backed by `coordinator_core.content_root.read_content_root()` with a
`CLAUDE_KLABAUTER_TEST_CONTENT_ROOT` override on top.

Negative-spec: does not validate the resolved root; callers apply their own
site-specific existence gate.
"""

from __future__ import annotations

import os

from coordinator_core.content_root import read_content_root


def resolve_content_root() -> str:
    """`CLAUDE_KLABAUTER_TEST_CONTENT_ROOT` if set, else `read_content_root()`; "" when nothing resolves."""
    override = os.environ.get("CLAUDE_KLABAUTER_TEST_CONTENT_ROOT")
    if override:
        return override
    return read_content_root() or ""


def content_root_and_present() -> tuple[str, bool]:
    root = resolve_content_root()
    return root, bool(root) and os.path.isdir(root)
