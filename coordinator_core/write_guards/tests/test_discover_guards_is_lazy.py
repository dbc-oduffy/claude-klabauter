"""Pins C10 (docs/plans/2026-08-22-the-import-path-costs-nothing.md):
`engine._discover_guards()` no longer imports every guard module up front —
it reads `CLASS`/`MATCHERS`/`PRIORITY` off each module's SOURCE via
`ast.parse` (`_cheap_guard_metadata`, stage one) and defers the real
`importlib.import_module` (`_make_lazy_check`, stage two) until `evaluate()`
actually reaches that guard's `check()` in PRIORITY order.

Covers:
  (a) a guard past the winning hard-deny is never imported for that call
      (observed directly via `sys.modules`, not inferred from timing).
  (b) same, for the first-wins advisory phase (`aggregate=False`).
  (c) `_cheap_guard_metadata` correctly reads a real guard module's
      literal `CLASS`/`MATCHERS`/`PRIORITY` without adding it to
      `sys.modules`.
  (d) `discover_guard_names()` (full introspection, `_discover_guards_full`)
      is untouched by the lazy hot path and still surfaces every guard —
      the manifest test (`test_guard_registry_manifest.py`) already pins
      this; this module only asserts the two functions remain independent.
  (e) an import failure in a guard the call never reaches is not surfaced
      in that call's `skipped_out` — the direct, documented consequence of
      going lazy (negative-spec).
"""

from __future__ import annotations

import sys

from coordinator_core.write_guards import engine


def _payload(tool_name="Write", file_path="/repo/some_file.py", session_id=""):
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    }


def _module_key(name: str) -> str:
    return f"coordinator_core.write_guards.{name}"


def _unload(name: str) -> None:
    sys.modules.pop(_module_key(name), None)


class TestHardDenyShortCircuitStopsImporting:
    def test_guard_past_the_winning_hard_deny_is_never_imported(self, monkeypatch):
        # block_illegal_filename (PRIORITY 20) fires before
        # block_confined_agent_write (PRIORITY 48) in real PRIORITY order --
        # pick two real, already-shipped guard modules so this exercises the
        # actual AST metadata scan, not a fake _Guard.
        _unload("block_illegal_filename")
        _unload("block_confined_agent_write")

        payload = _payload(file_path="/repo/bad:name.txt")  # NTFS-illegal ':' char
        out = engine.evaluate(payload)

        assert out is not None, "expected block_illegal_filename to fire"
        assert _module_key("block_illegal_filename") in sys.modules
        assert _module_key("block_confined_agent_write") not in sys.modules, (
            "a guard past the winning hard-deny (higher PRIORITY number) "
            "must never be imported for this call"
        )


class TestAdvisoryFirstWinsStopsImporting:
    def test_lower_priority_advisory_never_imported_once_one_fires(self, monkeypatch):
        calls = []

        def _fake_metadata(name):
            if name == "g-high":
                return ("advisory", ["Write"], 10)
            if name == "g-low":
                return ("advisory", ["Write"], 20)
            return None

        def _fake_iter_modules(path):
            class _Info:
                def __init__(self, name):
                    self.name = name

            return [_Info("g-high"), _Info("g-low")]

        def _fake_import(dotted):
            name = dotted.rsplit(".", 1)[-1]
            calls.append(name)

            class _Mod:
                @staticmethod
                def check(payload):
                    return {"hookSpecificOutput": {"additionalContext": f"fired:{name}"}}

            return _Mod()

        monkeypatch.setattr(engine, "_cheap_guard_metadata", _fake_metadata)
        monkeypatch.setattr(engine.pkgutil, "iter_modules", _fake_iter_modules)
        monkeypatch.setattr(engine.importlib, "import_module", _fake_import)

        out = engine.evaluate(_payload())

        assert out == {"hookSpecificOutput": {"additionalContext": "fired:g-high"}}
        assert calls == ["g-high"], "g-low must never be imported once g-high fires first"


class TestCheapMetadataReadsRealGuardWithoutImporting:
    def test_reads_class_matchers_priority_from_source_only(self):
        _unload("block_illegal_filename")

        meta = engine._cheap_guard_metadata("block_illegal_filename")

        assert meta is not None
        cls, matchers, priority = meta
        assert cls == "hard-deny"
        assert "Write" in matchers
        assert isinstance(priority, int)
        assert _module_key("block_illegal_filename") not in sys.modules, (
            "reading metadata via AST must not import the module"
        )


class TestFullDiscoveryUnaffectedByLazyHotPath:
    def test_discover_guard_names_still_uses_the_full_eager_path(self, monkeypatch):
        calls = []
        real_full = engine._discover_guards_full

        def _spy():
            calls.append(1)
            return real_full()

        monkeypatch.setattr(engine, "_discover_guards_full", _spy)

        names, import_failed = engine.discover_guard_names()

        assert calls == [1]
        assert "block_illegal_filename" in names
        assert import_failed == []


class TestSkippedOutOnlyReflectsGuardsActuallyReached:
    def test_import_failure_past_a_winning_hard_deny_is_not_surfaced(self, monkeypatch):
        def _fake_metadata(name):
            if name == "g-deny":
                return ("hard-deny", ["Write"], 5)
            if name == "g-broken":
                return ("hard-deny", ["Write"], 50)
            return None

        def _fake_iter_modules(path):
            class _Info:
                def __init__(self, name):
                    self.name = name

            return [_Info("g-deny"), _Info("g-broken")]

        def _fake_import(dotted):
            name = dotted.rsplit(".", 1)[-1]
            if name == "g-broken":
                raise ImportError("boom")

            class _Mod:
                @staticmethod
                def check(payload):
                    return {
                        "hookSpecificOutput": {
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "denied",
                        }
                    }

            return _Mod()

        monkeypatch.setattr(engine, "_cheap_guard_metadata", _fake_metadata)
        monkeypatch.setattr(engine.pkgutil, "iter_modules", _fake_iter_modules)
        monkeypatch.setattr(engine.importlib, "import_module", _fake_import)

        skipped = []
        out = engine.evaluate(_payload(session_id=""), skipped_out=skipped)

        assert out is not None
        assert skipped == [], (
            "g-broken is short-circuited past by g-deny firing first -- its "
            "import failure must not appear in THIS call's skipped_out"
        )
