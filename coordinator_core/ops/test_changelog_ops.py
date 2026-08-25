"""Characterization + parity tests for coordinator_core.ops.changelog_ops.main.

Port of: backfill-week-changelog-gaps.sh (DoE b5a4192c, 2026-07-20; retired
cc_invoke veneer).

Covers the CLI entrypoint (`main()`), not the underlying `backfill_gaps()` /
`_backfill_gaps_handler()` logic (op-level parity is the JSON-RPC handler's
own concern; this file's job is the CLI-shape contract: argv handling, repo
root resolution from $PWD, stdout JSON shape, exit codes).

Op-level per-day filename-collapse coverage (PM ruling 2026-07-19, AC7/AC8):
the `TestPerDayFilenameCollapse` class below covers `append_day` multi-machine
upsert onto ONE {date}.md and `_has_daily_file`'s collapsed-vs-legacy sacred-file
detection — narrower op-level scope than the byte-parity harness in
`tests/test_changelog_parity.py`, added here per dispatch brief.

Spec backlink: cross-repo/inbox/2026-07-06-strang-10-facade-adoption.md
DR authority (filename collapse): docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.changelog_ops import (
    _as_of_sha,
    _compose_block,
    _has_daily_file,
    _plan_status,
    _plans_touched,
    append_day,
    backfill_gaps,
    main,
    upsert_reviewed,
)

# Declared, not excused: this file spawns a real git process because `main()`
# resolves the repo root via real `git rev-parse`, and `_plans_touched` reads
# frontmatter at specific historical commits (point-in-time git log/show
# plumbing) that no mock stands in for. Several tests build commit spans
# (`test_plans_touched_status_is_point_in_time_not_compose_time`,
# `..._point_in_time_on_commit_span_path`) on top of a fresh `_init_repo`, so
# the fixture is not hoisted to module scope -- per-test commit history would
# collide across tests. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    return repo


def test_main_negative_not_a_git_repo(tmp_path: Path, monkeypatch, capsys) -> None:
    """No git repo at the working directory: exit 1, stderr diagnostic, no stdout JSON."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)

    rc = main([])

    assert rc == 1
    captured = capsys.readouterr()
    assert "cannot resolve git repo root" in captured.err
    assert captured.out == ""


def test_main_negative_no_header(tmp_path: Path, monkeypatch, capsys) -> None:
    """Git repo present but state/week-changelog/HEADER.md missing -> advisory 0, message key."""
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["backfilled"] == []
    assert result["skipped"] == []
    assert result.get("message") == "no HEADER.md"


def test_main_positive_backfills_gap(tmp_path: Path, monkeypatch, capsys) -> None:
    """HEADER.md present, week-start in the past, a commit in range -> backfill file written."""
    repo = _init_repo(tmp_path)
    week_changelog = repo / "state" / "week-changelog"
    week_changelog.mkdir(parents=True)

    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    (week_changelog / "HEADER.md").write_text(
        f"**Week starting:** {today}\n\n## Week summary\n\n(test)\n"
    )
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init commit"], check=True
    )

    monkeypatch.chdir(repo)
    # 2026-08-11 fix (cross-repo/inbox/2026-08-11-example-retrieval-repo-em-backfill-
    # changelog-cli-three-defects.md item 2): main() now resolves host via
    # compute_machine() — same machine slug the rest of the daily ceremony
    # uses — which honours COORDINATOR_MACHINE as its own first-priority
    # override. Prove main() DOES pick it up (the opposite of the retired
    # byte-parity contract this test used to assert).
    monkeypatch.setenv("COORDINATOR_MACHINE", "expected-slug")

    expected_host = "expected-slug"

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert len(result["backfilled"]) == 1
    out_path = Path(result["backfilled"][0])
    assert out_path.exists()
    content = out_path.read_text()
    assert f"## {today} — {expected_host} (synthesized backfill)" in content
    assert "init commit" in content


def test_main_ignores_repo_root_positional(tmp_path: Path, monkeypatch, capsys) -> None:
    """The optional [repo-root] positional is accepted but IGNORED (matches legacy contract)."""
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    rc = main(["/some/other/ignored/path"])

    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    # no HEADER.md in this repo -> advisory message, proving repo_root was
    # resolved from $PWD (the real repo), not the bogus positional argv[0]
    assert result.get("message") == "no HEADER.md"


def test_main_help_does_not_run_backfill(tmp_path: Path, monkeypatch, capsys) -> None:
    """Item 1 (cross-repo/inbox/2026-08-11-example-retrieval-repo-em-backfill-changelog-
    cli-three-defects.md): -h/--help must print usage and exit 0 WITHOUT
    running the backfill, even when a real gap is present."""
    repo = _init_repo(tmp_path)
    week_changelog = repo / "state" / "week-changelog"
    week_changelog.mkdir(parents=True)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    (week_changelog / "HEADER.md").write_text(f"**Week starting:** {today}\n")
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    monkeypatch.chdir(repo)

    rc = main(["--help"])

    assert rc == 0
    captured = capsys.readouterr()
    assert "Usage" in captured.out or "backfill-week-changelog-gaps" in captured.out
    assert list(week_changelog.glob("*backfill.md")) == []


def test_main_dry_run_reports_without_writing(tmp_path: Path, monkeypatch, capsys) -> None:
    """Item 1: --dry-run reports the same {backfilled, skipped} shape without
    writing anything to disk."""
    repo = _init_repo(tmp_path)
    week_changelog = repo / "state" / "week-changelog"
    week_changelog.mkdir(parents=True)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    (week_changelog / "HEADER.md").write_text(f"**Week starting:** {today}\n")
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    monkeypatch.chdir(repo)

    rc = main(["--dry-run"])

    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert len(result["backfilled"]) == 1
    would_be_path = Path(result["backfilled"][0])
    assert not would_be_path.exists()
    assert list(week_changelog.iterdir()) == [week_changelog / "HEADER.md"]
    assert "would write" in captured.err


def test_main_names_written_files_on_stderr(tmp_path: Path, monkeypatch, capsys) -> None:
    """Item 1: a real (non-dry-run) write must NAME the file it wrote on
    stderr -- silent writes were half of why --help running the backfill went
    unnoticed."""
    repo = _init_repo(tmp_path)
    week_changelog = repo / "state" / "week-changelog"
    week_changelog.mkdir(parents=True)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    (week_changelog / "HEADER.md").write_text(f"**Week starting:** {today}\n")
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    monkeypatch.chdir(repo)

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    out_path = result["backfilled"][0]
    assert "wrote" in captured.err
    assert out_path in captured.err


def test_main_host_resolves_via_compute_machine(tmp_path: Path, monkeypatch, capsys) -> None:
    """Item 2: main() resolves host via compute_machine() (the ceremony-wide
    lowercase machine slug), not the raw OS hostname -- and DOES honour
    COORDINATOR_MACHINE, reversing the retired byte-parity contract."""
    repo = _init_repo(tmp_path)
    week_changelog = repo / "state" / "week-changelog"
    week_changelog.mkdir(parents=True)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    (week_changelog / "HEADER.md").write_text(f"**Week starting:** {today}\n")
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    monkeypatch.chdir(repo)
    # compute_machine() lowercases via machine_slug() (coordinator_core.ops.emit._slug):
    # "MixedCaseSlug" -> "mixedcaseslug".
    monkeypatch.setenv("COORDINATOR_MACHINE", "MixedCaseSlug")

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    out_path = Path(result["backfilled"][0])
    assert "-mixedcaseslug-backfill.md" in out_path.name


class TestPerDayFilenameCollapse:
    """Op-level coverage for the per-day filename collapse (PM ruling 2026-07-19).

    append_day now writes state/week-changelog/{date}.md (was {date}-{machine}.md);
    _has_daily_file now covers {date}.md OR the legacy {date}-*.md glob.

    Spec backlink: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
    """

    _COMMON_KWARGS = dict(
        branch="main",
        commit_count=0,
        commit_range="n/a",
        scope="",
        plans_touched="none",
        handoffs_list="none",
        decisions="none",
        blockers="none",
        rc_validate="skipped",
        rc_plugin_suite="n/a",
        reviewed_lines=[],
        has_non_trivial=False,
    )

    def test_append_day_two_machines_share_one_file_own_sections(self, tmp_path: Path) -> None:
        """AC7: two machines appending the same date land in ONE {date}.md, each
        owning only their own '## {date} — {machine}' section; peers untouched."""
        date = "2026-02-01"

        result_a = append_day(
            worktree=tmp_path, date=date, machine="machine-a", **self._COMMON_KWARGS
        )
        result_b = append_day(
            worktree=tmp_path, date=date, machine="machine-b", **self._COMMON_KWARGS
        )

        assert result_a["out_path"] == result_b["out_path"], (
            "both machines must write the SAME collapsed {date}.md file"
        )
        out = Path(result_a["out_path"])
        assert out.name == "2026-02-01.md"

        content = out.read_text(encoding="utf-8")
        assert "## 2026-02-01 — machine-a" in content
        assert "## 2026-02-01 — machine-b" in content

        # Re-append machine-a with a changed scope; machine-b's section must survive
        # untouched (in-place section replace, not a whole-file rewrite).
        updated_kwargs = dict(self._COMMON_KWARGS, scope="Updated scope for A.")
        result_a2 = append_day(
            worktree=tmp_path, date=date, machine="machine-a", **updated_kwargs
        )
        assert result_a2["action"] == "replaced"

        content2 = out.read_text(encoding="utf-8")
        assert "**Scope:** Updated scope for A." in content2
        assert "## 2026-02-01 — machine-b" in content2, (
            "machine-b's section must remain untouched by machine-a's re-append"
        )

    def test_append_day_prefix_colliding_machine_names_do_not_clobber(
        self, tmp_path: Path
    ) -> None:
        """Review: code-reviewer (F1) — a lexically-prefix-colliding machine-name
        pair (both safe_id-valid) must NOT clobber each other's section. Prior to
        the fix, section_header lookup was a plain substring search: appending
        machine "host1" after "host10" already had a section would match
        "host10"'s header as a prefix of "host1"'s search string is reversed here
        deliberately — "host1" is written FIRST so "host10"'s header
        ("## {date} — host10") is a lexical extension of "host1"'s header
        ("## {date} — host1"), which is the actual unanchored-match failure mode
        (content.find("## {date} — host1") matches inside "## {date} — host10")."""
        date = "2026-02-01"

        result_1 = append_day(
            worktree=tmp_path, date=date, machine="host1", **self._COMMON_KWARGS
        )
        result_10 = append_day(
            worktree=tmp_path, date=date, machine="host10", **self._COMMON_KWARGS
        )

        assert result_1["out_path"] == result_10["out_path"]
        out = Path(result_1["out_path"])
        content = out.read_text(encoding="utf-8")
        assert "## 2026-02-01 — host1\n" in content
        assert "## 2026-02-01 — host10\n" in content

        # Re-append host1 with a changed scope; host10's section must survive
        # untouched — an unanchored search would find "host1" as a prefix inside
        # "host10"'s header and corrupt/replace the wrong section.
        updated_kwargs = dict(self._COMMON_KWARGS, scope="Updated scope for host1.")
        result_1b = append_day(
            worktree=tmp_path, date=date, machine="host1", **updated_kwargs
        )
        assert result_1b["action"] == "replaced"

        content2 = out.read_text(encoding="utf-8")
        assert "**Scope:** Updated scope for host1." in content2
        assert "## 2026-02-01 — host10" in content2, (
            "host10's section must remain untouched by host1's re-append"
        )
        # host1's own section must not have been duplicated/mis-replaced either.
        assert content2.count("## 2026-02-01 — host1\n") == 1

        # And the reverse direction: re-append host10, host1's section must survive.
        updated_kwargs_10 = dict(self._COMMON_KWARGS, scope="Updated scope for host10.")
        result_10b = append_day(
            worktree=tmp_path, date=date, machine="host10", **updated_kwargs_10
        )
        assert result_10b["action"] == "replaced"

        content3 = out.read_text(encoding="utf-8")
        assert "**Scope:** Updated scope for host10." in content3
        assert "**Scope:** Updated scope for host1." in content3, (
            "host1's previously-updated section must remain untouched by host10's re-append"
        )

    def test_has_daily_file_true_for_collapsed_date_file(self, tmp_path: Path) -> None:
        """AC8: _has_daily_file(date) is True when {date}.md exists (collapsed form)."""
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        (week_dir / "2026-02-01.md").write_text("stub\n", encoding="utf-8")

        assert _has_daily_file(
            "2026-02-01", today="2026-02-02", host="host-x", week_changelog_dir=week_dir
        ) is True

    def test_has_daily_file_true_for_legacy_machine_suffixed_file(self, tmp_path: Path) -> None:
        """AC8: _has_daily_file(date) is True for a day covered only by a legacy
        {date}-{machine}.md file (pre-collapse filename shape)."""
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        (week_dir / "2026-02-01-legacy-machine.md").write_text("stub\n", encoding="utf-8")

        assert _has_daily_file(
            "2026-02-01", today="2026-02-02", host="host-x", week_changelog_dir=week_dir
        ) is True

    def test_backfill_gaps_skips_day_with_collapsed_daily_file(self, tmp_path: Path) -> None:
        """AC8: backfill_gaps does NOT synthesize a spurious backfill file when
        {date}.md already exists — the existing per-day changelog is sacred."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        week_dir = repo / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        (week_dir / "HEADER.md").write_text(
            f"**Week starting:** {today}\n\n## Week summary\n\n(test)\n"
        )
        existing_content = f"## {today} — real-machine\n\nReal changelog entry already present.\n"
        existing_file = week_dir / f"{today}.md"
        existing_file.write_text(existing_content, encoding="utf-8")

        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init commit"], check=True)

        result = backfill_gaps(repo_root=repo / ".git", host="backfill-host", today_override=today)

        assert result["backfilled"] == [], f"expected no spurious backfill file, got: {result}"
        assert today in result["skipped"]
        assert existing_file.read_text(encoding="utf-8") == existing_content, (
            "pre-existing {date}.md must be untouched by backfill_gaps"
        )
        assert not (week_dir / f"{today}-backfill-host-backfill.md").exists()

    def test_backfill_gaps_still_overwrites_own_backfill_file_today(self, tmp_path: Path) -> None:
        """AC8: the collapsed-file sacred check does not block backfill_gaps'
        pre-existing overwrite-own-{today}-{host}-backfill.md behavior when no
        {date}.md exists — only the {date}.md path is newly sacred."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)

        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        week_dir = repo / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        (week_dir / "HEADER.md").write_text(
            f"**Week starting:** {today}\n\n## Week summary\n\n(test)\n"
        )
        own_backfill = week_dir / f"{today}-myhost-backfill.md"
        own_backfill.write_text("stale content\n", encoding="utf-8")

        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init commit"], check=True)

        result = backfill_gaps(repo_root=repo / ".git", host="myhost", today_override=today)

        assert result["backfilled"] == [str(own_backfill)], (
            f"expected own backfill file to be (re)written, got: {result}"
        )
        assert own_backfill.read_text(encoding="utf-8") != "stale content\n", (
            "own today-backfill file must be overwritten, not skipped as sacred"
        )


# ---------------------------------------------------------------------------
# **Backfilled:** provenance line (ASK 2,
# cross-repo/inbox/2026-07-20-claude-central-em-debash-windows-validation-gaps.md)
#
# `_compose_block`'s `is_backfill` param renders a **Backfilled:** line;
# previously the field was computed by `compute_day_fields` but never wired
# into rendering at all (self-documented gap).
# ---------------------------------------------------------------------------


class TestBackfilledLineRender:
    _COMMON_KWARGS = dict(
        date="2026-02-01",
        machine="machine-a",
        branch="main",
        commit_count=0,
        commit_range="n/a",
        scope="",
        plans_touched="none",
        handoffs_list="none",
        decisions="none",
        blockers="none",
        rc_validate="skipped",
        rc_plugin_suite="n/a",
        reviewed_lines=[],
        has_non_trivial=False,
    )

    def test_backfilled_line_renders_when_is_backfill_true(self) -> None:
        block = _compose_block(**self._COMMON_KWARGS, is_backfill=True)
        assert "**Backfilled:** yes" in block

    def test_backfilled_line_absent_when_is_backfill_false(self) -> None:
        block = _compose_block(**self._COMMON_KWARGS, is_backfill=False)
        assert "**Backfilled:**" not in block

    def test_backfilled_line_absent_by_default(self) -> None:
        """is_backfill defaults to False — omit-by-default, same as **Scope:**."""
        block = _compose_block(**self._COMMON_KWARGS)
        assert "**Backfilled:**" not in block

    def test_append_day_threads_is_backfill_through_to_render(self, tmp_path: Path) -> None:
        """The public append_day() entry point (not just _compose_block directly)
        must propagate is_backfill into the written file."""
        result = append_day(
            worktree=tmp_path,
            date="2026-02-01",
            machine="machine-a",
            branch="main",
            commit_count=0,
            commit_range="n/a",
            scope="",
            plans_touched="none",
            handoffs_list="none",
            decisions="none",
            blockers="none",
            rc_validate="skipped",
            rc_plugin_suite="n/a",
            reviewed_lines=[],
            has_non_trivial=False,
            is_backfill=True,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert "**Backfilled:** yes" in content


# ---------------------------------------------------------------------------
# _plans_touched — real frontmatter status, not a hardcoded literal
#
# Regression cover for the example-retrieval-repo-em memo 2026-07-20: the predecessor bash
# hardcoded "(status: in-progress)" and the literal survived the bash->Python
# port untouched, so every plan in every daily block read as in-progress.
# ---------------------------------------------------------------------------


def _commit_plan(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"plan: {rel}"], check=True)


def _plans_touched_all(repo: Path) -> str:
    """Run _plans_touched over the repo's whole history (commit_span form)."""
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    return _plans_touched(repo, "2026-07-20", commit_span=f"{first}..HEAD")


def test_plans_touched_reads_real_frontmatter_status(tmp_path: Path) -> None:
    """A plan reading `status: implemented` must NOT be rendered as in-progress."""
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    _commit_plan(
        repo,
        "docs/plans/2026-07-14-shipped-plan.md",
        "---\nstatus: implemented\n---\n\n# Shipped\n",
    )

    out = _plans_touched_all(repo)

    assert out == "docs/plans/2026-07-14-shipped-plan.md (status: implemented)", out
    assert "in-progress" not in out


def test_plans_touched_status_unknown_when_key_absent(tmp_path: Path) -> None:
    """Frontmatter with no `status:` key renders `unknown`, not a fabricated status."""
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    _commit_plan(repo, "docs/plans/2026-07-14-no-status.md", "---\ntitle: x\n---\n\n# Body\n")

    assert _plans_touched_all(repo) == "docs/plans/2026-07-14-no-status.md (status: unknown)"


def test_plans_touched_dangling_path_renders_removed(tmp_path: Path) -> None:
    """`git log --name-only` names deleted/renamed paths; they render `removed`."""
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    _commit_plan(repo, "docs/plans/2026-07-14-doomed.md", "---\nstatus: draft\n---\n\n# X\n")
    subprocess.run(
        ["git", "-C", str(repo), "rm", "-q", "--", "docs/plans/2026-07-14-doomed.md"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "drop plan"], check=True)

    assert _plans_touched_all(repo) == "docs/plans/2026-07-14-doomed.md (status: removed)"


def test_plans_touched_filters_checker_sidecars(tmp_path: Path) -> None:
    """Reviewer sidecars match docs/plans/*.md but are not plans — filtered out."""
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    _commit_plan(repo, "docs/plans/2026-07-14-real.md", "---\nstatus: draft\n---\n\n# X\n")
    for sidecar in (
        "docs/plans/2026-07-14-real.prior-art-check.md",
        "docs/plans/2026-07-14-real.plan-coverage-check.md",
        "docs/plans/2026-07-14-real.plan-coverage-check.2026-07-14T08-31-09Z.md",
        "docs/plans/2026-07-14-real.docs-check.md",
    ):
        _commit_plan(repo, sidecar, "sidecar\n")

    assert _plans_touched_all(repo) == "docs/plans/2026-07-14-real.md (status: draft)"


def test_plans_touched_filters_sidecars_with_no_check_segment(tmp_path: Path) -> None:
    """Regression: the predicate is structural, not a `-check` enumeration.

    The prior `\\.[a-z0-9-]*check(\\.|$)` regex rested on a comment asserting that
    every sidecar kind on disk carries a dotted `-check` segment. It never did:
    docs/plans/ holds ~46 persona-review and one-off sidecars that leaked into
    "Plans touched" and had a plan status asserted about them.
    """
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    _commit_plan(repo, "docs/plans/2026-07-14-real.md", "---\nstatus: draft\n---\n\n# X\n")
    for sidecar in (
        "docs/plans/2026-07-14-real.patrik-review.md",
        "docs/plans/2026-07-14-real.sonnet-review.md",
        "docs/plans/2026-07-14-real.eng-director-review.md",
        "docs/plans/2026-07-14-real.review.md",
        "docs/plans/2026-07-14-real.node-map.md",
        "docs/plans/2026-07-14-real.phase0.md",
        "docs/plans/2026-07-14-real.plan-review-check.md",
    ):
        _commit_plan(repo, sidecar, "sidecar\n")

    assert _plans_touched_all(repo) == "docs/plans/2026-07-14-real.md (status: draft)"


def test_plans_touched_keeps_plans_whose_slug_contains_check_or_review(tmp_path: Path) -> None:
    """The structural predicate must not over-fire on domain vocabulary.

    This repo's plan slugs are full of "check"/"review" nouns; a plan filename
    has no internal dot, which is exactly what keeps it out of the sidecar set.
    """
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    for plan in (
        "docs/plans/2026-07-14-coverage-gate-check.md",
        "docs/plans/2026-07-14-plan-review-ladder.md",
    ):
        _commit_plan(repo, plan, "---\nstatus: draft\n---\n\n# X\n")

    touched = _plans_touched_all(repo)
    assert "2026-07-14-coverage-gate-check.md (status: draft)" in touched
    assert "2026-07-14-plan-review-ladder.md (status: draft)" in touched


def test_plans_touched_none_when_only_sidecars(tmp_path: Path) -> None:
    """Filtering every entry collapses to the `none` sentinel, not an empty string."""
    repo = _init_repo(tmp_path)
    _commit_plan(repo, "README.md", "root\n")
    _commit_plan(repo, "docs/plans/2026-07-14-x.prior-art-check.md", "sidecar\n")

    assert _plans_touched_all(repo) == "none"


# ---------------------------------------------------------------------------
# _plans_touched — POINT-IN-TIME status resolution
#
# Every other field in a daily block is as-of-that-day; status must be too.
# `backfill_gaps` is the routine gap-filler, so a block composed days late must
# not stamp today's statuses under an older date.
# ---------------------------------------------------------------------------


def _commit_plan_dated(repo: Path, rel: str, body: str, when: str, msg: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = when
    env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(repo), "add", "--", rel], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_plans_touched_status_is_point_in_time_not_compose_time(tmp_path: Path) -> None:
    """A status flipped AFTER the day must not leak backward into that day's block.

    This is the backfill case: the plan was `draft` on 07-15 and only became
    `implemented` on 07-18. Composing the 07-15 block today must report `draft`.
    """
    repo = _init_repo(tmp_path)
    _commit_plan_dated(repo, "README.md", "root\n", "2026-07-14T10:00:00", "init")
    _commit_plan_dated(
        repo, "docs/plans/2026-07-15-p.md", "---\nstatus: draft\n---\n\n# P\n",
        "2026-07-15T10:00:00", "plan: add",
    )
    # Flipped three days later — must NOT colour the 07-15 block.
    _commit_plan_dated(
        repo, "docs/plans/2026-07-15-p.md", "---\nstatus: implemented\n---\n\n# P\n",
        "2026-07-18T10:00:00", "plan: ship",
    )

    result = _plans_touched(repo, "2026-07-15")

    assert result == "docs/plans/2026-07-15-p.md (status: draft)", result
    # The worktree says implemented; the 07-15 block must not.
    assert "implemented" not in result
    # ...and the day it actually shipped reports the new value.
    assert _plans_touched(repo, "2026-07-18") == "docs/plans/2026-07-15-p.md (status: implemented)"


def test_plans_touched_point_in_time_on_commit_span_path(tmp_path: Path) -> None:
    """The commit_span path resolves as-of from the span's TIP, not from HEAD."""
    repo = _init_repo(tmp_path)
    base = _commit_plan_dated(repo, "README.md", "root\n", "2026-07-14T10:00:00", "init")
    tip = _commit_plan_dated(
        repo, "docs/plans/2026-07-15-p.md", "---\nstatus: draft\n---\n\n# P\n",
        "2026-07-15T10:00:00", "plan: add",
    )
    _commit_plan_dated(
        repo, "docs/plans/2026-07-15-p.md", "---\nstatus: implemented\n---\n\n# P\n",
        "2026-07-18T10:00:00", "plan: ship",
    )

    result = _plans_touched(repo, "2026-07-15", commit_span=f"{base}..{tip}")

    assert result == "docs/plans/2026-07-15-p.md (status: draft)", result


def test_plans_touched_plan_deleted_later_still_reports_its_status_then(tmp_path: Path) -> None:
    """A plan alive on the day but deleted since reports its status THEN, not `removed`.

    Under compose-time-worktree resolution this rendered `removed`, erasing a
    plan that demonstrably existed and was implemented on the day in question.
    """
    repo = _init_repo(tmp_path)
    _commit_plan_dated(repo, "README.md", "root\n", "2026-07-14T10:00:00", "init")
    _commit_plan_dated(
        repo, "docs/plans/2026-07-15-doomed.md", "---\nstatus: implemented\n---\n\n# D\n",
        "2026-07-15T10:00:00", "plan: add",
    )
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "2026-07-19T10:00:00"
    subprocess.run(
        ["git", "-C", str(repo), "rm", "-q", "--", "docs/plans/2026-07-15-doomed.md"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "drop"], check=True, env=env)

    assert _plans_touched(repo, "2026-07-15") == (
        "docs/plans/2026-07-15-doomed.md (status: implemented)"
    )
    # On the day it was actually removed, `removed` is the truthful token.
    assert _plans_touched(repo, "2026-07-19") == "docs/plans/2026-07-15-doomed.md (status: removed)"


def test_plans_touched_falls_back_to_worktree_when_as_of_unresolvable(tmp_path: Path) -> None:
    """Unresolvable as-of (day precedes all history) degrades to the worktree read."""
    repo = _init_repo(tmp_path)
    _commit_plan_dated(repo, "README.md", "root\n", "2026-07-14T10:00:00", "init")
    _commit_plan_dated(
        repo, "docs/plans/2026-07-15-p.md", "---\nstatus: draft\n---\n\n# P\n",
        "2026-07-15T10:00:00", "plan: add",
    )

    assert _as_of_sha(repo, "2026-07-01") is None
    # Files list is empty for that window anyway, so the block is `none` — the
    # fallback is exercised directly instead.
    assert _plan_status(repo, "docs/plans/2026-07-15-p.md", as_of=None) == "draft"
    assert _plan_status(repo, "docs/plans/gone.md", as_of=None) == "removed"


# ---------------------------------------------------------------------------
# changelog.upsert_reviewed — surgical single-field **Reviewed:** upsert
# (cross-repo/inbox/2026-07-21-claude-central-em-reviewed-line-surgical-upsert.md)
#
# Curation-preserving counterpart to append_day: unlike append_day (which
# recomposes the whole machine section from supplied fields), upsert_reviewed
# must touch ONLY the **Reviewed:** line(s), leaving hand-curated
# Scope:/Commits:/etc content byte-identical.
# ---------------------------------------------------------------------------


def _write_review_record(worktree: Path, date: str, suffix: str, **fields) -> None:
    trail_dir = worktree / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "sha_range": "abc1234..def5678",
        "reviewer": "code-reviewer",
        "verdict": "pass",
        "diff_loc": 42,
    }
    record.update(fields)
    (trail_dir / f"{date}-{suffix}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


_CURATED_BLOCK_TMPL = (
    "## {date} — {machine}\n"
    "\n"
    "**Branch:** feature/curated\n"
    "**Commits:** 3 (range: abc1234..def5678)\n"
    "**Scope:** Hand-curated narrative that must survive untouched.\n"
    "**Plans touched:** none\n"
    "**Handoffs:** none\n"
    "**Decisions:** none\n"
    "**Blockers:** none\n"
    "**Validation:** validate=passed plugin-suite=n/a\n"
    "{reviewed_line}"
    "**Links:** archive/daily-summaries/{date}-{machine}.md, archive/completed/2026-02/ "
    '(per-entry files; query via `bin/query-completions --where "created={date}"`)\n'
)


class TestUpsertReviewed:
    def test_upsert_replaces_only_reviewed_line_rest_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """(a) upsert replaces only the Reviewed: line; curated Scope:/Commits:
        and every other line survive byte-identical."""
        date = "2026-02-01"
        machine = "machine-a"
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        changelog_file = week_dir / f"{date}.md"
        original = _CURATED_BLOCK_TMPL.format(
            date=date,
            machine=machine,
            reviewed_line="**Reviewed:** none — flag for /workweek-complete Step 7\n",
        )
        changelog_file.write_text(original, encoding="utf-8")

        _write_review_record(
            tmp_path, date, "120000-sid1",
            sha_range="aaa1111..bbb2222", reviewer="patrik", verdict="pass", diff_loc=12,
        )

        result = upsert_reviewed(worktree=tmp_path, date=date, machine=machine)

        assert result["action"] == "replaced"
        assert result["out_path"] == str(changelog_file)

        content = changelog_file.read_text(encoding="utf-8")
        assert (
            "**Reviewed:** sha_range=aaa1111..bbb2222 reviewer=the Staff Engineer verdict=pass diff_loc=12"
            in content
        )
        assert "none — flag for /workweek-complete Step 7" not in content

        # Every other line of the section is byte-identical to the original.
        expected = _CURATED_BLOCK_TMPL.format(
            date=date,
            machine=machine,
            reviewed_line=(
                "**Reviewed:** sha_range=aaa1111..bbb2222 reviewer=the Staff Engineer "
                "verdict=pass diff_loc=12\n"
            ),
        )
        assert content == expected

    def test_upsert_idempotent_rerun(self, tmp_path: Path) -> None:
        """(b) a second identical upsert is a no-op ("unchanged"), byte-identical output."""
        date = "2026-02-01"
        machine = "machine-a"
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        changelog_file = week_dir / f"{date}.md"
        changelog_file.write_text(
            _CURATED_BLOCK_TMPL.format(
                date=date,
                machine=machine,
                reviewed_line="**Reviewed:** none — flag for /workweek-complete Step 7\n",
            ),
            encoding="utf-8",
        )
        _write_review_record(
            tmp_path, date, "120000-sid1",
            sha_range="aaa1111..bbb2222", reviewer="patrik", verdict="pass", diff_loc=12,
        )

        first = upsert_reviewed(worktree=tmp_path, date=date, machine=machine)
        assert first["action"] == "replaced"
        content_after_first = changelog_file.read_text(encoding="utf-8")

        second = upsert_reviewed(worktree=tmp_path, date=date, machine=machine)
        assert second["action"] == "unchanged"

        content_after_second = changelog_file.read_text(encoding="utf-8")
        assert content_after_second == content_after_first

    def test_upsert_no_match_when_no_matching_section(self, tmp_path: Path) -> None:
        """(c) (date, machine) with no matching section -> "no_match" (not an error)."""
        date = "2026-02-01"
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        changelog_file = week_dir / f"{date}.md"
        changelog_file.write_text(f"## {date} — other-machine\n\nbody\n", encoding="utf-8")

        result = upsert_reviewed(worktree=tmp_path, date=date, machine="missing-machine")

        assert result["action"] == "no_match"
        # File untouched.
        assert changelog_file.read_text(encoding="utf-8") == f"## {date} — other-machine\n\nbody\n"

    def test_upsert_no_match_when_no_changelog_file(self, tmp_path: Path) -> None:
        """(c) no state/week-changelog/{date}.md at all -> "no_match", not an error."""
        result = upsert_reviewed(worktree=tmp_path, date="2026-02-01", machine="machine-a")

        assert result["action"] == "no_match"

    def test_upsert_inserts_reviewed_line_when_none_previously_rendered(
        self, tmp_path: Path
    ) -> None:
        """A section with NO **Reviewed:** line at all (has_non_trivial was False
        at compose time, reviewed_lines was empty) still gets the line inserted
        once review-trail records appear for that date."""
        date = "2026-02-01"
        machine = "machine-a"
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        changelog_file = week_dir / f"{date}.md"
        changelog_file.write_text(
            _CURATED_BLOCK_TMPL.format(date=date, machine=machine, reviewed_line=""),
            encoding="utf-8",
        )
        _write_review_record(
            tmp_path, date, "120000-sid1",
            sha_range="ccc3333..ddd4444", reviewer="sid", verdict="warn", diff_loc=7,
        )

        result = upsert_reviewed(worktree=tmp_path, date=date, machine=machine)

        assert result["action"] == "replaced"
        content = changelog_file.read_text(encoding="utf-8")
        assert (
            "**Reviewed:** sha_range=ccc3333..ddd4444 reviewer=sid verdict=warn diff_loc=7"
            in content
        )
        assert "**Scope:** Hand-curated narrative that must survive untouched." in content
        # Inserted right after **Validation:**, before **Links:**.
        lines = content.splitlines()
        validation_idx = next(i for i, ln in enumerate(lines) if ln.startswith("**Validation:**"))
        assert lines[validation_idx + 1].startswith("**Reviewed:**")

    def test_upsert_handler_rejects_unsafe_machine(self, tmp_path: Path) -> None:
        """JSON-RPC handler containment: 'machine' must be a safe filename segment."""
        import asyncio

        from coordinator_core.ops.changelog_ops import _upsert_reviewed_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _upsert_reviewed_handler(
                {"date": "2026-02-01", "machine": "../escape"}, repo_root=fake_git_dir
            )
        )
        assert result["action"] == "error"
        assert "not a safe filename segment" in result["error"]

    def test_upsert_handler_rejects_bad_date(self, tmp_path: Path) -> None:
        import asyncio

        from coordinator_core.ops.changelog_ops import _upsert_reviewed_handler

        fake_git_dir = tmp_path / ".git"
        fake_git_dir.mkdir()

        result = asyncio.run(
            _upsert_reviewed_handler(
                {"date": "not-a-date", "machine": "machine-a"}, repo_root=fake_git_dir
            )
        )
        assert result["action"] == "error"
        assert "not a valid YYYY-MM-DD date" in result["error"]

    def test_upsert_non_contiguous_stray_reviewed_line_preserved(
        self, tmp_path: Path
    ) -> None:
        """Review: code-reviewer (Finding 1) — a curator-added, non-contiguous
        stray line that happens to start with "**Reviewed:**" elsewhere in the
        section must NOT be relocated/collapsed by the strip-then-reinsert
        path. old_indices is non-contiguous here, so upsert_reviewed must fall
        back to fresh-insertion (anchored on **Validation:**) and leave the
        stray line exactly where the curator put it."""
        date = "2026-02-01"
        machine = "machine-a"
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        changelog_file = week_dir / f"{date}.md"
        # A hand-curated section with a stray "**Reviewed:**"-prefixed line
        # inside the Scope narrative (non-contiguous with the managed block).
        original = (
            f"## {date} — {machine}\n"
            "\n"
            "**Branch:** feature/curated\n"
            "**Commits:** 3 (range: abc1234..def5678)\n"
            "**Scope:** Hand-curated narrative that must survive untouched.\n"
            "**Reviewed:** stray curator note, not the managed block\n"
            "**Plans touched:** none\n"
            "**Handoffs:** none\n"
            "**Decisions:** none\n"
            "**Blockers:** none\n"
            "**Validation:** validate=passed plugin-suite=n/a\n"
            "**Reviewed:** none — flag for /workweek-complete Step 7\n"
            f"**Links:** archive/daily-summaries/{date}-{machine}.md, archive/completed/2026-02/ "
            '(per-entry files; query via `bin/query-completions --where "created={date}"`)\n'
        )
        changelog_file.write_text(original, encoding="utf-8")

        _write_review_record(
            tmp_path, date, "120000-sid1",
            sha_range="aaa1111..bbb2222", reviewer="patrik", verdict="pass", diff_loc=12,
        )

        result = upsert_reviewed(worktree=tmp_path, date=date, machine=machine)

        assert result["action"] == "replaced"
        content = changelog_file.read_text(encoding="utf-8")

        # The stray curator line survives, untouched and unrelocated.
        assert "**Reviewed:** stray curator note, not the managed block" in content
        # The newly-derived Reviewed value is present too.
        assert (
            "**Reviewed:** sha_range=aaa1111..bbb2222 reviewer=the Staff Engineer "
            "verdict=pass diff_loc=12" in content
        )
        # Non-contiguous old_indices means NO strip happens at all (this is
        # the point of the fix) — the pre-existing "none" placeholder line is
        # untouched, not stripped, exactly like the stray curator line.
        assert "**Reviewed:** none — flag for /workweek-complete Step 7" in content
        # Fresh-insertion path anchors the new block right after **Validation:**.
        lines = content.splitlines()
        validation_idx = next(i for i, ln in enumerate(lines) if ln.startswith("**Validation:**"))
        assert lines[validation_idx + 1].startswith(
            "**Reviewed:** sha_range=aaa1111..bbb2222"
        )
        # The stray line still precedes **Plans touched:**, exactly as authored.
        stray_idx = next(
            i for i, ln in enumerate(lines)
            if ln == "**Reviewed:** stray curator note, not the managed block"
        )
        plans_idx = next(i for i, ln in enumerate(lines) if ln.startswith("**Plans touched:**"))
        assert stray_idx < plans_idx

    def test_upsert_leaves_sibling_machine_section_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """Review: code-reviewer (Finding 2) — mirrors append_day's
        prefix-collision regression test style: a two-machine {date}.md file
        must have machine b's section left byte-identical when machine a's
        Reviewed: line is upserted."""
        date = "2026-02-01"
        week_dir = tmp_path / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        changelog_file = week_dir / f"{date}.md"

        section_a = _CURATED_BLOCK_TMPL.format(
            date=date,
            machine="machine-a",
            reviewed_line="**Reviewed:** none — flag for /workweek-complete Step 7\n",
        )
        section_b = _CURATED_BLOCK_TMPL.format(
            date=date,
            machine="machine-b",
            reviewed_line="**Reviewed:** none — flag for /workweek-complete Step 7\n",
        )
        changelog_file.write_text(section_a + "\n" + section_b, encoding="utf-8")

        _write_review_record(
            tmp_path, date, "120000-sid1",
            sha_range="aaa1111..bbb2222", reviewer="patrik", verdict="pass", diff_loc=12,
        )

        result = upsert_reviewed(worktree=tmp_path, date=date, machine="machine-a")
        assert result["action"] == "replaced"

        content = changelog_file.read_text(encoding="utf-8")
        # machine-b's section is byte-identical to what was originally written.
        assert section_b in content
        # machine-a's section was in fact changed.
        assert section_a not in content
        assert (
            "**Reviewed:** sha_range=aaa1111..bbb2222 reviewer=the Staff Engineer "
            "verdict=pass diff_loc=12" in content
        )
