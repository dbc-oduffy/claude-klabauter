
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_enumeration_cap_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_KEY] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_KEY, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


class TestCappedJoin:
    def test_under_cap_names_everything_and_adds_no_suffix(self) -> None:
        mod = _require_module()
        assert mod._capped_join(["a", "b"]) == "a, b"

    def test_over_cap_names_cap_items_plus_remainder_count(self) -> None:
        mod = _require_module()
        out = mod._capped_join([f"s{i}" for i in range(223)])
        assert out.startswith("s0, s1, s2, s3, s4")
        assert out.endswith("(+218 more)")
        assert "s5" not in out

    def test_does_not_truncate_an_individual_item(self) -> None:
        mod = _require_module()
        long_id = "x" * 200
        assert long_id in mod._capped_join([long_id])


class TestStablePidMissDetailIsCapped:

    def _stub_scan(self, mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        misses = [
            {"session": f"{i:08d}-aaaa-bbbb-cccc-dddddddddddd", "reason": "no_meta_json"}
            for i in range(223)
        ]
        fake = ModuleType("coordinator_core.session.stable_pid_watch")
        fake.STATUS_MISS = "MISS"  # type: ignore[attr-defined]
        fake.STATUS_CLEAN = "CLEAN"  # type: ignore[attr-defined]
        fake.STATUS_EMPTY = "EMPTY"  # type: ignore[attr-defined]
        fake.scan_stable_pid_misses = lambda *a, **k: {  # type: ignore[attr-defined]
            "status": "MISS",
            "checked": 224,
            "misses": misses,
            "summary": "uncapped summary: " + ", ".join(m["session"] for m in misses),
        }
        monkeypatch.setitem(
            sys.modules, "coordinator_core.session.stable_pid_watch", fake
        )

    def test_detail_keeps_count_and_hazard_and_drops_the_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        self._stub_scan(mod, monkeypatch)

        result = mod._run_probe_stable_pid_miss(tmp_path)

        assert result.status == mod._DEGRADED
        assert "223 of 224" in result.detail
        assert "K-006" in result.detail
        assert "(+218 more)" in result.detail
        assert result.detail.count("no_meta_json") == 5
        assert len(result.detail) < 500

    def test_full_population_still_reaches_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        self._stub_scan(mod, monkeypatch)

        result = mod._run_probe_stable_pid_miss(tmp_path)

        assert len(result.data["misses"]) == 223
