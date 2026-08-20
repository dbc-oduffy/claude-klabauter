"""
coordinator_core.ops.session.tests.test_fix_concrete_path_citations

Coverage:
  (a) A posix-home / drive-letter hit resolving to a known machine-local
      family is classified SUBSTITUTE with the repo-qualified replacement.
  (b) A line already carrying `abs-path-ok:`/`foreign-path-ok: <reason>` is
      classified MARKER and left untouched.
  (c) An unc / mixed-separators hit with no mapped family is classified
      REPORT-ONLY and left untouched.
  (d) A test-file / code-extension hit is REPORT-ONLY even when a family
      would otherwise match (correctness limit).
  (d2) A hit in RECORDED rather than authored content -- a `.diff`/`.patch`
      body, a `state/review-trail/**` evidence artifact, a
      `state/subagent-share/**` sidecar -- is REPORT-ONLY even when a family
      matches, because rewriting a record falsifies it; an ordinary authored
      `.md` on the same content still classifies SUBSTITUTE, so the carve-out
      is pinned as narrow rather than as a blanket opt-out.
  (e) `sweep(..., apply=True)` only rewrites SUBSTITUTE hits, preserves CRLF
      line endings exactly, and is idempotent (a second `--apply` pass finds
      zero SUBSTITUTE findings and rewrites zero files) -- exercised across
      all four detection rule shapes and both family categories.
  (f) `discover_families` builds repo/publish_mirror families from an
      injected registry-key lister, longest-match_name-first (so
      `example-retrieval-repo-ue-addon` is tried before the strict-prefix `example-retrieval-repo`).
  (g) A family name that is a normalized PREFIX of an unrelated folder (e.g.
      `claude-klabauter` inside `claude-klabauter-backup-2026`) never matches --
      a family match must fill an entire path segment, not merely appear as
      a substring.
  (h) `apply=False` (the CLI default) never writes to disk, and its reported
      SUBSTITUTE findings/replacements match what an `apply=True` pass on
      the same content would actually do.
  (i) A line carrying a valid `abs-path-ok:`/`foreign-path-ok: <reason>`
      marker is exempted through the full `sweep()` path (not just
      `classify()` in isolation), in both dry-run and apply mode, for both
      spellings; a BARE marker with no reason text does not exempt.
  (j) A live-doctrine file whose offending line also reads like incident
      evidence classifies MARKER (never guessed at); the same line on the
      same live-doctrine prefix with no incident wording classifies
      SUBSTITUTE -- pinning precedence, not just the leaf case.
  (k) `classify()`'s branch order is pinned (via `finding.reason`, not just
      `finding.outcome`) for every adjacent pair of conditions that can be
      simultaneously true: marker-vs-test-file, marker-vs-live-doctrine,
      test-file-vs-code-extension, test-file-vs-live-doctrine,
      code-extension-vs-live-doctrine.
  (l) The fixer's raw detection (`_raw_hits_in_line`) agrees with
      `guard_concrete_path_citations.detect_in_text` on rule-set and
      marked-exemption, over a shared fixture table -- so a future guard
      change this module doesn't learn about fails loudly here instead of
      silently leaving findings unfixable.
  (m) `main()` rejects an unrecognized `--only` family id instead of
      silently zeroing every substitution, and warns on stderr when family
      discovery finds zero repo/publish_mirror families (a degraded
      registry read must never read as "corpus is clean").
  (n) `_default_registry_keys` reads the machine-local registry TOML
      in-process -- it never execs the extensionless `machine-local` CLI
      shim, which is unexecutable on Windows and silently degraded every
      Windows run to zero families -- and returns `[]` (never raises) on an
      absent or malformed registry; the `baseline` test-marker is anchored
      to a filename's stem-suffix position, not a bare substring.

Every offending literal below carries a same-line `abs-path-ok:` marker with
a reason so THIS file itself can be written past the live
`write_guards.guard_concrete_path_citations` guard -- see
`test_guard_concrete_path_citations.py`'s own docstring for why a synthetic
fixture full of offending paths is the marker's designed use case.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops.session.fix_concrete_path_citations as fix_mod
import coordinator_core.ops.session.guard_concrete_path_citations as guard_mod
from coordinator_core.ops.session.fix_concrete_path_citations import (
    MARKER,
    REPORT_ONLY,
    SUBSTITUTE,
    _default_registry_keys,
    _is_test_file,
    _raw_hits_in_line,
    _raw_hits_in_text,
    classify,
    discover_families,
    main,
    sweep,
)


def _fake_machine_local():
    return ["repos.claude_klabauter", "repos.example_retrieval_repo", "repos.example_retrieval_repo_ue_addon"]


def test_discover_families_longest_match_first_and_config_families() -> None:
    families = discover_families(keys=_fake_machine_local)
    ids = [f.id for f in families]
    assert ids.index("repo_example_retrieval_repo_ue_addon") < ids.index("repo_example_retrieval_repo")
    assert any(f.id == "claude_config_dir" and f.canonical == "${CLAUDE_HOME:-$HOME}/.claude" for f in families)
    assert any(f.id == "settings_home" for f in families)


_FAMILIES = discover_families(keys=_fake_machine_local)


def _list_files(files):
    def _inner(_root):
        return list(files)

    return _inner


def test_substitute_for_mapped_repo_family(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    target.write_text(
        "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py for details\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    result = sweep(tmp_path, _FAMILIES, apply=True, list_files=_list_files(["doc.md"]))
    subs = [f for f in result.findings if f.outcome == SUBSTITUTE]
    assert len(subs) == 1
    assert subs[0].replacement == "claude-klabauter:coordinator/foo.py"
    assert "claude-klabauter:coordinator/foo.py" in target.read_text(encoding="utf-8")


def test_marker_line_untouched() -> None:
    tmp = "line: X:\\claude-klabauter\\coordinator foreign-path-ok: documented incident evidence\n"  # abs-path-ok: synthetic test fixture
    # Exercised directly against the lower-level hit scan/classifier rather
    # than through sweep()+a real file, mirroring how
    # test_guard_concrete_path_citations exercises detect_in_text directly.
    hits = _raw_hits_in_text(tmp, "marked.md")
    assert hits, "fixture line should produce a raw hit"
    findings = [classify(h, tmp, _FAMILIES) for h in hits]
    assert all(f.outcome == MARKER for f in findings)


def test_report_only_for_unmapped_family(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    target.write_text(
        "third-operator path: /Users/thislaptop/Code_Projects/whatever\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = target.read_bytes()
    result = sweep(tmp_path, _FAMILIES, apply=True, list_files=_list_files(["doc.md"]))
    assert all(f.outcome == REPORT_ONLY for f in result.findings)
    assert result.files_rewritten == []
    assert target.read_bytes() == before


def test_report_only_for_test_and_code_files_even_when_mapped(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    target = tmp_path / "tests" / "test_thing.py"
    target.write_text(
        "PATH = '/Users/example-operator/X/claude-klabauter/coordinator'\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = target.read_bytes()
    result = sweep(
        tmp_path, _FAMILIES, apply=True, list_files=_list_files(["tests/test_thing.py"])
    )
    assert result.findings and all(f.outcome == REPORT_ONLY for f in result.findings)
    assert target.read_bytes() == before


_RECORDED_CASES = [
    ("captured.diff", "captured diff body"),
    ("state/review-trail/diffs/write-guards.patch", "review-trail patch"),
    ("state/review-trail/2026-08-03/finding.md", "review-trail evidence markdown"),
    ("state/subagent-share/abc123/reviewer.md", "agent share sidecar"),
]


@pytest.mark.parametrize("rel,label", _RECORDED_CASES, ids=[c[0] for c in _RECORDED_CASES])
def test_report_only_for_recorded_content_even_when_mapped(
    tmp_path: Path, rel: str, label: str
) -> None:
    """Recorded content is quoted, not authored -- a rewrite falsifies the
    record (and, in a diff, can break the hunk). Exercised end-to-end through
    `sweep(apply=True)` so the assertion is "the bytes on disk did not
    change", not merely "classify returned a string"."""
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "-see /Users/example-operator/X/claude-klabauter/coordinator/foo.py\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = target.read_bytes()
    result = sweep(tmp_path, _FAMILIES, apply=True, list_files=_list_files([rel]))
    assert result.findings, f"{label}: fixture should produce a hit"
    assert all(f.outcome == REPORT_ONLY for f in result.findings), (
        f"{label}: recorded content must never be substituted"
    )
    assert all("recorded content" in f.reason for f in result.findings)
    assert result.files_rewritten == []
    assert target.read_bytes() == before


def test_authored_markdown_with_same_content_still_substitutes(tmp_path: Path) -> None:
    """Negative half of the recorded-content carve-out: the identical line in
    an ordinary authored `.md` is still fixed, pinning the carve-out as
    narrow rather than as a blanket "any markdown is quoted" opt-out."""
    target = tmp_path / "docs" / "note.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "-see /Users/example-operator/X/claude-klabauter/coordinator/foo.py\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    result = sweep(tmp_path, _FAMILIES, apply=True, list_files=_list_files(["docs/note.md"]))
    subs = [f for f in result.findings if f.outcome == SUBSTITUTE]
    assert len(subs) == 1
    assert subs[0].replacement == "claude-klabauter:coordinator/foo.py"
    assert "claude-klabauter:coordinator/foo.py" in target.read_text(encoding="utf-8")


def test_recorded_content_prefix_matches_a_nested_checkout(tmp_path: Path) -> None:
    """The prefix test is `/`-anchored, not root-anchored, so a review-trail
    tree inside a nested checkout is carved out identically."""
    rel = "sibling-repo/state/review-trail/notes.md"
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "recorded: /Users/example-operator/X/claude-klabauter/coordinator/foo.py\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = target.read_bytes()
    result = sweep(tmp_path, _FAMILIES, apply=True, list_files=_list_files([rel]))
    assert result.findings and all(f.outcome == REPORT_ONLY for f in result.findings)
    assert target.read_bytes() == before


def test_branch_precedence_marker_beats_recorded_content() -> None:
    line = "recorded /Users/example-operator/X/claude-klabauter/coordinator abs-path-ok: adjudicated\n"  # abs-path-ok: synthetic test fixture
    hits = _raw_hits_in_text(line, "state/review-trail/x.md")
    assert hits
    findings = [classify(h, line, _FAMILIES) for h in hits]
    assert all(f.outcome == MARKER for f in findings)


def test_apply_preserves_crlf_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "dirty.md"
    target.write_bytes(
        b"line one\r\n"
        b"some path /Users/example-operator/X/claude-klabauter/coordinator here\r\n"  # abs-path-ok: synthetic test fixture
    )
    files = _list_files(["dirty.md"])

    first = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert first.files_rewritten == ["dirty.md"]
    after_first = target.read_bytes()
    assert after_first.count(b"\r\n") == 2
    assert b"\n" not in after_first.replace(b"\r\n", b"")

    second = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert second.files_rewritten == []
    assert not [f for f in second.findings if f.outcome == SUBSTITUTE]
    assert target.read_bytes() == after_first


def test_only_family_restricts_apply(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    target.write_text(
        "a: /Users/example-operator/X/claude-klabauter/coordinator/foo\n"  # abs-path-ok: synthetic test fixture
        "b: X:\\example-retrieval-repo\\addon\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    result = sweep(
        tmp_path,
        _FAMILIES,
        only_family="repo_claude_klabauter",
        apply=True,
        list_files=_list_files(["doc.md"]),
    )
    text = target.read_text(encoding="utf-8")
    assert "claude-klabauter:coordinator/foo" in text
    assert "X:\\example-retrieval-repo\\addon" in text  # abs-path-ok: synthetic test fixture -- unmodified because --only excluded it


# ---------------------------------------------------------------------------
# Finding 1 -- segment-boundary family matching
# ---------------------------------------------------------------------------


def test_family_match_requires_full_segment_not_prefix_substring(tmp_path: Path) -> None:
    target = tmp_path / "doc.md"
    target.write_text(
        "backup lives at /Users/example-operator/X/claude-klabauter-backup-2026/notes.md\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = target.read_bytes()
    result = sweep(tmp_path, _FAMILIES, apply=True, list_files=_list_files(["doc.md"]))
    # "claude-klabauter" is a normalized substring of "claude-klabauter-backup-2026"
    # but not an EQUAL path segment -- must not match the claude-klabauter
    # family and must not be rewritten.
    assert all(f.outcome == REPORT_ONLY for f in result.findings)
    assert result.files_rewritten == []
    assert target.read_bytes() == before


def test_family_match_still_finds_the_real_segment_elsewhere_in_token(tmp_path: Path) -> None:
    # Companion to the prefix-collision case above: a single-letter segment
    # that is genuinely its own path component must still be found, even
    # though "a" is trivially a normalized substring of "example-operator" earlier in
    # the same token.
    families = discover_families(keys=_fake_machine_local_single_letter)
    target = tmp_path / "doc.md"
    target.write_text(
        "see /Users/example-operator/a/deep/sub/path.py here\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    result = sweep(tmp_path, families, apply=True, list_files=_list_files(["doc.md"]))
    subs = [f for f in result.findings if f.outcome == SUBSTITUTE]
    assert len(subs) == 1
    assert subs[0].replacement == "a:deep/sub/path.py"


# ---------------------------------------------------------------------------
# Finding 2 -- dry-run (apply=False) coverage
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_matches_what_apply_would_do(tmp_path: Path) -> None:
    content = "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py for details\n"  # abs-path-ok: synthetic test fixture

    dry_dir = tmp_path / "dry"
    dry_dir.mkdir()
    (dry_dir / "doc.md").write_text(content, encoding="utf-8")
    before = (dry_dir / "doc.md").read_bytes()

    dry = sweep(dry_dir, _FAMILIES, apply=False, list_files=_list_files(["doc.md"]))
    assert (dry_dir / "doc.md").read_bytes() == before, "dry-run must never write to disk"
    assert dry.files_rewritten == []
    assert dry.files_matched == ["doc.md"]

    apply_dir = tmp_path / "apply"
    apply_dir.mkdir()
    (apply_dir / "doc.md").write_text(content, encoding="utf-8")
    applied = sweep(apply_dir, _FAMILIES, apply=True, list_files=_list_files(["doc.md"]))
    assert applied.files_rewritten == ["doc.md"]

    dry_subs = [f for f in dry.findings if f.outcome == SUBSTITUTE]
    applied_subs = [f for f in applied.findings if f.outcome == SUBSTITUTE]
    assert len(dry_subs) == len(applied_subs) == 1
    assert dry_subs[0].replacement == applied_subs[0].replacement == "claude-klabauter:coordinator/foo.py"


# ---------------------------------------------------------------------------
# Finding 3 -- marker branches, exercised through sweep(), both spellings,
# both dry-run and apply, bare vs reasoned.
# ---------------------------------------------------------------------------


def test_marker_with_reason_both_spellings_exempt_via_sweep(tmp_path: Path) -> None:
    abs_target = tmp_path / "abs.md"
    abs_target.write_text(
        "path /Users/example-operator/X/claude-klabauter/coordinator/foo.py abs-path-ok: documented incident evidence\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    foreign_target = tmp_path / "foreign.md"
    foreign_target.write_text(
        "path /Users/example-operator/X/claude-klabauter/coordinator/foo.py foreign-path-ok: documented incident evidence\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before_abs = abs_target.read_bytes()
    before_foreign = foreign_target.read_bytes()

    for apply in (False, True):
        result = sweep(
            tmp_path, _FAMILIES, apply=apply, list_files=_list_files(["abs.md", "foreign.md"])
        )
        assert result.findings
        assert all(f.outcome == MARKER for f in result.findings)
        assert result.files_rewritten == []

    assert abs_target.read_bytes() == before_abs
    assert foreign_target.read_bytes() == before_foreign


def test_bare_marker_does_not_exempt(tmp_path: Path) -> None:
    # The fixture STRING content (not the surrounding python source line)
    # is what sweep() reads from disk -- it ends in a bare "abs-path-ok:"
    # with no reason text, which must NOT exempt it.
    content = "path here: /Users/example-operator/X/claude-klabauter/coordinator/foo.py abs-path-ok:\n"  # abs-path-ok: synthetic test fixture -- bare marker in fixture text must not exempt

    dry_dir = tmp_path / "dry"
    dry_dir.mkdir()
    (dry_dir / "doc.md").write_text(content, encoding="utf-8")
    # Snapshot the ON-DISK bytes, exactly as every other dry-run assertion in
    # this module does. `write_text` without `newline=""` applies Windows
    # newline translation, so `content.encode()` is not what landed on disk --
    # comparing against it fails on Windows for a reason that has nothing to
    # do with what this test is about.
    before = (dry_dir / "doc.md").read_bytes()
    dry = sweep(dry_dir, _FAMILIES, apply=False, list_files=_list_files(["doc.md"]))
    dry_subs = [f for f in dry.findings if f.outcome == SUBSTITUTE]
    assert len(dry_subs) == 1
    assert dry.files_rewritten == []
    assert (dry_dir / "doc.md").read_bytes() == before

    apply_dir = tmp_path / "apply"
    apply_dir.mkdir()
    (apply_dir / "doc.md").write_text(content, encoding="utf-8")
    applied = sweep(apply_dir, _FAMILIES, apply=True, list_files=_list_files(["doc.md"]))
    applied_subs = [f for f in applied.findings if f.outcome == SUBSTITUTE]
    assert len(applied_subs) == 1
    assert applied.files_rewritten == ["doc.md"]


def test_marker_for_live_doctrine_incident_evidence(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "coordinator" / "docs" / "wiki"
    wiki_dir.mkdir(parents=True)
    target = wiki_dir / "some-incident.md"
    target.write_text(
        "the corrupted path was /Users/example-operator/X/claude-klabauter/coordinator/foo.py\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = target.read_bytes()
    result = sweep(
        tmp_path,
        _FAMILIES,
        apply=True,
        list_files=_list_files(["coordinator/docs/wiki/some-incident.md"]),
    )
    assert result.findings
    assert all(f.outcome == MARKER for f in result.findings)
    assert all("incident evidence" in f.reason for f in result.findings)
    assert target.read_bytes() == before


def test_substitute_for_live_doctrine_without_incident_words(tmp_path: Path) -> None:
    wiki_dir = tmp_path / "coordinator" / "docs" / "wiki"
    wiki_dir.mkdir(parents=True)
    target = wiki_dir / "some-reference.md"
    target.write_text(
        "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py for details\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    result = sweep(
        tmp_path,
        _FAMILIES,
        apply=True,
        list_files=_list_files(["coordinator/docs/wiki/some-reference.md"]),
    )
    subs = [f for f in result.findings if f.outcome == SUBSTITUTE]
    assert len(subs) == 1
    assert subs[0].replacement == "claude-klabauter:coordinator/foo.py"


# ---------------------------------------------------------------------------
# Finding 4 -- classify() branch precedence, pinned via `reason`
# ---------------------------------------------------------------------------


def test_branch_precedence_test_file_beats_code_extension() -> None:
    line = "PATH = '/Users/example-operator/X/claude-klabauter/coordinator'\n"  # abs-path-ok: synthetic test fixture
    hits = _raw_hits_in_text(line, "test_thing.py")
    assert hits
    findings = [classify(h, line, _FAMILIES) for h in hits]
    assert all(f.outcome == REPORT_ONLY for f in findings)
    assert all("test/fixture file" in f.reason for f in findings)


def test_branch_precedence_marker_beats_test_file() -> None:
    line = "PATH = '/Users/example-operator/X/claude-klabauter/coordinator' abs-path-ok: synthetic test fixture\n"  # abs-path-ok: synthetic test fixture
    hits = _raw_hits_in_text(line, "test_thing.py")
    assert hits
    findings = [classify(h, line, _FAMILIES) for h in hits]
    assert all(f.outcome == MARKER for f in findings)
    assert all("already adjudicated" in f.reason for f in findings)


def test_branch_precedence_code_extension_beats_live_doctrine_incident() -> None:
    line = "path: /Users/example-operator/X/claude-klabauter/coordinator  # a corrupted path\n"  # abs-path-ok: synthetic test fixture
    hits = _raw_hits_in_text(line, "coordinator/skills/foo.yaml")
    assert hits
    findings = [classify(h, line, _FAMILIES) for h in hits]
    assert all(f.outcome == REPORT_ONLY for f in findings)
    assert all("executable/structured-config file" in f.reason for f in findings)


def test_branch_precedence_marker_beats_live_doctrine_incident() -> None:
    line = "the corrupted path was /Users/example-operator/X/claude-klabauter/coordinator abs-path-ok: documented incident evidence\n"  # abs-path-ok: synthetic test fixture
    hits = _raw_hits_in_text(line, "coordinator/docs/wiki/incident.md")
    assert hits
    findings = [classify(h, line, _FAMILIES) for h in hits]
    assert all(f.outcome == MARKER for f in findings)
    assert all("already adjudicated" in f.reason for f in findings)


def test_branch_precedence_test_file_beats_live_doctrine_incident() -> None:
    line = "the corrupted path was /Users/example-operator/X/claude-klabauter/coordinator\n"  # abs-path-ok: synthetic test fixture
    hits = _raw_hits_in_text(line, "coordinator/docs/wiki/tests/incident.md")
    assert hits
    findings = [classify(h, line, _FAMILIES) for h in hits]
    assert all(f.outcome == REPORT_ONLY for f in findings)
    assert all("test/fixture file" in f.reason for f in findings)


# ---------------------------------------------------------------------------
# Finding 5 -- idempotency across all four rule shapes and both family
# categories (repo/publish_mirror, config), plus the single-letter
# short_name edge case the docstring's claim depends on.
# ---------------------------------------------------------------------------


def _fake_machine_local_single_letter():
    return ["repos.a"]


def test_idempotency_config_families(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text(
        "claude home /Users/example-operator/.claude here, "  # abs-path-ok: synthetic test fixture
        "settings /Users/example-operator/.coordinator-claude-settings there\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    files = _list_files(["note.md"])
    first = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert first.files_rewritten == ["note.md"]
    subs = [f for f in first.findings if f.outcome == SUBSTITUTE]
    assert {s.replacement for s in subs} == {
        "${CLAUDE_HOME:-$HOME}/.claude",
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}",
    }
    after_first = target.read_bytes()

    second = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert second.files_rewritten == []
    assert not [f for f in second.findings if f.outcome == SUBSTITUTE]
    assert target.read_bytes() == after_first


def test_config_family_replacement_keeps_the_trailing_subpath(tmp_path: Path) -> None:
    """A config family's canonical text replaces the matched DIRECTORY, not
    the whole token. Returning the bare canonical for
    `<home>/.claude/settings.json` retargets the citation at the directory --
    a silent content change, not a path fix -- and `--apply` writes it."""
    target = tmp_path / "note.md"
    target.write_text(
        "config at /Users/example-operator/.claude/settings.json and "  # abs-path-ok: synthetic test fixture
        "shim /Users/example-operator/.coordinator-claude-settings/bin/machine-local\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    files = _list_files(["note.md"])
    first = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    subs = [f for f in first.findings if f.outcome == SUBSTITUTE]
    assert {s.replacement for s in subs} == {
        "${CLAUDE_HOME:-$HOME}/.claude/settings.json",
        "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/machine-local",
    }
    body = target.read_text(encoding="utf-8")
    assert "settings.json" in body
    assert "bin/machine-local" in body

    after_first = target.read_bytes()
    second = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert second.files_rewritten == []
    assert target.read_bytes() == after_first


def test_idempotency_drive_letter_and_mixed_separator_shapes(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    target.write_text(
        "drive form: X:\\claude-klabauter\\coordinator\\foo.py\n"  # abs-path-ok: synthetic test fixture
        "mixed form: X:\\example-retrieval-repo/addon\\bits\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    files = _list_files(["note.md"])
    first = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert first.files_rewritten == ["note.md"]
    after_first = target.read_bytes()

    second = sweep(tmp_path, _FAMILIES, apply=True, list_files=files)
    assert second.files_rewritten == []
    assert not [f for f in second.findings if f.outcome == SUBSTITUTE]
    assert target.read_bytes() == after_first


def test_idempotency_single_letter_short_name_does_not_relex_as_drive_letter(
    tmp_path: Path,
) -> None:
    # A repo/publish_mirror short_name of a single ASCII letter would, per
    # the code-reviewer's concern, produce a replacement of the shape
    # "x:rest" that could in principle be re-matched by WIN_DRIVE_RE
    # (`[A-Za-z]:[\\/]`). It doesn't in practice: `rest` always has its
    # leading separator stripped before the colon is appended, so the
    # character right after the colon is never `/` or `\`. Proven here
    # rather than merely reasoned about.
    families = discover_families(keys=_fake_machine_local_single_letter)
    target = tmp_path / "note.md"
    target.write_text(
        "see /Users/example-operator/a/deep/sub/path.py here\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    files = _list_files(["note.md"])
    first = sweep(tmp_path, families, apply=True, list_files=files)
    assert first.files_rewritten == ["note.md"]
    subs = [f for f in first.findings if f.outcome == SUBSTITUTE]
    assert len(subs) == 1
    assert subs[0].replacement == "a:deep/sub/path.py"
    after_first = target.read_bytes()

    second = sweep(tmp_path, families, apply=True, list_files=files)
    assert second.files_rewritten == []
    assert not [f for f in second.findings if f.outcome == SUBSTITUTE]
    assert target.read_bytes() == after_first


# ---------------------------------------------------------------------------
# Finding 6 -- detection parity with guard_concrete_path_citations
# ---------------------------------------------------------------------------


def test_detection_parity_with_guard() -> None:
    fixture_lines = [
        "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py for details",  # abs-path-ok: synthetic test fixture
        "see /Users/<username>/project for a placeholder segment",  # abs-path-ok: synthetic test fixture -- placeholder segment must not flag
        "root at X:\\claude-klabauter\\coordinator",  # abs-path-ok: synthetic test fixture
        "installed at C:\\Program Files\\Vendor\\tool.exe",  # abs-path-ok: synthetic test fixture -- well-known root must not flag
        "example root X:\\some-project\\...\\coordinator",  # abs-path-ok: synthetic test fixture -- ellipsis segment must not flag
        r"share \\fileserver\share is documentation",  # abs-path-ok: synthetic test fixture -- placeholder host must not flag
        r"share \\buildbox\artifacts is a real host",  # abs-path-ok: synthetic test fixture
        "mixed C:\\Users/example-operator\\claude-klabauter here",  # abs-path-ok: synthetic test fixture
        "plain https://example.com/not-a-drive-letter",  # abs-path-ok: synthetic test fixture -- URL scheme must not flag as drive-letter
        "nothing offending on this line at all",
    ]
    for line in fixture_lines:
        guard_rules = sorted({f.rule for f in guard_mod.detect_in_text(line, "parity.md")})
        fixer_rules = sorted({rule for rule, _token in fix_mod._raw_hits_in_line(line)})
        assert fixer_rules == guard_rules, f"detection drift on: {line!r}"


def test_detection_parity_marked_exemption() -> None:
    marked_line = "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py abs-path-ok: documented incident evidence"  # abs-path-ok: synthetic test fixture
    assert guard_mod.detect_in_text(marked_line, "parity.md") == []
    hits = _raw_hits_in_text(marked_line, "parity.md")
    assert hits
    assert all(h.marked for h in hits)
    assert sorted({h.rule for h in hits}) == sorted(
        {rule for rule, _token in _raw_hits_in_line(marked_line)}
    )


# ---------------------------------------------------------------------------
# Finding 7 -- degraded family discovery warns instead of reading as clean
# ---------------------------------------------------------------------------


def test_main_warns_when_family_discovery_finds_no_repo_families(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def _empty_machine_local():
        return []

    real_discover = fix_mod.discover_families
    monkeypatch.setattr(
        fix_mod, "discover_families", lambda: real_discover(keys=_empty_machine_local)
    )
    fix_mod.main(["--root", str(tmp_path), "--list-families"])
    captured = capsys.readouterr()
    assert "zero repo/publish_mirror families" in captured.err


def test_main_silent_when_family_discovery_finds_repo_families(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    fix_mod.main(["--root", str(tmp_path), "--list-families"])
    captured = capsys.readouterr()
    assert "zero repo/publish_mirror families" not in captured.err


# ---------------------------------------------------------------------------
# Finding 9 -- baseline test-marker anchored to filename suffix
# ---------------------------------------------------------------------------


def test_baseline_marker_anchored_to_filename_suffix() -> None:
    assert _is_test_file("state/posix-exec-baseline.json")
    assert _is_test_file("coordinator/tests/gendered_pronoun_baseline.json")
    assert not _is_test_file("coordinator/docs/wiki/new-performance-baseline-established.md")


# ---------------------------------------------------------------------------
# Finding 10 -- unknown --only family id errors instead of silently
# zeroing every substitution
# ---------------------------------------------------------------------------


def test_only_unknown_family_id_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    exit_code = fix_mod.main(
        ["--root", str(tmp_path), "--only", "totally-unknown-family", "--apply"]
    )
    assert exit_code != 0
    captured = capsys.readouterr()
    assert "totally-unknown-family" in captured.err


def test_only_known_family_id_does_not_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    monkeypatch.setattr(
        fix_mod,
        "sweep",
        lambda *a, **k: fix_mod.SweepResult(findings=[], files_rewritten=[], files_matched=[]),
    )
    exit_code = fix_mod.main(["--root", str(tmp_path), "--only", "repo_claude_klabauter"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "unknown --only" not in captured.err


# ---------------------------------------------------------------------------
# Finding 11 -- registry enumeration is an in-process TOML read, never a
# `machine-local` CLI exec. The CLI entry point is an extensionless POSIX
# shim; on Windows `subprocess.run` cannot exec it at all (WinError 193),
# and the caller's degrade-on-failure contract turned that into "zero repo
# families" -- every citation reported `no mapped family`, no file ever
# rewritten, on the platform this fleet commits from. Pinned three ways:
# the reader spawns nothing, it reads a real registry directory, and it
# degrades to `[]` rather than raising when there is nothing to read.
# ---------------------------------------------------------------------------


def test_default_registry_keys_never_spawns_a_subprocess(monkeypatch, tmp_path: Path) -> None:
    import subprocess as _subprocess

    def _explode(*_args, **_kwargs):  # pragma: no cover -- the assertion is that this never runs
        raise AssertionError("_default_registry_keys must not spawn a subprocess")

    monkeypatch.setattr(_subprocess, "run", _explode)
    monkeypatch.setattr(_subprocess, "Popen", _explode)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    assert _default_registry_keys() == []


def test_default_registry_keys_reads_registry_toml(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "registry.toml").write_text(
        '[repos]\nclaude_klabauter = "/some/where"\n\n'  # abs-path-ok: synthetic test fixture
        '[publish.mirrors.claude_klabauter]\npath = "/else/where"\n',  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    (tmp_path / "registry.local.toml").write_text(
        '[repos]\nexample_retrieval_repo = "/third/place"\n',  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    keys = _default_registry_keys()
    assert "repos.claude_klabauter" in keys
    assert "repos.example_retrieval_repo" in keys
    assert "publish.mirrors.claude_klabauter.path" in keys
    assert len(keys) == len(set(keys))

    ids = {f.id for f in discover_families(keys=_default_registry_keys)}
    assert "repo_claude_klabauter" in ids
    assert "repo_example_retrieval_repo" in ids
    assert "publish_mirror_claude_klabauter" in ids


def test_default_registry_keys_degrades_to_empty_on_malformed_registry(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "registry.toml").write_text("this is not = valid = toml [\n", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    assert _default_registry_keys() == []


# ---------------------------------------------------------------------------
# Finding 12 -- explicit-path CLI operation: sweep exactly the given files,
# tracked or not, git repo or not, so the guard's advisory remedy
# (`<fixer> --apply <the file just written>`) has something to invoke
# against an untracked file or a file outside any repo entirely.
# ---------------------------------------------------------------------------


def test_explicit_path_sweeps_untracked_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    target = tmp_path / "doc.md"
    target.write_text(
        "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py for details\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    exit_code = fix_mod.main(["--apply", str(target)])
    assert exit_code == 0
    assert "claude-klabauter:coordinator/foo.py" in target.read_text(encoding="utf-8")


def test_explicit_path_outside_any_git_repo_uses_parent_as_root(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    monkeypatch.setattr(fix_mod, "_git_toplevel_for", lambda _cwd: None)
    target = tmp_path / "doc.md"
    target.write_text(
        "see /Users/example-operator/X/claude-klabauter/coordinator/foo.py for details\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    exit_code = fix_mod.main(["--apply", str(target)])
    assert exit_code == 0
    assert "claude-klabauter:coordinator/foo.py" in target.read_text(encoding="utf-8")


def test_explicit_nonexistent_path_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    missing = tmp_path / "nope.md"
    exit_code = fix_mod.main([str(missing)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "not an existing file" in captured.err


def test_root_and_explicit_paths_together_exits_2(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    target = tmp_path / "doc.md"
    target.write_text("no findings here\n", encoding="utf-8")
    exit_code = fix_mod.main(["--root", str(tmp_path), str(target)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "--root" in captured.err


def test_explicit_path_grouping_preserves_recorded_content_classification(
    tmp_path: Path, monkeypatch
) -> None:
    """A file named explicitly still gets a MEANINGFUL rel path relative to
    its owning root, so `_is_recorded_content`'s `state/review-trail/`
    prefix check still fires and the file is not rewritten."""
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    monkeypatch.setattr(fix_mod, "_git_toplevel_for", lambda _cwd: tmp_path)
    recorded = tmp_path / "state" / "review-trail" / "diffs" / "write-guards.patch"
    recorded.parent.mkdir(parents=True)
    recorded.write_text(
        "-see /Users/example-operator/X/claude-klabauter/coordinator/foo.py\n",  # abs-path-ok: synthetic test fixture
        encoding="utf-8",
    )
    before = recorded.read_bytes()
    exit_code = fix_mod.main(["--apply", str(recorded)])
    assert exit_code == 0
    assert recorded.read_bytes() == before


def test_explicit_path_zero_positional_paths_still_runs_tracked_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(fix_mod, "discover_families", lambda: list(_FAMILIES))
    calls = []

    def _fake_sweep(root, families, only_family=None, apply=False, list_files=fix_mod._tracked_files):
        calls.append((root, list_files))
        return fix_mod.SweepResult(findings=[], files_rewritten=[], files_matched=[])

    monkeypatch.setattr(fix_mod, "sweep", _fake_sweep)
    exit_code = fix_mod.main(["--root", str(tmp_path)])
    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] is fix_mod._tracked_files
