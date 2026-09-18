"""coordinator_core.hooks.support — shared helpers most hook bodies import.

Purpose: the substrate tier for `coordinator_core.hooks`, mirroring
`docs/architecture/systems/support-libraries.md`'s "importable computation,
reached by import, never register_op" shape — this package registers no ops
of its own and holds no dispatch state; every hook body reaches it via a
plain `from coordinator_core.hooks.support import ...`.

Arrival, not authorship: every module here is a same-behavior port from
DoE-claude's `coordinator/hooks/scripts/_*.py` (the doctrine-plane hook
tree), landed ahead of any hook BODY move per
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3 ("The shared
helpers most hook bodies import ... land in coordinator_core/hooks/support/
before any body moves"). Two classes of adaptation were necessary, both
because a helper that lived beside its callers in a doctrine-plane
`hooks/scripts/` directory now lives inside the engine itself:

  1. A helper that reached across the doctrine/engine boundary via a
     `sys.path` insert plus an `_engine_root`-resolved import (to reach
     `coordinator_core.write_guards`, `coordinator_core.install`, or
     `coordinator_core.install.door_install`) now imports that claude-klabauter-native
     module directly — the boundary crossing this repo's own
     `docs/reference/boundary-and-data-planes.md` describes no longer
     exists once both sides are the same package. See `sentinel_write_guard`
     (`reconstruct_after`), `forwarder_resolve` (`is_native_image`), and
     `registry_write` (`ml_set`) for the three sites.
  2. A helper whose enrolment registry named SIBLING hook-body files by
     hyphenated filename (`guard_runner.REAL_GUARD_REGISTRY`,
     `stop_family_runner.REAL_STOP_FAMILY_REGISTRY`) lands with that
     registry EMPTY here: the bodies it would enrol are a later wave's
     `writes:` (W4-C5 onward), under claude-klabauter's own underscore-named,
     dotted-importable convention, not DoE's hyphenated-filename
     `importlib.util.spec_from_file_location` shape — see each module's own
     docstring for the enrolment note.

Negative-spec (support-libraries tier convention, carried forward): no
`state/` write lives here, no doctrine authored here, no `register_op()`
call in this package. A future module that wants either belongs in
`coordinator_core/hooks/` proper or in `coordinator_core/ops/`, not here.
"""

from __future__ import annotations
