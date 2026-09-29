"""
coordinator_core.testing.content_root — shared coordinator-content-repo sibling-checkout resolver for tests.

Purpose: one canonical `resolve_content_root()` for every test module across
`coordinator_core` that needs the sibling coordinator-content-repo checkout (parity oracles,
schema/records-query cross-repo fixtures, install-hook goldens) instead of each
call site independently guessing checkout depth via `Path(__file__).parents[N]`
or hardcoding a machine-local absolute path — both non-portable (the former
encodes a literal nesting depth that drifted across call sites — `parents[2]`,
`[3]`, `[4]` all appeared for the same conceptual target — the latter encodes
one user's literal home directory) and neither works on a machine other than
the one it was written on.

Wraps `coordinator_core.ops.coordinator_content_root.coordinator_content_root()` — the
already-ratified, full-ladder "resolve the coordinator-content-repo sibling root" resolver
(REPO_CONTENT_ROOT env -> machine-local registry `repos.content_root` (canonical)
-> `plugin.mirrors.coordinator-claude.live_path` fallback -> the native
`resolve_coordinator_clone.resolve_clone_root()` port, which itself falls
through to the DR-072 `.coordinator-content-root` pointer file and finally the flat
`~/.claude/plugins/coordinator-claude` layout) — NOT
`coordinator_core.content_root_pointer.read_content_root_pointer()` directly. An
earlier revision of this module wrapped the bare pointer-file read alone;
that was the wrong layer to standardize test call sites on. The registry key
and the pointer file are independently-writable state (a machine can have
`repos.content_root` set in the registry without the `.coordinator-content-root` pointer having
been (re)seeded, or vice versa on an older/partial install) and
`coordinator_content_root()` already encodes the ratified precedence between
them — wrapping the pointer file alone would silently ignore a machine that
only ever populated the registry half. Layers the `CLAUDE_KLABAUTER_TEST_CONTENT_ROOT`
test-only override on top, the pattern `coordinator_core.testing.test_collect`
established first — that module's private `_resolve_content_root` now delegates
here rather than keeping its own, now-duplicated, copy.

Port source: none — net-new (test-harness authoring).

Negative-spec:
    - Does NOT validate the resolved root looks like a real coordinator-content-repo checkout
      (e.g. a `coordinator/` subdir present) — callers apply their own
      site-specific existence gate (a schemas dir, a bin dir, a particular
      fixture file), exactly as before, since "present" means something
      different at each call site.
    - Does NOT read `CLAUDE_PLUGIN_ROOT` or any other override a caller
      established for itself before this module existed (e.g.
      `DOE_COORDINATOR_ROOT`) — a caller with its own prior override keeps
      checking that itself before falling back to this resolver.
"""

from __future__ import annotations

import os

from coordinator_core.ops.coordinator_content_root import coordinator_content_root


def resolve_content_root() -> str:
    """Resolve the sibling coordinator-content-repo checkout root for test call sites.

    Precedence: `CLAUDE_KLABAUTER_TEST_CONTENT_ROOT` env override (test-only escape hatch),
    then `coordinator_core.ops.coordinator_content_root.coordinator_content_root()`'s
    full ratified resolution ladder (see module docstring). Returns "" if
    nothing resolves — callers gate on emptiness/existence themselves.
    """
    env_override = os.environ.get("CLAUDE_KLABAUTER_TEST_CONTENT_ROOT")
    if env_override:
        return env_override
    return coordinator_content_root() or ""


def content_root_and_present() -> tuple[str, bool]:
    root = resolve_content_root()
    return root, bool(root) and os.path.isdir(root)
