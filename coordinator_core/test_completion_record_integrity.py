"""
coordinator_core.test_completion_record_integrity

Unit coverage for the ONE shared hollow-completion-record predicate --
`hollow_reasons` / `hollow_reasons_for_fields` -- and its two refusal
wrappers, `assert_finalize_ready` / `assert_fields_finalize_ready`.

Spec backlink: docs/plans/2026-09-28 hollow-completion-record refusal chunk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import completion_record_integrity as integrity


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_WELL_FORMED = """---
title: "A real title"
created: 2026-07-28
nature: infra
chain: "myplan"
commits:
  - "deadbeef"
status: pending-release
chain_terminal: true
authored_by: "sess-1"
---

Real prose describing what shipped.
"""

_FULLY_HOLLOW = """---
title: "PLACEHOLDER — replace with past-tense workstream title"
created: 2026-09-20
nature: tech-debt
chain: "myplan"
commits: []
status: pending-release
chain_terminal: false
---

<!-- PROSE: Replace this with a ≤8-sentence past-tense description of what shipped and why it matters. -->
"""


def test_well_formed_record_passes_every_check(tmp_path: Path) -> None:
    repo = tmp_path
    _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: implemented\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-07" / "entry.md"
    _write(record, _WELL_FORMED)

    assert integrity.hollow_reasons(record, repo) == []
    integrity.assert_finalize_ready(record, repo)  # does not raise


def test_fully_hollow_record_fails_all_three_checks(tmp_path: Path) -> None:
    repo = tmp_path
    _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(record, _FULLY_HOLLOW)

    reasons = integrity.hollow_reasons(record, repo)
    assert reasons == [
        integrity.REASON_PLACEHOLDER,
        integrity.REASON_EMPTY_COMMITS,
        integrity.REASON_PLAN_NOT_LANDED,
    ]

    with pytest.raises(integrity.HollowCompletionRecordError):
        integrity.assert_finalize_ready(record, repo)


def test_the_defect_fixture_reproduces_the_original_incident(tmp_path: Path) -> None:
    """Same field shape as archive/completed/2026-09/2026-09-20-2026-09-18-
    doe-holds-no-scripts-188007.md: real chain/deliverable_id/authored_by/loe
    filled in, but commits: [] , PROSE placeholder still present, and the
    governing plan still status: executing. Pins the exact incident shape."""
    repo = tmp_path
    slug = "2026-09-18-doe-holds-no-scripts"
    _write(repo / "docs" / "plans" / f"{slug}.md", "---\nstatus: executing\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        f"""---
title: "Accomplished: DoE holds no scripts: absorb DoE executables into claude-klabauter"
created: 2026-09-20
nature: tech-debt
chain: "{slug}"
deliverable_id: "dlv-x"
commits: []
status: pending-release
chain_terminal: false
authored_by: "sess-1"
---

<!-- PROSE: Replace this with a ≤8-sentence past-tense description of what shipped and why it matters. -->
""",
    )
    reasons = integrity.hollow_reasons(record, repo)
    assert reasons == [
        integrity.REASON_PLACEHOLDER,
        integrity.REASON_EMPTY_COMMITS,
        integrity.REASON_PLAN_NOT_LANDED,
    ]
    with pytest.raises(integrity.HollowCompletionRecordError):
        integrity.assert_finalize_ready(record, repo)


# ---------------------------------------------------------------------------
# assert_finalize_ready refuses on ANY single failing check, not just the
# all-three shape -- a defective record is defective on one axis alone.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("commits", [integrity.REASON_PLACEHOLDER, integrity.REASON_PLAN_NOT_LANDED]),
        ("plan", [integrity.REASON_PLACEHOLDER, integrity.REASON_EMPTY_COMMITS]),
        ("prose", [integrity.REASON_EMPTY_COMMITS, integrity.REASON_PLAN_NOT_LANDED]),
    ],
)
def test_fixing_one_axis_still_leaves_two_failing_and_still_refuses(tmp_path, mutation, expected) -> None:
    """Fixing one of the three axes while leaving the other two hollow is
    still refused -- `assert_finalize_ready` refuses on ANY nonempty
    reasons list, not only the all-three shape."""
    repo = tmp_path
    text = _FULLY_HOLLOW
    if mutation == "commits":
        text = text.replace("commits: []", 'commits:\n  - "deadbeef"')
        _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")
    elif mutation == "plan":
        _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: implemented\n---\nbody\n")
    elif mutation == "prose":
        text = text.replace(
            "<!-- PROSE: Replace this with a ≤8-sentence past-tense description of "
            "what shipped and why it matters. -->",
            "Real prose here.",
        )
        text = text.replace(integrity.PLACEHOLDER_TITLE, "A real, authored title")
        _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")

    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(record, text)

    reasons = integrity.hollow_reasons(record, repo)
    assert reasons == expected
    with pytest.raises(integrity.HollowCompletionRecordError):
        integrity.assert_finalize_ready(record, repo)


def test_a_single_failing_axis_alone_is_still_refused(tmp_path: Path) -> None:
    """The narrowest possible defect -- ONLY empty commits, everything else
    authored and the plan landed -- is still refused at finalize. This is
    the EM's correction: a fresh-scaffold-shaped all-three gate would miss
    this."""
    repo = tmp_path
    _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: landed\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "A real title"\nchain: "myplan"\ncommits: []\n---\n\nReal prose.\n',
    )
    assert integrity.hollow_reasons(record, repo) == [integrity.REASON_EMPTY_COMMITS]
    with pytest.raises(integrity.HollowCompletionRecordError):
        integrity.assert_finalize_ready(record, repo)


def test_no_chain_never_fails_the_plan_check(tmp_path: Path) -> None:
    """An adhoc/standalone entry (no `chain:`) has no governing plan to
    check -- neither plan-related reason ever fires for it."""
    repo = tmp_path
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    assert integrity.hollow_reasons(record, repo) == []


def test_unreadable_record_fails_every_checkable_reason(tmp_path: Path) -> None:
    """An unreadable record degrades to failing placeholder + empty-commits
    + plan-unresolvable (NOT plan-not-landed -- an unparseable record has no
    resolved `chain:`, so "unresolvable" is the honest reason)."""
    repo = tmp_path
    missing = repo / "archive" / "completed" / "2026-09" / "does-not-exist.md"
    reasons = integrity.hollow_reasons(missing, repo)
    assert reasons == [
        integrity.REASON_PLACEHOLDER,
        integrity.REASON_EMPTY_COMMITS,
        integrity.REASON_PLAN_UNRESOLVABLE,
    ]


def test_governing_plan_info_reports_the_walk(tmp_path: Path) -> None:
    repo = tmp_path
    _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(record, _FULLY_HOLLOW)

    info = integrity.governing_plan_info(record, repo)
    assert info.chain_slug == "myplan"
    assert info.plan_status == "executing"
    assert info.resolved is True
    assert info.plan_path is not None and info.plan_path.endswith("myplan.md")


def test_assert_fields_finalize_ready_matches_the_path_based_predicate(tmp_path: Path) -> None:
    repo = tmp_path
    _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")
    fm = {"title": integrity.PLACEHOLDER_TITLE, "chain": "myplan", "commits": []}
    body = integrity.PROSE_PLACEHOLDER_MARKERS[0]

    with pytest.raises(integrity.HollowCompletionRecordError):
        integrity.assert_fields_finalize_ready(fm, body, repo, label="prospective/path.md")


# ---------------------------------------------------------------------------
# REASON_PLAN_UNRESOLVABLE vs REASON_PLAN_NOT_LANDED, and the archived-plan
# fallback lookup (docs/plans/<slug>.md moved out to archive/specs/**, etc.)
# ---------------------------------------------------------------------------


def test_plan_missing_everywhere_is_unresolvable_not_not_landed(tmp_path: Path) -> None:
    repo = tmp_path
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\nchain: "never-existed-slug"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    reasons = integrity.hollow_reasons(record, repo)
    assert reasons == [integrity.REASON_PLAN_UNRESOLVABLE]
    info = integrity.governing_plan_info(record, repo)
    assert info.resolved is False
    assert info.plan_path is None


@pytest.mark.parametrize("archive_dir", ["archive/specs/2026-08", "archive/plans/2026-08", "archive/completed/plans/2026-08"])
def test_a_plan_moved_to_an_archived_plan_dir_still_resolves(tmp_path: Path, archive_dir: str) -> None:
    """A landed plan is routinely moved OUT of docs/plans/ by a plan
    archiver -- `docs/plans/<slug>.md` absent must not read as
    REASON_PLAN_NOT_LANDED (nor REASON_PLAN_UNRESOLVABLE) when the plan is
    findable under one of the known archived-plan directories, filename
    match, with a landed status."""
    repo = tmp_path
    _write(repo / archive_dir / "myplan.md", "---\nstatus: implemented\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\nchain: "myplan"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    assert integrity.hollow_reasons(record, repo) == []
    info = integrity.governing_plan_info(record, repo)
    assert info.resolved is True
    assert info.plan_status == "implemented"


def test_an_archived_plan_still_not_landed_reports_not_landed(tmp_path: Path) -> None:
    """Found in the archive, but its own status isn't implemented/landed --
    REASON_PLAN_NOT_LANDED, not REASON_PLAN_UNRESOLVABLE (it WAS resolved)."""
    repo = tmp_path
    _write(repo / "archive" / "specs" / "2026-08" / "myplan.md", "---\nstatus: superseded\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\nchain: "myplan"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    reasons = integrity.hollow_reasons(record, repo)
    assert reasons == [integrity.REASON_PLAN_NOT_LANDED]
    info = integrity.governing_plan_info(record, repo)
    assert info.resolved is True
    assert info.plan_status == "superseded"


def test_live_docs_plans_wins_over_an_archived_duplicate(tmp_path: Path) -> None:
    """A live `docs/plans/<slug>.md` is checked FIRST -- an archived
    filename match is only a fallback, never preferred over a live file."""
    repo = tmp_path
    _write(repo / "docs" / "plans" / "myplan.md", "---\nstatus: implemented\n---\nbody\n")
    _write(repo / "archive" / "specs" / "2026-08" / "myplan.md", "---\nstatus: superseded\n---\nbody\n")
    record = repo / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\nchain: "myplan"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    assert integrity.hollow_reasons(record, repo) == []
