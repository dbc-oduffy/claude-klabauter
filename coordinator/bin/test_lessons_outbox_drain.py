"""Fixture-based end-to-end test for lessons-outbox-drain.py.

Builds fake peer repos as plain temp directories (never a real peer repo — the whole point
of this test is that the drain can be exercised without touching sibling working trees).
`read`/`dedup_entries` operate purely on the filesystem and never shell out to git, so the
fixtures need no git state of their own.

Covers both surviving subcommands: read (+dedupe across two peers) and assert-empty
(peer-root enumeration monkeypatched via `drain.resolve_roots` — never a real peer repo,
never the real machine-local registry, never the real state/lessons-outbox/). The peer-
fetch/writeback/manifest model (`sync`, `write-manifest`, `writeback`, `record-outcome`)
this test used to also cover was retired from the script itself — see its module docstring
— once the central-write architecture made per-peer writeback dead weight; this test was
trimmed to match.

Converted from a hand-rolled runner (`lessons-outbox-drain.test.py`) to a pytest-collectable
module; the sequential fixture-building narrative is preserved as one function since later
steps depend on state built by earlier ones.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import importlib.util

spec = importlib.util.spec_from_file_location("lessons_outbox_drain", THIS_DIR / "lessons-outbox-drain.py")
drain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drain)


def _write_outbox_entry(peer: Path, filename: str, **fields) -> Path:
    outbox = peer / "state" / "lessons-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    p = outbox / filename
    body = ""
    for k, v in fields.items():
        if isinstance(v, list):
            body += f"{k}: {json.dumps(v)}\n"
        else:
            body += f'{k}: "{v}"\n'
    p.write_text(body, encoding="utf-8")
    return p


def test_lessons_outbox_drain(tmp_path: Path) -> None:
    tmp = tmp_path

    # --- fixture peers ---
    peer_a = tmp / "peer-a"
    peer_b = tmp / "peer-b"

    entry_a1 = _write_outbox_entry(
        peer_a, "2026-07-20T10-00-00-drain-branch-cut.yaml",
        id="a-uuid-1", created="2026-07-20T10:00:00Z", from_repo="peer-a",
        title="Drain-branch must cut from peer main", body="Body A1.",
        change_kind="wiki-append", target_wiki="docs/wiki/learn-lessons-routing.md",
    )
    entry_a2 = _write_outbox_entry(
        peer_a, "2026-07-20T11-00-00-unknown-target.yaml",
        id="a-uuid-2", created="2026-07-20T11:00:00Z", from_repo="peer-a",
        title="Something unresolved", body="Body A2.",
        change_kind="wiki-append", target_wiki="unknown",
    )

    # peer-b has a CONVERGING entry (same title/change_kind/target_wiki as entry_a1)
    entry_b1 = _write_outbox_entry(
        peer_b, "2026-07-20T09-30-00-drain-branch-cut.yaml",
        id="b-uuid-1", created="2026-07-20T09:30:00Z", from_repo="peer-b",
        title="Drain-branch must cut from peer main", body="Body B1 (converging).",
        change_kind="wiki-append", target_wiki="docs/wiki/learn-lessons-routing.md",
    )

    # --- read + dedupe across two peers ---
    entries_a, warn_a = drain.read_peer_outbox(peer_a)
    entries_b, warn_b = drain.read_peer_outbox(peer_b)
    assert len(entries_a) == 2, f"read_peer_outbox(peer_a) expected 2 entries, got {len(entries_a)}"
    assert len(entries_b) == 1, f"read_peer_outbox(peer_b) expected 1 entry, got {len(entries_b)}"

    merged, unknown = drain.dedup_entries(entries_a + entries_b)
    assert len(unknown) == 1 and unknown[0]["id"] == "a-uuid-2", (
        f"dedup unknown_target expected [a-uuid-2], got {unknown}"
    )
    assert len(merged) == 1, f"dedup merged expected 1 convergence-merged entry, got {merged}"
    sources = merged[0]["sources"]
    assert len(sources) == 2, f"dedup merged entry expected 2 sources (convergence), got {sources}"
    from_repos = sorted(s["from_repo"] for s in sources)
    assert from_repos == ["peer-a", "peer-b"], (
        f"dedup merged entry sources expected [peer-a, peer-b], got {from_repos}"
    )

    # --- dedup_entries: target_wiki suffix-spelling variance (A9) ---
    # Two entries, same (title, change_kind), one spelled with the `.md` suffix and
    # one without — the raw triple treats these as distinct; the canonicalized triple
    # must treat them as a convergence.
    entry_suffix_bare = {
        "id": "c-uuid-1", "created": "2026-07-22T10:00:00Z", "from_repo": "peer-c",
        "title": "Same lesson, bare target_wiki spelling", "body": "Body C1.",
        "change_kind": "wiki-append", "target_wiki": "concurrent-em-hazards",
        "_peer_path": "peer-c", "_filename": "c1.yaml",
    }
    entry_suffix_dotmd = {
        "id": "c-uuid-2", "created": "2026-07-22T10:05:00Z", "from_repo": "peer-d",
        "title": "Same lesson, bare target_wiki spelling", "body": "Body C2.",
        "change_kind": "wiki-append", "target_wiki": "concurrent-em-hazards.md",
        "_peer_path": "peer-d", "_filename": "c2.yaml",
    }
    merged_suffix, unknown_suffix = drain.dedup_entries([entry_suffix_bare, entry_suffix_dotmd])
    assert len(merged_suffix) == 1, (
        f"dedup_entries(target_wiki suffix variance) expected 1 merged entry (A9 fix), "
        f"got {len(merged_suffix)}: {merged_suffix}"
    )
    assert merged_suffix[0]["target_wiki"] == "concurrent-em-hazards", (
        "dedup_entries must NOT rewrite the stored target_wiki value as a side effect of "
        f"the comparison-only canonicalization, got {merged_suffix[0]['target_wiki']!r}"
    )
    assert len(merged_suffix[0]["sources"]) == 2, (
        f"dedup_entries(target_wiki suffix variance) expected 2 sources merged, "
        f"got {merged_suffix[0]['sources']}"
    )

    # --- dedup_entries: change_kind-gated canonicalization (A7/A9 tool-alignment fix) ---
    # Two wiki-append entries whose target_wiki differs only by directory-prefix
    # spelling ("test-design-discipline.md" vs "docs/wiki/test-design-discipline.md")
    # — the FULL collapse (now shared with coordinator-lesson-promote's own
    # normalization via target_wiki_canon) must treat these as the same target for
    # a wiki-targeting change_kind, closing the promote/drain canonicalization
    # mismatch (promote writes 'docs/wiki/foo.md'; the corpus also has legacy bare
    # 'foo.md' entries that never composed with promote's form under the old
    # suffix-only drain canonicalization).
    entry_prefix_bare = {
        "id": "e-uuid-1", "created": "2026-07-23T10:00:00Z", "from_repo": "peer-e",
        "title": "Same lesson, directory-prefix spelling variance", "body": "Body E1.",
        "change_kind": "wiki-append", "target_wiki": "test-design-discipline.md",
        "_peer_path": "peer-e", "_filename": "e1.yaml",
    }
    entry_prefix_full = {
        "id": "e-uuid-2", "created": "2026-07-23T10:05:00Z", "from_repo": "peer-f",
        "title": "Same lesson, directory-prefix spelling variance", "body": "Body E2.",
        "change_kind": "wiki-append", "target_wiki": "docs/wiki/test-design-discipline.md",
        "_peer_path": "peer-f", "_filename": "e2.yaml",
    }
    merged_prefix, _ = drain.dedup_entries([entry_prefix_bare, entry_prefix_full])
    assert len(merged_prefix) == 1, (
        f"dedup_entries(wiki-append directory-prefix variance) expected 1 merged entry "
        f"(promote/drain canonicalization now aligned), got {len(merged_prefix)}: {merged_prefix}"
    )
    assert len(merged_prefix[0]["sources"]) == 2, (
        f"dedup_entries(wiki-append directory-prefix variance) expected 2 sources merged, "
        f"got {merged_prefix[0]['sources']}"
    )

    # Non-wiki change_kinds must NOT get the directory-prefix collapse — two distinct
    # skill-edit entries whose target_wiki both end in the shared basename SKILL.md
    # must NOT merge just because a naive basename-only collapse would equate them.
    entry_skill_a = {
        "id": "f-uuid-1", "created": "2026-07-23T11:00:00Z", "from_repo": "peer-g",
        "title": "Different skill entirely", "body": "Body F1.",
        "change_kind": "skill-edit", "target_wiki": "skills/pickup/SKILL.md",
        "_peer_path": "peer-g", "_filename": "f1.yaml",
    }
    entry_skill_b = {
        "id": "f-uuid-2", "created": "2026-07-23T11:05:00Z", "from_repo": "peer-h",
        "title": "Different skill entirely", "body": "Body F2.",
        "change_kind": "skill-edit", "target_wiki": "skills/learn-lessons/SKILL.md",
        "_peer_path": "peer-h", "_filename": "f2.yaml",
    }
    merged_skill, _ = drain.dedup_entries([entry_skill_a, entry_skill_b])
    assert len(merged_skill) == 2, (
        f"dedup_entries(skill-edit basename collision) expected 2 DISTINCT entries "
        f"(non-wiki change_kinds must never collapse on shared SKILL.md basename), "
        f"got {len(merged_skill)}: {merged_skill}"
    )

    # --- read_peer_outbox: leading+trailing `---` document markers must still parse ---
    # Real on-disk entries in both claude-klabauter and coordinator-claude carry this shape;
    # `yaml.safe_load` (single-document) rejects it as "expected a single document in
    # the stream" — verified against the real corpus, every entry failed to parse
    # under the pre-fix code. `read_peer_outbox` must recover the first document.
    wrapped_peer = tmp / "wrapped-peer"
    wrapped_outbox = wrapped_peer / "state" / "lessons-outbox"
    wrapped_outbox.mkdir(parents=True)
    (wrapped_outbox / "2026-07-22T12-00-00-wrapped-entry.yaml").write_text(
        "---\n"
        "id: wrapped-uuid-1\n"
        'created: "2026-07-22T12:00:00Z"\n'
        "from_repo: wrapped-peer\n"
        "title: Entry wrapped in leading and trailing document markers\n"
        "body: Body wrapped.\n"
        "change_kind: wiki-append\n"
        "target_wiki: docs/wiki/some-doc.md\n"
        "---\n",
        encoding="utf-8",
    )
    wrapped_entries, wrapped_warnings = drain.read_peer_outbox(wrapped_peer)
    assert len(wrapped_entries) == 1 and not wrapped_warnings, (
        f"read_peer_outbox(wrapped `---` entry) expected 1 entry / 0 warnings, "
        f"got {len(wrapped_entries)} entries, warnings={wrapped_warnings}"
    )
    assert wrapped_entries[0]["id"] == "wrapped-uuid-1", (
        f"read_peer_outbox(wrapped `---` entry) parsed wrong id: {wrapped_entries[0]}"
    )

    # --- read: peer_path defaults to cwd when omitted (nargs="*" -> Path.cwd() fallback,
    # since SKILL.md has only ever invoked `read` with exactly one root — the drain's own) ---
    cwd_peer = tmp / "cwd-peer"
    _write_outbox_entry(
        cwd_peer, "2026-07-24T09-00-00-cwd-default.yaml",
        id="cwd-uuid-1", created="2026-07-24T09:00:00Z", from_repo="cwd-peer",
        title="Read defaults to cwd", body="Body CWD.",
        change_kind="wiki-append", target_wiki="docs/wiki/some-other-doc.md",
    )
    prior_cwd = Path.cwd()
    os.chdir(cwd_peer)
    try:
        cwd_default_output = drain.main(["read"])
    finally:
        os.chdir(prior_cwd)
    assert cwd_default_output == 0, f"main(read, no peer_path) expected exit 0, got {cwd_default_output}"

    # --- assert-empty: detector for the one-root invariant (never a real peer repo;
    # resolve_roots() is monkeypatched to a fixture peer-root list so this test never
    # touches the real machine-local registry or any real state/lessons-outbox/ tree) ---
    self_root = tmp / "assert-empty-self"
    self_root.mkdir()

    peer_verified_empty = tmp / "ae-peer-empty"
    (peer_verified_empty / "state" / "lessons-outbox").mkdir(parents=True)

    peer_stranded = tmp / "ae-peer-stranded"
    stranded_outbox = peer_stranded / "state" / "lessons-outbox"
    stranded_outbox.mkdir(parents=True)
    (stranded_outbox / "2026-07-23T10-00-00-stranded-1.yaml").write_text("id: x1\n", encoding="utf-8")
    (stranded_outbox / "2026-07-23T10-01-00-stranded-2.yaml").write_text("id: x2\n", encoding="utf-8")

    peer_drained_only = tmp / "ae-peer-drained-only"
    drained_only_outbox = peer_drained_only / "state" / "lessons-outbox"
    (drained_only_outbox / "drained").mkdir(parents=True)
    (drained_only_outbox / "drained" / "2026-07-01T00-00-00-old-entry.yaml").write_text(
        "id: old\n", encoding="utf-8"
    )

    peer_absent = tmp / "ae-peer-does-not-exist"  # deliberately never created

    # (d) self-root excluded, (e) absent peer skipped-with-reason — fixture roots
    # returned in a deliberately non-canonical order, including the self-root itself
    # and a trailing slash variant of it, to prove the path-normalized subtraction.
    fixture_roots = [
        str(self_root) + os.sep,  # trailing-slash variant of the self-root
        str(peer_verified_empty),
        str(peer_drained_only),
        str(peer_absent),
    ]
    drain.resolve_roots = lambda: fixture_roots

    # (a) all (non-stranded) peers empty -> PASS
    result_pass = drain.assert_empty(self_root)
    assert result_pass["status"] == "PASS", f"assert_empty(all empty) expected PASS, got {result_pass}"
    assert str(self_root.resolve()) not in result_pass["checked"] and str(self_root.resolve()) not in [
        s["peer_root"] for s in result_pass["skipped"]
    ], f"assert_empty must exclude self_root entirely, got {result_pass}"
    checked_names = sorted(Path(p).name for p in result_pass["checked"])
    assert checked_names == ["ae-peer-drained-only", "ae-peer-empty"], (
        f"assert_empty(all empty) expected [ae-peer-drained-only, ae-peer-empty] in checked "
        f"(drained/-only peer must count as verified-empty, not skipped or FAIL), got {checked_names}"
    )
    # (e) absent peer root is skipped-with-reason, never counted verified-empty
    skipped_names = {Path(s["peer_root"]).name: s["reason"] for s in result_pass["skipped"]}
    assert "ae-peer-does-not-exist" in skipped_names, (
        f"assert_empty: registered-but-absent peer root must be skipped-with-reason, "
        f"got skipped={result_pass['skipped']}"
    )
    assert "not on disk" in skipped_names["ae-peer-does-not-exist"], (
        f"assert_empty: absent-peer skip reason should explain 'not on disk', "
        f"got {skipped_names['ae-peer-does-not-exist']!r}"
    )

    # (b) one peer non-empty -> FAIL, correct root + count reported
    fixture_roots_with_stranded = fixture_roots + [str(peer_stranded)]
    drain.resolve_roots = lambda: fixture_roots_with_stranded
    result_fail = drain.assert_empty(self_root)
    assert result_fail["status"] == "FAIL", f"assert_empty(one peer stranded) expected FAIL, got {result_fail}"
    assert len(result_fail["non_empty"]) == 1, (
        f"assert_empty(one peer stranded) expected exactly 1 non_empty entry, "
        f"got {result_fail['non_empty']}"
    )
    ne = result_fail["non_empty"][0]
    assert Path(ne["peer_root"]).name == "ae-peer-stranded" and ne["count"] == 2, (
        f"assert_empty(one peer stranded) expected ae-peer-stranded/count=2, got {ne}"
    )

    # CLI exit-code contract: assert-empty is FAIL-LOUD (non-zero on FAIL), the
    # deliberate opposite of learn-lessons-roots.py's always-exit-0 convention — this
    # divergence is the entire point of the subcommand (see module docstring).
    drain.resolve_roots = lambda: fixture_roots  # no stranded peer
    exit_pass = drain.main(["assert-empty", str(self_root)])
    assert exit_pass == 0, f"main(assert-empty, all empty) expected exit 0, got {exit_pass}"

    drain.resolve_roots = lambda: fixture_roots_with_stranded
    exit_fail = drain.main(["assert-empty", str(self_root)])
    assert exit_fail == 1, f"main(assert-empty, one peer stranded) expected exit 1, got {exit_fail}"
