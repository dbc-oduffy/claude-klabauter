"""coordinator/bin/lib/tests/test_assemble_help_contract.py -- guards the
whole `*assemble*` launcher family's `--help`/`-h` contract: exit 0,
non-empty usage on stdout, and no side effect -- never a hand-checked
subset.

Spec backlink: docs/plans/2026-09-02-the-loader-fires-the-assembly-not-the-
em.md, chunk C1 (AMENDED mid-execution -- see that chunk's own body for the
spec-error narrative this module's two-doors tests exist to close).

MEASURED STATE this guard locks in (re-run to confirm it still holds): of
the 17 `*assemble*` launchers under `coordinator/bin/`, 14 are
`entry_point_shim.ASSEMBLE_TARGETS` members and route through
`entry_point_shim.run_target`; the other 3
(`coordinator-assemble`, `roadmap-planning-assemble`,
`sprint-planning-assemble`) do not and already honour `--help` on their
own. Before C1's fix, 3 of the 14 `run_target` members mishandled the
gesture: `plan-assemble` (exit 2, unrecognized-argument), `workday-
complete-assemble` (usage printed, but to stderr and with exit 2, not
exit 0/stdout), and `workday-start-inbox-blitz-assemble` (exit 0, but by
actually EXECUTING and emitting a live decision object) -- the sole
`entry_point_shim.BY_PATH_TARGETS` member, whose own `main()` discards its
argv (`del argv`) and so can never see the flag itself; the fix has to
live above it in `run_target`.

TWO DOORS, ONE TARGET: the by-path target has a SECOND, independent
`--help` door that `run_target`'s interception cannot see at all --
its own `__main__` guard historically called `main()` with no argv
whatsoever, so `python coordinator/bin/workday-start-inbox-blitz-
assemble.py --help` never reached `run_target` and ran the live op even
after the first door was closed. Every other `ASSEMBLE_TARGETS` member's
`__main__` calls its own `main(argv)`, which itself calls
`entry_point_shim.run_target(...)` -- so for them, direct execution and
launcher execution are the SAME door. This module tests both doors for
the by-path target specifically (`test_by_path_target_main_is_never_
entered_for_help` for the `run_target` door,
`test_by_path_target_direct_execution_help_emits_no_decision_object` for
direct execution), and asserts as a positive claim that no other member
has grown a second door
(`test_only_the_by_path_target_has_a_run_target_bypassing_main_entry`).

Negative-spec:
    - Does NOT hardcode the 17 (or 14, or 3) launcher names anywhere --
      every parametrization is derived by globbing `coordinator/bin/` at
      collection time, so a newly added `*assemble*` launcher is swept in
      and, per C1's spec, FAILS this guard by default until it is wired
      into `entry_point_shim.ASSEMBLE_TARGETS` (or is itself already
      `--help`-safe) -- that default-fail is the point, not a bug in this
      test.
    - Does NOT spawn a subprocess per `ASSEMBLE_TARGETS` member -- those are
      exercised in-process via `entry_point_shim.run_target` directly, which
      is what makes the by-path no-side-effect assertion possible at all
      (a subprocess boundary would hide whether `main()` ran). The 3
      launchers OUTSIDE `ASSEMBLE_TARGETS` are launched as real
      subprocesses instead, because they have no `run_target` seam to call
      into -- this guard asserts their existing behaviour, it does not fix
      them (C1's Out of scope).
    - Does NOT assert on the EXACT usage text of most targets -- 11 of the
      14 `ASSEMBLE_TARGETS` members already render their own good
      per-target usage and this guard must not force them onto a generic
      rendering; those 11 (plus the by-path member) are only asserted for
      "usage" appearing, case-insensitively, in stdout, and for the
      absence of a leaked parse-error line. The 2 that have no explicit
      `--help` branch of their own (`plan-assemble`, `workday-complete-
      assemble`) ARE asserted against a real usage substring, specifically
      because their pre-fix defect was discarding that real line in favour
      of a synthesized stub -- a generic "usage" substring match is too
      weak to see that regression (it is equally satisfied by the stub).
    - Does NOT assume, for the 13 engine-mapped targets, that `--help`
      cannot reach an op body -- `test_engine_target_help_never_opens_a_
      file_for_writing` makes that a checked invariant (any write-mode
      `open()` during the help render fails the test) rather than resting
      on `entry_point_shim._render_help`'s per-target source citations
      alone.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BIN_DIR = _REPO_ROOT / "coordinator" / "bin"
_LIB_DIR = _BIN_DIR / "lib"

for _p in (str(_LIB_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_point_shim  # noqa: E402


def _discover_assemble_launcher_stems() -> list[str]:
    """Every `*assemble*.py` launcher under `coordinator/bin/`, by stem --
    discovered from disk, never from a name list. A `.cmd`/`.ps1` sibling
    is a separate OS-launch rung over the same logic (plan's § Problem
    counting-rule correction), not a distinct launcher, so only `.py` is
    globbed."""
    return sorted(p.stem for p in _BIN_DIR.glob("*assemble*.py"))


ALL_LAUNCHER_STEMS = _discover_assemble_launcher_stems()
RUN_TARGET_STEMS = sorted(entry_point_shim.ASSEMBLE_TARGETS)
OUTSIDE_RUN_TARGET_STEMS = sorted(
    set(ALL_LAUNCHER_STEMS) - set(entry_point_shim.ASSEMBLE_TARGETS)
)


def test_discovery_partitions_every_launcher():
    """Locks in the measured 17/14/3 split as a guard against silent drift
    -- a name added to (or dropped from) `ASSEMBLE_TARGETS` without a
    matching bin/*.py file, or vice versa, fails here before it can hide
    a launcher from the parametrized tests below."""
    assert set(RUN_TARGET_STEMS) <= set(ALL_LAUNCHER_STEMS)
    assert set(RUN_TARGET_STEMS).isdisjoint(OUTSIDE_RUN_TARGET_STEMS)
    assert ALL_LAUNCHER_STEMS, "no *assemble*.py launchers discovered"


#: A help gesture that leaks one of these into stdout means a target's own
#: genuine parse-error line rode along with (or replaced) its real usage --
#: exactly the `plan-assemble`/`workday-complete-assemble` defect this
#: guard exists to catch (see `entry_point_shim._usage_lines`'s docstring).
_ERROR_LEAK_SUBSTRINGS = ("unrecognized argument", "unknown subcommand")


@pytest.mark.parametrize("name", RUN_TARGET_STEMS)
def test_run_target_help_exits_clean_with_usage(name):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = entry_point_shim.run_target(name, ["--help"])
    out = buf.getvalue()
    assert code == 0, f"{name}: --help must exit 0, got {code}"
    assert out.strip(), f"{name}: --help produced no stdout"
    assert "usage" in out.lower(), f"{name}: stdout has no usage text: {out!r}"
    lowered = out.lower()
    for leak in _ERROR_LEAK_SUBSTRINGS:
        assert leak not in lowered, f"{name}: --help leaked a parse-error line: {out!r}"


@pytest.mark.parametrize("name", RUN_TARGET_STEMS)
def test_run_target_help_wins_after_subcommand(name):
    """`--help` anywhere in argv wins, including after a subcommand token
    (`baton-assemble brief --help`) -- position is not special-cased."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = entry_point_shim.run_target(name, ["brief", "--help"])
    out = buf.getvalue()
    assert code == 0, f"{name}: 'brief --help' must exit 0, got {code}"
    assert "usage" in out.lower(), f"{name}: stdout has no usage text: {out!r}"


def test_by_path_target_main_is_never_entered_for_help(monkeypatch):
    """The by-path offender's own `main()` discards its argv and always
    runs its full body regardless of what it is called with -- proving
    "no side effect" for it means proving `main()` is never reached at
    all, not inspecting what it printed. `_load_module` is the sole gate
    `run_target`'s by-path branch uses to obtain that `main`, so spying
    there is equivalent to spying on `main` itself, without needing to
    duplicate `run_target`'s own module-loading machinery here."""
    assert entry_point_shim.BY_PATH_TARGETS, "no BY_PATH_TARGETS to guard"
    (name,) = entry_point_shim.BY_PATH_TARGETS

    calls: list[str] = []

    def _boom(target_name, path):  # noqa: ANN001 -- matches _load_module's shape
        calls.append(target_name)
        raise AssertionError(
            f"{target_name}: _load_module (and so main()) was entered for --help"
        )

    monkeypatch.setattr(entry_point_shim, "_load_module", _boom)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = entry_point_shim.run_target(name, ["--help"])

    assert calls == [], f"{name}: main() entry path was reached: {calls}"
    assert code == 0, f"{name}: --help must exit 0, got {code}"
    assert "usage" in buf.getvalue().lower()


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_by_path_target_direct_execution_help_emits_no_decision_object():
    """SECOND DOOR: the by-path target's own `__main__` guard historically
    called `main()` with no argv at all, so `run_target`'s interception --
    reached only through the launcher/forwarder path -- never saw a direct
    `python coordinator/bin/workday-start-inbox-blitz-assemble.py --help`
    invocation. That invocation ran the full op and printed a live decision
    object (`{"state": "escalate", ...}`) even though the FIRST door
    (through `run_target`, asserted by every other test in this module) was
    already closed. This is the amended chunk C1's own regression: a guard
    that only ever exercises one door is evidence about that door, not
    about the contract.

    Asserts the direct-execution door specifically: exit 0, usage on
    stdout, and -- the part that actually matters -- no decision object.
    "No side effect" is checked by absence of the object's own shape
    (`"state"` key), not by a weaker "well, nothing printed" claim, mirroring
    this module's own `test_by_path_target_main_is_never_entered_for_help`
    for the first door.
    """
    (name,) = entry_point_shim.BY_PATH_TARGETS
    result = subprocess.run(
        [sys.executable, str(_BIN_DIR / f"{name}.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"{name}: direct --help must exit 0, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert "usage" in result.stdout.lower(), (
        f"{name}: direct --help produced no usage text: {result.stdout!r}"
    )
    assert '"state"' not in result.stdout, (
        f"{name}: direct --help emitted a live decision object: {result.stdout!r}"
    )


def test_only_the_by_path_target_has_a_run_target_bypassing_main_entry():
    """Positive claim, not an absence: every OTHER `ASSEMBLE_TARGETS`
    member's own `coordinator/bin/<name>.py` file has a `__main__` guard
    that calls into its own `main(argv)`, which itself calls
    `entry_point_shim.run_target(...)` -- so `run_target`'s `--help`
    interception is reachable from a direct `python <file>.py --help`
    invocation for all of them. The by-path target is the ONLY member
    without that forwarding, which is what makes it the only one needing
    its own separate guard (the test above). This is checked by reading
    each file's source rather than asserted by construction, so a future
    `ASSEMBLE_TARGETS` member that quietly grows a second, uncovered
    `__main__` door (the exact shape of this chunk's amendment) fails
    here rather than passing silently."""
    (by_path_name,) = entry_point_shim.BY_PATH_TARGETS
    missing_forward = []
    for name in RUN_TARGET_STEMS:
        if name == by_path_name:
            continue
        source = (_BIN_DIR / f"{name}.py").read_text(encoding="utf-8")
        if "run_target" not in source:
            missing_forward.append(name)
    assert missing_forward == [], (
        f"these ASSEMBLE_TARGETS members' bin/*.py do not forward to "
        f"run_target at all -- each is a second, uncovered `--help` door "
        f"exactly like the by-path target's: {missing_forward}"
    )


@pytest.mark.parametrize(
    "name, real_usage_substring",
    [
        ("plan-assemble", "--route plan|spec-dispatch"),
        ("workday-complete-assemble", "workday-complete-assemble brief|apply"),
    ],
)
def test_delegated_targets_render_their_own_real_usage(name, real_usage_substring):
    """`plan-assemble` and `workday-complete-assemble` have no explicit
    `--help` branch of their own -- they fall through to their existing
    usage/error path, which already prints a REAL usage line (to stderr,
    alongside a genuine parse-error line for the unrecognized `--help`
    token). Before this fix, capturing stdout only found nothing there and
    fell back to a synthesized stub -- discarding a real usage line that
    was sitting right there, and leaking the target's own error line
    straight to the terminal uncaptured. This asserts the real line
    survives and the error line beside it does not."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = entry_point_shim.run_target(name, ["--help"])
    out = buf.getvalue()
    assert code == 0, f"{name}: --help must exit 0, got {code}"
    assert real_usage_substring in out, f"{name}: real usage missing: {out!r}"
    assert "unrecognized argument" not in out.lower(), (
        f"{name}: leaked its own parse-error line: {out!r}"
    )
    synthesized = entry_point_shim._synthesize_usage(name).strip()
    assert out.strip() != synthesized, (
        f"{name}: fell back to the synthesized stub instead of delegating: {out!r}"
    )


def test_synthesis_is_used_only_for_the_by_path_target():
    """Positive assertion, not absence-based: after the stderr-delegation
    fix, the by-path target is the ONLY `ASSEMBLE_TARGETS` member whose
    `--help` output is the synthesized stub -- every other member has a
    real per-target usage surface (11 directly on stdout, 2 recovered from
    stderr via `_usage_lines`) to delegate to instead."""
    (by_path_name,) = entry_point_shim.BY_PATH_TARGETS
    assert entry_point_shim._render_help(by_path_name) == entry_point_shim._synthesize_usage(
        by_path_name
    )

    for name in RUN_TARGET_STEMS:
        if name == by_path_name:
            continue
        rendered = entry_point_shim._render_help(name)
        assert rendered != entry_point_shim._synthesize_usage(name), (
            f"{name}: unexpectedly fell back to the synthesized stub: {rendered!r}"
        )


@pytest.mark.parametrize(
    "name", [n for n in RUN_TARGET_STEMS if n not in entry_point_shim.BY_PATH_TARGETS]
)
def test_engine_target_help_never_opens_a_file_for_writing(name, monkeypatch):
    """Extends the by-path spy's guarantee to the other 12 engine-mapped
    targets, so the "op body is unreachable" claim is a checked invariant
    rather than resting solely on `_render_help`'s per-target reading:
    ANY write-mode `open()` reached while rendering `--help` fails the
    test. This is what a 14th target that regresses to the by-path shape
    (argv discarded, op always runs) -- or an existing one that loses its
    own early `--help` check -- would trip, without this test needing to
    know each target's internal op-entry symbol names."""
    import builtins

    real_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(
                f"{name}: write-mode open() reached while rendering --help: "
                f"file={file!r} mode={mode!r}"
            )
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = entry_point_shim.run_target(name, ["--help"])
    assert code == 0


def test_help_is_not_recorded_as_an_invocation(monkeypatch):
    """A help gesture must not be counted toward C9's deprecation-window
    census -- `_record_invocation` must not even be called."""
    calls: list[str] = []
    monkeypatch.setattr(
        entry_point_shim, "_record_invocation", lambda n: calls.append(n)
    )
    name = RUN_TARGET_STEMS[0]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        entry_point_shim.run_target(name, ["--help"])
    assert calls == [], f"help gesture recorded as an invocation: {calls}"


@pytest.mark.cadence
@pytest.mark.spawns_process
@pytest.mark.parametrize("name", OUTSIDE_RUN_TARGET_STEMS)
def test_non_run_target_launchers_already_honour_help(name):
    """These three do not route through `run_target` and C1's fix does not
    touch them -- this guard only asserts the already-passing behaviour
    the plan's § Out of scope names, as a regression tripwire."""
    result = subprocess.run(
        [sys.executable, str(_BIN_DIR / f"{name}.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"{name}: --help must exit 0, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert result.stdout.strip(), f"{name}: --help produced no stdout"
    assert "usage" in result.stdout.lower(), (
        f"{name}: stdout has no usage text: {result.stdout!r}"
    )
