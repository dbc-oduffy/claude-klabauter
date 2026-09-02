"""Resume path: turn a persisted, answered decision object into the
``--decisions`` payload ``coordinator_core.contract.apply_base`` already
consumes.

Purpose (docs/plans/2026-09-02-the-loader-fires-the-assembly-not-the-em.md,
chunk C3): a brief already persists a full decision-object envelope to
``.git/coordinator-sessions/decisions/<session>__<artifact>.json`` (the
``ENVELOPE_KEYS`` 8-key shape from ``envelope.py``), but nothing reads it
back on a later turn to resume a run whose ``judgment_points`` were left
open. This module is that read-back: given (a) the persisted object and
(b) the EM's answers to its open judgment points, it produces the
``{jp_id: {"disposition": ..., ...}}`` mapping ``apply_base.normalize_decisions``
already widens and ``apply_base.disposition_resolves_directive`` already
reads. It never calls ``apply``, never mutates the artifact, and never
re-runs ``brief`` -- see ``docs/reference/loader-fires-assembly-contract.md``
for the full two-halves contract this module is one half of.

Negative-spec:
- This is NOT a new ``apply``. No commit, no artifact mutation, no re-brief.
- An answer naming a disposition value the judgment point's own
  ``dispositions[].value`` list does not contain is a REFUSAL
  (``ResumeRefused``), never a best-effort coercion onto the nearest legal
  value -- the same discipline C2 enforces for a mismatched discovery tier.
- A decision object whose underlying artifact has visibly moved on since
  the object was persisted is refused, not resumed against stale answers --
  see ``check_not_stale`` for exactly what "visibly moved on" means here and
  what it deliberately does NOT cover.

Distinct, pre-existing mechanism this module does NOT replace or duplicate:
``coordinator_core.pickup_assemble.apply._read_session_dispositions`` already
reads a disposition back out of the SAME on-disk file shape, but only after
the EM has mutated that file in place (writing a ``disposition`` key onto a
``judgment_points[]`` entry) and only for pickup's own ``apply()`` entry
point. This module is the generic, read-only sibling: it takes the EM's
answers as a separate, in-memory argument rather than requiring a prior
edit to the persisted file, and it lives at the shared ``contract/decision_object``
layer so any skill's assembler can call it, not only pickup's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from coordinator_core.contract.decision_object.envelope import judgment_points_by_id
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)


def _legal_disposition_values(judgment_point: Mapping[str, Any]) -> set[str]:
    """The disposition values one persisted judgment point actually offers.

    The `dispositions[].value` vocabulary `judgment.build_disposition` writes,
    read back. An answer naming a value absent from this set is what
    this module's `resume_decisions` refuses on rather than coercing.

    Review: overengineering-reviewer -- private to this module rather than a
    shared reader in `envelope.py`: this function has exactly one production
    caller (below), unlike `judgment_points_by_id`, which genuinely has two
    (`resume_decisions` here and `pickup_assemble.apply`).
    """
    dispositions = judgment_point.get("dispositions")
    if not isinstance(dispositions, list):
        return set()
    return {
        d["value"]
        for d in dispositions
        if isinstance(d, Mapping) and d.get("value") is not None
    }


class ResumeRefused(ValueError):
    """Raised whenever `resume_decisions` (or a helper it calls) must refuse
    rather than guess -- an unrecognized disposition, a stale artifact, or a
    malformed persisted object. Never caught-and-continued by a caller."""


def load_decision_object(path: Path | str) -> dict[str, Any]:
    """Read and parse a persisted decision-object JSON file.

    Raises `ResumeRefused` on a missing file, unreadable file, malformed
    JSON, or a top-level shape that is not a JSON object -- there is no
    silent-`{}` degrade here (contrast
    `pickup_assemble.apply._read_session_dispositions`, which degrades to
    `{}` because it is strictly-additive input to a recompute): a caller of
    THIS module asked to resume a specific object, so a file that cannot be
    read is a refusal to resume, not "nothing to add".
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResumeRefused(f"cannot read decision object at {p}: {exc}") from exc
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ResumeRefused(f"decision object at {p} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ResumeRefused(
            f"decision object at {p} must be a JSON object, got {type(obj).__name__}"
        )
    return obj


def check_not_stale(decision_object: Mapping[str, Any], *, repo_root: Path | str) -> None:
    """Refuse (`ResumeRefused`) if the artifact this decision object was
    persisted against has visibly moved on since then.

    Signal used, and why: `artifact.frontmatter` on the persisted object is
    a SNAPSHOT of the artifact's own frontmatter at brief-render time (see
    the real objects under `.git/coordinator-sessions/decisions/*.json` --
    every memo/handoff/spinoff-classified artifact carries a `status` key in
    that snapshot: `open` while unresolved, `actioned`/`archived`/`claimed`
    once a later turn (this session's own later run, or a competing one)
    disposes of it). Re-reading the artifact's CURRENT frontmatter `status`
    off disk and comparing it to the persisted snapshot's `status` catches
    exactly the case the negative spec names: something disposed of this
    artifact after the object was persisted, so resuming stale answers
    against it now would silently re-decide an already-decided artifact.

    Deliberately NOT used: file mtime, or a content hash of the whole file.
    Both drift for reasons that have nothing to do with staleness (a
    checkout resets mtimes; an unrelated body edit changes a hash without
    changing the disposition-relevant state), so either would be the
    "weak" signal the chunk's spec warns against inventing. `status` is
    not weak in that sense -- it IS the artifact's own state-machine field,
    already snapshotted, already comparable.

    Known gap, stated rather than papered over: some artifact classes
    (`archived`, `ambiguous` in the corpus this was verified against) are
    persisted with an EMPTY `frontmatter` snapshot -- no `status` key at
    all. There is no usable staleness signal for those today, and this
    function refuses in that case too rather than treating "no signal"
    as "safe to proceed" -- see `docs/reference/loader-fires-assembly-
    contract.md` for this being named as an open requirement on the
    persisting side (our own `brief()`), out of this chunk's scope.
    """
    artifact = decision_object.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ResumeRefused("decision object carries no `artifact` object to verify")

    persisted_frontmatter = artifact.get("frontmatter")
    persisted_status = (
        persisted_frontmatter.get("status")
        if isinstance(persisted_frontmatter, Mapping)
        else None
    )
    if persisted_status is None:
        raise ResumeRefused(
            "decision object's artifact.frontmatter carries no `status` snapshot "
            "to compare against -- refusing to resume against an artifact whose "
            "staleness cannot be verified (see check_not_stale docstring)"
        )

    artifact_path = artifact.get("path")
    if not artifact_path:
        raise ResumeRefused("decision object names no artifact.path to re-check")

    current_file = Path(repo_root) / artifact_path
    if not current_file.is_file():
        raise ResumeRefused(
            f"artifact {artifact_path!r} no longer exists on disk under {repo_root!r}"
        )

    try:
        current_text = current_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResumeRefused(f"cannot re-read artifact {artifact_path!r}: {exc}") from exc

    split = split_frontmatter(current_text)
    current_status = (
        read_fm_field_unquoted(split.fm_text, "status") if split is not None else None
    )

    if current_status != persisted_status:
        raise ResumeRefused(
            f"artifact {artifact_path!r} frontmatter status changed since this "
            f"decision object was persisted ({persisted_status!r} -> "
            f"{current_status!r}) -- refusing to apply stale answers"
        )


def resume_decisions(
    decision_object: Mapping[str, Any],
    answers: Mapping[str, str | Mapping[str, Any]],
    *,
    repo_root: Path | str,
) -> dict[str, dict[str, Any]]:
    """Marshal (persisted decision object, EM answers) into an
    ``apply``-shaped ``--decisions`` payload: ``{jp_id: {"disposition": ...,
    ...extra content keys}}``.

    `answers` maps a judgment-point id to either a bare disposition-value
    string (the shorthand every CLI caller already types, per
    `apply_base.normalize_decisions`'s docstring) or a dict carrying
    `disposition` plus any of the disposition-content keys
    (`decision_note`/`realized_by`/`actioned_note`/`distill_fate`, etc --
    this module does not enumerate or validate that content-key set; it
    is `apply_base`'s ``DISPOSITION_CONTENT_KEYS`` and `apply`'s own
    directive-building that own that vocabulary).

    Never commits, never mutates `decision_object` or the artifact on disk,
    never re-runs `brief` -- this function performs no I/O of its own
    besides `check_not_stale`'s read-only re-read of the artifact for the
    staleness check, always run.

    Raises `ResumeRefused`:
    - if staleness is not verified clean (see `check_not_stale`);
    - for any `answers` key naming a judgment-point id absent from
      `decision_object["judgment_points"]`;
    - for any answer naming a disposition value that judgment point's own
      `dispositions[].value` list does not contain -- this is the
      never-coerce guarantee: an answer is applied exactly as recorded or
      refused, never silently mapped onto the "closest" legal value.
    """
    check_not_stale(decision_object, repo_root=repo_root)

    jp_by_id = judgment_points_by_id(decision_object)

    payload: dict[str, dict[str, Any]] = {}
    for jp_id, answer in answers.items():
        jp = jp_by_id.get(jp_id)
        if jp is None:
            raise ResumeRefused(
                f"answer given for judgment point {jp_id!r}, which this decision "
                "object does not carry in `judgment_points`"
            )

        if isinstance(answer, str):
            entry: dict[str, Any] = {"disposition": answer}
        elif isinstance(answer, Mapping):
            entry = dict(answer)
        else:
            raise ResumeRefused(
                f"answer for judgment point {jp_id!r} must be a disposition-value "
                f"string or a mapping, got {type(answer).__name__}"
            )

        chosen = entry.get("disposition")
        legal_values = _legal_disposition_values(jp)
        if chosen not in legal_values:
            raise ResumeRefused(
                f"judgment point {jp_id!r} does not record disposition {chosen!r} "
                f"among its own dispositions {sorted(v for v in legal_values if v)!r} "
                "-- refusing rather than coercing onto the nearest legal value"
            )

        payload[jp_id] = entry

    return payload
