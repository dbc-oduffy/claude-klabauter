"""
coordinator_core.ops.detect_plugin_layout — JSON-RPC "detect.plugin_layout"
operation.

Purpose: determine whether a given plugin directory is checked out as a
FLAT publish-repo (a standalone `coordinator-claude/` clone) or a NESTED
working-repo (living under `~/.claude/plugins/coordinator/`
inside a meta-repo). The discriminator is presence of `docs/install/AGENT.md`
relative to the supplied plugin root — flat layouts ship that file directly
under the plugin root; nested layouts do not (`AGENT.md` lives one level up,
outside the plugin subtree, in the meta-repo).

Port source: `skills/setup/SKILL.md` § Step 1 ("Detect layout"), a bash
heuristic (`BASH_SOURCE`/`dirname`/`pwd` self-location + a single `[ -f ... ]`
test). Self-location by shell-path arithmetic is exactly the "boilerplate
evaporates" case this plan's oracle names it as: this op takes `plugin_root`
as an explicit caller-supplied parameter instead of re-deriving "my own
location" via `__file__`, so there is no path arithmetic to port at all —
just the existence check.

Idempotency (AC7): pure read-only filesystem existence check. Identical
`plugin_root` input always yields identical output; no state is written, so
a second invocation is trivially a safe no-op. No DEC-7 note required (rated
"none" on the idempotency-hazard column of op-classification.tsv).

Negative-spec (preserved verbatim from the bash oracle — do NOT "fix" while
porting): the oracle does not distinguish "AGENT.md missing because this is
a nested layout" from "AGENT.md missing because the plugin_root is wrong or
half-installed" — a missing `AGENT.md` always reads as "nested", never as an
error. This op reproduces that: there is no error/indeterminate state, only
"flat" or "nested".
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ipc import register_op

_FLAT_MARKER_RELPATH = Path("docs") / "install" / "AGENT.md"


def classify_plugin_layout(plugin_root: Path) -> str:
    """Pure decision logic — no I/O beyond a single existence check.

    Returns "flat" when `<plugin_root>/docs/install/AGENT.md` exists,
    "nested" otherwise. Mirrors the bash oracle's `[ -f "${FLAT_AGENT_MD}" ]`
    branch exactly — a missing marker always means "nested", never an error.
    """
    marker = plugin_root / _FLAT_MARKER_RELPATH
    return "flat" if marker.is_file() else "nested"


@register_op("detect.plugin_layout")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'detect.plugin_layout' handler.

    Params:
        plugin_root (str, required): the plugin directory to classify. An
            explicit caller-supplied path — NOT derived from `repo_root` or
            `__file__` — since the whole point of this op is to classify
            SOME plugin checkout (any plugin dir), not the caller's own
            dispatching worktree. Mirrors the `cartography.*`/
            `workflow.validate` `target_root` pattern per the op's
            scope-verdict.

    Returns: {"layout": "flat" | "nested"}.
    """
    plugin_root = Path(params["plugin_root"])
    return {"layout": classify_plugin_layout(plugin_root)}
