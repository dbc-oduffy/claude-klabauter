"""test_emit_cadence_seam_absent.py — emit-cadence.py's seam-absent exit code.

Purpose: `route_mutation`'s State-1 arm (`coordinator/bin/lib/cc_invoke.py`)
wraps ANY exception `legacy_fn()` raises in a plain `RuntimeError` — the
four-rung `CLAUDE_KLABAUTER_ROOT` remediation text — chained via `raise ... from exc`.
`emit-cadence.py`'s `main()` used to have an `except _SeamAbsentError` arm
ahead of `except RuntimeError`, which can never match: `_SeamAbsentError` is
raised by `legacy_cadence()`, but what actually propagates out of
`route_mutation` on State-1 is the wrapper `RuntimeError`, not the original
exception — a subclass handler cannot catch its own superclass's instance.
So the seam-absent case always fell through to the generic
`except RuntimeError` arm and exited 3 (native transport failure) instead of
the documented exit 1 (seam absent). This test locks the fix: `main()` now
unwraps `exc.__cause__` to recover `_SeamAbsentError` and exits 1.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C15.md
Spec backlink: coordinator/bin/emit-cadence.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "emit-cadence.py"

_loader = importlib.machinery.SourceFileLoader("emit_cadence_cli", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("emit_cadence_cli", _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)


def _run_main(repo_root="/repo/root"):
    with mock.patch.object(_cli_mod, "_gate_is_off", return_value=False), mock.patch.object(
        _cli_mod, "_resolve_repo_root", return_value=repo_root
    ):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = _cli_mod.main()
    return exit_code, stdout.getvalue(), stderr.getvalue()


class TestSeamAbsentExitCode(unittest.TestCase):
    def test_seam_absent_wrapped_runtimeerror_exits_1(self):
        """route_mutation's State-1 arm surfaces a plain RuntimeError whose
        __cause__ is the original _SeamAbsentError — main() must unwrap it
        and exit 1, not fall through to the generic RuntimeError -> exit 3
        arm."""
        seam_absent = _cli_mod._SeamAbsentError(
            "emit-cadence: cockpit emission cadence requires the claude-klabauter "
            "control plane, which is not present in this distribution. "
            "No emission fired."
        )
        wrapper = RuntimeError("four-rung CLAUDE_KLABAUTER_ROOT remediation text")
        wrapper.__cause__ = seam_absent

        def _raise_wrapper(*_a, **_kw):
            raise wrapper

        with mock.patch.object(_cli_mod, "route_mutation", side_effect=_raise_wrapper):
            exit_code, _stdout, stderr = _run_main()

        self.assertEqual(exit_code, 1)
        self.assertIn("cockpit emission cadence requires", stderr)

    def test_unrelated_runtimeerror_still_exits_3(self):
        """A RuntimeError with no _SeamAbsentError __cause__ (a genuine
        post-spawn transport failure) keeps the documented exit 3 — the
        unwrap must not swallow every RuntimeError into exit 1."""

        def _raise_plain(*_a, **_kw):
            raise RuntimeError("post-spawn transport failure")

        with mock.patch.object(_cli_mod, "route_mutation", side_effect=_raise_plain):
            exit_code, _stdout, stderr = _run_main()

        self.assertEqual(exit_code, 3)
        self.assertIn("post-spawn transport failure", stderr)


if __name__ == "__main__":
    unittest.main()
