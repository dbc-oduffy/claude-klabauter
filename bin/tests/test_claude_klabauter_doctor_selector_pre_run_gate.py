
from __future__ import annotations

import importlib.util
import inspect
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
    _KEY = "claude_klabauter_doctor_probe_selector_pre_run_gate"
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


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    m = _load_probe_module()
    if m is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not importable")
    return m


def _module_probe_function_names(mod: ModuleType) -> set[str]:
    return {
        name
        for name, _fn in inspect.getmembers(mod, predicate=inspect.isfunction)
        if name.startswith("_run_probe_")
    }


@pytest.fixture
def stubbed(mod, monkeypatch):
    called: list[str] = []

    table = [
        ("_run_probe_registry_key", "_REGISTRY_KEY_PROBE"),
        ("_run_probe_core_import", "_CORE_IMPORT_PROBE"),
        ("_run_probe_dialect_guard_armed", "_DIALECT_GUARD_ARMED_PROBE"),
        ("_run_probe_settings_home_complete", "_SETTINGS_HOME_COMPLETE_PROBE"),
        ("_run_probe_entrypoints_path_resolved", "_ENTRYPOINTS_PATH_RESOLVED_PROBE"),
        ("_run_probe_resident_debris", "_RESIDENT_DEBRIS_PROBE"),
        ("_run_probe_worktree_bloat", "_WORKTREE_BLOAT_PROBE"),
        ("_run_probe_version_sanity", "_VERSION_SANITY_PROBE"),
        ("_run_probe_invoke_smoke", "_INVOKE_SMOKE_PROBE"),
        ("_run_probe_strategic_draft_staleness", "_STRATEGIC_DRAFT_STALENESS_PROBE"),
        ("_run_probe_vendored_schema_drift", "_VENDOR_DRIFT_PROBE"),
        ("_run_probe_generator_output_staleness", "_GENERATOR_STALENESS_PROBE"),
        ("_run_probe_commitments_recheck", "_COMMITMENTS_RECHECK_PROBE"),
        ("_run_probe_stable_pid_miss", "_STABLE_PID_MISS_PROBE"),
        ("_run_probe_root_pointer", "_ROOT_POINTER_PROBE"),
        ("_run_probe_root_channels_reconciled", "_ROOT_CHANNELS_PROBE"),
        ("_run_probe_publish_provenance", "_PUBLISH_PROVENANCE_PROBE"),
        ("_run_probe_engine_target_rollout", "_ENGINE_TARGET_ROLLOUT_PROBE"),
        ("_run_probe_invoke_latency", "_INVOKE_LATENCY_PROBE"),
        ("_run_probe_orphaned_execnet_gateways", "_EXECNET_GATEWAY_PROBE"),
        ("_run_probe_launch_chain", "_LAUNCH_CHAIN_PROBE"),
        ("_run_probe_warm_residency", "_WARM_RESIDENCY_PROBE"),
        ("_run_probe_warm_generation", "_WARM_GENERATION_PROBE"),
        ("_run_probe_warm_route_share", "_WARM_ROUTE_SHARE_PROBE"),
        ("_run_probe_warm_roundtrip", "_WARM_ROUNDTRIP_PROBE"),
    ]

    covered = {fn_name for fn_name, _const_name in table} | {"_run_probe_claude_klabauter_root"}
    missing = _module_probe_function_names(mod) - covered
    assert not missing, (
        f"module defines _run_probe_* function(s) with no stub-table row: {sorted(missing)!r} "
        "— add a (function name, id constant) row above (or, for a make_root-shaped "
        "function, wire a dedicated stub) before this fixture stubs anything else"
    )

    def _make(pid: str):
        def _stub(*_args, **_kwargs):
            called.append(pid)
            return mod._ProbeResult(
                probe=pid, status=mod._PASS, detail="stub", remediation="—",
            )
        return _stub

    for fn_name, const_name in table:
        pid = getattr(mod, const_name)
        monkeypatch.setattr(mod, fn_name, _make(pid))

    root_pid = mod._CLAUDE_KLABAUTER_ROOT_PROBE

    def _stub_root():
        called.append(root_pid)
        return (
            mod._ProbeResult(
                probe=root_pid, status=mod._PASS, detail="stub", remediation="—",
            ),
            _REPO_ROOT,
        )

    monkeypatch.setattr(mod, "_run_probe_claude_klabauter_root", _stub_root)
    monkeypatch.setattr(mod, "_ensure_core_importable", lambda _root: None)
    return called


def _manifest(mod) -> dict:
    m = mod._load_probe_manifest(_REPO_ROOT)
    if not m:
        pytest.skip("doctor-probes.toml not loadable")
    return m


class TestKnownIdsIsTheRegistrationSurface:
    def test_known_ids_equals_manifest(self, mod, stubbed):
        _results, _root, known_ids = mod.run_probes()
        assert known_ids == set(_manifest(mod)), (
            "run_probes' call sites and doctor-probes.toml disagree; "
            "a probe is declared with no call site, or vice versa"
        )

    def test_default_run_calls_every_probe(self, mod, stubbed):
        results, _root, known_ids = mod.run_probes()
        assert set(stubbed) == known_ids
        assert {r.probe for r in results} == known_ids


class TestPreRunGating:
    def test_probe_selector_runs_only_that_probe(self, mod, stubbed):
        target = mod._WARM_GENERATION_PROBE
        results, _root, _known = mod.run_probes(selected={target})
        assert set(stubbed) == {mod._CLAUDE_KLABAUTER_ROOT_PROBE, target}
        assert [r.probe for r in results] == [target]

    def test_root_prerequisite_runs_but_is_not_emitted(self, mod, stubbed):
        target = mod._WARM_GENERATION_PROBE
        results, root, _known = mod.run_probes(selected={target})
        assert mod._CLAUDE_KLABAUTER_ROOT_PROBE in stubbed, "root probe must run as a prerequisite"
        assert mod._CLAUDE_KLABAUTER_ROOT_PROBE not in {r.probe for r in results}
        assert root == _REPO_ROOT, "an unselected root probe must still yield claude_klabauter_root"

    def test_root_probe_is_emitted_when_selected(self, mod, stubbed):
        results, _root, _known = mod.run_probes(selected={mod._CLAUDE_KLABAUTER_ROOT_PROBE})
        assert [r.probe for r in results] == [mod._CLAUDE_KLABAUTER_ROOT_PROBE]

    def test_cluster_selection_runs_exactly_that_cluster(self, mod, stubbed):
        manifest = _manifest(mod)
        clusters = {m.get("cluster") for m in manifest.values() if m.get("cluster")}
        assert clusters, "manifest declares no clusters"
        for cluster in sorted(clusters):
            stubbed.clear()
            selected = {
                pid for pid, meta in manifest.items() if meta.get("cluster") == cluster
            }
            results, _root, _known = mod.run_probes(selected=selected)
            assert set(stubbed) - {mod._CLAUDE_KLABAUTER_ROOT_PROBE} == selected - {mod._CLAUDE_KLABAUTER_ROOT_PROBE}
            assert {r.probe for r in results} == selected

    def test_triage_selection_runs_exactly_the_triage_set(self, mod, stubbed):
        manifest = _manifest(mod)
        selected = {pid for pid, meta in manifest.items() if meta.get("triage", False)}
        assert selected, "manifest declares no triage probes"
        results, _root, _known = mod.run_probes(selected=selected)
        assert {r.probe for r in results} == selected

    def test_every_id_runs_alone_and_returns_its_own_id(self, mod, stubbed):
        for pid in sorted(_manifest(mod)):
            results, _root, _known = mod.run_probes(selected={pid})
            assert [r.probe for r in results] == [pid], (
                f"call site for {pid!r} produced {[r.probe for r in results]!r}"
            )

    def test_timed_rows_are_exactly_the_selected_set(self, mod, stubbed):
        target = mod._VERSION_SANITY_PROBE
        results, _root, _known = mod.run_probes(selected={target})
        assert {r.probe for r in results if r.duration_ms is not None} == {target}

    def test_suite_total_equals_probe_total_on_a_selector_path(self, mod, stubbed):
        manifest = _manifest(mod)
        target = mod._WARM_GENERATION_PROBE
        results, claude_klabauter_root, known_ids = mod.run_probes(selected={target})
        suite_total_ms = sum(r.duration_ms for r in results if r.duration_ms is not None)
        envelope = mod._build_enriched_envelope(results, claude_klabauter_root, manifest, suite_total_ms)
        assert envelope["probe_suite_total_ms"] == envelope["probe_total_ms"]


class TestMisRegistrationIsLoud:
    def _args(self, mod, **kw):
        import argparse
        ns = argparse.Namespace(triage=False, cluster=None, probe=None, step_zero=False)
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_selected_known_but_did_not_run_raises(self, mod):
        manifest = _manifest(mod)
        pid = mod._WARM_GENERATION_PROBE
        with pytest.raises(RuntimeError, match="mis-wired call site"):
            mod._apply_selector(
                [], manifest, self._args(mod, probe=pid), known_ids={pid},
            )

    def test_declared_but_unimplemented_id_still_stubs(self, mod):
        manifest = dict(_manifest(mod))
        manifest["claude-klabauter.not.implemented"] = {"required": False, "cluster": "warm"}
        out = mod._apply_selector(
            [], manifest, self._args(mod, probe="claude-klabauter.not.implemented"), known_ids=set(),
        )
        assert len(out) == 1
        assert out[0].probe == "claude-klabauter.not.implemented"
        assert out[0].status == mod._INFO

    def test_absent_known_ids_keeps_pre_gate_behaviour(self, mod):
        manifest = _manifest(mod)
        pid = mod._WARM_GENERATION_PROBE
        out = mod._apply_selector([], manifest, self._args(mod, probe=pid))
        assert len(out) == 1 and out[0].status == mod._INFO


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_verdict_is_identical_selected_and_unselected(mod):
    full, _root, known_ids = mod.run_probes()
    baseline = {r.probe: (r.status, r.required, r.skipped) for r in full}
    for pid in sorted(known_ids):
        results, _r, _k = mod.run_probes(selected={pid})
        assert len(results) == 1 and results[0].probe == pid
        got = (results[0].status, results[0].required, results[0].skipped)
        assert got == baseline[pid], (
            f"{pid} reports {got} alone but {baseline[pid]} in a full run — "
            "a probe it depends on is being skipped"
        )
