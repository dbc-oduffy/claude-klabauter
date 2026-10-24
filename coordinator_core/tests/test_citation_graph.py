"""coordinator_core/tests/test_citation_graph.py -- tests for
`coordinator_core.citation_graph`.

Ported from coordinator-content-repo `coordinator/tests/test_citation_graph.py`
(`docs/plans/2026-08-30-citation-integrity-tier-1.md`, chunk C1) per
`docs/plans/2026-09-18-doe-holds-no-scripts.md` chunk W2-C3.

Covers extraction (fenced-code skip, frontmatter `spec_backlink:` exemption,
structural-comment skip, directory-convention placeholder skip, markdown-link
label dedup) and the four resolution classes -- bare-basename (live /
ambiguous / cross_surface / rot), pathed (repo-root-relative, NOT joined
against the citing file's directory -- the inherited-bug regression this
module's Anti-scope names explicitly), and markdown-link (resolved relative
to the citing file). Also covers the seeded sampler's determinism and a
smoke test against claude-klabauter's own real `docs/wiki/` corpus, which lives in
`test_citation_graph_claude_klabauter_corpus.py`, so a wiring regression (e.g.
`TRACKED_EXTENSIONS` widened without updating resolution) is caught even
though this module's detailed behaviour is otherwise tested on synthetic
fixtures.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import citation_graph as cg


def test_extracts_bare_basename_and_pathed():
    text = "See `some-page.md` and also `docs/wiki/other-page.md` for detail."
    citations = cg.extract_citations(text, Path("citing.md"))
    kinds = {(c.raw_target, c.kind) for c in citations}
    assert ("some-page.md", "bare_basename") in kinds
    assert ("docs/wiki/other-page.md", "pathed") in kinds


def test_extracts_markdown_link():
    text = "See [the page](some/relative/target.md) for detail."
    citations = cg.extract_citations(text, Path("citing.md"))
    assert len(citations) == 1
    assert citations[0].kind == "markdown_link"
    assert citations[0].raw_target == "some/relative/target.md"


def test_skips_fenced_code_block():
    text = "\n".join(
        [
            "prose before `real-citation.md`",
            "```",
            "`fenced-mention.md` should not count",
            "```",
            "prose after",
        ]
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {"real-citation.md"}


def test_skips_frontmatter_spec_backlink():
    text = "\n".join(
        [
            "---",
            "spec_backlink: some-spec.md",
            "title: x",
            "---",
            "body cites `real.md`",
        ]
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {"real.md"}


def test_skips_structural_html_comment_marker():
    text = "\n".join(
        [
            "<!-- BEGIN marker-file.md synced -->",
            "prose cites `genuine.md` outside the comment",
        ]
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {"genuine.md"}


def test_skips_directory_convention_placeholder():
    text = "Write results under `archive/<queue>/<YYYY-MM>/report.md` as a convention."
    citations = cg.extract_citations(text, Path("citing.md"))
    assert citations == []


def test_skips_ellipsis_and_bang_placeholder_variants():
    text = "\n".join(
        [
            "See `docs/plans/...md` for the pattern.",
            "See `…/phase-2-5-judgment-mining.md` for the pattern.",
            "Exclude `!scratch/important.md` from the sweep.",
            "But `real-citation.md` still counts.",
        ]
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {"real-citation.md"}


def test_skips_bare_extension_mention():
    text = "An agent `.md` file or a `SKILL.md` file both qualify."
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {"SKILL.md"}


def test_markdown_link_label_not_double_counted():
    text = "Cross-link: see [`shared-page.md`](../../other-repo/docs/wiki/shared-page.md) here."
    citations = cg.extract_citations(text, Path("citing.md"))
    assert len(citations) == 1
    assert citations[0].kind == "markdown_link"
    assert citations[0].raw_target == "../../other-repo/docs/wiki/shared-page.md"


def test_skips_shell_command_text_carrying_md_token():
    text = (
        "3. The commit subject names the closed backlog entry; "
        "`git log -- state/bug-backlog.md` becomes the audit trail."
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    assert citations == []


def test_skips_shell_command_text_with_two_md_arguments():
    text = (
        '- `grep -n "split" coordinator/skills/sizing/SKILL.md '
        "coordinator/skills/plan/SKILL.md` -- no"
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    assert citations == []


def test_splits_two_filenames_concatenated_in_one_backtick_span():
    text = (
        "    E2. DELETE the pointer line "
        "`→ daily-branch-discipline.md, scoped-safety-commits.md` under"
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {"daily-branch-discipline.md", "scoped-safety-commits.md"}
    assert all(c.kind == "bare_basename" for c in citations)


def test_single_target_span_unaffected_by_concatenation_split():
    text = "See `some-page.md` for detail."
    citations = cg.extract_citations(text, Path("citing.md"))
    assert len(citations) == 1
    assert citations[0].raw_target == "some-page.md"


def test_extracts_wikilink():
    text = (
        "See\n"
        "[[a-clean-tree-after-a-scoped-commit-is-not-a-divergence]],\n"
        "[[reading-a-file-on-a-shared-tree-tells-you-nothing-about-whose-it-is]],\n"
        "[[a-private-index-commit-leaves-the-shared-index-claiming-your-files-are-deleted]]. Greppable\n"
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    targets = {c.raw_target for c in citations}
    assert targets == {
        "a-clean-tree-after-a-scoped-commit-is-not-a-divergence.md",
        "reading-a-file-on-a-shared-tree-tells-you-nothing-about-whose-it-is.md",
        "a-private-index-commit-leaves-the-shared-index-claiming-your-files-are-deleted.md",
    }


def test_wikilink_normalizes_uppercase_greppable_token_slug():
    text = (
        "[[A_PLUGIN_RESOLVED_CLIS_OWN_REPO_IS_NOT_YOURS]] — same class, "
        "different face."
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    assert len(citations) == 1
    assert citations[0].raw_target == "a-plugin-resolved-clis-own-repo-is-not-yours.md"


def test_separatorless_double_bracket_token_is_not_a_wikilink():
    text = "a flat key after the last `[table]`/`[[array]]` header is table-scoped"
    assert cg.extract_citations(text, Path("citing.md")) == []

    kept = cg.extract_citations("see [[some-page]] for that", Path("citing.md"))
    assert [c.raw_target for c in kept] == ["some-page.md"]


def test_wikilink_skipped_in_fenced_code_block():
    text = "\n".join(
        [
            "```",
            "[[fenced-slug]] should not count",
            "```",
            "prose after",
        ]
    )
    citations = cg.extract_citations(text, Path("citing.md"))
    assert citations == []


def _citation(kind, raw_target, citing_file=Path("citing.md")):
    return cg.Citation(citing_file, 1, kind, raw_target, "excerpt")


def test_resolve_bare_basename_live():
    wiki_index = {"page.md": (Path("/wiki/page.md"),)}
    repo_index = {}
    verdict = cg.resolve_bare_basename(_citation("bare_basename", "page.md"), wiki_index, repo_index)
    assert verdict.status == "live"
    assert verdict.matches == (Path("/wiki/page.md"),)


def test_resolve_bare_basename_ambiguous():
    wiki_index = {"page.md": (Path("/wiki/a/page.md"), Path("/wiki/b/page.md"))}
    repo_index = {}
    verdict = cg.resolve_bare_basename(_citation("bare_basename", "page.md"), wiki_index, repo_index)
    assert verdict.status == "ambiguous"
    assert len(verdict.matches) == 2


def test_resolve_bare_basename_cross_surface():
    wiki_index = {}
    repo_index = {"plan.md": (Path("/docs/plans/plan.md"),)}
    verdict = cg.resolve_bare_basename(_citation("bare_basename", "plan.md"), wiki_index, repo_index)
    assert verdict.status == "cross_surface"
    assert verdict.matches == (Path("/docs/plans/plan.md"),)


def test_resolve_bare_basename_rot():
    verdict = cg.resolve_bare_basename(_citation("bare_basename", "nowhere.md"), {}, {})
    assert verdict.status == "rot"
    assert verdict.matches == ()


def test_resolve_pathed_is_repo_root_relative_not_dirname_joined(tmp_path):
    (tmp_path / "docs" / "wiki").mkdir(parents=True)
    target = tmp_path / "docs" / "wiki" / "target-page.md"
    target.write_text("content", encoding="utf-8")

    deep_citing_file = tmp_path / "coordinator" / "agents" / "deep" / "nested" / "citer.md"
    deep_citing_file.parent.mkdir(parents=True)
    deep_citing_file.write_text("citer", encoding="utf-8")

    citation = _citation("pathed", "docs/wiki/target-page.md", citing_file=deep_citing_file)
    verdict = cg.resolve_pathed(citation, roots=(tmp_path,))
    assert verdict.status == "live"
    assert verdict.matches == (target,)

    wrong_path = deep_citing_file.parent / "docs" / "wiki" / "target-page.md"
    assert not wrong_path.exists()


def test_resolve_pathed_rot_when_absent(tmp_path):
    citation = _citation("pathed", "docs/wiki/does-not-exist.md")
    verdict = cg.resolve_pathed(citation, roots=(tmp_path,))
    assert verdict.status == "rot"


def test_resolve_pathed_tries_plugin_root_before_repo_root(tmp_path):
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "docs" / "wiki").mkdir(parents=True)
    target = plugin_root / "docs" / "wiki" / "only-under-plugin-root.md"
    target.write_text("content", encoding="utf-8")

    citation = _citation("pathed", "docs/wiki/only-under-plugin-root.md")
    verdict = cg.resolve_pathed(citation, roots=(plugin_root, tmp_path))
    assert verdict.status == "live"
    assert verdict.matches == (target,)
    assert verdict.resolved_root == plugin_root


def test_resolve_pathed_falls_back_to_repo_root_when_absent_under_plugin_root(tmp_path):
    plugin_root = tmp_path / "coordinator"
    plugin_root.mkdir()
    target = tmp_path / "docs" / "wiki" / "only-under-repo-root.md"
    target.parent.mkdir(parents=True)
    target.write_text("content", encoding="utf-8")

    citation = _citation("pathed", "docs/wiki/only-under-repo-root.md")
    verdict = cg.resolve_pathed(citation, roots=(plugin_root, tmp_path))
    assert verdict.status == "live"
    assert verdict.matches == (target,)
    assert verdict.resolved_root == tmp_path


def test_resolve_pathed_rot_when_absent_under_every_root(tmp_path):
    plugin_root = tmp_path / "coordinator"
    plugin_root.mkdir()
    citation = _citation("pathed", "docs/wiki/nowhere.md")
    verdict = cg.resolve_pathed(citation, roots=(plugin_root, tmp_path))
    assert verdict.status == "rot"
    assert verdict.matches == ()


def test_resolve_pathed_home_relative_is_not_rot():
    citation = _citation("pathed", "~/.claude/CLAUDE.md")
    verdict = cg.resolve_pathed(citation, roots=())
    assert verdict.status == "home_relative"
    assert verdict.matches == ()


def test_resolve_pathed_portable_across_posix_and_windows_style_fixtures(tmp_path):
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "docs" / "wiki").mkdir(parents=True)
    target = plugin_root / "docs" / "wiki" / "portable-page.md"
    target.write_text("content", encoding="utf-8")

    posix_style = _citation("pathed", "docs/wiki/portable-page.md")
    windows_style = _citation("pathed", "docs\\wiki\\portable-page.md")

    posix_verdict = cg.resolve_pathed(posix_style, roots=(plugin_root, tmp_path))
    windows_verdict = cg.resolve_pathed(windows_style, roots=(plugin_root, tmp_path))

    assert posix_verdict.status == "live"
    assert posix_verdict.matches == (target,)
    import os

    if os.sep == "\\":
        assert windows_verdict.status == "live"
        assert windows_verdict.matches == (target,)
    else:
        assert windows_verdict.status == "rot"


def test_resolve_markdown_link_relative_to_citing_file(tmp_path):
    sibling_dir = tmp_path / "coordinator" / "docs" / "wiki"
    sibling_dir.mkdir(parents=True)
    target = sibling_dir / "linked.md"
    target.write_text("content", encoding="utf-8")
    citing_file = sibling_dir / "citer.md"
    citing_file.write_text("citer", encoding="utf-8")

    citation = _citation("markdown_link", "linked.md", citing_file=citing_file)
    verdict = cg.resolve_markdown_link(citation)
    assert verdict.status == "live"
    assert verdict.matches == (target,)


def test_resolve_markdown_link_dead(tmp_path):
    citing_file = tmp_path / "citer.md"
    citing_file.write_text("citer", encoding="utf-8")
    citation = _citation("markdown_link", "missing.md", citing_file=citing_file)
    verdict = cg.resolve_markdown_link(citation)
    assert verdict.status == "dead_link"


def test_resolve_citation_dispatches_by_kind():
    wiki_index = {"page.md": (Path("/wiki/page.md"),)}
    verdict = cg.resolve_citation(
        _citation("bare_basename", "page.md"), wiki_index, {}, roots=(Path("/repo"),)
    )
    assert verdict.status == "live"


def test_resolve_citation_rejects_unknown_kind():
    import pytest

    with pytest.raises(ValueError):
        cg.resolve_citation(_citation("mystery", "x.md"), {}, {})


def test_load_wiki_index_maps_basename_to_paths(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "a.md").write_text("a", encoding="utf-8")
    sub = wiki_root / "sub"
    sub.mkdir()
    (sub / "b.md").write_text("b", encoding="utf-8")

    index = cg.load_wiki_index(wiki_root)
    assert set(index.keys()) == {"a.md", "b.md"}
    assert index["a.md"] == (wiki_root / "a.md",)


def test_load_wiki_index_records_fold_collision_for_wikilink_lookup(tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "a_b.md").write_text("x", encoding="utf-8")
    (wiki_root / "a-b.md").write_text("x", encoding="utf-8")

    index = cg.load_wiki_index(wiki_root)

    assert index["a_b.md"] == (wiki_root / "a_b.md",)
    assert index["a-b.md"] == (wiki_root / "a-b.md",)
    fold_matches = index[cg._WIKI_FOLD_COLLISION_PREFIX + "a-b.md"]
    assert set(fold_matches) == {wiki_root / "a_b.md", wiki_root / "a-b.md"}


def test_resolve_bare_basename_wikilink_fold_collision_is_ambiguous():
    wiki_index = {
        "a-b.md": (Path("/wiki/a-b.md"),),
        "a_b.md": (Path("/wiki/a_b.md"),),
        cg._WIKI_FOLD_COLLISION_PREFIX + "a-b.md": (
            Path("/wiki/a_b.md"),
            Path("/wiki/a-b.md"),
        ),
    }
    citation = cg.Citation(Path("citing.md"), 1, "bare_basename", "a-b.md", "excerpt", is_wikilink=True)

    verdict = cg.resolve_bare_basename(citation, wiki_index, {})

    assert verdict.status == "ambiguous"
    assert len(verdict.matches) == 2


def test_resolve_bare_basename_exact_backtick_citation_unaffected_by_fold_collision():
    wiki_index = {
        "a-b.md": (Path("/wiki/a-b.md"),),
        "a_b.md": (Path("/wiki/a_b.md"),),
        cg._WIKI_FOLD_COLLISION_PREFIX + "a-b.md": (
            Path("/wiki/a_b.md"),
            Path("/wiki/a-b.md"),
        ),
    }
    citation = _citation("bare_basename", "a-b.md")

    verdict = cg.resolve_bare_basename(citation, wiki_index, {})

    assert verdict.status == "live"
    assert verdict.matches == (Path("/wiki/a-b.md"),)


def test_load_repo_index_excludes_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.md").write_text("x", encoding="utf-8")
    (tmp_path / "real.md").write_text("x", encoding="utf-8")

    index = cg.load_repo_index(tmp_path)
    assert "ignored.md" not in index
    assert "real.md" in index


def test_scan_corpus_end_to_end(tmp_path):
    wiki_root = tmp_path / "coordinator" / "docs" / "wiki"
    wiki_root.mkdir(parents=True)
    (wiki_root / "target.md").write_text("the target page", encoding="utf-8")
    (wiki_root / "citer.md").write_text(
        "See `target.md` and `missing-page.md` and [link](target.md).",
        encoding="utf-8",
    )

    report = cg.scan_corpus(wiki_root=wiki_root, repo_root=tmp_path)
    assert report.wiki_file_count == 2
    counts = report.counts()
    assert counts.get("live", 0) == 2
    assert counts.get("rot", 0) == 1


def _make_verdicts(n):
    return [
        cg.Verdict(_citation("bare_basename", f"page-{i}.md"), "rot")
        for i in range(n)
    ]


def test_sample_verdicts_is_deterministic_for_same_seed():
    verdicts = _make_verdicts(50)
    sample_a = cg.sample_verdicts(verdicts, seed=42, sample_size=10)
    sample_b = cg.sample_verdicts(verdicts, seed=42, sample_size=10)
    assert [v.citation.raw_target for v in sample_a] == [v.citation.raw_target for v in sample_b]
    assert len(sample_a) == 10


def test_sample_verdicts_different_seeds_can_differ():
    verdicts = _make_verdicts(50)
    sample_a = cg.sample_verdicts(verdicts, seed=1, sample_size=10)
    sample_b = cg.sample_verdicts(verdicts, seed=2, sample_size=10)
    targets_a = [v.citation.raw_target for v in sample_a]
    targets_b = [v.citation.raw_target for v in sample_b]
    assert targets_a != targets_b


def test_sample_verdicts_size_exceeds_population_returns_all():
    verdicts = _make_verdicts(5)
    sample = cg.sample_verdicts(verdicts, seed=1, sample_size=100)
    assert len(sample) == 5


def test_sample_verdicts_order_independent_of_input_order():
    verdicts = _make_verdicts(20)
    reversed_verdicts = list(reversed(verdicts))
    sample_a = cg.sample_verdicts(verdicts, seed=7, sample_size=5)
    sample_b = cg.sample_verdicts(reversed_verdicts, seed=7, sample_size=5)
    assert [v.citation.raw_target for v in sample_a] == [v.citation.raw_target for v in sample_b]


def test_git_head_sha_returns_none_for_non_git_dir(tmp_path):
    assert cg.git_head_sha(tmp_path) is None


# --- tombstone resolution proof and bare_cite_scope narrowing ---------------


def _wr(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _tomb_fixture(tmp_path: Path, *, successor: bool = True) -> Path:
    wiki = tmp_path / "coordinator" / "docs" / "wiki"
    succ = "docs/wiki/coordinator-tripwires/tripwire-registry/README.md"
    _wr(
        wiki / "coordinator-tripwires" / "tripwire-registry.md",
        f"---\nstatus: tombstone\n---\n# `tripwire-registry.md` moved to `{succ}`\n",
    )
    if successor:
        _wr(wiki / "coordinator-tripwires" / "tripwire-registry" / "README.md", "# Registry\n")
    _wr(
        wiki / "citer.md",
        "See `tripwire-registry.md` and "
        "`docs/wiki/coordinator-tripwires/tripwire-registry.md`.\n",
    )
    return wiki


def test_tombstone_resolves_old_citations_and_successor_hop(tmp_path):
    wiki = _tomb_fixture(tmp_path)
    report = cg.scan_corpus(wiki_root=wiki, repo_root=tmp_path)
    tomb = wiki / "coordinator-tripwires" / "tripwire-registry.md"
    by_file = {}
    for v in report.verdicts:
        by_file.setdefault(v.citation.citing_file.name, []).append(v)
    assert all(v.status == "live" for v in by_file["citer.md"])
    assert by_file["citer.md"][0].matches == (tomb,)
    hop = [v for v in by_file["tripwire-registry.md"] if v.citation.kind == "pathed"][0]
    assert hop.status == "live"
    assert hop.resolved_root is not None and tmp_path in hop.resolved_root.parents
    assert hop.matches[0].name == "README.md"


def test_dangling_tombstone_is_rot(tmp_path):
    wiki = _tomb_fixture(tmp_path, successor=False)
    report = cg.scan_corpus(wiki_root=wiki, repo_root=tmp_path)
    hop = [
        v
        for v in report.verdicts
        if v.citation.citing_file.name == "tripwire-registry.md" and v.citation.kind == "pathed"
    ]
    assert hop and hop[0].status == "rot"


def test_same_basename_tombstone_is_ambiguous(tmp_path):
    wiki = tmp_path / "coordinator" / "docs" / "wiki"
    _wr(wiki / "x.md", "---\nstatus: tombstone\n---\n# moved to `docs/wiki/sub/x.md`\n")
    _wr(wiki / "sub" / "x.md", "# x\n")
    _wr(wiki / "citer.md", "See `x.md`.\n")
    report = cg.scan_corpus(wiki_root=wiki, repo_root=tmp_path)
    assert [v.status for v in report.verdicts if v.citation.raw_target == "x.md"] == ["ambiguous"]


def test_verdict_status_values_unchanged():
    import typing

    hint = typing.get_type_hints(cg.Verdict).get("status")
    if hint is not None and getattr(hint, "__args__", None):
        assert len(hint.__args__) == 6


def _scope_fixture(tmp_path: Path, claims: "dict[str, str | None]", citer: str = "citer.md"):
    wiki = tmp_path / "wiki"
    for rel, scope in claims.items():
        fm = "" if scope is None else f"---\r\nbare_cite_scope: {scope}\r\n---\r\n"
        _wr(wiki / rel, fm + "# page\r\n")
    _wr(wiki / citer, "See `p.md`.\n")
    return wiki


def _status(wiki: Path, tmp_path: Path, with_notes: bool = True) -> "tuple[str, tuple]":
    idx = cg.load_wiki_index(wiki)
    notes = cg.load_disambiguation_notes(idx, wiki) if with_notes else None
    cit = [c for c in cg.extract_citations((wiki / "citer.md").read_text(), wiki / "citer.md")]
    v = cg.resolve_bare_basename(cit[0], idx, {}, notes)
    return v.status, v.matches


def test_dot_scope_narrows_crlf_page(tmp_path):
    wiki = _scope_fixture(tmp_path, {"p.md": ".", "sub/p.md": None})
    status, matches = _status(wiki, tmp_path)
    assert status == "live" and matches == (wiki / "p.md",)


def test_notes_none_stays_ambiguous(tmp_path):
    wiki = _scope_fixture(tmp_path, {"p.md": ".", "sub/p.md": None})
    assert _status(wiki, tmp_path, with_notes=False)[0] == "ambiguous"


def test_deeper_claim_wins_and_shallower_for_outsider(tmp_path):
    wiki = _scope_fixture(tmp_path, {"p.md": ".", "sub/p.md": "sub/"}, citer="sub/citer.md")
    (wiki / "citer.md").write_text("See `p.md`.\n")
    idx = cg.load_wiki_index(wiki)
    notes = cg.load_disambiguation_notes(idx, wiki)
    inside = cg.extract_citations("`p.md`", wiki / "sub" / "citer.md")[0]
    outside = cg.extract_citations("`p.md`", wiki / "citer.md")[0]
    assert cg.resolve_bare_basename(inside, idx, {}, notes).matches == (wiki / "sub" / "p.md",)
    assert cg.resolve_bare_basename(outside, idx, {}, notes).matches == (wiki / "p.md",)


def test_equal_depth_and_no_claims_stay_ambiguous(tmp_path):
    wiki = _scope_fixture(tmp_path, {"a/p.md": ".", "b/p.md": "."})
    assert _status(wiki, tmp_path)[0] == "ambiguous"
    wiki2 = _scope_fixture(tmp_path / "n", {"a/p.md": None, "b/p.md": None})
    assert _status(wiki2, tmp_path)[0] == "ambiguous"


def test_note_on_unambiguous_basename_is_unread(tmp_path):
    wiki = _scope_fixture(tmp_path, {"p.md": "."})
    assert cg.load_disambiguation_notes(cg.load_wiki_index(wiki), wiki) == {}
