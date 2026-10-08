"""
coordinator_core.ops.sizing_accept_exit_criterion — JSON-RPC "sizing.accept_exit_criterion".

Purpose: the single addressable applier for a sizing-object's `exit_criterion.accepted`
field (2026-09-27, docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md
§ C4, § Design § Acceptance). The exit criterion moves from plan time to sizing time
(target-design.md §11): the PM now confirms the primary success / exit criterion AT the
sizing touchpoint, and this op is what makes that confirmation a real, addressable write
rather than a hand-edited YAML field or an inferred fact. `_scaffold_plan` (C3) and
`prep_gate._prime_exit` (C5) both key off `exit_criterion.accepted` being non-null; this
op is the only writer of that sub-field.

Modelled directly on `sizing_discharge_surfaced.py`'s shape (single-target applier,
`locked_rmw`, schema-validate-before-write, `contained_path`, `main_worktree_root`) — a
single addressable applier over a different sub-field of the same sizing-object schema.

An amendment (a new `statement`) on an already-accepted sizing without `supersede` sets
`exit_criterion.statement` and appends {pm_quote, on, mode, statement} to
`exit_criterion.amendments`, leaving `accepted` untouched.

What it writes: `exit_criterion.accepted = {pm_quote, on, mode}`, and `exit_criterion.
statement` when `statement` is given (the PM's amended criterion replacing the proposed
one). When `mode` is passed and the document records no top-level `interaction_mode`, that
field is written too: the mode the PM accepted under IS the mode the sizing ran under, and
no other op can set it after assemble (`emit-wave-fire --from-sizing` refuses without it).
An `interaction_mode` already on record is never overwritten. Nothing else changes.

Negative-spec:
  - Does NOT write `pm_resolution`, `surfaced_to_pm`, `detents`, or `route` — those are
    other fields with their own writers; this op touches `exit_criterion` alone.
  - Does NOT compose or infer `pm_quote` or `apm_ruling`; the latter is the APM's verbatim
    text, admitted only in pm/ceo mode and never over a PM acceptance. `pm_quote` is the
    caller's verbatim transcription of what the PM said, the same live-evidence discipline `sizing.decline`'s
    `decision_record` and `sizing.discharge_surfaced`'s `resolved_by` both put on their
    own required params, applied here to the PM's own words instead of a file pointer.
  - Does NOT accept an empty `pm_quote`, a `statement` write with no statement already on
    record and none given, or a second acceptance with neither a new `statement` nor `supersede` — a silent
    overwrite of an already-accepted criterion would let a later caller displace the
    PM's own recorded acceptance without saying so.
  - Does NOT cascade, does NOT fan out to any other artifact, does NOT git-commit. Pure
    single-file frontmatter-shaped (whole-document YAML) mutation, the same `locked_rmw`
    RMW discipline as every other mutating op in this package.
  - Does NOT mint an execute stamp and does NOT let a downstream caller reuse `pm_quote`
    as an execution utterance (Design § Anti-scope, "No execute stamp is minted and no
    PM words are reused") — that is this op's caller's discipline to keep, not something
    this op enforces mechanically, since it has no visibility into what a caller does
    with its return value.

Spec backlink: pln-sizing-engine-carries-exit-cri-af770b § C4
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import insert_fm_field, write_fm_nested_field
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.roadmap.post_stamp_clause import post_stamp_refusal, suite_tier_refusal
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.sizing_acceptance import (
    APM_ADMISSIBLE_MODES,
    SOURCE_APM,
    SOURCE_PM,
    acceptance_source,
    acceptance_words,
)
from coordinator_core.session.job_mode_env import INTERACTION_MODES

# established per-module convention (see e.g. sizing_discharge_surfaced._SIZING_SCHEMA_PATH).
_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)


def _validate_sizing_fm(fm_dict: dict) -> list:
    return validate_frontmatter(fm_dict, _SIZING_SCHEMA_PATH)


def _click_paths(existing: object) -> dict:
    """The criterion's `click_paths`, carried through acceptance unchanged."""
    paths = existing.get("click_paths") if isinstance(existing, dict) else None
    return {"click_paths": paths} if paths else {}


def _render_exit_criterion(mapping: dict) -> str:
    dumped = yaml.safe_dump(
        mapping,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100000,
    )
    return "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))


_PARAMS_HINT = (
    "params: sizing (required, path under state/sizings/), exactly one of pm_quote / "
    "apm_ruling (required), statement, mode, supersede"
)


def _err(msg: str) -> dict:
    msg = msg or "accept_exit_criterion: refused with no detail"
    return {"exit_code": 1, "applied": False, "error": msg, "message": msg}


@register_op("sizing.accept_exit_criterion")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.accept_exit_criterion" handler.

    Plain `def`, deliberately not `async def`: every step here is blocking (path
    stats, `locked_rmw`), so a zero-await `async def` would make DISPATCH_TIMEOUT_SECS
    silently unenforceable for this op (the incident remediated at 3241c7c95573).

    Params:
        sizing       (str)  — absolute or repo-relative path to the sizing-object under
                              `state/sizings/`. Required.
        pm_quote     (str)  — the PM's own verbatim words accepting the exit criterion.
                              Exactly one of pm_quote / apm_ruling is required. Never
                              composed or paraphrased by this op or its caller.
        apm_ruling   (str)  — the APM's verbatim ruling standing in for the PM; admitted
                              only when the effective mode is pm or ceo, and never over a
                              PM acceptance. A pm_quote replaces an APM acceptance without
                              `supersede`.
        statement    (str)  — the PM's amended criterion, replacing the proposed one in
                              the same write. Optional; when omitted the statement
                              already on record is kept.
        mode         (str)  — the interaction_mode this sizing ran under at acceptance
                              time. Optional, one of hands-on/pm/ceo; omitted, the sizing's
                              recorded interaction_mode, else hands-on.
        supersede    (bool) — overwrite an already-accepted criterion. Without it, a
                              second acceptance of an already-accepted criterion is
                              refused.

    Returns: {exit_code, applied, message|error}.

    Exit-code contract:
        exit_code 1 — a missing required param; an unknown `mode`; a `sizing_path`
                      escaping state/sizings/ or absent on disk; no statement already
                      on record and none given; an already-accepted criterion without
                      `supersede`; an empty pm_quote; post-mutation schema validation
                      failure; lock timeout.
        exit_code 0, applied True  — exit_criterion.accepted (and, if given,
                      exit_criterion.statement) was written.
        exit_code 0, applied False — an identical acceptance was already recorded;
                      idempotent no-op.
    """
    sizing_raw: str = (params.get("sizing") or "").strip()
    pm_quote: str = (params.get("pm_quote") or "").strip()
    apm_ruling: str = (params.get("apm_ruling") or "").strip()
    statement_param: str = (params.get("statement") or "").strip()
    mode: str = (params.get("mode") or "").strip()
    supersede: bool = bool(params.get("supersede"))

    if not sizing_raw:
        return _err(
            f"missing required param: sizing — {_PARAMS_HINT}"
        )
    if pm_quote and apm_ruling:
        return _err(f"pass exactly one of pm_quote / apm_ruling, not both; {_PARAMS_HINT}")
    if not pm_quote and not apm_ruling:
        return _err(
            "missing required param: pm_quote or apm_ruling — the PM's or the APM's "
            f"verbatim words; this op never composes or infers either; {_PARAMS_HINT}"
        )
    if mode and mode not in INTERACTION_MODES:
        return _err(
            f"mode must be one of {list(INTERACTION_MODES)!r}, got {mode!r}"
        )
    if repo_root is None:
        return _err(
            "sizing.accept_exit_criterion: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(sizing_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [worktree / "state" / "sizings"])
    if p is None:
        return _err(f"sizing escapes state/sizings/: {sizing_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_raw}")

    _state: dict = {"applied": False}

    def _finish(new_text: str) -> str:
        try:
            new_doc = yaml.safe_load(new_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(
                f"accept_exit_criterion: post-mutation YAML parse error: {exc}"
            ) from exc
        errors = _validate_sizing_fm(new_doc)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(
                f"accept_exit_criterion: post-mutation schema validation failed: {details}"
            )

        _state["applied"] = True
        return new_text

    def mutate(old_text: str) -> str:
        try:
            doc = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"accept_exit_criterion: YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("accept_exit_criterion: sizing-object is not a YAML mapping")

        existing = doc.get("exit_criterion")
        existing = existing if isinstance(existing, dict) else {}
        existing_statement = str(existing.get("statement") or "").strip()
        existing_accepted = existing.get("accepted")
        record_mode = bool(mode) and not doc.get("interaction_mode")

        new_statement = statement_param or existing_statement
        if not new_statement:
            raise MutateAbort(
                f"refusing to accept on {p}: no statement is on record and none was "
                "given — this op never composes a criterion on the PM's behalf"
            )

        refusal = post_stamp_refusal(new_statement) or suite_tier_refusal(new_statement)
        if refusal is not None:
            raise MutateAbort(f"refusing to accept on {p}: {refusal}")

        eff_mode = mode or str(doc.get("interaction_mode") or "") or "hands-on"
        existing_source = acceptance_source(existing_accepted)
        today = date.today().isoformat()
        if apm_ruling:
            recorded_mode = str(doc.get("interaction_mode") or "")
            if recorded_mode and mode and mode != recorded_mode:
                raise MutateAbort(
                    f"refusing to accept on {p}: mode {mode!r} contradicts the recorded "
                    f"interaction_mode {recorded_mode!r}; an APM ruling is gated on the record"
                )
            if eff_mode not in APM_ADMISSIBLE_MODES:
                raise MutateAbort(
                    f"refusing to accept on {p}: an APM ruling stands in for the PM only in "
                    f"{list(APM_ADMISSIBLE_MODES)!r} mode; this sizing is {eff_mode!r}"
                )
            if existing_source == SOURCE_PM:
                raise MutateAbort(
                    f"refusing to accept on {p}: exit_criterion.accepted already carries a "
                    "PM acceptance; an APM ruling never displaces it"
                )
            new_accepted = {
                "source": SOURCE_APM,
                "apm_ruling": apm_ruling,
                "on": today,
                "mode": eff_mode,
            }
        else:
            new_accepted = {"pm_quote": pm_quote, "on": today, "mode": eff_mode}
        new_words = apm_ruling or pm_quote

        if isinstance(existing_accepted, dict) and not (
            existing_source == SOURCE_APM and not apm_ruling
        ):
            identical = (
                acceptance_words(existing_accepted) == new_words
                and existing_accepted.get("mode") == new_accepted["mode"]
                and new_statement == existing_statement
            )
            if identical and not record_mode:
                return old_text
            if not supersede:
                if not statement_param:
                    raise MutateAbort(
                        f"refusing to accept on {p}: exit_criterion.accepted already carries "
                        f"an acceptance ({str(acceptance_words(existing_accepted))[:120]!r}) — "
                        "pass a new statement to amend it, or supersede to replace the "
                        "acceptance"
                    )
                amendments = list(existing.get("amendments") or [])
                amendments.append({**new_accepted, "statement": new_statement})
                new_text = write_fm_nested_field(
                    old_text,
                    "exit_criterion",
                    _render_exit_criterion(
                        {
                            "statement": new_statement,
                            "accepted": existing_accepted,
                            "amendments": amendments,
                            **_click_paths(existing),
                        }
                    ),
                )
                return _finish(new_text)

        rendered = _render_exit_criterion(
            {"statement": new_statement, "accepted": new_accepted, **_click_paths(existing)}
        )
        new_text = write_fm_nested_field(old_text, "exit_criterion", rendered)
        if record_mode:
            new_text = insert_fm_field(new_text, "interaction_mode", mode)
        return _finish(new_text)

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object disappeared before the lock could be acquired: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "accept_exit_criterion: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"accept_exit_criterion: {type(exc).__name__}: {exc}")

    if _state["applied"]:
        what = "APM ruling accepting" if apm_ruling else "PM acceptance of"
        return {
            "exit_code": 0,
            "applied": True,
            "message": f"recorded {what} the exit criterion on {sizing_raw}",
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_raw} already records this acceptance — idempotent no-op",
    }
