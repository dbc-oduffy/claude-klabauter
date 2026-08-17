"""
bin/tests/test_claude_klabauter_doctor_advisory_rendering.py — the advisory-vs-hard rendering
split in bin/claude-klabauter-doctor-probe.py, and the vendor-drift remediation that names a
re-vendor entrypoint instead of `cp`.

Purpose: `claude-klabauter.schema.vendor_drift` is `required=false` and never gating by explicit
design, but it emitted an undifferentiated `"status":"fail"` that reads as actionable.
On 2026-07-28 that cost a fresh installer an entire detour
(state/audits/2026-07-28-windows-install-dogfood-friction.md § F3). The fix is
rendering-only, and this suite pins BOTH halves of that: the human-facing text now
distinguishes advisory from hard, AND the machine-facing contract is untouched.

Coverage:
  1. `_mark_advisory_detail` marks advisory failures, leaves required failures and
     non-failures alone, and is idempotent.
  2. `emit_step_zero` carries the marker in `detail` while `status`, `severity`, the
     five-key shape, and the exit-code rule are unchanged — including that an advisory
     fail still exits 0.
  3. `_build_enriched_envelope` marks the same rows in the JSON envelope.
  4. `_emit_severity_legend` writes to STDERR only, emits nothing on a clean run, and
     says explicitly when nothing blocking failed.
  5. The `claude-klabauter.schema.vendor_drift` remediation (probe body AND doctor-probes.toml)
     names `bin/claude-klabauter-revendor-schema.py` and warns off a hand copy — the discharge
     test. A regression to `cp`-shaped prose reintroduces the F3 trap.
  6. The `state/doctor-last-run.json` sentinel key shapes are untouched by all of it
     (documented external consumers — see `_write_doctor_sentinel`'s ADDITIVE-KEY POLICY).

Negative-spec: this suite does NOT assert on any probe's own `_ProbeResult.detail`
text — that is probe-authoring surface owned elsewhere. It asserts only on the
emission/rendering layer this change touches.

Spec backlink: state/audits/2026-07-28-windows-install-dogfood-friction.md § F3
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "claude-klabauter-doctor-probe.py"
_MANIFEST = _BIN_DIR / "doctor-probes.toml"

_spec = importlib.util.spec_from_file_location("_doctor_advisory_mod", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _result(probe: str, status: str, required: bool, detail: str = "d", skipped: bool = False):
    return _mod._ProbeResult(
        probe=probe,
        status=status,
        detail=detail,
        remediation="r",
        required=required,
        skipped=skipped,
    )


# ---------------------------------------------------------------------------
# 1. The marker itself
# ---------------------------------------------------------------------------

class TestMarkAdvisoryDetail:
    @pytest.mark.parametrize("status", ["BROKEN", "DEGRADED"])
    def test_advisory_failure_is_marked(self, status: str) -> None:
        out = _mod._mark_advisory_detail("drift on 3 schemas", required=False, status=status)
        assert out.startswith(_mod._ADVISORY_DETAIL_MARKER)
        assert "drift on 3 schemas" in out

    @pytest.mark.parametrize("status", ["BROKEN", "DEGRADED"])
    def test_required_failure_is_never_softened(self, status: str) -> None:
        """Nothing may make a hard failure read as optional."""
        assert _mod._mark_advisory_detail("x", required=True, status=status) == "x"

    @pytest.mark.parametrize("status", ["PASS", "INFO", "SKIP"])
    def test_non_failure_is_untouched(self, status: str) -> None:
        assert _mod._mark_advisory_detail("x", required=False, status=status) == "x"

    def test_idempotent(self) -> None:
        once = _mod._mark_advisory_detail("x", False, "DEGRADED")
        assert _mod._mark_advisory_detail(once, False, "DEGRADED") == once


# ---------------------------------------------------------------------------
# 2. Step-zero NDJSON — rendering changes, contract does not
# ---------------------------------------------------------------------------

class TestStepZeroContractUnchanged:
    def _emit(self, results, capsys) -> tuple[int, list[dict]]:
        rc = _mod.emit_step_zero(results)
        out = capsys.readouterr().out
        return rc, [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_advisory_fail_keeps_status_severity_and_exit_code(self, capsys) -> None:
        rc, rows = self._emit([_result("p.advisory", "DEGRADED", required=False)], capsys)
        assert rc == 0, "an advisory failure must never change the exit code"
        assert rows[0]["status"] == "fail"
        assert rows[0]["severity"] == "advisory"
        assert rows[0]["detail"].startswith(_mod._ADVISORY_DETAIL_MARKER)

    def test_required_fail_is_unmarked_and_exits_1(self, capsys) -> None:
        rc, rows = self._emit([_result("p.hard", "BROKEN", required=True)], capsys)
        assert rc == 1
        assert rows[0]["status"] == "fail"
        assert rows[0]["severity"] == "hard"
        assert not rows[0]["detail"].startswith(_mod._ADVISORY_DETAIL_MARKER)

    def test_five_key_shape_is_exactly_preserved(self, capsys) -> None:
        _, rows = self._emit(
            [
                _result("p.a", "DEGRADED", required=False),
                _result("p.b", "PASS", required=True),
                _result("p.c", "INFO", required=False, skipped=True),
            ],
            capsys,
        )
        for row in rows:
            assert set(row) == {"name", "status", "severity", "detail", "remediation"}

    def test_output_stays_strict_ndjson(self, capsys) -> None:
        """One JSON object per line, no prose interleaved — the legend must not leak here."""
        _mod.emit_step_zero(
            [
                _result("p.a", "DEGRADED", required=False),
                _result("p.b", "BROKEN", required=True),
            ]
        )
        captured = capsys.readouterr()
        for line in captured.out.splitlines():
            if line.strip():
                json.loads(line)
        assert captured.err == ""


# ---------------------------------------------------------------------------
# 3. JSON envelope
# ---------------------------------------------------------------------------

class TestEnvelopeRendering:
    def test_advisory_row_detail_is_marked_and_status_untouched(self) -> None:
        results = [
            _result("p.advisory", "DEGRADED", required=False, detail="3 schemas behind"),
            _result("p.hard", "BROKEN", required=True, detail="engine unimportable"),
        ]
        env = _mod._build_enriched_envelope(results, None, {})
        rows = {r["probe"]: r for r in env["probes"]}
        assert rows["p.advisory"]["status"] == "DEGRADED"
        assert rows["p.advisory"]["detail"].startswith(_mod._ADVISORY_DETAIL_MARKER)
        assert rows["p.hard"]["status"] == "BROKEN"
        assert not rows["p.hard"]["detail"].startswith(_mod._ADVISORY_DETAIL_MARKER)
        assert env["overall"] == "BROKEN"


# ---------------------------------------------------------------------------
# 4. Severity legend
# ---------------------------------------------------------------------------

class TestSeverityLegend:
    def test_clean_run_emits_nothing(self, capsys) -> None:
        _mod._emit_severity_legend([_result("p.ok", "PASS", required=True)])
        assert capsys.readouterr().err == ""

    def test_advisory_only_run_says_nothing_is_blocking(self, capsys) -> None:
        _mod._emit_severity_legend([_result("p.a", "DEGRADED", required=False)])
        err = capsys.readouterr().err
        assert "ADVISORY (1)" in err
        assert "No REQUIRED probe failed" in err
        assert "BLOCKING" not in err

    def test_mixed_run_separates_the_two(self, capsys) -> None:
        _mod._emit_severity_legend(
            [
                _result("p.a", "DEGRADED", required=False),
                _result("p.b", "BROKEN", required=True),
            ]
        )
        err = capsys.readouterr().err
        assert "BLOCKING (1)" in err and "p.b" in err
        assert "ADVISORY (1)" in err and "p.a" in err
        assert "No REQUIRED probe failed" not in err

    def test_writes_only_to_stderr(self, capsys) -> None:
        """scripts/setup.py concatenates our stderr onto our stdout before rendering —
        a stdout write here would corrupt a machine consumer's parse."""
        _mod._emit_severity_legend([_result("p.a", "DEGRADED", required=False)])
        assert capsys.readouterr().out == ""

    def test_never_raises_on_malformed_input(self) -> None:
        _mod._emit_severity_legend(["not a probe result"])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# 5. The vendor-drift remediation names the entrypoint, not `cp`
# ---------------------------------------------------------------------------

def _manifest_vendor_drift_row() -> dict:
    if sys.version_info < (3, 11):  # pragma: no cover
        pytest.skip("tomllib requires Python 3.11+")
    import tomllib

    data = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    rows = [p for p in data["probe"] if p["id"] == "claude-klabauter.schema.vendor_drift"]
    assert len(rows) == 1
    return rows[0]


class TestVendorDriftRemediationDischarge:
    """The F3 regression guard.

    The previous remediation said "copy DoE's <name>.schema.json in verbatim". Following
    it literally turns this probe green while breaking the pinned-SHA tamper-check,
    because the two compare against different references. Prose that hands the operator
    half the job discharges nothing (CLAUDE.md § North star).
    """

    def test_manifest_remediation_names_the_entrypoint(self) -> None:
        row = _manifest_vendor_drift_row()
        assert "bin/claude-klabauter-revendor-schema.py" in row["remediation"]
        assert "_QUEUE_SCHEMA_PINS" in row["remediation"]

    def test_manifest_remediation_warns_off_a_hand_copy(self) -> None:
        rem = _manifest_vendor_drift_row()["remediation"].lower()
        assert "do not cp" in rem or "do not copy" in rem

    def test_manifest_remediation_states_it_is_advisory(self) -> None:
        assert "advisory" in _manifest_vendor_drift_row()["remediation"].lower()

    def test_manifest_probe_stays_optional_and_non_gating(self) -> None:
        row = _manifest_vendor_drift_row()
        assert row["required"] is False
        assert row["severity_if_fail"] == "degraded"

    def test_named_entrypoint_actually_exists(self) -> None:
        assert (_BIN_DIR / "claude-klabauter-revendor-schema.py").is_file()

    def test_probe_body_drift_remediation_matches_the_manifest_intent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The DRIFT branch's own remediation string, not just the manifest's."""
        import types

        fake = types.ModuleType("coordinator_core.frontmatter.schema_drift_watch")
        fake.scan_vendored_schema_drift = lambda: {  # type: ignore[attr-defined]
            "status": "DRIFT",
            "summary": "3 of 13 vendored schemas behind DoE HEAD",
            "doe_repo_path": "/x",
            "checked": 13,
            "drifted": [{"schema": "review-findings.schema.json", "direction": "WE_ARE_BEHIND"}],
            "indeterminate": [],
        }
        monkeypatch.setitem(
            sys.modules, "coordinator_core.frontmatter.schema_drift_watch", fake
        )
        result = _mod._run_probe_vendored_schema_drift(tmp_path)
        assert result.status == "DEGRADED"
        assert result.required is False
        assert "claude-klabauter-revendor-schema.py" in result.remediation
        assert "cp the file in by hand" in result.remediation.lower() or (
            "do not cp" in result.remediation.lower()
        )


# ---------------------------------------------------------------------------
# 6. Sentinel key shapes are untouched
# ---------------------------------------------------------------------------

class TestSentinelShapeUnaffected:
    def test_seven_original_keys_plus_vendor_drift(self, tmp_path: Path) -> None:
        """Rendering changes must not reach the sentinel — it has external consumers
        in DoE (see _write_doctor_sentinel's ADDITIVE-KEY POLICY).

        NEGATIVE SPEC — do NOT restore `set(written) == {...}` here. Exact set
        equality contradicts the very policy this test cites, which states that
        new top-level keys MAY be added beside the original seven without a
        version bump provided they are additive-only and every consumer
        tolerates their absence. An equality assertion fails on each such
        permitted key (it fired on `advisory_only`, 2026-08-17) and would fire
        on the next one too — a test forbidding what the contract allows.

        What DoE actually depends on is that the documented keys keep their
        MEANING, not that the key set never grows, so that is what is pinned:
        presence of every required key, plus their values. A key going missing
        or changing shape still fails.
        """
        results = [_result("claude-klabauter.schema.vendor_drift", "DEGRADED", required=False)]
        envelope = _mod._build_enriched_envelope(results, None, {})
        _mod._write_doctor_sentinel(envelope, tmp_path)
        written = json.loads((tmp_path / "state" / "doctor-last-run.json").read_text())

        required_keys = {
            "ran_at", "ts", "verdict", "red_probes", "hint",
            "schema_version", "plugin", "vendor_drift",
        }
        assert required_keys <= set(written), (
            f"documented sentinel key(s) missing: {sorted(required_keys - set(written))}"
        )

        assert written["schema_version"] == 1
        assert written["plugin"] == "claude-klabauter"
        # `ran_at` is the ISO string, `ts` the int epoch — checked against the
        # writer, not assumed: both are consumed by DoE, so a type flip is the
        # breaking change this pins.
        assert isinstance(written["ran_at"], str) and written["ran_at"]
        assert isinstance(written["ts"], int)
        # A non-required DEGRADED probe: AMBER verdict, and `red_probes` stays
        # empty because it is filtered strictly by row status, never by overall.
        assert written["verdict"] == "AMBER"
        assert written["red_probes"] == []
        assert isinstance(written["hint"], str)
        assert set(written["vendor_drift"]) == {
            "status", "checked", "drifted", "indeterminate"
        }
