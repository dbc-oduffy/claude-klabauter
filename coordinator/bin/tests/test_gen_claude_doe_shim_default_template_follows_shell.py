"""test_gen_claude_doe_shim_default_template_follows_shell.py — the default
`--template` the trampoline injects must follow `--shell`.

Regression origin: the coordinator-claude memo of 2026-08-13 (PowerShell shim landed).
`_default_template_path()` hardcoded `claude-doe-shim.sh.tmpl` and never
branched on `--shell`, so `gen-claude-doe-shim --shell powershell` with no
explicit `--template` rendered the bash oracle's bytes into a file named
`claude-doe-shim.ps1`. That failure is silent by construction: the render
succeeds, `--check-only` reports "Template valid", and the breakage only
surfaces as a PowerShell profile that throws at every subsequent shell start.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since the CLI's
filename is hyphenated and not importable as a module.

Run: pytest coordinator/bin/tests/test_gen_claude_doe_shim_default_template_follows_shell.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "gen_claude_doe_shim_cli", str(_BIN_DIR / "gen-claude-doe-shim.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestDefaultTemplateFollowsShell(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli()

    def test_bash_family_resolves_the_sh_template(self):
        self.assertEqual(
            os.path.basename(self.cli._default_template_path("bash")),
            "claude-doe-shim.sh.tmpl",
        )

    def test_powershell_family_resolves_the_ps1_template(self):
        self.assertEqual(
            os.path.basename(self.cli._default_template_path("powershell")),
            "claude-doe-shim.ps1.tmpl",
        )

    def test_omitted_family_keeps_the_bash_default(self):
        self.assertEqual(
            os.path.basename(self.cli._default_template_path()),
            "claude-doe-shim.sh.tmpl",
        )

    def test_unrecognized_family_falls_through_to_bash(self):
        """The trampoline does not validate — the engine's own `--shell` check
        rejects the value before the template is ever read."""
        self.assertEqual(
            os.path.basename(self.cli._default_template_path("fish")),
            "claude-doe-shim.sh.tmpl",
        )


class TestShellFamilyFromArgv(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli()

    def test_reads_the_space_separated_form(self):
        self.assertEqual(
            self.cli._shell_family_from_argv(["--check-only", "--shell", "powershell"]),
            "powershell",
        )

    def test_defaults_to_bash_when_absent(self):
        self.assertEqual(self.cli._shell_family_from_argv(["--check-only"]), "bash")

    def test_trailing_flag_without_a_value_defaults_to_bash(self):
        """A dangling `--shell` is the engine's error to report, not a crash here."""
        self.assertEqual(self.cli._shell_family_from_argv(["--shell"]), "bash")


class TestBothTemplatesExistOnDisk(unittest.TestCase):
    """The `.ps1.tmpl` default is only correct because coordinator-claude ships the
    template at the mirrored name; if that ever drifts, the short form silently
    regresses to a template-not-found instead of a wrong-language render."""

    def setUp(self):
        self.cli = _load_cli()

    def test_resolved_defaults_are_real_files(self):
        for family in ("bash", "powershell"):
            with self.subTest(family=family):
                try:
                    path = self.cli._default_template_path(family)
                except RuntimeError as exc:
                    self.skipTest(f"data_root('templates') unresolvable here: {exc}")
                self.assertTrue(os.path.isfile(path), f"missing template: {path}")


if __name__ == "__main__":
    unittest.main()
