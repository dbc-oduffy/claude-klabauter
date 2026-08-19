"""
coordinator_core.authz.dispatchable — per-assembler op admission control.

Purpose: `ASSEMBLER_DISPATCHABLE` restores the closed-construction property the
engine already committed to for `_CLI_DISPATCH` (25 hand-reviewed literals), on the
wider namespace an op-named directive opens (259 registered ops). It is a
DEFAULT-DENY mapping from assembler module name to the frozenset of op names that
module may dispatch: absence from the mapping, or absence from a module's own set,
is a refusal. It is a coupling and blast-radius control with a test behind it, NOT
an authz boundary — its principal (`assembler_name`) is a self-asserted string, and
any in-process caller can still `import` a handler directly and skip this seam
entirely. What it buys is that the *declared* dispatch surface stays enumerable and
reviewed, the same property `_CLI_DISPATCH` gave for free at 25 entries and does not
give for free at 259.

Sited in `coordinator_core/authz/`, the SAME package as `classification.py ::
OP_CLASSIFICATION`, deliberately — so drift is caught by the artifact that already
exists (`registration_quad`'s discovery walk, reused by this module's own test)
rather than only by a new bespoke test.

Negative spec: this mapping is HAND-EDITED and reviewed as such.
  - It is NEVER derived from `coordinator_core.ipc._REGISTRY` or any other
    registry/module enumeration.
  - It is NEVER widened by a loop, comprehension, or programmatic population of any
    kind — every entry is a literal, typed by a reviewer.
  - It is NEVER populated from caller-supplied or directive-supplied input.
  - Ship it EMPTY except for the op names a migration chunk (C4-C6 of
    docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md) actually migrates.
    An allowlist seeded "for convenience" with the whole registry is the failure
    this control exists to prevent.

Spec backlink: pln-directives-name-an-op-not-a-cl-e283a9 § C1
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md (shape precedent)
"""

from __future__ import annotations

import types

# Review: hand-edited only — mutation raises TypeError rather than silently
# escalating an assembler's dispatch privilege at runtime. Copies
# coordinator_core.authz.classification.OP_CLASSIFICATION's MappingProxyType shape
# rather than inventing a new one. Keyed by assembler module name (e.g.
# "pickup_assemble"); each value is the frozenset of op names (assembler-family) or
# CONSUMES_MANIFEST script barewords (completion-family) that assembler may
# dispatch. See docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md
# § The discriminator for the mixed end state.
ASSEMBLER_DISPATCHABLE: "types.MappingProxyType[str, frozenset[str]]" = types.MappingProxyType({})
