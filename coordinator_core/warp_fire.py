"""The one next step every EM-facing guard and the sizing output name: submit the ask to the engine.

The EM's whole act on an engineering ask is one `Workflow` call naming the plugin's
`engine-ask` stub with the PM's words verbatim (or a routed sizing) as `args`. The
PreToolUse(Workflow) hook (`coordinator_core.hooks.submit_engine_ask`) emits the warp run
from those args and rewrites the call to fire it, so the call the EM makes IS the running
Workflow: it sizes, plans, executes, reviews, documents and commits in-run, and its completion
is the EM's one wake. Guards that stop an EM from working by hand name this and nothing else,
so no guard leaves the next dispatch to the EM's judgment. Leaf module: no imports.
"""

from __future__ import annotations

import json
from typing import Optional

#: Routes a single warp run carries end to end (the ask script's XS, S and M+ arms).
FIRE_ROUTES = frozenset({"dispatch", "spec-dispatch", "plan"})

#: The stub's `meta.name`. The harness resolves `name` before PreToolUse runs, so the stub
#: must ship as a `.js` file in the plugin's `workflows/` (only `.js` is loaded there) and
#: is addressed `<plugin>:<meta.name>`. Any plugin namespace is accepted by the hook.
SUBMIT_STUB_NAME = "engine-ask"
SUBMIT_WORKFLOW_NAME = f"coordinator:{SUBMIT_STUB_NAME}"

_ASK_PLACEHOLDER = "<the PM's words, verbatim>"


def is_submit_name(name: object) -> bool:
    """True for the stub's bare or plugin-namespaced workflow name."""
    return isinstance(name, str) and (
        name == SUBMIT_STUB_NAME or name.endswith(":" + SUBMIT_STUB_NAME)
    )


def fire_command(sizing_rel: Optional[str] = None) -> str:
    """The submit call: from a routed sizing when one exists, else from the PM's ask."""
    args = {"sizing": sizing_rel} if sizing_rel else {"ask": _ASK_PLACEHOLDER}
    return f"Workflow({{name: {json.dumps(SUBMIT_WORKFLOW_NAME)}, args: {json.dumps(args)}}})"


def fire_step(sizing_rel: Optional[str] = None) -> str:
    """One sentence: make the submit call, once."""
    return f"Next step, the only one: call `{fire_command(sizing_rel)}`, once."
