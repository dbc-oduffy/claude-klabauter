"""test_scaffold_body_is_never_generated.py — standing gate: the scaffolders
supply the SHAPE of a handoff/spinoff body and never its CONTENT.

Purpose: `state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md`
removes chores from the authoring surface, and its own § What must NOT be
automated names the failure mode that removal could slide into:

    "Don't bake content generation into this skill. No heuristic templates that
    fill `## Specification` from the slug."

    "A spinoff whose body was generated from its title is worse than no spinoff
    — the picking-up EM cannot act on it, and only discovers that after paying
    the pickup cost."

A prohibition stated only in prose is a rule the next author has to remember.
This is the artifact that discharges it: scaffold with a title made of
distinctive, unmistakable words and assert that not one of them appears in the
three authorship-bearing sections. A future "helpful" template that seeds
`## Specification` from the slug fails here, loudly, at the moment it is
written.

The gate is deliberately asymmetric. It constrains ONLY the three sections
whose content is the EM's judgment about the work. `## What this covers`,
`## Reference materials`, and the frontmatter are unconstrained — a machine
fact belongs in frontmatter, and the baton's own § The principle puts
machine-knowable facts in the "stamp it" row.

Discharges AC-6 of that baton.

FAST TIER ONLY: no subprocess spawn, no git spawn.

Run:
    python3 -m pytest coordinator/bin/tests/test_scaffold_body_is_never_generated.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import unittest
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "scaffold_body_never_generated_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader("scaffold_body_never_generated_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_A_UUID = "bc1ca482-6b06-4943-ab49-92c9b35482ad"

#: Deliberately absurd, mutually unrelated words. A heuristic that seeds body
#: content from the title or slug cannot help but echo at least one of them,
#: and none of them can plausibly appear in a fixed scaffold template.
_TITLE_TOKENS = ["Rhinoceros", "Trombone", "Escrow", "Persimmon"]
_TITLE = " ".join(_TITLE_TOKENS)

#: The three sections whose content IS the authorship. Everything else in the
#: body is shape.
_AUTHORSHIP_SECTIONS = (
    "## Specification",
    "## Acceptance criteria",
    "## Anti-scope",
)

_HEADING_RE = re.compile(r"^## ")


def _section_body(content: str, heading: str) -> str:
    lines = content.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:  # pragma: no cover — a missing section is its own failure
        raise AssertionError(f"scaffold has no {heading!r} section")
    end = start
    while end < len(lines) and not _HEADING_RE.match(lines[end]):
        end += 1
    return "\n".join(lines[start:end])


def _scaffold_spinoff(title: str) -> str:
    with mock.patch.object(
        _cli, "_resolve_session_id", return_value=_A_UUID
    ), mock.patch.object(
        _cli, "_resolve_session_display_name", return_value="claude-klabauter-51"
    ), mock.patch.object(
        _cli, "_resolve_spinoff_workstream", return_value=None
    ):
        return _cli._scaffold_spinoff(title=title, branch="b")


class SpinoffBodyIsNotGeneratedTest(unittest.TestCase):
    def test_no_title_token_reaches_an_authorship_section(self):
        content = _scaffold_spinoff(_TITLE)
        for heading in _AUTHORSHIP_SECTIONS:
            section = _section_body(content, heading)
            for token in _TITLE_TOKENS:
                self.assertNotIn(
                    token.lower(),
                    section.lower(),
                    f"{heading} echoes the title token {token!r} — the scaffold "
                    f"is generating body content, which the baton's § What must "
                    f"NOT be automated forbids outright",
                )

    def test_the_title_does_reach_the_frontmatter(self):
        """Control: the title is not being scrubbed from the file, it is being
        kept OUT of the sections where authorship lives. Without this, the test
        above would pass on a scaffold that dropped the title entirely."""
        self.assertIn(_TITLE, _scaffold_spinoff(_TITLE))

    def test_acceptance_criteria_ships_one_empty_box_not_a_populated_list(self):
        """The scaffold seeds the SHAPE — a single unticked box, so the author
        writes checkboxes rather than prose — and nothing else. A populated
        criteria list would be exactly the heuristic AC the baton forbids."""
        section = _section_body(_scaffold_spinoff(_TITLE), "## Acceptance criteria")
        boxes = [ln for ln in section.splitlines() if ln.strip().startswith("- [")]
        self.assertEqual(boxes, ["- [ ] "], boxes)

    def test_authorship_sections_carry_only_guidance_comments(self):
        """Every non-blank line in `## Specification` and `## Anti-scope` must
        be an HTML guidance comment. Any prose line is generated content."""
        content = _scaffold_spinoff(_TITLE)
        for heading in ("## Specification", "## Anti-scope"):
            for line in _section_body(content, heading).splitlines():
                if not line.strip():
                    continue
                self.assertTrue(
                    line.strip().startswith("<!--"),
                    f"{heading} carries a non-comment line {line!r} — the "
                    f"scaffold provides shape, the EM provides content",
                )


if __name__ == "__main__":
    unittest.main()
