"""
Tests for coordinator_core.ops.generate_exec_summary.

Mirrors the bash oracle's own test coverage (T1-T6) plus the Rule-5
meta-repo-quirk faithful-repro case (T7, this port's own coverage — the bash
oracle's behavior here was never independently unit-tested in bash, only
exercised implicitly).

Port of: generate-exec-summary.test.sh (DoE a2fe06f8, 2026-07-22)
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from coordinator_core import meta_repo_identity
from coordinator_core import state_root as state_root_mod
from coordinator_core.ops import generate_exec_summary as mod


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    (repo_dir / "docs").mkdir(parents=True)
    (repo_dir / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True, capture_output=True)

    readme = repo_dir / "README.md"
    readme.write_text(
        textwrap.dedent(
            """\
            # Test Project — A Sample Repo

            This is the lead paragraph describing what the project does.
            It spans a single paragraph of meaningful content.

            ## Other section
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(repo_dir)
    return repo_dir


def test_t1_check_flag_prints_without_writing(repo, capsys):
    target = repo / "docs" / "exec-summary.md"
    rc = mod.main(["--check"])
    out = capsys.readouterr().out

    # target is absent -- a real run would create it, so --check now fails
    # loud rather than silently reporting an always-green 0 (still previews
    # the would-be content to stdout without writing anything).
    assert rc == 1
    assert "kind: exec-summary" in out
    assert "Test Project" in out
    assert "<!-- BEGIN HAND: special -->" in out
    assert "<!-- BEGIN MANAGED: identity -->" in out
    assert not target.exists()


def test_t1b_check_flag_fresh_after_real_run_is_no_op(repo, capsys):
    target = repo / "docs" / "exec-summary.md"
    assert mod.main([]) == 0
    capsys.readouterr()

    rc = mod.main(["--check"])

    assert rc == 0
    assert not target.exists() or target.is_file()  # real file untouched by --check
    err = capsys.readouterr().err
    assert "up to date" in err


def test_t2_new_file_creation(repo):
    target = repo / "docs" / "exec-summary.md"
    rc = mod.main([])
    assert rc == 0

    content = target.read_text(encoding="utf-8")
    assert "<!-- BEGIN MANAGED: identity -->" in content
    assert "<!-- BEGIN HAND: special -->" in content
    assert "<!-- BEGIN MANAGED: progress -->" in content
    assert "This is the lead paragraph" in content


def test_t3_hand_block_preserved_verbatim_on_regen(repo):
    target = repo / "docs" / "exec-summary.md"
    mod.main([])

    sentinel = "MY_UNIQUE_SENTINEL_TEXT_7x9q"
    content = target.read_text(encoding="utf-8")
    content = content.replace(
        "<!-- BEGIN HAND: special -->",
        f"<!-- BEGIN HAND: special -->\n{sentinel}",
        1,
    )
    target.write_text(content, encoding="utf-8")

    rc = mod.main([])
    assert rc == 0
    assert sentinel in target.read_text(encoding="utf-8")


def test_t4_managed_identity_refreshed_on_regen(repo):
    target = repo / "docs" / "exec-summary.md"
    mod.main([])
    mod.main([])
    assert "Test Project" in target.read_text(encoding="utf-8")


def test_t5_fail_loud_on_malformed_hand_fence(repo):
    target = repo / "docs" / "exec-summary.md"
    mod.main([])

    sentinel = "MY_UNIQUE_SENTINEL_TEXT_7x9q"
    content = target.read_text(encoding="utf-8")
    content = content.replace(
        "<!-- BEGIN HAND: special -->",
        f"<!-- BEGIN HAND: special -->\n{sentinel}",
        1,
    )
    content = content.replace("<!-- END HAND: special -->\n", "")
    target.write_text(content, encoding="utf-8")

    rc = mod.main([])
    assert rc != 0
    # File must NOT be overwritten — sentinel from before the corruption survives.
    assert sentinel in target.read_text(encoding="utf-8")


def test_t6_git_log_fallback_when_no_week_changelog(repo):
    target = repo / "docs" / "exec-summary.md"
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-q", "-m", "initial", "--allow-empty")

    target.unlink(missing_ok=True)
    rc = mod.main([])
    assert rc == 0

    content = target.read_text(encoding="utf-8")
    start = content.index("<!-- BEGIN MANAGED: progress -->")
    end = content.index("<!-- END MANAGED: progress -->")
    progress_section = content[start:end]
    assert progress_section.strip() != "<!-- BEGIN MANAGED: progress -->"


def test_t7_unknown_argument_exits_2(repo):
    rc = mod.main(["--bogus"])
    assert rc == 2


def test_t8_not_a_git_repo_exits_1(tmp_path, monkeypatch, capsys):
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)
    rc = mod.main([])
    assert rc == 1
    assert "not inside a git repository" in capsys.readouterr().err


def test_t9_state_root_rule5_meta_repo_error_faithfully_treated_as_false(repo, monkeypatch):
    """Faithful oracle-bug repro: bash `if coordinator_is_meta_repo ...` treats
    a resolution ERROR (rc=2) the same as "false" (rc=1) — see module docstring
    negative-spec. A raised MetaRepoResolutionError must fall through to the
    sibling-repo branch (repo_root/state), not propagate as a hard failure."""

    def _raise(*_args, **_kwargs):
        raise meta_repo_identity.MetaRepoResolutionError("boom")

    monkeypatch.setattr(meta_repo_identity, "is_meta_repo", _raise)

    result = mod._resolve_state_root(str(repo))
    assert result == os.path.join(str(repo), "state")


def test_t10_resolved_engine_class_refuses_write_loudly(repo, tmp_path, monkeypatch, capsys):
    """Spec backlink: commit 5dedf53b9 (state_root's published-mirror guard).

    When the meta-repo branch resolves via `coordinator_claude_klabauter_root_with_class`
    to a `resolved-engine` class (a published claude-klabauter-style mirror,
    not a live working tree), the generator must refuse loudly rather than
    write claude-klabauter state into the mirror. Reuses
    `coordinator_core.state_root._claude_klabauter_state`'s own guard -- this test
    monkeypatches `coordinator_claude_klabauter_root_with_class` as SEEN THROUGH
    `state_root`'s own import binding (not generate_exec_summary's), which is
    only reachable if the fix actually calls through that shared mechanism
    rather than a reimplemented copy.
    """
    monkeypatch.setattr(meta_repo_identity, "is_meta_repo", lambda _root: True)

    mirror_root = str(tmp_path / "published-mirror-checkout")

    def _fake_with_class():
        return (mirror_root, "resolved-engine")

    monkeypatch.setattr(state_root_mod, "coordinator_claude_klabauter_root_with_class", _fake_with_class)

    rc = mod.main([])
    err = capsys.readouterr().err

    assert rc == 1
    assert "PUBLISHED engine mirror" in err
    assert not os.path.exists(os.path.join(mirror_root, "state"))
    assert not os.path.isfile(repo / "docs" / "exec-summary.md")


def test_t11_live_working_tree_class_unchanged_behavior(repo, tmp_path, monkeypatch):
    """Ordinary case: `live-working-tree` class behaves identically to the
    pre-fix class-less resolution -- the guard only intervenes on
    `resolved-engine`, never on a genuine working tree."""
    monkeypatch.setattr(meta_repo_identity, "is_meta_repo", lambda _root: True)

    live_root = str(tmp_path / "live-claude-klabauter-checkout")
    os.makedirs(live_root, exist_ok=True)

    def _fake_with_class():
        return (live_root, "live-working-tree")

    monkeypatch.setattr(state_root_mod, "coordinator_claude_klabauter_root_with_class", _fake_with_class)

    result = mod._resolve_state_root(str(repo))
    assert result == os.path.join(live_root, "state")


def test_link_rewriting_prefixes_bare_repo_relative_targets():
    text = "See [docs](archive/foo.md) and [ext](https://example.com) and [anchor](#top)."
    rewritten = mod._rewrite_managed_links(text)
    assert "](../archive/foo.md)" in rewritten
    assert "](https://example.com)" in rewritten
    assert "](#top)" in rewritten


def test_link_rewriting_renormalizes_stray_relative_targets():
    """A target harvested verbatim from a nested source (e.g. an archived
    week-changelog's own `## Highlights` section) may already carry a `../`
    prefix computed for THAT source's own location, not for
    docs/exec-summary.md's. The prior skip-list left such targets untouched,
    producing a one-`../`-too-many defect (`../../archive/...`) — this must
    renormalize to the SAME output a bare target for the same file would
    produce."""
    text = "[a](../archive/foo.md)"
    rewritten = mod._rewrite_managed_links(text)
    assert rewritten == "[a](../archive/foo.md)"
    assert mod._rewrite_managed_links("[a](archive/foo.md)") == rewritten


def test_link_rewriting_does_not_double_prefix_stray_double_relative_target():
    text = "[source](../../archive/daily-summaries/2026-07-12-machine-b.md)"
    rewritten = mod._rewrite_managed_links(text)
    assert rewritten == "[source](../archive/daily-summaries/2026-07-12-machine-b.md)"


def test_rewritten_managed_links_resolve_on_disk_relative_to_output_dir(tmp_path):
    """AC: every emitted MANAGED-section link target must RESOLVE ON DISK
    relative to docs/exec-summary.md's own directory — not merely pin the
    current string output."""
    repo_dir = tmp_path / "diskrepo"
    (repo_dir / "docs").mkdir(parents=True)
    (repo_dir / "archive" / "daily-summaries").mkdir(parents=True)
    target_file = repo_dir / "archive" / "daily-summaries" / "2026-07-12-machine-b.md"
    target_file.write_text("hi", encoding="utf-8")

    for raw_target in (
        "archive/daily-summaries/2026-07-12-machine-b.md",
        "../archive/daily-summaries/2026-07-12-machine-b.md",
        "../../archive/daily-summaries/2026-07-12-machine-b.md",
    ):
        text = f"[source]({raw_target})"
        rewritten = mod._rewrite_managed_links(text)
        emitted_target = rewritten[len("[source]("):-1]
        out_dir = repo_dir / os.path.dirname(mod._EXEC_SUMMARY_OUT_PATH)
        resolved = os.path.normpath(str(out_dir / emitted_target))
        assert resolved == os.path.normpath(str(target_file))


def test_extract_section_stops_only_at_next_heading_not_blank_lines():
    text = "## Counters\nline1\n\nline2\n## Next\nline3\n"
    assert mod._extract_section(text, "## Counters") == "line1\nline2"


def test_trim_trailing_blank():
    assert mod._trim_trailing_blank("a\nb\n\n\n") == "a\nb"


def test_derive_project_title_falls_back_to_basename(tmp_path):
    repo_dir = tmp_path / "no-readme-repo"
    repo_dir.mkdir()
    assert mod._derive_project_title(str(repo_dir)) == "no-readme-repo"


def test_derive_project_title_cap_200_chars(tmp_path):
    repo_dir = tmp_path / "capped"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("# " + ("x" * 250) + "\n", encoding="utf-8")
    result = mod._derive_project_title(str(repo_dir))
    assert len(result) == 200
