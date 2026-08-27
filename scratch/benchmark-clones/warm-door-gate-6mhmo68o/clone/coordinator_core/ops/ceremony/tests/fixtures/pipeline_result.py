"""
coordinator_core.ops.ceremony.tests.fixtures.pipeline_result — the one place
ceremony tests hand-build a `commit_pipeline.PipelineResult` stand-in.

Purpose: retire a defect class, not to save typing. `PipelineResult` is a
required-field dataclass, and it was extended TWICE in two days without a
sweep of the hand-built doubles in this package — `6a4d013d2` added
`carry_gate`, `b7b650bc6` added `op_scope_gate`, and four construction sites
across `test_wsc_tail_parity.py` and `test_wsc_tail_sha_unverified.py` began
failing with `TypeError: PipelineResult.__init__() missing 2 required
positional arguments`. Each such addition cost a fan-out fix across every
double. Now it costs one edit, here.

NEGATIVE SPEC — this helper must never absorb a new field silently. That
would reproduce the original bug inside a nicer wrapper: a test double
quietly carrying an unconsidered value for a field the production pipeline
sets deliberately is worse than a loud `TypeError`, because it fails as a
wrong assertion much later instead of at construction.

Two properties keep that honest:

  - `_FIELD_DEFAULTS` enumerates EVERY field of `PipelineResult` explicitly,
    including the ones that carry production defaults, and the real
    `PipelineResult` is constructed from it. A newly-added REQUIRED field is
    therefore still a hard `TypeError` — raised in this file, once, naming
    the missing field, rather than at N call sites.
  - A newly-added field that carries a DEFAULT would not raise, so it is
    covered instead by the drift test
    `test_commit_pipeline.py::test_make_pipeline_result_covers_every_field`,
    which asserts this map's keys equal `dataclasses.fields(PipelineResult)`.
    That test is the thing that fires on the next `b7b650bc6`-shaped change.

The defaults below encode the shape a ceremony test actually wants when it
says "a pipeline result I am not otherwise interested in": every gate and
outcome absent, nothing committed, nothing pushed, no failure. A test asserting
about any of those states must say so by passing it as an override — never by
relying on what this file happens to default to.
"""

from __future__ import annotations

from typing import Any, Dict

from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_STATUS_NOT_ATTEMPTED,
    PipelineResult,
)

#: Every field of `PipelineResult`, spelled out. Kept exhaustive by
#: `test_make_pipeline_result_covers_every_field` — see this module's own
#: NEGATIVE SPEC for why an implicit fallback is not used here.
_FIELD_DEFAULTS: Dict[str, Any] = {
    "stage": None,
    "deletion_gate": None,
    "dirty_gate": None,
    "carry_gate": None,
    "op_scope_gate": None,
    "commit": None,
    "push": None,
    "committed_sha": None,
    "pushed": None,
    "commit_failed": False,
    "integrity_breach": False,
    "sha_unverified": False,
    "push_status": PUSH_STATUS_NOT_ATTEMPTED,
    "pushed_range": None,
    "pushed_count": None,
    "reason": "",
    "unprovenanced_paths": (),
}


def make_pipeline_result(**overrides: Any) -> PipelineResult:
    """Build a real `PipelineResult` with every field supplied explicitly,
    overriding only what the calling test cares about.

    An unknown keyword is rejected here rather than forwarded, so a typo'd or
    renamed field name is a same-line error naming the offender instead of a
    bare `__init__() got an unexpected keyword argument` from inside the
    dataclass.

    `diagnostics` is deliberately absent from `_FIELD_DEFAULTS` and handled
    below: it is the one mutable field, and a shared list default would let
    one test's appends leak into the next test's double.
    """
    unknown = sorted(set(overrides) - set(_FIELD_DEFAULTS) - {"diagnostics"})
    if unknown:
        raise TypeError(
            "make_pipeline_result() got unknown field(s) %s -- valid fields "
            "are %s (if PipelineResult gained a field, add it to "
            "_FIELD_DEFAULTS in this module)"
            % (", ".join(unknown), ", ".join(sorted(_FIELD_DEFAULTS)))
        )
    kwargs = dict(_FIELD_DEFAULTS)
    kwargs.update(overrides)
    kwargs.setdefault("diagnostics", [])
    kwargs["diagnostics"] = list(kwargs["diagnostics"])
    return PipelineResult(**kwargs)
