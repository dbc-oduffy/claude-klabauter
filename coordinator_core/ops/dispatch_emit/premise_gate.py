"""Pure evaluator for ``gate_kind: epistemic-premise`` ``depends_on`` edges.

Interface:

* Sidecar key ``premises: {<row_id>: {verdict: yes|no, ref: <repo-relative path or sha>,
  recorded_at}}`` in the plan's evidence sidecar (``plan_tasks_mutate.evidence_sidecar_path``),
  beside ``rows`` and ``em_discharged``. Only this structured key counts; premise-row prose is
  never parsed.
* ``read_premise_verdicts(plan_file) -> dict``: the ``premises`` mapping; never raises; a missing
  or unparsable sidecar, or a non-dict value, reads as ``{}`` (mirrors ``read_em_discharges``).
* ``plan_opted_in(plan_file) -> bool``: True when the sidecar carries a ``premises:`` key, or the
  plan frontmatter ``created`` is on or after ``PREMISE_GATE_CUTOVER``. Never raises.
* ``evaluate_premise_gate(raw_row, rows_by_id, verdicts, opted_in) -> GateOutcome`` where
  ``GateOutcome(action: dispatch|close_wont_do|wait, cite: Optional[dict])``.
  ``cite`` is ``{chunk, verdict, ref}`` (``verdict`` is ``"no"`` or ``"absent"``) for
  ``close_wont_do``; ``{chunk}`` for ``wait``; ``{chunk, reason: "legacy-absent"}`` for a
  ``dispatch`` that a not-opted-in plan reached with the verdict absent (today's
  sequencing-only behaviour); ``None`` for any other ``dispatch``.
* ``PREMISE_RECORD_BRIEF_LINE``: the one line ask_stage appends to a premise row's brief.

Decision per row, over its epistemic-premise edges only: any edge whose premise row is finished
(``disposition`` other than ``open``) and recorded ``no`` -> ``close_wont_do``; recorded absent
-> ``close_wont_do`` when ``opted_in``, else legacy-absent dispatch; the first closing edge is
cited. Otherwise any premise row still ``open`` -> ``wait`` (a verdict never closes a row
while its premise row is open); else ``dispatch``. A premise row missing from ``rows_by_id`` is skipped: an
unknown id is a plan-shape defect owned by the spine validators, not a verdict. YAML 1.1 reads
an unquoted ``yes``/``no`` as a bool; both spellings normalise to ``"yes"``/``"no"``.

Dispatch seam (known limit):``ask_stage.stage`` parses the spine once (``_PlanParse`` via
``read_spine``) and computes ``waves = cap_waves(build_waves(rows), sibling_split.runtime_cap())``
once; nothing in it re-reads the spine or the sidecar between waves, and it composes briefs
rather than spawning executors. A premise recorded during the run is therefore invisible to
the already-built waves. C4 must evaluate each wave immediately before it spawns, re-reading
``read_premise_verdicts`` and the premise rows' current dispositions at that point, in
addition to the ``read_spine`` exclusion for the first pass.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import yaml

PREMISES_KEY = "premises"
EPISTEMIC_PREMISE = "epistemic-premise"
LEGACY_ABSENT = "legacy-absent"
PREMISE_GATE_CUTOVER = datetime.date(2026, 10, 27)

PREMISE_RECORD_BRIEF_LINE = (
    "This row is the premise of an epistemic-premise gate: before you report done, run "
    "plan_tasks_mutate `premise-record {id, verdict: yes|no, ref}` with your verdict "
    "and its evidence ref."
)

Action = Literal["dispatch", "close_wont_do", "wait"]

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class GateOutcome:
    """``action`` plus its citation; see the module docstring for the ``cite`` shapes."""

    action: Action
    cite: Optional[dict] = None


def _load_sidecar(plan_file: Path):
    try:
        sidecar = Path(plan_file)
        sidecar = sidecar.with_name(sidecar.stem + ".evidence.yaml")
        return yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        return None


def read_premise_verdicts(plan_file: Path) -> dict:
    """``{row_id: {verdict, ref, recorded_at}}``; ``{}`` on any read or parse failure."""
    loaded = _load_sidecar(plan_file)
    entries = loaded.get(PREMISES_KEY) if isinstance(loaded, dict) else None
    return entries if isinstance(entries, dict) else {}


def plan_opted_in(plan_file: Path) -> bool:
    """True when the sidecar has a ``premises:`` key or ``created`` >= ``PREMISE_GATE_CUTOVER``."""
    loaded = _load_sidecar(plan_file)
    if isinstance(loaded, dict) and PREMISES_KEY in loaded:
        return True
    try:
        match = _FRONTMATTER.match(Path(plan_file).read_text(encoding="utf-8"))
        front = yaml.safe_load(match.group(1)) if match else None
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        return False
    created = front.get("created") if isinstance(front, dict) else None
    if isinstance(created, datetime.datetime):
        created = created.date()
    elif isinstance(created, str):
        try:
            created = datetime.date.fromisoformat(created.strip()[:10])
        except ValueError:
            return False
    return isinstance(created, datetime.date) and created >= PREMISE_GATE_CUTOVER


def _normalise(verdict) -> Optional[str]:
    if verdict is True:
        return "yes"
    if verdict is False:
        return "no"
    if isinstance(verdict, str):
        v = verdict.strip().lower()
        if v in ("yes", "no"):
            return v
    return None


def evaluate_premise_gate(
    raw_row: dict, rows_by_id: dict, verdicts: dict, opted_in: bool
) -> GateOutcome:
    """May ``raw_row`` dispatch, given its epistemic-premise premises' recorded verdicts."""
    edges = raw_row.get("depends_on") if isinstance(raw_row, dict) else None
    waiting: Optional[dict] = None
    legacy: Optional[dict] = None
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, dict) or edge.get("gate_kind") != EPISTEMIC_PREMISE:
            continue
        chunk = edge.get("chunk")
        premise_row = rows_by_id.get(chunk) if isinstance(rows_by_id, dict) else None
        if not isinstance(premise_row, dict):
            continue
        disposition = premise_row.get("disposition") or "open"
        if disposition == "open":
            waiting = waiting or {"chunk": chunk}
            continue
        entry = verdicts.get(chunk) if isinstance(verdicts, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        verdict = _normalise(entry.get("verdict"))
        if verdict == "yes":
            continue
        if verdict is None and not opted_in:
            legacy = legacy or {"chunk": chunk, "reason": LEGACY_ABSENT}
            continue
        ref = entry.get("ref")
        return GateOutcome(
            "close_wont_do",
            {"chunk": chunk, "verdict": verdict or "absent", "ref": ref if verdict else None},
        )
    if waiting is not None:
        return GateOutcome("wait", waiting)
    return GateOutcome("dispatch", legacy)
