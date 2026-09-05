"""`fleet.archive_terminal_sizings` could not read a sizing-object at all.

Two independent reads stood between a shipped sizing and its archive, and both
were silently answering None on this corpus:

1. `parse_frontmatter_status` delegates to `dag._read_meta`, which parses
   MARKDOWN frontmatter and returns `{}` for anything not opening with a `---`
   fence. Sizing-objects are bare YAML documents. Every one of the 425 records
   in `state/sizings/` therefore read as `status: None`, the op enumerated zero
   candidates, and 81 terminal records accumulated while the sweep reported
   itself clean. `session_facts._read_frontmatter_status` read the same files
   correctly the whole time -- the surface that REPORTS the backlog and the op
   that CLEARS it did not share a status reader.

2. The AC6 forward-pointer gate resolved a sizing's `plan:` FK literally, at
   `docs/plans/<id>.md`. A shipped plan has been moved to
   `archive/specs/<month>/<id>.md` and nothing rewrites the citation, so the
   gate read every such plan as dangling and refused the sizing in place. Once
   read 1 was fixed this held 37 of 41 candidates -- a guard that looks
   conservative while actually firing on the wrong signal.

Negative-spec: these tests never assert that a sizing IS archived by status
alone. The forward-plan gate is load-bearing and each case below pins which
signal it fires on.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet._common import parse_frontmatter_status
from coordinator_core.ops.fleet.archive_sizings import (
    _forward_plan_refusal_reason,
    _read_plan_fk,
    _resolve_plan_fk,
)

_SIZING = "deliverable_id: dlv-x\nschema: sizing-object\nstatus: shipped\nplan: docs/plans/p.md\n"


def test_bare_yaml_sizing_status_is_read(tmp_path):
    record = tmp_path / "2026-08-01-x.yaml"
    record.write_text(_SIZING, encoding="utf-8")
    assert parse_frontmatter_status(record) == "shipped"


def test_bare_yaml_plan_fk_is_read(tmp_path):
    record = tmp_path / "2026-08-01-x.yaml"
    record.write_text(_SIZING, encoding="utf-8")
    assert _read_plan_fk(record) == "docs/plans/p.md"


def test_null_plan_fk_reads_as_absent(tmp_path):
    record = tmp_path / "2026-08-01-x.yaml"
    record.write_text("schema: sizing-object\nstatus: shipped\nplan: null\n", encoding="utf-8")
    assert _read_plan_fk(record) is None


def test_fenced_record_still_reads_through_the_fenced_parser(tmp_path):
    """The fallback must not change how a markdown record is read."""
    record = tmp_path / "plan.md"
    record.write_text("---\nstatus: implemented\n---\n\n# body\nstatus: decoy\n", encoding="utf-8")
    assert parse_frontmatter_status(record) == "implemented"


def test_fenced_record_without_status_does_not_fall_back_into_the_body(tmp_path):
    """A fence with no `status:` is an ANSWER -- None -- not a miss to retry.
    Falling through would let a `status:` line in prose masquerade as one."""
    record = tmp_path / "plan.md"
    record.write_text("---\ntitle: t\n---\n\nstatus: decoy\n", encoding="utf-8")
    assert parse_frontmatter_status(record) is None


def _plan(path: Path, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: {status}\n---\n", encoding="utf-8")


def test_plan_fk_resolves_to_the_live_plan_first(tmp_path):
    _plan(tmp_path / "docs" / "plans" / "p.md", "implemented")
    _plan(tmp_path / "archive" / "specs" / "2026-08" / "p.md", "executing")
    assert _resolve_plan_fk(tmp_path, "docs/plans/p.md") == tmp_path / "docs" / "plans" / "p.md"
    assert _forward_plan_refusal_reason(tmp_path, "docs/plans/p.md") is None


def test_plan_fk_resolves_under_archive_specs_when_the_plan_has_shipped(tmp_path):
    _plan(tmp_path / "archive" / "specs" / "2026-08" / "p.md", "implemented")
    assert _resolve_plan_fk(tmp_path, "docs/plans/p.md") is not None
    assert _forward_plan_refusal_reason(tmp_path, "docs/plans/p.md") is None


def test_an_archived_plan_that_is_not_terminal_still_refuses(tmp_path):
    """Resolution is not permission: the gate still reads the plan's status."""
    _plan(tmp_path / "archive" / "specs" / "2026-08" / "p.md", "executing")
    reason = _forward_plan_refusal_reason(tmp_path, "docs/plans/p.md")
    assert reason is not None and "not terminal" in reason


def test_an_ambiguous_archive_match_resolves_to_nothing(tmp_path):
    """Two same-basename records mean the resolver cannot say which plan was
    meant, so it says nothing -- and an unresolvable FK is a refusal, never
    'no constraint'. Mirrors `_sizing_citation._archive_sizings_fallback`."""
    _plan(tmp_path / "archive" / "specs" / "2026-07" / "p.md", "implemented")
    _plan(tmp_path / "archive" / "specs" / "2026-08" / "p.md", "implemented")
    assert _resolve_plan_fk(tmp_path, "docs/plans/p.md") is None
    reason = _forward_plan_refusal_reason(tmp_path, "docs/plans/p.md")
    assert reason is not None and "could not be resolved" in reason


def test_a_genuinely_missing_plan_fk_is_still_a_refusal(tmp_path):
    reason = _forward_plan_refusal_reason(tmp_path, "docs/plans/nope.md")
    assert reason is not None and "could not be resolved" in reason


# ---------------------------------------------------------------------------
# Read 3: the unfenced fallback's head window
#
# The same chain's third silent-None. `parse_frontmatter_field` scanned only
# `text[:4096]` of an unfenced record. A sizing-object spells `status:` as an
# ordinary top-level key wherever the scaffolder left it, and on this corpus
# three carried it at byte 5693, 6385 and 9051 -- read as None, refused as
# non-terminal, stranded while the sweep reported itself clean. Same shape as
# reads 1 and 2 above: a reader disagreeing with the corpus and failing toward
# "nothing to do". The window bought no I/O -- the whole file is already in
# memory one line earlier -- and it mis-taught its own investigation, which
# read the three sweepable records as correlating with a missing trailing
# `# draft | sized | ...` comment when the real discriminator was byte offset.
# ---------------------------------------------------------------------------

_PAST_WINDOW_PAD = "notes: " + ("x" * 6000) + "\n"


def test_status_past_the_4096_byte_head_window_is_still_read(tmp_path: Path) -> None:
    """A top-level `status:` beyond byte 4096 of an unfenced record reads back."""
    p = tmp_path / "sizing.yaml"
    p.write_text(
        "schema: sizing-object\n" + _PAST_WINDOW_PAD + "status: shipped\n",
        encoding="utf-8",
    )
    assert p.stat().st_size > 4096
    assert parse_frontmatter_status(p) == "shipped"


def test_trailing_comment_is_not_the_discriminator(tmp_path: Path) -> None:
    """The scaffolder's trailing enum comment never blocked the read.

    Pins the hypothesis that the byte-offset investigation ruled out, so it is
    not re-run: a trailing `# draft | sized | ...` comment is stripped by
    `read_fm_field_unquoted` at any offset, inside the old window or past it.
    """
    comment = "  # draft | sized | routed | shipped | declined | superseded"
    near = tmp_path / "near.yaml"
    near.write_text("status: shipped" + comment + "\n", encoding="utf-8")
    far = tmp_path / "far.yaml"
    far.write_text(_PAST_WINDOW_PAD + "status: shipped" + comment + "\n", encoding="utf-8")
    assert parse_frontmatter_status(near) == "shipped"
    assert parse_frontmatter_status(far) == "shipped"


def test_indented_status_past_the_window_is_not_read_as_top_level(tmp_path: Path) -> None:
    """Widening the scan must not let a nested `status:` answer for the record.

    `read_fm_field`'s pattern is column-anchored under re.MULTILINE; this is
    the assertion that keeps that property load-bearing rather than incidental,
    since the head window used to hide any such match by truncation.
    """
    p = tmp_path / "nested.yaml"
    p.write_text(
        "schema: sizing-object\n" + _PAST_WINDOW_PAD + "system:\n  status: shipped\n",
        encoding="utf-8",
    )
    assert parse_frontmatter_status(p) is None


def test_fenced_record_is_untouched_by_the_widened_scan(tmp_path: Path) -> None:
    """The fallback still fires only when there is no fence -- negative-spec."""
    p = tmp_path / "plan.md"
    p.write_text(
        "---\nstatus: implemented\n---\n\n" + _PAST_WINDOW_PAD + "status: shipped\n",
        encoding="utf-8",
    )
    assert parse_frontmatter_status(p) == "implemented"
