"""test_coordinator_write_review_trail_attestation_dispatch_id.py — argv ->
params forwarding regression test for `coordinator-write-review-trail.py
--attestation-dispatch-id`.

Defect this closes: chunk C2 of docs/plans/2026-08-18-chain-review-records-
and-credits-predecessors.md (landed 0ade09774d4c) made
`attestation_dispatch_id` the op parameter that engages the
reviewer-attestation admission path in
`review_trail_write._guard_foreign_session_range`, and chunk C5 (landed
a4e4e314e1d5) rewrote the refusal message to direct callers to supply
`attestation_dispatch_id` on this facade. The facade never grew the flag —
`--sha-range/--reviewer/--scope/--verdict/--diff-loc/--scope-kind/
--workstream/--reviewed-paths/--reviewer-evidence/--execution-basis` was the
complete set, leaving the admission path reachable only by an in-process op
call. Reported live: cross-repo/inbox/2026-08-18-doe-claude-em-review-trail-
foreign-session-remedy-is-unreachable-from-the-cli.md.

The `cc_invoke.route_mutation` seam is monkeypatched (same idiom as the
sibling archive-stamp-cli / session-claim-cli facade tests) so this suite
asserts ONLY the argv -> `params` translation, never the op behind it (that
is `coordinator_core/ops/test_review_trail_write.py`'s job).

Run:
    pytest coordinator/bin/tests/test_coordinator_write_review_trail_attestation_dispatch_id.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_write_review_trail_attestation_test",
        str(_BIN_DIR / "coordinator-write-review-trail.py"),
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_write_review_trail_attestation_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_BASE_ARGV = [
    "--sha-range", "a1fb455f9^..a1fb455f9",
    "--reviewer", "code-reviewer",
    "--scope", "chain",
    "--verdict", "ok",
    "--diff-loc", "12",
    "--scope-kind", "diff",
]


class AttestationDispatchIdForwardingTest(unittest.TestCase):
    def setUp(self):
        self._orig_route_mutation = _cli.cc_invoke.route_mutation
        self._orig_resolve_repo_root = _cli._resolve_repo_root
        self.addCleanup(self._restore)
        self.calls: list[dict] = []

        def _fake_route_mutation(op, params, repo_root, fallback):
            self.calls.append(params)
            return {"written": True}

        _cli.cc_invoke.route_mutation = _fake_route_mutation
        # abs-path-ok: opaque stub value, never a real filesystem path — repo
        # root is never dereferenced since route_mutation itself is stubbed.
        _cli._resolve_repo_root = lambda: "<stub-repo-root>"

    def _restore(self):
        _cli.cc_invoke.route_mutation = self._orig_route_mutation
        _cli._resolve_repo_root = self._orig_resolve_repo_root

    def test_flag_forwards_into_params(self):
        rc = _cli.main(
            _BASE_ARGV
            + [
                "--reviewer-evidence",
                "state/subagent-share/ffd5a3d9-ad5b-4356-b008-6c194dcee54a/"
                "coordinatorcode-reviewer-509f407e.md",
                "--attestation-dispatch-id",
                "wsc-ancestor-a1fb455f9@session-ffd5a3d9",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.calls[-1]["attestation_dispatch_id"],
            "wsc-ancestor-a1fb455f9@session-ffd5a3d9",
        )

    def test_absent_flag_key_absent_from_params(self):
        """Byte-identical-behaviour guarantee: no --attestation-dispatch-id
        must mean no `attestation_dispatch_id` key reaches the op, same as
        every caller before this flag existed."""
        rc = _cli.main(
            _BASE_ARGV + ["--reviewer-evidence", "waived: no reviewer available"]
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("attestation_dispatch_id", self.calls[-1])


if __name__ == "__main__":
    unittest.main()
