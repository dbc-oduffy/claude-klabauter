"""Every `finally`-based `[timing]` span in publish.py still prints when the call it wraps raises.

A failed round is the one whose timing matters most; a span that only prints on success hides where
the failing round spent its time. Stub-only: no git, no subprocess, no engine.

Run: python -m pytest coordinator/bin/tests/test_publish_timing_prints_survive_raise.py -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_timing_survives_raise_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class _Boom(RuntimeError):
    pass


def _raise(*_args, **_kwargs):
    raise _Boom("injected")


class _ClaudeKlabauter:
    """Engine stub; `raise_in` names the one call that raises."""

    def __init__(self, raise_in: str):
        self.raise_in = raise_in

    def resolve_target(self, store, name):
        return {"hooks": [], "file_surface": {}, "guards": [], "inject": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        if self.raise_in == "run_percolate":
            raise _Boom("injected")
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def run_identity_check(self, dest):
        raise _Boom("injected")


def _pre_ci(tmp_path: Path, raise_in: str) -> None:
    src = tmp_path / "src"
    src.mkdir()
    real_dest = tmp_path / "repo"
    (real_dest / ".git").mkdir(parents=True)
    staging = tmp_path / "repo.publish-staging"
    staging.mkdir()
    target = publish.ResolvedTarget(name="t", mode="flat-mirror", source_dir=src, dest_dir=staging)
    ctx = publish.PercolateEngineContext(engine_claude_klabauter=_ClaudeKlabauter(raise_in), store={"targets": {}})
    publish.dispatch_percolate_pre_ci(ctx, tmp_path / "store.yaml", target, src, None, identity_dest_dir=real_dest)


@pytest.mark.parametrize("span", ["run_percolate", "run_identity_check"])
def test_pre_ci_span_prints_when_the_engine_call_raises(span, tmp_path, capsys):
    with pytest.raises(publish.EngineUnavailableError):
        _pre_ci(tmp_path, span)
    assert f"[timing] t: dispatch_percolate_pre_ci: {span}: " in capsys.readouterr().out


def _gates(monkeypatch, tmp_path: Path) -> io.StringIO:
    for gate in ("check_live_install_clobber", "check_marketplace_version_regression", "check_version_consistency"):
        monkeypatch.setattr(publish, gate, lambda *a, **k: True)
    monkeypatch.setattr(publish, "warn_machine_slug_net", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_round_pin_source_sha", lambda root, pinned, *, out=None, late=False: "SHA")
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    monkeypatch.setattr(publish, "_git_materialize_ref", lambda root, ref="HEAD", withheld=None: shadow)
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    target = publish.ResolvedTarget(name="t", mode="mirror", source_dir=src, dest_dir=dest, allowlist="a")
    out = io.StringIO()
    with pytest.raises(_Boom):
        publish.run_pre_sync_gates(
            target,
            setup_dir=tmp_path,
            identity_file_exists=False,
            identity=None,
            totals=publish.RunTotals(),
            dry_run=True,
            out=out,
            err=io.StringIO(),
        )
    return out


def test_field7_span_prints_when_the_declaration_check_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "_field7_declaration_check", _raise)
    assert "[timing] t: run_pre_sync_gates: field7_declaration_check: " in _gates(monkeypatch, tmp_path).getvalue()


def test_allowlist_span_prints_when_the_build_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "_field7_declaration_check", lambda *_a, **_k: (True, "", None))
    monkeypatch.setattr(publish, "build_allowlisted_source", _raise)
    assert "[timing] t: run_pre_sync_gates: allowlist " in _gates(monkeypatch, tmp_path).getvalue()


def _stub_listing(monkeypatch) -> None:
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda toplevel, sha=None: ())
    monkeypatch.setattr(publish, "_tracked_paths_at_sha", lambda toplevel, sha: {"f.txt"})


def test_read_tree_span_prints_when_the_spawn_raises(monkeypatch, tmp_path, capsys):
    _stub_listing(monkeypatch)
    monkeypatch.setattr(publish.subprocess, "run", _raise)
    with pytest.raises(_Boom):
        publish._extract_git_archive(tmp_path, "SHA")
    assert "[timing] _extract_git_archive: read-tree: " in capsys.readouterr().out


def test_checkout_index_span_prints_when_the_checkout_fails(monkeypatch, tmp_path, capsys):
    """The stubbed `read-tree` succeeds, the stubbed `checkout-index` exits non-zero."""
    _stub_listing(monkeypatch)
    monkeypatch.setattr(
        publish.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1 if "checkout-index" in cmd else 0, "", b"boom" if "checkout-index" in cmd else ""),
    )
    with pytest.raises(publish.GitMaterializeError, match="boom"):
        publish._extract_git_archive(tmp_path, "SHA")
    out = capsys.readouterr().out
    assert "[timing] _extract_git_archive: read-tree: " in out
    assert "[timing] _extract_git_archive: checkout-index: " in out


def test_a_phase_that_raises_still_lands_in_the_summary_with_total_cost_fields():
    sink: list = []
    with pytest.raises(_Boom):
        with publish._time_phase(sink, "row", "boom_phase"):
            raise _Boom("injected")
    sink.append(("row", "REFUSED: legacy four-field row", 0.0, 0.0))
    out = io.StringIO()
    publish._print_round_timing_summary(sink, 0.0, out=out)
    lines = out.getvalue().splitlines()
    phase = next(ln for ln in lines if "row: boom_phase:" in ln)
    assert " cpu_total=" in phase and " spawns=" in phase and " files=" in phase and " engine=" in phase
    legacy = next(ln for ln in lines if "REFUSED: legacy" in ln)
    assert "cpu_total=" not in legacy
    assert any("round total:" in ln and "cpu_total=" in ln for ln in lines)
    assert any("round: unattributed cpu_total=" in ln for ln in lines)


@pytest.mark.parametrize("phase", ["dispatch_mirror_like", "dispatch_percolate_post_rsync"])
def test_klabauter_phase_lines_report_wall_cpu_and_spawns(phase):
    sink: list = []
    with publish._time_phase(sink, "row", phase):
        pass
    out = io.StringIO()
    publish._print_round_timing_summary(sink, 0.0, out=out)
    line = next(ln for ln in out.getvalue().splitlines() if f"row: {phase}:" in ln)
    assert "s wall / " in line and "s cpu" in line and " spawns=" in line and " cpu_total=" in line


@pytest.mark.parametrize("phase", ["dispatch_mirror_like", "dispatch_percolate_post_rsync"])
def test_the_row_loop_times_each_klabauter_phase_into_the_sink(phase):
    """The wiring pin: the call the row loop makes sits inside a `_time_phase` of the same name."""
    src = (_BIN_DIR / "publish.py").read_text(encoding="utf-8")
    assert f'with _time_phase(timing_sink, target.name, "{phase}"):' in src
