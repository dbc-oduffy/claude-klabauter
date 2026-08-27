"""Coverage for `--deliverable-id` reaching the MUTATING verb.

`_USAGE_LINES` advertises `--deliverable-id ID  (spinoff only) Existing
deliverable_id to carry (never re-mint)` for `baton-assemble` without
restricting it to a verb, and `main()`'s brief arm implements it. Until
2026-08-15 `apply.main_apply` did not parse it at all, so the flag was
reachable only on `brief` -- the read-only verb that mutates nothing.

The live break that produced this file: a spinoff authored to carry an
existing `deliverable_id` was rejected outright (`unrecognized argument
'--deliverable-id'`, exit 3). Passing `brief` the flag and then running
`apply` without it does NOT work around this: `apply()` recomputes the
brief in-process from `(kind, artifact_path)`, so the carried id is lost
and a fresh one is minted -- silently breaking the deliverable spine join
the flag exists to preserve.

Negative spec: this is about ARGUMENT REACHABILITY, not about the carry
semantics themselves (those are `brief()`'s, already covered by
`test_deliverable_collision_warn`). The assertion is that the apply arm
accepts the flag and threads it, never that it re-implements the policy.
"""
from __future__ import annotations

import inspect
import unittest

from coordinator_core.baton_assemble import _USAGE_LINES
from coordinator_core.baton_assemble.apply import apply, main_apply


class TestApplyDeliverableIdFlagIsReachable(unittest.TestCase):
    def test_usage_advertises_the_flag(self) -> None:
        """The premise: the flag is documented on the shared usage block."""
        self.assertTrue(
            any("--deliverable-id" in line for line in _USAGE_LINES),
            "usage no longer advertises --deliverable-id -- if it was "
            "deliberately retired, retire this test with it",
        )

    def test_apply_accepts_explicit_deliverable_id(self) -> None:
        """`apply()` takes the parameter `main_apply` must thread into it."""
        self.assertIn(
            "explicit_deliverable_id",
            inspect.signature(apply).parameters,
            "apply() cannot carry a deliverable_id -- main_apply has nowhere "
            "to thread the flag, so the documented flag is unreachable on the "
            "only verb that writes",
        )

    def test_main_apply_parses_the_flag(self) -> None:
        """The parse arm exists -- the token is never the `else:` reject."""
        source = inspect.getsource(main_apply)
        self.assertIn(
            '"--deliverable-id"',
            source,
            "main_apply does not parse --deliverable-id; it falls through to "
            "the unrecognized-argument reject (the 2026-08-15 live break)",
        )

    def test_main_apply_forwards_it_to_apply(self) -> None:
        """Parsed is not enough -- a parsed-then-dropped flag is the same
        silent re-mint as never parsing it."""
        source = inspect.getsource(main_apply)
        self.assertIn(
            "explicit_deliverable_id=explicit_deliverable_id",
            source,
            "main_apply parses --deliverable-id but does not forward it to "
            "apply() -- the id is dropped and a fresh one is minted",
        )


if __name__ == "__main__":
    unittest.main()
