"""
coordinator_core.tests.test_cache_coherency — Tests for cache.py primitives and
dag._read_meta content-hash re-key (C3, R5 same-second mtime staleness fix).

AC10 (load-bearing): Verifies that a same-mtime body-change is detected and
recomputed correctly by _read_meta, not served stale from cache.

AC13 (micro-benchmark): 100+ synthetic linked handoff files traversed via
walk_forward within a generous latency budget — regression guard for the
sub-10ms SLA under dag._read_meta content-hash cost.

Spec backlink: pln-tri-plane-boundary-claude-klabauter-side-landing-c-b393a7 § C3
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.cache import compute_stamp, read_revalidated, _REVALIDATED_CACHE
from coordinator_core import dag


# ---------------------------------------------------------------------------
# Fixture: clear dag._FRONTMATTER_CACHE between every test case so module-level
# cache state cannot falsely pass AC10 (a persistent cache would return the old
# parsed result even after the body changed, masking the re-key bug).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """Clear dag._FRONTMATTER_CACHE and cache._REVALIDATED_CACHE before each test."""
    dag._FRONTMATTER_CACHE.clear()
    _REVALIDATED_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()
    _REVALIDATED_CACHE.clear()


# ---------------------------------------------------------------------------
# compute_stamp — determinism and change-sensitivity
# ---------------------------------------------------------------------------

class TestComputeStamp:
    def test_deterministic_same_content(self, tmp_path: Path):
        """compute_stamp returns the same hex digest for the same file content."""
        f = tmp_path / "a.md"
        f.write_bytes(b"hello world")
        s1 = compute_stamp(f)
        s2 = compute_stamp(f)
        assert s1 == s2

    def test_changes_when_body_changes(self, tmp_path: Path):
        """compute_stamp returns a different digest when the file body changes."""
        f = tmp_path / "b.md"
        f.write_bytes(b"body v1")
        s1 = compute_stamp(f)
        f.write_bytes(b"body v2")
        s2 = compute_stamp(f)
        assert s1 != s2

    def test_hex_digest_length(self, tmp_path: Path):
        """compute_stamp returns a 64-char hex string (sha256)."""
        f = tmp_path / "c.md"
        f.write_bytes(b"test")
        s = compute_stamp(f)
        assert len(s) == 64
        assert all(c in "0123456789abcdef" for c in s)

    def test_empty_file(self, tmp_path: Path):
        """compute_stamp handles empty files without error."""
        f = tmp_path / "empty.md"
        f.write_bytes(b"")
        s = compute_stamp(f)
        assert len(s) == 64

    def test_accepts_str_path(self, tmp_path: Path):
        """compute_stamp accepts a string path as well as a Path object."""
        f = tmp_path / "str.md"
        f.write_bytes(b"str path test")
        s_path = compute_stamp(f)
        s_str = compute_stamp(str(f))
        assert s_path == s_str


# ---------------------------------------------------------------------------
# read_revalidated — cold compute / cache hit / body-change recompute
# ---------------------------------------------------------------------------

class TestReadRevalidated:
    def test_cold_compute(self, tmp_path: Path):
        """read_revalidated calls compute_fn on a cache miss."""
        f = tmp_path / "cold.md"
        f.write_bytes(b"alpha")
        calls = []

        def _fn(p: Path) -> str:
            calls.append(str(p))
            return p.read_text()

        result = read_revalidated(f, _fn)
        assert result == "alpha"
        assert len(calls) == 1

    def test_cache_hit_no_recompute(self, tmp_path: Path):
        """read_revalidated returns cached value when body is unchanged."""
        f = tmp_path / "cached.md"
        f.write_bytes(b"beta")
        calls = []

        def _fn(p: Path) -> str:
            calls.append(str(p))
            return p.read_text()

        read_revalidated(f, _fn)
        read_revalidated(f, _fn)
        # compute_fn called only once — second call is a cache hit
        assert len(calls) == 1

    def test_recomputes_on_body_change(self, tmp_path: Path):
        """read_revalidated calls compute_fn again after body changes."""
        f = tmp_path / "mutate.md"
        f.write_bytes(b"v1")
        results = []

        def _fn(p: Path) -> str:
            v = p.read_text()
            results.append(v)
            return v

        r1 = read_revalidated(f, _fn)
        assert r1 == "v1"

        f.write_bytes(b"v2")
        r2 = read_revalidated(f, _fn)
        assert r2 == "v2"
        assert len(results) == 2


# ---------------------------------------------------------------------------
# AC10 (LOAD-BEARING): same-mtime body change is detected by dag._read_meta
# ---------------------------------------------------------------------------

def _make_handoff(path: Path, predecessor: str = "none", status: str = "open",
                  slug: str = "test-slug") -> None:
    """Write a minimal handoff frontmatter file to path."""
    path.write_text(
        f"---\n"
        f"slug: {slug}\n"
        f"status: {status}\n"
        f"predecessor: {predecessor}\n"
        f"---\n"
        f"# Handoff body\n"
    )


class TestAC10MtimeEqual:
    def test_same_mtime_body_change_detected(self, tmp_path: Path):
        """AC10: body change is detected even when mtime is held constant.

        This is the R5 same-second mtime staleness hazard:
        - Write file with content A, read mtime t.
        - Mutate file body to content B, restore mtime to t.
        - With mtime-keyed cache: second _read_meta returns stale A (BUG).
        - With content-hash-keyed cache: second _read_meta returns fresh B (CORRECT).
        """
        hf = tmp_path / "orient.md"
        _make_handoff(hf, slug="orient-slug", status="open")

        # First read — populates cache
        meta_before = dag._read_meta(str(hf))
        assert meta_before.get("status") == "open"

        # Capture original mtime
        stat = hf.stat()
        original_mtime = (stat.st_atime, stat.st_mtime)

        # Mutate the body — status changes to claimed
        hf.write_text(
            "---\n"
            "slug: orient-slug\n"
            "status: claimed\n"
            "predecessor: none\n"
            "---\n"
            "# Handoff body updated\n"
        )

        # Restore mtime to original (defeats mtime-keyed cache)
        os.utime(str(hf), original_mtime)

        # Review: code-reviewer — F1: in-body clear removed; autouse fixture provides clean cache at
        # entry; without this clear a mtime-keyed impl would return a cache hit (stale "open") and
        # FAIL — that is the discrimination the test must provide.

        # Second read — must return the NEW content, not the stale cached value
        meta_after = dag._read_meta(str(hf))
        assert meta_after.get("status") == "claimed", (
            f"Expected 'claimed' after body mutation with mtime held constant, "
            f"got {meta_after.get('status')!r}. This indicates mtime-keyed cache "
            f"is still in use — R5 same-second staleness bug."
        )

    def test_cache_key_is_hash_not_mtime(self, tmp_path: Path):
        """Cache key must differ on body change even with identical mtime."""
        hf = tmp_path / "key-check.md"
        _make_handoff(hf, slug="key-check", status="dispatched")

        # First read — cache the result
        dag._read_meta(str(hf))
        initial_cache_keys = set(dag._FRONTMATTER_CACHE.keys())
        assert len(initial_cache_keys) == 1
        first_key = next(iter(initial_cache_keys))

        # Verify the cache key is (path, hex_str), not (path, float)
        path_part, stamp_part = first_key
        assert isinstance(stamp_part, str), (
            f"Cache key second element should be a string (content-hash), "
            f"got {type(stamp_part).__name__!r}. mtime is still being used."
        )
        assert len(stamp_part) == 64, (
            f"Expected 64-char sha256 hex digest, got {len(stamp_part)} chars."
        )


# ---------------------------------------------------------------------------
# AC13 (MICRO-BENCHMARK): 100+ linked handoff files traversed within budget
# ---------------------------------------------------------------------------

def _write_chain(tmp_path: Path, n: int) -> str:
    """Write n handoff files as a linear predecessor chain.

    handoff_000.md ← handoff_001.md ← … ← handoff_099.md
    Returns the absolute path of the tail (newest) handoff.
    """
    prev = "none"
    last_path = ""
    for i in range(n):
        name = f"handoff_{i:03d}.md"
        path = tmp_path / name
        slug = f"h{i:03d}"
        path.write_text(
            f"---\n"
            f"slug: {slug}\n"
            f"status: claimed\n"
            f"predecessor: {prev}\n"
            f"---\n"
            f"# Handoff {i}\n"
        )
        prev = name
        last_path = str(path)
    return last_path


class TestAC13MicroBenchmark:
    # Review: code-reviewer — F9: slow mark allows `pytest -m "not slow"` for fast-feedback loops
    @pytest.mark.slow
    def test_walk_forward_100_files_within_budget(self, tmp_path: Path):
        """AC13: walk_forward across 100+ linked handoffs completes within per-file budget.

        Regression guard for the sub-10ms per-request SLA, not a hard SLA gate.
        Arithmetic: ≤2ms/file × 5 files (realistic max per coverage-gate request) = ≤10ms.
        Run with `pytest -m "not slow"` to exclude from fast-feedback loops.
        """
        n = 100
        tail_path = _write_chain(tmp_path, n)

        # Clear cache to ensure no warm-cache advantage
        dag._FRONTMATTER_CACHE.clear()

        start = time.perf_counter()
        result = dag.walk_forward(
            tail_path,
            edge_kinds={"predecessor"},
            handoff_dir=str(tmp_path),
        )
        elapsed = time.perf_counter() - start

        # Verify traversal was complete (all nodes visited)
        assert len(result["nodes"]) == n, (
            f"Expected {n} nodes visited, got {len(result['nodes'])}. "
            f"terminatedEarly={result['terminatedEarly']!r}"
        )
        assert result["terminatedEarly"] == "", (
            f"walk_forward terminated early: {result['terminatedEarly']!r}"
        )

        # Review: code-reviewer — F6: per-file assertion tightens SLA guard
        # Arithmetic: ≤2ms/file × 5 files/request = ≤10ms SLA.
        # Widen per_file_budget_ms only with documented arithmetic justification.
        per_file_budget_ms = 2.0
        assert elapsed / n < per_file_budget_ms / 1000, (
            f"Per-file cost {elapsed*1000/n:.2f}ms exceeds {per_file_budget_ms}ms/file budget. "
            f"Arithmetic: {per_file_budget_ms}ms/file × 5 files/request = "
            f"{per_file_budget_ms * 5:.0f}ms < 10ms SLA. "
            f"On slow CI: widen per_file_budget_ms with a documented justification."
        )


class TestEnvelopeCallPathUnchangedByPersistedTier:
    """Pin (op_census C1, PM Ruling 3-C / hard constraint 8): resolvers.py's
    existing `cache.compute_stamp` content-hash call path must stay pure
    in-memory, unchanged semantics and cost, after cache.py grows the
    persisted `read_disk_revalidated` tier. resolvers.py never calls
    `read_disk_revalidated` and never touches an on-disk index — only
    `compute_stamp` (no caching at all) and `_REVALIDATED_CACHE` (via
    `read_revalidated`, unused by resolvers.py today) exist on its path.
    """

    def test_envelope_uses_compute_stamp_not_disk_revalidated(self):
        import ast
        from coordinator_core.ops.emit import envelope as envelope_mod

        source = Path(envelope_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_attrs = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "cache"
        }
        assert called_attrs == {"compute_stamp"}, (
            f"resolvers.py's cache.* call surface changed to {called_attrs!r} -- "
            "expected only compute_stamp. The persisted tier (read_disk_revalidated) "
            "must stay opt-in and unused by this existing caller."
        )

    def test_compute_stamp_still_pure_no_caching(self, tmp_path: Path):
        """compute_stamp itself does not consult or populate any cache --
        unchanged behaviour regardless of the new persisted tier existing
        alongside it in the same module."""
        f = tmp_path / "x.txt"
        f.write_bytes(b"v1")
        s1 = compute_stamp(f)
        f.write_bytes(b"v2")
        s2 = compute_stamp(f)
        assert s1 != s2
        assert _REVALIDATED_CACHE == {}
