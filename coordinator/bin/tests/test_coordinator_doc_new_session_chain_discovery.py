"""test_coordinator_doc_new_session_chain_discovery.py — coverage for the
session-chain discovery rung added 2026-08-25.

Defect it pins (state/bug-backlog/2026-08-25-deliverable-id-minted-from-title-
not-discovered-d2b445e3e44a.yaml, found on two independent chains the same
day): every carry rung in the scaffolder answers "was an id HANDED to me" —
explicit flag, DELIVERABLE_ID env, cited sizing, explicit predecessor edge.
None asked whether the chain being authored into ALREADY has one, so two
artifacts of one deliverable scaffolded under two titles minted two ids off
two title slugs, silently, and the split only surfaced at a deliverable-level
rollup — in shared history, unrepairable in place.

Covers both halves: the engine tier (`deliverable_carry.
resolve_session_chain_deliverable_id`) and the CLI wiring
(`_mint_deliverable_id_from_title` preferring a discovered chain id over a
title mint, and `--new-chain`/spinoff/roadmap-baton suppressing it).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint — same load
idiom as test_coordinator_doc_new_plural_carry.py. Calls the helpers directly,
in-process, no subprocess.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_session_chain_discovery.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from coordinator_core.ops.deliverable_carry import (
    resolve_session_chain_deliverable_id,
)
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_chain_discovery_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_chain_discovery_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _write_handoff(tmp: Path, deliverable_id: str | None) -> str:
    path = tmp / "2026-08-25_the-held-handoff.md"
    lines = ["---", "kind: handoff", "title: The held handoff"]
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    lines += ["---", "", "body"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


class EngineTierTest(unittest.TestCase):
    """`resolve_session_chain_deliverable_id` — carry on a hit, omit on
    every absence, never raise."""

    def setUp(self):
        self._tmp = Path(__file__).resolve().parent / "_chain_discovery_tmp"
        self._tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for child in self._tmp.iterdir():
            child.unlink()
        self._tmp.rmdir()

    def test_carries_the_held_handoffs_id(self):
        path = _write_handoff(self._tmp, "dlv-the-one-true-chain-abc123")
        self.assertEqual(
            resolve_session_chain_deliverable_id(read_frontmatter_field, path),
            "dlv-the-one-true-chain-abc123",
        )

    def test_absent_id_field_omits_rather_than_guesses(self):
        path = _write_handoff(self._tmp, None)
        self.assertIsNone(
            resolve_session_chain_deliverable_id(read_frontmatter_field, path)
        )

    def test_null_id_omits(self):
        path = _write_handoff(self._tmp, "null")
        self.assertIsNone(
            resolve_session_chain_deliverable_id(read_frontmatter_field, path)
        )

    def test_no_path_and_missing_file_both_omit_without_raising(self):
        self.assertIsNone(
            resolve_session_chain_deliverable_id(read_frontmatter_field, None)
        )
        self.assertIsNone(
            resolve_session_chain_deliverable_id(
                read_frontmatter_field, str(self._tmp / "nope.md")
            )
        )


class CliWiringTest(unittest.TestCase):
    """`_mint_deliverable_id_from_title` — a discovered chain id outranks a
    title mint, and the exemptions suppress discovery."""

    def setUp(self):
        _cli._NEW_CHAIN_REQUESTED = False

    tearDown = setUp

    def test_discovered_chain_id_is_carried_never_reminted(self):
        with mock.patch.object(
            _cli, "_resolve_session_chain_deliverable_id",
            return_value="dlv-the-one-true-chain-abc123",
        ), mock.patch.object(_cli, "_mint_deliverable_id") as minted:
            minted.side_effect = lambda **kw: kw.get("deliverable_id")
            got = _cli._mint_deliverable_id_from_title(
                "A sizing authored beside a live chain", "sizing-object", "/repo"
            )
        self.assertEqual(got, "dlv-the-one-true-chain-abc123")
        self.assertEqual(minted.call_args.kwargs["deliverable_id"],
                         "dlv-the-one-true-chain-abc123")
        self.assertNotIn("slug", minted.call_args.kwargs)

    def test_no_discovery_falls_back_to_the_title_mint_unchanged(self):
        with mock.patch.object(
            _cli, "_resolve_session_chain_deliverable_id", return_value=None
        ), mock.patch.object(_cli, "_mint_deliverable_id") as minted:
            minted.return_value = "dlv-minted-from-title-abc123"
            got = _cli._mint_deliverable_id_from_title(
                "A chain root", "sizing-object", "/repo"
            )
        self.assertEqual(got, "dlv-minted-from-title-abc123")
        self.assertIn("slug", minted.call_args.kwargs)

    def test_discovered_id_beats_a_placeholder_title_refusal(self):
        """A discovered id is not title-derived, so the placeholder guard —
        which exists to stop a PLACEHOLDER becoming durable — must not
        withhold it."""
        with mock.patch.object(
            _cli, "_resolve_session_chain_deliverable_id",
            return_value="dlv-the-one-true-chain-abc123",
        ), mock.patch.object(_cli, "_mint_deliverable_id") as minted:
            minted.side_effect = lambda **kw: kw.get("deliverable_id")
            got = _cli._mint_deliverable_id_from_title(
                "PLACEHOLDER — replace with the real title", "handoff", "/repo"
            )
        self.assertEqual(got, "dlv-the-one-true-chain-abc123")

    def test_new_chain_flag_suppresses_discovery(self):
        _cli._NEW_CHAIN_REQUESTED = True
        with mock.patch.object(_cli, "_resolve_session_held_handoff_path") as held:
            self.assertIsNone(
                _cli._resolve_session_chain_deliverable_id("sizing-object", "/repo")
            )
        held.assert_not_called()

    def test_spinoff_roadmap_baton_and_plan_are_exempt(self):
        """A spinoff mints its own id (PM, 2026-08-05); a roadmap baton's
        identity is its stub_id; and `plan` already asks this question one
        tier earlier, kind-gated, where AC4b's false-merge ruling says a held
        non-roadmap baton is NOT carry evidence. Without the `plan` exemption
        this tier re-reads the file that tier just rejected and carries it
        anyway — see `_resolve_session_chain_deliverable_id`'s docstring."""
        with mock.patch.object(_cli, "_resolve_session_held_handoff_path") as held:
            for doc_type in ("spinoff", "roadmap-baton", "plan"):
                self.assertIsNone(
                    _cli._resolve_session_chain_deliverable_id(doc_type, "/repo")
                )
        held.assert_not_called()

    def test_omitted_repo_root_disables_discovery(self):
        self.assertIsNone(_cli._resolve_session_held_handoff_path(None))


if __name__ == "__main__":
    unittest.main()
