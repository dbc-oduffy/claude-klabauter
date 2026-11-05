"""since_base_overlap: landing blobs that drop lines a foreign commit added since the run base.

Repos are written in-process with ``git_objects`` (no spawn).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.git import since_base_overlap
from coordinator_core.git.git_objects import build_tree, write_object
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

PATH = "pkg/mod.py"
PLAN = "docs/plans/own-plan.md"


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.gitdir = root / ".git"
        (self.gitdir / "refs" / "heads").mkdir(parents=True)
        (self.gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.head: Optional[str] = None

    def commit(self, files: Dict[str, str], message: str) -> str:
        entries = {
            p: (0o100644, write_object(self.gitdir, b"blob", text.encode())) for p, text in files.items()
        }
        tree = build_tree(self.gitdir, entries)
        parent = f"parent {self.head}\n" if self.head else ""
        body = (
            f"tree {tree}\n{parent}author t <t@t> 1700000000 +0000\n"
            f"committer t <t@t> 1700000000 +0000\n\n{message}\n"
        )
        self.head = write_object(self.gitdir, b"commit", body.encode())
        (self.gitdir / "refs" / "heads" / "main").write_text(self.head + "\n", encoding="utf-8")
        return self.head

    def land(self, text: str) -> None:
        target = self.root / PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())


def _base_lines() -> List[str]:
    out = []
    for i in range(30):
        out.append(f"def f{i}(\n")
        out.append("    a,\n")
        out.append(")\n")
        out.append("\n")
    return out


def _join(lines: List[str]) -> str:
    return "".join(lines)


def _setup(tmp_path: Path):
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    return repo, base, base_lines


def test_stale_base_add_only_landing_that_drops_a_sibling_line_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    foreign = repo.commit({PATH: _join(foreign_lines)}, "foreign terminal commit")
    repo.land(_join(list(base_lines) + ["own_edit = 2\n"]))

    findings, notes = since_base_overlap.check(tmp_path, [PATH], base, run_id=None)

    assert notes == []
    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]
    assert foreign[:10] in findings[0].detail


def test_add_only_landing_over_the_sibling_version_keeps_the_sibling_line_and_is_clean(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign terminal commit")
    landed = _join(foreign_lines) + "own_edit = 2\n"
    repo.land(landed)

    assert since_base_overlap.check(tmp_path, [PATH], base, run_id=None) == ([], [])


def test_stale_landing_that_also_edits_a_base_line_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    foreign = repo.commit({PATH: _join(foreign_lines)}, "foreign terminal commit")
    stale = list(base_lines) + ["own_edit = 2\n"]
    stale[1] = "    changed_by_run,\n"
    repo.land(_join(stale))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]
    assert foreign[:10] in findings[0].detail


def test_landing_that_keeps_foreign_lines_is_clean(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    kept = list(foreign_lines)
    kept[1] = "    changed_by_run,\n"
    repo.land(_join(kept) + "own = 1\n")

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_crlf_worktree_that_keeps_foreign_lines_is_clean(tmp_path: Path) -> None:
    # core.autocrlf checkout: LF blobs, CRLF worktree file.
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land((_join(foreign_lines) + "own = 1\n").replace("\n", "\r\n"))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_crlf_worktree_that_drops_foreign_lines_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    stale = list(base_lines)
    stale[1] = "    changed_by_run,\n"
    repo.land((_join(stale) + "own = 1\n").replace("\n", "\r\n"))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]


def test_crlf_stale_add_only_landing_that_drops_a_sibling_line_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land((_join(base_lines) + "own = 1\n").replace("\n", "\r\n"))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]


def test_removing_blank_or_paren_line_the_foreign_commit_did_not_add_is_clean(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "\n")
    foreign_lines.insert(41, ")\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign adds blank and paren")
    landed = list(foreign_lines)
    del landed[3]  # a base blank line
    del landed[2]  # a base ")" line
    repo.land(_join(landed))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_removing_the_blank_line_the_foreign_commit_added_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(41, "\n")
    foreign = repo.commit({PATH: _join(foreign_lines)}, "foreign adds a blank line")
    landed = list(foreign_lines)
    del landed[41]
    repo.land(_join(landed))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert len(findings) == 1 and foreign[:10] in findings[0].detail


def test_own_checkpoint_commit_lines_do_not_count(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    own_lines = list(base_lines)
    own_lines.insert(40, "own_checkpoint_line = 1\n")
    repo.commit(
        {PATH: _join(own_lines)},
        f"checkpoint\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}",
    )
    repo.land(_join(base_lines))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None, plan=PLAN) == []


def _older_base_setup(tmp_path: Path, plan: str):
    repo, b0, base_lines = _setup(tmp_path)
    b1 = repo.commit({PATH: _join(base_lines), "other.txt": "x\n"}, "run base")
    own_lines = list(base_lines)
    own_lines.insert(40, "own_checkpoint_line = 1\n")
    repo.commit({PATH: _join(own_lines)}, f"checkpoint\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {b0}")
    repo.land(_join(base_lines))
    return b1


def test_own_checkpoint_commit_stamped_with_ancestor_of_run_base_is_own(tmp_path: Path) -> None:
    b1 = _older_base_setup(tmp_path, PLAN)

    assert since_base_overlap.check(tmp_path, [PATH], b1, plan=PLAN) == ([], [])


def test_ancestor_walk_is_bounded_by_ancestor_cap(tmp_path: Path, monkeypatch) -> None:
    b1 = _older_base_setup(tmp_path, PLAN)
    monkeypatch.setattr(since_base_overlap, "ANCESTOR_CAP", 0)

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], b1, plan=PLAN)) == 1


def test_other_plan_checkpoint_commit_stamped_with_ancestor_of_run_base_is_foreign(tmp_path: Path) -> None:
    b1 = _older_base_setup(tmp_path, "docs/plans/peer.md")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], b1, plan=PLAN)) == 1


def test_other_plan_checkpoint_commit_is_foreign(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    other = list(base_lines)
    other.insert(40, "peer_line = 1\n")
    repo.commit(
        {PATH: _join(other)}, f"checkpoint\n\nCheckpoint-Plan: docs/plans/peer.md\nCheckpoint-Base: {base}"
    )
    repo.land(_join(base_lines))

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None, plan=PLAN)) == 1


def _halt_record_setup(tmp_path: Path, subject: str):
    repo, base, base_lines = _setup(tmp_path)
    halted = list(base_lines)
    halted.insert(40, "halt_record_line = 1\n")
    repo.commit({PATH: _join(halted)}, subject)
    repo.land(_join(base_lines))
    return base


def test_own_run_halt_record_commit_is_not_overlap(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, "warp: record halted run-R1 run artifacts")

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id="run-R1") == []


def test_own_run_halt_record_process_files_variant_is_not_overlap(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, "warp: record halted run-R1 run artifacts (process files)")

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id="run-R1") == []


def test_other_run_halt_record_commit_is_foreign(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, "warp: record halted run-R2 run artifacts")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id="run-R1")) == 1


_REFIRE = "warp-2026-11-01-engine-ask-one-door-all-arms-3542ec-20261101T172352-591218"
_PREDECESSOR = "warp-2026-11-01-engine-ask-one-door-all-arms-3542ec-20261101T150608-70c743"
_OTHER_SIZING = "warp-2026-11-01-some-other-sizing-aaaaaa-20261101T150608-70c743"


def test_predecessor_halt_record_of_same_sizing_lineage_is_own(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, f"warp: record halted {_PREDECESSOR} run artifacts")

    assert since_base_overlap.check(tmp_path, [PATH], base, run_id=_REFIRE, plan=PLAN) == ([], [])
    assert since_base_overlap.check(tmp_path, [PATH], base, run_id=_REFIRE) == ([], [])


def test_halt_record_of_another_sizing_lineage_is_foreign(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, f"warp: record halted {_OTHER_SIZING} run artifacts")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=_REFIRE, plan=PLAN)) == 1


def test_ask_run_id_has_no_lineage_so_other_ask_halt_is_foreign(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, "warp: record halted ask-20261101T150608-70c743 run artifacts")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id="ask-20261101T172352-591218")) == 1


def test_halt_record_commit_without_run_id_is_foreign(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, "warp: record halted run-R1 run artifacts")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)) == 1


_ASK = "ask-20261102T133847-486eca"
_CHILD = "warp-2026-11-02-920e91-1-rea-ask-2-ghidra-ingest-20261102T133953-8413fc"
_CHILD_OTHER_SIZING = "warp-2026-11-02-60ec97-2-refire-remeasures-landing-set-20261102T131916-b8162e"


def _record_ask_run(root: Path, run_id: str, sizing_rel: str) -> None:
    run_dir = root / "state" / "warp" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(f'{{"all_of_pm_ask": false, "sizing_rel": "{sizing_rel}"}}', encoding="utf-8")


def test_ask_run_child_warp_halt_record_is_own(tmp_path: Path) -> None:
    _record_ask_run(tmp_path, _ASK, "state/sizings/2026-11-02-920e91-1-rea-ask-2-ghidra-ingest.yaml")
    base = _halt_record_setup(tmp_path, f"warp: record halted {_CHILD} run artifacts")

    assert since_base_overlap.check(tmp_path, [PATH], base, run_id=_ASK, plan=PLAN) == ([], [])
    assert since_base_overlap.check(tmp_path, [PATH], base, run_id=_ASK) == ([], [])


def test_ask_run_halt_record_of_another_sizings_child_is_foreign(tmp_path: Path) -> None:
    _record_ask_run(tmp_path, _ASK, "state/sizings/2026-11-02-920e91-1-rea-ask-2-ghidra-ingest.yaml")
    base = _halt_record_setup(tmp_path, f"warp: record halted {_CHILD_OTHER_SIZING} run artifacts")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=_ASK, plan=PLAN)) == 1


def test_ask_run_without_recorded_sizing_has_no_child_lineage(tmp_path: Path) -> None:
    base = _halt_record_setup(tmp_path, f"warp: record halted {_CHILD} run artifacts")

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=_ASK, plan=PLAN)) == 1


def test_ask_sizing_lineage_rejects_path_shaped_run_id(tmp_path: Path) -> None:
    assert since_base_overlap._ask_sizing_lineage(tmp_path, "ask-../x") is None
    assert since_base_overlap._ask_sizing_lineage(tmp_path, "ask-a/b") is None
    assert since_base_overlap._ask_sizing_lineage(tmp_path, "ask-a\\b") is None


def test_path_unchanged_since_base_is_skipped(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    repo.commit({PATH: _join(base_lines), "other.txt": "x\n"}, "unrelated")
    repo.land(_join(base_lines[:-1]))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_unreachable_base_is_a_note_not_a_finding(tmp_path: Path) -> None:
    repo, _base, base_lines = _setup(tmp_path)
    repo.commit({PATH: _join(base_lines) + "x\n"}, "next")
    repo.land(_join(base_lines))

    findings, notes = since_base_overlap.check(tmp_path, [PATH], "0" * 40)

    assert findings == [] and notes == [since_base_overlap.NOTE_WALK_CAPPED]


def _count_meta_reads_after_chain_walk(monkeypatch) -> List[str]:
    real_chain = since_base_overlap._chain_to_base
    real_meta = since_base_overlap.commit_meta
    state = {"walking": False}
    reads: List[str] = []

    def chain(*args, **kwargs):
        state["walking"] = True
        try:
            return real_chain(*args, **kwargs)
        finally:
            state["walking"] = False

    def meta(common_dir, sha):
        if not state["walking"]:
            reads.append(sha)
        return real_meta(common_dir, sha)

    monkeypatch.setattr(since_base_overlap, "_chain_to_base", chain)
    monkeypatch.setattr(since_base_overlap, "commit_meta", meta)
    return reads


def test_clean_landing_reads_no_commit_metadata_beyond_the_chain_walk(tmp_path: Path, monkeypatch) -> None:
    repo, base, base_lines = _setup(tmp_path)
    for n in range(30):
        repo.commit({PATH: _join(base_lines), f"other{n}.txt": "x\n"}, f"unrelated {n}")
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land(_join(foreign_lines) + "own = 1\n")
    reads = _count_meta_reads_after_chain_walk(monkeypatch)

    findings, notes = since_base_overlap.check(tmp_path, [PATH], base, run_id="R1", plan=PLAN)

    assert (findings, notes) == ([], []) and reads == []


def test_landing_that_drops_no_line_never_walks_the_chain(tmp_path: Path, monkeypatch) -> None:
    repo, base, base_lines = _setup(tmp_path)
    repo.commit({PATH: _join(base_lines) + "foreign = 1\n"}, "foreign")
    repo.land(_join(base_lines) + "foreign = 1\n" + "own = 1\n")
    walks: List[tuple] = []
    real_chain = since_base_overlap._chain_to_base
    monkeypatch.setattr(since_base_overlap, "_chain_to_base", lambda *a: walks.append(a) or real_chain(*a))

    assert since_base_overlap.check(tmp_path, [PATH], base, run_id="R1", plan=PLAN) == ([], [])
    assert walks == []


def test_landing_that_drops_a_line_walks_the_chain_once(tmp_path: Path, monkeypatch) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land(_join(base_lines))
    walks: List[tuple] = []
    real_chain = since_base_overlap._chain_to_base
    monkeypatch.setattr(since_base_overlap, "_chain_to_base", lambda *a: walks.append(a) or real_chain(*a))

    findings, _notes = since_base_overlap.check(tmp_path, [PATH, PATH], base)

    assert len(findings) == 2 and len(walks) == 1


def test_dropping_landing_reads_commit_metadata_for_the_chain(tmp_path: Path, monkeypatch) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land(_join(base_lines))
    reads = _count_meta_reads_after_chain_walk(monkeypatch)

    findings, _notes = since_base_overlap.check(tmp_path, [PATH], base)

    assert len(findings) == 1 and reads != []


def _tiles(ops, a, b) -> bool:
    i = j = 0
    for tag, i1, i2, j1, j2 in ops:
        if (i1, j1) != (i, j) or (tag == "equal" and a[i1:i2] != b[j1:j2]):
            return False
        i, j = i2, j2
    return (i, j) == (len(a), len(b))


def _repetitive_pair(n: int):
    a = [b"    0.0,\n" if i % 5 else f"    {i}.5,\n".encode() for i in range(n)]
    b = [b"    0.0,\n" if i % 4 else f"    {i}.25,\n".encode() for i in range(n)]
    b[0], a[-1] = b"first\n", b"last\n"
    return a, b


def test_opcodes_on_a_70k_line_repetitive_pair_tile_both_sides_under_the_bar() -> None:
    import time

    a, b = _repetitive_pair(70_000)
    start = time.process_time()
    ops = since_base_overlap._opcodes(a, b)
    spent = time.process_time() - start

    assert spent < 0.5, spent
    assert _tiles(ops, a, b)


def test_opcodes_hand_difflib_no_more_than_the_bound_per_side(monkeypatch) -> None:
    import difflib

    from coordinator_core.ops.dispatch_emit.run_hunk_claims import MAX_MATCHED_MIDDLE

    seen: List[tuple] = []
    real = difflib.SequenceMatcher

    class Spy(real):
        def __init__(self, isjunk=None, a="", b="", autojunk=True):
            seen.append((len(a), len(b)))
            super().__init__(isjunk, a, b, autojunk)

    monkeypatch.setattr(difflib, "SequenceMatcher", Spy)
    a, b = _repetitive_pair(20_000)
    ops = since_base_overlap._opcodes(a, b)

    assert _tiles(ops, a, b)
    assert all(max(x, y) <= MAX_MATCHED_MIDDLE for x, y in seen)
    assert any(tag == "replace" and i2 - i1 > MAX_MATCHED_MIDDLE for tag, i1, i2, _j1, _j2 in ops)


def test_opcodes_small_middle_matches_difflib_exactly() -> None:
    import difflib

    a = [f"l{i}\n".encode() for i in range(40)]
    b = list(a)
    b[10:12] = [b"x\n"]
    b.insert(30, b"y\n")
    want = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()

    assert since_base_overlap._opcodes(a, b) == want


def test_opcodes_unique_heavy_pair_stays_exact_and_tiles() -> None:
    a = [f"u{i}\n".encode() for i in range(5_000)]
    b = list(a)
    b[0], b[-1] = b"head\n", b"tail\n"
    del b[2_000:2_010]
    ops = since_base_overlap._opcodes(a, b)

    assert _tiles(ops, a, b)
    assert [op for op in ops if op[0] != "equal"] == [
        ("replace", 0, 1, 0, 1),
        ("delete", 2_000, 2_010, 2_000, 2_000),
        ("replace", 4_999, 5_000, 4_989, 4_990),
    ]


def test_module_cannot_spawn() -> None:
    assert not hasattr(since_base_overlap, "subprocess")


def test_path_history_beyond_version_cap_is_a_note_not_a_finding(tmp_path: Path, monkeypatch) -> None:
    repo, base, base_lines = _setup(tmp_path)
    monkeypatch.setattr(since_base_overlap, "VERSIONS_PER_PATH_CAP", 2)
    lines = list(base_lines)
    for n in range(3):
        lines.insert(40, f"foreign_{n} = 1\n")
        repo.commit({PATH: _join(lines)}, f"foreign {n}")
    repo.land(_join(base_lines))

    findings, notes = since_base_overlap.check(tmp_path, [PATH], base)

    assert findings == [] and notes == [since_base_overlap.NOTE_WALK_CAPPED]
