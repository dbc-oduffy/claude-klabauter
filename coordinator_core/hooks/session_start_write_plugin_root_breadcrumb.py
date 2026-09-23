"""coordinator_core.hooks.session_start_write_plugin_root_breadcrumb —
SessionStart(*) op: writes this plugin's own root to a fixed `$HOME` path so
the plugin-shipped `subagentStatusLine` command can locate a script inside
its own plugin.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude
`coordinator/hooks/scripts/session-start-write-plugin-root-breadcrumb.py`.
`${CLAUDE_PLUGIN_ROOT}` does not resolve inside a `subagentStatusLine` command
(upstream anthropics/claude-code#81320) — this hook hands that command the one
fact it cannot obtain for itself, at the one seam (a hook) where
`${CLAUDE_PLUGIN_ROOT}` DOES resolve.

ADAPTATION (class 1): DoE's `_plugin_root()` resolved via `_engine_root.
_find_plugin_root` — a `.claude-plugin/`-marker ancestor scan from this
file's own `__file__`, correct there because that script's `__file__` lived
inside the doctrine plugin tree. That scan has no analogue here: this op's
own `__file__` resolves inside the ENGINE (claude-klabauter), never inside the
doctrine plugin DoE ships. `CLAUDE_PLUGIN_ROOT`-anchored resolution is used
instead — the same substitution `bin_impl_drift.py`'s W4-C4 arrival already
made for the identical displacement (the harness sets this env var for the
invoking hook process; see that module's own arrival note).

Op contract: `params` is unused (mirrors the source script's own
`__file__`/env-based resolution, never cwd or payload fields). Writes
atomically (temp file + `os.replace`) since the consumer is a hot-path `cat`.
Idempotent: reads the current value first and writes nothing when it already
matches. Fail-open at every step — an unresolvable `$HOME`, an absent
`CLAUDE_PLUGIN_ROOT`, or an unwritable path each degrade to a silent no-op.

Negative-spec:
    Does NOT perform a `.claude-plugin/`-marker ancestor walk — see
    ADAPTATION above.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op

BREADCRUMB_RELATIVE_PATH = ".claude/.coordinator-plugin-root"


def _plugin_root() -> "str | None":
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not raw:
        return None
    try:
        return Path(raw).as_posix()
    except Exception:
        return None


def _breadcrumb_path() -> "Path | None":
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if not home or home == "~":
        return None
    return Path(home) / BREADCRUMB_RELATIVE_PATH


@register_op("hooks.session_start_write_plugin_root_breadcrumb")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        target = _breadcrumb_path()
        if target is None:
            return no_advisory()
        root = _plugin_root()
        if root is None:
            return no_advisory()
        try:
            if target.read_text(encoding="utf-8").strip() == root:
                return no_advisory()
        except OSError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(root + "\n")
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        return no_advisory()
    return no_advisory()
