"""Teammate-presence probe for a Group-EM session.

PURPOSE. Holding the Group EM Group-EM obliges the session to be running two
standing teammates -- `coordinator:group-em-assistant` (the warm assistant)
and the fleet watcher. Until this module existed, that obligation was purely
doctrinal: it was named only in DoE-claude's `skills/group-em/SKILL.md`, and
a Group-EM that simply skipped the dispatch produced no error, no warning, and
no record. `groupem.enter` re-runs on every tick and already emits
`digest["gate_declaration_required"]`, the shape for an obligation the EM
cannot silently skip; this module supplies the same shape for teammates, so
a Group-EM session holding neither agent carries a VISIBLY UNMET OBLIGATION
instead of a silence.

The engine can only ASSERT. It cannot dispatch an agent on the session's
behalf -- dispatch is the session's own act -- so the assertion is the whole
engine half. Nothing here writes, spawns, or nudges.

EVIDENCE IS A DISPATCH RECORD, NEVER A CLOCK. Presence is established by the
EXISTENCE of the teammate's own subagent transcript sidecar under
`<projects>/<slug>/<group-em-session-id>/subagents/agent-<id>.meta.json`, keyed
on the agent identity that sidecar records. There is deliberately NO
staleness threshold, no mtime read, and no freshness window anywhere in this
module, and adding one is a defect, not an improvement: an obligation that
discharges on "something touched a file recently" re-derives the mtime lie
inside the very mechanism meant to catch it (see
`read_pass._transcript_mtime_epoch`'s own negative-spec for the measured
version of that failure). "This session dispatched this teammate" is the
claim; "this teammate did something lately" is a different claim this module
does not make and must not be extended to make.

TWO IDENTITY NAMESPACES PER TEAMMATE, BOTH ACCEPTED. Observed on disk
(2026-08-31, this repo's own projects tree): the assistant is dispatched by
agent TYPE -- `{"agentType": "coordinator:group-em-assistant", ...}` -- while
the fleet watcher appears as a NAMED `general-purpose` agent,
`{"agentType": "general-purpose", "name": "fleet-watch", ...}`. That is a
LAUNCH-TIME artefact, not a missing agent type: the harness enumerates agent
types once per session, and `coordinator:fleet-watch` first existed at
DoE-claude `6eb9c0051` (2026-08-31 16:01 +0100, on that repo's
`work/machine-a/2026-08-22to31`; not yet on its `origin/main`), so a session
launched before that timestamp cannot dispatch by type and falls back to a
named `general-purpose` dispatch, while one launched after dispatches by
type. Both namespaces are therefore live simultaneously across a fleet
straddling the restart, and neither is vestigial -- do NOT delete the
type-keyed set on the strength of a `general-purpose` sidecar, nor the
name-keyed set once every session has restarted. A matcher keyed on
`agentType` alone reports a pre-restart watcher permanently absent, and one
keyed on `name` alone reports the assistant permanently absent whenever it
is dispatched unnamed. Each teammate below carries BOTH an accepted-type set
and an accepted-name set, and matches on either.

THE WATCHER IS THE WORSE ABSENCE. `missing` is ordered by severity, watcher
first: an absent assistant costs the EM its own throughput, while an absent
watcher makes a STOPPED FLEET LOOK HEALTHY. The two are reported separately
and never collapsed into one boolean.

Negative-spec:
    - Never dispatches, never writes, never mutates any file. Read-only.
    - No staleness/freshness/mtime term, per the section above.
    - `unreadable` is not `absent`. A session whose subagents directory
      cannot be listed at all reports `unreadable: True` with both teammates
      unverified -- the obligation still stands (`dispatch_required` is True,
      failing toward the EM checking) but the module never claims it LOOKED
      and found nothing.
    - Bounded read: one `scandir` of one directory, capped at
      `_MAX_META_FILES` sidecars, each a sub-kilobyte JSON object, with an
      early exit once every teammate is accounted for. No git, no
      subprocess, no interpreter start, no recursion into peer sessions.

Spec backlink: state/sizings/2026-08-31-a-crowned-group-em-always-has-a-warm-assistant.yaml
    (deliverable_id dlv-a-crowned-group-em-always-has-a-warm-ass-1893f5)
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from coordinator_core.group_em.read_pass import _transcript_path_for
from coordinator_core.session import machinery_paths

#: Ordered worst-first -- see "THE WATCHER IS THE WORSE ABSENCE" above. Each
#: entry is `(key, accepted agentType values, accepted name values)`; a
#: sidecar matches on EITHER namespace.
_TEAMMATES: tuple[tuple[str, frozenset, frozenset], ...] = (
    (
        "fleet_watch",
        frozenset({"coordinator:fleet-watch"}),
        frozenset({"fleet-watch", "fleetwatch"}),
    ),
    (
        "group_em_assistant",
        frozenset({"coordinator:group-em-assistant"}),
        frozenset({"gem-assistant", "group-em-assistant"}),
    ),
)

#: Hard ceiling on sidecars read in one probe. A steady session accumulates
#: tens; this bounds the pathological case so the probe stays single-digit
#: milliseconds on a tick that runs forever.
_MAX_META_FILES = 512

_META_SUFFIX = ".meta.json"

#: The one word this module uses for how it knows. Emitted verbatim so a
#: consumer can assert on the evidence CLASS, not just the verdict.
PROBE = "subagent-dispatch-record"


def subagents_dir(repo_root: str, session_id: str) -> str:
    """The Group-EM session's own subagents directory.

    Derived from `read_pass._transcript_path_for` rather than re-encoding the
    projects slug here -- one encoder for the whole package, so a drift in
    the harness's `<projects>/<slug>/` naming is fixed in one place. The
    harness's own layout is `dirname(transcript)/stem(transcript)/subagents/`.
    """
    transcript = _transcript_path_for(session_id, repo_root)
    return os.path.join(os.path.splitext(transcript)[0], "subagents")


def _sidecar_identity(path: str) -> tuple[Optional[str], Optional[str]]:
    """`(agentType, name)` from one `.meta.json`, or `(None, None)`.

    Any failure -- unreadable, truncated, not an object -- yields
    `(None, None)` and the sidecar simply matches nothing. A malformed
    sidecar must never satisfy an obligation.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return None, None
    if not isinstance(meta, dict):
        return None, None
    agent_type = meta.get("agentType")
    name = meta.get("name")
    return (
        agent_type if isinstance(agent_type, str) else None,
        name if isinstance(name, str) else None,
    )


def presence(repo_root: str, session_id: Optional[str]) -> dict[str, Any]:
    """Whether this Group-EM session holds each standing teammate.

    Returns:
        {
          "probe": "subagent-dispatch-record",
          "subagents_dir": str | None,
          "unreadable": bool,
          "agents": {
             "<key>": {"present": bool, "dispatch_records": [str, ...]},
             ...
          },
          "missing": [str, ...],        # worst-first: fleet_watch, then assistant
          "dispatch_required": bool,    # True iff `missing` is non-empty
        }

    `dispatch_required` is the `gate_declaration_required` analogue: a
    standing, per-tick obligation flag the EM cannot discharge by ignoring.
    Unlike that field it is DERIVED (`missing != []`), never asserted
    independently, so the flag and the evidence can never disagree.
    """
    found: dict[str, list[str]] = {key: [] for key, _types, _names in _TEAMMATES}
    unreadable = False
    directory: Optional[str] = None

    if session_id and machinery_paths.safe_session_id(session_id):
        directory = subagents_dir(repo_root, session_id)
        try:
            entries = sorted(
                entry.name
                for entry in os.scandir(directory)
                if entry.name.endswith(_META_SUFFIX)
            )
        except OSError:
            # Absent or unlistable: the probe never LOOKED. Distinguished
            # from "looked, found nothing" -- see the module negative-spec.
            entries = []
            unreadable = True
        for filename in entries[:_MAX_META_FILES]:
            agent_type, name = _sidecar_identity(os.path.join(directory, filename))
            if agent_type is None and name is None:
                continue
            for key, types, names in _TEAMMATES:
                if agent_type in types or name in names:
                    found[key].append(filename[: -len(_META_SUFFIX)])
                    break
            if all(found[key] for key, _t, _n in _TEAMMATES):
                break
    else:
        unreadable = True

    agents = {
        key: {"present": bool(found[key]), "dispatch_records": found[key]}
        for key, _types, _names in _TEAMMATES
    }
    missing = [key for key, _types, _names in _TEAMMATES if not found[key]]
    return {
        "probe": PROBE,
        "subagents_dir": directory,
        "unreadable": unreadable,
        "agents": agents,
        "missing": missing,
        "dispatch_required": bool(missing),
    }
