"""test_coordinator_doc_new_spinoff_marker.py -- standing gate: the trailing
`<!-- spinoff: ... -->` greppability marker is STAMPED by `_scaffold_spinoff`,
never hand-typed by the authoring EM (2026-08-21).

Purpose: `skills/spinoff/SKILL.md` Step 2 instructed the human-driven EM to
type the marker by hand at the end of every spinoff, while
`coordinator_core/backlog_grind_assemble/readers_blitz.py` already emitted the
same line programmatically on the bug-blitz path. Every fact in the line
(`created`, the authoring session, the EM display name) is resolved at scaffold
time and already emitted as frontmatter, so the chore falls in R6's
"machine knows it -- stamp it" row
(`docs/research/spike-verdicts/2026-08-21-ceremony-assemblers-cost-attribution.md`
§ PM rulings; `state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md`
§ Known instances 1).

What this gate holds:
  - the marker is present in every scaffolded spinoff body;
  - its `<!-- spinoff: ` prefix stays byte-identical to the bug-blitz
    producer's, so the two producers remain ONE grep (the greppability leg of
    that baton's AC-2);
  - every value in it agrees with the frontmatter the same scaffold emitted --
    a marker that disagrees with `created:`/`authoring_session:` would be a
    second, divergent source of the same facts;
  - an unresolvable display name degrades to the literal `current EM` (R1:
    absence is information) rather than omitting the marker or scanning for an
    identity.

FAST TIER ONLY: no subprocess spawn, no git spawn -- `_scaffold_spinoff` is
called in-process with every resolver mocked at the point of use. Module loaded
by file path since `coordinator-doc-new.py` is an extensionless polyglot
entrypoint; same idiom as this directory's sibling suites.

Run:
    python3 -m pytest coordinator/bin/tests/test_coordinator_doc_new_spinoff_marker.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import unittest
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_spinoff_marker_test",
        str(_BIN_DIR / "coordinator-doc-new.py"),
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_spinoff_marker_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_A_UUID = "bc1ca482-6b06-4943-ab49-92c9b35482ad"
_MARKER_RE = re.compile(r"^<!-- spinoff: (\S+) by (.+) during (\S+) -->$")


def _scaffold(display_name: str | None = "claude-klabauter-51") -> str:
    with mock.patch.object(
        _cli, "_resolve_session_id", return_value=_A_UUID
    ), mock.patch.object(
        _cli, "_resolve_session_display_name", return_value=display_name
    ), mock.patch.object(
        _cli, "_resolve_spinoff_workstream", return_value=None
    ):
        return _cli._scaffold_spinoff(title="t", branch="b")


def _marker_line(content: str) -> str:
    markers = [
        line for line in content.splitlines() if line.startswith("<!-- spinoff: ")
    ]
    assert len(markers) == 1, f"expected exactly one marker, got {markers!r}"
    return markers[0]


class SpinoffMarkerIsStampedTest(unittest.TestCase):
    def test_marker_is_present_and_well_formed(self):
        match = _MARKER_RE.match(_marker_line(_scaffold()))
        self.assertIsNotNone(
            match, "scaffolded marker does not match the canonical marker shape"
        )

    def test_marker_is_the_last_non_blank_line(self):
        """The marker is a trailing file marker, not a mid-body element -- an
        EM appending a section below it would be re-opening the file after its
        own terminator."""
        lines = [ln for ln in _scaffold().splitlines() if ln.strip()]
        self.assertTrue(lines[-1].startswith("<!-- spinoff: "))

    def test_marker_values_agree_with_the_frontmatter(self):
        """The marker restates facts the frontmatter already carries. If the
        two ever diverge, one of them is lying -- this pins them together."""
        content = _scaffold()
        created, who, session = _MARKER_RE.match(_marker_line(content)).groups()
        self.assertIn(f"created: {created}", content)
        self.assertIn(f'authoring_session: "{session}"', content)
        self.assertEqual(session, _A_UUID)
        self.assertIn(f"# minted by {who}", content)

    def test_unresolvable_display_name_degrades_to_current_em(self):
        """R1 -- absence is information. No display name means the literal
        words the hand-typed form used, never a corpus scan for an identity
        and never a dropped marker."""
        content = _scaffold(display_name=None)
        created, who, session = _MARKER_RE.match(_marker_line(content)).groups()
        self.assertEqual(who, "current EM")
        self.assertEqual(session, _A_UUID)


class SpinoffMarkerStaysOneGrepTest(unittest.TestCase):
    """Greppability leg: the hand path and the bug-blitz path must remain
    findable by the same single prefix. A divergence here silently splits the
    corpus in two for anyone grepping for spinoff provenance."""

    def test_prefix_matches_the_bug_blitz_producer(self):
        readers_blitz = (
            _REPO_ROOT
            / "coordinator_core"
            / "backlog_grind_assemble"
            / "readers_blitz.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'f"<!-- spinoff: {created} by bug-blitz {run_id} -->"',
            readers_blitz,
            "the bug-blitz marker producer changed shape; the scaffolder's "
            "_spinoff_marker must change with it or the two stop being one grep",
        )
        self.assertTrue(_marker_line(_scaffold()).startswith("<!-- spinoff: "))


if __name__ == "__main__":
    unittest.main()
