"""
coordinator_core.orientation.abandoned_claim_signal — surfaces batons reading
`status: claimed` whose claiming session is no longer in this box's registry.

Purpose: there are TWO claim planes in this engine and only one of them has
liveness. The claim-DIRECTORY plane (`.git/coordinator-sessions/<sid>/claims/`)
has `liveness.cs_claim_holder_live`, a reaper, and a TOCTOU double-read. The
FRONTMATTER plane -- `status: claimed` / `deployment_state: in_flight` /
`claimed_by` on a handoff -- is read by nothing that asks whether the claimant
still exists. A baton can therefore read in-flight forever with a dead owner,
and "the work is progressing" is indistinguishable from "nobody has touched
this in a week" from every surface.

Observed 2026-08-30, which is why this exists: a cross-repo peer asked who owned
`state/handoffs/2026-08-30-ceremony-driven-git-maintenance.md` (claimed 13:35Z,
`deployment_state: in_flight`). Its claimant had exited; the artifact could not
say so, and neither could anything else on the box. A first sweep found 31 such
batons.

WHAT THIS DOES NOT CLAIM, and the distinction is the whole honesty of the
signal. Absence from this box's session registry is NOT proof of abandonment. A
session on ANOTHER machine, or one whose harness cross-session-inbox gate is
off, is legitimately absent while its human is mid-work. So the rendered line
says the claimant is not in this box's registry -- an observation -- and never
that the work is abandoned, a conclusion this module cannot reach. That is also
why this is REPORT-ONLY (PM ruling, 2026-08-30): releasing a claim on this
signal would race a live peer and hand their baton to someone else.

Cost shape, measured 2026-08-30 on the live corpus: 15.6 ms process time for
307 handoffs -- a 3 KB head read per file plus ONE `harness_registry.snapshot()`
for the whole set. No mtime watermark index, deliberately: `expired_grant_signal`
needs one because its corpus is ~1,637 records across three trees and its
predecessor was killed at ~425-465 ms (`state/kill-ledger.md` K-063), whereas
this corpus is one directory an order of magnitude smaller and the measurement
above is the reason the index is not built, not an assumption that it is
unnecessary. If `state/handoffs/` ever grows such that this crosses the bar, the
watermark shape is sitting next door -- copy it then, on a new measurement.

Posture follows `budget_breach_signal`/`expired_grant_signal`: render NOTHING
when every claimed baton's claimant resolves, so the section is absent on a
healthy box rather than carrying a standing "0 abandoned" line that trains the
eye to skip it. An unreachable claimant is a defect to act on, not an accepted
residual.

Cold, orientation-regen-only, matching every other `emit_*` helper in
`regenerate_cache.py` -- never `PreToolUse` or a dispatch hot path.

Fail-open throughout: an unreadable directory, a malformed record, an
unavailable registry, an import failure, or any exception at all resolves to
`""` and the section is omitted.

Negative-spec:
  - Does NOT report a claimed baton carrying no `claimed_by` at all. That is a
    different defect (a claim transition that did not stamp) and folding it in
    here would report two unrelated causes under one count, which is how a
    signal stops being actionable.
  - Does NOT read `archive/handoffs/`. An archived baton is terminal by
    construction and would false-positive forever -- the same Anti-scope
    `baton_assemble._scan_deliverable_collision` already states for the closest
    existing corpus scan.
  - Does NOT YAML-parse records. The corpus holds records `yaml.safe_load`
    refuses outright, and a classifier that parses everything crashes on a
    defect unrelated to claims. Frontmatter is read via the canonical
    `frontmatter.primitives.read_fm_field_unquoted` line reader over a bounded
    head slice (never `split_frontmatter`'s full round trip), the same
    tolerance the rest of this package keeps — `read_fm_field` does not
    YAML-parse either, so this module carries no private regex of its own.
  - Does NOT resolve per record. ONE registry snapshot serves the whole scan; a
    per-baton `resolve_address` would be the per-item amplification
    `test_no_unbatched_per_item_git_spawn.py` exists to catch.
  - Does NOT send anything, to anyone. It renders the claimant's stamped
    `claimed_by_name` for a human to act on. That label is forensic, not a routing
    key -- harness peer labels are recycled across sessions -- and a caller wanting
    to reach the claimant re-resolves `claimed_by` through
    `reachability.resolve_address` and sends to what THAT returns. A sibling
    `claimed_by_address` stamp was removed on 2026-08-30 for having no consumer;
    this module was that stamp's only plausible one and never read it.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

# Generator-provenance declaration: this module writes nothing -- it is a pure
# reader feeding one orientation-cache section.
GENERATES = []

#: Bytes of each handoff read to find its frontmatter. Frontmatter sits at the
#: top by construction; a bounded slice keeps the scan off the body entirely.
_HEAD_BYTES = 3000

#: Most abandoned claims named individually in the rendered section. The count
#: is always exact; the enumeration is capped so one bad week cannot push every
#: other orientation section off the screen.
_MAX_NAMED = 5


def _plausible_timestamp(value: Optional[str]) -> str:
    """`value` if it opens with a four-digit year, else `""`.

    Deliberately NOT a date parse — the module never parses a timestamp, it sorts
    one lexically. This rejects the "present but garbage" class only, so a value
    that reaches the rendered line looks like a date to a reader who has to trust
    a defect report.
    """
    if value and len(value) >= 4 and value[:4].isdigit():
        return value
    return ""


def _scan(handoff_dir: Path, live_session_ids) -> List[Tuple[str, str, str, str]]:
    """Return `(claimed_at, filename, claimant_label, session_id)` per claimed
    baton whose `claimed_by` is absent from `live_session_ids`, oldest first.

    `claimed_at` is carried as its raw ISO string and sorted lexically — ISO-8601
    UTC sorts correctly as text, so this needs no datetime parse and cannot raise
    on a malformed timestamp.

    THREE claim-age states, not two, and the third is why `_plausible_timestamp`
    exists (Review: code-reviewer, 2026-08-30, P2). A field that is ABSENT and one
    that is PRESENT BUT GARBAGE (`claimed_at: TBD`, `claimed_at: null`) both mean
    "no real claim time was ever stamped", and both must render as no timestamp at
    all. Before the canonical-reader substitution a `{10,}`-length regex dropped
    garbage incidentally; `read_fm_field_unquoted` accepts any non-empty scalar, so
    the check is now explicit. Without it a defect report renders `(claimed TBD)` —
    a fabricated-looking timestamp in the one section a reader must be able to
    trust. Either way the row is KEPT and sorts last under the empty string: an
    unknown claim age is still an unreachable claimant.
    """
    rows: List[Tuple[str, str, str, str]] = []
    for path in sorted(handoff_dir.glob("*.md")):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                head = handle.read(_HEAD_BYTES)
        except OSError:
            continue  # one unreadable record never sinks the scan
        if read_fm_field_unquoted(head, "status") != "claimed":
            continue
        session_id = read_fm_field_unquoted(head, "claimed_by")
        if not session_id:
            continue  # negative-spec: a claim that never stamped is a different defect
        if session_id in live_session_ids:
            continue
        rows.append(
            (
                _plausible_timestamp(read_fm_field_unquoted(head, "claimed_at")),
                path.name,
                read_fm_field_unquoted(head, "claimed_by_name") or "",
                session_id,
            )
        )
    rows.sort(key=lambda row: (row[0] == "", row[0]))
    return rows


def emit_abandoned_claims(repo_root: Path) -> str:
    """Render the ``## Abandoned claims`` section body, or ``""`` to omit it.

    ``""`` on every failure path and whenever no claimed baton has an
    unreachable claimant — "nothing to report" and "could not tell" render
    identically here by design, the same fail-open-to-silence contract every
    ``emit_*`` helper in this package keeps.
    """
    try:
        handoff_dir = Path(repo_root) / "state" / "handoffs"
        if not handoff_dir.is_dir():
            return ""

        from coordinator_core.session import harness_registry

        live_session_ids = set(harness_registry.snapshot())
        if not live_session_ids:
            # An empty registry cannot distinguish "every claimant is gone" from
            # "the registry is unreadable/unbound", and the first reading would
            # report the entire claimed corpus as unreachable on a box where the
            # harness simply never wrote records. Stay silent.
            return ""

        rows = _scan(handoff_dir, live_session_ids)
        if not rows:
            return ""

        named = rows[:_MAX_NAMED]
        lines = [
            f"- ⚠ {len(rows)} baton(s) read `status: claimed` but the claiming session is "
            f"not in this box's registry — not proof of abandonment (another machine, or a "
            f"messaging gate that is off, looks identical), but nothing is tracking them:"
        ]
        for claimed_at, name, label, session_id in named:
            who = label or f"session {session_id[:8]}"
            when = f" (claimed {claimed_at})" if claimed_at else ""
            lines.append(f"  - `{name}` — {who}{when}")
        if len(rows) > len(named):
            lines.append(f"  - …and {len(rows) - len(named)} more")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — fail-open; an orientation section never breaks regen
        return ""
