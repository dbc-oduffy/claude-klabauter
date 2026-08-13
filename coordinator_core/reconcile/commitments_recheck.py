"""
coordinator_core.reconcile.commitments_recheck — daily re-resolution of the
``state/cross-repo-commitments/`` ledger via ``sibling_fact``.

Purpose: the ledger (31 records, ``docs/plans/2026-07-26-structured-sibling-
evidence-gates.md`` § C12a/C12b) went stale exactly the way unstructured prose
does — one record's own title reads "(now satisfied)" beside ``status: open``,
thirteen days on. C12a backfilled a machine-parseable ``evidence:`` string
("commit-sha:<sha>" / "symbol:<module.qualname>" / "file:<path>") onto most
records; this module is the RE-RESOLUTION half — the part that actually closes
the staleness, not merely a second sweep that records "we looked". It parses
each record's ``evidence:`` convention and resolves it LIVE against the
committing sibling's disk via ``coordinator_core.sibling_fact.resolve_leg``,
never re-implementing sibling I/O of its own.

NEGATIVE SPEC (mirrors C3's ``gate_eval``): this module authors NO resolution
logic and NO verdict vocabulary of its own. It is a thin projection — observed
evidence versus the record's own recorded ``status:`` — and a mismatch
(evidence resolves truthy while ``status`` is still ``"open"``) surfaces as
``actionable: True``, mirroring C6's distinct-from-merely-aged signal. It never
invents a fourth sibling_fact primitive, never re-derives sibling I/O, and
NEVER writes ``status:`` on any record — evaluation and mutation stay separate
(D5); a human applies the flip.

An ``evidence:`` field that is unset, empty, or not shaped like the C12a
convention is "not yet resolvable", reported via ``resolvable: False`` — never
collapsed to a false ``actionable: False`` verdict that looks the same as "we
checked and it wasn't satisfied yet". The same holds for a record whose
``committed_by`` names a sibling this module has no repo-id mapping for, and
for a leg ``sibling_fact`` itself could not read (``read_ok: False`` — an
absent clone, an unreachable git ref, and so on).

Cadence seam: registered as the ``claude-klabauter.commitments.recheck`` doctor probe
(``bin/doctor-probes.toml``, ``bin/claude-klabauter-doctor-probe.py``), triage=true,
following the exact precedent of
``coordinator_core.frontmatter.schema_drift_watch`` — a probe that runs on the
daily ``--triage``/full doctor cadence so re-resolution happens ON ITS OWN,
never only via a hand-invoked verb. Deliberately NOT wired to
``handoff.reconcile_open``/gate-recheck (C4/C8): those are hand-invoked verbs
(reachable only via ``archive-stamp-cli gate-recheck-handoff`` or
``/pickup`` step 3d) and wiring re-resolution to one would reproduce the exact
"nothing re-resolves on its own" pathology this chunk exists to close.

Windows + macOS both first-class: ``pathlib.Path`` joins only, no POSIX
separators, no shell.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb § C12b
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.sibling_fact import resolve_leg

__all__ = ["recheck_commitments"]

#: Default ledger directory — repo_root/state/cross-repo-commitments.
DEFAULT_COMMITMENTS_DIR = (
    Path(__file__).resolve().parents[2] / "state" / "cross-repo-commitments"
)

# C12a's closed evidence-string convention: "<kind>:<value>". Any other shape
# (prose, a bare value, an unrecognised kind) is NOT this convention and is
# treated as "not yet resolvable", never guessed at.
_EVIDENCE_KIND_COMMIT_SHA = "commit-sha"
_EVIDENCE_KIND_SYMBOL = "symbol"
_EVIDENCE_KIND_FILE = "file"
_EVIDENCE_KINDS = frozenset(
    {_EVIDENCE_KIND_COMMIT_SHA, _EVIDENCE_KIND_SYMBOL, _EVIDENCE_KIND_FILE}
)

# committed_by -> sibling_fact repo id. Closed table, verified against the live
# ledger (2026-07-26 survey): every one of the 31 records is committed_by one of
# coordinator-claude's two EM personas — "claude-central-em" (legacy) or "coordinator-claude-em"
# (current) — both the SAME sibling clone, repo id "example_doctrine_repo". A committed_by
# this table has never seen is a new sibling, not a guessable one; it resolves
# to "not yet resolvable", not a repo-id guess.
_COMMITTED_BY_TO_REPO_ID = {
    "claude-central-em": "example_doctrine_repo",
    "coordinator-claude-em": "example_doctrine_repo",
}

# One backfilled record ("2026-07-13-doe-to-land-report-sidecar-consuming-hal-
# 43e1dbc3e01c.yaml") authored its `file:` evidence with a leading repo-name
# segment ("file:coordinator-claude/coordinator/hooks/scripts/enforce-agent-dispatch-
# mode.py") even though `sibling_fact.resolve_leg`'s `file_exists` already
# roots at the resolved clone directory — leaving the prefix in would double
# the repo directory into the resolved path. Stripping it here is a named
# correction for a known C12a corpus quirk, not a general normalization rule.
_REPO_NAME_PREFIXES_TO_STRIP: dict[str, tuple[str, ...]] = {
    "example_doctrine_repo": ("coordinator-claude/",),
}


def _repo_id_for(committed_by: Any) -> Optional[str]:
    """Map a record's `committed_by` to a `sibling_fact` repo id, or None."""
    if not isinstance(committed_by, str):
        return None
    return _COMMITTED_BY_TO_REPO_ID.get(committed_by.strip())


def _strip_known_repo_prefix(repo_id: str, rel_path: str) -> str:
    """Strip a known-quirk leading repo-name segment for `repo_id`, if present."""
    for prefix in _REPO_NAME_PREFIXES_TO_STRIP.get(repo_id, ()):
        if rel_path.startswith(prefix):
            return rel_path[len(prefix):]
    return rel_path


def _parse_evidence(evidence: Any) -> Optional[tuple[str, str]]:
    """Split a C12a `evidence:` string into `(kind, value)`, or None.

    None covers BOTH an unset/empty field and a value that does not match the
    "<kind>:<value>" convention with a recognised kind (free prose, e.g.) — the
    caller treats both identically as "not yet resolvable", never guessing a
    kind out of unstructured text.
    """
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    text = evidence.strip()
    if ":" not in text:
        return None
    kind, _, value = text.partition(":")
    kind = kind.strip()
    value = value.strip()
    if kind not in _EVIDENCE_KINDS or not value:
        return None
    return kind, value


def _leg_for(record_id: str, repo_id: str, kind: str, value: str) -> dict[str, Any]:
    """Project a parsed `(kind, value)` evidence pair onto a `sibling_fact` leg dict.

    `commit-sha` maps onto `commit_ancestor` against the sibling's `HEAD` — "has
    this commit landed on the sibling's current history?". `file` maps onto
    `file_exists` directly.

    `symbol` is the one non-literal mapping: `sibling_fact`'s primitive set is
    deliberately closed at three kinds with no live symbol-introspection
    primitive (see that module's docstring, "Deliberately NOT here") — this
    chunk is a CONSUMER of the primitive, never a fourth-kind author. A
    `symbol:<module.qualname>` leg is therefore projected onto `file_exists`
    over the qualname's OWN module file (the trailing qualname segment
    dropped) — an existence check on the containing file, the "as appropriate"
    substitution this chunk's spec calls for, not a live read of the symbol
    itself.
    """
    if kind == _EVIDENCE_KIND_COMMIT_SHA:
        return {
            "leg_id": record_id,
            "kind": "commit_ancestor",
            "repo": repo_id,
            "commit": value,
            "ref": "HEAD",
        }

    if kind == _EVIDENCE_KIND_FILE:
        rel_path = _strip_known_repo_prefix(repo_id, value)
        return {"leg_id": record_id, "kind": "file_exists", "repo": repo_id, "path": rel_path}

    # kind == _EVIDENCE_KIND_SYMBOL
    module_path, _, _qualname = value.rpartition(".")
    module_path = module_path or value
    rel_path = str(Path(*module_path.split(".")).with_suffix(".py"))
    rel_path = _strip_known_repo_prefix(repo_id, rel_path)
    return {"leg_id": record_id, "kind": "file_exists", "repo": repo_id, "path": rel_path}


def _collect_records(directory: Path) -> list[dict[str, Any]]:
    """Enumerate every `*.yaml` ledger record under `directory`.

    An unreadable or unparseable record is INCLUDED with `parse_error: True`
    and all other fields `None` — never silently dropped from the sweep
    (mirrors `coordinator_core.ops.crossrepo_closure_status.collect_commitment_
    entries`'s fail-included-not-absent posture).
    """
    if not directory.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = path.read_text(encoding="utf-8")
            doc = yaml.safe_load(raw)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            records.append(
                {
                    "entry": path.name,
                    "title": None,
                    "status": None,
                    "evidence": None,
                    "committed_by": None,
                    "parse_error": True,
                }
            )
            continue

        if not isinstance(doc, dict):
            records.append(
                {
                    "entry": path.name,
                    "title": None,
                    "status": None,
                    "evidence": None,
                    "committed_by": None,
                    "parse_error": True,
                }
            )
            continue

        records.append(
            {
                "entry": path.name,
                "title": doc.get("title"),
                "status": doc.get("status"),
                "evidence": doc.get("evidence"),
                "committed_by": doc.get("committed_by"),
                "parse_error": False,
            }
        )

    return records


def _evaluate_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project one parsed ledger record to its re-resolution result.

    Returns `{entry, title, status, resolvable, actionable, observation, reason}`.
    `observation` is the raw `sibling_fact.LegObservation` (or None when no
    sibling read was attempted) — passed through verbatim, never re-interpreted
    into a second vocabulary. `actionable` is True ONLY when a sibling read
    succeeded, its observed value is truthy, AND the record's own `status` is
    still `"open"` — the mismatch this chunk exists to surface. It is never set
    on an unresolvable record (evidence unset, repo unmapped, or a failed
    sibling read all report `resolvable: False`, `actionable: False`).
    """
    entry = record["entry"]
    title = record.get("title")
    status = record.get("status")

    if record.get("parse_error"):
        return {
            "entry": entry,
            "title": title,
            "status": status,
            "resolvable": False,
            "actionable": False,
            "observation": None,
            "reason": "ledger record unreadable or unparseable YAML — not yet resolvable",
        }

    parsed = _parse_evidence(record.get("evidence"))
    if parsed is None:
        return {
            "entry": entry,
            "title": title,
            "status": status,
            "resolvable": False,
            "actionable": False,
            "observation": None,
            "reason": "evidence: unset (or not the C12a kind:value convention) — not yet resolvable",
        }

    kind, value = parsed
    repo_id = _repo_id_for(record.get("committed_by"))
    if repo_id is None:
        return {
            "entry": entry,
            "title": title,
            "status": status,
            "resolvable": False,
            "actionable": False,
            "observation": None,
            "reason": (
                f"committed_by {record.get('committed_by')!r} has no known sibling "
                "repo-id mapping — not yet resolvable"
            ),
        }

    leg = _leg_for(entry, repo_id, kind, value)
    observation = resolve_leg(leg)

    if not observation["read_ok"]:
        return {
            "entry": entry,
            "title": title,
            "status": status,
            "resolvable": False,
            "actionable": False,
            "observation": observation,
            "reason": f"sibling read failed: {observation['error']}",
        }

    observed_truthy = bool(observation["observed"])
    actionable = observed_truthy and status == "open"
    return {
        "entry": entry,
        "title": title,
        "status": status,
        "resolvable": True,
        "actionable": actionable,
        "observation": observation,
        "reason": None,
    }


def recheck_commitments(commitments_dir: Optional[Path | str] = None) -> dict[str, Any]:
    """Re-resolve every `state/cross-repo-commitments/` record against its
    committing sibling's live disk-truth.

    Args:
        commitments_dir: ledger directory override (test isolation). None ->
            `DEFAULT_COMMITMENTS_DIR`.

    Returns:
        {"checked": int, "records": [...per-record result, see
         `_evaluate_record`...], "actionable": [...subset with actionable=True...],
         "not_yet_resolvable": [...subset with resolvable=False...]}

    NEVER AUTO-CLEARS (D5): purely a read + compute + return. No record's
    `status:` field is written, appended, or otherwise mutated by this
    function or anything it calls.
    """
    directory = Path(commitments_dir) if commitments_dir is not None else DEFAULT_COMMITMENTS_DIR
    records = _collect_records(directory)
    results = [_evaluate_record(record) for record in records]

    return {
        "checked": len(results),
        "records": results,
        "actionable": [r for r in results if r["actionable"]],
        "not_yet_resolvable": [r for r in results if not r["resolvable"]],
    }
