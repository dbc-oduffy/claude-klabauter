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
  - Never fetches the engine corpus. It is the lazy tier — see `Report.corpus`
    and the § "The example-retrieval-repo half" banner below.

Two halves, one script. The steps above `main`'s doctrine rows land a working COORDINATOR
and no retrieval; the rag rows after them land the retrieval surfaces a session cannot
create for itself (MCP registration, LSP, a daemon). The ordering between the halves is a
hard constraint with its reason stated at the § "The example-retrieval-repo half" banner — read it
before moving a row.

Contract for the rag half (ratified — conform, do not re-derive): DoE-claude
coordinator/docs/wiki/cloud-preboot-install-contract.md.

Spec backlink: pln-a-deterministic-cloud-install-076cf1 § C1;
DoE-claude docs/plans/2026-09-09-cloud-environment-install-mode-pre-boot.md § C8.
"""

from __future__ import annotations

import dataclasses
import json
import os
import platform
import subprocess
import sys
import time
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
    # The example-retrieval-repo half. Same shape as the two above, so `clone_repo` and the
    # report read identically for all four; the rag pair additionally accepts an
    # already-present checkout (see `locate_or_clone_repo`).
    "example-retrieval-repo": {
        "url": "https://github.com/dbc-oduffy/example-retrieval-repo.git",
        "dest": "/root/example-retrieval-repo",
    },
    "example-retrieval-repo-ue-addon": {
        "url": "https://github.com/dbc-oduffy/example-retrieval-repo-ue-addon.git",
        "dest": "/root/example-retrieval-repo-ue-addon",
    },
}

#: Machine-local registry key for each rag-half clone. The key spellings are the
#: fleet's, not this file's -- `machine-local keys | grep '^repos\.'` is the
#: authority and a wrong guess returns a bare not-found that reads as "no such
#: repo". Registering them is what lets a session (or the hydration verb) find
#: either checkout without a literal path.
MACHINE_LOCAL_REPO_KEYS: dict[str, str] = {
    "example-retrieval-repo": "repos.example_retrieval_repo",
    "example-retrieval-repo-ue-addon": "repos.example_retrieval_repo_ue_addon",
}

#: Where a claude.ai cloud environment mounts the repositories it checks out.
#: Same mount `_find_doctrine_source` globs; named once here so the rag half's
#: "locate before clone" rung and the project-root resolution agree.
WORKSPACE_MOUNT = Path("/workspace")

INSTALL_REPORT_PATH = Path("/root/cloud-setup-report.json")

NETWORK_MAX_ATTEMPTS = 3

#: Ceiling for example-retrieval-repo's installer subprocess. Sized to sit just ABOVE that
#: installer's own 900s pip ceiling (`_CLOUD_PIP_TIMEOUT_S`), so a wedged pip is
#: reported by the installer -- which names the package and the refresh rule --
#: rather than truncated into a bare "timed out" by this caller.
#: A CEILING IS NOT THE BUDGET. The pre-boot phase's budget is measured, not
#: bounded: every step's elapsed time is recorded (see `run_step`) and read back
#: from the setup log and the install report.
RAG_INSTALL_TIMEOUT_S = 960

#: Ceiling for one `machine-local set`. A registry write is a single small file
#: rewrite; anything near this is a wedged interpreter probe, not slow I/O.
MACHINE_LOCAL_TIMEOUT_S = 60


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    #: Wall-clock seconds this step occupied, recorded for EVERY step whatever
    #: its verdict. The pre-boot phase is held to a time bar, and a bar nobody
    #: measures is a memory: this field plus `_print_summary`'s rendering of it
    #: is what puts the per-step budget in the setup log, where a later
    #: verification reads it instead of re-timing the run by hand.
    elapsed_s: float | None = None


@dataclass
class Report:
    steps: list[StepResult] = field(default_factory=list)
    engine_root: str | None = None
    settings_home: str | None = None
    container_optin_requested: bool | None = None
    setup_exit_code: int | None = None
    plugin_settings: dict | None = None
    doctrine_candidates_tried: list[str] = field(default_factory=list)
    global_doctrine: dict | None = None
    #: Sum of every recorded step's elapsed time — the pre-boot phase's own cost,
    #: written to the report so it is read off an artifact, not a stopwatch.
    total_elapsed_s: float | None = None
    #: The example-retrieval-repo half. Resolved checkout path per repo name (None until
    #: located or cloned), the registry keys written, the installer's verdict,
    #: the MCP registration as read back off disk, and the corpus declaration.
    rag_roots: dict = field(default_factory=dict)
    #: Names of the /workspace checkouts when more than one was mounted and the
    #: project root could not be chosen without guessing. Empty is the ordinary
    #: case; a populated list means the fallback root was used deliberately.
    rag_project_root_ambiguity: list = field(default_factory=list)
    machine_local_keys: dict = field(default_factory=dict)
    rag_install: dict | None = None
    mcp_registration: dict | None = None
    #: The engine corpus is never fetched here: it is the lazy tier, and nothing
    #: about launching a session needs it. Declared as a constant on the report
    #: rather than produced by a pipeline step — a step that assigns a literal
    #: buys timing, a failure envelope and a step-list row for no work.
    #: Review: coordinator:overengineering-reviewer (F7).
    corpus: dict = field(
        default_factory=lambda: {
            "hydrated": False,
            "reason": (
                "the engine corpus is the lazy tier: nothing about the session's launch "
                "needs it, and the session can obtain it afterwards"
            ),
            "remedy": (
                "hydrate at first use; the semantic tools report a typed not-hydrated "
                "verdict until then"
            ),
            "fetched_by_this_script": False,
        }
    )

    def to_dict(self) -> dict:
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

    Returns nothing: every step must still run and be named in the report
    even after an earlier one fails (the all-steps-failing case
    `scripts/tests/test_cloud_setup.py` pins), so short-circuiting later
    steps on an earlier failure is not this function's job.

    Every verdict carries the step's elapsed time, measured here rather than at
    each call site so no step can be added without one. A FAILED step is timed
    too: a step that blew a subprocess ceiling is exactly the one whose duration
    a later reader needs.
    """
    started = time.monotonic()
    try:
        fn()
    except SystemExit as e:
        elapsed = time.monotonic() - started
        report.steps.append(StepResult(name, False, f"SystemExit({e.code})", elapsed))
    except Exception as e:  # noqa: BLE001 - deliberate: a step must never propagate
        elapsed = time.monotonic() - started
        report.steps.append(StepResult(name, False, f"{type(e).__name__}: {e}", elapsed))
    else:
        elapsed = time.monotonic() - started
        report.steps.append(StepResult(name, True, "ok", elapsed))


def _network_retry(name: str, attempt_fn) -> None:
    """Bounded retry for a network-touching step.

    Max NETWORK_MAX_ATTEMPTS attempts, each attempt's failure cause logged.
    Raises the last exception if every attempt fails, so the caller's
    `run_step` records it — no silent skip.

    No separate total-time budget: git clone's own `timeout=60` already
    bounds wall clock, and a second, independently-configured bound would
    trip to the same outcome (recorded failure, exit 0) as the attempt count.
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
        # stdin explicitly closed rather than inherited: an ambient closed fd 0
        # would otherwise let git (or a credential helper it spawns) be handed
        # an unrelated fd as "stdin".
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
        # --non-interactive passed explicitly rather than relying solely on
        # maximalist.py's own isatty() fallback: that guard is not this
        # caller's to control, and asserting the flag directly means a future
        # prompt gated only on it (not also on isatty) still fails fast
        # instead of hanging into run_step's 180s subprocess timeout.
        ["python3", str(orchestrator), "--non-interactive"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        # stdin explicitly closed, not inherited; see the matching comment on
        # _git_clone's subprocess.run.
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
    # `container_optin_requested` and `setup_exit_code` are two separate
    # first-hand facts, never conflated into one: this process knows it PASSED
    # --i-assert-no-other-consumer regardless of what setup.py does with it,
    # but does NOT know whether setup.py's own host-precondition gate actually
    # honoured the opt-in. `setup_exit_code` is captured on every path (not
    # just failure) so a later reader can see exit 96 (PEP-668 refusal —
    # DR-411's precise "was the carve-out honoured?" case) versus exit 0. Even
    # so, exit 0 alone cannot distinguish "opt-in honoured, guarded candidate
    # found" from "ordinary install, opt-in never exercised" -- setup.py
    # surfaces no distinct channel for that without scanning stdout for a
    # marker string. The report can prove REFUSAL was avoided, not that the
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
    # Printed on both success and failure, consistent with
    # run_coordinator_install_trampoline: this is a one-shot snapshotted VM
    # with the JSON report as the only other durable artifact, so a
    # successful run's setup.py progress output (interpreter chosen, dep path
    # taken, DR-411 opt-in honoured or not) would otherwise be visible only
    # when the run fails.
    print(result.stdout, end="")
    report.setup_exit_code = result.returncode
    if result.returncode != 0:
        raise RuntimeError(f"scripts/setup.py exited {result.returncode}: {result.stderr.strip()}")


def _claude_home() -> Path:
    """Resolve the VM's own `.claude` directory: `CLAUDE_HOME` first, then
    `HOME`, then the platform home directory (`expanduser("~")`).

    This is NOT the same resolution order as
    `coordinator_core/hooks/platform_localize.py :: resolve_registry_paths`,
    which never reads `HOME` directly (it goes straight from `CLAUDE_HOME` to
    `expanduser("~")`, which only consults `HOME` internally on POSIX, not on
    Windows). The explicit `HOME` step here is a deliberate, real behavioural
    difference, kept because this module targets a cloud VM where `HOME` is
    set by the environment before this script runs (fact 2's env-var block
    is this process' own, not the shell's) -- naming `HOME` explicitly makes
    that resolution readable without knowing `expanduser`'s
    platform-conditional internals.

    `CLAUDE_HOME` names the PARENT of `.claude` and this function appends that
    segment itself, matching `platform_localize`'s contract. A `CLAUDE_HOME`
    that already ends in `.claude` is therefore a misconfiguration, and is
    refused rather than resolved: silently writing to `<...>/.claude/.claude/`
    would put the plugin registration and the doctrine somewhere no session
    reads, while every step still reported OK.

    This refusal is a second spelling of a rule whose canonical home is
    `coordinator_core/hooks/platform_localize.py :: reject_doubled_claude_home`
    (four importers on current HEAD). It is re-implemented here, not imported,
    because this script is dependency-free by design (see module docstring):
    it runs on a bare cloud VM BEFORE `coordinator_core` is cloned or
    installed, so importing from it is not available at the point this
    function runs. If the shared rule's shape or message changes, check here
    too.

    # Review: coordinator:code-reviewer Finding 1 -- the refusal only has
    # standing to judge CLAUDE_HOME. HOME and expanduser("~") are values this
    # process's operator never set and never chose to misconfigure; a VM whose
    # real home legitimately ends in .claude must resolve, not be refused with
    # a message blaming a variable that may never have been set. Only the
    # CLAUDE_HOME source is checked, and only CLAUDE_HOME's own value is
    # compared -- so the raised message can only ever be true.
    """
    claude_home_env = os.environ.get("CLAUDE_HOME")
    if claude_home_env:
        # Compared case-insensitively: a `.Claude` spelling must not slip past
        # this on a case-insensitive filesystem. An EMPTY CLAUDE_HOME is unset,
        # not a misconfiguration -- falling through to HOME is what the earlier
        # `or` chain did, and treating "" as a chosen value would resolve the
        # whole install to a relative `.claude` directory.
        if Path(claude_home_env).name.lower() == ".claude":
            raise ValueError(
                f"CLAUDE_HOME names the parent of .claude, but is {claude_home_env!r} -- "
                "point it at the home directory, not at .claude itself"
            )
        base = claude_home_env
    else:
        base = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(base) / ".claude"


def register_plugin_settings() -> None:
    """Write `$CLAUDE_HOME/settings.json` (or `$HOME/.claude/settings.json`)
    so the coordinator plugin is registered before Claude Code launches in
    this cloud VM.

    Modelled on DoE-claude's `coordinator/templates/cloud-env/setup.sh`
    phase 3 (lines 125-161): a `directory` marketplace source pointing at the
    already-cloned `coordinator-claude` checkout, plus `enabledPlugins`, plus
    `env.COORDINATOR_PROBE_CANARY`.

    THE CANARY IS NOT OPTIONAL AND IS NOT A DoE-SIDE CONCERN. Without it,
    EVERY Bash call in the cloud session is denied for that session's whole
    life. `coordinator_core/warm/hook_http.py :: OVERRIDE_CANARY_ENV` sends
    `${COORDINATOR_PROBE_CANARY}` interpolated into the
    `X-Coordinator-Env-Canary` header, and its own docstring calls it "a var
    the launcher always exports non-empty" -- an empty canary beside a
    declared channel is read as a setting-level VETO and disarms the channel.
    A cloud session has no launcher, so nothing exports it and the veto fires
    on a session that never vetoed anything. Seeding it here is the recovery
    the forwarder's own deny text prescribes, applied at provision time so no
    session has to.

    Merge, never clobber: an existing `settings.json` is read and patched.
    Unparseable existing JSON is a recorded step FAILURE (raised so `run_step`
    catches it), never a reason to overwrite the file.
    """
    claude_home = _claude_home()
    claude_home.mkdir(parents=True, exist_ok=True)
    settings_path = claude_home / "settings.json"

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception as e:
            raise ValueError(f"existing settings.json is not valid JSON: {e}") from e
    else:
        settings = {}

    marketplace_path = CLONES["coordinator-claude"]["dest"]
    settings.setdefault("extraKnownMarketplaces", {})["coordinator-claude"] = {
        "source": {"source": "directory", "path": marketplace_path}
    }
    settings.setdefault("enabledPlugins", {})["coordinator@coordinator-claude"] = True
    settings.setdefault("env", {})["COORDINATOR_PROBE_CANARY"] = "1"

    tmp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2))
    tmp_path.replace(settings_path)


def verify_plugin_settings(report: Report) -> None:
    """Read `settings.json` back off disk and record onto `report.plugin_settings`
    what a session launched in this VM will actually see -- never the values
    `register_plugin_settings` just wrote in memory, which is the failure mode
    this half exists to prevent (a write that silently no-ops would otherwise
    still report success).
    """
    settings_path = _claude_home() / "settings.json"
    result = {
        "settings_path": str(settings_path),
        "marketplace_registered": False,
        "marketplace_path": None,
        "plugin_enabled": False,
        "probe_canary_seeded": False,
    }
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        report.plugin_settings = result
        return

    marketplace = settings.get("extraKnownMarketplaces", {}).get("coordinator-claude", {})
    marketplace_path = marketplace.get("source", {}).get("path")
    result["marketplace_registered"] = marketplace_path is not None
    result["marketplace_path"] = marketplace_path
    result["plugin_enabled"] = bool(settings.get("enabledPlugins", {}).get("coordinator@coordinator-claude"))
    result["probe_canary_seeded"] = bool(settings.get("env", {}).get("COORDINATOR_PROBE_CANARY"))
    report.plugin_settings = result


def _find_doctrine_source() -> tuple[Path | None, list[str]]:
    """Search order for the global-doctrine source, first hit wins.

    A candidate is accepted only if it contains a readable `CLAUDE.md`. Returns
    the accepted candidate (or None) plus the full list of candidates tried, in
    order, so a miss can be reported with what was searched.

    Three candidates, each a source that actually ships doctrine: the cwd copy
    (a self-hosted runner checked out with one alongside it), any
    `/workspace/*/global-doctrine` (the claude.ai cloud-environment mount), and
    the coordinator-claude clone's `templates/global-doctrine` (published to
    `dbc-oduffy/coordinator-claude`, verified end-to-end 2026-09-07). Re-add a
    candidate when a new source starts shipping there, not in anticipation.
    """
    candidates: list[Path] = [Path.cwd() / "global-doctrine"]
    candidates.extend(sorted(Path("/workspace").glob("*/global-doctrine")))
    coordinator_dest = Path(CLONES["coordinator-claude"]["dest"])
    candidates.append(coordinator_dest / "templates" / "global-doctrine")

    tried = [str(c) for c in candidates]
    for cand in candidates:
        candidate_doctrine = cand / "CLAUDE.md"
        # Review: coordinator:code-reviewer Finding 2 -- is_file() does not
        # test readability (it only needs traversal permission on parent
        # dirs, not read permission on the leaf), so an unreadable CLAUDE.md
        # would previously win the search and a later, good candidate would
        # never be tried. os.access is skipped deliberately -- it lies under
        # some containers and on Windows -- in favour of actually attempting
        # the read and treating any failure as "this candidate is not it".
        try:
            candidate_doctrine.read_bytes()
        except OSError:
            continue
        return cand, tried
    return None, tried


def install_global_doctrine(report: Report) -> None:
    """Land the fleet's global doctrine into this VM's own `.claude` directory.

    Modelled on `coordinator/templates/cloud-env/setup.sh` phase 3b. Copies
    `CLAUDE.md` and every `rules/*.md` from the first accepted source
    candidate (see `_find_doctrine_source`) into `_claude_home()`.

    Copy, never mirror-and-prune: nothing already present under
    `<claude_home>` or its `rules/` is ever deleted or cleared — a
    self-hosted runner's `$HOME/.claude` may carry seeded content this must
    not eat. `CLAUDE.md` itself IS overwritten unconditionally, and that is
    correct, not a bug to "fix" into write-if-absent: per
    `coordinator/bin/check-global-doctrine-mirror.py`, the tracked
    `global-doctrine/` copy is the AUTHORITATIVE source and the `.claude`
    copy is the derived, live one.

    No source found is a RECORDED MISS, not an exception and not a silent
    skip: this function never raises for that case (a doctrine-blind VM is a
    real posture problem, but not one that should abort the whole cloud
    session), and the candidates tried are stashed onto `report` so
    `verify_global_doctrine` — and a human reading the report — can see
    exactly what was searched.
    """
    source, tried = _find_doctrine_source()
    if source is None:
        report.doctrine_candidates_tried = tried
        report.global_doctrine = {"source": None}
        _safe_print(
            "[cloud_setup] doctrine: FAIL (no copy found in any of "
            f"{tried} -- session runs doctrine-blind)"
        )
        return

    report.global_doctrine = {"source": str(source)}
    claude_home = _claude_home()
    claude_home.mkdir(parents=True, exist_ok=True)
    (claude_home / "CLAUDE.md").write_bytes((source / "CLAUDE.md").read_bytes())

    rules_src = source / "rules"
    if rules_src.is_dir():
        rules_dest = claude_home / "rules"
        rules_dest.mkdir(parents=True, exist_ok=True)
        rules_files = sorted(rules_src.glob("*.md"))
        # Review: coordinator:code-reviewer Finding 3 -- no per-file
        # isolation (deliberate: no rollback on a partial copy, see the
        # docstring above), but a mid-loop failure previously left the
        # step's detail as raw exception text with no way to tell "0 of N
        # landed" from "N-1 of N landed". copied_count is tracked here and
        # folded into the re-raised message so a partial rules set is
        # legible in the report without cross-referencing
        # verify_global_doctrine's disk read-back.
        copied_count = 0
        try:
            for md_file in rules_files:
                (rules_dest / md_file.name).write_bytes(md_file.read_bytes())
                copied_count += 1
        except Exception as e:
            raise RuntimeError(
                f"doctrine rules copy failed after {copied_count} of {len(rules_files)} "
                f"files (from {rules_src}): {type(e).__name__}: {e}"
            ) from e
    _safe_print(f"[cloud_setup] doctrine: OK -> {claude_home / 'CLAUDE.md'} (from {source})")


def verify_global_doctrine(report: Report) -> None:
    """Read back off disk whether global doctrine landed -- never asserts what
    `install_global_doctrine` just wrote in memory, only what is actually on
    disk now. Adds its disk-read facts onto the same `report.global_doctrine`
    dict `install_global_doctrine` started (which already holds `source`);
    it does not re-derive `source` itself.
    """
    claude_home = _claude_home()
    claude_md = claude_home / "CLAUDE.md"
    rules_dir = claude_home / "rules"
    exists = claude_md.is_file()
    size = claude_md.stat().st_size if exists else 0
    rules_count = len(list(rules_dir.glob("*.md"))) if rules_dir.is_dir() else 0
    if report.global_doctrine is None:
        report.global_doctrine = {"source": None}
    report.global_doctrine.update(
        {
            "claude_md_present": exists,
            "claude_md_size_bytes": size,
            "rules_file_count": rules_count,
        }
    )


# ---------------------------------------------------------------------------
# The example-retrieval-repo half.
#
# Contract (ratified — conform to it, do not re-derive): DoE-claude
# coordinator/docs/wiki/cloud-preboot-install-contract.md § "The example-retrieval-repo half".
# Spec backlink: DoE-claude
# docs/plans/2026-09-09-cloud-environment-install-mode-pre-boot.md § C8.
#
# ORDERING IS LOAD-BEARING, and it is the whole reason these functions sit at the
# END of `main`'s step list rather than beside the clones they resemble.
# example-retrieval-repo's installer seeds a concern into the machine-local registry, which
# the coordinator install trampoline and scripts/setup.py are what create. Run
# these earlier and the installer refuses — and the refusal reads as a
# example-retrieval-repo defect rather than an ordering one, which is how the hour is lost.
#
# Negative-spec for this half:
#   - NO dependency list is declared here. Example-retrieval-repo's installer derives the
#     pinned pre-boot set from its own tracked list at run time; a second copy
#     living here would be the copy nobody updates and the one that is stale.
#   - NO corpus is fetched. The engine corpus is the lazy tier whatever the
#     transfer rate makes it look like it could afford — see
#     `Report.corpus`.
#   - NO cloud DETECTION is performed here. The mode is the installer's own
#     decision from the documented harness signal (`resolve_cloud_mode`); this
#     caller passes `--cloud` because it KNOWS first-hand it is the cloud
#     provisioning entrypoint, which is an assertion, not an inference.
#   - The daemon port is never a literal in this file. It is read from the
#     example-retrieval-repo checkout's own `http_config.py`, the truth source the contract
#     names.
# ---------------------------------------------------------------------------


def locate_existing_checkout(name: str) -> Path | None:
    """First already-present checkout of *name*, or None.

    Two rungs, in order: the clone destination this module chose, then
    ``/workspace/<name>`` — the mount a claude.ai cloud environment uses for the
    repositories it checks out itself (the same mount `_find_doctrine_source`
    globs). An environment configured to check out example-retrieval-repo already has it on
    disk, and cloning a second copy would leave two trees where the registry key
    can only name one.

    A candidate must be a DIRECTORY containing a `.git` entry: a bare directory
    of the right name is not a checkout, and accepting one would register a
    registry key pointing at nothing.
    """
    candidates = [Path(CLONES[name]["dest"]), WORKSPACE_MOUNT / name]
    for cand in candidates:
        if cand.is_dir() and (cand / ".git").exists():
            return cand
    return None


def locate_or_clone_repo(name: str, report: Report) -> None:
    """Resolve a rag-half checkout — locate first, clone only if absent.

    Records the resolved path onto ``report.rag_roots[name]`` so every later step
    reads ONE resolved value rather than re-deriving it (and possibly resolving
    differently once the clone exists).
    """
    found = locate_existing_checkout(name)
    if found is None:
        clone_repo(name)
        found = locate_existing_checkout(name)
    if found is None:
        report.rag_roots[name] = None
        raise RuntimeError(
            f"{name}: no checkout at {CLONES[name]['dest']} or {WORKSPACE_MOUNT / name} "
            "after the clone step — nothing later in this half can resolve it"
        )
    report.rag_roots[name] = str(found)
    print(f"[cloud_setup] {name}: {found}")


def _resolved_root(name: str, report: Report) -> Path:
    """The path `locate_or_clone_repo` recorded, or a raise naming the missing step."""
    resolved = report.rag_roots.get(name)
    if not resolved:
        raise RuntimeError(
            f"{name} was never resolved (its locate-or-clone step failed) — "
            "this step depends on it and reports rather than guessing a path"
        )
    return Path(resolved)


def _machine_local_argv() -> list[str]:
    """Argv for one `machine-local` invocation, resolved from the coordinator clone.

    Two rungs, mirroring `coordinator_core.install._shared.resolve_machine_local_cli`
    (rungs 1 and 2 of three; the PATH rung is deliberately absent — nothing has
    put this CLI on the setup script's PATH, and a `machine-local` found there
    would belong to some other install):

      1. ``<coordinator clone>/templates/bin/_machine_local.py`` under this
         interpreter — the implementation, and the only rung that cannot be
         defeated by a missing executable bit or a Windows extension rule.
      2. ``<settings home>/bin/machine-local`` — the installed forwarder, which
         exists only once the trampoline has run.

    Re-implemented rather than imported for the same reason `_claude_home`
    re-implements its refusal: this module is dependency-free by design and
    `coordinator_core` is not importable from the ambient interpreter.
    """
    impl = Path(CLONES["coordinator-claude"]["dest"]) / "templates" / "bin" / "_machine_local.py"
    if impl.is_file():
        return [sys.executable, str(impl)]
    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if settings_home:
        forwarder = Path(settings_home) / "bin" / "machine-local"
        if forwarder.is_file():
            return [str(forwarder)]
    raise FileNotFoundError(
        f"no machine-local CLI: neither {impl} nor a forwarder under "
        f"COORDINATOR_SETTINGS_HOME={settings_home!r} — the coordinator install "
        "trampoline has not run, or this step is running before it"
    )


def register_machine_local_repo_keys(report: Report) -> None:
    """Write ``repos.example_retrieval_repo`` and ``repos.example_retrieval_repo_ue_addon`` into the
    machine-local registry, so a session resolves either checkout by key rather
    than by a literal path this script happened to choose.

    Both keys in one step: they are one fact about this box (where the rag half
    lives), and splitting them would let the report show half a registry as a
    clean pass. Each key's verdict is recorded individually inside
    ``report.machine_local_keys``, so a partial write is still legible.

    The registry lives under COORDINATOR_SETTINGS_HOME, which `set_engine_env`
    put in this process' environment — the child inherits it.
    """
    # Seed both verdicts BEFORE the resolver can raise. _machine_local_argv
    # raises when neither rung is available, and the keys were left as {} —
    # indistinguishable in the report from a step that never ran, against a
    # docstring promising each key's verdict individually.
    # Review: coordinator:code-reviewer.
    for _key in ("repos.example_retrieval_repo", "repos.example_retrieval_repo_ue_addon"):
        report.machine_local_keys.setdefault(_key, "skipped: no machine-local CLI resolved")
    argv = _machine_local_argv()
    failures: list[str] = []
    for name, key in MACHINE_LOCAL_REPO_KEYS.items():
        root = report.rag_roots.get(name)
        if not root:
            report.machine_local_keys[key] = "skipped: no resolved checkout"
            failures.append(f"{key} (no resolved checkout)")
            continue
        result = subprocess.run(
            [*argv, "set", key, root],
            capture_output=True,
            text=True,
            timeout=MACHINE_LOCAL_TIMEOUT_S,
            # stdin explicitly closed, not inherited; see _git_clone's comment.
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            report.machine_local_keys[key] = root
        else:
            report.machine_local_keys[key] = f"failed (exit {result.returncode}): {result.stderr.strip()}"
            failures.append(f"{key} (exit {result.returncode})")
    if failures:
        raise RuntimeError("machine-local registry writes failed: " + ", ".join(failures))


def _resolve_rag_project_root(report: Report) -> str:
    """Which project the pre-boot daemon is installed against.

    The single `/workspace/*` checkout when there is exactly one, else the
    example-retrieval-repo clone itself. The
    workspace rung is the session's own repository — the thing an operator will
    ask questions about. The fallback is not arbitrary: example-retrieval-repo-on-example-retrieval-repo
    is that repo's own canonical Python case, so a box with no workspace mount
    still gets a project root its owner recognises rather than a blank one.

    Recorded onto the report either way, because "which project" is not
    inferable afterwards from the MCP entry — the HTTP registration carries a URL
    and nothing else.
    """
    checkouts = []
    if WORKSPACE_MOUNT.is_dir():
        checkouts = [
            cand for cand in sorted(WORKSPACE_MOUNT.iterdir())
            if cand.is_dir() and (cand / ".git").exists()
        ]
    if len(checkouts) == 1:
        return str(checkouts[0])
    if len(checkouts) > 1:
        # More than one mount and no way to tell which the operator meant. The
        # earlier version took whichever sorted first, which is a coin toss made
        # silently and then baked into a registration nothing can read back.
        # This fleet routinely mounts six or more. Record the ambiguity and fall
        # back to the one root that is defensible without guessing.
        # Review: coordinator:code-reviewer.
        report.rag_project_root_ambiguity = [c.name for c in checkouts]
        return str(_resolved_root("example-retrieval-repo", report))
    return str(_resolved_root("example-retrieval-repo", report))


def run_example_retrieval_repo_cloud_install(report: Report) -> None:
    """Run example-retrieval-repo's installer in ITS cloud pre-boot mode.

    Argv: ``--cloud`` (the mode's explicit operator flag), ``--non-interactive``
    (no prompt can be answered here), ``--project-root <resolved>``. Nothing else
    — every skip cloud mode performs (the vendored scip builds, the per-OS
    persistent service, the embed-model prefetch) is that mode's own decision,
    and re-asserting one here would create a second place to keep in step.

    ``--cloud`` is an ASSERTION, not a detection. This module IS the cloud
    provisioning entrypoint (see the module docstring), so it knows the mode
    first-hand; `resolve_cloud_mode` accepts the explicit flag for exactly this
    caller, and consults `env_locality` only to corroborate. No branch here is
    taken on locality.

    The child's stdout is printed in full, on success as well as failure. It
    carries two things nothing else records: the LSP PATH requirement this
    installer deliberately does not persist (the session's environment is
    already fixed, so an rc write cannot reach it — that leg belongs in the
    environment-variable box), and the paragraph stating what a correctly
    installed lean box reports about itself. Both must land in the setup log, or
    the first operator to read /health treats a working install as broken.
    """
    rag_root = _resolved_root("example-retrieval-repo", report)
    installer = rag_root / "example_retrieval_repo_scripts" / "install_example_retrieval_repo_plugin.py"
    if not installer.is_file():
        raise FileNotFoundError(
            f"example-retrieval-repo installer not found at {installer} — the checkout is "
            "incomplete or its layout changed"
        )
    project_root = _resolve_rag_project_root(report)
    # sys.executable, not a bare "python3": under an interpreter that is not the
    # first python3 on PATH the installer would resolve a different one than
    # everything around it, and the pre-boot set would land where the rest of
    # the run does not look. _machine_local_argv already does this.
    # Review: coordinator:code-reviewer.
    argv = [
        sys.executable,
        str(installer),
        "--cloud",
        "--non-interactive",
        "--project-root",
        project_root,
    ]
    report.rag_install = {
        "installer": str(installer),
        "project_root": project_root,
        "argv": argv,
        "exit_code": None,
    }
    # stderr is FOLDED INTO stdout rather than captured beside it. The installer
    # routes its most consequential lines to stderr -- the boot-postcondition
    # ERROR and its log dump, the harness/locality disagreement WARNING, the
    # interpreter probe line -- and exits 0 on some of them. Capturing stderr
    # only to interpolate into the raise below discarded those lines on exactly
    # the runs where they mattered, leaving the setup log reading "OK" for a box
    # whose daemon does not boot. Review: coordinator:code-reviewer (P1).
    result = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=RAG_INSTALL_TIMEOUT_S,
        # stdin explicitly closed, not inherited; see _git_clone's comment. The
        # installer's own --non-interactive is not relied on alone: a prompt
        # gated on isatty rather than on the flag would otherwise block for the
        # whole RAG_INSTALL_TIMEOUT_S ceiling.
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _safe_print(result.stdout.rstrip())
    report.rag_install["exit_code"] = result.returncode
    if result.returncode != 0:
        raise RuntimeError(
            f"example-retrieval-repo installer exited {result.returncode}; its combined output is above"
        )


def _claude_json_path() -> Path:
    """`$HOME/.claude.json` — the MCP config surface, a SIBLING of `.claude/`.

    Derived from `_claude_home()` so both resolve from the same base (and so a
    doubled CLAUDE_HOME is refused here too, rather than silently verifying a
    file no session reads).
    """
    return _claude_home().parent / ".claude.json"


def _expected_daemon_url(rag_root: Path) -> tuple[str | None, str]:
    """The daemon URL the MCP entry must carry, DERIVED from the example-retrieval-repo
    checkout's own `example_retrieval_repo_mcp/http_config.py`.

    Returns ``(url_or_None, source_description)``.

    The contract names that module as the port's truth source and says the
    registration URL and the spawned daemon must agree; a port copied into this
    file would be a second declaration that agrees only until someone moves the
    port. The module is loaded BY PATH, not imported as part of its package: it
    is stdlib-only, so this works on an interpreter that carries none of
    example-retrieval-repo's dependencies — which is exactly the interpreter running here.
    """
    config_path = rag_root / "example_retrieval_repo_mcp" / "http_config.py"
    if not config_path.is_file():
        return None, f"{config_path} not found"
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_cloud_setup_http_config", config_path)
        if spec is None or spec.loader is None:
            return None, f"{config_path} could not be loaded as a module"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        host = getattr(module, "EXAMPLE_RETRIEVAL_REPO_HTTP_HOST")
        port = getattr(module, "EXAMPLE_RETRIEVAL_REPO_HTTP_PORT")
    except Exception as e:  # noqa: BLE001 - an unreadable truth source is a recorded miss
        return None, f"{config_path} did not yield host/port: {type(e).__name__}: {e}"
    return f"http://{host}:{port}/mcp", str(config_path)


def verify_mcp_registration(report: Report) -> None:
    """Read `~/.claude.json` back OFF DISK and record what a session launched in
    this VM will actually see for `mcpServers.example-retrieval-repo`.

    Never asserts what the installer reported doing: a session reads this file at
    startup and cannot be told afterwards, so the only fact worth recording is
    the one on disk. A missing or mismatched entry is a RECORDED verdict, not a
    raise — the coordinator half of this install is still good without it, and
    aborting would trade a retrieval-less session for no session at all.

    ``url_matches_daemon_port`` is the one check that is not cosmetic: an entry
    naming a different port than the daemon binds is a registration that connects
    to nothing, and it looks identical to a healthy one in the config file.
    """
    config_path = _claude_json_path()
    expected_url, port_source = (None, "example-retrieval-repo checkout unresolved")
    rag_root = report.rag_roots.get("example-retrieval-repo")
    if rag_root:
        expected_url, port_source = _expected_daemon_url(Path(rag_root))
    result = {
        "config_path": str(config_path),
        "registered": False,
        "type": None,
        "url": None,
        "expected_url": expected_url,
        "port_source": port_source,
        "url_matches_daemon_port": False,
    }
    try:
        data = json.loads(config_path.read_text())
    except Exception as e:  # noqa: BLE001 - absent/unreadable config is a recorded miss
        result["read_error"] = f"{type(e).__name__}: {e}"
        report.mcp_registration = result
        return
    entry = data.get("mcpServers", {}).get("example-retrieval-repo")
    if isinstance(entry, dict):
        result["registered"] = True
        result["type"] = entry.get("type")
        result["url"] = entry.get("url")
        result["url_matches_daemon_port"] = bool(expected_url) and entry.get("url") == expected_url
    report.mcp_registration = result
    verdict = "OK" if result["url_matches_daemon_port"] else "NOT REGISTERED as expected"
    _safe_print(
        f"[cloud_setup] MCP registration: {verdict} "
        f"(entry={result['url']!r}, expected={expected_url!r}, port source: {port_source})"
    )


def write_report(report: Report) -> None:
    INSTALL_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_REPORT_PATH.write_text(json.dumps(report.to_dict(), indent=2))


def _safe_print(text: str) -> None:
    """Print text that may contain content this process did not choose, without
    ever raising past this call.

    Called from `_print_summary`, which runs in `main` outside any `run_step`
    net. `step.detail` is built from raw subprocess stderr / exception text
    (git, pip, the install orchestrator), so a minimal-locale host (LANG=C, no
    UTF-8) can hand this a non-ASCII byte `print()` cannot encode. An
    encoding-safe write makes that failure impossible rather than caught: a
    try/except around the call would still lose the whole summary (and the
    install report, written after it) the moment one byte is odd, where
    sanitizing keeps the summary visible.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_text)


def _print_summary(report: Report) -> None:
    """Print every step's verdict AND its elapsed time, plus the phase total.

    The elapsed figures are printed, not merely stored, because the setup log is
    the artifact a later verification reads the pre-boot budget out of: the JSON
    report can be lost with the container, while the log is what the operator
    already has in front of them.
    """
    _safe_print("[cloud_setup] summary:")
    total = 0.0
    for step in report.steps:
        verdict = "OK" if step.ok else "FAILED"
        elapsed = f"{step.elapsed_s:.2f}s" if step.elapsed_s is not None else "unmeasured"
        total += step.elapsed_s or 0.0
        _safe_print(f"  - {step.name}: {verdict} [{elapsed}] ({step.detail})")
    report.total_elapsed_s = total
    _safe_print(f"[cloud_setup] pre-boot elapsed total: {total:.2f}s across {len(report.steps)} steps")
    # The corpus non-action still reaches the log, without buying a pipeline step
    # for a literal — Review: coordinator:overengineering-reviewer (F7).
    _safe_print(
        "[cloud_setup] engine corpus: NOT hydrated, by design (lazy tier). "
        "Structural and lexical retrieval work now; semantic retrieval reports a typed "
        "not-hydrated verdict rather than an empty result list."
    )


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
    run_step("register plugin settings", register_plugin_settings, report)
    run_step("verify plugin settings", lambda: verify_plugin_settings(report), report)
    run_step("install global doctrine", lambda: install_global_doctrine(report), report)
    run_step("verify global doctrine", lambda: verify_global_doctrine(report), report)

    # The example-retrieval-repo half, AFTER the coordinator trampoline and scripts/setup.py
    # above. That ordering is a hard constraint, not a preference: example-retrieval-repo's
    # installer seeds a concern into the machine-local registry those two steps
    # create, so running it earlier reproduces the documented refusal — and the
    # refusal reads as a example-retrieval-repo defect rather than an ordering one.
    run_step("locate or clone example-retrieval-repo", lambda: locate_or_clone_repo("example-retrieval-repo", report), report)
    run_step(
        "locate or clone example-retrieval-repo-ue-addon",
        lambda: locate_or_clone_repo("example-retrieval-repo-ue-addon", report),
        report,
    )
    run_step("register machine-local repo keys", lambda: register_machine_local_repo_keys(report), report)
    run_step("example-retrieval-repo cloud install", lambda: run_example_retrieval_repo_cloud_install(report), report)
    run_step("verify MCP registration", lambda: verify_mcp_registration(report), report)

    _print_summary(report)

    _write_report_best_effort(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
