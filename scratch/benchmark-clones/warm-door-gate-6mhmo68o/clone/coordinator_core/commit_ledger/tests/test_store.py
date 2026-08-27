"""
Tests for coordinator_core.commit_ledger.store.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C1.md
Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C2.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.commit_ledger import store


def _init_git_repo(tmp_path: Path) -> Path:
    """Fixture-only ``.git`` marker — a bare directory is sufficient for the
    walk-based resolvers under test (``show_toplevel`` / ``git_common_dir``
    both recognise a ``.git`` dir OR file, no real repo state needed), so no
    ``git init`` subprocess is spawned to build it."""
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_handoff(path: Path, handoff_id: str, predecessor: str = "", predecessor_id: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"handoff_id: {handoff_id}", "kind: session-handoff"]
    if predecessor:
        lines.append(f'predecessor: "{predecessor}"')
    if predecessor_id:
        lines.append(f'predecessor_id: "{predecessor_id}"')
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# C1 coverage — append/read basics
# ---------------------------------------------------------------------------


def test_append_and_read_roundtrip(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    assert store.append_entry("hnd-a", "sha1", "code", weight_basis={"loc": 5}, cwd=cwd)
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["sha"] == "sha1"
    assert entries[0]["kind"] == "code"
    assert entries[0]["reviewed_by"] == []


def test_duplicate_sha_deduped_on_read_first_wins(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd)
    store.append_entry("hnd-a", "sha1", "doc", cwd=cwd)  # retry-shaped duplicate
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["kind"] == "code"


def test_read_entries_empty_when_no_ledger(tmp_path):
    repo = _init_git_repo(tmp_path)
    assert store.read_entries("hnd-nonexistent", cwd=str(repo)) == []


def test_append_skips_unlocked_when_lock_unavailable(tmp_path, monkeypatch, caplog):
    """Forced lock-acquisition failure must SKIP the append (never fall
    through to an unlocked write) and log a warning naming the sha — the
    fix for the reviewed unlocked-append fallback (an unlocked multi-writer
    append is not proven atomic on Windows)."""
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    def _raise_timeout(*args, **kwargs):
        raise store.LockTimeout("forced for test")

    monkeypatch.setattr(store, "held_lock", _raise_timeout)

    with caplog.at_level("WARNING"):
        result = store.append_entry("hnd-a", "sha-lost", "code", cwd=cwd)

    assert result is False
    assert store.read_entries("hnd-a", cwd=cwd) == []
    assert any("sha-lost" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# C9 coverage — mark_reviewed, read-time reviewed_by union
# ---------------------------------------------------------------------------


def test_mark_reviewed_after_entry_is_visible_on_read(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd)
    assert store.mark_reviewed("hnd-a", ["sha1"], "reviewer-1", cwd=cwd)
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["kind"] == "code"
    assert entries[0]["reviewed_by"] == ["reviewer-1"]


def test_mark_reviewed_multiple_reviewers_union_dedup(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd)
    store.mark_reviewed("hnd-a", ["sha1"], "reviewer-1", cwd=cwd)
    store.mark_reviewed("hnd-a", ["sha1"], "reviewer-2", cwd=cwd)
    store.mark_reviewed("hnd-a", ["sha1"], "reviewer-1", cwd=cwd)  # duplicate mark
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert entries[0]["reviewed_by"] == ["reviewer-1", "reviewer-2"]


def test_mark_reviewed_multiple_shas_marks_each(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd)
    store.append_entry("hnd-a", "sha2", "doc", cwd=cwd)
    assert store.mark_reviewed("hnd-a", ["sha1", "sha2"], "reviewer-1", cwd=cwd)
    entries = {e["sha"]: e for e in store.read_entries("hnd-a", cwd=cwd)}
    assert entries["sha1"]["reviewed_by"] == ["reviewer-1"]
    assert entries["sha2"]["reviewed_by"] == ["reviewer-1"]


def test_mark_reviewed_before_entry_still_unions_once_entry_lands(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    # Fails safe: marking a sha the ledger has not seen yet is not an
    # error (no verification machinery, Anti-scope) — it simply appends
    # a mark-only line that unions in once the real entry exists.
    assert store.mark_reviewed("hnd-a", ["sha1"], "reviewer-1", cwd=cwd)
    store.append_entry("hnd-a", "sha1", "code", cwd=cwd)
    entries = store.read_entries("hnd-a", cwd=cwd)
    assert len(entries) == 1
    assert entries[0]["reviewed_by"] == ["reviewer-1"]


def test_mark_reviewed_empty_args_returns_false(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    assert store.mark_reviewed("", ["sha1"], "reviewer-1", cwd=cwd) is False
    assert store.mark_reviewed("hnd-a", [], "reviewer-1", cwd=cwd) is False
    assert store.mark_reviewed("hnd-a", ["sha1"], "", cwd=cwd) is False


# ---------------------------------------------------------------------------
# C2 coverage — read_chain, archive-aware, pointer-driven
# ---------------------------------------------------------------------------


def test_read_chain_single_baton_no_ancestor(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-leaf", "sha1", "code", cwd=cwd)
    chain = store.read_chain("hnd-leaf", cwd=cwd)
    assert [e["sha"] for e in chain] == ["sha1"]


def test_read_chain_follows_pointer_across_archive(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    # Predecessor archived under a month-nested archive/handoffs/ dir —
    # exercising the 153/154 archive-not-state split the plan measured.
    pred_path = repo / "archive" / "handoffs" / "2026-07" / "2026-07-01-predecessor.md"
    _write_handoff(pred_path, handoff_id="hnd-pred")

    store.append_entry("hnd-pred", "sha-old", "code", cwd=cwd)
    store.append_entry("hnd-succ", "sha-new", "code", cwd=cwd)

    ok = store.record_predecessor_pointer("hnd-succ", "hnd-pred", repo_root=str(repo), cwd=cwd)
    assert ok

    chain = store.read_chain("hnd-succ", cwd=cwd)
    shas = {e["sha"] for e in chain}
    assert shas == {"sha-new", "sha-old"}


def test_read_chain_pointer_by_path_form(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    pred_path = repo / "archive" / "handoffs" / "2026-07" / "2026-07-02-predecessor.md"
    _write_handoff(pred_path, handoff_id="hnd-pred2")
    store.append_entry("hnd-pred2", "sha-p", "code", cwd=cwd)
    store.append_entry("hnd-s2", "sha-s", "code", cwd=cwd)

    rel = str(pred_path.relative_to(repo)).replace("\\", "/")
    ok = store.record_predecessor_pointer("hnd-s2", rel, repo_root=str(repo), cwd=cwd)
    assert ok

    chain = store.read_chain("hnd-s2", cwd=cwd)
    assert {e["sha"] for e in chain} == {"sha-s", "sha-p"}


def test_read_chain_legacy_fallback_no_pointer(tmp_path):
    """A baton minted before the pointer existed still resolves via the
    bounded legacy fallback (state/handoffs frontmatter, no pointer sidecar)."""
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    succ_path = repo / "state" / "handoffs" / "successor.md"
    _write_handoff(succ_path, handoff_id="hnd-legacy-succ", predecessor_id="hnd-legacy-pred")

    store.append_entry("hnd-legacy-pred", "sha-old", "code", cwd=cwd)
    store.append_entry("hnd-legacy-succ", "sha-new", "code", cwd=cwd)

    chain = store.read_chain("hnd-legacy-succ", cwd=cwd)
    assert {e["sha"] for e in chain} == {"sha-new", "sha-old"}


def test_read_chain_legacy_hop_cap_stops_walk(tmp_path):
    """A chain of 5 consecutive pointer-less (legacy) ancestors must be
    walked only up to `_MAX_LEGACY_FALLBACK_HOPS` (3) hops — AC18's bound
    stated by the docstring must actually stop the walk, and
    `legacy_hops_used` must count exactly what got walked, not the whole
    chain."""
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    # hnd-l0 -> hnd-l1 -> hnd-l2 -> hnd-l3 -> hnd-l4, each a legacy
    # (no predecessor-pointer sidecar) frontmatter-only edge.
    depth = 5
    ids = [f"hnd-l{i}" for i in range(depth)]
    for i, hid in enumerate(ids):
        store.append_entry(hid, f"sha-l{i}", "code", cwd=cwd)

    for i in range(depth - 1):
        succ_path = repo / "state" / "handoffs" / f"{ids[i]}.md"
        _write_handoff(succ_path, handoff_id=ids[i], predecessor_id=ids[i + 1])
    # Leaf ancestor also needs an on-disk file so the resolver can find it,
    # even though it has no further predecessor.
    _write_handoff(repo / "state" / "handoffs" / f"{ids[-1]}.md", handoff_id=ids[-1])

    chain = store.read_chain(ids[0], cwd=cwd)
    shas = {e["sha"] for e in chain}

    # Own entry + at most _MAX_LEGACY_FALLBACK_HOPS ancestor entries.
    assert shas == {f"sha-l{i}" for i in range(store._MAX_LEGACY_FALLBACK_HOPS + 1)}
    assert len(shas) == store._MAX_LEGACY_FALLBACK_HOPS + 1


def test_read_chain_terminates_on_cycle(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-x", "sha-x", "code", cwd=cwd)
    store.append_entry("hnd-y", "sha-y", "code", cwd=cwd)

    # Manually craft a pointer cycle x -> y -> x.
    px = store._predecessor_pointer_path("hnd-x", cwd=cwd)
    py = store._predecessor_pointer_path("hnd-y", cwd=cwd)
    px.parent.mkdir(parents=True, exist_ok=True)
    px.write_text(json.dumps({"predecessor_id": "hnd-y"}), encoding="utf-8")
    py.write_text(json.dumps({"predecessor_id": "hnd-x"}), encoding="utf-8")

    chain = store.read_chain("hnd-x", cwd=cwd)
    # Must terminate (not hang) and must not duplicate either baton's entry.
    assert {e["sha"] for e in chain} == {"sha-x", "sha-y"}


def test_read_chain_deep_synthetic_depth(tmp_path):
    """Synthetic depth-10 chain, per the plan's explicit instruction not to
    validate only at today's depth-1/2 corpus shape."""
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    depth = 10
    ids = [f"hnd-d{i}" for i in range(depth)]
    for i, hid in enumerate(ids):
        store.append_entry(hid, f"sha-{i}", "code", cwd=cwd)
    for i in range(depth - 1):
        # ids[i] continues FROM ids[i + 1] (successor -> predecessor).
        ok = store.record_predecessor_pointer(ids[i], ids[i + 1], repo_root=str(repo), cwd=cwd)
        # No on-disk predecessor handoff file exists for these synthetic ids,
        # so record_predecessor_pointer cannot resolve a file and returns
        # False; fall back to hand-writing the sidecar directly, which is
        # exactly what a successful resolution would have produced.
        if not ok:
            path = store._predecessor_pointer_path(ids[i], cwd=cwd)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"predecessor_id": ids[i + 1]}), encoding="utf-8")

    chain = store.read_chain(ids[0], cwd=cwd)
    assert {e["sha"] for e in chain} == {f"sha-{i}" for i in range(depth)}


def test_read_chain_ac9_zero_subprocess_zero_interpreter(tmp_path, monkeypatch):
    """AC9: reading a chain spawns zero git subprocesses and starts zero
    nested interpreters — a RUNTIME test patching the subprocess/interpreter
    seam and counting invocations, per the C2 brief (the static
    per-item-loop AST amplification gate does not cover this non-loop
    path)."""
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    pred_path = repo / "archive" / "handoffs" / "2026-07" / "2026-07-03-pred.md"
    _write_handoff(pred_path, handoff_id="hnd-pred9")
    store.append_entry("hnd-pred9", "sha-old", "code", cwd=cwd)
    store.append_entry("hnd-succ9", "sha-new", "code", cwd=cwd)
    store.record_predecessor_pointer("hnd-succ9", "hnd-pred9", repo_root=str(repo), cwd=cwd)

    calls = {"subprocess": 0, "popen": 0}

    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _counting_run(*args, **kwargs):
        calls["subprocess"] += 1
        return real_run(*args, **kwargs)

    def _counting_popen(*args, **kwargs):
        calls["popen"] += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    monkeypatch.setattr(subprocess, "Popen", _counting_popen)

    chain = store.read_chain("hnd-succ9", cwd=cwd)

    assert {e["sha"] for e in chain} == {"sha-new", "sha-old"}
    assert calls["subprocess"] == 0
    assert calls["popen"] == 0


def test_read_chain_empty_handoff_id(tmp_path):
    assert store.read_chain("", cwd=str(tmp_path)) == []


def test_record_predecessor_pointer_unresolvable_returns_false(tmp_path):
    repo = _init_git_repo(tmp_path)
    assert store.record_predecessor_pointer(
        "hnd-a", "hnd-does-not-exist-anywhere", repo_root=str(repo), cwd=str(repo)
    ) is False
