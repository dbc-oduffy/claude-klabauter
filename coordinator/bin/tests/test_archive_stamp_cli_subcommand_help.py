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

    def test_resolve_memo_help_enumerates_disposition_flags(self):
        with unittest.mock.patch.object(_cli, "_import_module", _explode):
            with unittest.mock.patch("sys.stdout") as out:
                rc = _cli.main(["resolve-memo", "--help"])
        self.assertEqual(rc, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list if c.args)
        self.assertIn("--superseded-by", printed)
        self.assertIn("--decision", printed)
        self.assertIn("mutually exclusive", printed)

    def test_bareword_help_is_not_a_subcommand_help_flag(self):
        self.assertNotIn("help", _cli._SUBCOMMAND_HELP_FLAGS)


class TestDeprecatedAliasDispatch(unittest.TestCase):
    # Only `--help` exercised the alias table before;
    # nothing proved the rewired `_DEPRECATED_ALIASES.get(subcmd) == "..."`
    # condition actually dispatches to the same engine call as the canonical
    # verb. A typo in a map VALUE would silently fall through to bareword
    # positional handling with every existing test still green.
    def test_consume_handoff_dispatches_like_claim_handoff(self):
        mock_mod = unittest.mock.Mock()
        mock_mod.cs_claim_handoff.return_value = {"exit_code": 0, "writes": {}}
        for verb in ("consume-handoff", "claim-handoff"):
            mock_mod.reset_mock()
            with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
                with unittest.mock.patch("sys.stdout"):
                    rc = _cli.main([verb, "state/handoffs/x.md"])
            self.assertEqual(rc, 0)
            mock_mod.cs_claim_handoff.assert_called_once_with(
                "state/handoffs/x.md", return_result=True
            )

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


class TestRepairShippedInClearAdvancementUsage(unittest.TestCase):
    def test_usage_declares_the_flag(self):
        self.assertIn(
            "--clear-advancement", _cli._SUBCOMMAND_USAGE["repair-archived-shipped-in"]
        )

    def test_help_prints_the_flag(self):
        with unittest.mock.patch.object(_cli, "_import_module", lambda: object()):
            with unittest.mock.patch("sys.stdout") as out:
                rc = _cli.main(["repair-archived-shipped-in", "--help"])
        self.assertEqual(rc, 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list if c.args)
        self.assertIn("--clear-advancement", printed)

    def test_unknown_flag_guard_accepts_the_flag(self):
        self.assertIsNone(
            _cli._reject_unknown_flags(
                "repair-archived-shipped-in",
                ["--reason", "r", "--clear-advancement"],
            )
        )


class TestUsageLine(unittest.TestCase):
    def test_usage_line_prints_verbatim_and_refuses(self):
        with unittest.mock.patch("sys.stderr") as err:
            rc = _cli._usage_line(_cli._SUBCOMMAND_USAGE["ship-handoff"])
        self.assertEqual(rc, 2)
        printed = "".join(c.args[0] for c in err.write.call_args_list if c.args)
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
