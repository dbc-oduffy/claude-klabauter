"""Tests for coordinator_core.ops.changelog_ops.compute_day_fields (Zone-A build #4).

Port of: workday-complete-step9-append-changelog.sh (coordinator-claude 6fb5fb37, 2026-07-22)
COMPUTE_ONLY sibling of changelog.append_day — see changelog_ops.py module
comment above `compute_day_fields` for the full negative-spec.

Covers:
  - commit-window collection (date-window path + commit_span path), self-commit
    exclusion, has_non_trivial classification.
  - plans_touched extraction.
  - handoffs enumeration.
  - decisions/blockers extraction — BOTH the primary (YAML-frontmatter +
    markdown-heading) path and the grep-fallback path, independently.
  - review-trail Reviewed: line parsing — BOTH the primary (json.load) path
    and the fallback (regex) path, independently.
  - HEADER staleness reporting (report-only, no skip decision).
  - the orchestrator (compute_day_fields) end-to-end.
  - the changelog.compute_day_fields IPC handler (param validation + dispatch).

Spec backlink: cross-repo ZONE-A BUILD #4 dispatch brief (changelog.compute_day_fields)
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.changelog_ops import (
    _collect_commits,
    _commit_range,
    _compute_day_fields_handler,
    _handoffs_for_date,
    _header_staleness,
    _plans_touched,
    _reviewed_lines_for_date,
    compute_day_fields,
    extract_field_from_handoffs,
    parse_review_record,
)

# Declared, not excused: this file spawns a real git process because
# `_collect_commits`/`_commit_range` under test read real commit-window
# history (date-window and commit-span paths, self-commit exclusion) that no
# mock stands in for. Tests each build their own commit sequence via
# `_commit`, so `_init_repo` is not hoisted to module scope -- per-test
# isolation across distinct commit-history scenarios. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    return repo


def _commit(repo: Path, msg: str, fname: str = "f.txt", content: str = "x") -> str:
    (repo / fname).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--date", "2026-07-15T10:00:00", "-m", msg],
        check=True,
        env=_env_for_commit(),
    )
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _env_for_commit() -> dict:
    import os

    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = "2026-07-15T10:00:00"
    env["GIT_COMMITTER_DATE"] = "2026-07-15T10:00:00"
    return env


# ---------------------------------------------------------------------------
# _collect_commits / _commit_range / has_non_trivial
# ---------------------------------------------------------------------------


def test_collect_commits_date_window_excludes_self_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feat: real work")
    _commit(repo, "chore(week-changelog): daily block 2026-07-15", fname="g.txt")

    commits = _collect_commits(repo, "2026-07-15")

    subjects = [s for _h, s in commits]
    assert "feat: real work" in subjects
    assert not any("chore(week-changelog): daily block" in s for s in subjects)


def test_collect_commits_commit_span_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    base = _commit(repo, "chore: base")
    tip = _commit(repo, "feat: spanned work", fname="h.txt")

    commits = _collect_commits(repo, "2026-07-15", commit_span=f"{base}..{tip}")

    hashes = [h for h, _s in commits]
    assert tip in hashes
    assert base not in hashes  # 2-dot range excludes the base itself


def test_commit_range_zero_and_one() -> None:
    assert _commit_range([]) == ("n/a", "n/a", "n/a")
    assert _commit_range(["abcdef1234567890"]) == ("abcdef12", "abcdef12", "abcdef12")


def test_commit_range_multiple() -> None:
    hashes = ["newestsha0000000", "oldestsha0000000"]  # newest-first, per git log order
    oldest, newest, rng = _commit_range(hashes)
    assert newest == "newestsh"
    assert oldest == "oldestsh"
    assert rng == "oldestsh..newestsh"


# ---------------------------------------------------------------------------
# plans_touched
# ---------------------------------------------------------------------------


def test_plans_touched_none_when_no_plan_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feat: no plans")
    assert _plans_touched(repo, "2026-07-15") == "none"


def test_plans_touched_lists_docs_plans_files(tmp_path: Path) -> None:
    """Status token is read from the plan's own frontmatter.

    Previously pinned the hardcoded `(status: in-progress)` literal — the very
    assertion that let the bash hardcode survive the Python port (example-retrieval-repo-em
    memo 2026-07-20). A plan with no frontmatter renders `unknown`, never a
    fabricated status.
    """
    repo = _init_repo(tmp_path)
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "2026-07-15-thing.md").write_text("# plan\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "feat: add plan"],
        check=True,
        env=_env_for_commit(),
    )
    result = _plans_touched(repo, "2026-07-15")
    assert result == "docs/plans/2026-07-15-thing.md (status: unknown)"


# ---------------------------------------------------------------------------
# handoffs
# ---------------------------------------------------------------------------


def test_handoffs_for_date_none_when_dir_absent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    handoffs_list, paths = _handoffs_for_date(repo, "2026-07-15")
    assert handoffs_list == "none"
    assert paths == []


def test_handoffs_for_date_matches_glob(tmp_path: Path) -> None:
    """Glob is `{date}-*.md` (dash), faithfully mirroring the oracle's
    `find ... -name "${TODAY}-*.md"` — reproduced verbatim, not "fixed" against
    the underscore-separated `{date}_HHMMSS_slug.md` convention seen elsewhere
    on disk (that mismatch is a pre-existing oracle quirk, out of scope here)."""
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f1 = hdir / "2026-07-15-100000-thing.md"
    f1.write_text("---\nDecisions: none\n---\nbody\n")
    (hdir / "2026-07-14-100000-other.md").write_text("not today\n")

    handoffs_list, paths = _handoffs_for_date(repo, "2026-07-15")

    assert handoffs_list == "state/handoffs/2026-07-15-100000-thing.md"
    assert paths == [f1]


# ---------------------------------------------------------------------------
# decisions/blockers — BOTH extraction paths
# ---------------------------------------------------------------------------


def test_extract_field_primary_frontmatter(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("---\nDecisions: use approach X\n---\nbody\n")

    result = extract_field_from_handoffs("Decisions", [f])

    assert result == "use approach X"


def test_extract_field_primary_markdown_heading(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("body\n\n## Blockers\n\n- waiting on review\n- flaky CI\n\n## Next\nmore\n")

    result = extract_field_from_handoffs("Blockers", [f])

    assert result == "waiting on review; flaky CI"


def test_extract_field_none_when_absent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("---\nstatus: active\n---\nbody\n")

    assert extract_field_from_handoffs("Decisions", [f]) == "none"
    assert extract_field_from_handoffs("Decisions", []) == "none"


def test_extract_field_fallback_frontmatter(tmp_path: Path) -> None:
    """force_fallback=True exercises the grep-style fallback path directly."""
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("---\nDecisions: use approach Y\n---\nbody\n")

    result = extract_field_from_handoffs("Decisions", [f], force_fallback=True)

    assert result == "use approach Y"


def test_extract_field_fallback_ignores_markdown_heading(tmp_path: Path) -> None:
    """Fallback path has narrower coverage than primary — no markdown-heading scan
    (mirrors the oracle's grep -E fallback, F6 generic-strip semantics)."""
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("body\n\n## Blockers\n\n- waiting on review\n")

    result = extract_field_from_handoffs("Blockers", [f], force_fallback=True)

    assert result == "none"


def test_extract_field_falls_back_when_primary_raises(tmp_path: Path, monkeypatch) -> None:
    """Any primary-path exception degrades to the fallback (mirrors the oracle's
    'python3 available but result empty' -> grep chain)."""
    import coordinator_core.ops.changelog_ops as mod

    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("---\nDecisions: use approach Z\n---\nbody\n")

    def _boom(field, paths):
        raise ValueError("simulated parse failure")

    monkeypatch.setattr(mod, "_extract_field_primary", _boom)

    result = extract_field_from_handoffs("Decisions", [f])

    assert result == "use approach Z"


# ---------------------------------------------------------------------------
# Regression: canonical handoff headings never matched (break-class, 2026-08-06).
# `## Decisions` / `## Blockers` are not headings any handoff template writes —
# the canonical census on disk is `## Key Decisions Made` / `## Blockers or
# Issues`, so the bare-name-only markdown leg returned "none" in 100% of real
# handoffs. See `_HEADING_ALIASES`.
# ---------------------------------------------------------------------------


def test_extract_field_primary_matches_key_decisions_made_heading(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text(
        "body\n\n## Key Decisions Made\n\n- use approach X\n- pinned dependency Y\n\n## Next\nmore\n"
    )

    result = extract_field_from_handoffs("Decisions", [f])

    assert result == "use approach X; pinned dependency Y"


def test_extract_field_primary_matches_blockers_or_issues_heading(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("body\n\n## Blockers or Issues\n\n- waiting on review\n\n## Next\nmore\n")

    result = extract_field_from_handoffs("Blockers", [f])

    assert result == "waiting on review"


def test_extract_field_primary_bare_decisions_heading_still_matches(tmp_path: Path) -> None:
    """No regression: the bare `## Decisions` heading (already covered by
    test_extract_field_primary_markdown_heading for Blockers) must still match
    once aliases are introduced."""
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("body\n\n## Decisions\n\n- use approach X\n\n## Next\nmore\n")

    result = extract_field_from_handoffs("Decisions", [f])

    assert result == "use approach X"


def test_extract_field_primary_caps_long_section(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    bullets = "\n".join(f"- decision number {i} with some extra prose padding" for i in range(40))
    f.write_text(f"body\n\n## Key Decisions Made\n\n{bullets}\n\n## Next\nmore\n")

    result = extract_field_from_handoffs("Decisions", [f])

    assert len(result) <= 400
    assert result.endswith("…")


def test_extract_field_no_double_collection_for_single_heading(tmp_path: Path) -> None:
    """A body containing `## Key Decisions Made` exactly once must not be
    collected twice via both the "Key Decisions Made" and "Decisions"
    aliases matching the same occurrence."""
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("body\n\n## Key Decisions Made\n\n- use approach X\n\n## Next\nmore\n")

    result = extract_field_from_handoffs("Decisions", [f])

    assert result == "use approach X"
    assert result.count("use approach X") == 1


def test_extract_field_neither_heading_nor_frontmatter_is_none(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    f = hdir / "h.md"
    f.write_text("---\nstatus: active\n---\nbody with no relevant heading\n")

    assert extract_field_from_handoffs("Decisions", [f]) == "none"
    assert extract_field_from_handoffs("Blockers", [f]) == "none"


# ---------------------------------------------------------------------------
# Regression: present-but-empty `field:` must not harvest the NEXT line
# (break-class, 2026-07-28). `_FM_FIELD_RE_TMPL` padded the value with `\s*`,
# and `\s` matches a newline, so the fallback's whole-file MULTILINE search
# walked past the line break of a bare `Decisions:` and `(.+)` took the
# following `Blockers:` line into the changelog. Parametrized over BOTH line
# endings: an LF-only test passes against the unfixed code for the CRLF half
# and would prove nothing about Windows-authored handoffs.
# ---------------------------------------------------------------------------

_EOLS = pytest.mark.parametrize("eol", ["\n", "\r\n"], ids=["lf", "crlf"])


def _write_handoff(tmp_path: Path, lines: list[str], eol: str) -> Path:
    """Write a handoff with an explicit line ending, bypassing Path.write_text's
    universal-newline translation (newline="" keeps `\\r\\n` intact on every
    platform, and stops `\\n` being rewritten to `\\r\\n` on Windows)."""
    repo = _init_repo(tmp_path)
    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True, exist_ok=True)
    f = hdir / "h.md"
    with open(f, "w", encoding="utf-8", newline="") as fh:
        fh.write(eol.join(lines))
    return f


@_EOLS
def test_extract_field_fallback_empty_key_does_not_harvest_next_line(
    tmp_path: Path, eol: str
) -> None:
    f = _write_handoff(
        tmp_path,
        ["---", "Decisions:", "Blockers: NEIGHBOUR-VALUE", "---", "body", ""],
        eol,
    )

    result = extract_field_from_handoffs("Decisions", [f], force_fallback=True)

    assert result == "none"
    assert "NEIGHBOUR-VALUE" not in result


@_EOLS
def test_extract_field_primary_empty_key_does_not_harvest_next_line(
    tmp_path: Path, eol: str
) -> None:
    """The primary path splits the frontmatter block into lines before matching,
    so it was never exploitable — pinned anyway, because both paths now share
    `_FM_FIELD_RE_TMPL` and a future edit that stops splitting would silently
    inherit the fallback's blast radius."""
    f = _write_handoff(
        tmp_path,
        ["---", "Decisions:", "Blockers: NEIGHBOUR-VALUE", "---", "body", ""],
        eol,
    )

    assert extract_field_from_handoffs("Decisions", [f]) == "none"


@_EOLS
def test_extract_field_fallback_skips_empty_key_for_a_later_real_value(
    tmp_path: Path, eol: str
) -> None:
    """`(.+)` declines to match the empty occurrence, so the whole-file search
    continues to the next `Decisions:` line rather than stopping at the first."""
    f = _write_handoff(
        tmp_path,
        ["---", "Decisions:", "---", "body", "Decisions: the real one", ""],
        eol,
    )

    result = extract_field_from_handoffs("Decisions", [f], force_fallback=True)

    assert result == "the real one"


@_EOLS
def test_extract_field_fallback_reads_populated_key_unchanged(
    tmp_path: Path, eol: str
) -> None:
    """Positive control for the CRLF half: the value must come back without the
    line's trailing carriage return glued onto it."""
    f = _write_handoff(
        tmp_path,
        ["---", "Decisions: use approach W", "Blockers: none", "---", "body", ""],
        eol,
    )

    result = extract_field_from_handoffs("Decisions", [f], force_fallback=True)

    assert result == "use approach W"


# ---------------------------------------------------------------------------
# review-trail Reviewed: lines — BOTH extraction paths
# ---------------------------------------------------------------------------


def test_parse_review_record_primary(tmp_path: Path) -> None:
    f = tmp_path / "rec.json"
    f.write_text(json.dumps({"sha_range": "abc..def", "reviewer": "code-reviewer", "verdict": "OK", "diff_loc": 42}))

    line = parse_review_record(f)

    assert line == "sha_range=abc..def reviewer=code-reviewer verdict=OK diff_loc=42"


def test_parse_review_record_fallback(tmp_path: Path) -> None:
    """The fallback regex only matches QUOTED string values (mirrors the oracle's
    `grep -oE '"diff_loc"[[:space:]]*:[[:space:]]*"[^"]*"'`, which is itself
    quotes-only) — an unquoted JSON number degrades to "unknown" on this path,
    see test_parse_review_record_fallback_missing_fields for that shape."""
    f = tmp_path / "rec.json"
    f.write_text(
        json.dumps({"sha_range": "abc..def", "reviewer": "code-reviewer", "verdict": "OK", "diff_loc": "42"})
    )

    line = parse_review_record(f, force_fallback=True)

    assert line == "sha_range=abc..def reviewer=code-reviewer verdict=OK diff_loc=42"


def test_parse_review_record_fallback_missing_fields(tmp_path: Path) -> None:
    f = tmp_path / "rec.json"
    f.write_text('{"reviewer": "code-reviewer"}')

    line = parse_review_record(f, force_fallback=True)

    assert line == "sha_range=unknown reviewer=code-reviewer verdict=unknown diff_loc=unknown"


def test_parse_review_record_primary_falls_back_on_malformed_json(tmp_path: Path) -> None:
    f = tmp_path / "rec.json"
    f.write_text("not valid json {{{")

    line = parse_review_record(f)

    assert "sha_range=unknown" in line


def test_reviewed_lines_for_date_filters_by_prefix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    rdir = repo / "state" / "review-trail"
    rdir.mkdir(parents=True)
    (rdir / "2026-07-15-100000-x.json").write_text(
        json.dumps({"sha_range": "a..b", "reviewer": "r1", "verdict": "OK", "diff_loc": 5})
    )
    (rdir / "2026-07-14-100000-x.json").write_text(
        json.dumps({"sha_range": "c..d", "reviewer": "r2", "verdict": "OK", "diff_loc": 5})
    )

    lines = _reviewed_lines_for_date(repo, "2026-07-15")

    assert len(lines) == 1
    assert "reviewer=r1" in lines[0]


# ---------------------------------------------------------------------------
# HEADER staleness (report-only)
# ---------------------------------------------------------------------------


def test_header_staleness_absent_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = _header_staleness(repo, "2026-07-15")
    assert result == {"stale": False, "days_ago": None, "week_start": None}


def test_header_staleness_fresh(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wc = repo / "state" / "week-changelog"
    wc.mkdir(parents=True)
    (wc / "HEADER.md").write_text("Week starting: 2026-07-13\n")

    result = _header_staleness(repo, "2026-07-15")

    assert result == {"stale": False, "days_ago": 2, "week_start": "2026-07-13"}


def test_header_staleness_stale(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    wc = repo / "state" / "week-changelog"
    wc.mkdir(parents=True)
    (wc / "HEADER.md").write_text("Week starting: 2026-06-01\n")

    result = _header_staleness(repo, "2026-07-15")

    assert result["stale"] is True
    assert result["days_ago"] == 44


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_compute_day_fields_end_to_end(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feat: do the thing")

    hdir = repo / "state" / "handoffs"
    hdir.mkdir(parents=True)
    (hdir / "2026-07-15-100000-thing.md").write_text(
        "---\nDecisions: ship it\nBlockers: none\n---\nbody\n"
    )

    result = compute_day_fields(worktree=repo, date="2026-07-15")

    assert result["commit_count"] == 1
    assert result["has_non_trivial"] is True
    assert result["decisions"] == "ship it"
    assert result["blockers"] == "none"
    assert result["handoffs_list"] == "state/handoffs/2026-07-15-100000-thing.md"
    assert result["is_backfill"] is False
    assert result["local_today"] == "2026-07-15"
    assert result["header_stale"] is False


def test_compute_day_fields_backfill_marker(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    result = compute_day_fields(worktree=repo, date="2026-07-10", local_today="2026-07-15")
    assert result["is_backfill"] is True
    assert result["local_today"] == "2026-07-15"


def test_compute_day_fields_all_trivial_has_non_trivial_false(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "chore: bump deps")
    result = compute_day_fields(worktree=repo, date="2026-07-15")
    assert result["has_non_trivial"] is False


# ---------------------------------------------------------------------------
# IPC handler
# ---------------------------------------------------------------------------


def test_handler_requires_repo_root() -> None:
    result = asyncio.run(_compute_day_fields_handler({}, repo_root=None))
    assert "error" in result


def test_handler_rejects_invalid_date(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    result = asyncio.run(
        _compute_day_fields_handler({"date": "not-a-date"}, repo_root=common_dir)
    )
    assert "error" in result


def test_handler_rejects_three_dot_commit_span(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    result = asyncio.run(
        _compute_day_fields_handler(
            {"date": "2026-07-15", "commit_span": "abc...def"}, repo_root=common_dir
        )
    )
    assert "error" in result


def test_handler_rejects_invalid_local_today(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    common_dir = repo / ".git"
    result = asyncio.run(
        _compute_day_fields_handler(
            {"date": "2026-07-15", "local_today": "nope"}, repo_root=common_dir
        )
    )
    assert "error" in result


def test_handler_returns_field_bundle(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit(repo, "feat: work")
    common_dir = repo / ".git"

    result = asyncio.run(
        _compute_day_fields_handler({"date": "2026-07-15"}, repo_root=common_dir)
    )

    assert result["commit_count"] == 1
    assert result["date"] == "2026-07-15"
    assert "error" not in result
