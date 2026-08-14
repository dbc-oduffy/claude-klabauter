"""
coordinator_core.ops.sizing_spike_verdict — JSON-RPC "sizing.record_spike_verdict"
operation.

Purpose: the missing PRODUCER for the sizing-object schema's
`premise.spike_verdict` pointer field. `coordinator_core/plan_assemble/
predicates/substrate_seven_dim.py::trampoline_verdict` (the `plan⇄spike`
back-edge's eighth-dimension gate, `gates.substrate.trampoline.verdict_cited`/
`.verdict_path`/`.verdict`) has always CONSUMED this field — reading it off
the sizing object, resolving the named spike-verdict record, and surfacing
its `verdict` enum — but nothing in this engine ever WROTE it. A grep of all
skill bodies (2026-08-14 audit) found only two consumers (this predicate and
the `sizing` skill's Step 1b prose) and zero producers; `spike/SKILL.md` §
Durable Output lists only `gated_route`/`discharged_by` as the spike-result
record's own fields, never a write-back onto the sizing object that gated
the spike. Consequence: a plan trampolined to a spike to derisk an unproven
mechanism had no path back to GREEN — the resume leg of the back-edge could
never be discharged as the plan (Branch B) and the skill both specify.

The schema (`frontmatter/schemas/sizing-object.schema.json`) already
DECLARES `premise.spike_verdict` — this op does not extend the schema, only
supplies the missing writer, following the same single-field, single-target,
`locked_rmw` applier shape as its `sizing_ship`/`sizing_decline` siblings.

Semantic decisions:
  - Writes exactly one field: `premise.spike_verdict` (a path string pointing
    at the spike-verdict record under `docs/research/spike-verdicts/`).
    Never touches `premise.provenance`/`premise.evidence` (those stay
    hand-authored per the schema's own "answered in place" doctrine — see
    `evidence`'s description) nor any other sizing-object field.
  - Requires `spike_verdict_path` to resolve to a REAL, on-disk, schema-valid
    spike-result record — same "live evidence, not a caller's assertion"
    discipline as `sizing_decline`'s `decision_record` gate. The record's
    frontmatter is parsed and its `verdict` enum (`viable`/`not-viable`,
    per `frontmatter/schemas/spike-result.schema.json`) is validated as
    present and legal, so a malformed/incomplete verdict record can never be
    wired onto a sizing object's premise.
  - Idempotent no-op when `premise.spike_verdict` already equals the given
    path (byte-identical no-op, same idempotency floor as `sizing_ship`/
    `sizing_decline`). Refuses (MutateAbort) to silently overwrite a
    DIFFERENT already-recorded pointer — two spikes gating the same sizing
    is a genuine ambiguity this op does not resolve on its own say-so.
  - Does NOT resolve or interpret `gated_route`/`discharged_by` on the spike
    record — this op is the sizing-side half of the wiring; whether the
    spike record's own `gated_route`/`discharged_by` actually name this
    sizing/plan is a caller (ceremony-level) concern, out of scope here,
    exactly like `sizing_ship`'s own "caller seam (report-only, not wired in
    this dispatch)" note.

Spec backlink: docs/plans/2026-07-28-spike-verdict-records-stable-evidence-home.md
Spec backlink: coordinator_core/plan_assemble/predicates/substrate_seven_dim.py::trampoline_verdict
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    _strip_trailing_comment,
    read_fm_nested_field,
    serialize_yaml_scalar,
    unquote_yaml_scalar,
    write_fm_nested_field,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    parse_frontmatter,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

# Vendored sizing-object schema path — own local copy per this package's
# established per-module convention (see sizing_ship._SIZING_SCHEMA_PATH,
# sizing_decline._SIZING_SCHEMA_PATH).
_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

#: Legal `verdict` values on a spike-result record — mirrors
#: frontmatter/schemas/spike-result.schema.json's CLOSED `verdict` enum
#: exactly (no third "inconclusive" value: "an inconclusive spike is not a
#: verdict record yet").
_LEGAL_SPIKE_VERDICTS = frozenset({"viable", "not-viable"})

# Matches an indented `spike_verdict:` line inside a `premise:` nested
# block's raw block text (2-space indent, same shape as `provenance:`/
# `evidence:` siblings). Trailing `# comment` and any inline value are
# captured so the whole line can be replaced in place.
_SPIKE_VERDICT_LINE_RE = re.compile(
    r"^([ \t]*)spike_verdict:[ \t]*(.*)$", re.MULTILINE
)


def _validate_sizing_fm(fm_text: str) -> list:
    """Parse fm_text as whole-document YAML and validate against the
    sizing-object schema. Mirrors sizing_ship._validate_sizing_fm's contract
    exactly."""
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SIZING_SCHEMA_PATH)


def _read_premise_spike_verdict(premise_block: str) -> str | None:
    """Read the current `spike_verdict:` value out of a `premise:` block's
    raw indented block text, or None if absent."""
    m = _SPIKE_VERDICT_LINE_RE.search(premise_block)
    if m is None:
        return None
    return unquote_yaml_scalar(_strip_trailing_comment(m.group(2))) or None


def _write_premise_spike_verdict(premise_block: str, value: str) -> str:
    """Set (replace or append) `spike_verdict:` inside a `premise:` block's
    raw indented block text, preserving every other line untouched."""
    serialized = serialize_yaml_scalar(value)
    new_line = f"  spike_verdict: {serialized}"
    if _SPIKE_VERDICT_LINE_RE.search(premise_block):
        return _SPIKE_VERDICT_LINE_RE.sub(lambda _m: new_line, premise_block, count=1)
    trimmed = premise_block.rstrip("\n")
    sep = "\n" if trimmed else ""
    return trimmed + sep + new_line + "\n"


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


@register_op("sizing.record_spike_verdict")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.record_spike_verdict" handler — the missing producer
    for `premise.spike_verdict` (see module docstring).

    Plain `def`, deliberately not `async def` — mirrors sizing_ship/
    sizing_decline's own rationale exactly: every step here is blocking
    (path stats, `locked_rmw`), so a zero-await `async def` would make
    DISPATCH_TIMEOUT_SECS unenforceable (incident remediated at 3241c7c95573).

    Params:
        sizing_path       (str) — absolute or repo-relative path to the
                                   sizing-object under `state/sizings/`.
                                   Required.
        spike_verdict_path (str) — repo-relative or absolute path to the
                                   spike-verdict record
                                   (`docs/research/spike-verdicts/*.md`) to
                                   record onto `premise.spike_verdict`.
                                   Required, must resolve to a real,
                                   schema-legal-verdict file on disk.

    Returns a dict with keys:
        exit_code  (int)  — 0 ok (write or idempotent no-op) / 1 error.
        applied    (bool) — True iff `premise.spike_verdict` was written;
                             False on an idempotent no-op or on error.
        message    (str)  — present on exit_code 0; human-readable outcome.
        error      (str)  — present on exit_code 1; human-readable reason.

    Exit-code contract:
        exit_code 1 — missing sizing_path/spike_verdict_path;
                      spike_verdict_path escapes docs/research/spike-verdicts/,
                      is not found on disk, fails to parse, or carries a
                      `verdict` outside {"viable", "not-viable"}; sizing_path
                      escapes state/sizings/ or is not found on disk;
                      `premise.spike_verdict` already records a DIFFERENT
                      path; post-mutation schema validation fails; lock
                      timeout.
        exit_code 0, applied True  — premise.spike_verdict written.
        exit_code 0, applied False — premise.spike_verdict already records
                                      this exact path; idempotent no-op.
    """
    sizing_path_raw: str = (params.get("sizing_path") or "").strip()
    spike_verdict_path_raw: str = (params.get("spike_verdict_path") or "").strip()

    if not sizing_path_raw:
        return _err("missing required param: sizing_path")
    if not spike_verdict_path_raw:
        return _err(
            "missing required param: spike_verdict_path — this op requires live "
            "evidence a spike verdict record actually exists, not a caller's "
            "assertion that it will be written"
        )
    if repo_root is None:
        return _err(
            "sizing.record_spike_verdict: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(sizing_path_raw)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "state" / "sizings"]
    p = contained_path(p, allowed_roots)
    if p is None:
        return _err(f"sizing_path escapes state/sizings/: {sizing_path_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_path_raw}")

    sv = Path(spike_verdict_path_raw)
    if not sv.is_absolute():
        sv = worktree / sv
    sv = contained_path(sv, [worktree / "docs" / "research" / "spike-verdicts"])
    if sv is None:
        return _err(
            f"spike_verdict_path escapes docs/research/spike-verdicts/: "
            f"{spike_verdict_path_raw!r} — this op requires live evidence the "
            "spike verdict is actually recorded, not a caller's assertion "
            "that it will be"
        )
    if not sv.is_file():
        return _err(
            f"spike_verdict_path does not resolve to a real file: "
            f"{spike_verdict_path_raw!r}"
        )

    try:
        sv_text = sv.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(f"could not read spike_verdict_path {sv}: {exc}")

    parsed = parse_frontmatter(sv_text)
    sv_frontmatter = parsed.get("frontmatter") or {}
    sv_verdict = sv_frontmatter.get("verdict")
    if sv_verdict not in _LEGAL_SPIKE_VERDICTS:
        return _err(
            f"spike_verdict_path {spike_verdict_path_raw!r} carries verdict "
            f"{sv_verdict!r}, not one of {sorted(_LEGAL_SPIKE_VERDICTS)} — refusing "
            "to wire an incomplete/malformed verdict record onto a sizing "
            "object's premise"
        )

    # Record the repo-relative form, matching trampoline_verdict's own
    # `context.repo_root / spike_verdict` resolution and the on-disk
    # convention every existing `premise.spike_verdict`/top-level
    # `spike_verdict:` field already uses.
    recorded_value = spike_verdict_path_raw
    if Path(spike_verdict_path_raw).is_absolute():
        try:
            recorded_value = str(sv.relative_to(worktree))
        except ValueError:
            recorded_value = str(sv)

    _state = {"applied": False, "prior_value": None}

    def mutate(old_text: str) -> str:
        premise_block = read_fm_nested_field(old_text, "premise")
        if premise_block is None:
            raise MutateAbort(
                f"refusing to record spike verdict onto {p}: no `premise:` block "
                "found — every sizing-object is schema-required to carry one "
                "(this record is malformed independent of this op)"
            )

        current_value = _read_premise_spike_verdict(premise_block)
        _state["prior_value"] = current_value

        if current_value == recorded_value:
            # Already recording this exact pointer — idempotency floor,
            # byte-identical no-op.
            return old_text

        if current_value:
            raise MutateAbort(
                f"refusing to record spike verdict onto {p}: "
                f"premise.spike_verdict already records {current_value!r}, a "
                f"DIFFERENT path than {recorded_value!r} — this op never "
                "silently overwrites an already-recorded pointer"
            )

        new_premise_block = _write_premise_spike_verdict(premise_block, recorded_value)
        new_text = write_fm_nested_field(old_text, "premise", new_premise_block)

        errors = _validate_sizing_fm(new_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(
                f"record_spike_verdict: post-mutation schema validation failed: {details}"
            )

        _state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object not found: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(
            str(exc.args[0]) if exc.args else "record_spike_verdict: mutation aborted"
        )

    if _state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": (
                f"recorded spike verdict onto {sizing_path_raw} "
                f"(premise.spike_verdict -> {recorded_value})"
            ),
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": (
            f"{sizing_path_raw} premise.spike_verdict already records "
            f"{recorded_value} — idempotent no-op"
        ),
    }
