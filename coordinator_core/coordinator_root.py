"""coordinator_core/coordinator_root.py — resolve a coordinator-claude root's
plugin-root shape.

stdlib-only contract: this module imports ONLY stdlib (`pathlib`). It is
loaded by `scripts/setup.py` — the bootstrap installer that runs BEFORE
`coordinator_core`'s own third-party dependencies are provisioned — via an
in-function import, mirroring `coordinator_core.install.ensure_venv`'s own
stdlib-only contract (see that module's docstring for the same rationale). A
future edit that casually adds a dependency here breaks the installer
bootstrap; don't.
"""

from __future__ import annotations

from pathlib import Path


def _resolve_plugin_root_for_machine_local(coord_path: Path) -> Path | None:
    """Map a resolved coordinator-claude root onto the `plugin_root` shape
    `coordinator_core.install._shared.resolve_machine_local_cli` expects
    (the dir directly containing `templates/bin/_machine_local.py` and
    `bin/machine-local`) — a coordinator-claude root can be shaped two ways:
    (a) the OSS mirror clone, plugin_root == coord_path itself, or (b) a coordinator-claude
    dev-clone, where the coordinator plugin source (and its bin/) lives
    under a `coordinator/` subdir. Returns None if neither shape matches,
    in which case `resolve_machine_local_cli` falls back to an on-PATH
    lookup only.

    negative-spec: probe for the artifact this function actually needs
    (`templates/bin/_machine_local.py`), never for a doctrine file standing in
    as a proxy for it. This branch tested `coordinator/CLAUDE.md` until coordinator-claude
    retired that file (`e8f9051db`); the dev clone then resolved to None, and
    `install_bin_forwarders` skipped with a "no templates/ dir" advisory while
    `templates/bin/_machine_local.py` sat right there — so the documented
    installer exited 0 having silently installed no forwarders at all. A proxy
    probe can go stale without the thing it stands for moving; a probe for the
    real artifact cannot."""
    for candidate in (coord_path / "coordinator", coord_path):
        if (candidate / "templates" / "bin" / "_machine_local.py").is_file():
            return candidate
    if (coord_path / ".claude-plugin" / "plugin.json").is_file():
        return coord_path
    return None
