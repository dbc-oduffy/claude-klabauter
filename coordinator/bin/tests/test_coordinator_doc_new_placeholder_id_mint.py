"""test_coordinator_doc_new_placeholder_id_mint.py -- unit coverage for the
placeholder-title guard on durable artifact-id minting (2026-08-05).

Purpose: every `--title`-less scaffold defaults `title` to a "PLACEHOLDER -- replace
with ..." string, which then flowed into `_slug_from_title` and on into the durable
id mints -- producing ids like `hnd-placeholder-replace-with-one-l-5f04ba` that
survive the author fixing the title afterwards, because ids are minted once at
scaffold time and nothing re-mints them.

Why that is worse than a missing id, and the reason this suite pins REFUSAL rather
than some tidier placeholder value: a placeholder-derived id is *well-formed*. It
matches `gate_eval._HANDOFF_ID_PATTERN`, so a `blocked_by` pointing at one RESOLVES
and silently clears instead of dangling. Every polluted record is a live false-clear
edge. `test_placeholder_id_would_false_clear_a_blocked_by` pins that mechanism
directly against the real pattern, so a future change that reintroduces a
"harmless-looking" placeholder id fails here with the reason attached.

Spec backlink: cross-repo/inbox/2026-08-05-doe-claude-em-placeholder-id-minting-fix-unfiled.md
Sizing: state/sizings/2026-08-05-placeholder-title-guard-at-artifact-id-m.yaml

Negative-spec: does NOT assert that a real title mints any PARTICULAR id -- the mint
basis includes epoch/pid/random by design (see `_mint_artifact_id`). These tests
assert only presence-vs-absence and prefix shape.
Negative-spec: does NOT cover the carry path (`--deliverable-id`/`DELIVERABLE_ID`)
or the roadmap-baton `stub_id` path. Neither derives from the title, so the
placeholder failure mode cannot reach them; that is asserted once, in
`test_carry_path_unaffected_by_placeholder_title`.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py` module --
same load idiom as test_coordinator_doc_new_predecessor.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_placeholder_id_mint.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

from coordinator_core.reconcile.gate_eval import _HANDOFF_ID_PATTERN

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_placeholder_mint_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_placeholder_mint_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_MOD = _load_cli_module()

# The exact strings `main()` assigns when --title is omitted, one per affected
# doc_type family. Copied verbatim rather than imported: if the scaffolder's
# defaults are reworded, these tests must FAIL loudly (the guard's prefix contract
# may no longer hold) instead of silently tracking the new wording.
#
# Review: coordinator:code-reviewer (913d6318) F4 — goal-seed/roadmap-seed now have
# their own dedicated title-default branches (F3 fix), so they are represented here
# by their own distinct strings rather than sharing the generic memo fallback by
# coincidence.
_PLACEHOLDER_TITLES = [
    "PLACEHOLDER — replace with one-line handoff title",
    "PLACEHOLDER — replace with one-line recovery handoff title",
    "PLACEHOLDER — replace with one-line spinoff title",
    "PLACEHOLDER — replace with one-line roadmap-baton stub title",
    "PLACEHOLDER — replace with one-line plan title",
    "PLACEHOLDER — replace with past-tense workstream title",
    "PLACEHOLDER — replace with the PM's ask, verbatim",
    "PLACEHOLDER — replace with memo title",
    "PLACEHOLDER — replace with one-line goal-seed title",
    "PLACEHOLDER — replace with one-line roadmap-seed title",
]


class TestIsPlaceholderTitle(unittest.TestCase):
    def test_every_scaffold_default_is_detected(self):
        for title in _PLACEHOLDER_TITLES:
            with self.subTest(title=title):
                self.assertTrue(
                    _MOD._is_placeholder_title(title),
                    f"scaffold default not detected as a placeholder: {title!r}",
                )

    def test_real_titles_are_not_flagged(self):
        # The third case is the one that makes this more than a smoke test: a
        # legitimate title ABOUT placeholder ids must still mint. A naive
        # `"placeholder" in title.lower()` check passes the first two and fails this.
        for title in [
            "Guard placeholder ids at mint time",
            "C2 supersede gate: honest refusal reason",
            "Placeholder ids leak into blocked_by and false-clear it",
            "placeholder",
        ]:
            with self.subTest(title=title):
                self.assertFalse(
                    _MOD._is_placeholder_title(title),
                    f"real title wrongly flagged as a placeholder: {title!r}",
                )


class TestMintRefusal(unittest.TestCase):
    def test_deliverable_mint_refuses_on_placeholder(self):
        for title in _PLACEHOLDER_TITLES:
            with self.subTest(title=title):
                self.assertIsNone(
                    _MOD._mint_deliverable_id_from_title(title, "handoff")
                )

    def test_hnd_cmp_mint_refuses_on_placeholder(self):
        # Review: coordinator:code-reviewer (913d6318) F1/F2 — pln- was removed from
        # the guard (never in gate_eval._HANDOFF_ID_PATTERN's blast radius; guarding
        # it broke the D3 "plan_id always present, never null" contract). hnd-/cmp-
        # now route through the shared _mint_artifact_id_from_title wrapper (F2).
        for title in _PLACEHOLDER_TITLES:
            with self.subTest(title=title):
                self.assertIsNone(
                    _MOD._mint_artifact_id_from_title("hnd", title, "handoff", "handoff_id")
                )
        self.assertIsNone(
            _MOD._mint_artifact_id_from_title(
                "cmp", "PLACEHOLDER — replace with past-tense workstream title",
                "completion", "completion_id",
            )
        )

    def test_real_title_still_mints_each_id_space(self):
        title = "Guard placeholder ids at mint time"
        # plan_id is unconditional (D3) — no placeholder guard applies to it (F1).
        self.assertTrue(
            _MOD._mint_plan_id(_MOD._slug_from_title(title)).startswith("pln-")
        )
        self.assertTrue(
            _MOD._mint_artifact_id_from_title(
                "hnd", title, "handoff", "handoff_id"
            ).startswith("hnd-")
        )
        self.assertTrue(
            _MOD._mint_artifact_id_from_title(
                "cmp", title, "completion", "completion_id"
            ).startswith("cmp-")
        )
        minted_dlv = _MOD._mint_deliverable_id_from_title(title, "handoff")
        self.assertIsNotNone(minted_dlv)
        self.assertTrue(minted_dlv.startswith("dlv-"))
        # Deliberately NOT asserting "placeholder" is absent from the id: this
        # title contains the word, so the correct id does too. What must be absent
        # is the SCAFFOLD SENTINEL's slug -- the `placeholder-replace-with-` shape
        # that only a defaulted title produces.
        self.assertNotIn("placeholder-replace-with", minted_dlv.lower())

    def test_carry_path_unaffected_by_placeholder_title(self):
        # Carry derives from a caller-supplied id, never the title -- a placeholder
        # title must not suppress it. Pins the guard's blast radius, so a future
        # broadening that routes carry through the refusal fails here.
        carried = _MOD._mint_deliverable_id(
            deliverable_id="dlv-real-carried-id-abc123", carry_source="explicit --deliverable-id"
        )
        self.assertEqual(carried, "dlv-real-carried-id-abc123")


class TestFalseClearMechanism(unittest.TestCase):
    def test_resolver_refuses_a_placeholder_id(self):
        # Originally this pinned the DEFECT's premise -- that the polluted id MATCHED
        # gate_eval's pattern and so resolved-and-cleared a blocked_by. That premise
        # is now false by construction: review finding (code-reviewer 913d6318/F1 and
        # fad4c85d/F1) established the schema narrow alone did NOT close the class,
        # because gate_eval reads frontmatter straight off disk and never validates,
        # so _HANDOFF_ID_PATTERN itself was tightened to match.
        #
        # Flipped rather than deleted: the assertion that the resolver REFUSES a
        # placeholder id is the regression guard for that fix, and it fails loudly if
        # anyone widens the pattern back. Deleting it would have removed the only
        # direct pin on the mechanism this whole change exists to close.
        polluted = "hnd-placeholder-replace-with-one-l-5f04ba"
        self.assertIsNone(
            _HANDOFF_ID_PATTERN.match(polluted),
            "gate_eval's resolver must refuse a placeholder-derived id — if it matches, "
            "a blocked_by naming one resolves and silently clears (the false-clear class)",
        )
        self.assertIsNotNone(
            _HANDOFF_ID_PATTERN.match("hnd-a-real-handoff-title-5f04ba"),
            "the narrow must not over-reject: a genuine id still has to resolve",
        )

    def test_guard_output_cannot_false_clear(self):
        # Refusal yields None -> the field emits as absent/null, which cannot match
        # the pattern and therefore dangles honestly instead of clearing.
        refused = _MOD._mint_artifact_id_from_title(
            "hnd", "PLACEHOLDER — replace with one-line handoff title",
            "handoff", "handoff_id",
        )
        self.assertIsNone(refused)
        for absent in ["", "null", "none"]:
            with self.subTest(absent=absent):
                self.assertIsNone(_HANDOFF_ID_PATTERN.match(absent))


if __name__ == "__main__":
    unittest.main()
