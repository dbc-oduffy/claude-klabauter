"""Tests for the `moved` verdict and the anchor pass of
`coordinator_core.citation_graph`: fragment links and `§ Heading` citations
over file-live verdicts, on tmp_path fixture corpora only."""

from __future__ import annotations

from pathlib import Path

import coordinator_core.citation_graph as cg


def _corpus(tmp_path: Path, files: "dict[str, str]") -> cg.CorpusReport:
    wiki = tmp_path / "coordinator" / "docs" / "wiki"
    for rel, body in files.items():
        p = wiki / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode("utf-8"))
    return cg.scan_corpus(wiki_root=wiki, repo_root=tmp_path)


def _anchors(report):
    return {(a.anchor, a.grammar, a.status) for a in cg.scan_anchors(report)}


TARGET = "# Title\n\n## Claim at pickup\n\n## Run-Report Sidecar\n"


def test_fragment_link_missing_and_live(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "target.md": TARGET,
            "a.md": "[x](target.md#run-report-sidecar) and [y](target.md#gone)\n",
        },
    )
    got = _anchors(report)
    assert ("run-report-sidecar", "fragment", "anchor_live") in got
    assert ("gone", "fragment", "anchor_missing") in got


def test_section_renamed_heading_flagged_existing_not(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "target.md": TARGET,
            "a.md": "See `target.md` § Run-Report Sidecar and `target.md` § Flight-Recorder Sidecar\n",
        },
    )
    got = _anchors(report)
    assert ("Run-Report Sidecar", "section", "anchor_live") in got
    assert ("Flight-Recorder Sidecar", "section", "anchor_missing") in got


def test_comma_listed_second_section_flagged(tmp_path):
    report = _corpus(
        tmp_path,
        {"target.md": TARGET, "a.md": "`target.md` § Claim at pickup, § Nonexistent thing\n"},
    )
    got = _anchors(report)
    assert ("Claim at pickup", "section", "anchor_live") in got
    assert ("Nonexistent thing", "section", "anchor_missing") in got


def test_heading_only_in_fence_counts_absent(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "target.md": "# T\n\n```\n## Hidden heading\n```\n",
            "a.md": "`target.md` § Hidden heading\n",
        },
    )
    assert ("Hidden heading", "section", "anchor_missing") in _anchors(report)


def test_fenced_citation_not_scanned(tmp_path):
    report = _corpus(
        tmp_path,
        {"target.md": TARGET, "a.md": "```\n`target.md` § Nope nope\n```\n`target.md`\n"},
    )
    assert _anchors(report) == set()


def test_non_live_file_not_evaluated(tmp_path):
    report = _corpus(
        tmp_path,
        {"a.md": "`missing-page.md` § Whatever heading\n[z](gone.md#frag)\n"},
    )
    assert cg.scan_anchors(report) == ()


def test_crlf_target_headings(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "target.md": "# T\r\n\r\n## Crlf Heading\r\n",
            "a.md": "`target.md` § Crlf Heading\r\n[x](target.md#crlf-heading)\r\n",
        },
    )
    assert {a.status for a in cg.scan_anchors(report)} == {"anchor_live"}
    assert len(cg.scan_anchors(report)) == 2


def test_id_like_ref_skipped_and_number_matches(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "target.md": "# T\n\n## 3. Wave gate\n",
            "a.md": "`target.md` § C2 and `target.md` § 3\n",
        },
    )
    assert _anchors(report) == {("3", "section", "anchor_live")}


def test_quoted_and_bold_lead_in(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "target.md": "# T\n\n- **Lead in phrase.** body\n",
            "a.md": '`target.md` § "Lead in phrase" and `target.md` § "Other"\n',
        },
    )
    got = _anchors(report)
    assert ("Lead in phrase", "section", "anchor_live") in got
    assert ("Other", "section", "anchor_missing") in got


def test_anchor_pass_leaves_corpus_report_untouched(tmp_path):
    files = {
        "target.md": TARGET,
        "a.md": "`target.md` § Gone heading here\n[x](target.md#nope)\n`nothere.md`\n",
    }
    report = _corpus(tmp_path, files)
    before_counts = report.counts()
    cg.scan_anchors(report)
    assert report.counts() == before_counts
    assert _corpus(tmp_path, files).counts() == before_counts


def test_pathed_wiki_citation_moved_when_one_basename_match(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "new/renamed.md": "# R\n",
            "a.md": "See `docs/wiki/old/renamed.md`.\n",
        },
    )
    assert report.counts().get("moved") == 1
    assert report.counts().get("rot", 0) == 0


def test_pathed_wiki_citation_ambiguous_when_several_basename_matches(tmp_path):
    report = _corpus(
        tmp_path,
        {
            "x/renamed.md": "# R\n",
            "y/renamed.md": "# R\n",
            "a.md": "See `docs/wiki/old/renamed.md`.\n",
        },
    )
    assert report.counts().get("ambiguous") == 1


def test_pathed_citation_outside_wiki_prefix_stays_rot(tmp_path):
    report = _corpus(
        tmp_path,
        {"new/renamed.md": "# R\n", "a.md": "See `docs/plans/renamed.md`.\n"},
    )
    assert report.counts().get("rot") == 1
    assert report.counts().get("moved", 0) == 0
