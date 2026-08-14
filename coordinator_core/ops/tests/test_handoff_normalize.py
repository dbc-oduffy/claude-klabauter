"""
coordinator_core.ops.tests.test_handoff_normalize -- restored 2026-08-10 (C1b).

The prior 43-test module was deleted by commit `1d4e686a9` ("test cull:
delete the spawn-heavy Windows-poison test set from orbit") -- see
`state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md`. That commit's
own PM ruling ("delete now, commit the delete, plan the restoration
separately; the coverage gap is pre-authorized") explicitly anticipates this
restoration.

Scope of THIS restoration (C1b of
docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md): ONLY the
C1a blast-radius non-regression coverage for
`_resolve_claimed_plan_deliverable_id` -- `ops/handoff_normalize.py`'s sole
caller of `resolve_claimed_plan_path` -- covering single-claim byte-identical
resolution and multi-claim deterministic earliest-`claimed_at` tie-break
(tier (b) only; tier (a)'s precedence is unchanged by C1a). This is NOT a
restoration of the deleted module's full 43-test surface -- that remains a
separately-scoped gap.

WHAT MUST NOT COME BACK: the four per-test real-`git init` conftest fixtures
the deleted module consumed (`norm_repo` among them) -- see the ledger's
"What was cut" section. `resolve_claimed_plan_path` (and the
`list_held_plan_claims` it delegates to for its tier-(b) fallback) reads
DIRECTORIES on disk, never git objects, so this file monkeypatches
`coordinator_core.session.core.sessions_dir` to point directly at a
`tmp_path`-rooted `coordinator-sessions` dir instead -- no `git init`
anywhere in this module.

Spec backlink: pln-a-commit-trailer-that-names-th-ce8a2e § C1b

C5 ADDITION (2026-08-12, person-identity-primitive-first-slice § C5, re-done):
the `minted_by` stamping cases below drive `_normalize_one_text` directly
against synthetic content strings (no disk I/O, no `git init`), passing
`minted_by=` as a caller-supplied parameter -- NEVER by monkeypatching a
`resolve_operating_person` import inside `handoff_normalize` itself, which no
longer imports that symbol at all (break-class fix: the batch sweep must not
resolve the operating human internally, or every session that runs a sweep
stamps its own identity onto the whole corpus -- see
`_normalize_one_text`'s docstring and step-7 comment). The end-to-end,
creation-door-level case monkeypatches `resolve_operating_person` at its
import site in `coordinator_core.ops.handoff_author_fork` instead, per the
authoring plan's § The test-design constraint that makes this harder than it
looks (this is a solo-user box; a resolver that ignores its inputs and
returns a constant passes every real-world check).

A separate regression test, `test_batch_sweep_never_stamps_minted_by_on_unrelated_handoffs`
below, drives the `handoff.normalize` batch-sweep handler directly (mirroring
`test_handoff_normalize_carry_scope.py`'s real-`git init` fixture pattern) to
assert the two-door hazard itself cannot recur.

Spec backlink (C5): pln-person-identity-primitive-firs-dbc797 § C5
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import handoff_normalize
from coordinator_core.ops.fleet._common import plan_claim_dir
from coordinator_core.ops.handoff_normalize import (
    _handler,
    _normalize_one_text,
    _resolve_claimed_plan_deliverable_id,
)
from coordinator_core.session import core

# Real-git spawn is load-bearing: the batch-sweep regression test drives
# `handoff.normalize` against a real `git init` fixture (mirroring
# test_handoff_normalize_carry_scope.py's pattern) to prove the two-door
# minted_by hazard cannot recur against real repo state, not a mock. Per-test
# isolation via tmp_path fixtures, not hoisted. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _write_plan(worktree_root: Path, slug: str, deliverable_id: str) -> Path:
    plan_path = worktree_root / "docs" / "plans" / f"{slug}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\ndeliverable_id: {deliverable_id}\n---\n\n# Plan\n\nBody.\n",
        encoding="utf-8",
    )
    return plan_path


def _seed_plan_claim(
    worktree_root: Path, session_id: str, plan_slug: str, claimed_at: str
) -> None:
    common_dir = worktree_root / ".git"
    claim_dir = plan_claim_dir(common_dir, Path(f"{plan_slug}.md"))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")


def _monkeypatch_sessions_dir(monkeypatch, worktree_root: Path) -> None:
    """Point `core.sessions_dir` at `<worktree_root>/.git/coordinator-sessions`
    directly -- no `git rev-parse`, no `git init`. `resolve_claimed_plan_path`
    (and `list_held_plan_claims`, which it delegates to for tier (b)) reads
    this directory tree only; it never touches git objects, so a real repo
    is unnecessary here."""
    monkeypatch.setattr(
        core,
        "sessions_dir",
        lambda cwd=None: str(worktree_root / ".git" / "coordinator-sessions"),
    )


def test_single_claim_resolution_is_byte_identical_before_and_after_c1a(
    tmp_path, monkeypatch
):
    """N<=1 held claims: C1a's tier-(b) tie-break change is a no-op here --
    the deliverable_id resolved via `resolve_claimed_plan_path` is unchanged
    from pre-C1a behavior."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-hn-single")
    _monkeypatch_sessions_dir(monkeypatch, tmp_path)
    plan_slug = "2026-08-10-hn-single-claim"
    _write_plan(tmp_path, plan_slug, "dlv-hn-single-abc123")
    _seed_plan_claim(tmp_path, "sid-hn-single", plan_slug, "2026-08-10T09:00:00Z")

    resolved = _resolve_claimed_plan_deliverable_id(tmp_path)

    assert resolved == "dlv-hn-single-abc123"


def test_multi_claim_tier_b_only_picks_earliest_claimed_at_not_alphabetical(
    tmp_path, monkeypatch
):
    """N>1 held claims, tier (a) untouched (no `session-shape.json` write at
    all in this test) so this is exercised via tier (b) ONLY: C1a's
    tie-break is deterministic earliest-`claimed_at`, not
    alphabetical-by-slug. Slugs are seeded so alphabetical order DISAGREES
    with claim order -- a regression to the pre-C1a alphabetical tie-break
    would carry the wrong plan's deliverable_id."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-hn-multi")
    _monkeypatch_sessions_dir(monkeypatch, tmp_path)
    earlier_claimed_but_later_alpha = "2026-08-10-zzz-claimed-first"
    later_claimed_but_earlier_alpha = "2026-08-10-aaa-claimed-second"
    _write_plan(tmp_path, earlier_claimed_but_later_alpha, "dlv-hn-first-claimed")
    _write_plan(tmp_path, later_claimed_but_earlier_alpha, "dlv-hn-second-claimed")
    _seed_plan_claim(
        tmp_path,
        "sid-hn-multi",
        earlier_claimed_but_later_alpha,
        "2026-08-10T09:00:00Z",
    )
    _seed_plan_claim(
        tmp_path,
        "sid-hn-multi",
        later_claimed_but_earlier_alpha,
        "2026-08-10T10:00:00Z",
    )

    resolved = _resolve_claimed_plan_deliverable_id(tmp_path)

    assert resolved == "dlv-hn-first-claimed"


# ---------------------------------------------------------------------------
# C5 -- minted_by stamping (person-identity-primitive-first-slice)
# ---------------------------------------------------------------------------

_MINIMAL_HANDOFF = """\
---
title: Some handoff
created: 2026-08-12
pickup_ready: true
category: infra
summary: A summary already present.
deliverable_id: dlv-existing-123abc
initiative: null
owner: dbc-em-session-xyz
author: dbc-em-session-xyz
---

# Some handoff

Body.
"""


def test_minted_by_stamped_on_creation():
    """A created handoff (no minted_by field yet) carries minted_by after
    normalization, sourced from the CALLER-supplied `minted_by` param --
    `_normalize_one_text` never resolves the operating human itself; see the
    creation-door end-to-end case in `test_handoff_author_fork.py`."""
    result = _normalize_one_text(
        _MINIMAL_HANDOFF, Path("state/handoffs/x.md"), minted_by="dbc-example-operator"
    )

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    assert handoff_normalize.read_fm_field(fm_text, "minted_by") == "dbc-example-operator"
    assert any("minted_by" in c for c in result["changes"])


def test_minted_by_lands_casefolded_for_mixed_case_github_alias():
    """AC9: a mixed-case resolved github alias lands casefolded in minted_by --
    the caller (the resolver's 'github' key) already casefolds, this asserts
    the seam does not re-uppercase or otherwise disturb that casefolding on
    the way into frontmatter."""
    result = _normalize_one_text(
        _MINIMAL_HANDOFF,
        Path("state/handoffs/x.md"),
        minted_by="dbc-example-operator".casefold(),
    )

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    minted_by = handoff_normalize.read_fm_field(fm_text, "minted_by")
    assert minted_by == minted_by.casefold()
    assert minted_by == "dbc-example-operator"


def test_minted_by_omitted_entirely_when_unresolvable():
    """Unresolvable identity (no minted_by supplied by the caller) omits
    minted_by ENTIRELY -- not null, not "unknown" (DEC-41, extended)."""
    result = _normalize_one_text(
        _MINIMAL_HANDOFF, Path("state/handoffs/x.md"), minted_by=None
    )

    # _MINIMAL_HANDOFF has no other drift (all six prior fields already
    # clean), so with minted_by unresolvable there is no drift at all.
    assert result is None


def test_minted_by_omitted_when_unresolvable_alongside_other_drift():
    """Same unresolvable case, but forced through the changed-file path via
    an absent `category` field, to assert the key-absent contract even when
    the file is NOT already-clean end-to-end."""
    content = _MINIMAL_HANDOFF.replace("category: infra\n", "")

    result = _normalize_one_text(content, Path("state/handoffs/x.md"), minted_by=None)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    assert handoff_normalize.read_fm_field(fm_text, "minted_by") is None
    assert not any("minted_by" in c for c in result["changes"])


def test_owner_and_author_untouched():
    """owner/author are untouched in meaning and population (AC7) -- both
    survive normalization byte-identical."""
    result = _normalize_one_text(
        _MINIMAL_HANDOFF, Path("state/handoffs/x.md"), minted_by="dbc-example-operator"
    )

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    assert handoff_normalize.read_fm_field(fm_text, "owner") == "dbc-em-session-xyz"
    assert handoff_normalize.read_fm_field(fm_text, "author") == "dbc-em-session-xyz"
    assert not any("owner" in c for c in result["changes"])
    assert not any("author" in c for c in result["changes"])


def test_existing_normalize_behaviour_unchanged_when_already_clean():
    """Idempotency: a fully-clean file (including minted_by already present)
    still returns None -- the C5 addition does not disturb the existing
    already-clean contract."""
    content = _MINIMAL_HANDOFF.replace(
        "author: dbc-em-session-xyz\n",
        "author: dbc-em-session-xyz\nminted_by: dbc-example-operator\n",
    )

    result = _normalize_one_text(content, Path("state/handoffs/x.md"), minted_by="dbc-example-operator")

    assert result is None


def test_existing_minted_by_carried_unchanged_not_reminted():
    """An already-present minted_by is carried, never re-stamped, even when
    the caller would now supply a different identity (session-independent
    carry, mirrors deliverable_id's D1 carry discipline)."""
    content = _MINIMAL_HANDOFF.replace("category: infra\n", "").replace(
        "author: dbc-em-session-xyz\n",
        "author: dbc-em-session-xyz\nminted_by: dbc-original-minter\n",
    )

    result = _normalize_one_text(content, Path("state/handoffs/x.md"), minted_by="someone-else")

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    assert handoff_normalize.read_fm_field(fm_text, "minted_by") == "dbc-original-minter"
    assert not any("minted_by" in c for c in result["changes"])


# ---------------------------------------------------------------------------
# C5 -- batch-sweep regression: the defect that shipped in the first C5 pass
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **_GIT_ENV},
        timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _write_unrelated_handoff(worktree_root: Path, slug: str) -> Path:
    """A handoff with no `minted_by` and no `claimed_by` tying it to any
    session -- unrelated to whatever session runs the sweep."""
    path = worktree_root / "state" / "handoffs" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {slug}\ncreated: 2026-08-12\n---\n\n# {slug}\n\nBody.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.spawns_process
def test_batch_sweep_never_stamps_minted_by_on_unrelated_handoffs(tmp_path, monkeypatch):
    """Break-class regression (the defect the first C5 attempt shipped):
    running the `handoff.normalize` batch sweep (write=True) over a corpus of
    handoffs that lack `minted_by` and are NOT this session's own creation
    must stamp `minted_by` on NONE of them. The prior (defective) design
    resolved the operating human INSIDE `_normalize_one_text`/`_normalize_one`,
    which the sweep calls unconditionally for every file in
    `state/handoffs/*.md` -- meaning any session that ran a sweep would stamp
    its own identity onto the entire corpus, since `minted_by` is a brand-new
    field absent from every existing handoff.

    This test is the DIRECT catch for that: swap the fix (`minted_by=None`
    threaded through the sweep's dry-run and write-path call sites) for the
    old-shape defect and this test FAILS -- see the executor's dispatch
    report for the swap-and-confirm this test performed before landing.
    """
    # NOTE for future editors: this test tree's autouse HOME-quarantine fixture
    # (conftest.py) makes resolve_operating_person() unresolvable by default
    # in-suite. If a future defective change reintroduces an in-normalizer
    # resolve call, this test as written will NOT catch it unless
    # `handoff_normalize.resolve_operating_person` is also monkeypatched to a
    # resolvable identity here -- verified by hand (see the executor's
    # dispatch report for this chunk): with the in-normalizer resolve
    # temporarily restored AND `resolve_operating_person` monkeypatched to
    # return {"github": "dbc-example-operator"}, this test fails exactly as intended.
    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: str(tmp_path / "repo" / ".git" / "coordinator-sessions"))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-sweep-never-stamps")
    # Review: coordinator:code-reviewer c71df2b9 (P1) -- this module does not
    # import `resolve_operating_person` today (that is the fix), so
    # `raising=False` creates the attribute for the duration of this test
    # only. This closes the vacuous-test gap: the autouse HOME-quarantine
    # fixture in conftest.py already makes a REAL resolve_operating_person()
    # unresolvable, so without this monkeypatch the test would pass whether
    # or not a defective in-normalizer resolve call were reintroduced. By
    # forcing a resolvable identity here, the test can only pass if the
    # sweep path genuinely never calls the resolver at all.
    monkeypatch.setattr(
        handoff_normalize,
        "resolve_operating_person",
        lambda: {"github": "dbc-example-operator"},
        raising=False,
    )
    repo = tmp_path / "repo"
    _init_repo(repo)
    first = _write_unrelated_handoff(repo, "2026-08-12-unrelated-one")
    second = _write_unrelated_handoff(repo, "2026-08-12-unrelated-two")

    result = asyncio.run(_handler({"write": True}, repo_root=repo / ".git"))

    assert result["errors"] == []
    for path in (first, second):
        content = path.read_text(encoding="utf-8")
        assert handoff_normalize.read_fm_field(content, "minted_by") is None, (
            f"{path} was stamped with minted_by by the batch sweep -- "
            f"the two-door hazard has regressed:\n{content}"
        )


# ---------------------------------------------------------------------------
# C1 -- placeholder-shaped summary treated as absent
# (docs/plans/2026-08-14-placeholder-summaries-and-the-drift-guards-uncounted-
# callers.md § C1, AC1/AC2/AC3)
# ---------------------------------------------------------------------------

_SPINOFF_PLACEHOLDER_SUMMARY = (
    "PLACEHOLDER — replace with one-line spinoff summary (≤140 chars)"
)

# Exact shape `coordinator-doc-new.py::_scaffold_spinoff` emits: a quoted
# `title:`, the literal placeholder `summary:`, and NO H1 in the body (the
# scaffold's canonical section grammar starts at `## What this covers`) --
# the fallback-to-title branch is load-bearing for this shape, not the
# H1-derivation branch.
_SPINOFF_NO_H1 = f"""\
---
title: "test placeholder repro"
created: 2026-08-14
branch: "work/machine-b/2026-08-12"
status: open
predecessor: none
kind: spinoff
deployment_state: ready_to_fire
category: infra
summary: "{_SPINOFF_PLACEHOLDER_SUMMARY}"
pickup_ready: true
authoring_session: PLACEHOLDER
workstream: PLACEHOLDER
deliverable_id: dlv-test-placeholder-repro-abc123
initiative: null
---

## What this covers

<!-- One paragraph. -->
"""


def test_placeholder_summary_survives_untouched_before_the_fix_is_the_red():
    """AC1 (reproduction, pinned): a scaffolder-shaped placeholder `summary:`
    is PRESENT and under the 140-char cap, so absent the fix it is carried
    byte-identical -- the defect this chunk closes. This test asserts the
    FIXED behaviour (drift is now detected); its docstring records the red
    this chunk reproduced live before the fix landed: `_normalize_one_text`
    returned `None` (already "clean") and `summary:` stayed the literal
    placeholder string. See the executor's dispatch report for the verbatim
    repro transcript.
    """
    result = _normalize_one_text(_SPINOFF_NO_H1, Path("state/handoffs/x.md"))

    assert result is not None
    assert any("summary" in c and "placeholder" in c for c in result["changes"])


def test_placeholder_summary_falls_back_to_unquoted_title_when_no_h1():
    """AC2: a placeholder-shaped `summary:` is treated as absent, and (this
    scaffold shape having no H1) the backfill falls through to the `title:`
    field -- which must land UNQUOTED (latent-bug fix: `read_fm_field`
    returns the raw quoted text verbatim; a naive backfill would have
    embedded the literal quote characters into the new summary)."""
    result = _normalize_one_text(_SPINOFF_NO_H1, Path("state/handoffs/x.md"))

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    assert (
        handoff_normalize.read_fm_field(fm_text, "summary")
        == "test placeholder repro"
    )


def test_placeholder_summary_prefers_h1_over_title_when_both_present():
    """AC2, sibling shape: when the body DOES carry an H1 (a hand-started
    spinoff, or any other handoff kind that happens to carry this literal),
    the existing H1-derivation branch still wins over the title fallback --
    the placeholder classification only widens what counts as "absent", it
    does not reorder the existing derivation preference."""
    content = _SPINOFF_NO_H1.replace(
        "## What this covers", "# From the H1 instead\n\n## What this covers"
    )

    result = _normalize_one_text(content, Path("state/handoffs/x.md"))

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    assert (
        handoff_normalize.read_fm_field(fm_text, "summary")
        == "From the H1 instead"
    )


def test_present_nonplaceholder_summary_still_untouched():
    """Negative-spec guard: a present, non-placeholder, under-cap summary is
    NOT reclassified as absent -- the literal-match discipline (Anti-scope:
    no "looks unfilled" heuristic) leaves every other present summary,
    including a short or terse one, byte-identical."""
    result = _normalize_one_text(_MINIMAL_HANDOFF, Path("state/handoffs/x.md"))

    # _MINIMAL_HANDOFF's summary ("A summary already present.") is not the
    # placeholder literal, so with no other drift this is fully clean.
    assert result is None


def test_batch_sweep_backfills_placeholder_summary_on_a_committed_record(
    tmp_path, monkeypatch
):
    """AC3 (load-bearing): the batch-sweep caller (`handoff.normalize`,
    write=True) run over a corpus containing an ALREADY-COMMITTED record that
    carries the scaffolder's literal placeholder summary. This is the
    corpus-pass case AC3 exists to catch -- "it works on my freshly-
    scaffolded test file" is not sufficient evidence on its own.

    Finding (see dispatch report): no opt-out is wired for the batch caller.
    The placeholder classification is a literal string match (not a
    heuristic), and a corpus-committed record carrying this exact literal is
    exactly as broken as a freshly-scaffolded one -- the AC2 fix backfilling
    it via the pre-existing H1/title derivation on a batch pass is the
    correct, intended within-scope behaviour, not an accidental corpus-wide
    rewrite. A grep of `state/handoffs/*.md` for this literal (see dispatch
    report) found zero pre-existing matches in this repo's live corpus today
    -- this test is coverage for the shape, not evidence it currently fires.
    """
    monkeypatch.setattr(
        core, "sessions_dir", lambda cwd=None: str(tmp_path / "repo" / ".git" / "coordinator-sessions")
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-placeholder-sweep")
    repo = tmp_path / "repo"
    _init_repo(repo)
    placeholder_path = repo / "state" / "handoffs" / "2026-08-14-placeholder-spinoff.md"
    placeholder_path.parent.mkdir(parents=True, exist_ok=True)
    placeholder_path.write_text(_SPINOFF_NO_H1, encoding="utf-8")
    clean_path = _write_unrelated_handoff(repo, "2026-08-14-clean-unrelated")
    clean_before = clean_path.read_text(encoding="utf-8")

    result = asyncio.run(_handler({"write": True}, repo_root=repo / ".git"))

    assert result["errors"] == []
    placeholder_after = placeholder_path.read_text(encoding="utf-8")
    assert (
        handoff_normalize.read_fm_field(placeholder_after, "summary")
        == "test placeholder repro"
    )
    changed_files = {entry["file"] for entry in result["changed"]}
    assert any(str(placeholder_path.name) in f for f in changed_files)
    # The unrelated clean-shaped file's own drift (category/summary/etc.
    # backfill from ITS OWN H1) is expected -- what this asserts is that the
    # placeholder fix does not touch a file that carries no placeholder text.
    assert (
        handoff_normalize.read_fm_field(clean_before, "summary") is None
    )  # sanity: was absent, not placeholder, before the sweep
