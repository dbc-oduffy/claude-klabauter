"""coordinator_core.install.engine_root_for_install — the one root the
install uses for BOTH the warm server and the door.

Spec backlink: docs/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C1.md
(F3 CORRECTED, supersedes the original F3 finding and the "relocate the
ladder into coordinator_core" decision that followed it).

THIS MODULE IS A COMPOSITION, NOT A THIRD RESOLVER — true as written, not
aspirational. It answers "which root should an install use, before any
process is running?" by calling exactly two existing resolvers, never by
re-deriving a path from `__file__`, a registry read, or a pointer-file read
itself:

  - `coordinator_core.engine_root.published_engine_mirror_path()` — the
    published branch. Resolves the registered `repos.claude_klabauter` key
    directly from the machine-local registry (no subprocess, no pointer-file
    read), confirms `<root>/coordinator_core` exists, and requires a valid
    engine build stamp before returning anything. Its own docstring states
    the "single-implementation property" this module reuses rather than
    re-derives.
  - `coordinator_core.warm.engine_root.current_engine_clone()` /
    `is_engine_root()` — the self-rooted branch, for the klabauter-front
    case where the caller's own process IS a stamped published mirror (the
    mirror is self-rooted). `current_engine_clone()` is the existing
    depth-independent anchor for "the clone this process is running from";
    `is_engine_root()` is the stamp predicate that turns that anchor into a
    validated root.

Three typed outcomes, none raising:
  - `kind="published"` — a stamped published root, from
    `published_engine_mirror_path()`.
  - `kind="self-rooted"` — the caller's own root, when IT is itself stamped
    (the klabauter-front case). Note `published_engine_mirror_path()`
    naturally returns the SAME path when run from inside a self-registered
    mirror, since the registry key is machine-wide, not per-repo — this
    branch exists for the case where the registry key is not (yet)
    registered but the caller is demonstrably a stamped engine root anyway.
  - `kind="none"` — no published engine on this box: both resolvers came
    back empty/unstamped. Carries a runnable remediation string.

NEGATIVE SPEC:
  - Never returns the live claude-klabauter checkout as an engine root answer.
  - Never invents a path.
  - Never reads `$LOCALAPPDATA`, `~/.cache`, or any literal — settings-home
    comes from `coordinator_core._settings_home.settings_home()` (reached
    only indirectly, through the two composed resolvers above).
  - Never reads `.claude-klabauter-root`/`.claude-klabauter-root` pointer files
    directly, and never shells out to `machine-local` — those are exactly
    the second-ladder regression this module's F3 finding closes.
  - Imports of `coordinator_core.engine_root` and `coordinator_core.warm.
    engine_root` (which pulls in `warm.skew`) are deferred into the function
    body, not module-level — `scripts/setup.py` imports some
    `coordinator_core.install` modules before dependency install, and this
    module must not force those deferred deps to resolve at import time
    (eng-director F11).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

EngineRootKind = Literal["published", "self-rooted", "none"]


@dataclass(frozen=True)
class InstallEngineRoot:
    """The install's answer to "which engine root do I use?".

    `root` is `None` iff `kind == "none"`; `remediation` is populated only
    for `kind == "none"` and is `None` otherwise.
    """

    kind: EngineRootKind
    root: Optional[Path]
    remediation: Optional[str]


_REMEDIATION = (
    "engine_root_for_install: no published engine root found on this "
    "machine, and this process is not itself a stamped engine root.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.claude_klabauter /path/to/published/engine\n"
    "    Re-run the publish step to produce and register a stamped engine "
    "build.\n"
    "  Reference: coordinator_core/engine_root.py :: published_engine_mirror_path()"
)


def resolve_engine_root_for_install() -> InstallEngineRoot:
    """Resolve the engine root the install must use for BOTH the warm
    server and the door.

    Composes exactly two existing resolvers (see module docstring). Never
    raises.
    """
    from coordinator_core.engine_root import published_engine_mirror_path
    from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

    published = published_engine_mirror_path()
    if published:
        return InstallEngineRoot(kind="published", root=Path(published), remediation=None)

    self_root = current_engine_clone()
    if is_engine_root(self_root):
        return InstallEngineRoot(kind="self-rooted", root=self_root, remediation=None)

    return InstallEngineRoot(kind="none", root=None, remediation=_REMEDIATION)
