"""coordinator_core.ops.compute_layer_scaffold.op — JSON-RPC "compute_layer.scaffold" operation.

Purpose: thin transport wrapper registering `compute_layer.scaffold`. Builds
params for, and forwards to, `emit.compose_producer_module` (mode "emit") or
`check.score_fleet`/`check.render_report` (mode "check") and returns their
result verbatim — no emission logic and no scoring logic reimplemented here,
mirroring `coordinator_core.ops.workflow_scaffold`'s own thin-wrapper shape.

Wire params:
    mode (str, required)      — "emit" or "check".

    mode == "emit":
        skill_name (str, required) — the producer's skill identifier, passed
            straight through to `emit.compose_producer_module`.
        verbs (list[str], required) — the declared `CONSUMES_MANIFEST`,
            passed straight through.

    mode == "check":
        modules (list[str], optional) — module names to score; omitted ->
            `check.SUB_SHAPE_B` (the default `check.score_fleet` itself uses).

Reply fields:
    mode == "emit":  {"module_text": "<text>"} — the composed module TEXT.
        NO file write (AC12 — emission stays text-only all the way through
        this RPC seam; the caller decides whether/where to write it).
    mode == "check": {"report": "<rendered text>"} — `check.render_report`'s
        output over `check.score_fleet(modules)`. Read-only: `check.py`
        performs no file I/O beyond reading the scored packages' own source
        (see its own module docstring) and this handler writes nothing
        either — both paths are COMPUTE_ONLY (see `authz/classification.py`).

Raises:
    ValueError — missing/unknown `mode`, or a missing required param for the
    selected mode (message names the missing param, matching the
    cartography.symbols / workflow.scaffold error contract). `emit`/`check`'s
    own `ValueError`/`InvalidVerb` (a `ValueError` subclass) propagate
    unchanged for a malformed `skill_name`/`verbs`/`modules` entry.

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?    No.
     `emit` mode builds a string in memory; `check` mode reads producer
     source files read-only (see check.py's own affirmation) and renders a
     report string. No file is opened for write, no git object is touched.
  2. Writes into rag's relational store?                                   No.
     Returns a structured dict to the caller; no rag interaction of any kind.
  3. Opens any file for write (including sentinel creation)?               No.
     `emit` mode performs no file I/O at all; `check` mode opens producer
     source files read-only via `Path.read_text` (see check.py).
  4. Mutates shared mutable state outside its own module?                  No.
     Every value returned is freshly built per call; no shared/global state
     is written by this handler.
  5. Persistent state changes observable across process boundaries?       No.
     Nothing is written to disk by either mode; the only observable effect
     is the return value handed back to the caller. Placing `module_text`
     into a file is the CALLER's responsibility, deliberately kept out of
     this op's budget/surface (AC12).
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: pln-the-compute-layer-scaffolder-e-90d036 § C4
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.compute_layer_scaffold import check, emit


def _dispatch_emit(params: dict) -> dict:
    skill_name = params.get("skill_name")
    if not skill_name:
        raise ValueError("compute_layer.scaffold (mode=emit) requires param: skill_name")
    verbs = params.get("verbs")
    if not verbs:
        raise ValueError("compute_layer.scaffold (mode=emit) requires param: verbs")

    module_text = emit.compose_producer_module(skill_name, verbs)
    return {"module_text": module_text}


def _dispatch_check(params: dict) -> dict:
    modules = params.get("modules")
    report = check.score_fleet(modules) if modules else check.score_fleet()
    return {"report": check.render_report(report)}


@register_op("compute_layer.scaffold")
def _compute_layer_scaffold(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "compute_layer.scaffold" handler.

    Args (via params):
        mode (str): "emit" or "check".
        skill_name (str, mode=emit): passed to `emit.compose_producer_module`.
        verbs (list[str], mode=emit): passed to `emit.compose_producer_module`.
        modules (list[str], mode=check, optional): passed to
            `check.score_fleet`; omitted uses its own `SUB_SHAPE_B` default.

    Returns:
        {"module_text": "<text>"} for mode=emit, or {"report": "<text>"}
        for mode=check. Neither mode writes to disk.

    Raises:
        ValueError — missing/unknown `mode`, or a missing required param for
        the selected mode.
    """
    mode = params.get("mode")
    if mode == "emit":
        return _dispatch_emit(params)
    if mode == "check":
        return _dispatch_check(params)
    raise ValueError(
        f"compute_layer.scaffold received unknown mode {mode!r} — expected 'emit' or 'check'"
    )
