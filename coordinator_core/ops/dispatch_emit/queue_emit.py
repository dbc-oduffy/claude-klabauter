"""
coordinator_core.ops.dispatch_emit.queue_emit — the queue-grind engine's
entrypoint: profile + appetite + queue dirs -> one `QueueEmission`.

Purpose: `emit_queue_script` is the ONE call DoE makes (docs/plans/2026-09-21-
bug-blitz-emitter-engine-leg.md § Design § Entrypoint, § Answers to DoE's
addendum questions #5). It composes the pipeline this plan built in the
depended-on chunks -- `grind_profile.load_profile`/`validate_graph`/
`resolve_appetite` (C3), `queue_select.select_rows` (C2), and
`grind_compose.compose_grind_script` (C7) -- into one fireable `.mjs` script
plus the receipt extras `dispatch.emit`'s queue route writes beside it. This
module owns none of that pipeline logic itself: it resolves a profile, folds
overrides into knobs, selects rows, composes the script, and reports what it
did. It does NOT touch disk for a write -- that is `op.py`'s job, same
division `emit.emit_script` already keeps from `op.py`'s own write leg.

Wire shape:
    `emit_queue_script(profile, appetite="standard", overrides=None, *,
    queue, profile_dir, repo_root, run_dir, session_id=None,
    agent_type_host=None) -> QueueEmission`. `overrides` keys are a subset of
    `{"where", "limit", "budget_tokens"}` (`grind_vocab.OVERRIDABLE_KNOBS`) --
    `resolve_appetite` refuses any other key (`UnoverridableKnobError`).
    `queue` is one or more queue directories; `run_dir` is the guarded output
    path's parent (the caller -- `op.py` -- derives it from the already
    path-guarded script path, never from a raw caller string).

`QueueEmission.receipt_extras` carries `queue`, `profile`, `profile_digest`
(sha256 of the loaded profile's own source bytes -- a distinct fact from
`triage_policy_sha256`, which digests only the inlined `triage_policy` text,
not the whole profile), `appetite`, `resolved_knobs`, `manifest_digest`
(`Manifest.digest`, never the manifest itself -- § Design § Selector, "Frozen
means frozen"), `source` (the manifest's own `SourceOpResult`, JSON-shaped, or
`None`), and `reemit` -- the argv list a caller re-runs
(`emit-dispatch-workflow.py --queue ... --profile ... --appetite ... [...]`)
to re-derive this same script from the same queue/profile/appetite/overrides
inputs. `op.py :: _write_emission_receipt` writes `plan: null` itself; this
dict does NOT repeat that key.

Containment: every `queue` directory and `run_dir` are refused unless they
resolve under `repo_root` (`coordinator_core.ops._path_guard.contained_path`)
-- the same post-`.resolve()` symlink-safe containment shape every other
write-adjacent op in this package uses, applied here to read-side directories
because a queue dir named outside the repo is exactly the kind of fat-finger
the guard exists to catch (module docstring,
`coordinator_core/ops/_path_guard.py`).

Negative-spec:
  - Does NOT write anything to disk. `op.py` is the one write leg in this
    pipeline (module docstring, `op.py`); this module returns the script TEXT
    and the receipt extras dict, nothing more.
  - Does NOT re-implement `select_rows`/`load_profile`/`validate_graph`/
    `resolve_appetite`/`compose_grind_script`. Every disk read, ledger
    fold-in, `where` evaluation, and script-body composition happens inside
    those functions; this module only sequences their calls and its own
    containment/digest/reemit bookkeeping.
  - Does NOT fall back to a plan-spine emission on missing queue/profile
    params -- `op.py`'s queue route and its plan route are mutually
    exclusive (`QueuePlanConflictError`), and queue input never falls back to
    the wave path (§ Design § Entrypoint).
  - Does NOT resolve or call a source op itself -- `source` is forwarded
    straight from the loaded profile's own (optional) `source` block to
    `select_rows`, which is the one module that resolves/calls it
    (`queue_select._call_source_op`, S1). A profile with no `source` block
    passes `None`, unchanged from before.
  - Does NOT derive `run_dir` from a raw caller string. The caller (`op.py`)
    is responsible for deriving it from an already-guarded script path before
    calling in -- this module only re-checks containment under `repo_root`,
    it does not resolve or default it.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Entrypoint, § Answers to DoE's addendum questions #5, Tasks § C8.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.grind_compose import compose_grind_script
from coordinator_core.ops.dispatch_emit.grind_profile import (
    load_profile,
    resolve_appetite,
    validate_graph,
)
from coordinator_core.ops.dispatch_emit.queue_select import select_rows

__all__ = ["QueueEmission", "QueuePathEscapeError", "emit_queue_script"]


class QueuePathEscapeError(ValueError):
    """Raised when `run_dir` or a `queue` directory resolves outside `repo_root`.

    Mirrors `op.py :: PathEscapeError`'s shape for the plan route's
    `output_path`/`target_root` pair, applied here to the queue route's
    read-side directories -- both are the same
    `coordinator_core.ops._path_guard.contained_path` containment check.
    """


class QueueEmission(NamedTuple):
    """`emit_queue_script`'s return: the composed script text plus the
    receipt extras `op.py` writes beside it (module docstring)."""

    script: str
    receipt_extras: dict


def _resolved_batch_sizes(resolved_knobs: Mapping[str, Any]) -> dict:
    """The `batch_sizes` `select_rows` records on the frozen `Manifest` --
    the resolved `batch_size` knob, reshaped to the `{batch_key: int}` (or
    `{"default": int}`) mapping `Manifest.batch_sizes` is typed as. Recorded
    honestly off the SAME resolved knob `grind_compose._group_into_batches`
    groups by, never a second, independently-computed value."""
    batch_size = resolved_knobs.get("batch_size", 4)
    if isinstance(batch_size, Mapping):
        return dict(batch_size)
    return {"default": batch_size}


def _reemit_argv(
    profile: str,
    appetite: str,
    profile_dir: Path,
    queue: Sequence[Path],
    overrides: Optional[Mapping[str, Any]],
) -> list:
    """The argv list `emit-dispatch-workflow.py` re-runs to re-derive this
    same script from the same queue/profile/appetite/overrides inputs --
    never the resolved knobs, which are a DERIVED fact, not an input; re-
    deriving from the resolved value rather than the raw override would mask
    a profile-preset change on re-emit."""
    argv: list = [
        "--profile", profile,
        "--appetite", appetite,
        "--profile-dir", str(profile_dir),
    ]
    for queue_dir in queue:
        argv += ["--queue", str(queue_dir)]
    overrides = overrides or {}
    if "where" in overrides:
        argv += ["--where", json.dumps(overrides["where"], sort_keys=True)]
    if "limit" in overrides:
        argv += ["--limit", str(overrides["limit"])]
    if "budget_tokens" in overrides:
        argv += ["--budget-tokens", str(overrides["budget_tokens"])]
    return argv


def emit_queue_script(
    profile: str,
    appetite: str = "standard",
    overrides: Optional[Mapping[str, Any]] = None,  # keys ⊆ {"where", "limit", "budget_tokens"}
    *,
    queue: Sequence[Path],
    profile_dir: Path,
    repo_root: Path,
    run_dir: Path,
    session_id: Optional[str] = None,
    agent_type_host: Optional[str] = None,
) -> QueueEmission:
    """Turn a profile, an appetite and one or more queue directories into one
    composed queue-grind script plus its receipt extras. See module docstring.
    """
    repo_root = Path(repo_root).resolve()

    guarded_run_dir = contained_path(Path(run_dir), [repo_root])
    if guarded_run_dir is None:
        raise QueuePathEscapeError(
            f"run_dir escapes repo_root: {run_dir!r} not under {repo_root!r}"
        )

    guarded_queue_dirs: list = []
    for queue_dir in queue:
        guarded = contained_path(Path(queue_dir), [repo_root])
        if guarded is None:
            raise QueuePathEscapeError(
                f"queue dir escapes repo_root: {queue_dir!r} not under {repo_root!r}"
            )
        guarded_queue_dirs.append(guarded)

    loaded_profile = load_profile(profile, profile_dir)
    validate_graph(loaded_profile)

    resolved_knobs = resolve_appetite(
        loaded_profile, appetite, dict(overrides) if overrides else None
    )

    manifest = select_rows(
        guarded_queue_dirs,
        where=resolved_knobs.get("where"),
        order=(loaded_profile.priority["field"], loaded_profile.priority["order"]),
        limit=resolved_knobs.get("limit"),
        batch_key=list(loaded_profile.batch_key),
        batch_sizes=_resolved_batch_sizes(resolved_knobs),
        row_id_key=loaded_profile.row_id_key,
        profile=loaded_profile.name,
        repo_root=repo_root,
        source=loaded_profile.source,
        absent_sentinels=loaded_profile.absent_sentinels,
    )

    script = compose_grind_script(
        manifest,
        loaded_profile,
        resolved_knobs,
        repo_root=repo_root,
        run_dir=guarded_run_dir,
        session_id=session_id,
        agent_type_host=agent_type_host,
    )

    profile_digest = hashlib.sha256(loaded_profile.source_path.read_bytes()).hexdigest()

    source_extra = None
    if manifest.source is not None:
        source_extra = {
            "op": manifest.source.op,
            "args": dict(manifest.source.args),
            "output_sha256": manifest.source.output_sha256,
        }

    receipt_extras = {
        "queue": [str(q) for q in queue],
        "profile": loaded_profile.name,
        "profile_digest": profile_digest,
        "appetite": appetite,
        "resolved_knobs": dict(resolved_knobs),
        "manifest_digest": manifest.digest,
        "source": source_extra,
        "reemit": _reemit_argv(profile, appetite, Path(profile_dir), queue, overrides),
    }

    return QueueEmission(script=script, receipt_extras=receipt_extras)
