"""The one next step every EM-facing guard and the sizing output name: fire the warp run.

A raw ask or a routed sizing goes through one `emit-dispatch-workflow` call whose printed
Workflow line the EM fires once; the run sizes, plans, executes, reviews, documents and commits
in-run. Guards that stop an EM from working by hand name this and nothing else, so no guard
leaves the next dispatch to the EM's judgment. Leaf module: no imports.
"""

from __future__ import annotations

from typing import Optional

#: Routes a single warp run carries end to end (the ask script's XS, S and M+ arms).
FIRE_ROUTES = frozenset({"dispatch", "spec-dispatch", "plan"})


def fire_command(sizing_rel: Optional[str] = None) -> str:
    """The emit command: from a routed sizing when one exists, else from the PM's ask."""
    if sizing_rel:
        return f"emit-dispatch-workflow --sizing {sizing_rel}"
    return 'emit-dispatch-workflow --ask "<the PM\'s ask, verbatim>"'


def fire_step(sizing_rel: Optional[str] = None) -> str:
    """One sentence: run the emit, then fire the Workflow line it prints, once."""
    return (
        f"Next step, the only one: run `{fire_command(sizing_rel)}`, then call the "
        "Workflow({...}) line it prints, once. The run sizes, plans, executes, reviews and "
        "commits; its completion is your one wake."
    )
