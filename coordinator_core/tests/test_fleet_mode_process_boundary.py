"""
coordinator_core.tests.test_fleet_mode_process_boundary — Clause 1's ONLY
real discharge: the real invocation door, a real subprocess, against the
PUBLISHED engine.

WHY THIS FILE EXISTS AS A SUBPROCESS-ONLY TEST, NOT AN IN-PROCESS ONE.
``state/lessons/2026-08-20-a-process-boundary-ac-cannot-be-discharged-by-
pytest.md`` established that a criterion phrased "one command, and a live
session behaves differently" is not dischargeable by pytest however
faithfully the test imitates the wire — the evidence has to be a real
command's exit code and a real subprocess's stdout, never a same-process
call into the modules under test. Every test below shells out; NONE of them
imports ``coordinator_core.session.fleet_mode``,
``coordinator_core.session.mode_resolution``, or either converted hook
module (``coordinator_core.hooks.nudge_em_code_dispatch`` /
``coordinator_core.hooks.postuse_advisory_dispatch``) to exercise them —
importing those to drive the assertion is exactly the in-process shortcut
this file exists to refuse. ``coordinator_core/hooks/tests/
test_fleet_mode_reaches_the_hooks.py`` (C3) is the in-process positive
control that proves the seam is wired in THIS interpreter; that is a
different, narrower claim than "a live session behaves differently across a
real process boundary", which only a real subprocess can establish.

TWO DOORS, NOT ONE. C4's op (``fleet.mode_set`` / ``fleet.mode_show``) is
reached through the ``coordinator-invoke`` RPC door and appears in
``coordinator_core/ops/_registry_map.py``. The converted turn-boundary hook
(DoE-claude's ``coordinator/hooks/scripts/postuse-advisory-dispatch.py``) is
invoked directly by the harness as a subprocess fed JSON on stdin — a
different transport, reached a different way, and neither substitutes for
the other. Leg 1 below exercises the first; leg 2 exercises the second.

THE ENGINE-SURFACE TRAP. ``coordinator-invoke`` and the converted hook
script both resolve the machine's PUBLISHED engine (a ``claude-klabauter``
mirror), not this working tree — a run against a mirror that predates C4
tests the OLD engine and can pass or fail for reasons that have nothing to
do with this plan. ``engine_check`` below compares live vs mirror
``_registry_map.py`` for the fleet op before either leg runs, and reports
which engine it resolved in every failure message. If the mirror does not
carry the op, the fix is a publish round (C8), never a hand-edit to the
mirror — see the lesson cited above.

ISOLATION. Every subprocess call below points ``COORDINATOR_SETTINGS_HOME``
at a fresh, per-test directory seeded only with a copy of the REAL
machine-local registry (needed so the engine-resolution seam still resolves
the real published engine) — never at the real, shared, machine-wide
``fleet-mode.json`` or context-usage sidecar, which a real peer session on
this box may be reading or writing concurrently.

SKIP DISCIPLINE. Per this module's own dispatch brief: "Skip loudly with a
stated reason if the door is unavailable; never pass by degrading to an
import." Every door-unavailable branch below is a ``pytest.skip`` with a
concrete reason, never a silent pass and never a fallback to an in-process
call.

Spec backlink: state/dispatch-briefs/2026-08-28-the-fleet-gets-one-file-and-
the-floor-moves-to-the-reader/C7.md
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HOME_ENV = "COORDINATOR_SETTINGS_HOME"
_NO_CONSOLE = no_console_creationflags()


def _real_settings_home() -> Path:
    override = os.environ.get(_HOME_ENV)
    return Path(override) if override else Path.home() / ".coordinator-claude-settings"


def _coordinator_invoke_binary(real_home: Path) -> Path | None:
    for name in ("coordinator-invoke.exe", "coordinator-invoke.cmd", "coordinator-invoke"):
        candidate = real_home / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def _engine_provenance(real_home: Path) -> dict | None:
    candidate = real_home / "bin" / "coordinator-invoke.exe.provenance.json"
    if not candidate.is_file():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _registry_text(real_home: Path) -> str | None:
    candidate = real_home / "machine-local" / "registry.local.toml"
    if not candidate.is_file():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return None


def _registry_value(text: str, key: str) -> str | None:
    """Minimal single-key TOML string-value reader — this file must not
    depend on a TOML library being installed just to locate a sibling
    repo root."""
    pattern = re.compile(r'^"' + re.escape(key) + r'"\s*=\s*\'([^\']*)\'', re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else None


def _doe_claude_root(real_home: Path) -> Path | None:
    text = _registry_text(real_home)
    if text is None:
        return None
    value = _registry_value(text, "repos.doe_claude")
    return Path(value) if value else None


def _postuse_advisory_dispatch_script(doe_root: Path) -> Path | None:
    candidate = doe_root / "coordinator" / "hooks" / "scripts" / "postuse-advisory-dispatch.py"
    return candidate if candidate.is_file() else None


def _live_registry_map_text() -> str:
    path = Path(__file__).resolve().parents[1] / "ops" / "_registry_map.py"
    return path.read_text(encoding="utf-8")


def _mirror_registry_map_text(engine_root: str) -> str | None:
    candidate = Path(engine_root) / "coordinator_core" / "ops" / "_registry_map.py"
    if not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8")


def _fleet_op_present(text: str) -> bool:
    return '"fleet.mode_set"' in text and '"fleet.mode_show"' in text


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write-to-temp-then-``os.replace`` so a peer concurrently reading
    ``path`` never observes a torn/partial write."""
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _isolated_settings_home(tmp_path: Path, real_home: Path) -> Path:
    """A fresh settings home carrying only a copy of the REAL machine-local
    registry -- so the engine-resolution seam (both the compiled
    ``coordinator-invoke`` door and DoE-claude's ``_engine_root.py`` ladder)
    still resolves the real published engine, while every file this test
    writes (fleet-mode.json, the context-usage sidecar) lands in an
    isolated location the real machine never sees."""
    home = tmp_path / "isolated-settings-home"
    home.mkdir()
    real_machine_local = real_home / "machine-local"
    if real_machine_local.is_dir():
        shutil.copytree(real_machine_local, home / "machine-local")
    return home


@pytest.fixture(scope="module")
def real_home() -> Path:
    return _real_settings_home()


@pytest.fixture(scope="module")
def engine_check(real_home):
    """THE ENGINE-SURFACE TRAP guard. Resolves the engine ``coordinator-
    invoke`` actually targets (via its own build provenance file) and
    compares that mirror's ``_registry_map.py`` against this live tree's
    own copy for the fleet op, per this module's own docstring and
    ``state/lessons/2026-08-20-a-process-boundary-ac-cannot-be-discharged-
    by-pytest.md``.

    Skips (door unavailable) rather than fails when no provenance file can
    be found at all -- that is an environment gap, not evidence the plan's
    mechanism is broken. FAILS (never skips, never passes) when a
    provenance file names an engine that lacks the op: per this chunk's own
    hard constraint, "if this chunk somehow runs before C8 has landed, it
    must report unreachable-pending-publish and FAIL -- never pass, and
    never quietly fall back to an in-process import to get green."
    """
    prov = _engine_provenance(real_home)
    if prov is None:
        pytest.skip(
            f"no coordinator-invoke provenance file under {real_home / 'bin'} -- "
            "cannot verify which engine the real door resolves; the real "
            "invocation door is unavailable on this machine"
        )
    engine_root = prov.get("engine_root")
    if not engine_root:
        pytest.skip("coordinator-invoke provenance file carries no engine_root")

    live_present = _fleet_op_present(_live_registry_map_text())
    mirror_text = _mirror_registry_map_text(engine_root)

    if mirror_text is None:
        pytest.fail(
            f"unreachable-pending-publish: the published engine at {engine_root!r} "
            "has no coordinator_core/ops/_registry_map.py at all -- fleet.mode_set/"
            "fleet.mode_show cannot be reached through the real door on this "
            "machine. This is a publish-round gap (C8), not a code defect in this "
            "chunk."
        )

    mirror_present = _fleet_op_present(mirror_text)
    if live_present and not mirror_present:
        pytest.fail(
            "unreachable-pending-publish: the live tree registers "
            "fleet.mode_set/fleet.mode_show but the PUBLISHED engine at "
            f"{engine_root!r} does not -- per state/lessons/2026-08-20-a-process-"
            "boundary-ac-cannot-be-discharged-by-pytest.md, the fix is a publish "
            "round (C8), never a hand-edit to the mirror (undone by the next "
            "publish)."
        )
    if not mirror_present:
        pytest.fail(
            f"the published engine at {engine_root!r} does not register "
            f"fleet.mode_set/fleet.mode_show (live tree has it: {live_present}); "
            "the real door cannot reach C4's op on this machine"
        )

    return {
        "engine_root": engine_root,
        "verdict": (
            f"published engine at {engine_root!r} registers fleet.mode_set/"
            "fleet.mode_show -- verified against the live tree's own "
            "_registry_map.py"
        ),
    }


# ---------------------------------------------------------------------------
# Leg 1 (write via the real door) + Leg 3 (no message, no enumeration),
# asserted negatively over leg 1's own real-door run.
# ---------------------------------------------------------------------------


_SESSION_LIKE_DIR_NAMES = {"sessions", "outbox", "cross-repo", "cross-repo-inbox"}


def test_leg1_real_door_write_and_leg3_no_session_traffic(tmp_path, real_home, engine_check):
    """Leg 1: invoke fleet.mode_set the way a human will -- the
    coordinator-invoke surface, not a Python call into the module -- and
    assert on ITS EXIT CODE and output, then confirm the file appeared at
    settings_home()/fleet-mode.json.

    Leg 3, folded in here per this chunk's own instructions ("assert
    negatively over leg 1"): the op's real-door execution must open no
    session registry, resolve no peer address, and spawn no messaging
    surface. Asserted black-box, over whichever settings home the write
    actually landed in: the ONLY file this run may create is
    fleet-mode.json itself, and no session/messaging-shaped directory may
    appear.

    THE WARM-SERVER CAVEAT (empirically confirmed on this machine, not a
    defect in this test). The compiled ``coordinator-invoke`` door
    round-trips through a machine-wide warm server keyed on
    (user, engine-clone, engine-token) -- NOT on ``COORDINATOR_SETTINGS_
    HOME``. Once that server is alive for this box's engine clone, it
    resolves ``settings_home()`` against ITS OWN spawn-time environment,
    not this test's per-call override or ``COORDINATOR_WARM=0`` (verified:
    a read-only ``fleet.mode_show`` call under an isolated env + COORDINATOR_
    WARM=0 still reported this machine's real ``compaction_warnings``
    value). This test therefore checks BOTH the isolated home and the real
    one for where the write actually landed, asserts against whichever one
    changed, and -- in a ``finally``, ONLY WHEN this run's own write actually
    landed in the real (shared, machine-wide) file rather than the isolated
    one -- restores it to its exact pre-test bytes (or removes it if it did
    not exist). The restore is CONDITIONAL, not unconditional: on ~50 live
    peer sessions, an unconditional restore would rewrite a shared file this
    test never touched, and blindly restoring stale pre-test bytes risks
    clobbering a peer's write that lands between this test's snapshot and its
    restore. So when the real file was the one written to, this test
    re-reads it immediately before restoring and only restores if its
    content still matches the exact bytes this test itself wrote -- if a peer
    mutated it in between, this test leaves the peer's write alone rather
    than overwrite it with a stale snapshot. The restore write itself is
    atomic (write-to-temp + ``os.replace``) so a peer reading the file
    mid-restore never observes a torn write. Only the ``autonomous`` key is
    ever written here (never ``compaction_warnings``): it is session-wins by
    design (mode_resolution.py), so a transient fleet value never changes
    any *other* live session's own behaviour even during the tiny window
    this test's write is live.
    """
    binary = _coordinator_invoke_binary(real_home)
    if binary is None:
        pytest.skip(
            f"no coordinator-invoke binary under {real_home / 'bin'} -- the real "
            "invocation door is unavailable on this machine"
        )

    home = _isolated_settings_home(tmp_path, real_home)
    env = dict(os.environ)
    env[_HOME_ENV] = str(home)
    env["COORDINATOR_WARM"] = "0"

    isolated_fleet_file = home / "fleet-mode.json"
    real_fleet_file = real_home / "fleet-mode.json"
    real_before = real_fleet_file.read_bytes() if real_fleet_file.is_file() else None

    isolated_dirs_before = {p.name for p in home.iterdir() if p.is_dir()}
    real_dirs_before = (
        {p.name for p in real_home.iterdir() if p.is_dir()} if real_home.is_dir() else set()
    )

    # Set only when the warm-server caveat fires and this run's own write
    # actually lands in the real, shared file -- gates the conditional
    # restore in `finally` below. `real_after_write` is this test's own
    # write, captured immediately, so the restore can detect a peer's write
    # landing in the interim rather than blindly overwriting it.
    wrote_to_real_home = False
    real_after_write: bytes | None = None

    try:
        result = subprocess.run(
            [str(binary), "fleet.mode_set", json.dumps({"key": "autonomous", "value": "on"})],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )

        assert result.returncode == 0, (
            f"real-door write failed against {engine_check['verdict']}; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        response = json.loads(result.stdout)
        assert response.get("result", {}).get("autonomous") is True, (
            f"unexpected real-door response: {result.stdout!r}"
        )

        if isolated_fleet_file.is_file():
            record = json.loads(isolated_fleet_file.read_text(encoding="utf-8"))
            assert record.get("autonomous") is True

            after_dirs = {p.name for p in home.iterdir() if p.is_dir()}
            after_files = {p.name for p in home.iterdir() if p.is_file()}
            unexpected_dirs = (after_dirs - isolated_dirs_before) & _SESSION_LIKE_DIR_NAMES
            unexpected_files = after_files - {"fleet-mode.json", "machine-local"}
        else:
            # Warm-server caveat fired: the write landed in the REAL home
            # instead of the isolated one this test tried to redirect it
            # to -- still the real door, against the real published
            # engine, still settings_home()'s own resolution for this
            # call.
            assert real_fleet_file.is_file(), (
                "fleet-mode.json appeared at NEITHER the isolated settings home "
                f"({isolated_fleet_file}) NOR the real one ({real_fleet_file}) "
                "after a successful fleet.mode_set call"
            )
            wrote_to_real_home = True
            real_after_write = real_fleet_file.read_bytes()
            record = json.loads(real_after_write.decode("utf-8"))
            assert record.get("autonomous") is True

            after_dirs = {p.name for p in real_home.iterdir() if p.is_dir()}
            unexpected_dirs = (after_dirs - real_dirs_before) & _SESSION_LIKE_DIR_NAMES
            unexpected_files = set()  # real home is shared/busy -- file-level
            # diffing there is unsafe (concurrent peer writes); the directory-
            # level denylist check above is leg 3's evidence in this branch.

        assert not unexpected_dirs, (
            "fleet.mode_set's real-door run created session/messaging-shaped "
            f"directories: {sorted(unexpected_dirs)} -- no session was enumerated "
            "or messaged by a write to a single file"
        )
        assert not unexpected_files, (
            "fleet.mode_set's real-door run created file(s) beyond fleet-mode.json: "
            f"{sorted(unexpected_files)} -- clause 1 forbids any session-registry / "
            "peer-address / messaging-surface side effect"
        )
    finally:
        # CONDITIONAL restore: only when this run's own write actually
        # landed in the real, shared file (the warm-server caveat branch).
        # When the write landed in the isolated home instead -- the common
        # case -- the real file was never touched by this test and must not
        # be rewritten at all, atomically or otherwise.
        if wrote_to_real_home:
            current = real_fleet_file.read_bytes() if real_fleet_file.is_file() else None
            if current != real_after_write:
                # TOCTOU: a peer session mutated the real, shared file
                # between this test's write and this restore. The pre-test
                # snapshot (`real_before`) is now stale -- restoring it would
                # clobber the peer's write with bytes that predate it. Leave
                # the file exactly as the peer left it.
                pass
            elif real_before is None:
                try:
                    real_fleet_file.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_write_bytes(real_fleet_file, real_before)


# ---------------------------------------------------------------------------
# Leg 2 (read via a real hook process) -- "a live session behaves
# differently", established across a real process boundary.
# ---------------------------------------------------------------------------


def test_leg2_real_hook_subprocess_reflects_fleet_value(tmp_path, real_home, engine_check):
    """Execute the converted turn-boundary hook entry point AS A SUBPROCESS
    with a harness-shaped JSON payload on stdin, once with no fleet file
    and once with ``compaction_warnings: informational`` set, and assert
    its STDOUT differs in the declared way. A same-process call into
    ``postuse_advisory_dispatch`` cannot establish this half of clause 1 --
    see this module's own docstring.
    """
    doe_root = _doe_claude_root(real_home)
    if doe_root is None or not doe_root.is_dir():
        pytest.skip(
            "could not resolve repos.doe_claude from the real machine-local "
            "registry -- the DoE-claude root (home of the converted turn-boundary "
            "hook entry point) is unavailable on this machine"
        )
    script = _postuse_advisory_dispatch_script(doe_root)
    if script is None:
        pytest.skip(
            f"postuse-advisory-dispatch.py not found under {doe_root} -- the "
            "converted turn-boundary hook entry point is unavailable on this "
            "machine"
        )

    home = _isolated_settings_home(tmp_path, real_home)
    context_dir = home / "state" / "context-window"
    context_dir.mkdir(parents=True)

    def _write_usage(session_id: str, used_pct: float) -> None:
        stem = "".join(c for c in session_id if c.isalnum() or c in "-_")
        record = {
            "context_window": {
                "used_percentage": used_pct,
                "remaining_percentage": 100 - used_pct,
            },
            "captured_at": time.time(),
        }
        (context_dir / f"{stem}.json").write_text(json.dumps(record), encoding="utf-8")

    def _run(session_id: str) -> str:
        payload = json.dumps({
            "session_id": session_id,
            "tool_name": "Read",
            "transcript_path": "/does/not/matter/transcript.jsonl",
        })
        env = dict(os.environ)
        env[_HOME_ENV] = str(home)
        result = subprocess.run(
            [sys.executable, str(script)],
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )
        assert result.returncode == 0, (
            f"real hook subprocess exited nonzero against {engine_check['verdict']}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        return result.stdout

    # A fresh, unique session_id per call: the hook's own durable throttle/
    # bark-once state is keyed by session_id in the platform temp dir, and
    # this test must not depend on -- or trip over -- a prior run's state.
    session_a = f"c7leg2a-{uuid.uuid4().hex[:12]}"
    _write_usage(session_a, 48.0)
    stdout_no_fleet = _run(session_a)

    assert stdout_no_fleet.strip(), (
        "baseline (no fleet file) run produced no advisory at all -- cannot "
        "prove a difference; expected the standard HANDOFF NOW text at 48% usage "
        f"against {engine_check['verdict']}"
    )
    assert "HANDOFF NOW" in stdout_no_fleet
    assert "INFORMATIONAL" not in stdout_no_fleet

    (home / "fleet-mode.json").write_text(
        json.dumps({"compaction_warnings": "informational"}), encoding="utf-8"
    )

    session_b = f"c7leg2b-{uuid.uuid4().hex[:12]}"
    _write_usage(session_b, 48.0)
    stdout_with_fleet = _run(session_b)

    assert stdout_no_fleet != stdout_with_fleet, (
        "the real hook subprocess's stdout did not change between the no-fleet-"
        "file baseline and a fleet compaction_warnings=informational run -- the "
        "fleet file is not reaching a live session's turn-boundary hook fire"
    )
    assert "INFORMATIONAL" in stdout_with_fleet
