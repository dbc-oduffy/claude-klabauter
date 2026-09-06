"""scripts/cloud_setup.py — the cloud provisioning entrypoint for the engine.

Purpose: this is the single decision-carrying artifact behind the bash pasted into a
claude.ai cloud environment's Setup script field (docs/install/cloud-environment.md, C3).
That field "runs when a new cloud session starts, before Claude Code launches"
(https://code.claude.com/docs/en/cloud-environments) — this module IS what runs there.

Targets exactly one host: Ubuntu 24.04, running as root, the documented cloud VM. It is a
cloud PROVISIONING entrypoint, not a portable installer, and must never become the install
path for a developer workstation — that stays scripts/setup.py. See
docs/plans/2026-09-06-a-deterministic-cloud-install-for-the-engine.md
§ "The PEP-668 gate, and how it closed" and
docs/decisions/DR-411-the-pep-668-refusal-does-not-reach-an-ephemeral-container.md for why
this file is the only caller permitted to assert the DR-411 container opt-in.

Four facts (docs/research/spike-verdicts/2026-09-06-cloud-environment-setup-script-installs-the-engine.md)
shape every decision below:
  1. The script runs ONCE and the filesystem is snapshotted. A process does not survive;
     a file does. No warm engine start here (anti-scope) — anything durable is a disk write.
  2. The env-var block is NOT readable from the setup script's own shell
     (anthropics/claude-code#55440, closed as not planned). COORDINATOR_ENGINE_ROOT and
     COORDINATOR_SETTINGS_HOME are set by THIS script, in its own process environment,
     before invoking scripts/setup.py.
  3. Exit zero is mandatory. A non-zero exit here means the cloud session fails to start.
     Every failure is a RECORDED failure that still exits 0 — see `run_step`.
  4. A genuinely closed stdin raises RuntimeError, not EOFError (measured, this repo,
     2026-09-06) — scripts/setup.py's own C2 chunk fixes its two input() sites for this;
     this module never calls input() at all, so it is unaffected but shares the same
     closed-stdin operating assumption (no prompt, ever).

Negative-spec:
  - Not a general installer. Never wire this into a workstation's install-doc surface.
  - Never starts the warm engine. A SessionStart hook is the correct place for that, not here.
  - Never lets a failed step raise past `run_step` / `main`. `main` always calls
    ``sys.exit(0)`` at the end, unconditionally, whatever the recorded verdicts say.
  - Never writes ``PIP_BREAK_SYSTEM_PACKAGES`` (or any pip env var) into the child
    environment for the setup.py subprocess. The DR-411 opt-in is a single argv flag
    (``--i-assert-no-other-consumer``) passed explicitly; nothing rides in via env,
    which `_run_pip` would inherit invisibly.
  - Never declares its own dependency list. scripts/setup.py derives deps from
    pyproject.toml at run time specifically so the set cannot drift — re-declaring
    them here would reintroduce that drift. This module runs no `pip install` itself.
  - The host precondition below is a NEGATIVE check (refuse unless proven safe), never a
    positive "is this a container" detection — DR-411 rejects the latter because a
    misdetect there would wrongly ADMIT a workstation. A negative check can only
    wrongly REFUSE a real container, which is loud and recoverable.

Spec backlink: pln-a-deterministic-cloud-install-076cf1 § C1
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CLONES: dict[str, dict[str, str]] = {
    "coordinator-claude": {
        "url": "https://github.com/dbc-oduffy/coordinator-claude.git",
        "dest": "/root/coordinator-claude",
    },
    "klabauter": {
        "url": "https://github.com/dbc-oduffy/claude-klabauter.git",
        "dest": "/root/klabauter",
    },
}

INSTALL_REPORT_PATH = Path("/root/cloud-setup-report.json")

NETWORK_MAX_ATTEMPTS = 3


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    steps: list[StepResult] = field(default_factory=list)
    engine_root: str | None = None
    settings_home: str | None = None
    container_optin_requested: bool | None = None
    setup_exit_code: int | None = None

    def to_dict(self) -> dict:
        # Review: overengineering-reviewer — dataclasses.asdict already produces
        # this exact structure; the hand-enumerated version was a second edit
        # site that would silently drift out of date on a new field.
        return dataclasses.asdict(self)


def host_precondition_met() -> tuple[bool, str]:
    """Refuse unless the host is Linux AND running as euid 0.

    NEGATIVE check by design (DR-411): this can only wrongly REFUSE a real
    container, never wrongly ADMIT a workstation. `os.geteuid` does not exist
    on Windows — the attribute access is guarded rather than assumed.
    """
    if platform.system() != "Linux":
        return False, f"host precondition failed: platform is {platform.system()!r}, not Linux"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False, "host precondition failed: os.geteuid is unavailable on this platform"
    euid = geteuid()
    if euid != 0:
        return False, f"host precondition failed: euid is {euid}, not 0"
    return True, "host precondition met: Linux, euid 0"


def run_step(name: str, fn, report: Report) -> None:
    """Run one step, recording its verdict, and NEVER propagate a failure.

    Catches `Exception` and `SystemExit` explicitly (recording SystemExit's code),
    lets `KeyboardInterrupt` propagate.

    The `SystemExit` arm is unreachable from today's four steps, which all shell
    out rather than exiting in-process — the reviewer flagged it as dead and
    that reading is correct for the current call sites. It stays anyway, and
    not on general defensiveness: `except Exception` does NOT catch SystemExit
    (BaseException), so the day a step calls `sys.exit` in-process, the escape
    lands as a non-zero exit from this script, which is the one failure this
    module exists to prevent — the cloud session would not start at all. Two
    lines against that is not the same trade as an ordinary dead branch.

    This is how fact 3 (exit zero, or the cloud session fails to start) is
    satisfied without pretending a failed step succeeded.

    # Review: overengineering-reviewer — previously returned a bool no call
    # site read. The reviewer's preferred fix (short-circuit later steps on
    # an earlier failure) was not landed: `main`'s all-steps-failing arm
    # (scripts/test_cloud_setup.py, this plan's prime exit-criterion
    # falsifier) requires every step to still run and be named in the
    # report even when an earlier one fails, so a return value nothing
    # else needs was dropped instead. See dispatch escalation.
    """
    try:
        fn()
    except SystemExit as e:
        report.steps.append(StepResult(name, False, f"SystemExit({e.code})"))
    except Exception as e:  # noqa: BLE001 - deliberate: a step must never propagate
        report.steps.append(StepResult(name, False, f"{type(e).__name__}: {e}"))
    else:
        report.steps.append(StepResult(name, True, "ok"))


def _network_retry(name: str, attempt_fn) -> None:
    """Bounded retry for a network-touching step.

    Max NETWORK_MAX_ATTEMPTS attempts, each attempt's failure cause logged.
    Raises the last exception if every attempt fails, so the caller's
    `run_step` records it — no silent skip.

    # Review: overengineering-reviewer — dropped the separate total-time
    # budget axis. Whichever axis tripped, the outcome was identical
    # (recorded failure, exit 0), and git clone's own `timeout=60` already
    # bounds wall clock; a second, independently-configured bound was
    # specified but not justified.
    """
    last_exc: Exception | None = None
    for attempt in range(1, NETWORK_MAX_ATTEMPTS + 1):
        try:
            attempt_fn()
            return
        except Exception as e:  # noqa: BLE001 - retried explicitly, cause logged
            last_exc = e
            print(f"[cloud_setup] {name}: attempt {attempt}/{NETWORK_MAX_ATTEMPTS} failed: {e}")
    raise RuntimeError(f"{name}: all {NETWORK_MAX_ATTEMPTS} attempts failed; last cause: {last_exc}")


def _git_clone(url: str, dest: str) -> None:
    dest_path = Path(dest)
    if dest_path.exists():
        return
    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, dest],
        capture_output=True,
        text=True,
        timeout=60,
        # Review: code-reviewer (2026-09-06) -- stdin explicitly closed rather
        # than inherited: an ambient closed fd 0 would otherwise let git (or a
        # credential helper it spawns) be handed an unrelated fd as "stdin".
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone {url} -> {dest} failed: {result.stderr.strip()}")


def clone_repo(name: str) -> None:
    spec = CLONES[name]
    _network_retry(f"clone {name}", lambda: _git_clone(spec["url"], spec["dest"]))


def run_coordinator_install_trampoline() -> None:
    """Run coordinator-claude's install orchestrator to deposit the doctrine-side substrate.

    Negative-spec, because the obvious readings of coordinator-claude's INSTALL.md
    are all wrong here and the next reader will re-derive this otherwise:

    - It is NOT `install.sh`. No such file exists in that repo at any path.
    - It is NOT `/coordinator:install`, the documented entry. That is agent-executed
      prose in a slash command, so it needs a running Claude Code session — the one
      thing that by construction does not exist yet at setup-script time. This whole
      module exists because Claude Code's LAUNCH is the restart that chain calls for.
    - It is NOT `bin/coordinator-install` under the settings home. That trampoline is
      GENERATED by an install that has not happened, so in a fresh VM it is absent.

    What is left is `coordinator_core/install/maximalist.py`, which INSTALL.md marks
    "do NOT invoke directly". Read its stated reason: the module "requires
    CLAUDE_PLUGIN_ROOT and REPO_DOE_CLAUDE to be exported by the trampoline and
    refuses outright without them", and needs an already-resolvable engine root. Those
    are the trampoline's whole job, and this process knows all three values first-hand
    — it chose both clone destinations itself. So it exports them and calls the module,
    which is what the trampoline would have done.

    That reading is ours, not coordinator-claude's, and it is the one thing in this
    module resting on another repo's undocumented surface. A memo is out asking them to
    confirm or name the correct pre-launch entry; if they name a different one, this
    function is the only thing that changes.
    """
    coord_root = Path(CLONES["coordinator-claude"]["dest"])
    engine_root = Path(CLONES["klabauter"]["dest"])
    orchestrator = engine_root / "coordinator_core" / "install" / "maximalist.py"
    if not orchestrator.exists():
        raise FileNotFoundError(
            f"install orchestrator not found at {orchestrator} — the klabauter clone "
            "is absent or its layout changed; see this function's docstring for the "
            "entry points that were ruled out and why"
        )

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = str(coord_root)
    env["REPO_DOE_CLAUDE"] = str(coord_root)
    env["COORDINATOR_ENGINE_ROOT"] = str(engine_root)

    result = subprocess.run(
        # Review: code-reviewer (2026-09-06) — pass --non-interactive explicitly
        # rather than relying solely on maximalist.py's own isatty() fallback.
        # The cloud host's non-tty stdin makes that fallback safe today, but it
        # is a guard this caller does not control; asserting it directly means a
        # future prompt gated only on the flag (not also on isatty) still fails
        # fast instead of hanging into run_step's 180s subprocess timeout.
        ["python3", str(orchestrator), "--non-interactive"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        # Review: code-reviewer (2026-09-06) -- stdin explicitly closed, not
        # inherited; see the matching comment on _git_clone's subprocess.run.
        stdin=subprocess.DEVNULL,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        raise RuntimeError(
            f"install orchestrator exited {result.returncode}: {result.stderr.strip()}"
        )


def set_engine_env(report: Report) -> None:
    """Set COORDINATOR_ENGINE_ROOT and COORDINATOR_SETTINGS_HOME in this process' own
    environment — the env-var block is not readable from the setup script's shell
    (fact 2), so this process must set them itself before invoking setup.py.
    """
    engine_root = CLONES["klabauter"]["dest"]
    settings_home = str(Path(CLONES["coordinator-claude"]["dest"]) / ".coordinator-claude-settings")
    os.environ["COORDINATOR_ENGINE_ROOT"] = engine_root
    os.environ["COORDINATOR_SETTINGS_HOME"] = settings_home
    report.engine_root = engine_root
    report.settings_home = settings_home


def run_claude_klabauter_setup(report: Report) -> None:
    """Run scripts/setup.py, passing the DR-411 container opt-in.

    Argv: --i-am-agent (suppress prompts), --coordinator-root <CLONES coordinator-claude
    dest>, --i-assert-no-other-consumer (the DR-411 opt-in; cloud_setup.py is the only
    caller permitted to pass it). No PIP_BREAK_SYSTEM_PACKAGES or any pip env var is set —
    the flag rides in argv only.
    """
    claude_klabauter_root = Path(CLONES["klabauter"]["dest"])
    setup_py = claude_klabauter_root / "scripts" / "setup.py"
    coordinator_root = CLONES["coordinator-claude"]["dest"]
    argv = [
        "python3",
        str(setup_py),
        "--i-am-agent",
        "--coordinator-root",
        coordinator_root,
        "--i-assert-no-other-consumer",
    ]
    # Review: code-reviewer (2026-09-06) — the prior field name/value
    # ("flag passed in setup.py argv", called "recorded") implied an outcome
    # this process cannot know: it knows it PASSED --i-assert-no-other-consumer
    # (first-hand, true regardless of what setup.py does with it); it does NOT
    # know whether setup.py's own host-precondition gate actually honoured the
    # opt-in (Linux + euid 0 + a guarded candidate found). Two first-hand facts
    # are recorded instead of one conflated one:
    #   - container_optin_requested: this process asserted the flag in argv.
    #   - setup_exit_code: setup.py's real exit code, captured on every path
    #     (not just failure) so a later reader can see exit 96 (PEP-668
    #     refusal — DR-411's precise "was the carve-out honoured?" case) versus
    #     exit 0.
    # This still does not fully discharge DR-411 § "Recorded in the install
    # report": exit 0 alone cannot distinguish "opt-in honoured, guarded
    # candidate found" from "ordinary install, opt-in never exercised because
    # nothing was guarded" — setup.py does not surface that distinction on any
    # channel this process can observe without re-scanning stdout for a
    # marker string, which the prior review rejected. Named here rather than
    # papered over: the report can prove REFUSAL was avoided, not that the
    # carve-out was exercised.
    report.container_optin_requested = True
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=300,
        # stdin is CLOSED BY CONSTRUCTION, not assumed. setup.py's
        # `_offer_homebrew_removal` docstring is explicit that the offer still
        # fires under --i-am-agent and declines only via "--i-am-agent's
        # typically-closed stdin". Typically is not a guarantee: inherit a tty
        # here and `input()` blocks forever, inside a setup script with a
        # ~5-minute budget and no console anyone can answer from. DEVNULL
        # raises EOFError, the closed case raises RuntimeError, and setup.py
        # now catches both — so the decline is constructed rather than lucky.
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Review: code-reviewer (2026-09-06) -- printed on both success and
    # failure, consistent with run_coordinator_install_trampoline: this is a
    # one-shot snapshotted VM with the JSON report as the only other durable
    # artifact, so a successful run's setup.py progress output (interpreter
    # chosen, dep path taken, DR-411 opt-in honoured or not) would otherwise
    # be visible only when the run fails.
    print(result.stdout, end="")
    report.setup_exit_code = result.returncode
    if result.returncode != 0:
        raise RuntimeError(f"scripts/setup.py exited {result.returncode}: {result.stderr.strip()}")


def write_report(report: Report) -> None:
    INSTALL_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_REPORT_PATH.write_text(json.dumps(report.to_dict(), indent=2))


def _safe_print(text: str) -> None:
    """Print text that may contain content this process did not choose, without
    ever raising past this call.

    # Review: code-reviewer (2026-09-06) -- called from `_print_summary`, which
    # runs in `main` outside any `run_step` net. `step.detail` is built from raw
    # subprocess stderr / exception text (git, pip, the install orchestrator),
    # so a minimal-locale host (LANG=C, no UTF-8) can hand this a non-ASCII byte
    # `print()` cannot encode. An encoding-safe write makes that failure
    # impossible rather than caught: a try/except around the call would still
    # lose the whole summary (and the install report, written after it) the
    # moment one byte is odd, where sanitizing keeps the summary visible.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_text)


def _print_summary(report: Report) -> None:
    _safe_print("[cloud_setup] summary:")
    for step in report.steps:
        verdict = "OK" if step.ok else "FAILED"
        _safe_print(f"  - {step.name}: {verdict} ({step.detail})")


def _write_report_best_effort(report: Report) -> None:
    """Write the install report, swallowing any failure.

    Both exits from `main` -- precondition refusal and normal completion --
    route through here. Neither may let a report-write failure break the
    exit-zero contract the cloud setup script is held to.
    """
    try:
        write_report(report)
    except Exception as e:  # noqa: BLE001 - even the report write must not raise
        print(f"[cloud_setup] could not write install report: {e}")


def main() -> int:
    ok, reason = host_precondition_met()
    if not ok:
        print(f"[cloud_setup] refusing: {reason}")
        report = Report()
        report.steps.append(StepResult("host precondition", False, reason))
        _write_report_best_effort(report)
        return 0
    print(f"[cloud_setup] {reason}")

    report = Report()

    run_step("clone coordinator-claude", lambda: clone_repo("coordinator-claude"), report)
    run_step("clone klabauter", lambda: clone_repo("klabauter"), report)
    run_step("set engine env", lambda: set_engine_env(report), report)
    run_step(
        "coordinator-claude install orchestrator",
        run_coordinator_install_trampoline,
        report,
    )
    run_step("run scripts/setup.py", lambda: run_claude_klabauter_setup(report), report)

    _print_summary(report)

    _write_report_best_effort(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
