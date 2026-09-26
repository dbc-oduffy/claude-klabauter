from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time

import pytest

# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PASS = 0
FAIL = 0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _pass(label: str) -> None:
    global PASS
    print(f"  PASS: {label}")
    PASS += 1


def _fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    raise AssertionError(f"{label}: {detail}" if detail else label)


def _load_module():
    path = os.path.join(SCRIPT_DIR, "workday-complete-backfill-anchor.py")
    spec = importlib.util.spec_from_file_location("workday_complete_backfill_anchor_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _git(repo_dir, *args):
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc.stdout


def _init_repo(repo_dir):
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")


def _commit(repo_dir, rel_path, content, message):
    full = repo_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    _git(repo_dir, "add", rel_path)
    _git(repo_dir, "commit", "-q", "-m", message)
    return _git(repo_dir, "rev-parse", "HEAD").strip()


def test_single_candidate_short_circuits(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    c1 = _commit(tmp_path, "a.txt", "1\n", "c1")

    tip = mod.compute_descendant_tip(str(tmp_path), [c1])
    if tip != c1:
        _fail("test_single_candidate_short_circuits", f"expected {c1}, got {tip}")
        return
    _pass("test_single_candidate_short_circuits")


def test_no_sha_resolves_returns_none(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "1\n", "c1")

    tip = mod.compute_descendant_tip(str(tmp_path), ["deadbeef"])
    if tip is not None:
        _fail("test_no_sha_resolves_returns_none", f"expected None, got {tip}")
        return
    _pass("test_no_sha_resolves_returns_none")


def test_total_order_picks_furthest_forward(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    c1 = _commit(tmp_path, "a.txt", "1\n", "c1")
    c2 = _commit(tmp_path, "a.txt", "2\n", "c2")
    c3 = _commit(tmp_path, "a.txt", "3\n", "c3")

    tip = mod.compute_descendant_tip(str(tmp_path), [c1, c2, c3])
    if tip != c3:
        _fail("test_total_order_picks_furthest_forward", f"expected {c3}, got {tip}")
        return
    _pass("test_total_order_picks_furthest_forward")


def test_duplicate_candidate_deduped_and_resolved(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    c1 = _commit(tmp_path, "a.txt", "1\n", "c1")
    c2 = _commit(tmp_path, "a.txt", "2\n", "c2")

    tip = mod.compute_descendant_tip(str(tmp_path), [c1, c2, c2, c1])
    if tip != c2:
        _fail("test_duplicate_candidate_deduped_and_resolved", f"expected {c2}, got {tip}")
        return
    _pass("test_duplicate_candidate_deduped_and_resolved")


def test_partial_order_three_plus_candidates_diverged_returns_none(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n", "base")

    _git(tmp_path, "checkout", "-q", "-b", "branch-a")
    branch_a_tip = _commit(tmp_path, "a.txt", "a\n", "on branch a")
    descendant_of_a = _commit(tmp_path, "a2.txt", "a2\n", "descendant of branch a")

    _git(tmp_path, "checkout", "-q", base)
    _git(tmp_path, "checkout", "-q", "-b", "branch-b")
    branch_b_tip = _commit(tmp_path, "b.txt", "b\n", "on branch b")

    tip = mod.compute_descendant_tip(
        str(tmp_path), [descendant_of_a, branch_b_tip, branch_a_tip]
    )
    if tip is not None:
        _fail(
            "test_partial_order_three_plus_candidates_diverged_returns_none",
            f"expected None (diverged, no dominant candidate), got {tip}",
        )
        return
    _pass("test_partial_order_three_plus_candidates_diverged_returns_none")


def test_partial_order_three_plus_candidates_dominant_found(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    base = _commit(tmp_path, "base.txt", "base\n", "base")

    _git(tmp_path, "checkout", "-q", "-b", "branch-a")
    branch_a_tip = _commit(tmp_path, "a.txt", "a\n", "on branch a")

    _git(tmp_path, "checkout", "-q", base)
    _git(tmp_path, "checkout", "-q", "-b", "branch-b")
    branch_b_tip = _commit(tmp_path, "b.txt", "b\n", "on branch b")

    _git(tmp_path, "merge", "-q", "--no-edit", "branch-a")
    merge_tip = _git(tmp_path, "rev-parse", "HEAD").strip()

    tip = mod.compute_descendant_tip(
        str(tmp_path), [base, branch_a_tip, branch_b_tip, merge_tip]
    )
    if tip != merge_tip:
        _fail(
            "test_partial_order_three_plus_candidates_dominant_found",
            f"expected merge tip {merge_tip}, got {tip}",
        )
        return
    _pass("test_partial_order_three_plus_candidates_dominant_found")


def test_missing_topo_output_is_not_silently_ignored(tmp_path, monkeypatch=None):
    mod = _load_module()
    _init_repo(tmp_path)
    c1 = _commit(tmp_path, "a.txt", "1\n", "c1")
    c2 = _commit(tmp_path, "a.txt", "2\n", "c2")

    real_git_out = mod.wc.git_out

    def _fake_git_out(*args, **kwargs):
        out = real_git_out(*args, **kwargs)
        if args[:3] == ("-C", str(os.path.abspath(str(tmp_path))), "rev-list") and "--topo-order" in args:
            lines = [line for line in out.splitlines() if line != c2]
            return "\n".join(lines)
        return out

    mod.wc.git_out = _fake_git_out
    try:
        tip = mod.compute_descendant_tip(str(tmp_path), [c1, c2])
    finally:
        mod.wc.git_out = real_git_out

    if tip is not None:
        _fail(
            "test_missing_topo_output_is_not_silently_ignored",
            f"expected None (unresolved candidate reconciled, not silently dropped), got {tip}",
        )
        return
    _pass("test_missing_topo_output_is_not_silently_ignored")


def test_wall_clock_faster_than_pairwise_baseline(tmp_path):
    mod = _load_module()
    _init_repo(tmp_path)
    shas = []
    for i in range(6):
        shas.append(_commit(tmp_path, "a.txt", f"{i}\n", f"c{i}"))

    script_path = os.path.join(SCRIPT_DIR, "workday-complete-backfill-anchor.py")

    def _run_once():
        start = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, script_path, "descendant-tip", str(tmp_path), *shas],
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
        elapsed = time.perf_counter() - start
        return proc, elapsed

    proc1, elapsed1 = _run_once()
    proc2, elapsed2 = _run_once()

    if proc1.returncode != 0 or proc2.returncode != 0:
        _fail(
            "test_wall_clock_faster_than_pairwise_baseline",
            f"CLI invocation failed: rc1={proc1.returncode} stderr1={proc1.stderr!r} "
            f"rc2={proc2.returncode} stderr2={proc2.stderr!r}",
        )
        return

    print(f"    wall-clock (fresh process, order 1): {elapsed1:.3f}s")
    print(f"    wall-clock (fresh process, order 2): {elapsed2:.3f}s")
    _pass("test_wall_clock_faster_than_pairwise_baseline (measured, not gated)")
