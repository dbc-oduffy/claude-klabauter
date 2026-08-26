"""test_cc_invoke_delegate_to_gate_by_root.py — C0 pin: `_delegate_to_gate`
disambiguates by ROOT, not by module name.

Chunk: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C0

THE DEFECT this pins against: `_delegate_to_gate` did
`sys.path.insert(0, candidate)` then
`from coordinator_core.<name> import <entry>`. That was safe only because two
trees spelled `<name>` differently. Once both spell it the same, the SECOND
call in one process would get the FIRST call's cached `sys.modules` entry
regardless of which candidate it was asked about — a silent wrong-root
answer, not a raise.

This test builds TWO synthetic candidate roots in one interpreter, each
defining its own `coordinator_core/claude_klabauter_root.py` with a
`coordinator_claude_klabauter_root_with_class` returning a distinguishable answer, and
calls `_delegate_to_gate` on each in turn (SAME process, so any module-name
caching defect is directly observable — no subprocess needed here, unlike the
self-location-rung tests, because the property under test IS in-process
`sys.modules` reuse).

Run: pytest coordinator/bin/tests/test_cc_invoke_delegate_to_gate_by_root.py -q
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)


def _build_candidate_root(root: Path, *, answer: str) -> Path:
    """Build a minimal synthetic checkout at `root`: a real `coordinator_core`
    package (so it's importable) with an `engine_root.py` whose
    `coordinator_engine_root_with_class()` returns a distinguishable,
    caller-supplied `answer` — this is what makes cross-root confusion
    observable rather than silently masked by both candidates agreeing.

    THE SPELLING HERE IS LOAD-BEARING, not cosmetic. This fixture built
    `claude_klabauter_root.py` until C1 renamed the module, and a stale fixture does not
    fail loudly — it fails INERT. `_load_foreign_gate_entry_point` probes
    `<candidate>/coordinator_core/engine_root.py` first, so an old-spelling
    candidate misses that probe, `_is_same_tree_as_canonical` sends the call
    down the same-tree short-circuit instead, and the real engine's gate
    answers from the ambient environment. The assertions below then compare a
    resolved PATH against an answer sentinel and fail for a reason that has
    nothing to do with root identity — while the C0 mechanism this module
    exists to guard is never entered at all. Measured 2026-08-20: with this
    spelling the foreign loader runs and the candidate is honoured; with the
    pre-C1 spelling it is never called.
    """
    pkg_dir = root / "coordinator_core"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "engine_root.py").write_text(
        "def coordinator_engine_root_with_class():\n"
        f"    return ({answer!r}, 'live-working-tree')\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _clean_sys_state():
    """Isolate this module's mutations of sys.path / sys.modules from other
    test files sharing the same interpreter (pytest runs tests in one
    process). Snapshots and restores both around every test in this file.
    """
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    yield
    sys.path[:] = path_before
    for name in set(sys.modules) - modules_before:
        del sys.modules[name]


def test_second_call_returns_second_roots_own_answer_not_the_first(monkeypatch, tmp_path):
    """THE PIN. Two genuinely different candidate roots, same process, same
    (pre-rename) module spelling `coordinator_core.claude_klabauter_root`. The second
    `_resolve_claude_klabauter_root()` call (reached via Rung 1's engine-root env
    candidate — the real production entry to `_delegate_to_gate`) must return
    the SECOND root's own answer, not the first call's cached module's.

    C14 (docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md)
    closed the dual-read window: `_resolve_claude_klabauter_root()`'s Rung 1 now reads
    ONLY `COORDINATOR_ENGINE_ROOT` (see this module's own C14 comment above
    the `existing = os.environ.get(_ENGINE_ROOT_NEW_VAR, "")` line) — the old
    `CLAUDE_KLABAUTER_ROOT` name no longer supplies the Rung 1 candidate at all. Setting
    it here would fall through Rung 1 to the real ambient registry/pointer
    rungs instead of the synthetic candidate root, which is exactly what this
    test measured (an ambient published-engine path in place of
    `root-a-answer`) before the env var was switched. Using the surviving name
    reaches
    `_delegate_to_gate` on the synthetic roots as before, so the module-name
    cache collision C0 fixes is still directly observable.

    Must FAIL against the pre-C0 bare `importlib.import_module` implementation:
    the first call caches `sys.modules["coordinator_core.claude_klabauter_root"]` from
    root A; the second call's `from coordinator_core.claude_klabauter_root import ...`
    is satisfied from that cache and answers with A's function object, which
    (because A's answer is a closed-over literal) still returns A's own
    'root-A-answer' string even though root B was asked.
    """
    root_a = _build_candidate_root(tmp_path / "root-a", answer="root-a-answer")
    root_b = _build_candidate_root(tmp_path / "root-b", answer="root-b-answer")

    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(root_a))
    first = _mod._resolve_claude_klabauter_root()
    assert first == "root-a-answer"

    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(root_b))
    second = _mod._resolve_claude_klabauter_root()
    assert second == "root-b-answer", (
        f"expected the second call to answer from root B ({root_b!r}), "
        f"got {second!r} — this is the module-name-cache collision C0 fixes"
    )


def test_same_tree_short_circuit_returns_canonical_module_not_a_synthetic_copy(monkeypatch):
    """Pins the load-bearing short-circuit: a candidate that resolves to the
    SAME root `coordinator_core` is already cached from must go through the
    ordinary `importlib.import_module` path and return the CANONICAL
    `coordinator_core.engine_root` module object — not a synthetic
    file-path-loaded copy — so the `_GATE_MEMO`/`_reset_*_memo` seams keep
    observing the one module object they always did.
    """
    canonical = sys.modules.get("coordinator_core")
    if canonical is None or not getattr(canonical, "__file__", None):
        pytest.skip("coordinator_core is not ambiently importable with a real __file__ in this env")
    canonical_root = str(Path(canonical.__file__).resolve().parents[1])

    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", canonical_root)
    _mod._resolve_claude_klabauter_root()

    # No synthetic "_cc_engine_root_<digest>" module should exist for the
    # same-tree candidate — the short-circuit must never take the
    # foreign-candidate file-path-load branch.
    synthetic = [name for name in sys.modules if name.startswith("_cc_engine_root_")]
    assert not synthetic, (
        f"same-tree candidate took the foreign-candidate load path, creating "
        f"synthetic module(s) {synthetic!r} instead of using the short-circuit"
    )
    assert "coordinator_core.engine_root" in sys.modules, (
        "expected the ordinary import to populate the canonical "
        "coordinator_core.engine_root module entry"
    )
