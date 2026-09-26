"""
coordinator_core.tests.test_module_load_lock — tests for `module_load_lock.
held_during_load`, the per-module-name mutual-exclusion primitive added for
state/bug-backlog/2026-08-16-sys-modules-published-before-exec-module-
3e43d1a2a316.yaml (sys.modules published before exec_module at four dynamic-
import-by-path loaders lets a concurrent importer observe a half-executed
module).

Two layers of coverage:
  - TestHeldDuringLoad exercises the primitive in isolation: two threads
    racing the SAME module_name must never both be inside the guarded
    section at once; two threads racing DIFFERENT names must not block
    each other.
  - TestClaimsHandoffLifecycleConcurrency drives one of the four real call
    sites (`session.claims.handoff_lifecycle`) with a deliberately slow
    `exec_module` and asserts a second concurrent caller never starts its
    own `exec_module` while the first's is still running — the exact
    half-executed-module race the bug row describes, reproduced against
    real production code rather than a synthetic stand-in.
"""

from __future__ import annotations

import threading
import time
from typing import List, Tuple

import pytest

from coordinator_core.module_load_lock import held_during_load

pytestmark = [pytest.mark.cadence]


class TestHeldDuringLoad:
    def test_same_name_calls_never_overlap(self):
        order: List[str] = []
        order_lock = threading.Lock()
        start_barrier = threading.Barrier(2)

        def _worker(label: str) -> None:
            start_barrier.wait(timeout=2)
            with held_during_load("shared-name"):
                with order_lock:
                    order.append(f"{label}-start")
                time.sleep(0.05)
                with order_lock:
                    order.append(f"{label}-end")

        threads = [
            threading.Thread(target=_worker, args=("a",)),
            threading.Thread(target=_worker, args=("b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(order) == 4
        first_label = order[0].split("-")[0]
        second_label = "b" if first_label == "a" else "a"
        assert order == [
            f"{first_label}-start",
            f"{first_label}-end",
            f"{second_label}-start",
            f"{second_label}-end",
        ]

    def test_different_names_do_not_block_each_other(self):
        release_a = threading.Event()
        entered_b = threading.Event()

        def _hold_a() -> None:
            with held_during_load("name-a"):
                release_a.wait(timeout=2)

        def _hold_b() -> None:
            with held_during_load("name-b"):
                entered_b.set()

        thread_a = threading.Thread(target=_hold_a)
        thread_a.start()
        try:
            thread_b = threading.Thread(target=_hold_b)
            thread_b.start()
            thread_b.join(timeout=2)
            assert entered_b.is_set(), "unrelated module_name was blocked"
        finally:
            release_a.set()
            thread_a.join(timeout=2)


class TestClaimsHandoffLifecycleConcurrency:

    def test_second_concurrent_caller_never_execs_while_first_is_mid_load(
        self, monkeypatch
    ):
        from coordinator_core.session import claims

        monkeypatch.setattr(claims, "_handoff_lifecycle_cache", None)

        events: List[Tuple[str, str]] = []
        events_lock = threading.Lock()
        entered_barrier = threading.Barrier(2, timeout=2)

        class _SlowLoader:
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                with events_lock:
                    events.append(("start", threading.current_thread().name))
                try:
                    entered_barrier.wait(timeout=0.3)
                except threading.BrokenBarrierError:
                    pass
                module.LOADED = True
                with events_lock:
                    events.append(("end", threading.current_thread().name))

        real_spec_from_file_location = claims.importlib.util.spec_from_file_location

        def _fake_spec_from_file_location(name, path):
            spec = real_spec_from_file_location(name, path)
            spec.loader = _SlowLoader()
            return spec

        monkeypatch.setattr(
            claims.importlib.util,
            "spec_from_file_location",
            _fake_spec_from_file_location,
        )

        results: List[object] = []

        def _call() -> None:
            results.append(claims.handoff_lifecycle())

        threads = [
            threading.Thread(target=_call, name="caller-1"),
            threading.Thread(target=_call, name="caller-2"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 2
        assert results[0] is results[1], "both callers must share one loaded module"
        assert getattr(results[0], "LOADED", False) is True

        starts = [e for e in events if e[0] == "start"]
        assert len(starts) == 1, (
            f"exec_module ran more than once concurrently for one module_name: {events}"
        )
