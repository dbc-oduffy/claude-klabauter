"""
coordinator_core.tests.test_dag_handoff_id_index — Coverage for the C6
pointer-normalization seam (2026-07-26): `EDGE_KIND_FIELD_ALIASES`,
`build_handoff_id_index`, `resolve_target(..., id_index=...)`, and the
`predecessor_id`/`origin_handoff_id` alias tier as actually followed by
`walk_forward`/`referenced_by`.

Added 2026-07-27 (review Finding 3): the diff that introduced this machinery
shipped with zero new unit tests. This closes that gap:
  (1) build_handoff_id_index — dedup/collision-last-wins, blank/missing
      handoff_id skipped.
  (2) resolve_target(..., id_index=...) — id hit, id miss falls through to
      the existing filename-based tiers, id_index=None is byte-for-byte
      unchanged.
  (3) walk_forward integration — a predecessor_id-only edge (no
      predecessor: field present) is actually followed end to end.
  (4) A stale/dangling id (no matching id_index entry) is fail-closed: falls
      through to the filename tiers and, when nothing there resolves it
      either, resolves to None rather than colliding with an id-shaped
      string that happens to also look like a valid filename fragment.

Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48 § C6
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import dag
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=root, check=True, **no_console_passthrough_kwargs()
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=root,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=root,
        check=True,
        **no_console_passthrough_kwargs(),
    )
    return root


def _write_handoff(root: Path, rel_path: str, lines: list[str]) -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "\n".join(lines) + "\n---\n\nbody\n"
    p.write_text(body, encoding="utf-8")
    return p


class TestBuildHandoffIdIndex:
    def test_maps_handoff_id_to_absolute_path(self, tmp_path):
        root = _init_repo(tmp_path)
        p = _write_handoff(root, "state/handoffs/a.md", ["handoff_id: hnd-a-111"])

        index = dag.build_handoff_id_index([str(p)])

        assert index == {"hnd-a-111": str(Path(p).absolute())}

    def test_blank_or_missing_handoff_id_is_skipped(self, tmp_path):
        root = _init_repo(tmp_path)
        missing = _write_handoff(root, "state/handoffs/no-id.md", ["title: no id here"])
        blank = _write_handoff(root, "state/handoffs/blank-id.md", ['handoff_id: ""'])

        index = dag.build_handoff_id_index([str(missing), str(blank)])

        assert index == {}

    def test_collision_last_path_in_input_order_wins(self, tmp_path):
        root = _init_repo(tmp_path)
        first = _write_handoff(root, "state/handoffs/first.md", ["handoff_id: hnd-dup"])
        second = _write_handoff(root, "state/handoffs/second.md", ["handoff_id: hnd-dup"])

        index = dag.build_handoff_id_index([str(first), str(second)])

        assert index["hnd-dup"] == str(Path(second).absolute())

    def test_unreadable_path_does_not_raise(self, tmp_path):
        root = _init_repo(tmp_path)
        nonexistent = str(root / "state" / "handoffs" / "does-not-exist.md")

        index = dag.build_handoff_id_index([nonexistent])

        assert index == {}


class TestResolveTargetIdIndex:
    def test_id_shaped_ref_hits_index_before_filename_tiers(self, tmp_path):
        root = _init_repo(tmp_path)
        target = _write_handoff(root, "state/handoffs/target.md", ["handoff_id: hnd-target-1"])
        handoff_dir = str(root / "state" / "handoffs")
        id_index = {"hnd-target-1": str(target.absolute())}

        result = dag.resolve_target(
            "hnd-target-1", handoff_dir, str(root), id_index=id_index
        )

        assert result == str(target.absolute())

    def test_id_shaped_ref_not_in_index_falls_through_to_filename_tiers(self, tmp_path):
        root = _init_repo(tmp_path)
        handoff_dir = str(root / "state" / "handoffs")
        result = dag.resolve_target(
            "hnd-not-indexed", handoff_dir, str(root), id_index={}
        )

        assert result is None

    def test_id_index_none_is_byte_for_byte_unchanged_for_path_shaped_ref(self, tmp_path):
        root = _init_repo(tmp_path)
        target = _write_handoff(root, "state/handoffs/plain.md", ["title: plain"])
        handoff_dir = str(root / "state" / "handoffs")

        without_index = dag.resolve_target("plain.md", handoff_dir, str(root))
        with_none_index = dag.resolve_target(
            "plain.md", handoff_dir, str(root), id_index=None
        )

        assert without_index == with_none_index == str(target.absolute())

    def test_lazy_index_eligible_but_empty_converges_with_plain_empty_dict(self, tmp_path):
        root = _init_repo(tmp_path)
        handoff_dir = str(root / "state" / "handoffs")
        _write_handoff(root, "state/handoffs/no-id.md", ["title: no id here"])

        lazy_index = dag._LazyHandoffIdIndex(str(root))
        result_lazy = dag.resolve_target(
            "hnd-not-present", handoff_dir, str(root), id_index=lazy_index
        )
        result_plain = dag.resolve_target(
            "hnd-not-present", handoff_dir, str(root), id_index={}
        )

        assert result_lazy == result_plain is None

    def test_md_suffixed_ref_never_consults_id_index_even_on_a_matching_key(self, tmp_path):
        root = _init_repo(tmp_path)
        decoy = _write_handoff(root, "state/handoffs/decoy.md", ["handoff_id: real.md"])
        real = _write_handoff(root, "state/handoffs/real.md", ["title: real file"])
        handoff_dir = str(root / "state" / "handoffs")
        id_index = {"real.md": str(decoy.absolute())}

        result = dag.resolve_target("real.md", handoff_dir, str(root), id_index=id_index)

        assert result == str(real.absolute())


class TestWalkForwardFollowsPredecessorIdAlias:
    def test_predecessor_id_only_edge_with_no_predecessor_field_is_followed(self, tmp_path):
        root = _init_repo(tmp_path)
        predecessor = _write_handoff(
            root, "state/handoffs/predecessor.md", ["handoff_id: hnd-pred-1"]
        )
        successor = _write_handoff(
            root,
            "state/handoffs/successor.md",
            [
                "predecessor_id: hnd-pred-1",
            ],
        )

        result = dag.walk_forward(
            str(successor), edge_kinds={"predecessor"}, repo_root=str(root)
        )

        assert result["terminatedEarly"] == ""
        assert str(predecessor.absolute()) in result["nodes"]
        assert result["orderedPaths"] == [
            str(successor.absolute()),
            str(predecessor.absolute()),
        ]

    def test_edge_kinds_without_an_aliased_kind_skips_the_corpus_scan(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        start = _write_handoff(root, "state/handoffs/start.md", ["title: start"])

        calls = []
        orig = dag._scan_handoff_corpus_paths

        def counting(repo_root):
            calls.append(repo_root)
            return orig(repo_root)

        monkeypatch.setattr(dag, "_scan_handoff_corpus_paths", counting)

        dag.walk_forward(str(start), edge_kinds={"forked_from"}, repo_root=str(root))

        assert calls == []


class TestWalkForwardLazyIdIndexScan:

    def test_zero_id_shaped_refs_never_triggers_the_corpus_scan(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        predecessor = _write_handoff(root, "state/handoffs/predecessor.md", ["title: pred"])
        successor = _write_handoff(
            root,
            "state/handoffs/successor.md",
            ["predecessor: predecessor.md"],
        )

        calls = []
        orig = dag._scan_handoff_corpus_paths

        def counting(repo_root):
            calls.append(repo_root)
            return orig(repo_root)

        monkeypatch.setattr(dag, "_scan_handoff_corpus_paths", counting)

        result = dag.walk_forward(
            str(successor), edge_kinds={"predecessor"}, repo_root=str(root)
        )

        assert calls == []
        assert result["terminatedEarly"] == ""
        assert str(predecessor.absolute()) in result["nodes"]

    def test_an_id_shaped_ref_still_triggers_exactly_one_scan_and_resolves(
        self, tmp_path, monkeypatch
    ):
        root = _init_repo(tmp_path)
        predecessor = _write_handoff(
            root, "state/handoffs/predecessor.md", ["handoff_id: hnd-pred-lazy"]
        )
        successor = _write_handoff(
            root,
            "state/handoffs/successor.md",
            ["predecessor_id: hnd-pred-lazy"],
        )

        calls = []
        orig = dag._scan_handoff_corpus_paths

        def counting(repo_root):
            calls.append(repo_root)
            return orig(repo_root)

        monkeypatch.setattr(dag, "_scan_handoff_corpus_paths", counting)

        result = dag.walk_forward(
            str(successor), edge_kinds={"predecessor"}, repo_root=str(root)
        )

        assert len(calls) == 1
        assert result["terminatedEarly"] == ""
        assert str(predecessor.absolute()) in result["nodes"]
        assert result["orderedPaths"] == [
            str(successor.absolute()),
            str(predecessor.absolute()),
        ]


class TestDanglingIdFailsClosed:
    def test_dangling_predecessor_id_terminates_early_as_missing_link(self, tmp_path):
        root = _init_repo(tmp_path)
        successor = _write_handoff(
            root,
            "state/handoffs/successor.md",
            ["predecessor_id: hnd-does-not-exist"],
        )

        result = dag.walk_forward(
            str(successor), edge_kinds={"predecessor"}, repo_root=str(root)
        )

        assert result["terminatedEarly"] == "missing-link"
        assert result["orderedPaths"] == [str(successor.absolute())]


class TestBuildHandoffIdIndexRawScan:
    def test_raw_scan_matches_full_parse_for_bare_handoff_id(self, tmp_path):
        root = _init_repo(tmp_path)
        p = _write_handoff(root, "state/handoffs/raw.md", ["handoff_id: hnd-raw-1"])

        index = dag.build_handoff_id_index([str(p)])

        assert index == {"hnd-raw-1": str(Path(p).absolute())}

    def test_metas_supplied_call_is_untouched_by_raw_scan(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        p = _write_handoff(root, "state/handoffs/meta.md", ["handoff_id: hnd-meta-1"])

        def boom(*_a, **_kw):
            raise AssertionError("raw scan must not run when metas is supplied")

        monkeypatch.setattr(dag, "_raw_scan_handoff_id", boom)

        index = dag.build_handoff_id_index(
            [str(p)], metas={str(Path(p).absolute()): {"handoff_id": "hnd-meta-1"}}
        )

        assert index == {"hnd-meta-1": str(Path(p).absolute())}

    def test_frontmatter_terminator_beyond_4kb_falls_back_to_read_meta(self, tmp_path):
        root = _init_repo(tmp_path)
        p = root / "state" / "handoffs" / "big.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        padding = "\n".join(f"pad_field_{i}: value_{i}" for i in range(400))
        body = "---\nhandoff_id: hnd-big-1\n" + padding + "\n---\n\nbody\n"
        p.write_text(body, encoding="utf-8")
        assert len(body[:4096].encode("utf-8")) == 4096
        assert "---" not in body[:4096].split("handoff_id", 1)[1][-50:]

        index = dag.build_handoff_id_index([str(p)])

        assert index == {"hnd-big-1": str(p.absolute())}

    def test_quoted_handoff_id_value_falls_back_to_read_meta(self, tmp_path):
        root = _init_repo(tmp_path)
        p = _write_handoff(
            root, "state/handoffs/quoted.md", ['handoff_id: "hnd-quoted-1"']
        )

        index = dag.build_handoff_id_index([str(p)])

        assert index == {"hnd-quoted-1": str(p.absolute())}

    def test_body_handoff_id_line_past_terminator_is_not_picked_up(self, tmp_path):
        root = _init_repo(tmp_path)
        p = root / "state" / "handoffs" / "body-id.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\ntitle: no real id here\n---\n\n"
            "Some prose mentions handoff_id: hnd-body-decoy in passing.\n",
            encoding="utf-8",
        )

        index = dag.build_handoff_id_index([str(p)])

        assert index == {}

    def test_short_frontmatter_body_quotes_handoff_id_in_prose_not_matched(self, tmp_path):
        root = _init_repo(tmp_path)
        p = root / "state" / "handoffs" / "prose-decoy.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\ntitle: short frontmatter\n---\n\n"
            "Example fenced block:\n```\nhandoff_id: hnd-fenced-decoy\n```\n",
            encoding="utf-8",
        )

        index = dag.build_handoff_id_index([str(p)])

        assert index == {}


class TestRawScanHandoffIdUnit:
    def test_returns_none_when_key_absent_within_window(self, tmp_path):
        p = tmp_path / "no-id.md"
        p.write_text("---\ntitle: none here\n---\n\nbody\n", encoding="utf-8")

        assert dag._raw_scan_handoff_id(str(p)) is None

    def test_returns_fallback_sentinel_when_terminator_missing(self, tmp_path):
        p = tmp_path / "unterminated.md"
        p.write_text("---\nhandoff_id: hnd-x\n" + ("pad\n" * 2000), encoding="utf-8")

        assert dag._raw_scan_handoff_id(str(p)) is dag._RAW_SCAN_FALLBACK

    def test_returns_fallback_sentinel_for_unreadable_path(self, tmp_path):
        missing = tmp_path / "does-not-exist.md"

        assert dag._raw_scan_handoff_id(str(missing)) is dag._RAW_SCAN_FALLBACK


class TestRawScanSkipsLeadingHtmlCommentPrologue:
    def test_banner_before_frontmatter_does_not_defeat_the_raw_scan(self, tmp_path):
        p = tmp_path / "banner.md"
        p.write_text(
            "<!-- generated, do not hand-edit -->\n"
            "---\nhandoff_id: hnd-banner-1\n---\n\nbody\n",
            encoding="utf-8",
        )

        index = dag.build_handoff_id_index([str(p)])

        assert index == {"hnd-banner-1": str(p.absolute())}

    def test_unclosed_leading_comment_falls_back_to_read_meta(self, tmp_path):
        p = tmp_path / "unclosed-banner.md"
        p.write_text(
            "<!-- unterminated banner\n"
            "---\nhandoff_id: hnd-unclosed-1\n---\n\nbody\n",
            encoding="utf-8",
        )

        index = dag.build_handoff_id_index([str(p)])

        assert index == {}


def test_raw_scan_agrees_with_the_general_parser_over_the_live_corpus():
    root = Path(__file__).resolve().parents[2]
    paths = dag.scan_repo_handoff_corpus(str(root))
    if not paths:
        pytest.skip("no handoff corpus in this checkout")

    disagreements = []
    for p in paths:
        raw = dag._raw_scan_handoff_id(p)
        if raw is dag._RAW_SCAN_FALLBACK:
            continue
        raw_value = raw if raw else None

        general_meta = dag.read_handoff_meta(p)
        general_hid = general_meta.get("handoff_id") if general_meta else None
        general_value = (
            str(general_hid).strip() if general_hid not in (None, "") else None
        )
        general_value = general_value or None

        if raw_value != general_value:
            disagreements.append((p, raw_value, general_value))

    assert not disagreements, (
        f"{len(disagreements)} handoff file(s) read differently by the raw "
        f"4KB scan and the general parser; first 5: {disagreements[:5]}"
    )
