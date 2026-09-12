"""test_coordinator_doc_new_session_segment_sanitizer_parity.py -- pins
byte-parity between coordinator-doc-new.py's `_sanitize_session_segment` and
`coordinator_core.subagent_sandbox.provision_report._sanitize_segment`.

Purpose: review finding (coordinator:code-reviewer, cli-schema slice,
commit `5bcf896bf9`) -- the CLI's whitelist was documented and commented as
mirroring the engine's sanitizer "exactly", but its regex omitted `@`, which
the engine's whitelist admits (to let the EM-side canonical agent id
`<name>@session-<short8>` survive sanitization unchanged). Two independently
hand-copied expectations already drifted apart once under this exact
"exactly" claim; asserting each side against its OWN fixed expected string
would let a future drift of the same shape pass silently again -- one file
changes its regex, the test pinned to that file's old output stays green.

This test instead runs BOTH functions against ONE shared input set and
asserts their outputs are identical to each other, not to a separately
hand-copied literal, so a future re-divergence fails here regardless of
which side moves.

Negative spec: the CLI's `_sanitize_session_segment` fails CLOSED to
'em-unknown' where the engine's `_sanitize_segment` fails open to `None`
(the CLI has no eligibility gate to fail open through -- see its own
docstring). That is a DELIBERATE, documented divergence in failure mode, not
character-set parity, so this test normalizes it explicitly (treats `None`
and `'em-unknown'` as the same "rejected" outcome) rather than asserting
raw-return equality -- asserting raw equality would either false-fail on the
documented failure-mode difference or, worse, invite "fixing" it away.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_slug_truncation_boundary.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_session_segment_sanitizer_parity.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment

_BIN_DIR = Path(__file__).resolve().parent.parent

_REJECTED_SENTINELS = {None, "em-unknown"}

_SHARED_INPUTS = [
    "plain-ascii-segment",
    "agent@session-1a2b3c4d",  # the canonical EM-side agent id shape '@' exists to preserve
    "has spaces and/slashes",
    "colons:and:more",
    "..",
    ".",
    "",
    "___",
    "mixed@case_WITH.dots-and-dashes",
    "trailing-dash-",
    "@leading-at",
    "multiple@@ats@@in@@a@@row",
]


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_segment_parity_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_segment_parity_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()


class TestSessionSegmentSanitizerParity(unittest.TestCase):
    def test_shared_inputs_agree_between_cli_and_engine_sanitizers(self):
        for seg in _SHARED_INPUTS:
            cli_result = _MOD._sanitize_session_segment(seg)
            engine_result = _sanitize_segment(seg)
            if cli_result in _REJECTED_SENTINELS or engine_result in _REJECTED_SENTINELS:
                self.assertIn(
                    cli_result,
                    _REJECTED_SENTINELS,
                    f"input {seg!r}: engine rejected ({engine_result!r}) but CLI did not ({cli_result!r})",
                )
                self.assertIn(
                    engine_result,
                    _REJECTED_SENTINELS,
                    f"input {seg!r}: CLI rejected ({cli_result!r}) but engine did not ({engine_result!r})",
                )
                continue
            self.assertEqual(
                cli_result,
                engine_result,
                f"input {seg!r}: CLI sanitizer {cli_result!r} diverges from engine sanitizer {engine_result!r}",
            )

    def test_at_sign_survives_both_sanitizers_unchanged(self):
        seg = "agent@session-1a2b3c4d"
        self.assertEqual(_MOD._sanitize_session_segment(seg), seg)
        self.assertEqual(_sanitize_segment(seg), seg)


if __name__ == "__main__":
    unittest.main()
