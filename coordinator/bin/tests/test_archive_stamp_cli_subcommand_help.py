"""test_archive_stamp_cli_subcommand_help.py — `<subcommand> --help` argv
parsing for `archive-stamp-cli` (2026-07-28 fix).

Defect this closes: top-level `--help` was handled, but a per-subcommand
`--help` fell through to that subcommand's own parser, which took the flag as
the positional path argument — `archive-stamp-cli ship-handoff --help`
answered with three stacked `handoff_path escapes state/handoffs/: '--help'`
errors. Since `block_consumed_handoff_edit`'s deny text routes every
handoff close onto `ship-handoff`, that verb's flags (notably `--sha`) were
discoverable only by failing into the remediation text twice.

Also covers `_usage_line`: a complete per-subcommand usage string must print
verbatim, not get the top-level `<subcommand> <args...>` synopsis and the
full verb list appended to it.

The `_import_module()` seam is NOT monkeypatched here — help must be
answerable before the engine import, so these tests double as the assertion
that no CLAUDE_KLABAUTER_ROOT resolution happens on a help path.

Spec backlink: cross-repo memo
cross-repo/inbox/2026-07-28-example-retrieval-repo-em-consumed-handoff-guard-scaffolds-on-close.md
§ "Second, smaller item — the CLI the guard forces you onto".

Run:
    pytest coordinator/bin/tests/test_archive_stamp_cli_subcommand_help.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
import unittest.mock
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_subcommand_help_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(
        "archive_stamp_cli_subcommand_help_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _explode(*_args, **_kwargs):
    raise AssertionError("engine import must not happen on a help path")


class TestSubcommandHelp(unittest.TestCase):
    def test_ship_handoff_help_exits_zero_on_stdout(self):
        with unittest.mock.patch.object(_cli, "_import_module", _explode):
            with unittest.mock.patch("sys.stdout") as out:
                rc = _cli.main(["ship-handoff", "--help"])
        self.assertEqual(rc, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list if c.args)
        self.assertIn("ship-handoff <handoff_path>", printed)
        # The flag the memo could not discover.
        self.assertIn("--sha", printed)
        self.assertNotIn("escapes state/handoffs/", printed)

    def test_short_flag_is_accepted(self):
        with unittest.mock.patch.object(_cli, "_import_module", _explode):
            with unittest.mock.patch("sys.stdout"):
                rc = _cli.main(["close-handoff", "-h"])
        self.assertEqual(rc, 0)

    def test_help_after_a_positional_still_helps(self):
        with unittest.mock.patch.object(_cli, "_import_module", _explode):
            with unittest.mock.patch("sys.stdout"):
                rc = _cli.main(["ship-handoff", "state/handoffs/x.md", "--help"])
        self.assertEqual(rc, 0)

    def test_every_subcommand_has_a_usage_entry(self):
        # The top-level synopsis and the per-subcommand table must not drift
        # apart — a verb listed in one and missing from the other is exactly
        # the discoverability hole this suite exists to close.
        #
        # Exception, by design (92c902051, "rename handoff transition verb
        # consume->claim, unconsume->unclaim"): `_cli._DEPRECATED_ALIASES`
        # names the accepted-but-unadvertised deprecated verbs — deliberately
        # left OUT of the top-level `_SUBCOMMANDS` advertisement, yet still
        # carrying their own `_SUBCOMMAND_USAGE` entry so `<alias> --help`
        # answers directly rather than falling through to the subcommand's
        # own parser (the exact failure mode this suite's module docstring
        # describes). A blanket set-equality assertion would force a false
        # choice between advertising a deprecated verb and deleting its
        # still-functioning help text — neither of which matches the
        # alias-compat design intent.
        listed = {
            v.strip()
            for v in _cli._SUBCOMMANDS.split("\n")[0]
            .removeprefix("subcommands:")
            .split("|")
        }
        self.assertEqual(
            listed | set(_cli._DEPRECATED_ALIASES), set(_cli._SUBCOMMAND_USAGE)
        )

    def test_bareword_help_is_not_a_subcommand_help_flag(self):
        # `action-memo` forwards its tail to the engine verbatim; a disposition
        # value is free to be the string "help", so only the dashed forms count.
        self.assertNotIn("help", _cli._SUBCOMMAND_HELP_FLAGS)


class TestDeprecatedAliasDispatch(unittest.TestCase):
    # Review: code-reviewer — only `--help` exercised the alias table before;
    # nothing proved the rewired `_DEPRECATED_ALIASES.get(subcmd) == "..."`
    # condition actually dispatches to the same engine call as the canonical
    # verb. A typo in a map VALUE would silently fall through to bareword
    # positional handling with every existing test still green.
    def test_consume_handoff_dispatches_like_claim_handoff(self):
        mock_mod = unittest.mock.Mock()
        with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
            rc = _cli.main(["consume-handoff", "state/handoffs/x.md"])
        self.assertEqual(rc, mock_mod.cs_claim_handoff.return_value)
        mock_mod.cs_claim_handoff.assert_called_once_with("state/handoffs/x.md")

        mock_mod.reset_mock()
        with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
            rc = _cli.main(["claim-handoff", "state/handoffs/x.md"])
        self.assertEqual(rc, mock_mod.cs_claim_handoff.return_value)
        mock_mod.cs_claim_handoff.assert_called_once_with("state/handoffs/x.md")

    def test_unconsume_handoff_dispatches_like_unclaim_handoff(self):
        mock_mod = unittest.mock.Mock()
        with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
            rc = _cli.main(["unconsume-handoff", "state/handoffs/x.md", "a note"])
        self.assertEqual(rc, mock_mod.cs_unclaim_handoff.return_value)
        mock_mod.cs_unclaim_handoff.assert_called_once_with(
            "state/handoffs/x.md", "a note", None
        )

        mock_mod.reset_mock()
        with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
            rc = _cli.main(["unclaim-handoff", "state/handoffs/x.md", "a note"])
        self.assertEqual(rc, mock_mod.cs_unclaim_handoff.return_value)
        mock_mod.cs_unclaim_handoff.assert_called_once_with(
            "state/handoffs/x.md", "a note", None
        )


class TestUsageLine(unittest.TestCase):
    def test_usage_line_prints_verbatim_and_refuses(self):
        with unittest.mock.patch("sys.stderr") as err:
            rc = _cli._usage_line(_cli._SUBCOMMAND_USAGE["ship-handoff"])
        self.assertEqual(rc, 2)
        printed = "".join(c.args[0] for c in err.write.call_args_list if c.args)
        # No top-level synopsis, no full verb list appended.
        self.assertNotIn("<subcommand> <args...>", printed)
        self.assertNotIn("repair-archived-shipped-in", printed)

    def test_bare_ship_handoff_refuses_with_its_own_usage_only(self):
        with unittest.mock.patch.object(_cli, "_import_module", lambda: object()):
            with unittest.mock.patch("sys.stderr") as err:
                rc = _cli.main(["ship-handoff"])
        self.assertEqual(rc, 2)
        printed = "".join(c.args[0] for c in err.write.call_args_list if c.args)
        self.assertIn("ship-handoff <handoff_path>", printed)
        self.assertNotIn("<subcommand> <args...>", printed)

    def test_unknown_subcommand_still_gets_the_full_verb_list(self):
        with unittest.mock.patch.object(_cli, "_import_module", lambda: object()):
            with unittest.mock.patch("sys.stderr") as err:
                rc = _cli.main(["bogus"])
        self.assertEqual(rc, 2)
        printed = "".join(c.args[0] for c in err.write.call_args_list if c.args)
        self.assertIn("unknown subcommand", printed)
        self.assertIn("repair-archived-shipped-in", printed)


if __name__ == "__main__":
    unittest.main()
