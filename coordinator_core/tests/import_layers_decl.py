"""Declared import layers and forbidden cross-layer edges for coordinator_core and coordinator/bin.

Scope: module-level imports only. A function-local import is deferred and outside the
static check by declaration; hot-path deferred imports are caught behaviourally by the
sys.modules probes, not here.
"""

from __future__ import annotations

from typing import NamedTuple

#: layer name -> module prefixes; the longest matching prefix wins.
LAYERS: "dict[str, tuple[str, ...]]" = {
    "bash_guards": ("coordinator_core.bash_guards",),
    "hooks": ("coordinator_core.hooks",),
    "core": ("coordinator_core",),
    "bin": ("coordinator.bin",),
}


class ForbiddenEdge(NamedTuple):
    """`source_layer` may not import `target_layer` at module level."""

    source_layer: str
    target_layer: str
    root_cause: str
    allowed_route: str


FORBIDDEN_EDGES: "tuple[ForbiddenEdge, ...]" = (
    ForbiddenEdge(
        source_layer="bash_guards",
        target_layer="hooks",
        root_cause=(
            "coordinator_core.hooks.__init__ eagerly imports every hook module for "
            "register_op side effects, paid on every PreToolUse(Bash) call"
        ),
        allowed_route="import the envelope builders from coordinator_core._hook_envelope",
    ),
    ForbiddenEdge(
        source_layer="core",
        target_layer="bin",
        root_cause=(
            "coordinator/bin is the launcher layer and partly ships from DoE; the engine "
            "depending on it inverts the dependency direction"
        ),
        allowed_route="move the shared helper into coordinator_core and have bin import it from there",
    ),
)


def layer_of(module: str) -> "str | None":
    """Layer owning dotted `module` by longest matching prefix, or None when unowned."""
    best_layer: "str | None" = None
    best_len = -1
    for layer, prefixes in LAYERS.items():
        for prefix in prefixes:
            if (module == prefix or module.startswith(prefix + ".")) and len(prefix) > best_len:
                best_layer, best_len = layer, len(prefix)
    return best_layer
