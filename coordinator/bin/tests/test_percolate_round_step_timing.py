"""The percolate round driver emits one `[timing] <round>: step:<name>: wall=<s>s cpu=<s>s`
line per driver step on stderr, ending with `step:total`.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_step_timing.py -q
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.percolate_round_ab import _TIMING_PHASE_RE

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TESTS_DIR = Path(__file__).resolve().parent

_STEP_LINE = re.compile(
    r"^  \[timing\] (?P<round>[^:]+): step:(?P<step>[a-z0-9_]+): wall=(?P<wall>\d+\.\d{3})s cpu=(?P<cpu>\d+\.\d{3})s"
    r" cpu_total=(?P<cpu_total>\d+\.\d{3}s|unavailable) spawns=(?P<spawns>\d+|unavailable)"
    r" files=(?P<files>\d+) engine=(?P<engine>\d+\.\d{3}s|unavailable:\S+)$"
)
_UNATTRIBUTED = re.compile(r"^  \[timing\] (?P<round>[^:]+): unattributed cpu_total=(?P<cpu>\d+\.\d{3}s|unavailable) spawns=\S+$")

_ROUND_STEPS = [
    "engine_import_probe",
    "real_run",
    "scan_secrets",
    "inverse_drift",
    "parse",
    "step3_gate",
    "commit",
    "engine_import_gate",
    "push",
    "total",
]


@pytest.fixture(scope="module")
def _harness():
    """The round harness the sibling suite already drives: stubbed subprocess boundary, no real publish."""
    spec = importlib.util.spec_from_file_location("_percolate_round_harness", _TESTS_DIR / "test_percolate_round.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod  # type: ignore[union-attr]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def _stubbed(_harness, monkeypatch):
    mod = _harness._mod
    monkeypatch.setattr(mod._publish_slot, "slot_remote", lambda repo_root: None)
    monkeypatch.setattr(mod, "_early_engine_import_refusal", lambda *a, **k: None)
    return _harness


def _step_lines(stderr: str) -> list:
    return [m for m in (_STEP_LINE.match(line) for line in stderr.splitlines()) if m]


def test_clean_round_emits_one_line_per_step_in_order(_stubbed, tmp_path, monkeypatch, capsys):
    from pathlib import Path as _Path

    _stubbed._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(_Path, "exists", lambda self: True)
    rc, _out, _spy, _dest = _stubbed._run_round(tmp_path, monkeypatch)
    assert rc == _stubbed._mod._EXIT_OK

    lines = _step_lines(capsys.readouterr().err)
    assert [m["step"] for m in lines] == _ROUND_STEPS
    assert {m["round"] for m in lines} == {"alpha"}
    assert float(lines[-1]["wall"]) >= max(float(m["wall"]) for m in lines[:-1])


_SIX_PHASE_ROUND_STEPS = {
    "scan": "scan_secrets",
    "commit": "commit",
    "push": "push",
}


def _materialize_timing_line(monkeypatch, tmp_path, raise_after: bool) -> str:
    """Drive publish.run_pre_sync_gates past its git materialize loop with stubs; returns stdout."""
    import io

    spec = importlib.util.spec_from_file_location("_publish_phase_timing_under_test", _TESTS_DIR.parent / "publish.py")
    publish = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = publish  # type: ignore[union-attr]
    spec.loader.exec_module(publish)  # type: ignore[union-attr]

    for gate in ("check_live_install_clobber", "check_marketplace_version_regression", "check_version_consistency"):
        monkeypatch.setattr(publish, gate, lambda *a, **k: True)
    monkeypatch.setattr(publish, "warn_machine_slug_net", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_round_pin_source_sha", lambda root, pinned, *, out=None, late=False: "SHA")
    shadow = tmp_path / "shadow"
    shadow.mkdir()

    def _materialize(root, ref="HEAD", withheld=None):
        if not raise_after:
            raise publish.GitMaterializeError("injected")
        return shadow

    monkeypatch.setattr(publish, "_git_materialize_ref", _materialize)
    monkeypatch.setattr(publish, "_open_introduced_red_rows", lambda *_a, **_k: [])

    def _stop(*_a, **_k):
        raise RuntimeError("stop after materialize")

    monkeypatch.setattr(publish, "_field7_declaration_check", _stop)
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    target = publish.ResolvedTarget(name="t", mode="mirror", source_dir=src, dest_dir=dest, allowlist="a")
    out = io.StringIO()
    try:
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
    except RuntimeError:
        pass
    return out.getvalue()


@pytest.mark.parametrize("materialize_succeeds", [True, False])
def test_publish_row_reports_git_materialize_wall_seconds(monkeypatch, tmp_path, materialize_succeeds):
    out = _materialize_timing_line(monkeypatch, tmp_path, raise_after=materialize_succeeds)
    assert re.search(r"^  \[timing\] t: run_pre_sync_gates: git_materialize: \d+\.\d{3}s$", out, re.MULTILINE)


def test_default_round_covers_every_named_phase_with_wall_seconds(_stubbed, tmp_path, monkeypatch, capsys):
    """The round-owned phases (scan, commit, push) each emit a step line; commit-clone refresh and
    git materialize are asserted by their own tests, and sync/compare is not covered here."""
    from pathlib import Path as _Path

    _stubbed._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(_Path, "exists", lambda self: True)
    _stubbed._run_round(tmp_path, monkeypatch)

    steps = {m["step"]: float(m["wall"]) for m in _step_lines(capsys.readouterr().err)}
    for phase, step in _SIX_PHASE_ROUND_STEPS.items():
        assert step in steps, phase


def test_total_is_followed_by_the_unattributed_remainder(_stubbed, tmp_path, monkeypatch, capsys):
    from pathlib import Path as _Path

    _stubbed._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(_Path, "exists", lambda self: True)
    _stubbed._run_round(tmp_path, monkeypatch)

    err = capsys.readouterr().err.splitlines()
    total_at = max(i for i, ln in enumerate(err) if "step:total" in ln)
    assert _UNATTRIBUTED.match(err[total_at + 1])


def test_step_fields_are_a_number_or_the_word_unavailable_never_a_blank_zero(_stubbed, tmp_path, monkeypatch, capsys):
    from pathlib import Path as _Path

    _stubbed._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(_Path, "exists", lambda self: True)
    _stubbed._run_round(tmp_path, monkeypatch)

    for m in _step_lines(capsys.readouterr().err):
        assert m["engine"].startswith("unavailable:") or m["engine"].endswith("s")
        assert m["cpu_total"] == "unavailable" or m["cpu_total"].endswith("s")


def test_step_lines_never_match_the_phase_parser(_stubbed, tmp_path, monkeypatch, capsys):
    from pathlib import Path as _Path

    _stubbed._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(_Path, "exists", lambda self: True)
    _stubbed._run_round(tmp_path, monkeypatch)

    raw = [ln for ln in capsys.readouterr().err.splitlines() if "step:" in ln and "[timing]" in ln]
    assert raw
    assert not any(_TIMING_PHASE_RE.match(ln) for ln in raw)


def test_a_failing_step_still_reports_itself_and_the_total(_stubbed, tmp_path, monkeypatch, capsys):
    rc, _out, _spy, _dest = _stubbed._run_round(tmp_path, monkeypatch, scan_returncode=1)
    assert rc == _stubbed._mod._EXIT_FAIL

    steps = [m["step"] for m in _step_lines(capsys.readouterr().err)]
    assert steps == ["engine_import_probe", "real_run", "scan_secrets", "total"]


def test_delegation_times_the_commit_clone_materialise(_stubbed, tmp_path, monkeypatch, capsys):
    mod = _stubbed._mod
    clone_module = mod._commit_clone_module()
    ran = []
    monkeypatch.setattr(mod, "_resolve_clone_sha", lambda root, ref: "a" * 40)
    monkeypatch.setattr(clone_module, "materialise_commit_clone", lambda root, sha, cache: tmp_path / "clone")
    monkeypatch.setattr(
        clone_module, "run_entry_from_clone", lambda clone, entry, argv: ran.append((clone, entry, argv)) or 0
    )

    assert mod._delegate_to_commit_clone(["alpha"], None, "alpha") == 0

    assert ran and ran[0][0] == tmp_path / "clone"
    lines = _step_lines(capsys.readouterr().err)
    assert [(m["round"], m["step"]) for m in lines] == [("alpha", "commit_clone_materialise")]


def test_a_failed_materialise_still_reports_its_step(_stubbed, tmp_path, monkeypatch, capsys):
    mod = _stubbed._mod
    clone_module = mod._commit_clone_module()

    def _boom(*_a, **_k):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(mod, "_resolve_clone_sha", lambda root, ref: "a" * 40)
    monkeypatch.setattr(clone_module, "materialise_commit_clone", _boom)

    assert mod._delegate_to_commit_clone(["alpha"], None, "alpha") == mod._EXIT_FAIL
    assert [m["step"] for m in _step_lines(capsys.readouterr().err)] == ["commit_clone_materialise"]
