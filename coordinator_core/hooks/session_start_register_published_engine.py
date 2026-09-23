"""coordinator_core.hooks.session_start_register_published_engine —
SessionStart(*) op: self-heals `repos.claude_klabauter` in the machine-local
registry so a box that never ran the installer still resolves a published
engine.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/session-start-register-published-engine.py`. Its
gap: a cloud container clones every fleet repo but installs no machine-local
registry, so `_engine_root`'s ladder falls past the published-engine rung to
the sibling walk and resolves an UNSTAMPED live tree, which every op dispatch
then refuses. The published mirror (`claude-klabauter`) may be sitting right
there as a sibling; this hook registers it.

ADAPTATION (class 1): DoE's `resolve_claude_klabauter_root_with_provenance` (a shim
loaded from `_engine_root`) is replaced with this repo's own
`coordinator_core.engine_root.coordinator_engine_root_with_class()`, which
already does the identical env-rung/gate resolution IN-PROCESS (see that
function's own docstring: "wraps ... `resolve_claude_klabauter_root_with_class()`").
`_registry_value`/`_settings_home_registry_dir`/`machine_local_set` are
replaced with `coordinator_core.machine_resolver.registry_get`/`registry_set`
— the same in-process TOML reader/writer every sibling arrival in this row
uses (DR-071).

SECOND ADAPTATION — discovery. DoE's `discover_published_mirror()` scanned
for a `claude-klabauter` directory beside `_find_plugin_root`'s own
DoE-plugin-root candidate — a DoE-side "where is my OWN plugin root" question
this op has no analogue for (claude-klabauter is not itself a Claude Code plugin). This
op instead asks the one question that IS answerable from inside the engine
process: is the tree THIS module's own `__file__` resolves under itself a
stamped, `claude-klabauter`-named root? If this op is running at all, its own
`coordinator_core` package is by definition on `sys.path` somewhere — when
that somewhere is a published mirror the caller already resolved (e.g. via
`hook-run`'s own resolution), self-identifying it is equivalent to (and
simpler than) re-deriving a sibling-walk DoE never needed once the check
moved inside the tree being checked.

Op contract: `params` is unused (mirrors the source script, which resolves
everything from its own file position). Every failure degrades to
`no_advisory()`. Idempotent and silent-when-healthy per the source script's
own contract: a box already resolving `RESOLUTION_RESOLVED_ENGINE`, or a live
working tree that is itself stamped, returns immediately with no read/write.
Never overwrites a value a real install or operator wrote.

Negative-spec:
    Does NOT write `engine.target` — that key is a DECLARATION of which
    channel the box runs, not an observation this self-heal is positioned to
    make (see the source script's own "What this hook deliberately does NOT
    write" section; unchanged here).
    Does NOT perform a sibling-directory scan — see SECOND ADAPTATION above.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.engine_root import (
    _RESOLUTION_RESOLVED_ENGINE_LITERAL as _RESOLUTION_RESOLVED_ENGINE,
    coordinator_engine_root_with_class,
)
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.machine_resolver import registry_get, registry_set

_REGISTRY_KEY = "repos.claude_klabauter"
_MIRROR_DIR_NAME = "claude-klabauter"


def is_stamped_engine_root(candidate: Path) -> bool:
    """True when `candidate` carries a readable, non-empty
    `coordinator_core/_engine_stamp`. Never raises — a read failure is
    `False`, the fail-closed direction for a predicate gating a registration.
    """
    try:
        root = Path(candidate)
        if not (root / "coordinator_core").is_dir():
            return False
        return bool((root / "coordinator_core" / "_engine_stamp").read_bytes())
    except Exception:
        return False


def _own_root() -> "Path | None":
    """This module's own repo root (`coordinator_core/hooks/<file>.py` ->
    two parents up). See module docstring's "SECOND ADAPTATION"."""
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return None


def discover_published_mirror() -> "Path | None":
    """This op's own tree, if it is a stamped `claude-klabauter` root."""
    try:
        root = _own_root()
        if root is None:
            return None
        if root.name == _MIRROR_DIR_NAME and is_stamped_engine_root(root):
            return root
    except Exception:
        return None
    return None


@register_op("hooks.session_start_register_published_engine")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        root, resolution_class = coordinator_engine_root_with_class()
    except Exception:
        return no_advisory()

    if resolution_class == _RESOLUTION_RESOLVED_ENGINE:
        return no_advisory()  # already healthy -- no read, no write, no spawn

    try:
        if root and is_stamped_engine_root(Path(root)):
            return no_advisory()  # a live tree that IS a usable build
    except Exception:
        return no_advisory()

    mirror = discover_published_mirror()
    if mirror is None:
        return no_advisory()  # nothing on this box to register

    try:
        if registry_get(_REGISTRY_KEY):
            return no_advisory()  # a real install (or an operator) already wrote it
    except Exception:
        return no_advisory()

    try:
        registry_set(_REGISTRY_KEY, str(mirror))
    except Exception:
        pass

    return no_advisory()
