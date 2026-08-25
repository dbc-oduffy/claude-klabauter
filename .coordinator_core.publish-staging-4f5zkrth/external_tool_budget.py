"""
coordinator_core.external_tool_budget — the named carve-out family for spawns of
third-party linter / scanner / CI binaries.

THE GRANT, AND WHAT IT ATTACHES TO.
`docs/decisions/DR-349-one-budget-governs-every-constructed-op.md` § Carve-outs
records this carve-out as PM-ratified: *"linter packages that run at a triggered
cadence to keep us in line, that's needed."* It is granted; nothing here argues
for it.

What that record then makes load-bearing is **cadence**. The grant attaches to
*when a tool runs*, never to *how long it takes*. A linter fired at a weekly or
pre-merge gate is carved out. The same linter on the commit or session path is
not — whatever its runtime — and the fix there is moving it off the path rather
than granting it time. DR-349 states the closing condition in as many words:
*"An author may not migrate a tool onto a hot path and carry this carve-out
along with it."*

That sentence is why this module is shaped the way it is. `Trigger` is a closed
enum whose members each declare whether they are a hot path, and a row's
`trigger` is what decides its grant. A reader asking "why is this exempt" gets
"because it fires at the weekly gate", not "because it needs 300 seconds". An
author moving a tool onto the commit path must restate its trigger, and the new
trigger takes the grant away — the migration cannot be silent, because the
registry has no way to spell "granted, and on the commit path".

ENUMERATION IS CONSTITUTIVE. A site holds the carve-out only if it is a row in
`EXTERNAL_TOOL_SITES`. A site that spawns a linter and satisfies every rationale
here but is absent from that registry has no grant — DR-349: *"A carve-out is
named in this record or it does not exist. Satisfying a rationale is not
membership."* Same discipline `docs/reference/shell-out-carve-outs.md` uses.
`bound_for` therefore raises on an unnamed site rather than defaulting.

THE CEILING ONLY EVER CUTS. `EXTERNAL_TOOL_BUDGET_SECS` is the most any one
spawn may be given. A row may declare less and keeps its smaller number;
`__post_init__` refuses a row declaring more. The census behind DR-349 found
that dials drift upward by copying, so the only structurally safe direction is
down. Cadence decides membership; the ceiling only decides how much a member
gets, and it is not a target.

A FAN-OUT IS BOUNDED AS A COMPOSITION, NOT AS A TERM. DR-349 § 4: a per-spawn
clamp lets N files take N times the budget while staying formally compliant. A
site spawning the same tool once per item stamps `sweep_deadline()` once at
entry and derives each spawn's bound from the remainder (`spawn_bound`), capped
by `EXTERNAL_TOOL_SWEEP_BUDGET_SECS`.

CONSUMER-OWNED CODE IS NOT AN EXTERNAL TOOL. § G9's named exception: a script a
publish target owns is not a linter package we pinned — it can change between
runs without our review. `consumer_owned` denies a grant on its own, and does
so independently of the trigger.

NEGATIVE-SPEC.
    - This module reads NO environment variable and exposes no dial. DR-349
      § 3: a stale `export` in a sibling repo must not reach into this engine.
      There is nothing here to set.
    - It does NOT let a duration argue for membership. A slow tool on a hot
      path is not a bigger carve-out; it is a worse one.
    - It does NOT bound `git`. Every `git` spawn in the registered sites is a
      makima-constructed op leg governed by the central budget (§ G7 /
      `coordinator_core/git/run.py`), not a third-party tool.
    - It does NOT bound a spawn of makima's own code. A child that runs
      `coordinator/bin/publish.py` is our process time and our defect if it is
      slow; see `docs/reference/external-tool-carve-outs.md` § "Considered and
      out of family".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

#: Ceiling for ONE spawn of ONE third-party binary. Not a target: a row takes
#: the smallest bound that works. Raising this line raises it for every named
#: site at once — the blast radius DR-349 § 2 chose as the deterrent.
EXTERNAL_TOOL_BUDGET_SECS: float = 120.0

#: End-to-end ceiling for a whole fan-out (one op spawning the same tool once
#: per item). Deliberately not a multiple of the per-spawn ceiling: a bound
#: computed as `per_item * len(items)` is the self-raising dial DR-349
#: § Anti-patterns names as break-class.
EXTERNAL_TOOL_SWEEP_BUDGET_SECS: float = 300.0


class Trigger(Enum):
    """What fires a spawn — the property the carve-out actually attaches to.

    Closed by design. A new way of firing a tool is a new member, and adding one
    forces its author to answer the only question that decides the grant: does a
    session or an operator wait on this? `hot=True` members carry no grant and
    exist so that migrating a tool onto a hot path is *expressible and refused*
    rather than unrepresentable and therefore invisible. Two of them
    (`SESSION_START`, `COMMIT`) have no rows today and are expected never to.

    `label` is the answer a reader gets to "why is this exempt".
    """

    WEEKLY_GATE = ("the workweek-complete fence, weekly", False)
    MERGE_GATE = ("gate.validate_invocable, pre-merge", False)
    DOD_FLOOR_REMEASURE = ("a DoD floor being re-derived", False)
    REVIEW_DISPATCH = ("a dispatched security review over a diff scope", False)
    DEPENDENCY_AUDIT = ("a dependency audit against an explicit lockfile", False)
    UPDATE_DOCS_GATE = ("the /update-docs battery", False)
    PUBLISH_ROUND = ("a percolate publish round, synchronously awaited", True)
    SESSION_START = ("session start", True)
    COMMIT = ("git commit", True)

    def __init__(self, label: str, hot: bool) -> None:
        self.label = label
        self.hot = hot


@dataclass(frozen=True)
class ExternalToolSite:
    """One named site that spawns a third-party binary, and its disposition.

    `site` anchors on the enclosing function, never a line number — repo
    convention, and the anchor `docs/reference/shell-out-carve-outs.md` uses for
    the same reason.

    `trigger` is the load-bearing field: it decides the grant, and it is what a
    reader is owed when they ask why a site is exempt. `consumer_owned` denies a
    grant independently, for artifacts a publish target may rewrite between runs.

    `remedy` is required on any row that is not granted, and must name a
    runnable change — "move the pre-CI identity leg off the synchronous publish
    path", not "reduce the timeout". It is the artifact that keeps a refusal
    from decaying into a large timeout nobody revisits.
    """

    site: str
    tool: str
    trigger: Trigger
    bound_secs: float
    why: str
    consumer_owned: bool = False
    remedy: str = ""

    def __post_init__(self) -> None:
        if self.bound_secs > EXTERNAL_TOOL_BUDGET_SECS:
            raise ValueError(
                f"external_tool_budget: {self.site} declares {self.bound_secs}s, above the "
                f"{EXTERNAL_TOOL_BUDGET_SECS}s ceiling — a site may declare a tighter bound, "
                f"never a looser one"
            )
        if not self.granted() and not self.remedy:
            raise ValueError(
                f"external_tool_budget: {self.site} fires at {self.trigger.name} and is refused "
                f"the cadence carve-out (DR-349 § Carve-outs), but names no remedy. A tool on a "
                f"hot path is moved off it, not granted time"
            )

    def granted(self) -> bool:
        """True iff this site holds the carve-out.

        Derived from the trigger, never stored, so a row cannot be marked
        granted while declaring a hot-path trigger — DR-349's "an author may not
        migrate a tool onto a hot path and carry this carve-out along with it",
        expressed as something the type system will not let you write.
        """
        return not self.trigger.hot and not self.consumer_owned

    def rationale(self) -> str:
        """The one-line answer to "why is this exempt", phrased around the
        trigger rather than the number — the framing DR-349's ratified wording
        makes load-bearing."""
        if self.granted():
            return f"carved out because it fires at {self.trigger.label}"
        reason = "spawns consumer-owned code" if self.consumer_owned else "is on a hot path"
        return f"NOT carved out: it fires at {self.trigger.label} and {reason} — {self.remedy}"


_SITES = (
    ExternalToolSite(
        site="coordinator_core/ops/gate_dimension_types.py :: _run_mypy",
        tool="mypy",
        trigger=Trigger.MERGE_GATE,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why=(
            "mypy's own analysis over the changed-file set; no rewrite of ours shortens a "
            "third-party type checker's inference pass"
        ),
    ),
    ExternalToolSite(
        site="coordinator_core/ops/gate_dimension_docstrings.py :: _run_ruff",
        tool="ruff",
        trigger=Trigger.MERGE_GATE,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why="ruff's own lint pass over the changed-file set",
    ),
    ExternalToolSite(
        site="coordinator_core/ops/gate_dimension_docstrings.py :: _run_interrogate",
        tool="interrogate",
        trigger=Trigger.MERGE_GATE,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why="interrogate's own coverage walk over the changed-file set",
    ),
    ExternalToolSite(
        site="coordinator_core/ops/dod_floor_ratchet.py :: _measure_docstrings",
        tool="interrogate",
        trigger=Trigger.DOD_FLOOR_REMEASURE,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why="interrogate over the full ported-ops path list, which is wider than a changed-file set",
    ),
    ExternalToolSite(
        site="coordinator_core/ops/run_semgrep_scan.py :: _run_semgrep",
        tool="semgrep",
        trigger=Trigger.REVIEW_DISPATCH,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why=(
            "semgrep's rule compilation plus scan; always diff-scoped, never a whole-tree "
            "sweep. Cut from a standing 300 — § G9 Condition 2 exists precisely so that 300 "
            "could not be cargo-culted onward the way 30 was"
        ),
    ),
    ExternalToolSite(
        site="coordinator_core/ops/run_shellcheck_sweep.py :: _lint_one_file",
        tool="shellcheck",
        trigger=Trigger.WEEKLY_GATE,
        bound_secs=30.0,
        why=(
            "one shellcheck spawn per tracked .sh file. Keeps its pre-existing 30 rather than "
            "taking the ceiling: the ceiling never lifts a bound that is already tighter. The "
            "sweep as a whole is held to EXTERNAL_TOOL_SWEEP_BUDGET_SECS, which is the bound "
            "this fan-out previously had none of"
        ),
    ),
    ExternalToolSite(
        site="coordinator_core/ops/run_pip_audit.py :: _run_pip_audit",
        tool="pip-audit",
        trigger=Trigger.DEPENDENCY_AUDIT,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why=(
            "pip-audit resolves every pinned dependency against a remote advisory endpoint. "
            "Cut from 180: DR-349 grants network legs no standing carve-out, so the leg lives "
            "inside the external-tool ceiling like any other spawn"
        ),
    ),
    ExternalToolSite(
        site="coordinator_core/ops/updatedocs_gates.py :: _run",
        tool="project-declared gate CLIs",
        trigger=Trigger.UPDATE_DOCS_GATE,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why=(
            "the single funnel every externally-declared gate CLI is spawned through. A gate "
            "needing less passes an explicit tighter `timeout=`; there is no path to a longer one"
        ),
    ),
    ExternalToolSite(
        site="coordinator_core/ops/percolate_ci_smoke_check.py :: run_ci_smoke_check",
        tool="<dest>/.github/scripts/run-all-checks.py",
        trigger=Trigger.PUBLISH_ROUND,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why=(
            "§ G9's named exception, and a hot-path row under the ratified wording. The spawned "
            "script is a publish target's own, editable by its owners between runs; it is not a "
            "linter package we pinned. Its prior 600 was set to stop a prompting script wedging "
            "the worker thread forever, which argues for a bound, never for a ten-minute one"
        ),
        consumer_owned=True,
        remedy=(
            "move the smoke check out-of-process so a publish round never blocks on "
            "consumer-owned code; the bound is at the ceiling meanwhile, not above it"
        ),
    ),
    ExternalToolSite(
        site="coordinator_core/ops/percolate_identity_check.py :: run_identity_check",
        tool="<dest>/.github/scripts/check-persona-names.py",
        trigger=Trigger.PUBLISH_ROUND,
        bound_secs=EXTERNAL_TOOL_BUDGET_SECS,
        why=(
            "the same consumer-owned shape as the row above, and the one G9 site with a measured "
            "hot-path cost: coordinator/bin/publish.py's own dispatch_percolate_pre_ci records "
            "27.7-33.0s on every one of 8 rows, ~225s of one publish round, all of it inside a "
            "command an operator waits on. § G9's Sites list names it; the trigger denies it"
        ),
        consumer_owned=True,
        remedy=(
            "move the pre-CI identity leg off the synchronous publish path — the end-of-run scan "
            "already covers the same tree; the bound is at the ceiling meanwhile"
        ),
    ),
)

#: Every named site, keyed by anchor. The complete answer to "what may spawn a
#: third-party binary here" — granted and refused alike, so a reader never has
#: to consult a second list to learn a site was considered.
EXTERNAL_TOOL_SITES: Dict[str, ExternalToolSite] = {entry.site: entry for entry in _SITES}

#: The sites that hold the carve-out: fired by a cadence trigger, tool pinned by
#: us, nothing waiting on them.
CARVE_OUTS: Dict[str, ExternalToolSite] = {
    site: entry for site, entry in EXTERNAL_TOOL_SITES.items() if entry.granted()
}

#: The sites that were considered and refused. Still bounded — at the ceiling,
#: not above it — and each names the change that retires the row.
REFUSED: Dict[str, ExternalToolSite] = {
    site: entry for site, entry in EXTERNAL_TOOL_SITES.items() if not entry.granted()
}

#: Triggers that put a spawn on a path something waits on. Membership here is
#: what denies a grant; it is not a property of the tool or of its runtime.
HOT_TRIGGERS = frozenset(trigger for trigger in Trigger if trigger.hot)


def bound_for(site: str) -> float:
    """The bound for a named site.

    Raises KeyError for an unnamed one rather than falling back to the ceiling:
    a default would make membership implicit, which is the exact failure mode
    DR-349's "named in this record or it does not exist" forbids.
    """
    try:
        return EXTERNAL_TOOL_SITES[site].bound_secs
    except KeyError:
        raise KeyError(
            f"external_tool_budget: {site!r} is not a named external-tool site. Add a row to "
            f"EXTERNAL_TOOL_SITES naming the Trigger that fires it; a site is not sanctioned by "
            f"resembling one that is, and a runtime is not a reason"
        ) from None


def sweep_deadline(now: Optional[float] = None) -> float:
    """Stamp the monotonic deadline a whole fan-out must finish inside.

    Called once, at op entry — not per item. `now` is injectable so a test can
    drive the remainder arithmetic without sleeping.
    """
    base = time.monotonic() if now is None else now
    return base + EXTERNAL_TOOL_SWEEP_BUDGET_SECS


def spawn_bound(site: str, deadline: float, now: Optional[float] = None) -> float:
    """The bound for one spawn inside a fan-out: the site's own bound, or what
    is left of the sweep deadline, whichever is smaller.

    Returns 0.0 once the deadline has passed, which callers treat as "stop
    spawning" — never as "spawn with no bound".
    """
    current = time.monotonic() if now is None else now
    return max(0.0, min(bound_for(site), deadline - current))
