
from __future__ import annotations

import pytest

from coordinator_core.claude_md_budget import (
    AUDIENCE_MANIFEST_RELPATH,
    DEV_REPO_SENTINEL,
    BudgetVerdict,
    HARD_LIMIT_BYTES,
    RatchetWatermark,
    RatchetWatermarkError,
    SOFT_LIMIT_BYTES,
    budget_verdict,
    governed_surface_paths,
    is_claude_md_class,
    is_governed_claude_md,
    is_ledger_admission_scoped,
    load_audience_manifest,
    parse_watermark,
    ratchet_check,
    resolve_ledger_path,
    surface_slug,
)


def test_thresholds_are_the_external_perf_ceiling_pair_soft_below_hard():
    """HARD_LIMIT_BYTES/SOFT_LIMIT_BYTES are Claude Code's own documented
    40K load-time perf-warning threshold (see module docstring "Derivation")
    -- an EXTERNAL constant this module reports against, not a surface the
    AC4 ratchet targets. The ratchet governs each GOVERNED SURFACE's own
    content size via its ledger watermark (see TestRatchetWatermark below);
    it does not, and must not, retarget this externally-imposed pair.
    """
    assert SOFT_LIMIT_BYTES < HARD_LIMIT_BYTES
    assert HARD_LIMIT_BYTES == 40000
    assert SOFT_LIMIT_BYTES == 38000


def test_global_home_claude_md_is_governed(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    global_file = home / ".claude" / "CLAUDE.md"
    global_file.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(global_file, home=home) is True


def test_bare_home_claude_md_without_dotclaude_dir_is_NOT_governed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bare = home / "CLAUDE.md"
    bare.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(bare, home=home) is False


def test_dev_repo_coordinator_claude_md_is_governed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / DEV_REPO_SENTINEL).write_text("sentinel", encoding="utf-8")
    coord_dir = repo_root / "coordinator"
    coord_dir.mkdir()
    coord_claude_md = coord_dir / "CLAUDE.md"
    coord_claude_md.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(coord_claude_md, home=home) is True


def test_repo_scoped_claude_md_is_NOT_governed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    (repo_root / DEV_REPO_SENTINEL).write_text("sentinel", encoding="utf-8")
    repo_scoped = repo_root / "CLAUDE.md"
    repo_scoped.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(repo_scoped, home=home) is False


def test_sibling_repo_without_sentinel_coordinator_claude_md_is_NOT_governed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "some-other-repo"
    coord_dir = repo_root / "coordinator"
    coord_dir.mkdir(parents=True)
    other_claude_md = coord_dir / "CLAUDE.md"
    other_claude_md.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(other_claude_md, home=home) is False


def test_non_claude_md_basename_is_never_governed(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    other = home / ".claude" / "NOT-CLAUDE.md"
    other.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(other, home=home) is False


def test_governed_surface_paths_global_only_without_repo_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    paths = governed_surface_paths(home=home)

    assert paths == [home / ".claude" / "CLAUDE.md"]


def test_governed_surface_paths_includes_coordinator_claude_md_with_sentinel(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "coordinator-content-repo"
    repo_root.mkdir()
    (repo_root / DEV_REPO_SENTINEL).write_text("sentinel", encoding="utf-8")

    paths = governed_surface_paths(home=home, repo_root=repo_root)

    assert paths == [home / ".claude" / "CLAUDE.md", repo_root / "coordinator" / "CLAUDE.md"]


def test_governed_surface_paths_excludes_coordinator_claude_md_without_sentinel(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "some-other-repo"
    repo_root.mkdir()

    paths = governed_surface_paths(home=home, repo_root=repo_root)

    assert paths == [home / ".claude" / "CLAUDE.md"]


def test_global_home_claude_md_is_claude_md_class(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    global_file = home / ".claude" / "CLAUDE.md"
    global_file.write_text("x", encoding="utf-8")

    assert is_claude_md_class(global_file) is True


def test_global_doctrine_mirror_claude_md_is_claude_md_class(tmp_path):
    repo_root = tmp_path / "coordinator-content-repo"
    mirror_dir = repo_root / "global-doctrine"
    mirror_dir.mkdir(parents=True)
    mirror_file = mirror_dir / "CLAUDE.md"
    mirror_file.write_text("x", encoding="utf-8")

    assert is_claude_md_class(mirror_file) is True


def test_coordinator_claude_md_is_claude_md_class_even_without_sentinel(tmp_path):
    repo_root = tmp_path / "some-other-repo"
    coord_dir = repo_root / "coordinator"
    coord_dir.mkdir(parents=True)
    coord_claude_md = coord_dir / "CLAUDE.md"
    coord_claude_md.write_text("x", encoding="utf-8")

    assert is_claude_md_class(coord_claude_md) is True


def test_coordinator_templates_claude_md_tmpl_is_claude_md_class(tmp_path):
    templates_dir = tmp_path / "coordinator-content-repo" / "coordinator" / "templates"
    templates_dir.mkdir(parents=True)
    tmpl_file = templates_dir / "CLAUDE.md.tmpl"
    tmpl_file.write_text("x", encoding="utf-8")

    assert is_claude_md_class(tmpl_file) is True


def test_repo_root_claude_md_is_claude_md_class(tmp_path):
    repo_root = tmp_path / "some-repo"
    repo_root.mkdir()
    repo_scoped = repo_root / "CLAUDE.md"
    repo_scoped.write_text("x", encoding="utf-8")

    assert is_claude_md_class(repo_scoped) is True


def test_non_claude_md_basename_is_not_claude_md_class(tmp_path):
    other = tmp_path / "NOT-CLAUDE.md"
    other.write_text("x", encoding="utf-8")

    assert is_claude_md_class(other) is False


def test_claude_local_md_is_not_claude_md_class(tmp_path):
    other = tmp_path / "CLAUDE.local.md"
    other.write_text("x", encoding="utf-8")

    assert is_claude_md_class(other) is False


def test_tmpl_file_outside_templates_dir_is_not_claude_md_class(tmp_path):
    other_dir = tmp_path / "coordinator" / "not-templates"
    other_dir.mkdir(parents=True)
    tmpl_file = other_dir / "CLAUDE.md.tmpl"
    tmpl_file.write_text("x", encoding="utf-8")

    assert is_claude_md_class(tmpl_file) is False


def test_ledger_admission_scoped_agrees_with_claude_md_class():
    import inspect

    assert (
        inspect.signature(is_ledger_admission_scoped).parameters.keys()
        == inspect.signature(is_claude_md_class).parameters.keys()
    )


def test_sibling_repo_coordinator_claude_md_without_sentinel_is_ledger_scoped(tmp_path):
    repo_root = tmp_path / "some-other-repo"
    coord_dir = repo_root / "coordinator"
    coord_dir.mkdir(parents=True)
    other_claude_md = coord_dir / "CLAUDE.md"
    other_claude_md.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(other_claude_md, home=tmp_path / "home") is False
    assert is_ledger_admission_scoped(other_claude_md) is True


def test_template_claude_md_tmpl_is_ledger_scoped(tmp_path):
    templates_dir = tmp_path / "coordinator-content-repo" / "coordinator" / "templates"
    templates_dir.mkdir(parents=True)
    tmpl_file = templates_dir / "CLAUDE.md.tmpl"
    tmpl_file.write_text("x", encoding="utf-8")

    assert is_ledger_admission_scoped(tmpl_file) is True


def test_repo_root_claude_md_is_ledger_scoped(tmp_path):
    repo_root = tmp_path / "some-repo"
    repo_root.mkdir()
    repo_scoped = repo_root / "CLAUDE.md"
    repo_scoped.write_text("x", encoding="utf-8")

    assert is_ledger_admission_scoped(repo_scoped) is True


def test_non_claude_md_basename_is_not_ledger_scoped(tmp_path):
    other = tmp_path / "NOT-CLAUDE.md"
    other.write_text("x", encoding="utf-8")

    assert is_ledger_admission_scoped(other) is False


def test_global_governed_claude_md_is_still_ledger_scoped(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    global_file = home / ".claude" / "CLAUDE.md"
    global_file.write_text("x", encoding="utf-8")

    assert is_ledger_admission_scoped(global_file) is True


# --- C7b AC3: governed BY AUDIENCE, not filename ----------------------------


def test_load_audience_manifest_absent_file_returns_empty(tmp_path):
    repo_root = tmp_path / "some-repo"
    repo_root.mkdir()

    assert load_audience_manifest(repo_root) == []


def test_load_audience_manifest_parses_entries_and_skips_comments_and_blanks(tmp_path):
    repo_root = tmp_path / "some-repo"
    manifest_dir = repo_root / "coordinator"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "audience-manifest.txt").write_text(
        "# governed every-agent surfaces\n"
        "\n"
        "global-doctrine/CLAUDE.md\n"
        "CLAUDE.md\n"
        "  coordinator/snippets/agent-role-dispatched.md  \n",
        encoding="utf-8",
    )

    assert load_audience_manifest(repo_root) == [
        "global-doctrine/CLAUDE.md",
        "CLAUDE.md",
        "coordinator/snippets/agent-role-dispatched.md",
    ]


def test_audience_manifest_relpath_is_coordinator_scoped():
    assert AUDIENCE_MANIFEST_RELPATH == "coordinator/audience-manifest.txt"


def test_a_new_always_on_surface_of_any_name_is_governed_without_a_code_change(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "any-repo"
    target = repo_root / "coordinator" / "snippets" / "brand-new-always-on-surface.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    assert (
        is_governed_claude_md(
            target,
            home=home,
            repo_root=repo_root,
            audience_manifest=["coordinator/snippets/brand-new-always-on-surface.md"],
        )
        is True
    )


def test_audience_manifest_surface_not_listed_stays_ungoverned(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "any-repo"
    other = repo_root / "coordinator" / "snippets" / "unrelated.md"
    other.parent.mkdir(parents=True)
    other.write_text("x", encoding="utf-8")

    assert (
        is_governed_claude_md(
            other,
            home=home,
            repo_root=repo_root,
            audience_manifest=["coordinator/snippets/brand-new-always-on-surface.md"],
        )
        is False
    )


def test_audience_manifest_resolved_from_disk_when_not_passed_explicitly(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "any-repo"
    manifest_dir = repo_root / "coordinator"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "audience-manifest.txt").write_text(
        "docs/some-always-on-file.md\n", encoding="utf-8"
    )
    target = repo_root / "docs" / "some-always-on-file.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    assert is_governed_claude_md(target, home=home, repo_root=repo_root) is True


def test_governed_surface_paths_appends_audience_manifest_entries(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "any-repo"
    repo_root.mkdir()

    paths = governed_surface_paths(
        home=home,
        repo_root=repo_root,
        audience_manifest=["global-doctrine/CLAUDE.md", "coordinator/snippets/foo.md"],
    )

    assert paths == [
        home / ".claude" / "CLAUDE.md",
        repo_root / "global-doctrine" / "CLAUDE.md",
        repo_root / "coordinator" / "snippets" / "foo.md",
    ]


def test_governed_surface_paths_unmanifested_repo_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "unmanifested-repo"
    repo_root.mkdir()

    assert governed_surface_paths(home=home, repo_root=repo_root) == [
        home / ".claude" / "CLAUDE.md"
    ]


class TestSurfaceSlug:
    def test_slashes_become_hyphens_and_md_suffix_drops(self):
        assert surface_slug("global-doctrine/CLAUDE.md") == "global-doctrine-claude"

    def test_lowercased(self):
        assert surface_slug("CLAUDE.md") == "claude"


class TestResolveLedgerPath:
    def test_override_surface_resolves_to_the_2026_07_27_ledger(self, tmp_path):
        path = resolve_ledger_path(tmp_path, "global-doctrine/CLAUDE.md")
        assert path == tmp_path / "state" / "audits" / "2026-07-27-doctrine-envelope-classification.md"

    def test_non_override_surface_resolves_by_convention(self, tmp_path):
        path = resolve_ledger_path(tmp_path, "coordinator/snippets/agent-role-dispatched.md")
        assert path == (
            tmp_path
            / "state"
            / "audits"
            / "coordinator-snippets-agent-role-dispatched-classification.md"
        )


class TestParseWatermark:
    def test_missing_ledger_returns_none(self, tmp_path):
        assert parse_watermark(tmp_path / "nope.md") is None

    def test_ledger_with_no_watermark_section_returns_none(self, tmp_path):
        ledger = tmp_path / "ledger.md"
        ledger.write_text("## Classification table\n| # | Heading | ... |\n", encoding="utf-8")
        assert parse_watermark(ledger) is None

    def test_armed_watermark_parses_bytes_and_reason(self, tmp_path):
        ledger = tmp_path / "ledger.md"
        ledger.write_text(
            "## Classification table\n\n"
            "## Watermark\n"
            "- Bytes: 5997\n"
            "- Reason: post-cut arming, C7b\n",
            encoding="utf-8",
        )
        watermark = parse_watermark(ledger)
        assert watermark == RatchetWatermark(bytes=5997, reason="post-cut arming, C7b")

    def test_watermark_missing_bytes_row_fails_loud(self, tmp_path):
        ledger = tmp_path / "ledger.md"
        ledger.write_text("## Watermark\n- Reason: no bytes row\n", encoding="utf-8")
        with pytest.raises(RatchetWatermarkError):
            parse_watermark(ledger)

    def test_watermark_missing_reason_row_fails_loud(self, tmp_path):
        ledger = tmp_path / "ledger.md"
        ledger.write_text("## Watermark\n- Bytes: 100\n", encoding="utf-8")
        with pytest.raises(RatchetWatermarkError):
            parse_watermark(ledger)


class TestRatchetWatermark:

    def test_unarmed_always_allows(self):
        allowed, msg = ratchet_check(999_999, None)
        assert allowed is True
        assert msg == ""

    def test_shrinking_under_watermark_allowed(self):
        watermark = RatchetWatermark(bytes=6000, reason="post-cut")
        allowed, _msg = ratchet_check(5000, watermark)
        assert allowed is True

    def test_holding_exactly_at_watermark_allowed(self):
        watermark = RatchetWatermark(bytes=6000, reason="post-cut")
        allowed, _msg = ratchet_check(6000, watermark)
        assert allowed is True

    def test_growing_past_watermark_denied_with_reason_in_message(self):
        watermark = RatchetWatermark(bytes=6000, reason="post-cut arming")
        allowed, msg = ratchet_check(6001, watermark)
        assert allowed is False
        assert "6000" in msg
        assert "post-cut arming" in msg


class TestBudgetVerdict:
    """In-memory-reader mirror of every case in
    bash_guards/tests/test_check_seven_claude_md_budget.py."""

    SURFACE = "coordinator/CLAUDE.md"

    def _dev_repo(self, tmp_path, watermark=None):
        (tmp_path / DEV_REPO_SENTINEL).write_text("s\n", encoding="utf-8")
        if watermark is not None:
            ledger = resolve_ledger_path(tmp_path, self.SURFACE)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(watermark, encoding="utf-8")
        return tmp_path

    def _verdict(self, root, size, head=None, path=None):
        path = path or self.SURFACE
        post = {path: b"x" * size}
        heads = {} if head is None else {path: b"x" * head}
        return budget_verdict(root, [path], post.get, heads.get)

    def test_over_hard_limit(self, tmp_path):
        v = self._verdict(self._dev_repo(tmp_path), HARD_LIMIT_BYTES + 1)
        assert len(v.hard) == 1 and v.soft == ()
        assert v.hard[0].startswith("\n  coordinator/CLAUDE.md = 40001 chars")
        assert v.hard[0].endswith("(limit 40000)")

    def test_exact_hard_limit_is_soft_only(self, tmp_path):
        v = self._verdict(self._dev_repo(tmp_path), HARD_LIMIT_BYTES)
        assert v.hard == () and len(v.soft) == 1
        assert v.soft[0].endswith("(soft 38000; hard 40000)")

    def test_soft_boundaries(self, tmp_path):
        root = self._dev_repo(tmp_path)
        assert len(self._verdict(root, SOFT_LIMIT_BYTES + 1).soft) == 1
        assert self._verdict(root, SOFT_LIMIT_BYTES) == BudgetVerdict((), ())
        assert self._verdict(root, SOFT_LIMIT_BYTES - 1) == BudgetVerdict((), ())

    def test_repo_root_claude_md_ungoverned(self, tmp_path):
        v = self._verdict(self._dev_repo(tmp_path), HARD_LIMIT_BYTES + 50000, path="CLAUDE.md")
        assert v == BudgetVerdict((), ())

    def test_no_sentinel_ungoverned(self, tmp_path):
        assert self._verdict(tmp_path, HARD_LIMIT_BYTES + 50000) == BudgetVerdict((), ())

    def test_manifest_named_surface_governed(self, tmp_path):
        (tmp_path / "coordinator").mkdir()
        (tmp_path / AUDIENCE_MANIFEST_RELPATH).write_text(
            "coordinator/snippets/em.md\n", encoding="utf-8"
        )
        v = self._verdict(tmp_path, HARD_LIMIT_BYTES + 1, path="coordinator/snippets/em.md")
        assert len(v.hard) == 1
        other = self._verdict(tmp_path, HARD_LIMIT_BYTES + 1, path="coordinator/snippets/x.md")
        assert other == BudgetVerdict((), ())

    WM = "## Watermark\n- Bytes: 6000\n- Reason: post-cut arming\n"

    def test_growth_past_watermark_refused(self, tmp_path):
        v = self._verdict(self._dev_repo(tmp_path, self.WM), 6001)
        assert len(v.hard) == 1 and "6000" in v.hard[0] and v.soft == ()

    def test_hold_and_shrink_under_watermark_allowed(self, tmp_path):
        root = self._dev_repo(tmp_path, self.WM)
        assert self._verdict(root, 6000) == BudgetVerdict((), ())
        assert self._verdict(root, 100) == BudgetVerdict((), ())

    def test_over_watermark_shrink_admitted(self, tmp_path):
        root = self._dev_repo(tmp_path, self.WM)
        assert self._verdict(root, 7000, head=8000) == BudgetVerdict((), ())

    def test_over_watermark_growth_and_same_size_refused(self, tmp_path):
        root = self._dev_repo(tmp_path, self.WM)
        assert "6000" in self._verdict(root, 8001, head=8000).hard[0]
        assert "6000" in self._verdict(root, 8000, head=8000).hard[0]

    def test_over_watermark_without_head_refused(self, tmp_path):
        root = self._dev_repo(tmp_path, self.WM)
        assert len(self._verdict(root, 7000, head=None).hard) == 1

    def test_malformed_watermark_is_hard_entry(self, tmp_path):
        root = self._dev_repo(tmp_path, "## Watermark\n- Reason: x\n")
        v = self._verdict(root, 100)
        assert len(v.hard) == 1 and v.hard[0].startswith("\n  coordinator/CLAUDE.md: ")

    def test_empty_or_absent_post_bytes_skipped(self, tmp_path):
        root = self._dev_repo(tmp_path)
        assert budget_verdict(root, [self.SURFACE], lambda p: None, lambda p: None) == BudgetVerdict((), ())
        assert budget_verdict(root, [self.SURFACE], lambda p: b"", lambda p: None) == BudgetVerdict((), ())
