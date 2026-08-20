"""test_scaffold_ac_checkboxes_are_gate_readable.py — write-side/read-side
contract test between the baton scaffolders in `coordinator-doc-new.py` (and
bug-blitz's spinoff renderer in `readers_blitz.py`) and leg A of
`gates.consumed_handoff_completeness`
(`coordinator_core.workstream_complete.directives_session_hygiene.parse_consumed_handoff_acceptance_criteria`).

The gate's leg A parses a predecessor handoff's own `## Acceptance criteria`
section and counts `- [ ]`/`- [x]` checkboxes; a section present with zero
checkboxes reads as `indeterminate`, not "no criteria." Every writer that
emits an `## Acceptance criteria` heading for a handoff-family kind must
therefore emit at least one checkbox under it, or the gate can never resolve
that baton's completeness.

Covers:
  - The four `coordinator-doc-new.py --type` sites that scaffold a
    handoff-family kind carrying an AC section: spinoff, roadmap-baton,
    goal-seed, roadmap-seed.
  - A regression guard on the count of `## Acceptance criteria` emitter
    sites in `coordinator-doc-new.py` itself, so a new (or removed) site
    doesn't silently fall outside the parametrized list above.
  - `readers_blitz._render_spinoff_handoff_body`, bug-blitz's pure
    body-rendering function, called directly with minimal literal fields
    (not via `build_spinoff_handoff`'s directive wrapper or a heavy
    backlog-item fixture — that wrapper returns a directive dict, not
    the rendered body text itself).

Does NOT cover:
  - `--type handoff` (kind: session-handoff) or `--type recovery`: leg A
    never parses AC for these kinds — it joins on `deliverable_id` and reads
    the resolved plan's `status:` field instead, per this session's verified
    background.
  - Criteria QUALITY or content — this only asserts the mechanical shape
    (heading present, checkbox count > 0) that the gate parser depends on,
    never whether the criteria are well-formed English or good judgment.
  - Any scaffold site's frontmatter/schema validity — that is
    `coordinator/bin/tests/test_coordinator_doc_new*.py`'s job, if such
    files exist; this suite only checks the AC section shape.

Run:
    python3 -m pytest coordinator/bin/tests/test_scaffold_ac_checkboxes_are_gate_readable.py -q -p no:randomly
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAFFOLDER = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"

sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.workstream_complete.directives_session_hygiene import (
    parse_consumed_handoff_acceptance_criteria,
)
from coordinator_core.backlog_grind_assemble.readers_blitz import (
    _render_spinoff_handoff_body,
)

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


_HANDOFF_FAMILY_TYPES_WITH_AC = ["spinoff", "roadmap-baton", "goal-seed", "roadmap-seed"]


#: roadmap-baton is held to the same explicit-sizing-answer bar as --type plan.
#: This suite probes the AC-checkbox section, so it declares absence rather than
#: minting a sizing object it would not otherwise need.
_EXTRA_ARGS: dict[str, list[str]] = {"roadmap-baton": ["--no-sizing-object"]}


def _scaffold(doc_type: str, out_path: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(_SCAFFOLDER),
            "--type",
            doc_type,
            "--title",
            "probe",
            "--out",
            str(out_path),
            *_EXTRA_ARGS.get(doc_type, []),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"coordinator-doc-new.py --type {doc_type} failed rc={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    return out_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("doc_type", _HANDOFF_FAMILY_TYPES_WITH_AC)
def test_scaffold_ac_section_is_gate_readable(doc_type: str, tmp_path: Path) -> None:
    out_path = tmp_path / f"{doc_type}-probe.md"
    body = _scaffold(doc_type, out_path)

    result = parse_consumed_handoff_acceptance_criteria(body)

    assert result is not None, (
        f"--type {doc_type} produced no parseable '## Acceptance criteria' "
        "section — leg A cannot count it"
    )
    assert result["total"] > 0, (
        f"--type {doc_type} emitted an '## Acceptance criteria' heading with "
        "zero checkboxes underneath — leg A reads this as indeterminate"
    )


def test_ac_heading_emitter_site_count_is_covered_by_parametrize_list() -> None:
    """A change to this count means a new (or removed) `## Acceptance
    criteria`-emitting scaffold site exists in coordinator-doc-new.py; the
    `_HANDOFF_FAMILY_TYPES_WITH_AC` parametrize list above must be updated
    to add (or drop) coverage for it before this test is allowed to pass
    again — this test intentionally does not self-heal that drift.
    """
    source = _SCAFFOLDER.read_text(encoding="utf-8")
    site_count = source.count('"## Acceptance criteria"')

    assert site_count == len(_HANDOFF_FAMILY_TYPES_WITH_AC), (
        f"coordinator-doc-new.py now has {site_count} '## Acceptance criteria' "
        f"emitter sites but only {len(_HANDOFF_FAMILY_TYPES_WITH_AC)} types are "
        "covered above — update _HANDOFF_FAMILY_TYPES_WITH_AC to match"
    )


def test_bug_blitz_spinoff_ac_section_is_gate_readable() -> None:
    body = _render_spinoff_handoff_body(
        title="probe bug",
        created="2026-08-14",
        branch="work/probe/2026-08-14",
        run_id="probe-run",
        item_id="probe-item",
        classification_reason="footprint >=3 files",
        scope=["path/a.py", "path/b.py"],
        body="Something breaks under condition X.",
        cross_ref="",
        why_blocked="",
    )

    result = parse_consumed_handoff_acceptance_criteria(body)

    assert result is not None, (
        "_render_spinoff_handoff_body produced no parseable "
        "'## Acceptance criteria' section — leg A cannot count it"
    )
    assert result["total"] > 0, (
        "_render_spinoff_handoff_body emitted an '## Acceptance criteria' "
        "heading with zero checkboxes underneath — leg A reads this as "
        "indeterminate"
    )
