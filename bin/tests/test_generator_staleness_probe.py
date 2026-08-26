"""
bin.tests.test_generator_staleness_probe — unit tests for the
Claude-klabauter.generator.output_staleness doctor probe.

Covers the probe's verdict mapping over the dict shape
coordinator_core.ops.check_generator_output_staleness.compute_all_staleness returns
(this file tests the WIRING: verdict dict -> _ProbeResult status/required/skipped,
not the checker's own predicate — that is C0/C3/C6's own test surface), by
monkeypatching that module's compute_all_staleness function per the shipped
`_run_probe_vendored_schema_drift` precedent (bin/claude-klabauter-doctor-probe.py ~L1479).

  any STALE                        -> DEGRADED
  no STALE, any UNDECLARED/
    INDETERMINATE                  -> INFO/skipped=True, worded as incomplete —
                                       excluded from _local_reduce_overall so a
                                       coverage/resolution gap cannot capture the
                                       daily sentinel's verdict/hint the way a real
                                       STALE finding does.
  all FRESH/UNSTAMPED, or empty    -> PASS

Probe-authoring invariant (per state/lessons/2026-07-04-a-diagnostic-must-always-emit-a-parseabl.yaml):
  Every probe must emit a parseable _ProbeResult on ALL paths — including its own
  bootstrap failure, and including a repo with NO declared generators (empty dict).

Non-gating invariant (AC7): required=False on every path, so --step-zero (whose exit
code keys off REQUIRED probes) can never fail because of generator/output staleness.

Spec backlink: docs/plans/2026-08-13-generator-output-staleness-detector.md § Tasks
id C5, § Acceptance Criteria AC7.
"""

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
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Registered in sys.modules under a unique key BEFORE exec so Python's dataclass
    annotation-resolution path (sys.modules[cls.__module__]) finds a valid namespace
    on Python 3.14+.
    """
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_generator_staleness_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(key, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


def _is_parseable_probe_result(r: object) -> bool:
    """Return True iff r is a _ProbeResult with the required fields populated."""
    return (
        hasattr(r, "probe")
        and hasattr(r, "status")
        and hasattr(r, "detail")
        and hasattr(r, "remediation")
        and isinstance(r.probe, str) and len(r.probe) > 0  # type: ignore[union-attr]
        and isinstance(r.status, str) and len(r.status) > 0  # type: ignore[union-attr]
    )


def _patch_compute(monkeypatch: pytest.MonkeyPatch, report) -> None:
    """Replace the checker module's compute_all_staleness with a stub."""
    import coordinator_core.ops.check_generator_output_staleness as checker

    def _stub(*_args, **_kwargs):
        if isinstance(report, BaseException):
            raise report
        return report

    monkeypatch.setattr(checker, "compute_all_staleness", _stub)


def _entry(artifact: str, verdict: str, detail: str = "stub detail"):
    return {"artifact": artifact, "verdict": verdict, "detail": detail}


class TestGeneratorStalenessProbe:
    """Verdict mapping for claude-klabauter.generator.output_staleness."""

    def test_empty_report_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A repo with NO declared generators returns a verdict dict, not a raise."""
        mod = _require_module()
        _patch_compute(monkeypatch, {})

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.generator.output_staleness"
        assert result.status == mod._PASS
        assert result.required is False
        assert result.skipped is False
        assert result.data == {}

    def test_all_fresh_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {"docs/exec-summary.md": _entry("docs/exec-summary.md", "FRESH")},
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._PASS
        assert result.required is False

    def test_stale_is_degraded_never_broken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "state/orientation-cache.json": _entry(
                    "state/orientation-cache.json", "STALE", "sources moved after stamp"
                )
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.status != mod._BROKEN
        assert result.required is False, "STALE must never fail --step-zero"
        assert result.skipped is False
        assert "state/orientation-cache.json" in result.detail
        assert result.data["state/orientation-cache.json"]["verdict"] == "STALE"

    def test_undeclared_is_info_skipped_not_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A coverage gap (UNDECLARED, no STALE) must not capture the daily sentinel —
        it maps to INFO/skipped=True, mirroring _run_probe_vendored_schema_drift's
        UNRESOLVED precedent, so _local_reduce_overall excludes it from `overall`."""
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "coordinator/bin/some-generator.py": _entry(
                    "coordinator/bin/some-generator.py", "UNDECLARED", "no GENERATES declaration"
                )
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.status != mod._DEGRADED
        assert result.skipped is True
        assert result.required is False

    def test_write_target_unresolved_is_info_skipped_not_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as UNDECLARED: an unresolved-write population (no STALE) must not
        capture the sentinel's verdict/hint or consume a hint slot."""
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "coordinator/bin/some-generator.py": _entry(
                    "coordinator/bin/some-generator.py",
                    "WRITE_TARGET_UNRESOLVED",
                    "writes through an unresolved path expression and declares no GENERATES",
                )
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.status != mod._DEGRADED
        assert result.skipped is True
        assert result.required is False

    def test_indeterminate_is_info_skipped_not_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as UNDECLARED: a resolution gap (no STALE) must not capture the
        sentinel's verdict/hint."""
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "DoE-claude:coordinator/hooks/hooks.json": _entry(
                    "DoE-claude:coordinator/hooks/hooks.json",
                    "INDETERMINATE",
                    "generated_from_dirty_tree=true",
                )
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.status != mod._DEGRADED
        assert result.skipped is True
        assert result.required is False

    def test_stale_wins_over_undeclared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both STALE and UNDECLARED are present, STALE's detail leads."""
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "a": _entry("a", "STALE"),
                "b": _entry("b", "UNDECLARED"),
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert result.status == mod._DEGRADED
        assert "a" in result.detail

    def test_claude_klabauter_root_none_is_skip(self) -> None:
        mod = _require_module()

        result = mod._run_probe_generator_output_staleness(None)

        assert _is_parseable_probe_result(result)
        assert result.skipped is True
        assert result.required is False

    def test_compute_raising_still_yields_parseable_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashing checker must degrade to SKIP, not propagate — the doctor never crashes."""
        mod = _require_module()
        _patch_compute(monkeypatch, RuntimeError("boom"))

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.skipped is True
        assert result.required is False
        assert "boom" in result.detail

    def test_undeclared_gap_does_not_capture_overall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the P1 the reviewer flagged: an UNDECLARED/INDETERMINATE-only
        outcome (no STALE) must not drag the run's `overall` down alongside a PASS
        probe — this is the mechanism behind the daily sentinel's verdict/hint, not
        just this probe's own status field."""
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "coordinator/bin/some-generator.py": _entry(
                    "coordinator/bin/some-generator.py", "UNDECLARED", "no GENERATES declaration"
                )
            },
        )

        gap_result = mod._run_probe_generator_output_staleness(_REPO_ROOT)
        pass_result = mod._ProbeResult(
            probe="claude-klabauter.some.other_probe",
            status=mod._PASS,
            detail="all good",
            remediation="—",
            required=True,
        )

        overall = mod._local_reduce_overall([gap_result, pass_result])

        assert overall == mod._PASS

    def test_live_run_against_real_tree_never_raises(self) -> None:
        """Unpatched, end-to-end against this machine's real tree: always a parseable result."""
        mod = _require_module()

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.generator.output_staleness"
        assert result.required is False
        assert result.status != mod._BROKEN


class TestCoverageHonesty:
    """`detail` must report a ratio a reader can act on.

    The denominator is DECLARED PAIRS, never the swept population. Census of
    the live corpus, 2026-08-26 (`docs/problems/
    2026-08-26-what-a-reinstall-on-the-mac-actually-hits.md` § 6): of 100 swept
    entries only 8 are declared pairs; 89 are outside the staleness contract by
    construction (34 declared corpus mutators whose output set is
    data-dependent, 54 writers whose destination is a caller-supplied parameter
    that does not exist at parse time, 1 environmental INDETERMINATE) and no
    fix to any of them could raise the ratio. A ratio over the sweep therefore
    reported `4/100` for a population that is really 4 of 8 compared — a
    coverage gap of 96 that does not exist.
    """

    def test_pass_detail_reports_pairs_not_swept_population(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        report = {"docs/exec-summary.md": _entry("docs/exec-summary.md", "FRESH")}
        for i in range(9):
            key = f"coordinator_core/mutator_{i}.py"
            report[key] = _entry(None, "MUTATES_DECLARED")
        _patch_compute(monkeypatch, report)

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert result.status == mod._PASS
        assert "coverage 1/1 declared pairs compared" in result.detail
        assert "9 outside the staleness contract (9 declared corpus mutator)" in result.detail
        assert "1/10" not in result.detail

    def test_gap_detail_carries_the_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "docs/exec-summary.md": _entry("docs/exec-summary.md", "FRESH"),
                "coordinator_core/writer.py": _entry(None, "WRITE_TARGET_UNRESOLVED"),
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert result.status == mod._INFO
        assert result.skipped is True
        assert "coverage 1/1 declared pairs compared" in result.detail
        assert "write target not statically resolvable" in result.detail

    def test_stale_detail_carries_the_same_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        _patch_compute(
            monkeypatch,
            {
                "state/orientation-cache.json": _entry("state/orientation-cache.json", "STALE"),
                "coordinator_core/mutator.py": _entry(None, "MUTATES_DECLARED"),
            },
        )

        result = mod._run_probe_generator_output_staleness(_REPO_ROOT)

        assert result.status == mod._DEGRADED
        assert "coverage 1/1 declared pairs compared" in result.detail
        assert "1 outside the staleness contract (1 declared corpus mutator)" in result.detail

    def test_unstamped_joins_the_denominator_undeclared_does_not(self) -> None:
        """The two coverage gaps are not the same gap and do not share a remedy.

        UNSTAMPED is a declared pair whose artifact carries no readable stamp:
        the pair exists, so it belongs in the ratio and closing it raises the
        ratio. UNDECLARED is a module with no pair at all — its remedy is a
        `GENERATES`/`MUTATES` block, and counting it as an uncompared pair
        names the wrong fix.
        """
        mod = _require_module()
        data = {
            "a": {"verdict": "FRESH"},
            "b": {"verdict": "STALE"},
            "c": {"verdict": "UNSTAMPED"},
            "d": {"verdict": "INDETERMINATE"},
            "e": {"verdict": "UNDECLARED"},
        }

        line = mod._generator_staleness_coverage(data)

        assert line.startswith("coverage 2/3 declared pairs compared;")
        assert "1 unstamped" in line
        assert "1 module(s) owe a declaration" in line
        assert "1 outside the staleness contract (1 indeterminate)" in line

    def test_exempt_entries_are_counted_and_reasoned_never_dropped(self) -> None:
        """Excluded from the ratio is not excluded from the report."""
        mod = _require_module()
        data = {f"m{i}": {"verdict": "MUTATES_DECLARED"} for i in range(34)}
        data.update({f"w{i}": {"verdict": "WRITE_TARGET_UNRESOLVED"} for i in range(54)})

        line = mod._generator_staleness_coverage(data)

        assert line.startswith("coverage: no declared pair carried a comparison")
        assert (
            "88 outside the staleness contract "
            "(34 declared corpus mutator, 54 write target not statically resolvable)"
        ) in line

    def test_live_census_shape_reports_four_of_eight_not_four_of_a_hundred(self) -> None:
        """Regression pin for the reported defect, at the census's own numbers."""
        mod = _require_module()
        data: dict = {}
        for verdict, count in (
            ("STALE", 1),
            ("FRESH", 3),
            ("UNSTAMPED", 4),
            ("UNDECLARED", 3),
            ("MUTATES_DECLARED", 34),
            ("WRITE_TARGET_UNRESOLVED", 54),
            ("INDETERMINATE", 1),
        ):
            for i in range(count):
                data[f"{verdict}-{i}"] = {"verdict": verdict}
        assert len(data) == 100

        line = mod._generator_staleness_coverage(data)

        assert line.startswith("coverage 4/8 declared pairs compared;")
        assert "4/100" not in line
        assert "89 outside the staleness contract" in line

    def test_unrecognised_verdict_is_surfaced_and_stays_out_of_the_ratio(self) -> None:
        mod = _require_module()
        data = {"a": {"verdict": "FRESH"}, "b": {"verdict": "WAT"}}

        line = mod._generator_staleness_coverage(data)

        assert line.startswith("coverage 1/1 declared pairs compared;")
        assert "1 unrecognised ('WAT')" in line

    def test_coverage_line_omits_absent_categories(self) -> None:
        mod = _require_module()

        assert mod._generator_staleness_coverage({"a": {"verdict": "FRESH"}}) == (
            "coverage 1/1 declared pairs compared"
        )


class TestManifestRegistration:
    """The probe must be declared in bin/doctor-probes.toml, satisfying the
    triage/weight invariant, and never gating (AC7)."""

    def test_probe_is_registered_and_triage_true(self) -> None:
        import tomllib

        data = tomllib.loads((_REPO_ROOT / "bin" / "doctor-probes.toml").read_text(encoding="utf-8"))
        entries = [p for p in data["probe"] if p["id"] == "claude-klabauter.generator.output_staleness"]

        assert len(entries) == 1, "claude-klabauter.generator.output_staleness must be declared exactly once"
        entry = entries[0]
        assert entry["triage"] is True, (
            "must be triage=true — only --triage/full runs write state/doctor-last-run.json, "
            "the sentinel /workday-start reads; triage=false would never reach cadence"
        )
        assert entry["weight"] == "standard", "spawns git log; not cheap, not heavy"
        assert entry["weight"] in {"cheap", "standard"}, (
            "manifest INVARIANT: triage=true ==> weight in {cheap, standard}"
        )
        assert entry["required"] is False, "AC7: a probe reports, it does not gate"
        assert entry["body"] == "bin/claude-klabauter-doctor-probe.py:_run_probe_generator_output_staleness"
