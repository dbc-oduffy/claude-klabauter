"""test_emit_cadence.py — pytest suite for emit-cadence.py.

Converted from a hand-rolled `.test.py` runner (print-based PASS/FAIL, its own
main()/sys.exit) into collectable top-level test_* functions; assertion intent
preserved 1:1.

Port of: test-emit-cadence-facade.sh (9ec1a670, 2026-07-19) — B-facade repoint,
2026-07-19 Windows de-bash campaign, Wave 1b. The retired bash suite forced the
legacy branch via COORDINATOR_FORCE_LEGACY=1 (a legacy-facade-only lever
cc_invoke.py's two-state route() does not honor — see cc_invoke.py's negative-spec).
This port achieves the equivalent hermetic coverage by pre-populating
`sys.modules["cc_invoke"]` with a fake module BEFORE importing the subject:
emit-cadence.py does `from cc_invoke import RouteMutationError, route_mutation` after its
own `sys.path.insert(0, lib_dir)`, but Python's import machinery checks sys.modules first
-- a pre-seeded entry short-circuits the file search entirely, so no live CLAUDE_KLABAUTER_ROOT /
coordinator_core.invoke subprocess is ever spawned and no real emission fires. Each test
loads a FRESH copy of the subject module (importlib, a new module object per test) so the
fake `route_mutation` can vary per test without cross-test leakage.

Test coverage:
  T1  gate off (COORDINATOR_EMISSION_CADENCE_LIVE=0/false/off, case-insensitive) — exit 0,
      skip note on stderr, route_mutation never invoked (gate check precedes op routing)
  T1b gate default (unset) — same as T1: skips, route_mutation never invoked. Default flipped
      OFF per PM ruling 2026-08-10 (supersedes D2's default-ON, 2026-07-11).
  T2  gate on (explicit 1/true/on, case-insensitive) — route_mutation invoked with
      op="emit.cadence", params={}
  T3  legacy_cadence path (State-1 seam absent) — the legacy_fn raises _SeamAbsentError,
      which route_mutation propagates unchanged (fakes call legacy_fn() directly, same as
      the real route()'s State-1 branch) — exit 1, "claude-klabauter control plane" on stderr
  T4  route_mutation raises RouteMutationError (op-level refusal) — exit 1, op detail
      surfaced on stderr
  T5  route_mutation raises a bare RuntimeError (native transport failure) — exit 3
  T6  route_mutation returns normally (op success) — exit 0
  T7  route_mutation raises StructuralPinError (structural contract-pin
      failure) — exit 4, remediation detail passed through unmangled,
      will-not-self-heal note on stderr; proves the except ladder checks
      StructuralPinError BEFORE the plain-RuntimeError branch (it subclasses
      RuntimeError, so wrong ordering would misroute it to exit 3)

Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Wave 1b)
Spec backlink: docs/plans/2026-07-11-emission-cadence-trigger-rewire.md § C2 / D3
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "emit-cadence.py")


class _FakeRouteMutationError(RuntimeError):
    """Stand-in for cc_invoke.RouteMutationError — carries .result like the real one."""

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


class _FakeStructuralPinError(RuntimeError):
    """Stand-in for cc_invoke.StructuralPinError — a RuntimeError subclass,
    so the T5b/T7 pairing proves the subject's except ladder orders this
    branch BEFORE the plain-RuntimeError branch (else it would never be
    reached)."""


_ABSENT = object()


def _install_fake_cc_invoke(route_mutation_fn):
    """Seed sys.modules["cc_invoke"] with a fake module exposing route_mutation
    and RouteMutationError. Must run BEFORE the subject module is imported —
    the subject's `from cc_invoke import ...` resolves against sys.modules
    first, so this fully short-circuits any real file/CLAUDE_KLABAUTER_ROOT lookup and
    guarantees no live emission fires.

    Returns the prior sys.modules entry (or `_ABSENT`) — hand it to
    `_restore_cc_invoke` in a `finally`.

    Negative spec: `sys.modules["cc_invoke"]` is process-global, and 30+
    `coordinator/bin/` scripts import `cc_invoke` by bare name. A fake left
    installed past the test that seeded it makes every later such import in the
    same worker resolve against a module carrying only these three attributes;
    the missing name surfaces as an ImportError at the victim's fixture setup,
    which pytest reports as an ERROR in an unrelated file. Never install
    without a paired restore."""
    fake = types.ModuleType("cc_invoke")
    fake.route_mutation = route_mutation_fn
    fake.RouteMutationError = _FakeRouteMutationError
    fake.StructuralPinError = _FakeStructuralPinError
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    """Undo `_install_fake_cc_invoke`, restoring absence as absence."""
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    """Import a brand-new copy of the subject module (bypassing any cached
    entry) so per-test route_mutation fakes never leak across tests."""
    sys.modules.pop("emit-cadence", None)
    spec = importlib.util.spec_from_file_location("emit-cadence", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_mutation_fn, env_overrides: dict | None = None):
    """Load a fresh subject with the given fake route_mutation under the given
    env overrides, call main(), and capture (exit_code, stdout, stderr)."""
    prior_cc_invoke = _install_fake_cc_invoke(route_mutation_fn)
    out, err = io.StringIO(), io.StringIO()
    env_key = "COORDINATOR_EMISSION_CADENCE_LIVE"
    had_env = env_key in os.environ
    prior = os.environ.get(env_key)
    try:
        subject = _load_subject_fresh()
        if env_overrides and env_key in env_overrides:
            os.environ[env_key] = env_overrides[env_key]
        elif env_key in os.environ:
            del os.environ[env_key]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main()
    finally:
        if had_env:
            os.environ[env_key] = prior
        elif env_key in os.environ:
            del os.environ[env_key]
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


@pytest.mark.parametrize("off_val", ["0", "false", "off", "FALSE", "OFF"])
def test_gate_off_skips_op_routing(off_val):
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"exit_code": 0}

    code, out, err = _run_main(
        _route_mutation, {"COORDINATOR_EMISSION_CADENCE_LIVE": off_val}
    )
    assert code == 0
    assert "skipping" in err
    assert reached["called"] is False


def test_gate_default_unset_skips_op_routing():
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"exit_code": 0}

    code, out, err = _run_main(_route_mutation)
    assert code == 0
    assert "skipping" in err
    assert reached["called"] is False


@pytest.mark.parametrize("on_val", [" 1 ", "true ", " TRUE", "on\t"])
def test_gate_on_invokes_op_with_whitespace_padded_value(on_val):
    captured = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        captured["op"] = op
        captured["params"] = params
        return {"exit_code": 0}

    code, out, err = _run_main(
        _route_mutation, {"COORDINATOR_EMISSION_CADENCE_LIVE": on_val}
    )
    assert code == 0
    assert captured.get("op") == "emit.cadence"
    assert captured.get("params") == {}


@pytest.mark.parametrize("off_val", [" 0 ", "false ", " OFF", "\tfalse"])
def test_gate_off_skips_with_whitespace_padded_value(off_val):
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"exit_code": 0}

    code, out, err = _run_main(
        _route_mutation, {"COORDINATOR_EMISSION_CADENCE_LIVE": off_val}
    )
    assert code == 0
    assert "skipping" in err
    assert reached["called"] is False


@pytest.mark.parametrize("on_val", ["1", "true", "on", "TRUE", "ON"])
def test_gate_on_invokes_op_with_correct_op_and_params(on_val):
    captured = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        captured["op"] = op
        captured["params"] = params
        captured["repo_root"] = repo_root
        return {"exit_code": 0}

    code, out, err = _run_main(
        _route_mutation, {"COORDINATOR_EMISSION_CADENCE_LIVE": on_val}
    )
    assert code == 0
    assert captured.get("op") == "emit.cadence"
    assert captured.get("params") == {}


_GATE_ON = {"COORDINATOR_EMISSION_CADENCE_LIVE": "1"}


def test_legacy_path_seam_absent_propagates():
    def _route_mutation(op, params, repo_root, legacy_fn):
        legacy_fn()  # mirrors route()'s State-1 branch: call and propagate

    code, out, err = _run_main(_route_mutation, _GATE_ON)
    assert code == 1
    assert "claude-klabauter control plane" in err
    assert "No emission fired" in err


def test_op_level_refusal_route_mutation_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeRouteMutationError(
            "op refused: exit_code=1", {"exit_code": 1, "error": "backlog.record failed"}
        )

    code, out, err = _run_main(_route_mutation, _GATE_ON)
    assert code == 1
    assert "op refused" in err


def test_transport_failure_bare_runtime_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure (rc=127)")

    code, out, err = _run_main(_route_mutation, _GATE_ON)
    assert code == 3
    assert "transport failure" in err


def test_op_success():
    def _route_mutation(op, params, repo_root, legacy_fn):
        return {"exit_code": 0, "message": "cadence fired"}

    code, out, err = _run_main(_route_mutation, _GATE_ON)
    assert code == 0


def test_structural_contract_pin_failure_not_exit_3():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeStructuralPinError("CONTRACT_VERSION drift: expected 2.20.0, found 2.18.0")

    code, out, err = _run_main(_route_mutation, _GATE_ON)
    assert code == 4, "must not be misrouted to the exit-3 plain-RuntimeError branch"
    assert "CONTRACT_VERSION drift" in err
    assert "will NOT" in err


# --- Self-stamping HALTED marker coverage (2026-08-10) ---
#
# T8  marker created on gate-off skip when absent
# T9  marker not rewritten (mtime/content unchanged) when already correct
# T10 marker removed when the gate is ON
# T11 a write failure (unwritable state/) still returns 0 and does not wedge
# T12 nothing written outside the repo root (a sibling dir untouched)
# T13 emitted_at is extracted from a large artifact without a full read


def _run_main_with_repo_root(route_mutation_fn, repo_root, env_overrides=None):
    """Like `_run_main`, but monkeypatches both repo-root resolvers to the
    given path instead of shelling out to git — keeps the marker tests
    hermetic and independent of the real repo's git state."""
    prior_cc_invoke = _install_fake_cc_invoke(route_mutation_fn)
    out, err = io.StringIO(), io.StringIO()
    env_key = "COORDINATOR_EMISSION_CADENCE_LIVE"
    had_env = env_key in os.environ
    prior = os.environ.get(env_key)
    try:
        subject = _load_subject_fresh()
        subject._resolve_repo_root_safe = lambda: repo_root
        subject._resolve_repo_root = lambda: repo_root
        if env_overrides and env_key in env_overrides:
            os.environ[env_key] = env_overrides[env_key]
        elif env_key in os.environ:
            del os.environ[env_key]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main()
    finally:
        if had_env:
            os.environ[env_key] = prior
        elif env_key in os.environ:
            del os.environ[env_key]
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


def _noop_route_mutation(op, params, repo_root, legacy_fn):
    return {"exit_code": 0}


def _marker_path(repo_root: Path) -> Path:
    return repo_root / "state" / "cockpit-emission.HALTED.md"


def test_marker_created_on_skip_when_absent(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "cockpit-emission.json").write_text(
        '{\n  "schema_version": "1.0.0",\n  "emitted_at": "2026-08-10T19:28:45Z"\n}\n',
        encoding="utf-8",
    )

    code, out, err = _run_main_with_repo_root(_noop_route_mutation, str(tmp_path))
    assert code == 0
    marker = _marker_path(tmp_path)
    assert marker.is_file()
    content = marker.read_text(encoding="utf-8")
    assert "HALTED" in content
    assert "2026-08-10T19:28:45Z" in content
    assert "DR-287" in content


def test_marker_not_rewritten_when_already_correct(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "cockpit-emission.json").write_text(
        '{"emitted_at": "2026-08-10T19:28:45Z"}', encoding="utf-8"
    )

    code, out, err = _run_main_with_repo_root(_noop_route_mutation, str(tmp_path))
    assert code == 0
    marker = _marker_path(tmp_path)
    mtime_1 = marker.stat().st_mtime_ns
    content_1 = marker.read_text(encoding="utf-8")

    code, out, err = _run_main_with_repo_root(_noop_route_mutation, str(tmp_path))
    assert code == 0
    mtime_2 = marker.stat().st_mtime_ns
    content_2 = marker.read_text(encoding="utf-8")

    assert content_1 == content_2
    assert mtime_1 == mtime_2, "marker must not be rewritten when content is unchanged"


def test_marker_removed_when_gate_on(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = _marker_path(tmp_path)
    marker.write_text("stale marker content", encoding="utf-8")

    code, out, err = _run_main_with_repo_root(
        _noop_route_mutation, str(tmp_path), _GATE_ON
    )
    assert code == 0
    assert not marker.exists()


def test_write_failure_still_returns_0_and_does_not_wedge(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    def _boom_write_text(self, *args, **kwargs):
        raise OSError("simulated read-only tree")

    prior_cc_invoke = _install_fake_cc_invoke(_noop_route_mutation)
    out, err = io.StringIO(), io.StringIO()
    env_key = "COORDINATOR_EMISSION_CADENCE_LIVE"
    had_env = env_key in os.environ
    prior = os.environ.get(env_key)
    try:
        subject = _load_subject_fresh()
        subject._resolve_repo_root_safe = lambda: str(tmp_path)
        subject._resolve_repo_root = lambda: str(tmp_path)
        monkeypatch.setattr(Path, "write_text", _boom_write_text, raising=True)
        if env_key in os.environ:
            del os.environ[env_key]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main()
    finally:
        if had_env:
            os.environ[env_key] = prior
        elif env_key in os.environ:
            del os.environ[env_key]
        _restore_cc_invoke(prior_cc_invoke)

    assert code == 0
    assert not _marker_path(tmp_path).exists()


def test_marker_survives_failed_emission_on_gate_on(tmp_path):
    """P2 regression: a marker present before a gate-ON run must survive a
    failed route_mutation — removal is only correct once emission has
    actually succeeded, else the repo is left with cadence conceptually
    live, no marker, and no fresh artifact."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = _marker_path(tmp_path)
    marker.write_text("pre-existing marker content", encoding="utf-8")

    def _failing_route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure (rc=127)")

    code, out, err = _run_main_with_repo_root(
        _failing_route_mutation, str(tmp_path), _GATE_ON
    )
    assert code == 3
    assert marker.is_file(), "marker must survive a failed emission"
    assert marker.read_text(encoding="utf-8") == "pre-existing marker content"


def test_nothing_written_outside_repo_root(tmp_path):
    repo_root = tmp_path / "repo"
    sibling = tmp_path / "sibling"
    (repo_root / "state").mkdir(parents=True)
    sibling.mkdir()

    code, out, err = _run_main_with_repo_root(_noop_route_mutation, str(repo_root))
    assert code == 0
    assert _marker_path(repo_root).is_file()
    assert not (sibling / "state").exists()
    assert list(sibling.iterdir()) == []


def test_emitted_at_extracted_without_full_read(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    artifact = state_dir / "cockpit-emission.json"
    # Large artifact (well beyond the bounded head-read window), emitted_at
    # near the top mirrors the real envelope's shape.
    padding = "x" * (200 * 1024)
    artifact.write_text(
        '{\n  "schema_version": "3.10.0",\n  "emitted_at": "2026-08-10T19:28:45Z",\n'
        f'  "padding": "{padding}"\n}}\n',
        encoding="utf-8",
    )

    subject = _load_subject_fresh()

    real_open = open
    captured_read_sizes = []

    def _tracking_open(path, mode="r", *args, **kwargs):
        fh = real_open(path, mode, *args, **kwargs)
        if str(path) == str(artifact):
            real_read = fh.read

            def _tracking_read(size=-1):
                captured_read_sizes.append(size)
                return real_read(size)

            fh.read = _tracking_read
        return fh

    monkeypatch.setattr("builtins.open", _tracking_open)
    emitted_at = subject._extract_emitted_at(artifact)
    assert emitted_at == "2026-08-10T19:28:45Z"
    # The read must be genuinely bounded, not a regression to a full read of
    # the ~200KB+ artifact — a whole-file read would still pass the assert
    # above (emitted_at is present somewhere) but must fail this one.
    assert captured_read_sizes, "expected the artifact to be opened and read"
    assert all(
        0 < size <= subject._MARKER_READ_HEAD_BYTES for size in captured_read_sizes
    ), f"read size(s) {captured_read_sizes} exceeded the bounded head-read window"


def test_emitted_at_outside_bounded_window_degrades_to_none(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    artifact = state_dir / "cockpit-emission.json"
    # emitted_at is pushed PAST the bounded head-read window this time —
    # the padding comes first, so the regex must miss and the extractor
    # must degrade to None rather than reading past the window to find it.
    subject = _load_subject_fresh()
    padding = "x" * (subject._MARKER_READ_HEAD_BYTES + 1024)
    artifact.write_text(
        f'{{\n  "schema_version": "3.10.0",\n  "padding": "{padding}",\n'
        '  "emitted_at": "2026-08-10T19:28:45Z"\n}\n',
        encoding="utf-8",
    )

    emitted_at = subject._extract_emitted_at(artifact)
    assert emitted_at is None

    content = subject._build_halted_marker_content(emitted_at)
    assert "unknown" in content


def test_sync_halted_marker_cleans_tmp_file_on_failed_replace(tmp_path, monkeypatch):
    # Review: coordinator:code-reviewer (a3ab9cae) — P3, e77aec9c7's
    # try/finally cleanup had no dedicated regression test; this forces
    # os.replace to fail and asserts the .tmp-<pid> survivor is still
    # removed, and that _sync_halted_marker still returns without raising.
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    subject = _load_subject_fresh()

    def _boom_replace(src, dst):
        raise OSError("simulated failed atomic swap")

    monkeypatch.setattr(subject.os, "replace", _boom_replace)

    result = subject._sync_halted_marker(str(tmp_path))
    assert result is None

    marker = _marker_path(tmp_path)
    assert not marker.exists(), "failed swap must not leave a marker behind"
    leftovers = list(state_dir.glob("cockpit-emission.HALTED.md.tmp-*"))
    assert leftovers == [], f"tmp file survivor(s) left behind: {leftovers}"
