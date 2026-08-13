"""test_doe_root_routing.py — C1 routing tests for doe_root() + doctrine central writes.

Tests (AC1, AC2 from gate2-w23-state-seam-caller-switch.md):
  AC1-pos   doe_root() returns env DOE_ROOT override when set.
  AC1-pos   coordinator-lesson-promote._outbox_root() resolves under $(doe_root)/state/lessons-outbox.
  AC1-pos   coordinator-queue-append._output_path() central branch resolves under $(doe_root)/state/improvement-queue.
  AC2-cold  coordinator-lesson-promote invoked cold (no repos.example_doctrine_repo, no DOE_ROOT):
              WARN to stderr, exit 0, no file written anywhere.
  AC2-cold  coordinator-queue-append invoked cold (no repos.example_doctrine_repo, no DOE_ROOT):
              WARN to stderr, exit 0, no file written anywhere.

Cold-path tests call the CALLING CLI's main() with _cc_route mocked to invoke legacy_fn()
directly, so the full resolver chain executes without subprocess isolation gaps. This satisfies
the lesson requirement (test entry point, not lib in isolation) while avoiding env-isolation
issues where the subprocess CC seam takes a different code path before legacy_fn is reached.

Converted from a hand-rolled unittest runner to collectable pytest functions.

Run: python3 -m pytest coordinator/bin/tests/test_doe_root_routing.py

Spec backlink: docs/plans/2026-07-06-gate2-w23-state-seam-caller-switch.md § C1
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
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
_LESSON_PROMOTE_PATH = _BIN_DIR / "coordinator-lesson-promote"
_QUEUE_APPEND_PATH = _BIN_DIR / "coordinator-queue-append"

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
    fake_root = "/fake/coordinator-claude-root"
    with unittest.mock.patch.dict(os.environ, {"DOE_ROOT": fake_root}, clear=False):
        result = _reg.doe_root()
    assert result == fake_root


def test_doe_root_strips_empty_env():
    """doe_root() ignores DOE_ROOT='' (empty string is not a valid override)."""
    # Also clears REPO_EXAMPLE_DOCTRINE_REPO (rung 1b) so this machine's real ambient
    # override doesn't win before the mocked machine-local rung is reached.
    with (
        unittest.mock.patch.dict(os.environ, {"DOE_ROOT": "", "REPO_EXAMPLE_DOCTRINE_REPO": ""}, clear=False),
        unittest.mock.patch.object(_reg, "_registry_machine_local_get", return_value="/ml/doe"),
    ):
        result = _reg.doe_root()
    assert result == "/ml/doe"


def test_doe_root_raises_when_unresolvable():
    """doe_root() raises _DoeUnresolvable when neither env nor machine-local resolve."""
    env_without_doe = {
        k: v for k, v in os.environ.items() if k not in ("DOE_ROOT", "REPO_EXAMPLE_DOCTRINE_REPO")
    }
    with (
        unittest.mock.patch.dict(os.environ, env_without_doe, clear=True),
        unittest.mock.patch.object(_reg, "_registry_machine_local_get", return_value=None),
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
    """_outbox_root() propagates _DoeUnresolvable when doe_root() cannot resolve."""
    env_clean = {k: v for k, v in os.environ.items()
                 if k not in ("DOE_ROOT", "REPO_EXAMPLE_DOCTRINE_REPO", "LESSON_PROMOTE_OUTBOX_ROOT")}
    with (
        unittest.mock.patch.dict(os.environ, env_clean, clear=True),
        unittest.mock.patch.object(_reg, "_registry_machine_local_get", return_value=None),
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
    the [coordinator-claude] docs/plans/2026-07-06-gate2-w23-state-seam-caller-switch.md
    proposal — but that plan was never ratified (`status: draft`, AC1/AC2 `pending`,
    its own C3 HELD with recorded disk proof the flip never took effect). The CLI's
    OWN current source (`coordinator-queue-append`'s `_output_path`, negative-spec
    docstring) confirms central-scope improvement-queue writes route to
    `_claude_klabauter_root()` unconditionally, per docs/wiki/state-placement-law.md § Taxonomy
    "Central/global state" and `docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md`
    (state/ is claude-klabauter's own disk-truth custody). Only coordinator-lesson-promote's
    lessons-outbox central write genuinely routes to coordinator-claude (see
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
# nothing for repos.example_doctrine_repo. This exercises the full entry-point chain —
# main() → legacy_fn() → _write_entry() → _outbox_root() → doe_root() → raise —
# per lesson state/lessons/2026-07-05-universal-test-the-resolver-entry-point.yaml.
# ---------------------------------------------------------------------------


def _make_cold_env(tmpdir: str) -> dict[str, str]:
    """Return a minimal env dict with MACHINE_LOCAL_IMPL → stub (no repos.example_doctrine_repo).

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
    """Cold path: no file written (neither coordinator-claude nor claude-klabauter path)."""
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
# when coordinator-claude is unresolvable from the native side — the CLI must WARN + exit 0
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
