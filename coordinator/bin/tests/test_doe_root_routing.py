"""test_doe_root_routing.py — C1 routing tests for doe_root() + doctrine central writes.

Tests (AC1, AC2 from gate2-w23-state-seam-caller-switch.md):
  AC1-pos   doe_root() returns env DOE_ROOT override when set.
  AC1-pos   coordinator-lesson-promote._outbox_root() resolves under $(doe_root)/state/lessons-outbox.
  AC1-pos   coordinator-queue-append._output_path() central branch resolves under $(doe_root)/state/improvement-queue.
  AC2-cold  coordinator-lesson-promote invoked cold (no repos.doe_claude, no DOE_ROOT):
              WARN to stderr, exit 0, no file written anywhere.
  AC2-cold  coordinator-queue-append invoked cold (no repos.doe_claude, no DOE_ROOT):
              WARN to stderr, exit 0, no file written anywhere.

Cold-path tests call the CALLING CLI's main() with _cc_route mocked to invoke legacy_fn()
directly, so the full resolver chain executes without subprocess isolation gaps. This satisfies
the lesson requirement (test entry point, not lib in isolation) while avoiding env-isolation
issues where the subprocess CC seam takes a different code path before legacy_fn is reached.

Converted from a hand-rolled unittest runner to collectable pytest functions.

Run: python3 -m pytest coordinator/bin/tests/test_doe_root_routing.py

Spec backlink: DoE-claude:pln-gate-2-w2-3-live-caller-switch-3e51cf § C1
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import unittest.mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — locate CLIs and lib relative to this test file.
# test file: coordinator/bin/tests/test_doe_root_routing.py
# CLIs:      coordinator/bin/coordinator-lesson-promote
#            coordinator/bin/coordinator-queue-append
# lib:       coordinator/bin/lib/
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"
_LESSON_PROMOTE_PATH = _BIN_DIR / "coordinator-lesson-promote.py"
_QUEUE_APPEND_PATH = _BIN_DIR / "coordinator-queue-append.py"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# Load coordinator_registry for unit-testing doe_root() directly.
import coordinator_registry as _reg  # noqa: E402


def _load_cli(path: Path, module_name: str):
    """Load a CLI (no .py extension) as a Python module for unit testing."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_lesson_cli = _load_cli(_LESSON_PROMOTE_PATH, "coordinator_lesson_promote")
_queue_cli = _load_cli(_QUEUE_APPEND_PATH, "coordinator_queue_append")


# ---------------------------------------------------------------------------
# AC1-pos — doe_root() resolves from DOE_ROOT env override
# ---------------------------------------------------------------------------


def test_doe_root_returns_env_override():
    """doe_root() trusts DOE_ROOT env var as-is (§4b idempotency parity)."""
    fake_root = "/fake/doe-claude-root"
    with unittest.mock.patch.dict(os.environ, {"DOE_ROOT": fake_root}, clear=False):
        result = _reg.doe_root()
    assert result == fake_root


def test_doe_root_strips_empty_env():
    """doe_root() ignores DOE_ROOT='' (empty string is not a valid override)."""
    # Also clears REPO_DOE_CLAUDE (rung 1b) so this machine's real ambient
    # override doesn't win before the mocked machine-local rung is reached.
    with (
        unittest.mock.patch.dict(os.environ, {"DOE_ROOT": "", "REPO_DOE_CLAUDE": ""}, clear=False),
        unittest.mock.patch.object(_reg, "_registry_machine_local_get", return_value="/ml/doe"),
    ):
        result = _reg.doe_root()
    assert result == "/ml/doe"


def test_doe_root_raises_when_unresolvable():
    """doe_root() raises _DoeUnresolvable when neither env nor machine-local resolve.

    Also stubs the codename-free pointer/marketplace-cache/flat-layout rungs
    to "" — on a box with a real `.doe-root` pointer configured (e.g. this
    dev machine, which resolves `_mp_doe_root_pointer_rung()` to a real
    DoE-claude checkout), clearing only DOE_ROOT/REPO_DOE_CLAUDE and
    _registry_machine_local_get leaves those filesystem-probing rungs live,
    so "unresolvable" was not actually achievable and the test passed only
    on a machine without that pointer configured. Mirrors
    test_coordinator_registry.py::_clear_doe_root_env's isolation pattern.
    """
    env_without_doe = {
        k: v for k, v in os.environ.items()
        if k not in ("DOE_ROOT", "REPO_DOE_CLAUDE", "CLAUDE_PLUGIN_ROOT")
    }
    with (
        unittest.mock.patch.dict(os.environ, env_without_doe, clear=True),
        unittest.mock.patch.object(_reg, "_registry_machine_local_get", return_value=None),
        unittest.mock.patch.object(_reg, "_mp_doe_root_pointer_rung", return_value=""),
        unittest.mock.patch.object(_reg, "_mp_marketplace_cache_rung", return_value=""),
        unittest.mock.patch.object(_reg, "_mp_flat_layout_probe_rung", return_value=""),
    ):
        try:
            _reg.doe_root()
        except _reg._DoeUnresolvable:
            pass
        else:
            raise AssertionError("expected _DoeUnresolvable to be raised")


# ---------------------------------------------------------------------------
# AC1-pos — lesson-promote._outbox_root() routes to $(doe_root)/state/lessons-outbox
# ---------------------------------------------------------------------------


def test_outbox_root_under_doe_state():
    """_outbox_root() returns $(doe_root)/state/lessons-outbox when DOE_ROOT is set."""
    fake_doe = "/fake/doe"
    env_with_doe = dict(os.environ)
    env_with_doe.pop("LESSON_PROMOTE_OUTBOX_ROOT", None)
    env_with_doe["DOE_ROOT"] = fake_doe
    with unittest.mock.patch.dict(os.environ, env_with_doe, clear=True):
        result = _lesson_cli._outbox_root()
    assert result == os.path.join(fake_doe, "state", "lessons-outbox")


def test_outbox_root_env_override_wins():
    """LESSON_PROMOTE_OUTBOX_ROOT takes precedence over doe_root() resolution."""
    override = "/override/outbox"
    with unittest.mock.patch.dict(os.environ, {"LESSON_PROMOTE_OUTBOX_ROOT": override}, clear=False):
        result = _lesson_cli._outbox_root()
    assert result == override


def test_outbox_root_raises_doe_unresolvable():
    """_outbox_root() propagates _DoeUnresolvable when doe_root() cannot resolve.

    See test_doe_root_raises_when_unresolvable's docstring — the
    codename-free pointer/marketplace-cache/flat-layout rungs must also be
    stubbed to "" so a real on-box `.doe-root` pointer doesn't make
    "unresolvable" unreachable.
    """
    env_clean = {k: v for k, v in os.environ.items()
                 if k not in ("DOE_ROOT", "REPO_DOE_CLAUDE", "LESSON_PROMOTE_OUTBOX_ROOT",
                               "CLAUDE_PLUGIN_ROOT")}
    with (
        unittest.mock.patch.dict(os.environ, env_clean, clear=True),
        unittest.mock.patch.object(_reg, "_registry_machine_local_get", return_value=None),
        unittest.mock.patch.object(_reg, "_mp_doe_root_pointer_rung", return_value=""),
        unittest.mock.patch.object(_reg, "_mp_marketplace_cache_rung", return_value=""),
        unittest.mock.patch.object(_reg, "_mp_flat_layout_probe_rung", return_value=""),
    ):
        try:
            _lesson_cli._outbox_root()
        except _reg._DoeUnresolvable:
            pass
        else:
            raise AssertionError("expected _DoeUnresolvable to be raised")


# ---------------------------------------------------------------------------
# AC1-pos — queue-append._output_path() central branch routes to $(doe_root)/state/...
# ---------------------------------------------------------------------------


def test_central_improvement_queue_under_doe():
    """Central improvement-queue path lands under $(claude_klabauter_root)/state/improvement-queue.

    Rewired (DR-236, 2026-07-25): this test originally asserted DOE_ROOT routing per
    the [DoE-claude] docs/plans/2026-07-06-gate2-w23-state-seam-caller-switch.md
    proposal — but that plan was never ratified (`status: draft`, AC1/AC2 `pending`,
    its own C3 HELD with recorded disk proof the flip never took effect). The CLI's
    OWN current source (`coordinator-queue-append`'s `_output_path`, negative-spec
    docstring) confirms central-scope improvement-queue writes route to
    `_claude_klabauter_root()` unconditionally, per docs/wiki/state-placement-law.md § Taxonomy
    "Central/global state" and `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`
    (state/ is claude-klabauter's own disk-truth custody). Only coordinator-lesson-promote's
    lessons-outbox central write genuinely routes to DoE-claude (see
    test_outbox_root_under_doe_state below) — the two central-state schemas route to
    different owners, and this test previously conflated them.
    """
    fake_claude_klabauter = "/fake/claude-klabauter"
    with unittest.mock.patch.object(_queue_cli, "_claude_klabauter_root", return_value=fake_claude_klabauter):
        result = _queue_cli._output_path(
            "improvement-queue", "My improvement", queue_scope="central"
        )
    assert result.startswith(os.path.join(fake_claude_klabauter, "state", "improvement-queue")), (
        f"Expected path under {fake_claude_klabauter}/state/improvement-queue, got: {result}"
    )


def test_central_scope_raises_doe_unresolvable():
    """_output_path() central branch raises _ClaudeKlabauterUnresolvable when claude_klabauter_root() cannot resolve.

    Rewired (DR-236, 2026-07-25): see test_central_improvement_queue_under_doe's
    docstring — central-scope improvement-queue routes to CLAUDE_KLABAUTER_ROOT (via
    `_claude_klabauter_root()`, which returns None rather than raising — see
    coordinator/bin/lib/cli_shared.py::claude_klabauter_root()'s own negative-spec, "never
    raises"), not DOE_ROOT; the CLI raises its own `_ClaudeKlabauterUnresolvable` when
    `_claude_klabauter_root()` returns None, not `_reg._DoeUnresolvable`.
    """
    with unittest.mock.patch.object(_queue_cli, "_claude_klabauter_root", return_value=None):
        try:
            _queue_cli._output_path("improvement-queue", "title", queue_scope="central")
        except _queue_cli._ClaudeKlabauterUnresolvable:
            pass
        else:
            raise AssertionError("expected _ClaudeKlabauterUnresolvable to be raised")


def test_project_scope_unaffected(tmp_path):
    """Project-scope (cwd-relative) path is not routed through doe_root()."""
    tmpdir = str(tmp_path)
    with (
        unittest.mock.patch.object(_queue_cli, "_current_repo_root", return_value=tmpdir),
        unittest.mock.patch.object(_queue_cli, "_claude_home", return_value="/fake/home"),
        unittest.mock.patch.dict(os.environ,
                                 {k: v for k, v in os.environ.items()
                                  if k != "QUEUE_APPEND_OUTPUT_ROOT"},
                                 clear=True),
    ):
        result = _queue_cli._output_path(
            "improvement-queue", "title", queue_scope="project"
        )
    assert result.startswith(os.path.join(tmpdir, "state", "improvement-queue")), (
        f"Project-scope path should be under {tmpdir}/state/improvement-queue, got: {result}"
    )


# ---------------------------------------------------------------------------
# AC2-cold — lesson-promote CLI cold-path: WARN + exit 0, no file written
#
# Tests the CALLING CLI's main() entry point with the CC seam simulated absent
# (mock _cc_route calls legacy_fn directly) and machine-local stub returning
# nothing for repos.doe_claude. This exercises the full entry-point chain —
# main() → legacy_fn() → _write_entry() → _outbox_root() → doe_root() → raise —
# per lesson state/lessons/2026-07-05-universal-test-the-resolver-entry-point.yaml.
# ---------------------------------------------------------------------------


def _make_cold_env(tmpdir: str) -> dict[str, str]:
    """Return a minimal env dict with MACHINE_LOCAL_IMPL → stub (no repos.doe_claude).

    DOE_ROOT and CLAUDE_KLABAUTER_ROOT are intentionally absent from the returned dict.
    Pass this as `os.environ` replacement so no parent-env leakage occurs.
    MACHINE_LOCAL_IMPL is the test-isolation knob shared by coordinator_registry
    (_registry_machine_local_get) and both CLIs, so one stub covers all callers.
    """
    ml_impl = os.path.join(tmpdir, "_machine_local_stub.py")
    with open(ml_impl, "w", encoding="utf-8") as fh:
        fh.write(
            "import sys\n"
            "if len(sys.argv) >= 3 and sys.argv[1] == 'get':\n"
            "    sys.exit(1)\n"
            "elif len(sys.argv) >= 2 and sys.argv[1] == 'keys':\n"
            "    print('')\n"
            "sys.exit(0)\n"
        )
    return {"MACHINE_LOCAL_IMPL": ml_impl}


_FAKE_LESSON_SCHEMA = {
    "enums": {"change_kind": ["doctrine-edit", "wiki-append", "skill-edit"]}
}

_MINIMAL_LESSON_ARGV = [
    "--title", "cold-path test lesson",
    "--body", "body text",
    "--change-kind", "doctrine-edit",
    "--target-wiki", "docs/wiki/test.md",
]


def _run_cold_lesson(tmpdir: str) -> tuple[int, str]:
    """Invoke lesson-promote main() with seam-absent simulation and cold env.

    Mocks _cc_route to call legacy_fn() directly (no CC native path), patches
    env to have MACHINE_LOCAL_IMPL pointing to stub but no DOE_ROOT. Returns
    (return_code, captured_stderr).
    """
    cold_env = _make_cold_env(tmpdir)
    captured_err = io.StringIO()

    def fake_route(op, params, repo_root, legacy_fn):
        # Simulate seam-absent: call legacy_fn directly.
        return legacy_fn()

    with (
        unittest.mock.patch.dict(os.environ, cold_env, clear=True),
        unittest.mock.patch.object(
            _lesson_cli, "_cc_route", side_effect=fake_route
        ),
        unittest.mock.patch.object(
            _lesson_cli, "_describe_schema_node", return_value=_FAKE_LESSON_SCHEMA
        ),
        unittest.mock.patch.object(
            _lesson_cli, "_resolve_from_repo", return_value="test-em"
        ),
        unittest.mock.patch.object(
            _lesson_cli, "_current_repo_root", return_value="/fake/repo"
        ),
        unittest.mock.patch("sys.stderr", captured_err),
    ):
        rc = _lesson_cli.main(_MINIMAL_LESSON_ARGV)

    return rc, captured_err.getvalue()


def test_lesson_promote_cold_exits_nonzero(tmp_path):
    """Cold path: exit _EXIT_DOE_UNRESOLVABLE (3) — WARN+skip is graceful but NOT
    silent success (A13 fix: pre-A13 this returned 0, which was a defect — a caller
    checking only returncode==0 must be able to trust that the write happened).
    """
    rc, _ = _run_cold_lesson(str(tmp_path))
    assert rc == _lesson_cli._EXIT_DOE_UNRESOLVABLE, (
        "cold-path must exit _EXIT_DOE_UNRESOLVABLE (3), not silently claim success"
    )


def test_lesson_promote_cold_warn_to_stderr(tmp_path):
    """Cold path: WARN message on stderr mentioning DOE_ROOT."""
    _, err = _run_cold_lesson(str(tmp_path))
    assert "warn:" in err, "cold-path must emit 'warn:' to stderr"
    assert "DOE_ROOT" in err, "cold-path WARN must mention DOE_ROOT"


def test_lesson_promote_cold_no_file_written(tmp_path):
    """Cold path: no file written (neither DoE nor claude-klabauter path)."""
    _run_cold_lesson(str(tmp_path))
    written = list(tmp_path.rglob("*.yaml"))
    assert written == [], f"cold-path must write NO files; found: {written}"


# ---------------------------------------------------------------------------
# AC2-cold — queue-append CLI cold-path: WARN + exit 0, no file written
#
# Same pattern: main() with seam-absent simulation. queue-append.main() uses
# parse_args() with no argv param, so sys.argv is patched via mock.
# ---------------------------------------------------------------------------

_CENTRAL_IQ_ARGV = [
    "coordinator-queue-append",
    "--schema", "improvement-queue",
    "--title", "cold-path test improvement",
    "--body", "body text",
    "--status", "open",
    "--surface", "coordinator/bin/test",
    "--proposed-action", "fix it",
    "--change-kind", "script-edit",
    "--queue-scope", "central",
]


def _run_cold_queue(tmpdir: str) -> str:
    """Invoke queue-append main() with seam-absent simulation and cold env.

    Returns captured_stderr. main() returns None on graceful paths.
    """
    cold_env = _make_cold_env(tmpdir)
    captured_err = io.StringIO()

    def fake_route(op, params, repo_root, legacy_fn):
        # Simulate seam-absent: call legacy_fn directly.
        return legacy_fn()

    with (
        unittest.mock.patch.dict(os.environ, cold_env, clear=True),
        unittest.mock.patch("sys.argv", _CENTRAL_IQ_ARGV),
        unittest.mock.patch.object(
            _queue_cli, "_cc_route", side_effect=fake_route
        ),
        unittest.mock.patch.object(
            _queue_cli, "_schema_cli_describe",
            return_value={
                "required": ["title", "body", "status", "surface",
                             "proposed_action", "from_repo", "change_kind"],
                "optional": ["queue_scope", "tags", "evidence"],
                "enums": {
                    "status": ["open", "closed", "deferred"],
                    "change_kind": ["script-edit", "doctrine-edit", "wiki-append"],
                },
            }
        ),
        unittest.mock.patch.object(
            _queue_cli, "_schema_cli_validate", return_value=(True, [])
        ),
        unittest.mock.patch.object(
            _queue_cli, "_resolve_from_repo", return_value="test-em"
        ),
        unittest.mock.patch.object(
            _queue_cli, "_current_repo_root", return_value="/fake/repo"
        ),
        unittest.mock.patch("sys.stderr", captured_err),
    ):
        _queue_cli.main()

    return captured_err.getvalue()


def test_queue_append_cold_warn_to_stderr(tmp_path):
    """Cold central invocation: WARN message on stderr (CLAUDE_KLABAUTER_ROOT unresolvable).

    Rewired (DR-236, 2026-07-25): queue-append's central-scope write routes to
    CLAUDE_KLABAUTER_ROOT, not DOE_ROOT — see test_central_improvement_queue_under_doe's
    docstring above. The CLI's own WARN text confirms this: "CLAUDE_KLABAUTER_ROOT
    unresolvable — skipping central write: ...".
    """
    err = _run_cold_queue(str(tmp_path))
    assert "warn:" in err, "cold-path must emit 'warn:' to stderr"
    assert "CLAUDE_KLABAUTER_ROOT" in err, "cold-path WARN must mention CLAUDE_KLABAUTER_ROOT"


def test_queue_append_cold_no_file_written(tmp_path):
    """Cold central invocation: no file written anywhere."""
    _run_cold_queue(str(tmp_path))
    written = list(tmp_path.rglob("*.yaml"))
    assert written == [], f"cold-path must write NO files; found: {written}"


# ---------------------------------------------------------------------------
# AC2-native-skip — lesson-promote CLI native path: skipped:true → WARN + exit 0
#
# Exercises the State-2 (coordinator_core present) skipped:true branch at
# lesson-promote lines 618-628. coordinator_core returns {"skipped": True}
# when DoE is unresolvable from the native side — the CLI must WARN + exit 0
# and write nothing, matching the legacy-path _DoeUnresolvable contract.
# ---------------------------------------------------------------------------


def _run_native_skip_lesson() -> tuple[int, str]:
    """Invoke lesson-promote main() with coordinator_core returning skipped:true.

    Returns (return_code, captured_stderr).
    """
    captured_err = io.StringIO()

    def fake_route_skip(op, params, repo_root, legacy_fn):
        # Simulate State-2 (coordinator_core present) returning skipped:true.
        return {"skipped": True, "reason": "doe root unresolvable"}

    with (
        unittest.mock.patch.object(
            _lesson_cli, "_cc_route", side_effect=fake_route_skip
        ),
        unittest.mock.patch.object(
            _lesson_cli, "_describe_schema_node", return_value=_FAKE_LESSON_SCHEMA
        ),
        unittest.mock.patch.object(
            _lesson_cli, "_resolve_from_repo", return_value="test-em"
        ),
        unittest.mock.patch.object(
            _lesson_cli, "_current_repo_root", return_value="/fake/repo"
        ),
        unittest.mock.patch("sys.stderr", captured_err),
    ):
        rc = _lesson_cli.main(_MINIMAL_LESSON_ARGV)

    return rc, captured_err.getvalue()


def test_lesson_promote_native_skip_exits_nonzero():
    """Native skipped:true must exit _EXIT_DOE_UNRESOLVABLE (3) — graceful degradation,
    but not silent success (A13 fix — native-op mirror of the legacy_fn
    _DoeUnresolvable handler; pre-A13 this returned 0, which was a defect)."""
    rc, _ = _run_native_skip_lesson()
    assert rc == _lesson_cli._EXIT_DOE_UNRESOLVABLE, (
        "native skipped:true must exit _EXIT_DOE_UNRESOLVABLE (3), not silently claim success"
    )


def test_lesson_promote_native_skip_warns_doe_root():
    """Native skipped:true → 'warn:' + 'DOE_ROOT' on stderr."""
    _, err = _run_native_skip_lesson()
    assert "warn:" in err, "must emit 'warn:' to stderr"
    assert "DOE_ROOT" in err, "WARN must mention DOE_ROOT"


# ---------------------------------------------------------------------------
# AC2-native-skip — queue-append CLI native path: skipped:true → WARN + exit 0
# ---------------------------------------------------------------------------


def _run_native_skip_queue() -> str:
    """Invoke queue-append main() with coordinator_core returning skipped:true.

    Returns captured_stderr. main() returns None on graceful paths.
    """
    captured_err = io.StringIO()

    def fake_route_skip(op, params, repo_root, legacy_fn):
        # Simulate State-2 (coordinator_core present) returning skipped:true.
        return {"skipped": True, "reason": "doe root unresolvable"}

    with (
        unittest.mock.patch("sys.argv", _CENTRAL_IQ_ARGV),
        unittest.mock.patch.object(
            _queue_cli, "_cc_route", side_effect=fake_route_skip
        ),
        unittest.mock.patch.object(
            _queue_cli, "_schema_cli_describe",
            return_value={
                "required": ["title", "body", "status", "surface",
                             "proposed_action", "from_repo", "change_kind"],
                "optional": ["queue_scope", "tags", "evidence"],
                "enums": {
                    "status": ["open", "closed", "deferred"],
                    "change_kind": ["script-edit", "doctrine-edit", "wiki-append"],
                },
            }
        ),
        unittest.mock.patch.object(
            _queue_cli, "_schema_cli_validate", return_value=(True, [])
        ),
        unittest.mock.patch.object(
            _queue_cli, "_resolve_from_repo", return_value="test-em"
        ),
        unittest.mock.patch.object(
            _queue_cli, "_current_repo_root", return_value="/fake/repo"
        ),
        unittest.mock.patch("sys.stderr", captured_err),
    ):
        _queue_cli.main()

    return captured_err.getvalue()


def test_queue_append_native_skip_warns_doe_root():
    """Native skipped:true → 'warn:' + 'CLAUDE_KLABAUTER_ROOT' on stderr.

    Rewired (DR-236, 2026-07-25): see test_queue_append_cold_warn_to_stderr's
    docstring — queue-append's central-scope WARN prefix names CLAUDE_KLABAUTER_ROOT, not
    DOE_ROOT (the fixture's own `reason` string, "doe root unresolvable", is
    test-authored filler text passed through verbatim by the native op's skip
    envelope — it does not make the CLI's own WARN prefix DOE_ROOT-flavored).
    """
    err = _run_native_skip_queue()
    assert "warn:" in err, "must emit 'warn:' to stderr"
    assert "CLAUDE_KLABAUTER_ROOT" in err, "WARN must mention CLAUDE_KLABAUTER_ROOT"


def test_queue_append_native_skip_no_file_written(tmp_path):
    """Native skipped:true → no file written anywhere."""
    _run_native_skip_queue()
    written = list(tmp_path.rglob("*.yaml"))
    assert written == [], f"native skipped:true must write NO files; found: {written}"


# ---------------------------------------------------------------------------
# C2: coordinator_registry's three machine-local read sites bound to the
# in-process reader (machine_local_impl_resolve.registry_get), CLI subprocess
# spawn retained as the fallback rung. AC3-AC6 below.
#
# Spec backlink: state/dispatch-briefs/2026-08-20-doe-root-rung-2-stops-
# spawning/C2.md
# ---------------------------------------------------------------------------
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402


def _seed_registry_local_toml(reg_dir: str, body: str) -> None:
    os.makedirs(reg_dir, exist_ok=True)
    with open(os.path.join(reg_dir, "registry.local.toml"), "w", encoding="utf-8") as fh:
        fh.write(body)


def _clear_doe_env(monkeypatch) -> None:
    for _k in ("DOE_ROOT", "REPO_DOE_CLAUDE"):
        monkeypatch.delenv(_k, raising=False)


def test_doe_root_rung2_in_process_zero_spawn(tmp_path, monkeypatch):
    """AC3: with DOE_ROOT/REPO_DOE_CLAUDE cleared and a seeded scratch
    registry, doe_root() returns the registered root AND spawns nothing —
    asserted by making subprocess.run raise if called, not by timing."""
    _clear_doe_env(monkeypatch)
    fake_root = str(tmp_path / "registered-doe-root")
    reg_dir = str(tmp_path / "registry")
    _seed_registry_local_toml(
        reg_dir,
        f'"repos.doe_claude" = {fake_root!r}\n'.replace("'", '"'),
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", reg_dir)

    def _raise_if_spawned(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called — rung 2 must resolve in-process")

    monkeypatch.setattr(_reg.subprocess, "run", _raise_if_spawned)

    result = _reg.doe_root()
    assert result == fake_root


def test_doe_root_rung2_registry_beats_codename_free_rungs(monkeypatch):
    """AC4: with the registry naming root A and a codename-free rung also
    resolving to root B, doe_root() returns A — the 2026-08-10 precedence
    regression test; must pass before and after this chunk's edit."""
    monkeypatch.setattr(_reg, "_registry_machine_local_get", lambda key: "A" if key == "repos.doe_claude" else None)
    monkeypatch.setattr(_reg, "_mp_doe_root_pointer_rung", lambda: "B")
    monkeypatch.setattr(_reg, "_mp_marketplace_cache_rung", lambda: "")
    monkeypatch.setattr(_reg, "_mp_flat_layout_probe_rung", lambda: "")
    with unittest.mock.patch.dict(os.environ, {"DOE_ROOT": "", "REPO_DOE_CLAUDE": ""}, clear=False):
        result = _reg.doe_root()
    assert result == "A"


def test_doe_root_rung2_backslash_form_passes_through_unchanged(tmp_path, monkeypatch):
    """AC4b: with registry.local.toml storing repos.doe_claude in
    backslash-drive form, doe_root() returns exactly what
    machine_local_impl_resolve.registry_get() itself computes for that same
    stored value — the value-level normalization is owned by registry_get
    (C1), and doe_root()/_registry_machine_local_get() must pass it through
    unchanged rather than re-deriving or re-splitting it. This is a
    pass-through equivalence check (doe_root() doesn't re-derive or mangle
    registry_get's output), NOT a proof of normalization-parity with the
    real CLI — that equivalence is pinned separately by
    test_machine_local_registry_reader_parity.py, not re-pinned here.
    """
    _clear_doe_env(monkeypatch)
    reg_dir = str(tmp_path / "registry")
    _seed_registry_local_toml(
        reg_dir,
        '"repos.doe_claude" = "X:\\\\DoE-claude\\\\worktree"\n',  # abs-path-ok: synthetic TOML fixture value, not a real repo reference
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", reg_dir)

    def _raise_if_spawned(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called — rung 2 must resolve in-process")

    monkeypatch.setattr(_reg.subprocess, "run", _raise_if_spawned)

    expected = _reg._mlir_registry_get("repos.doe_claude")
    assert expected, "fixture setup failed to seed a resolvable repos.doe_claude value"
    result = _reg.doe_root()
    assert result == expected


def test_doe_root_rung2_cli_fallback_fires_on_none(monkeypatch):
    """AC5: with registry_get monkeypatched to return None, the CLI spawn
    still fires and its value is returned."""
    _clear_doe_env(monkeypatch)
    monkeypatch.setattr(_reg, "_mlir_registry_get", lambda key: None)

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "/spawned/doe/root\n"

    def _fake_run(cmd, **kwargs):
        assert cmd[-2:] == ["get", "repos.doe_claude"]
        return _FakeCompletedProcess()

    monkeypatch.setattr(_reg.subprocess, "run", _fake_run)
    monkeypatch.setattr(_reg, "_mp_doe_root_pointer_rung", lambda: "")
    monkeypatch.setattr(_reg, "_mp_marketplace_cache_rung", lambda: "")
    monkeypatch.setattr(_reg, "_mp_flat_layout_probe_rung", lambda: "")

    result = _reg.doe_root()
    assert result == "/spawned/doe/root"


# ---------------------------------------------------------------------------
# AC6: same zero-spawn assertion for the module-scope manifest bootstrap
# (the hand-rolled `for _ml_cand in _mlir_machine_local_bin_candidates()`
# loop), exercised via a fresh import under a seeded registry and an absent
# co-located manifest.
# ---------------------------------------------------------------------------

_MANIFEST_FIXTURE_BODY = (
    '{"docTypes": [], "queueTypes": [], '
    '"identity": {"repoAliases": [], "centralReceiverIds": ["x-em"]}}'
)


def _build_bootstrap_fixture_tree(root: str) -> str:
    """Build a copy of coordinator_registry.py + machine_local_impl_resolve.py
    under <root>/coordinator/bin/lib/, with NO <root>/coordinator/schemas —
    so the co-located manifest rung is genuinely absent, forcing the
    split-repo bootstrap block (including this chunk's edited loop) to run.

    Returns the copied coordinator_registry.py path.
    """
    fixture_lib_dir = os.path.join(root, "coordinator", "bin", "lib")
    os.makedirs(fixture_lib_dir)
    for _name in ("coordinator_registry.py", "machine_local_impl_resolve.py"):
        _shutil.copyfile(os.path.join(str(_LIB_DIR), _name), os.path.join(fixture_lib_dir, _name))
    return os.path.join(fixture_lib_dir, "coordinator_registry.py")


def test_doe_root_module_bootstrap_zero_spawn(monkeypatch):
    """AC6: fresh import of the module-scope manifest bootstrap, with the
    registry naming a `repos.doe_claude` root that carries a real manifest —
    the hand-rolled `_mlir_machine_local_bin_candidates()` spawn loop must
    not fire, and no `.exists()` candidate in that loop's body may be
    reached via subprocess.run either."""
    _tmp = _tempfile.mkdtemp(prefix="c2-ac6-bootstrap-fixture-")
    try:
        fake_doe_root = os.path.join(_tmp, "fake-doe-claude")
        manifest_dir = os.path.join(fake_doe_root, "coordinator", "schemas")
        os.makedirs(manifest_dir)
        with open(
            os.path.join(manifest_dir, "coordinator-registry.manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(_MANIFEST_FIXTURE_BODY)

        reg_dir = os.path.join(_tmp, "registry")
        _seed_registry_local_toml(
            reg_dir,
            '"repos.doe_claude" = {!r}\n'.format(fake_doe_root).replace("'", '"'),
        )

        copied_registry_path = _build_bootstrap_fixture_tree(_tmp)

        env = dict(os.environ)
        env.pop("DOE_ROOT", None)
        env.pop("REPO_DOE_CLAUDE", None)
        env["MACHINE_LOCAL_REGISTRY_DIR"] = reg_dir

        with unittest.mock.patch.dict(os.environ, env, clear=True):
            def _raise_if_spawned(*args, **kwargs):
                raise AssertionError(
                    "subprocess.run must not be called during the module-scope "
                    "manifest bootstrap — the registry rung must resolve in-process first"
                )

            with unittest.mock.patch.object(subprocess, "run", _raise_if_spawned):
                loader = importlib.machinery.SourceFileLoader(
                    "coordinator_registry_ac6_fixture", copied_registry_path
                )
                spec = importlib.util.spec_from_loader(loader.name, loader)
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                loader.exec_module(mod)

        assert mod._MANIFEST_PATH == os.path.join(
            fake_doe_root, "coordinator", "schemas", "coordinator-registry.manifest.json"
        )
    finally:
        _shutil.rmtree(_tmp, ignore_errors=True)
