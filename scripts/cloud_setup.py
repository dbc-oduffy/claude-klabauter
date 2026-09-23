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

import base64
import dataclasses
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
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
    # The retrieval half is NOT hardcoded here, deliberately. This file is
    # published to a PUBLIC mirror, so a private repository's URL cannot live in
    # it: the publish pipeline scrubs the name, and a scrubbed URL is worse than
    # an absent one because it clones something that does not exist and records
    # a failure nobody reads as "the mirror rewrote my URL". The two entries are
    # composed at run time from the environment instead, and the setup-script
    # field in the cloud-environment form is where an operator supplies them —
    # that field is private to the environment, which the published mirror is
    # not. Absent, the retrieval steps skip with a recorded verdict.
}

#: The retrieval repo's hyphenated slug, and the underscored module-name
#: prefix derived from it. Read from an env var FIRST, the real in-repo name
#: second. That ordering is what makes an operator-side override survive the
#: publish scrub: the scrub rewrites literal SOURCE TEXT (so the fallback
#: below still turns into the public placeholder in the published mirror,
#: exactly as before), but it cannot see or touch a value that only exists at
#: runtime in someone's own environment. So an operator whose Setup-script
#: bash does ``export COORDINATOR_RETRIEVAL_REPO_SLUG=example-retrieval-repo`` ahead of
#: the ``curl | python3`` line restores the real name for every retrieval-half
#: step below, with no edit to this file and no change to what gets scrubbed.
#: Unset, behavior is exactly what it was before this override existed.
RETRIEVAL_REPO_SLUG = (os.environ.get("COORDINATOR_RETRIEVAL_REPO_SLUG") or "example-retrieval-repo").strip()
RETRIEVAL_MODULE_PREFIX = RETRIEVAL_REPO_SLUG.replace("-", "_")
RETRIEVAL_UE_ADDON_SLUG = f"{RETRIEVAL_REPO_SLUG}-ue-addon"

#: Where a cloud environment puts the repositories an operator selected for it.
#: DISCOVERED, not assumed: the first cut hardcoded /workspace, which does not
#: exist on this platform — the selected checkouts live under /home/user — so the
#: locate step could never have found them and every environment would have
#: reported the retrieval half absent while it was sitting on disk.
#: Ordered by specificity; the env override wins for a layout none of these match.
#: Registry key per retrieval repo, so a session resolves either checkout by
#: key rather than by a literal path this script happened to choose.
MACHINE_LOCAL_REPO_KEYS: dict[str, str] = {
    RETRIEVAL_REPO_SLUG: f"repos.{RETRIEVAL_MODULE_PREFIX}",
    RETRIEVAL_UE_ADDON_SLUG: f"repos.{RETRIEVAL_MODULE_PREFIX}_ue_addon",
}

#: The two clones' machine-local registry keys, written BEFORE the install
#: orchestrator runs. These are the trust anchors
#: `coordinator_core/trusted_root_guard.py` resolves `CLAUDE_PLUGIN_ROOT`
#: against: with neither key set, the orchestrator's own install-health phase
#: fail-loud-refuses the clone THIS SCRIPT made and aborts mid-run, leaving the
#: hook plane unwired and `.doe-root` unwritten. A clone destination this process
#: chose itself is not an untrusted root, and this is where that gets recorded
#: rather than worked around with COORDINATOR_PLUGIN_ROOT_TRUSTED=1.
TRUST_ANCHOR_KEYS: dict[str, str] = {
    "coordinator-claude": "repos.doe_claude",
    "klabauter": "repos.claude_klabauter",
}

#: The SERVED plugin tree's own key, written for the coordinator clone in every
#: shape. `repos.doe_claude` and this key mean two different things that coincide
#: on a workstation and diverge here: the former names the DoE-claude AUTHORING
#: checkout (the fleet sibling-map entry, and what `coordinator_doe_root` resolves
#: for anything reading schemas, wikis or doctrine records), the latter names the
#: tree a session actually RUNS. This container serves the flat published mirror,
#: which carries no `coordinator/schemas/`, no `docs/wiki/` and no decision
#: records — so registering it under the authoring key makes every doctrine read
#: resolve against a tree that does not carry the content, silently.
#: Same key-split reasoning as `PUBLISH_MIRROR_KEYS` below, for the same reason.
PLUGIN_MIRROR_LIVE_PATH_KEY = "plugin.mirrors.coordinator-claude.live_path"

#: The fleet-wide dev-vs-OSS discriminant, per DoE-claude's own `CLAUDE.md`. Its
#: presence at a checkout's ROOT is what makes that checkout the authoring tree;
#: a repository NAME is not the test (a name is also the one thing the publish
#: scrub rewrites, so a literal here would be dead on the public mirror).
DEV_REPO_SENTINEL = ".coordinator-dev-repo"

#: Where this run's own verdicts land. A process exiting 0 is not evidence that
#: anything installed; this file is.
INSTALL_REPORT_PATH = Path("/root/cloud-setup-report.json")

#: Binaries whose absence from a SESSION's PATH silently disables a whole plane:
#: `python3` carries every coordinator hook registration, the two language
#: servers carry the LSP plugins. Resolved here, pre-boot, so the value the
#: cloud dialog's env-var box needs is determined off the image rather than
#: re-derived by hand against a session that has already booted wrong.
SESSION_PATH_BINARIES = ("python3", "pyright-langserver", "typescript-language-server")

#: Basenames of the two session-facing surfaces. `<claude_home>/rules/*.md` is
#: loaded into session context by the harness itself, with no interpreter and no
#: hook — which is the property these files are chosen for, not a convenience.
#:
#: They are separate because they mean opposite things. The VERDICT file reports
#: that something is wrong, so its presence is the signal and a clean run leaves
#: none. ORIENTATION states the ordinary shape of a cloud container — several
#: repos mounted, no single work target — which is not a fault and must never be
#: filed next to failures, where a reader infers one.
SESSION_VERDICT_RULE = "cloud-preboot-verdict.md"
SESSION_ORIENTATION_RULE = "cloud-session-orientation.md"

#: The variable a session sets to declare which mounted repo is the SUBJECT of its
#: work, the rest being present to hold the system up. Named in example-retrieval-repo's
#: namespace because example-retrieval-repo owns the resolution semantics and consumers read
#: them, never the reverse.
#:
#: Stated, never read, by this script: the cloud dialog's env-var box does not
#: reach this process, so whether a session declares a target is unknowable here.
#: The orientation surface therefore describes the contract and lets the session,
#: which can see its own environment, resolve it.
SESSION_FOCUS_ENV = "EXAMPLE_RETRIEVAL_REPO_FOCUS_REPO"

#: The platform's own record of where each installed plugin lives.
#: `<claude_home>/plugins/installed_plugins.json` is what every `claude`
#: process reads to expand `${CLAUDE_PLUGIN_ROOT}` in a plugin's `hooks.json`.
#: An `installPath` naming a directory that does not exist expands that
#: variable to EMPTY, which disables the whole hook plane while every other
#: surface — `settings.json`, the marketplace record, the plugin listing —
#: still reports healthy.
PLUGIN_RECORD_REL = ("plugins", "installed_plugins.json")

#: Marker a resolvable plugin root must carry. Presence of the directory alone
#: is not enough: a plugin root without its own manifest is not one.
PLUGIN_MANIFEST_REL = (".claude-plugin", "plugin.json")

#: The plugin's own hook manifest, relative to its install root. Where plugin-side
#: hook delivery lives — and, on a healthy install, the ONLY place hooks are
#: registered (see `assert_hook_plane_armed`).
PLUGIN_HOOKS_REL = ("hooks", "hooks.json")

#: A script file's recognized extensions inside a hook command or argument —
#: the engine's own `guard_settings_integrity._SCRIPT_TOKEN_RE` set, re-stated
#: because this module must not import `coordinator_core`. Checked against an
#: already `shlex`-isolated piece (`_shlex_pieces`), not scanned with `\S*`
#: over a raw multi-word string: a quoted path with an embedded space is one
#: piece by then, and `\S*` cannot cross a space it might still contain
#: (code-reviewer F2).
_SCRIPT_EXTENSIONS = (".py", ".sh", ".mjs", ".js")


def _script_tail(piece: str) -> str | None:
    """The `<dir>/<file>` tail of `piece` if it names a script file, else None."""
    normalized = piece.replace("\\", "/").strip("'\"")
    if not normalized.endswith(_SCRIPT_EXTENSIONS):
        return None
    tail = "/".join(normalized.split("/")[-2:])
    return tail or None


#: The marketplace manifest, read for the marketplace's declared name. Both
#: halves of the record's `<plugin>@<marketplace>` key are READ from the clone,
#: never spelled here — a literal survives exactly until either is renamed and
#: then registers a key nothing resolves while reporting success.
MARKETPLACE_MANIFEST_REL = (".claude-plugin", "marketplace.json")

#: The auto-compact window this script pins for a cloud container, in tokens.
#:
#: Compaction, not the handoff, is the continuity primitive in cloud: an
#: unattended session has no next turn to hand to, so the only thing that
#: keeps a long run alive is the context being folded down in place. Left
#: unset, the window resolves from the model default — up to 1,000,000 — and
#: the first compaction lands correspondingly late, which is the expensive
#: behaviour this value exists to prevent.
#:
#: The runner injects `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80`, which does not set
#: the window: it only LOWERS the firing threshold within whatever window is in
#: force. So the two stack, and the effective cut is ~0.8 x this value, i.e.
#: ~400k tokens — an earlier cut than a desktop session wants, deliberately,
#: because a desktop operator can compact by hand and a cloud container cannot.
#:
#: The binary's own bounds are 100,000..1,000,000 and it clamps to the model
#: window, so this value must stay inside that range.
CLOUD_AUTO_COMPACT_WINDOW_TOKENS = 500_000

#: The env var and the settings key the binary resolves the window from, in
#: that order of precedence (`env` outranks `settings`). BOTH are written, from
#: the one constant above so they cannot drift: the env rung is the one no
#: later `settings`-rung resolution can outrank, and the settings key is what
#: `/autocompact` and the context UI read back — leaving it unset would make
#: the UI misreport a window that is actually in force.
AUTO_COMPACT_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
AUTO_COMPACT_WINDOW_SETTING = "autoCompactWindow"

NETWORK_MAX_ATTEMPTS = 3
#: Ceiling for the retrieval installer subprocess, sized to sit just ABOVE that
#: installer's own internal pip ceiling so a wedged pip is reported by the layer
#: that can name the package. A ceiling is not a budget.
RAG_INSTALL_TIMEOUT_S = 960
MACHINE_LOCAL_TIMEOUT_S = 60

RETRIEVAL_ROOT_ENV = "COORDINATOR_RETRIEVAL_ROOT"

#: `pytest-of-*` basename prefix under the system temp dir. pytest's own
#: retention keeps the last 3 base directories PER caller, which is unbounded
#: in practice across many concurrent test waves (claude-klabauter#27):
#: observed trees up to ~21G each, filling the container volume and failing
#: every subsequent Bash/Write/Edit with ENOSPC — including the harness's own
#: subprocess-output capture, which destroys in-flight executor work with no
#: diagnosable error at the point it is lost.
STALE_PYTEST_TREE_PREFIX = "pytest-of-"

#: Age floor, in seconds, below which a `pytest-of-*` tree is left alone. A
#: concurrent run may still own a tree younger than this — mtime updates on
#: every write beneath it — so only a tree this old is presumed abandoned.
STALE_PYTEST_TREE_AGE_S = 6 * 60 * 60


def retrieval_search_roots() -> "list[Path]":
    override = (os.environ.get(RETRIEVAL_ROOT_ENV) or "").strip()
    roots = [Path(override)] if override else []
    roots += [Path("/home/user"), Path("/workspace"), Path("/root"), Path.cwd()]
    seen: list[Path] = []
    for r in roots:
        if r not in seen:
            seen.append(r)
    return seen


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
    #: Verdict of `install_engine_cli_shims`: shim dir, the bin dir CLI names
    #: were enumerated off, and which bare names were written vs. left alone
    #: because a non-sentinel file already owned that name.
    engine_cli_shims: dict | None = None
    container_optin_requested: bool | None = None
    setup_exit_code: int | None = None
    plugin_settings: dict | None = None
    #: The DoE-claude authoring checkout this container mounted, if any, and what
    #: was done about it. None means a pure-consumer container — a supported shape.
    doe_authoring_tree: str | None = None
    #: Whether the cloned engine's guard trusts a registry anchor's own root, and
    #: so whether the install orchestrator can accept the flat served mirror at
    #: all. Recorded because the refusal it causes names no empty anchor, which
    #: reads as a misconfiguration this script could fix rather than the engine
    #: version skew it is. `None` means the probe did not run.
    engine_guard_anchor_root_trust: str | None = None
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
    #: Where the session-critical binaries actually are on this image, and the
    #: PATH value the cloud dialog's env-var box must therefore carry. A
    #: determination, never an observation of the running session: the box does
    #: not reach this process, so what the session got is unknowable from here.
    session_path: dict | None = None
    #: Whether this run left a verdict surface for the session to read, and where.
    session_verdict: dict | None = None
    #: Whether the composed PATH was pinned into `settings.json`'s `env` block,
    #: and the value pinned. Kept OFF `session_path` on purpose: that field is
    #: re-derived post-pipeline and would wipe a key written here.
    session_path_pin: dict | None = None
    #: The post-boot assertion that the hook plane is actually armed: hooks
    #: registered by `settings.json` or by the coordinator plugin's own manifest,
    #: and a resolvable `.doe-root` at each
    #: location the no-launcher fences read. The one check that converts "wired
    #: nothing" from byte-identical-to-healthy into a named failure.
    hook_plane: dict | None = None
    #: `settings.json` hooks removed because the coordinator plugin already
    #: delivers them on the same event (`drop_double_fired_settings_hooks`).
    hook_dedupe: dict | None = None
    #: The settings-manifest env checker's verdict after its apply pass: which
    #: all-machines values it wrote and which findings remain
    #: (`apply_settings_manifest_env`).
    settings_env: dict | None = None
    #: The platform's installed-plugin record as this run left it, and whether
    #: the path it names actually resolves. Separate from `plugin_settings`
    #: because they are different files answering different questions: that one
    #: says the plugin is ENABLED, this one says where its code IS.
    plugin_install_path: dict | None = None
    #: The auto-compact window this run pinned, and through which rungs. A
    #: determination recorded the same way `session_path.env_box_value` is, so
    #: an operator reads the chosen window off an artifact rather than off code.
    auto_compact: dict | None = None
    #: The MCP entry this run wrote, independently of the clone steps.
    mcp_entry_written: dict | None = None
    machine_local_keys: dict = field(default_factory=dict)
    #: Verdict of the git-hook-fleet install step: whether
    #: `coordinator-ensure-hooks-fleet` was invoked, and — asserted against
    #: disk, never trusted off its rc — which registered repos actually ended
    #: up with a `prepare-commit-msg` hook file. See `install_hooks_fleet`.
    hooks_fleet: dict | None = None
    rag_install: dict | None = None
    mcp_registration: dict | None = None
    #: Verdict of the stale-pytest-tree reaper (claude-klabauter#27): which
    #: `pytest-of-*` trees under the system temp dir were removed vs. left
    #: alone as too young, and the bytes freed.
    pytest_tree_reap: dict | None = None
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
    `scripts/tests/test_cloud_setup_orchestration.py` pins), so short-circuiting later
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


#: Name of the platform's own per-session fresh checkout of the engine repo
#: under `/home/user`, distinct from `CLONES["klabauter"]` (this script's own
#: `/root` clone). See `install_engine_cli_shims` for why the fresh checkout
#: is preferred over the frozen clone. `locate_existing_checkout` already
#: treats any name absent from `CLONES` as "search every retrieval root for a
#: `.git` checkout of this name", reused rather than duplicated here.
FRESH_ENGINE_CHECKOUT_NAME = "claude-klabauter"

#: Where the fresh checkout lands when the platform mounts it, spelled out
#: literally for `_shim_source` — a generated shim cannot import this module
#: (see `install_engine_cli_shims`) and must carry this path as its own text.
FRESH_ENGINE_CHECKOUT_PATH = f"/home/user/{FRESH_ENGINE_CHECKOUT_NAME}"  # abs-path-ok: single-host cloud VM entrypoint (module docstring)

#: Directory the bare-name engine CLI shims are written into
#: (`install_engine_cli_shims`). First entry on this container's own root
#: PATH (verified against the image, not derived from `/etc/environment`,
#: which does not carry it — a login-shell-only addition `resolve_session_path`
#: already accounts for by searching this process' own PATH as a fallback
#: rung), so a bare `coordinator-invoke` et al. resolves here before anything
#: else on PATH.
SHIM_DIR = Path("/root/.local/bin")  # abs-path-ok: single-host cloud VM entrypoint (module docstring)

#: First line of every shim this script writes, so a re-run can tell its own
#: prior output (safe to overwrite) from any other file at the same bare name
#: — an EM's hand-wired shim, an operator's own script — which it must leave
#: alone. Never matched against by prefix or basename: a file this script did
#: not write itself never carries this exact line.
_SHIM_SENTINEL = "# cloud_setup.py: engine-cli-shim (auto-generated, do not hand-edit)"


#: The stable symlink pinned as `COORDINATOR_ENGINE_ROOT` and the target every
#: engine CLI shim execs through. Created ONLY at setup time, pointed at the
#: frozen `/root/klabauter` clone — never at a `/home/user/...` checkout,
#: which is not mounted yet when this script runs (module docstring, fact 1).
#: A SessionStart hook (`coordinator_core.hooks.repin_cloud_engine_root`)
#: re-points this same link, atomically, onto a fresher per-session checkout
#: once one is mounted — see that module's docstring for the freshness rule.
#: claude-klabauter#67 (comments 5785027514, 5785078234).
ENGINE_CURRENT_LINK = Path("/root/engine-current")  # abs-path-ok: single-host cloud VM entrypoint (module docstring)


def _create_engine_current_symlink() -> None:
    """Create/refresh the stable `/root/engine-current` symlink onto this
    script's own frozen `/root/klabauter` clone.

    Idempotent: a temp symlink is created beside the target and `os.replace`d
    onto it, so a re-run always re-creates the link atomically rather than
    erroring on an existing one.
    """
    target = Path(CLONES["klabauter"]["dest"])
    ENGINE_CURRENT_LINK.parent.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE_CURRENT_LINK.with_name(ENGINE_CURRENT_LINK.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(ENGINE_CURRENT_LINK)


def set_engine_env(report: Report) -> None:
    """Set COORDINATOR_ENGINE_ROOT and COORDINATOR_SETTINGS_HOME in this process' own
    environment — the env-var block is not readable from the setup script's shell
    (fact 2), so this process must set them itself before invoking setup.py.

    Also (re-)creates `ENGINE_CURRENT_LINK`, since `_engine_env` now pins
    `COORDINATOR_ENGINE_ROOT` at that stable link rather than at a root chosen
    at this moment — see `ENGINE_CURRENT_LINK`'s own docstring for why.
    """
    _create_engine_current_symlink()
    engine_env = _engine_env()
    os.environ.update(engine_env)
    report.engine_root = engine_env["COORDINATOR_ENGINE_ROOT"]
    report.settings_home = engine_env["COORDINATOR_SETTINGS_HOME"]


def _engine_env() -> dict[str, str]:
    """The engine root and settings home this script installs into, as the env
    vars every engine surface resolves them from. One derivation, read by both
    this process (`set_engine_env`) and every later session (`register_plugin_settings`).

    `COORDINATOR_ENGINE_ROOT` is pinned at the STABLE `ENGINE_CURRENT_LINK`,
    never at a root resolved at this moment — see that constant's docstring:
    the link is what a SessionStart hook can re-point later without this
    script (or any session env) needing to change.
    """
    return {
        "COORDINATOR_ENGINE_ROOT": str(ENGINE_CURRENT_LINK),
        "COORDINATOR_SETTINGS_HOME": str(
            Path(CLONES["coordinator-claude"]["dest"]) / ".coordinator-claude-settings"
        ),
    }


def _preferred_engine_root() -> Path:
    """The engine root to enumerate CLI names off (`install_engine_cli_shims`):
    the platform's fresh per-session checkout if mounted right now, else this
    script's own `/root/klabauter` clone.

    A miss (fresh checkout not yet mounted) is the routine case, since this
    runs before the platform mounts anything under `/home/user` — see
    `install_engine_cli_shims` for the fresh-vs-frozen rationale. Distinct
    from `ENGINE_CURRENT_LINK`, which every generated shim execs through
    instead of re-deriving this preference at call time — see
    `_shim_source`.
    """
    fresh = locate_existing_checkout(FRESH_ENGINE_CHECKOUT_NAME)
    if fresh is not None:
        return fresh
    return Path(CLONES["klabauter"]["dest"])


def _shim_source(name: str, engine_link: str) -> str:
    """The exact text of the bare-name trampoline for CLI *name*.

    Self-contained on purpose: this file cannot `import cloud_setup` (it runs
    as `sys.executable <this file>`, standalone, in whatever process later
    types the bare command — the harness never puts this repo on that
    process' `sys.path`), so every value it needs is interpolated as a
    literal at generation time.

    Execs through `engine_link` — the stable `ENGINE_CURRENT_LINK` symlink,
    never a fresh-vs-fallback choice made here. The fresh-vs-frozen decision
    now lives in one place, `coordinator_core.hooks.repin_cloud_engine_root`
    (a SessionStart hook, re-pointing the same link atomically), not
    re-derived per shim invocation — see `install_engine_cli_shims`'s
    docstring and claude-klabauter#67 (comments 5785027514, 5785078234).
    """
    return (
        "#!/usr/bin/env python3\n"
        f"{_SHIM_SENTINEL}\n"
        f'"""{name} — bare-name trampoline for coordinator/bin/{name}.py,\n'
        "generated by scripts/cloud_setup.py :: install_engine_cli_shims.\n"
        "\n"
        "Execs through the stable engine-current symlink, whose target a\n"
        "SessionStart hook may re-point onto a fresher per-session checkout\n"
        "— this shim never re-derives fresh-vs-frozen itself.\n"
        '"""\n'
        "import os\n"
        "import sys\n"
        "\n"
        f"ENGINE_LINK = {engine_link!r}\n"
        "\n"
        "os.environ[\"COORDINATOR_ENGINE_ROOT\"] = ENGINE_LINK\n"
        f"target = os.path.join(ENGINE_LINK, \"coordinator\", \"bin\", {name!r} + \".py\")\n"
        "os.execv(sys.executable, [sys.executable, target, *sys.argv[1:]])\n"
    )


def install_engine_cli_shims(report: Report) -> None:
    """Generate a bare-name trampoline in `SHIM_DIR` for every public
    `coordinator/bin/*.py` CLI of the engine checkout.

    Skills and emitted workflow prompts invoke coordinator CLIs by bare name
    (`backlog-grind-assemble`, `coordinator-invoke`, ...), extensionless.
    Nothing on this image's PATH supplies that: the `.py` files need a `.py`
    suffix to resolve and the engine's own `.cmd`/`.ps1` launchers are
    Windows-only. `SHIM_DIR` is the first entry already on this container's
    PATH, so a shim landing there wins bare-name resolution for free.

    Heuristic for "public CLI": a `.py` file in `coordinator/bin/` with a
    sibling `.cmd` of the same stem. `coordinator/bin/gen-launcher-shim.py`
    generates a `.cmd` ONLY for the engine's own public entry points, so the
    pairing is the engine's own signal, not a guess — verified against this
    checkout: every `.cmd`-less `.py` under `coordinator/bin/` is a test
    module (`test_*.py`), `conftest.py`, or a `_`-prefixed private helper,
    never something meant to be run bare.

    This step enumerates CLI names off whichever checkout is on disk right
    now (`_preferred_engine_root`), but — unlike that enumeration — what each
    generated shim actually execs against is re-decided at CALL time inside
    the shim itself (`_shim_source`), so a fresh checkout that appears only
    AFTER this step ran is still picked up by every later invocation.

    THE FRESH-VS-FROZEN RATIONALE (stated once, here — every other reference
    in this module points back to this paragraph): this script's own
    `/root/klabauter` clone is snapshotted the moment this run ends, while the
    platform re-clones `claude-klabauter` fresh under `/home/user` every
    session (abs-path-ok: single-host cloud VM entrypoint, module docstring),
    so a value pinned once at setup time goes stale the instant the
    engine's default branch moves. Both `coordinator_core.engine_root`
    (claude-klabauter's own resolver) and claude-klabauter's `cc_invoke.resolve_engine_root`
    treat `COORDINATOR_ENGINE_ROOT` as their highest-precedence rung, so
    whichever path a consumer resolves is what it actually runs against.

    FOLDED BACK (claude-klabauter#67, comments 5785027514, 5785078234): every
    generated shim used to re-resolve fresh-vs-frozen itself at CALL time.
    It now execs through `ENGINE_CURRENT_LINK`, a stable symlink this script
    pins at the frozen clone (`_create_engine_current_symlink`) and that a
    SessionStart hook (`coordinator_core.hooks.repin_cloud_engine_root`)
    atomically re-points onto a fresher per-session checkout once one is
    mounted and stamped no older than the frozen root's own stamp. One
    fresh-vs-frozen decision, made where a session actually exists to judge
    freshness — not re-derived per shim invocation from a same-process
    `os.path.isdir` check that could never see a stamp.

    Idempotent and non-destructive: a target already carrying
    `_SHIM_SENTINEL` is this step's own prior output and is overwritten; any
    OTHER pre-existing file at that bare name (an EM's hand-wired shim, an
    operator's own script) is left untouched and recorded as skipped, never
    clobbered.
    """
    engine_root = _preferred_engine_root()
    bin_dir = engine_root / "coordinator" / "bin"
    if not bin_dir.is_dir():
        raise FileNotFoundError(
            f"{bin_dir} does not exist — the engine checkout this step "
            "enumerates CLIs off is absent or its layout changed"
        )
    SHIM_DIR.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped_foreign: list[str] = []
    for py_file in sorted(bin_dir.glob("*.py")):
        name = py_file.stem
        if not (bin_dir / f"{name}.cmd").is_file():
            continue
        target = SHIM_DIR / name
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if _SHIM_SENTINEL not in existing:
                skipped_foreign.append(name)
                continue
        source = _shim_source(name, str(ENGINE_CURRENT_LINK))
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(source, encoding="utf-8", newline="\n")
        tmp.chmod(0o755)
        tmp.replace(target)
        written.append(name)

    report.engine_cli_shims = {
        "shim_dir": str(SHIM_DIR),
        "bin_dir": str(bin_dir),
        "written": written,
        "skipped_foreign": skipped_foreign,
    }


def seed_trust_anchor_keys(report: Report) -> None:
    """Register both clone roots in the machine-local registry BEFORE the
    install orchestrator runs, so the orchestrator's own phases can trust them.

    THIS IS THE ORDERING THE ORCHESTRATOR DEPENDS ON, not a convenience.
    `coordinator_core/trusted_root_guard.py` decides whether a resolved
    `CLAUDE_PLUGIN_ROOT` is trusted by comparing it against, among others, the
    registry keys `repos.doe_claude` and `repos.claude_klabauter`. Nothing on a
    fresh container has written either: `.doe-root` is generated by
    `scripts/setup.py`, which runs AFTER the orchestrator, and the orchestrator
    is where the pointer generator's own prerequisites get installed. So every
    anchor resolved empty, the orchestrator's install-health phase
    fail-loud-refused the clone this very process had just made, and the run
    aborted with the hook plane unwired, `.doe-root` unwritten, and
    `cloud_setup.py` still exiting 0 — a container behaviourally
    indistinguishable from a healthy one until an agent notices a hook that
    never fired.

    The keys are written from `CLONES`, whose destinations this process CHOSE.
    That is why this is a seeding step and not a `COORDINATOR_PLUGIN_ROOT_TRUSTED=1`
    export: the env-var opt-out masks an empty anchor (the guard's own
    diagnostics say so), while this makes the anchor correct. A container that
    later re-resolves either root reads the same value a session would.

    TWO KEYS, NOT ONE, for the coordinator clone. `repos.doe_claude` names the
    DoE-claude AUTHORING checkout — the fleet sibling-map entry, and what
    `coordinator_core.ops.coordinator_doe_root` resolves for anything reading
    schemas, wikis or decision records. `plugin.mirrors.coordinator-claude.live_path`
    names the tree a session RUNS. On a workstation those are one directory and the
    distinction never surfaces; here they diverge, because what runs is the flat
    published mirror and the mirror publishes none of the authoring content. Writing
    the mirror path into the authoring key made every doctrine read resolve against a
    tree that does not carry it — silently, since an absent doctrine file under a
    mirror is the documented normal case and therefore reads as nothing being wrong.

    So the mirror is registered under the mirror key ALWAYS (that is the trust anchor
    the orchestrator needs, and the anchor `trusted_root_guard` resolves for it), and
    `repos.doe_claude` is pointed at a mounted authoring tree when one is present,
    detected by the `.coordinator-dev-repo` sentinel. With no authoring tree mounted
    — the pure-consumer container, a supported shape — `repos.doe_claude` keeps
    naming the mirror exactly as before: a demoted-but-present anchor beats an empty
    one, and the mirror is the only coordinator content that box has.

    Nothing here changes what the session RUNS. The marketplace registration in
    `settings.json` names the mirror path literally and is not read from either key.

    Every key is attempted; per-key verdicts land on `report.machine_local_keys`
    beside the retrieval half's. The step RAISES when a key the orchestrator
    needs did not land, because a silent skip here is exactly the failure this
    function exists to end.
    """
    argv = _machine_local_argv()
    failures: list[str] = []

    def _write(key: str, value: str, *, fatal: bool) -> None:
        result = subprocess.run(
            [*argv, "set", key, value],
            capture_output=True,
            text=True,
            timeout=MACHINE_LOCAL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            report.machine_local_keys[key] = value
            return
        report.machine_local_keys[key] = (
            f"failed (exit {result.returncode}): {result.stderr.strip()}"
        )
        if fatal:
            failures.append(f"{key} (exit {result.returncode})")

    targets: dict[str, tuple[str, bool]] = {}
    for clone_name, key in TRUST_ANCHOR_KEYS.items():
        root = CLONES[clone_name]["dest"]
        if not Path(root).is_dir():
            report.machine_local_keys[key] = f"skipped: {clone_name} clone absent at {root}"
            failures.append(f"{key} ({clone_name} clone absent)")
            continue
        targets[key] = (root, True)

    mirror_root = CLONES["coordinator-claude"]["dest"]
    if Path(mirror_root).is_dir():
        targets[PLUGIN_MIRROR_LIVE_PATH_KEY] = (mirror_root, True)

    # Recorded before the keys are written, because no arrangement of them can
    # substitute for the capability: a guard matching strict descendants only
    # refuses the flat mirror whichever key names it.
    if engine_guard_trusts_anchor_root_itself():
        report.engine_guard_anchor_root_trust = "honoured"
    else:
        report.engine_guard_anchor_root_trust = (
            "ABSENT: this container's engine clone matches registry trust anchors by "
            "strict descendant only, so the install orchestrator will refuse the flat "
            f"served mirror at {mirror_root} even though repos.doe_claude names it "
            "exactly. Not fixable by any key this script writes — the engine needs a "
            "guard carrying the anchor-root equality arm published to it."
        )

    authoring = locate_doe_authoring_tree()
    if authoring is None:
        report.doe_authoring_tree = None
    elif not engine_guard_honours_plugin_mirror_anchor():
        report.doe_authoring_tree = (
            f"{authoring} (found, NOT registered: this container's engine clone has no "
            f"{PLUGIN_MIRROR_LIVE_PATH_KEY} trust anchor, so repos.doe_claude is left "
            "naming the served mirror — the mirror's last anchor on that engine)"
        )
    elif "repos.doe_claude" in targets:
        report.doe_authoring_tree = str(authoring)
        targets["repos.doe_claude"] = (str(authoring), True)

    for key, (value, fatal) in targets.items():
        _write(key, value, fatal=fatal)

    if failures:
        raise RuntimeError(
            "trust anchor keys not registered: "
            + ", ".join(failures)
            + " — the install orchestrator will refuse its own plugin root"
        )


def pin_session_path(report: Report) -> None:
    """Pin the composed PATH into `settings.json`'s `env` block.

    The cloud dialog's env-var box REPLACES PATH, expands nothing, and is an
    operator must-remember. A box left carrying its template placeholder
    (`<npm global bin dir>:$PATH`) is stored verbatim, so a session starts with
    no real directory on PATH: `python3` stops resolving, every coordinator hook
    fails open, and the session still boots and still loads its plugin. The
    running session's PATH is not observable from here (the box does not reach
    this process), so this does not detect the broken value — it makes the box
    unable to be the only rung that decides.

    `settings.json`'s `env` is the one PATH rung this script can reach, and it
    is the same surface the auto-compact window is pinned through for the same
    stated reason: discharging an operator must-remember is what this script is
    for. `resolve_session_path` composes the value off the image (image default
    plus the directories the session-critical binaries actually live in); this
    writes it. Recorded on `report.session_path_pin`, NOT on `report.session_path`,
    which `_record_session_surfaces_best_effort` re-derives afterwards and would
    overwrite.

    Merge, never clobber — same read-patch-replace as `register_plugin_settings`.
    """
    resolve_session_path(report)
    value = (report.session_path or {}).get("env_box_value")
    settings_path = _claude_home() / "settings.json"
    if not value:
        report.session_path_pin = {
            "settings_path": str(settings_path),
            "pinned": False,
            "value": None,
            "reason": "no PATH value could be composed off this image",
        }
        raise RuntimeError("no PATH value could be composed off this image")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception as e:
            raise ValueError(f"existing settings.json is not valid JSON: {e}") from e
    else:
        settings = {}
    settings.setdefault("env", {})["PATH"] = value
    tmp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2), newline="\n")
    tmp_path.replace(settings_path)

    # Read back off disk, not asserted from the dict just written — the same
    # rule `verify_plugin_settings` exists to enforce.
    landed = None
    try:
        landed = json.loads(settings_path.read_text()).get("env", {}).get("PATH")
    except Exception:  # noqa: BLE001 - a read-back failure is a recorded verdict
        pass
    report.session_path_pin = {
        "settings_path": str(settings_path),
        "pinned": landed == value,
        "value": value,
        "on_disk": landed,
    }


def assert_hook_plane_armed(report: Report) -> None:
    """Assert, against disk, that a session launched here will actually run hooks.

    The class of failure this closes: three surfaces fail open in the same
    direction — the install orchestrator's probes degrade to `skip`, the
    autofire hooks' bootstrap treats a missing script as silence rather than
    error, and this script exits 0 whatever its verdicts. Composed, a container
    that wired NOTHING behaves identically to one that wired everything, right
    up until an agent notices a ceremony running with no engine-minted run-id.
    Nothing gated on that. This does.

    Two facts, all read off disk:

    - At least one hook-delivery surface the runtime consults registers hooks:
      `settings.json`'s own `hooks` block, OR the coordinator plugin's
      `hooks/hooks.json` (see `_plugin_hook_delivery`). The plugin surface is
      not a fallback, it is the NORMAL case (claude-klabauter#28). Double-fired
      entries are `drop_double_fired_settings_hooks`'s job, run after every
      writer and just before this; it either removes them or raises, so this
      assert does not re-detect them.
    - `.doe-root` resolves through at least one rung the no-launcher fences read.
      At least one, deliberately NOT all: `<settings-home>/machine-local/.doe-root`
      is the canonical target and `~/.claude/.doe-root` is a legacy fallback that
      `gen-doe-root-pointer` refuses to dual-write, so requiring both would
      encode a clobber that ruling exists to prevent. Each rung's outcome is
      recorded so a reader sees which one answered.

    RAISES on either failure, so the verdict surface names it and a session
    reads it as context. There is nothing this script can do to repair a hook
    plane the orchestrator did not wire; being loud is the whole remit.
    """
    settings_path = _claude_home() / "settings.json"
    settings: dict = {}
    hook_event_count = 0
    read_error = None
    try:
        loaded = json.loads(settings_path.read_text())
        settings = loaded if isinstance(loaded, dict) else {}
        hooks = settings.get("hooks") or {}
        hook_event_count = len(hooks) if isinstance(hooks, (dict, list)) else 0
    except Exception as e:  # noqa: BLE001 - an unreadable settings file is a verdict
        read_error = f"{type(e).__name__}: {e}"
    plugin = _plugin_hook_delivery(settings)
    plugin.pop("_delivered", None)
    settings_armed = hook_event_count > 0
    if settings_armed and plugin["armed"]:
        delivery = "both"
    elif settings_armed:
        delivery = "settings"
    elif plugin["armed"]:
        delivery = "plugin"
    else:
        delivery = "none"
    hooks_present = delivery != "none"

    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or ""
    rungs: dict[str, str | None] = {}
    if settings_home:
        rungs["registry repos.doe_claude"] = _registry_value_or_none(
            Path(settings_home) / "machine-local", "repos.doe_claude"
        )
        rungs[f"{settings_home}/machine-local/.doe-root"] = _first_line_or_none(
            Path(settings_home) / "machine-local" / ".doe-root"
        )
    else:
        rungs["registry repos.doe_claude"] = None
        rungs["<settings-home>/machine-local/.doe-root"] = None
    legacy = _claude_home() / ".doe-root"
    rungs[str(legacy)] = _first_line_or_none(legacy)
    doe_root_resolves = any(value for value in rungs.values())

    report.hook_plane = {
        "settings_path": str(settings_path),
        "hooks_registered": hooks_present,
        "hook_delivery": delivery,
        "hook_event_count": hook_event_count,
        "settings_read_error": read_error,
        "plugin_hooks": plugin,
        "doe_root_resolves": doe_root_resolves,
        "doe_root_rungs": rungs,
    }

    problems: list[str] = []
    if not hooks_present:
        problems.append(
            f"`hooks` in {settings_path} is empty"
            + (f" ({read_error})" if read_error else "")
            + f" and the coordinator plugin delivers none ({plugin['reason']})"
        )
    if not doe_root_resolves:
        problems.append("`.doe-root` resolves through no rung the no-launcher fences read")
    if problems:
        raise RuntimeError("; ".join(problems))


def _plugin_hook_delivery(settings: dict) -> dict:
    """Whether the coordinator plugin's own hook manifest will deliver hooks to a
    session launched here, read off disk the way the runtime reads it.

    Three links, each one the runtime actually walks: the plugin is ENABLED in
    `settings.json`'s `enabledPlugins`; `installed_plugins.json` records an
    `installPath` for it (what `${CLAUDE_PLUGIN_ROOT}` expands to); and
    `<installPath>/hooks/hooks.json` registers at least one event, with every
    `${CLAUDE_PLUGIN_ROOT}`-relative file it names present. That last check is
    what separates delivered from merely declared: each entry's bootstrap
    fails OPEN on a missing script, so a manifest pointing at absent files is
    exactly as silent as no manifest.

    Re-implemented rather than importing the engine's
    `detect_hook_delivery_duplication`, for `_claude_home`'s reason: this module
    is dependency-free by design and must not import `coordinator_core`.

    Never raises: every broken link is a recorded `reason`, `armed` False.
    Also carries, under `_delivered` (a dict of sets, popped by
    `drop_double_fired_settings_hooks` and never left on a value stored into the
    JSON report), the per-event script identities the manifest actually
    delivers. A script identity counts as delivered only when EVERY token
    naming it is `${CLAUDE_PLUGIN_ROOT}`-prefixed — the same convention
    `missing_files` existence-checks — so a hook this function never verified
    exists can't stand in as the "still delivered" copy that licenses removing
    the `settings.json` side (code-reviewer F1).
    """
    result: dict = {
        "armed": False,
        "event_count": 0,
        "missing_files": [],
        "reason": "",
        "_delivered": {},
    }
    delivered: dict[str, set[str]] = result["_delivered"]
    try:
        key = _plugin_record_key(Path(CLONES["coordinator-claude"]["dest"]))
    except Exception as e:  # noqa: BLE001 - an unreadable clone manifest is a recorded miss
        result["reason"] = f"plugin key unreadable: {type(e).__name__}: {e}"
        return result
    result["key"] = key
    enabled = settings.get("enabledPlugins")
    if not (isinstance(enabled, dict) and enabled.get(key) is True):
        result["reason"] = f"{key} is not enabled in settings.json"
        return result
    try:
        records = json.loads(
            _claude_home().joinpath(*PLUGIN_RECORD_REL).read_text(encoding="utf-8")
        )["plugins"][key]
        if not isinstance(records, list):
            raise TypeError(f"expected a list of plugin records, got {type(records).__name__}")
        install_path = next(
            r["installPath"] for r in records if isinstance(r, dict) and r.get("installPath")
        )
    except Exception as e:  # noqa: BLE001 - an absent record is a recorded miss
        result["reason"] = f"no installed-plugin record names an installPath for {key} ({type(e).__name__}: {e})"
        return result
    result["install_path"] = install_path
    manifest = Path(install_path).joinpath(*PLUGIN_HOOKS_REL)
    try:
        hooks = json.loads(manifest.read_text(encoding="utf-8")).get("hooks")
    except Exception as e:  # noqa: BLE001 - an unreadable manifest is a recorded miss
        result["reason"] = f"{manifest} unreadable: {type(e).__name__}"
        return result
    if not isinstance(hooks, dict) or not hooks:
        result["reason"] = f"{manifest} registers no hook event"
        return result
    result["event_count"] = len(hooks)
    referenced: set[str] = set()
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                if not isinstance(hook, dict):
                    continue
                if hook.get("type") == "http":
                    url = hook.get("url")
                    if isinstance(url, str) and url:
                        delivered.setdefault(event, set()).add(f"url:{url}")
                    continue
                args = hook.get("args") if isinstance(hook.get("args"), list) else []
                ids: set[str] = set()
                all_prefixed = True
                for token in [hook.get("command"), *args]:
                    if not isinstance(token, str):
                        continue
                    for piece in _shlex_pieces(token):
                        normalized = piece.replace("\\", "/")
                        prefixed = normalized.startswith("${CLAUDE_PLUGIN_ROOT}/")
                        if prefixed:
                            referenced.add(normalized[len("${CLAUDE_PLUGIN_ROOT}/"):])
                        tail = _script_tail(normalized)
                        if tail:
                            ids.add(tail)
                            all_prefixed = all_prefixed and prefixed
                if ids and all_prefixed:
                    delivered.setdefault(event, set()).update(ids)
    missing = sorted(rel for rel in referenced if not Path(install_path, rel).is_file())
    result["missing_files"] = missing
    if missing:
        result["reason"] = f"{manifest} names {len(missing)} absent file(s), e.g. {missing[0]}"
        return result
    result["armed"] = True
    result["reason"] = f"{key} delivers {len(hooks)} hook event(s) from {manifest}"
    return result


def _shlex_pieces(token: str) -> list[str]:
    """`token` split on whitespace the way a shell would, so a quoted path
    containing a space keeps its full tail instead of being cut at the space
    (code-reviewer F2). Falls back to a plain whitespace split on anything
    `shlex` can't parse (an unbalanced quote) rather than raising."""
    try:
        return shlex.split(token, posix=True)
    except ValueError:
        return token.split()


def _hook_identities(hook: dict) -> set[str]:
    """What a hook entry actually runs, independent of which surface registers it:
    `url:<url>` for an http hook, else the `<dir>/<file>` tail of every script it
    names. The tail, not the full path, because the two surfaces spell the root
    differently — `${CLAUDE_PLUGIN_ROOT}` in the manifest, an env-var expression
    or a baked path in `settings.json` — the same reason the engine's
    `guard_settings_integrity._tail_key` compares tails.
    """
    if hook.get("type") == "http":
        url = hook.get("url")
        return {f"url:{url}"} if isinstance(url, str) and url else set()
    args = hook.get("args") if isinstance(hook.get("args"), list) else []
    identities: set[str] = set()
    for token in [hook.get("command"), *args]:
        if not isinstance(token, str):
            continue
        for piece in _shlex_pieces(token):
            tail = _script_tail(piece.replace("\\", "/"))
            if tail:
                identities.add(tail)
    return identities


def drop_double_fired_settings_hooks(report: Report) -> None:
    """Remove from `settings.json` every hook the coordinator plugin already
    delivers on the same event, so no hook fires twice.

    How a container gets here: the engine's `gen_settings_hooks.generate` writes
    `settings.json` hooks only when plugin-side delivery is NOT verified live at
    the moment it runs, and never removes a block it wrote earlier. Plugin
    delivery can become live after that — `register_live_plugin_record` runs
    after the orchestrator — and the stale block then doubles every hook it
    names: two guard verdicts, two SessionStart injections, two writes of every
    side effect.

    Removes only when the plugin is verified armed (`_plugin_hook_delivery`),
    so a removed entry is always still delivered — never the last copy. Only
    exact duplicates go; every other entry, group, field and event is kept in
    the same walk, and a `hooks` block emptied by the removal is dropped.
    Runs once, after every writer has finished, immediately before
    `assert_hook_plane_armed`. A failed write raises, which `run_step` records.
    """
    settings_path = _claude_home() / "settings.json"
    try:
        settings = json.loads(settings_path.read_text())
    except FileNotFoundError:
        report.hook_dedupe = {"settings_path": str(settings_path), "removed": []}
        return
    if not isinstance(settings, dict):
        raise ValueError(f"{settings_path} is not a JSON object")
    plugin = _plugin_hook_delivery(settings)
    delivered: dict[str, set[str]] = plugin.pop("_delivered", {})
    removed: list[str] = []
    if plugin["armed"] and isinstance(settings.get("hooks"), dict):
        kept_events: dict = {}
        for event, groups in settings["hooks"].items():
            kept_groups = []
            for group in groups if isinstance(groups, list) else []:
                if not isinstance(group, dict):
                    kept_groups.append(group)
                    continue
                kept_hooks = []
                for hook in group.get("hooks", []):
                    ids = _hook_identities(hook) if isinstance(hook, dict) else set()
                    if ids and ids <= delivered.get(event, set()):
                        removed.append(f"{event}: {', '.join(sorted(ids))}")
                        continue
                    kept_hooks.append(hook)
                if kept_hooks:
                    kept_groups.append({**group, "hooks": kept_hooks})
            if kept_groups:
                kept_events[event] = kept_groups
        if removed:
            if kept_events:
                settings["hooks"] = kept_events
            else:
                del settings["hooks"]
    report.hook_dedupe = {"settings_path": str(settings_path), "removed": removed}
    if not removed:
        return
    tmp = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2), newline="\n")
    tmp.replace(settings_path)
    _safe_print(f"[cloud_setup] removed {len(removed)} double-fired hook(s) from {settings_path}")


def _registry_value_or_none(machine_local_dir: Path, key: str) -> str | None:
    """One registry key's value, read with tomllib and no subprocess.

    A flat `"<key>" = '<value>'` read, not a general TOML flattener: this script
    is dependency-free by design (`coordinator_core` is not importable from the
    ambient interpreter at the point this runs) and the keys it asks about are
    written by `machine-local set` in exactly that quoted-dotted-key form.

    `tomllib` is imported INSIDE the try, not at the top: it is stdlib only from
    3.11, and an ImportError escaping here would make `assert_hook_plane_armed`
    report an unresolvable `.doe-root` on a box whose registry is perfectly
    fine — a false alarm on the one surface that exists to be believed. An
    interpreter too old to read the registry means this rung says nothing, and
    the two file rungs still answer.
    """
    for fname in ("registry.local.toml", "registry.toml"):
        try:
            import tomllib

            data = tomllib.loads((machine_local_dir / fname).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - absent, unreadable, unparseable, or no tomllib
            continue
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_line_or_none(path: Path) -> str | None:
    """A pointer file's single line, or None when absent, unreadable, or blank."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def run_claude_klabauter_setup(report: Report) -> None:
    """Run scripts/setup.py, passing the DR-411 container opt-in.

    Argv: --i-am-agent (suppress prompts), --coordinator-root <CLONES coordinator-claude
    dest>, --i-assert-no-other-consumer (the DR-411 opt-in; cloud_setup.py is the only
    caller permitted to pass it), --with-test-deps (a cloud session's agents run the
    documented test tiers, whose `-n`/`--timeout` flags exit 4 without the test extra's
    plugins; setup.py derives that set from pyproject, so nothing is re-declared here).
    No PIP_BREAK_SYSTEM_PACKAGES or any pip env var is set — the flag rides in argv only.
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
        "--with-test-deps",
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
    # stderr is FOLDED INTO stdout, for the same reason `run_rag_install` folds
    # it: the raise below is what reaches the durable JSON report, and
    # interpolating stderr alone discards the evidence on exactly the runs that
    # need it. Exit 94 is the case that proves it -- that code means a
    # hard-severity health probe reported `fail`, and WHICH probe is printed by
    # `run_health_probe`'s per-probe loop on STDOUT. Captured beside stderr,
    # that loop's output went only to a console nobody reads on a snapshotted
    # VM, so the report recorded a 94 whose cause was absent from it: a verdict
    # naming no defect, which reads as an unattributable installer failure. The
    # combined stream keeps cause and exit code in the same record.
    result = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
        # Inlined the former
        # `_output_tail` helper (single call site). The report stores
        # `step.detail` untruncated, but a whole install log per failed step
        # would bury the verdict it exists to deliver, so the raise carries
        # only the last 40 lines, marked when truncated.
        all_lines = (result.stdout or "").rstrip().splitlines()
        tail_lines = all_lines if len(all_lines) <= 40 else ["... (earlier output omitted)", *all_lines[-40:]]
        raise RuntimeError(
            f"scripts/setup.py exited {result.returncode}"
            + _hard_probe_failure_summary(result.stdout)
            + "\n--- combined output (tail) ---\n"
            + "\n".join(tail_lines)
        )


def _hard_probe_failure_summary(output: str) -> str:
    """Name the hard-severity probes that reported `fail`, for the FIRST line of
    the raise.

    Exit 94 means a hard-severity post-install health probe failed, and which
    one is the only part of that verdict anyone can act on. It reaches here
    because this process runs `setup.py --i-am-agent`, under which
    `run_health_probe` emits the probe suite as raw NDJSON rather than a human
    summary -- so the failing probe is machine-readable at exactly the moment
    the report is being written, and reconstructing it later is impossible: the
    container is snapshotted and the probe is not re-runnable against the state
    that produced the verdict.

    Front-loaded rather than left in the tail because the session-facing rule
    surface (`_verdict_body`) quotes a step's FIRST detail line, capped -- a
    verdict reading only "exited 94" names no defect and reads as an
    unattributable installer failure.

    Silent by design when the output carries no such row: this is an additive
    annotation on a failure that is already being raised, never itself a
    verdict. `inconclusive` is deliberately not collected -- `run_health_probe`
    treats it as "could not measure", not as a broken install, and repeating
    that here would contradict the exit code it is annotating.
    """
    failed: list[str] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("severity") == "hard" and row.get("status") == "fail":
            failed.append(str(row.get("name") or "<unnamed probe>"))
    if not failed:
        return ""
    return " — hard health probe(s) failed: " + ", ".join(failed)


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

    # The refusal only has
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
        # `Path.home()` alone ignores `HOME` on Windows (it reads
        # `USERPROFILE`), contradicting the docstring's stated resolution
        # order. Read `HOME` explicitly first, falling through to
        # `Path.home()` only when unset, so the order is CLAUDE_HOME -> HOME
        # -> platform home on every platform, not just POSIX.
        base = os.environ.get("HOME") or Path.home()
    return Path(base) / ".claude"


def register_plugin_settings() -> None:
    """Write `$CLAUDE_HOME/settings.json` (or `$HOME/.claude/settings.json`)
    so the coordinator plugin is registered before Claude Code launches in
    this cloud VM.

    Modelled on DoE-claude's `coordinator/templates/cloud-env/setup.sh`
    phase 3 (lines 125-161): a `directory` marketplace source pointing at the
    already-cloned `coordinator-claude` checkout, plus `enabledPlugins`, plus
    `env.COORDINATOR_PROBE_CANARY`, plus the engine root and settings home the
    install used (`_engine_env`).

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

    ALSO PINS THE AUTO-COMPACT WINDOW, through both rungs the binary resolves
    it from. See `CLOUD_AUTO_COMPACT_WINDOW_TOKENS` for the value and why cloud
    wants an earlier cut than a desktop does. Both writes derive from that one
    constant: `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` (the highest-precedence
    rung, which nothing resolved later can outrank) and the top-level
    `autoCompactWindow` setting (what `/autocompact` and the context UI read
    back). Writing it HERE rather than into the cloud dialog's env-var box is
    the point: the box is an operator must-remember, and discharging those is
    what this script is for.

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
    env_block = settings.setdefault("env", {})
    env_block["COORDINATOR_PROBE_CANARY"] = "1"
    # The install lands in a non-default settings home, and this process' own
    # env dies with it. Unpinned, a session resolves ~/.coordinator-claude-settings,
    # finds no launchers, and every fail-open hook (mise/pickup autofire) silently
    # no-ops (claude-klabauter#15).
    env_block.update(_engine_env())
    # Assigned, not defaulted: a stale value from an earlier run (or from an
    # image that seeded a different one) must be brought to the value this
    # script determined, or a re-run would report a window it is not pinning.
    env_block[AUTO_COMPACT_WINDOW_ENV] = str(CLOUD_AUTO_COMPACT_WINDOW_TOKENS)
    settings[AUTO_COMPACT_WINDOW_SETTING] = CLOUD_AUTO_COMPACT_WINDOW_TOKENS

    tmp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2), newline="\n")
    tmp_path.replace(settings_path)


#: The coordinator plugin's settings-env checker, relative to its clone. Its
#: `_SPEC` is the executable form of the settings manifest's env table (DoE pins
#: the two together), so running it keeps the cloud on that single source: a new
#: all-machines row reaches cloud with no change here.
SETTINGS_ENV_CHECKER_REL = ("bin", "check-settings-env.py")


def apply_settings_manifest_env(report: Report) -> None:
    """Apply the settings manifest's all-machines env values to the
    `settings.json` this run wrote, and record the checker's verdict.

    The values are not written here by name, deliberately — see
    `SETTINGS_ENV_CHECKER_REL`. `--apply` writes only `all_machines` rows, so a
    machine-specific row is reported, never forced. Unapplied values each gate a
    tool out of the session silently (agent teams, the task tools), so any
    finding left after the apply pass raises, and `run_step` carries it to the
    session verdict rule.
    """
    checker = Path(CLONES["coordinator-claude"]["dest"]).joinpath(*SETTINGS_ENV_CHECKER_REL)
    if not checker.is_file():
        raise FileNotFoundError(f"settings-env checker not found at {checker}")
    settings_path = _claude_home() / "settings.json"
    result = subprocess.run(
        [sys.executable, str(checker), "--settings", str(settings_path), "--apply", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        verdict = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        verdict = None
    if not isinstance(verdict, dict):
        report.settings_env = {"exit_code": result.returncode, "applied": None, "findings": None}
        raise RuntimeError(
            f"check-settings-env exited {result.returncode} with no JSON verdict: "
            + ((result.stderr or result.stdout or "").strip()[-400:] or "<no output>")
        )
    findings = verdict.get("findings") or []
    report.settings_env = {
        "exit_code": result.returncode,
        "applied": verdict.get("applied") or [],
        "findings": findings,
    }
    if result.returncode != 0:
        named = ", ".join(f"{f.get('var')} ({f.get('kind')})" for f in findings) or "<none named>"
        raise RuntimeError(f"check-settings-env exited {result.returncode}; unapplied: {named}")


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
        "auto_compact_window_env": None,
        "auto_compact_window_setting": None,
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
    result["auto_compact_window_env"] = settings.get("env", {}).get(AUTO_COMPACT_WINDOW_ENV)
    result["auto_compact_window_setting"] = settings.get(AUTO_COMPACT_WINDOW_SETTING)
    report.plugin_settings = result
    # Recorded as its own top-level fact, not only as two settings-file
    # readings: the window is a determination this run made, and an operator
    # asking "what will this container compact at?" should not have to know
    # which rungs it was written through to find the answer.
    report.auto_compact = {
        "window_tokens": CLOUD_AUTO_COMPACT_WINDOW_TOKENS,
        "env_var": AUTO_COMPACT_WINDOW_ENV,
        "settings_key": AUTO_COMPACT_WINDOW_SETTING,
        "env_rung_on_disk": result["auto_compact_window_env"],
        "settings_rung_on_disk": result["auto_compact_window_setting"],
        # The runner injects CLAUDE_AUTOCOMPACT_PCT_OVERRIDE into the session's
        # environment, which this process cannot read (fact 2). The stacking is
        # stated, not measured: at the 80 the runner currently injects, the cut
        # lands at ~0.8 x the window.
        "pct_override_note": (
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE lowers the firing threshold within "
            "this window; it does not set the window. At 80 the cut is ~0.8x."
        ),
    }


def _plugin_record_key(plugin_root: Path) -> str:
    """``<plugin>@<marketplace>``, both halves READ from the clone.

    Neither half is spelled as a literal here. The record's key is what the
    platform matches an enabled plugin against, and a hardcoded pair survives
    exactly until either the plugin or the marketplace is renamed — after which
    this writes a record keyed to a name nothing looks up, while every step
    still reports OK. That is the same failure this whole pair of functions
    exists to make loud, so it must not be reintroduced by the fix.
    """
    manifest = plugin_root.joinpath(*PLUGIN_MANIFEST_REL)
    marketplace = plugin_root.joinpath(*MARKETPLACE_MANIFEST_REL)
    plugin_name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    marketplace_name = json.loads(marketplace.read_text(encoding="utf-8")).get("name")
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ValueError(f"{manifest} declares no usable plugin name")
    if not isinstance(marketplace_name, str) or not marketplace_name:
        raise ValueError(f"{marketplace} declares no usable marketplace name")
    return f"{plugin_name}@{marketplace_name}"


def register_live_plugin_record() -> None:
    """Seed `<claude_home>/plugins/installed_plugins.json` with a record whose
    ``installPath`` IS the coordinator clone.

    WHY THIS EXISTS, measured on a real cloud container (2026-09-17): a box
    whose only registration was `settings.json`'s marketplace + `enabledPlugins`
    came up with an installed record naming
    ``<claude_home>/plugins/cache/<marketplace>/<plugin>/<version>`` and a
    pinned ``gitCommitSha`` — a path that had never been created, under a
    ``plugins/cache`` tree that did not exist at all. `${CLAUDE_PLUGIN_ROOT}`
    therefore expanded to empty in every `hooks.json` entry and every
    coordinator hook died on a missing bootstrap, while `settings.json`, the
    marketplace record and this script's own verdicts all read healthy. The
    plugin's content was present and complete the whole time.

    The SHAPE is not invented here. `coordinator_core/install/live_plugin_
    registration.py` already established it for the desktop path — installPath
    at the clone, no ``gitCommitSha``, so nothing is copied and nothing can go
    stale — and this box's `example-retrieval-repo@example-retrieval-repo` record has exactly that
    shape and resolved correctly through the same launch that invented the
    broken coordinator one. What that module CANNOT do is help here: it repairs
    an existing record and reports ``absent`` when there is none, and at
    pre-boot there is none — the platform writes it later, at launch. So the
    correct move for the cloud entrypoint is to write the record FIRST, which
    is what this does.

    Re-implemented rather than imported, for `_claude_home`'s reason: this
    module is dependency-free by design and must not import `coordinator_core`.
    If that module's record shape changes, change it here too.

    Idempotent: an existing record already naming the clone with no pinned SHA
    is left alone; any other record for this key is repointed rather than
    appended beside, because two records for one key is how a stale path
    survives a fix. Other plugins' records are never touched.
    """
    plugin_root = Path(CLONES["coordinator-claude"]["dest"])
    key = _plugin_record_key(plugin_root)
    record_path = _claude_home().joinpath(*PLUGIN_RECORD_REL)

    data: dict = {}
    if record_path.exists():
        try:
            loaded = json.loads(record_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"existing installed_plugins.json is not valid JSON: {e}") from e
        if isinstance(loaded, dict):
            data = loaded
    data.setdefault("version", 2)
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    data["plugins"] = plugins

    live = str(plugin_root)
    existing = plugins.get(key)
    records = [r for r in existing if isinstance(r, dict)] if isinstance(existing, list) else []
    if not records:
        records = [{"scope": "user"}]
    for record in records:
        record["installPath"] = live
        # Dropped, never rewritten: a SHA pins the record to a commit the
        # session is not running, which is a manifest that is a false witness.
        record.pop("gitCommitSha", None)
    plugins[key] = records

    record_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(record_path)
    print(f"[cloud_setup] plugin record: {key} -> {live}")


def _resolve_marketplace_plugin_source(marketplace_path: Path, plugin_name: str) -> str | None:
    """The relative `source` string a directory marketplace's `plugin.json`-less
    root declares for `plugin_name`, or None when the marketplace manifest is
    unreadable, names no such plugin, or names it via a non-relative `source`
    (a `github`/`url` object — not this process's to resolve).

    `marketplace.json`'s plugin list entries carry `name` and `source`; only a
    plain relative-path `source` (e.g. ``"./plugin"``) is ever returned here.
    """
    try:
        data = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    entries = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for candidate in entries:
        if isinstance(candidate, dict) and candidate.get("name") == plugin_name:
            source = candidate.get("source")
            return source if isinstance(source, str) and source else None
    return None


def verify_plugin_install_path(report: Report) -> None:
    """Read the installed-plugin record back OFF DISK and check that the path it
    names actually resolves to a plugin root.

    THIS IS THE STEP THE LESSON IS IN, and it is worth more than the write above.
    A plugin whose install path does not resolve disables the ENTIRE hook plane,
    and it does so silently: hooks fail open, so their absence produces no
    output at all, and nothing that runs inside a session can report it —
    a hook cannot report that hooks are broken. So the check runs here, pre-boot,
    and its failure lands on `<claude_home>/rules/`, which the harness reads with
    no interpreter and which survives a dead hook plane (see
    `write_session_verdict`).

    Never raises: a miss is a RECORDED verdict. Presence of the directory is not
    accepted on its own — the plugin manifest must be readable under it, because
    an empty directory at the right path resolves and serves nothing.

    Each unresolvable entry carries a `reason` naming WHICH of the two probes
    failed and on WHAT path — `installPath` itself missing/not-a-directory, or
    `installPath` present but its `.claude-plugin/plugin.json` (`manifest_path`)
    missing or unreadable — rather than a bare "not resolvable" that a reader
    cannot act on when the registered path visibly exists on disk
    (claude-klabauter#26 (2)).

    ROOT CAUSE, confirmed against the real `example-retrieval-repo` checkout: `installPath`
    can name a directory MARKETPLACE, not a plugin root directly — it carries
    `.claude-plugin/marketplace.json` but no `.claude-plugin/plugin.json` of its
    own. `example-retrieval-repo`'s marketplace entry for the `example-retrieval-repo` plugin has
    `"source": "./plugin"`, and the real manifest sits at
    `<installPath>/plugin/.claude-plugin/plugin.json`. When the direct-root
    manifest probe misses AND `installPath` carries a marketplace manifest,
    `_resolve_marketplace_plugin_source` looks up the plugin (the part of `key`
    before `@`) in that marketplace's plugin list and re-probes the manifest
    under its relative `source`. A `source` that is not a plain relative string
    (a `github`/`url` object) is left alone — that shape names no local path
    this process can resolve, so it falls through to the ordinary
    manifest-missing reason.
    """
    record_path = _claude_home().joinpath(*PLUGIN_RECORD_REL)
    result: dict = {
        "record_path": str(record_path),
        "expected_root": CLONES["coordinator-claude"]["dest"],
        "entries": [],
        "resolves": False,
    }
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - an absent/unreadable record is a recorded miss
        result["read_error"] = f"{type(e).__name__}: {e}"
        report.plugin_install_path = result
        return

    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        result["read_error"] = "record carries no 'plugins' object"
        report.plugin_install_path = result
        return

    for key, records in plugins.items():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            path = record.get("installPath")
            entry = {
                "key": key,
                "install_path": path,
                "path_exists": False,
                "manifest_readable": False,
                "pinned_sha": record.get("gitCommitSha"),
            }
            if isinstance(path, str) and path:
                root = Path(path)
                manifest_path = root.joinpath(*PLUGIN_MANIFEST_REL)
                entry["manifest_path"] = str(manifest_path)
                entry["path_exists"] = root.is_dir()
                try:
                    manifest_path.read_bytes()
                    entry["manifest_readable"] = True
                except OSError:
                    pass

                if entry["path_exists"] and not entry["manifest_readable"]:
                    marketplace_path = root.joinpath(*MARKETPLACE_MANIFEST_REL)
                    plugin_name = key.split("@", 1)[0]
                    source = (
                        _resolve_marketplace_plugin_source(marketplace_path, plugin_name)
                        if marketplace_path.is_file()
                        else None
                    )
                    if source:
                        sub_manifest = root.joinpath(source, *PLUGIN_MANIFEST_REL)
                        entry["marketplace_source"] = source
                        entry["manifest_path"] = str(sub_manifest)
                        try:
                            sub_manifest.read_bytes()
                            entry["manifest_readable"] = True
                        except OSError:
                            pass
                        manifest_path = sub_manifest

                if not entry["path_exists"]:
                    entry["reason"] = f"installPath {path!r} is not a directory (or does not exist)"
                elif not entry["manifest_readable"]:
                    if entry.get("marketplace_source"):
                        entry["reason"] = (
                            f"installPath {path!r} is a directory marketplace; its plugin "
                            f"{plugin_name!r} names source {entry['marketplace_source']!r}, but "
                            f"no manifest is readable at {manifest_path}"
                        )
                    else:
                        entry["reason"] = (
                            f"installPath {path!r} exists, but its plugin manifest is missing or "
                            f"unreadable at {manifest_path}"
                        )
            else:
                entry["reason"] = "no installPath was recorded for this entry"
            result["entries"].append(entry)

    result["unresolvable"] = [
        entry["key"]
        for entry in result["entries"]
        if not (entry["path_exists"] and entry["manifest_readable"])
    ]
    result["resolves"] = bool(result["entries"]) and not result["unresolvable"]
    report.plugin_install_path = result
    verdict = "OK" if result["resolves"] else "UNRESOLVABLE"
    _safe_print(
        f"[cloud_setup] plugin install paths: {verdict} "
        f"({len(result['entries'])} record(s), unresolvable: {result['unresolvable']})"
    )


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
        # is_file() does not
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
        # No per-file
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
    if name in CLONES:
        candidates = [Path(CLONES[name]["dest"])]
    else:
        candidates = [root / name for root in retrieval_search_roots()]
    for cand in candidates:
        if cand.is_dir() and (cand / ".git").exists():
            return cand
    return None


def locate_doe_authoring_tree() -> Path | None:
    """The mounted DoE-claude AUTHORING checkout, or None.

    Detected by the `.coordinator-dev-repo` sentinel at a checkout's root — the
    discriminant DoE-claude's `CLAUDE.md` names as fleet-wide — never by a
    repository name and never by a hardcoded path. A pure consumer container has
    no such tree, which is a SUPPORTED shape and returns None rather than a
    failure.

    This script's own coordinator clone is excluded explicitly: it is the flat
    published mirror, the thing an authoring tree is being distinguished FROM,
    and it will never carry the sentinel.
    """
    own_clones = {Path(c["dest"]).as_posix() for c in CLONES.values()}
    for root in retrieval_search_roots():
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for cand in children:
            if cand.as_posix() in own_clones:
                continue
            if not cand.is_dir():
                continue
            if (cand / DEV_REPO_SENTINEL).exists() and (cand / ".git").exists():
                return cand
    return None


def _engine_guard_source() -> str:
    """The cloned engine's `trusted_root_guard.py` source text, or "" if unreadable.

    Both capability probes below read the SOURCE rather than importing and calling
    `is_trusted`: the answer is a property of the engine version this container
    happened to clone, and importing a sibling repo's module into this process to
    ask about its own trust boundary is a wider coupling than a substring read
    needs to be.
    """
    guard = Path(CLONES["klabauter"]["dest"]) / "coordinator_core" / "trusted_root_guard.py"
    try:
        return guard.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def engine_guard_honours_plugin_mirror_anchor() -> bool:
    """Whether THIS container's engine clone trusts a root by the served-mirror
    key, rather than only by `repos.doe_claude`.

    An older engine has only the authoring key as its coordinator anchor, so
    moving `repos.doe_claude` off the mirror there strips the mirror of its last
    anchor. The key split is therefore gated on this capability.

    Negative-spec — this is NOT the capability that decides whether the
    orchestrator accepts the served mirror, and gating on it does not make the
    pre-split arrangement work. That is
    `engine_guard_trusts_anchor_root_itself`: the mirror is a FLAT checkout whose
    plugin root IS the anchor path, and an engine matching strict descendants only
    refuses it however the keys are arranged. A reader who takes the gated
    fallback for a working shape re-derives exactly the abort this module
    documents.
    """
    return PLUGIN_MIRROR_LIVE_PATH_KEY in _engine_guard_source()


def engine_guard_trusts_anchor_root_itself() -> bool:
    """Whether THIS container's engine clone trusts a registry anchor's OWN root,
    not merely its strict descendants.

    This is the capability the install orchestrator's Phase-3 trusted-prefix gate
    actually turns on, and the reason it is probed separately from the mirror
    anchor. The served mirror is flat: its plugin root is the anchor path itself,
    spelled identically. An engine whose guard matches `<anchor>/...` only
    false-rejects the very clone the operator registered — no anchor is empty and
    the refusal names none, which is what makes the abort read as a
    misconfiguration rather than an engine version skew.

    Detected by the presence of the guard's `_at_or_under` helper, which is the
    named seam carrying the equality arm. Absence is not remediable from this
    script: every lever here is a path spelling or a registry value, and none of
    them can add an equality arm to a guard that has none. Widening trust to the
    mirror's PARENT, or setting `COORDINATOR_PLUGIN_ROOT_TRUSTED=1`, would both
    clear the gate while masking any genuinely-empty anchor on the next box —
    trading an abort that reports itself for a silence that does not. So this is
    recorded as a verdict and left to a publish, deliberately.
    """
    return "_at_or_under" in _engine_guard_source()


def locate_or_clone_repo(name: str, report: Report) -> None:
    """Resolve a rag-half checkout — locate first, clone only if absent.

    Records the resolved path onto ``report.rag_roots[name]`` so every later step
    reads ONE resolved value rather than re-deriving it (and possibly resolving
    differently once the clone exists).
    """
    found = locate_existing_checkout(name)
    if found is None:
        if name not in CLONES:
            # No URL supplied and nothing already mounted. This is a coordinator-
            # only environment, which is a supported shape, not a failure.
            report.rag_roots[name] = None
            print(
                f"[cloud_setup] {name}: skipped — no checkout on disk and "
                f"searched {', '.join(str(r) for r in retrieval_search_roots())}. "
                "Select it as a repository for this environment, or set "
                f"{RETRIEVAL_ROOT_ENV}. The retrieval half will not be installed."
            )
            return
        clone_repo(name)
        found = locate_existing_checkout(name)
    if found is None:
        report.rag_roots[name] = None
        raise RuntimeError(
            f"{name}: no checkout under any of {[str(r) for r in retrieval_search_roots()]} "
            "after the clone step — nothing later in this half can resolve it"
        )
    report.rag_roots[name] = str(found)
    print(f"[cloud_setup] {name}: {found}")


def retrieval_half_skipped(report: Report) -> bool:
    """True when the retrieval half was deliberately not installed.

    Distinguishes "no URL was supplied, so this environment is coordinator-only"
    from "a step failed". The dependent steps read this and skip with a recorded
    verdict rather than raising, because a raise here would fill the report with
    consequential failures for a shape the operator chose.
    """
    return (
        RETRIEVAL_REPO_SLUG in report.rag_roots
        and report.rag_roots.get(RETRIEVAL_REPO_SLUG) is None
    )


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
    ``report.machine_local_keys``, so a partial write is still legible — and a
    partial write is a PASS: the step fails only when nothing landed at all, so
    one unmounted checkout does not bury the keys that were written.

    A key with no resolved checkout is recorded as skipped, not raised: the
    locate-or-clone step already failed loudly for that checkout, and failing
    here too blocked nothing yet read as the registry write itself breaking. Only
    a write that was attempted and refused fails the step.

    The registry lives under COORDINATOR_SETTINGS_HOME, which `set_engine_env`
    put in this process' environment — the child inherits it.
    """
    if retrieval_half_skipped(report):
        print("[cloud_setup] registry keys: skipped — retrieval half not installed.")
        return

    # Seed both verdicts BEFORE the resolver can raise. _machine_local_argv
    # raises when neither rung is available, and the keys were left as {} —
    # indistinguishable in the report from a step that never ran, against a
    # docstring promising each key's verdict individually.
    for _key in MACHINE_LOCAL_REPO_KEYS.values():
        report.machine_local_keys.setdefault(_key, "skipped: no machine-local CLI resolved")
    argv = _machine_local_argv()
    failures: list[str] = []
    landed: list[str] = []
    for name, key in MACHINE_LOCAL_REPO_KEYS.items():
        root = report.rag_roots.get(name)
        if not root:
            report.machine_local_keys[key] = "skipped: no resolved checkout"
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
            landed.append(key)
        else:
            report.machine_local_keys[key] = f"failed (exit {result.returncode}): {result.stderr.strip()}"
            failures.append(f"{key} (exit {result.returncode})")
    # Partial-tolerant: one unresolvable key must not discard the ones that DID
    # land, nor report a registry that is mostly correct as a failed step. An
    # unresolvable retrieval checkout is the ordinary shape of a container that
    # simply did not mount it, and failing the whole step over it buried the
    # keys that were written under a red row nobody could act on.
    if failures and not landed:
        raise RuntimeError("machine-local registry writes failed: " + ", ".join(failures))
    if failures:
        print(
            "[cloud_setup] registry keys: "
            f"{len(landed)} landed, unresolved: {', '.join(failures)}"
        )
def install_hooks_fleet(report: Report) -> None:
    """Install the Session-Id-stamping git hooks into every repo this script
    just registered, and ASSERT the install landed — never trust the
    installer's own rc.

    Vehicle: `<klabauter clone>/coordinator/bin/coordinator-ensure-hooks-fleet`.
    That path is a LITERAL join, not routed through a content-root resolver —
    the published mirror carries both `coordinator/bin` and a flat `bin/` as
    two distinct percolate namespaces and has no `.claude-plugin/plugin.json`,
    so a content-root probe would resolve wrong there; `engine_root.py` owns
    that plane and this join deliberately bypasses it.

    Placement matters: this runs AFTER `register_machine_local_repo_keys`
    (called from `main`, not enforced here) because the fleet installer
    enumerates `repos.*` registry keys — running it before registration
    heals nothing, the exact silent-no-op shape this function exists to
    catch.

    `coordinator-ensure-hooks-fleet` "always exits 0" by contract (its own
    docstring) and its underlying `ensure_hooks_fleet` returns 0 on every
    path including "no registered repos found" — so rc alone cannot
    distinguish an install from a no-op. That is precisely the failure mode
    `state/bug-backlog/2026-08-25-hook-emitters-exit-0-having-installed-no-*.yaml`
    records for the single-repo emitters this fleet script wraps, reproduced
    against a shallow clone under a sandboxed HOME — exactly this container's
    shape. So this step STATs `.git/hooks/prepare-commit-msg` in each repo
    this process itself registered (`TRUST_ANCHOR_KEYS`' destinations) after
    the call, and raises naming which repo/hook is missing, rather than
    reporting the subprocess's own exit code as the verdict.

    Dependency-free by design (module docstring): the vehicle script is
    invoked as a subprocess, its own `git_hook_install` machinery is never
    imported here.
    """
    engine_root = Path(CLONES["klabauter"]["dest"])
    vehicle = engine_root / "coordinator" / "bin" / "coordinator-ensure-hooks-fleet"
    if not vehicle.is_file():
        raise FileNotFoundError(
            f"hooks-fleet vehicle not found at {vehicle} — the klabauter clone "
            "is absent or its layout changed"
        )

    result = subprocess.run(
        ["python3", str(vehicle)],
        capture_output=True,
        text=True,
        timeout=MACHINE_LOCAL_TIMEOUT_S,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    checked: dict[str, bool] = {}
    missing: list[str] = []
    for clone_name in TRUST_ANCHOR_KEYS:
        repo_root = Path(CLONES[clone_name]["dest"])
        hook_path = repo_root / ".git" / "hooks" / "prepare-commit-msg"
        # Presence alone (`is_file()`) is
        # satisfied by a stale, zero-byte, or hand-authored non-executable
        # hook surviving an earlier aborted run; require it be executable too,
        # since git silently skips a non-executable hook at commit time.
        landed = hook_path.is_file() and os.access(hook_path, os.X_OK)
        checked[str(hook_path)] = landed
        if not landed:
            missing.append(f"{clone_name}: {hook_path}")

    report.hooks_fleet = {
        "vehicle": str(vehicle),
        "exit_code": result.returncode,
        "hooks_checked": checked,
    }
    if missing:
        raise RuntimeError(
            "prepare-commit-msg hook missing after coordinator-ensure-hooks-fleet "
            f"(exit {result.returncode}): " + ", ".join(missing)
        )


#: Publish mirrors this script seeds, as `{repo slug: machine-local key}`.
#: A publish mirror is a SEPARATE clone from the engine root: `repos.
#: claude_klabauter` names the deployed engine (`/root/klabauter`, checked out
#: on `main` with a live push remote), and pointing a publish target at it makes
#: a round commit and push straight onto the published mirror's default branch.
#: The two keys are deliberately different keys for that reason, and this step
#: refuses to write the engine root into the publish one.
PUBLISH_MIRROR_KEYS: dict[str, str] = {
    "claude-klabauter": "publish.mirrors.claude_klabauter.path",
}


def register_publish_mirror_keys(report: Report) -> None:
    """Point `publish.mirrors.*` at a mirror checkout when this container has a
    SEPARATE one, and record why when it does not.

    Cloud is the environment where the percolate/publish path breaks first,
    because nothing here seeds these keys: a container gets `repos.
    claude_klabauter` (the engine clone this script makes) and nothing else, so
    the first publish dies on an unset key with no checkout in sight. Where the
    environment ALSO mounted the publish repo — the ordinary shape when an
    operator selects it for the session — that checkout is the right value and
    is registered here, pre-boot, instead of being set by hand in the session
    that hits the failure.

    NEVER the engine clone. `locate_existing_checkout` returns this script's own
    `CLONES` destination first for a name it clones, which for `claude-klabauter`
    IS `/root/klabauter`; writing that into a publish key is the live trap this
    key split exists to prevent, so a candidate equal to the engine clone is
    rejected with a recorded reason rather than registered.

    Additive and non-fatal: a key that cannot be resolved or written is recorded
    on the report and the step returns. Nothing else in the boot depends on it,
    and failing the run over an absent publish mirror would break every container
    that has no reason to publish.
    """
    argv = _machine_local_argv()
    for slug, key in PUBLISH_MIRROR_KEYS.items():
        engine_clone = Path(CLONES[slug]["dest"]).resolve() if slug in CLONES else None
        candidate = None
        for root in retrieval_search_roots():
            cand = root / slug
            if cand.is_dir() and (cand / ".git").exists() and cand.resolve() != engine_clone:
                candidate = cand
                break
        if candidate is None:
            report.machine_local_keys[key] = (
                "skipped: no publish checkout of {0} separate from the engine clone "
                "at {1}".format(slug, engine_clone)
            )
            continue
        result = subprocess.run(
            [*argv, "set", key, str(candidate)],
            capture_output=True,
            text=True,
            timeout=MACHINE_LOCAL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        report.machine_local_keys[key] = (
            str(candidate)
            if result.returncode == 0
            else "failed (exit {0}): {1}".format(result.returncode, result.stderr.strip())
        )


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
    for root in retrieval_search_roots():
        if not root.is_dir():
            continue
        checkouts = [
            cand for cand in sorted(root.iterdir())
            if cand.is_dir() and (cand / ".git").exists()
        ]
        if checkouts:
            break
    if len(checkouts) == 1:
        return str(checkouts[0])
    if len(checkouts) > 1:
        # More than one mount and no way to tell which the operator meant. The
        # earlier version took whichever sorted first, which is a coin toss made
        # silently and then baked into a registration nothing can read back.
        # This fleet routinely mounts six or more. Record the ambiguity and fall
        # back to the one root that is defensible without guessing.
        report.rag_project_root_ambiguity = [c.name for c in checkouts]
        return str(_resolved_root(RETRIEVAL_REPO_SLUG, report))
    return str(_resolved_root(RETRIEVAL_REPO_SLUG, report))


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
    if retrieval_half_skipped(report):
        print("[cloud_setup] cloud install: skipped — retrieval half not installed.")
        return

    rag_root = _resolved_root(RETRIEVAL_REPO_SLUG, report)
    installer = (
        rag_root
        / f"{RETRIEVAL_MODULE_PREFIX}_scripts"
        / f"install_{RETRIEVAL_MODULE_PREFIX}_plugin.py"
    )
    if not installer.is_file():
        raise FileNotFoundError(
            f"{RETRIEVAL_REPO_SLUG} installer not found at {installer} — the checkout is "
            "incomplete or its layout changed"
        )
    project_root = _resolve_rag_project_root(report)
    # sys.executable, not a bare "python3": under an interpreter that is not the
    # first python3 on PATH the installer would resolve a different one than
    # everything around it, and the pre-boot set would land where the rest of
    # the run does not look. _machine_local_argv already does this.
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
            f"{RETRIEVAL_REPO_SLUG} installer exited {result.returncode}; its combined output is above"
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
    config_path = rag_root / f"{RETRIEVAL_MODULE_PREFIX}_mcp" / "http_config.py"
    if not config_path.is_file():
        return None, f"{config_path} not found"
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_cloud_setup_http_config", config_path)
        if spec is None or spec.loader is None:
            return None, f"{config_path} could not be loaded as a module"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        host = getattr(module, f"{RETRIEVAL_MODULE_PREFIX.upper()}_HTTP_HOST")
        port = getattr(module, f"{RETRIEVAL_MODULE_PREFIX.upper()}_HTTP_PORT")
    except Exception as e:  # noqa: BLE001 - an unreadable truth source is a recorded miss
        return None, f"{config_path} did not yield host/port: {type(e).__name__}: {e}"
    return f"http://{host}:{port}/mcp", str(config_path)


#: The retrieval daemon's loopback endpoint. Example-retrieval-repo's own
#: ``example_retrieval_repo_mcp/http_config.py`` is the truth source for the port; this
#: default is what a registration written BEFORE that checkout exists must
#: assume, and `verify_mcp_registration` re-derives from the checkout and
#: records a mismatch once it lands.
RETRIEVAL_MCP_DEFAULT_URL = "http://127.0.0.1:8767/mcp"


def register_retrieval_mcp_entry(report: Report) -> None:
    """Write ``mcpServers."example-retrieval-repo"`` into ``$HOME/.claude.json``, always.

    THIS STEP HAS NO DEPENDENCIES AND MUST NOT ACQUIRE ANY. The entry is a
    pointer to a loopback URL, not a reference to code: nothing about writing it
    requires the repository to be present, cloned, installed, or reachable. That
    matters because the registration is the one half a session CANNOT repair —
    `mcpServers` is read once at startup — while the daemon behind it can arrive
    at any later moment, including a subsequent session, and the pre-registered
    entry connects to it then.

    So this runs BEFORE the clone steps and independently of whether they
    succeed. An environment whose private checkout could not be fetched still
    gets a correct registration, and becomes useful the moment the repository is
    present, with no second pass over the config and no restart owed for the
    config's sake.

    Merged, never replaced: Claude Code may have written this file already.
    """
    config_path = _claude_json_path()
    entry = {"type": "http", "url": RETRIEVAL_MCP_DEFAULT_URL}
    data: dict = {}
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text())
            if not isinstance(data, dict):
                data = {}
    except Exception as e:  # noqa: BLE001 - an unreadable config is replaced, not fatal
        report.mcp_entry_written = {"read_error": f"{type(e).__name__}: {e}"}
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[RETRIEVAL_REPO_SLUG] = entry
    data["mcpServers"] = servers
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(config_path)
    report.mcp_entry_written = {"config_path": str(config_path), "entry": entry}
    print(f"[cloud_setup] MCP entry registered: {RETRIEVAL_REPO_SLUG} -> {entry['url']}")


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
    expected_url, port_source = (None, f"{RETRIEVAL_REPO_SLUG} checkout unresolved")
    rag_root = report.rag_roots.get(RETRIEVAL_REPO_SLUG)
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
    entry = data.get("mcpServers", {}).get(RETRIEVAL_REPO_SLUG)
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


def _image_default_path() -> str:
    """The PATH a shell on this image is given, read off `/etc/environment`.

    Needed because the cloud dialog's env-var box REPLACES PATH rather than
    extending it: a value composed for that box must carry the image's own
    default explicitly, since nothing survives underneath it.
    """
    fallback = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    try:
        for line in Path("/etc/environment").read_text(encoding="utf-8").splitlines():
            if line.startswith("PATH="):
                value = line[len("PATH=") :].strip().strip('"').strip("'")
                # /etc/environment is not a
                # shell and performs no expansion, so a value written with
                # shell-expansion syntax (e.g. PATH="$PATH:/opt/foo") is unusable
                # verbatim: it puts a literal "$PATH" into the env-var box, which
                # is precisely the failure mode this function exists to prevent.
                if "$" in value:
                    return fallback
                return value or fallback
    except OSError:
        pass
    return fallback


#: Literal candidate dirs for the session-critical binaries, beyond this
#: process' own PATH and `/etc/environment`. An image commonly puts a toolchain
#: on PATH through `/etc/profile.d`, which only a login shell reads — and this
#: script is not one — so `shutil.which` against the inherited PATH can miss a
#: binary that is plainly installed. Named literally rather than harvested from
#: shell fragments: a fragment parser matches any `/`-prefixed token on any line
#: containing `PATH=`, so a comment or an `unset PATH` guard would contribute a
#: directory that was never actually added. `unresolved` below already exists to
#: report a miss as a named gap, so a candidate dir this list doesn't carry
#: degrades honestly rather than being guessed at.
_IMAGE_TOOLCHAIN_DIRS = ("/opt/node22/bin",)


def _image_search_path(default_entries: list[str]) -> str:
    """Where to look for the session-critical binaries, beyond this process' PATH.

    Search order only. What the env-var box must carry is composed separately in
    `resolve_session_path`, which keeps `/etc/environment`'s default as the base.
    `default_entries` is the caller's already-computed `_image_default_path()`
    split, passed in rather than re-derived here.
    """
    # This process' own PATH used to be
    # searched AHEAD of the image default, so a transient directory carried
    # only by whatever bootstrap wrapper launched this script (a venv, a shim
    # dir) could win `shutil.which` and get baked into the durable
    # `env_box_value`. The named toolchain dirs still lead (they are literal,
    # not process-derived, and never transient); the image default comes next
    # so a real installed binary wins over an inherited PATH entry; this
    # process' own PATH is searched LAST, as a fallback for a binary the image
    # itself doesn't carry rather than a preference over what it does.
    own_entries = [p for p in os.environ.get("PATH", "").split(":") if p]
    ordered: list[str] = []
    for candidate in list(_IMAGE_TOOLCHAIN_DIRS) + default_entries + own_entries:
        if candidate not in ordered:
            ordered.append(candidate)
    return ":".join(ordered)


def resolve_session_path(report: Report) -> None:
    """Determine the PATH value the cloud dialog's env-var box must carry.

    That box replaces PATH outright, expands nothing, and strips surrounding
    quotes. A value written as `<npm global bin dir>:$PATH` is therefore stored
    verbatim, leaving no real directory on PATH: `python3` stops resolving and
    every coordinator hook fails open, while the session still boots, still
    loads its plugin and still reads its `CLAUDE.md`, so the only symptom is
    hook output that never appears.

    This resolves the binaries off the image and composes the exact value,
    because the alternative is an operator deriving it by hand from a session
    that has already booted wrong — and `npm prefix -g`, the obvious hand
    derivation, points at whichever node install npm was configured against and
    may carry neither language server.

    NOT a check of the running session's PATH, which is structurally
    unobservable from here: the env-var box does not reach this process.
    """
    found: dict[str, str | None] = {}
    leading: list[str] = []
    default_entries = _image_default_path().split(":")
    search_path = _image_search_path(default_entries)
    for binary in SESSION_PATH_BINARIES:
        resolved = shutil.which(binary, path=search_path)
        found[binary] = resolved
        if resolved is None:
            continue
        parent = str(Path(resolved).parent)
        if parent not in default_entries and parent not in leading:
            leading.append(parent)
    report.session_path = {
        "binaries": found,
        "unresolved": [name for name, path in found.items() if path is None],
        "env_box_value": ":".join(leading + default_entries),
    }


def _hook_plane_status_line(report: Report) -> str:
    """`HOOK PLANE: ARMED|UNARMED (delivery: <surface>)` — the literal FIRST line
    of the verdict surface, on every run, healthy or not.

    Read straight off `report.hook_plane` (claude-klabauter#26 (3)): a session
    must be able to see whether the hook plane is armed without going looking,
    and a clean run writing no verdict at all made silence indistinguishable
    from health — the exact failure mode this line exists to end. ARMED means
    everything `assert_hook_plane_armed` requires: at least one delivery
    surface registers hooks and `.doe-root` resolves. `hook_plane` absent
    (the probe never ran) reports UNARMED with an `unknown` delivery surface
    rather than silently omitting the line.
    """
    hook_plane = report.hook_plane or {}
    delivery = hook_plane.get("hook_delivery", "unknown")
    armed = bool(
        report.hook_plane
        and hook_plane.get("hooks_registered")
        and hook_plane.get("doe_root_resolves")
    )
    return f"HOOK PLANE: {'ARMED' if armed else 'UNARMED'} (delivery: {delivery})"


def _verdict_body(report: Report) -> str:
    """Render the session-facing verdict. Never None: the hook-plane status
    line (`_hook_plane_status_line`) is written on EVERY run, healthy or not,
    so a session never has to infer health from the surface's absence.

    Terse by contract: a session reads this as context on every turn, so it
    carries the fact and the one move that follows from it, never the reasoning
    that produced it. The JSON report holds the detail.
    """
    sections: list[str] = []

    failed = [step for step in report.steps if not step.ok]
    if failed:
        # "".splitlines() is [], so an
        # empty detail (StepResult.detail's own default) raised IndexError here,
        # which _record_session_surfaces_best_effort then swallowed, losing the
        # whole verdict surface for a `list index out of range` line instead of
        # the actual failures.
        lines = [
            f"- `{step.name}`: {((step.detail or '').strip().splitlines() or [''])[0][:200]}"
            for step in failed
        ]
        sections.append(
            "## Pre-boot steps that failed\n\n"
            + "\n".join(lines)
            + "\n\nThe setup script exits 0 whatever its verdicts, so this did not stop the "
            "container from starting. Treat the affected surface as absent, not working."
        )

    if report.global_doctrine is not None and report.global_doctrine.get("source") is None:
        sections.append(
            "## No global doctrine\n\n"
            "No doctrine source was found at pre-boot. This session runs doctrine-blind: "
            "absent standing rules are absent, not satisfied."
        )

    install_paths = report.plugin_install_path or {}
    if install_paths and not install_paths.get("resolves"):
        if install_paths.get("read_error"):
            detail = f"the record could not be read: {install_paths['read_error']}"
        else:
            named = ", ".join(
                f"`{entry['key']}` -> `{entry['install_path']}` "
                f"({entry.get('reason', 'unresolvable')})"
                for entry in install_paths.get("entries", [])
                if not (entry.get("path_exists") and entry.get("manifest_readable"))
            ) or "no plugin is recorded as installed at all"
            detail = f"registered but not resolvable: {named}"
        sections.append(
            "## A plugin's install path does not resolve\n\n"
            f"`{install_paths.get('record_path')}` — {detail}.\n\n"
            "`${CLAUDE_PLUGIN_ROOT}` expands to EMPTY for that plugin, so every hook it "
            "registers fails open and produces no output. Hooks cannot report this: treat "
            "the whole hook plane as absent, not working, and do not read a silent hook as "
            "a passing one."
        )

    hook_plane = report.hook_plane or {}
    if hook_plane and not (hook_plane.get("hooks_registered") and hook_plane.get("doe_root_resolves")):
        broken = []
        if not hook_plane.get("hooks_registered"):
            broken.append(
                f"`hooks` in `{hook_plane.get('settings_path')}` is empty and the coordinator "
                f"plugin delivers none ({(hook_plane.get('plugin_hooks') or {}).get('reason')}) — "
                "no coordinator hook is registered, so every autofire hook produces nothing"
            )
        if not hook_plane.get("doe_root_resolves"):
            broken.append(
                "`.doe-root` resolves through no rung — every no-launcher fence in a "
                "ceremony exits 1"
            )
        sections.append(
            "## The hook plane is NOT armed\n\n"
            + "\n".join(f"- {line}" for line in broken)
            + "\n\nA hook that produces no output is indistinguishable from one that "
            "passed, so do not read silence as success: a ceremony started here runs "
            "with no engine-minted run-id and no claimed-baton list, and reports success "
            "anyway. Check for `additionalContext` rather than assuming it. Remedy: "
            "re-run the install orchestrator "
            "(`python3 <engine clone>/coordinator_core/install/maximalist.py --non-interactive`) "
            "and read the step above it in the report for why it stopped."
        )

    unresolved = (report.session_path or {}).get("unresolved") or []
    if unresolved:
        missing = ", ".join(f"`{name}`" for name in unresolved)
        sections.append(
            "## Missing binaries on the image\n\n"
            f"Not resolvable at pre-boot: {missing}. Anything depending on them is inert; "
            "`python3` in that list means the coordinator hook plane cannot run at all."
        )

    status_line = _hook_plane_status_line(report)
    if not sections:
        return (
            f"{status_line}\n\n"
            "# Cloud pre-boot verdict\n\n"
            "Written by `cloud_setup.py` before this session started; every other pre-boot "
            "check passed. Full detail: "
            f"`{INSTALL_REPORT_PATH}`.\n"
        )
    return (
        f"{status_line}\n\n"
        "# Cloud pre-boot verdict\n\n"
        "Written by `cloud_setup.py` before this session started. Full detail: "
        f"`{INSTALL_REPORT_PATH}`.\n\n" + "\n\n".join(sections) + "\n"
    )


def _orientation_body(report: Report) -> str | None:
    """Render the container's ordinary shape, or None when there is nothing to state.

    Deliberately NOT a verdict. A cloud container carries several checkouts by
    construction — the coordinator and engine repos have to be present for the
    system to run at all — so a session whose working directory sits above them
    all is the normal case, not a degraded one. What follows from it is that no
    single work target is implied, which is a fact to state plainly and once.
    """
    if not report.rag_project_root_ambiguity:
        return None
    mounted = ", ".join(f"`{name}`" for name in sorted(report.rag_project_root_ambiguity))
    keys = [key for key, value in (report.machine_local_keys or {}).items() if "/" in str(value)]
    known = ", ".join(f"`{key}`" for key in sorted(keys)) if keys else "none registered"
    return (
        "# Cloud session orientation\n\n"
        "Written by `cloud_setup.py` before this session started.\n\n"
        "## Several repos are mounted\n\n"
        f"Checkouts mounted: {mounted}. This is the ordinary shape of a cloud container — the "
        "coordinator and engine repos are present because the system needs them to run — and the "
        "session's working directory is above all of them rather than inside one.\n\n"
        "## Which one the work is in\n\n"
        f"`{SESSION_FOCUS_ENV}` declares it. Read it from this session's environment:\n\n"
        f"- **Set** — that repo is the subject of the work, and the others are here to hold the "
        "system up. Retrieval answers for it without being asked each time.\n"
        f"- **Unset** — the work target is **unspecified**. That is a normal state, not a "
        "shortfall: every index that exists is intact and queryable, and a call answers as soon "
        "as it names the repo it means.\n\n"
        f"Addressable keys: {known}. Pass one as `repo=\"repos.<key>\"` on a call, or set "
        f"`{SESSION_FOCUS_ENV}` to one for the whole session.\n"
    )


def _write_rule_surface(basename: str, body: str | None) -> bool:
    """Land (or clear) one `<claude_home>/rules/*.md` surface.

    Shared write mechanic for `write_session_orientation` and
    `write_session_verdict`: render → unlink-if-None → mkdir → write. Each
    caller keeps its own renderer and its own report bookkeeping; only the
    mechanics are shared. Returns whether a file was written.
    """
    rule_path = _claude_home() / "rules" / basename
    if body is None:
        rule_path.unlink(missing_ok=True)
        return False
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    rule_path.write_text(body, encoding="utf-8", newline="\n")
    return True


def write_session_orientation(report: Report) -> None:
    """Land the container's ordinary shape where a session reads it.

    Kept apart from `write_session_verdict` on purpose: filing "several repos are
    mounted" beside a list of failures teaches a reader to treat the normal cloud
    shape as breakage, which is the opposite of what it means.
    """
    _write_rule_surface(SESSION_ORIENTATION_RULE, _orientation_body(report))


def write_session_verdict(report: Report) -> None:
    """Land the pre-boot verdict where a session reads it without running anything.

    `<claude_home>/rules/*.md` is pulled into session context by the harness
    directly. That is the entire reason this surface is chosen over a
    SessionStart hook: the failure most worth reporting — a PATH that resolves
    nothing — is precisely the one that silences every hook, so a hook cannot be
    the thing that reports it. The JSON report is the operator's artifact and is
    read into no session at all.

    Written on EVERY run, healthy or not (claude-klabauter#26 (3)): its first
    line always states the hook-plane status plainly (ARMED/UNARMED plus the
    delivery surface), so a session can read that off the file's presence AND
    its content, and silence never stands for health. A clean run's body still
    differs from a broken one's — no `##` failure sections follow the status
    line — but the file itself is never absent.
    """
    rule_path = _claude_home() / "rules" / SESSION_VERDICT_RULE
    written = _write_rule_surface(SESSION_VERDICT_RULE, _verdict_body(report))
    report.session_verdict = {"written": written, "path": str(rule_path) if written else None}


def write_report(report: Report) -> None:
    INSTALL_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_REPORT_PATH.write_text(json.dumps(report.to_dict(), indent=2), newline="\n")


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
    if report.auto_compact:
        _safe_print(
            "[cloud_setup] auto-compact window pinned: "
            f"{report.auto_compact['window_tokens']} tokens "
            f"(env {report.auto_compact['env_var']}="
            f"{report.auto_compact['env_rung_on_disk']!r}, settings "
            f"{report.auto_compact['settings_key']}="
            f"{report.auto_compact['settings_rung_on_disk']!r}). "
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE lowers the cut within it; at 80 that is ~0.8x."
        )
    # The env-var box is filled by hand before any session exists, so the value it
    # needs is printed where the operator filling it is already looking.
    if report.session_path:
        _safe_print(
            "[cloud_setup] cloud env-var box needs, verbatim and unquoted:\n"
            f"  PATH={report.session_path['env_box_value']}"
        )
        if report.session_path["unresolved"]:
            _safe_print(
                f"[cloud_setup] NOT resolvable on this image: {report.session_path['unresolved']}"
            )


def _dir_size_bytes(path: Path) -> int:
    """Recursive directory size via `os.scandir`, one call per level — never a
    `du` subprocess, and never `Path.rglob`'s slower stat-per-match walk."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _dir_size_bytes(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def reap_stale_pytest_trees(report: Report) -> None:
    """Remove `pytest-of-*` trees under the system temp dir whose own mtime is
    older than `STALE_PYTEST_TREE_AGE_S`, and report the bytes freed.

    claude-klabauter#27: pytest's own retention keeps the last 3 base
    directories per caller, unbounded in practice across many concurrent test
    waves — trees observed up to ~21G each. Once the container volume fills,
    every Bash/Write/Edit call fails ENOSPC, including the harness's own
    subprocess-output capture, so in-flight executor work is lost with no
    diagnosable error at the point of loss.

    Bounded and cheap by construction: no subprocess (spawning `du`/`find`
    would itself add the cost this reaper exists to cut), one `os.scandir`
    per directory level (`_dir_size_bytes`, this function's own top-level
    scan), and a strict age floor. A tree younger than the floor may still be
    owned by a concurrent run — its mtime keeps advancing while it writes —
    so it is left alone and recorded as skipped, never removed on a guess.

    Never raises: an unreadable temp root, or one tree's `rmtree` failing, is
    a recorded verdict, not a broken pipeline step.
    """
    tmp_root = Path(tempfile.gettempdir())
    removed: list[str] = []
    skipped: list[str] = []
    freed_bytes = 0
    now = time.time()
    try:
        entries = list(os.scandir(tmp_root))
    except OSError as e:
        report.pytest_tree_reap = {
            "tmp_root": str(tmp_root),
            "removed": removed,
            "skipped": skipped,
            "bytes_freed": 0,
            "error": f"{type(e).__name__}: {e}",
        }
        return

    for entry in entries:
        if not entry.name.startswith(STALE_PYTEST_TREE_PREFIX):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            age_s = now - entry.stat(follow_symlinks=False).st_mtime
        except OSError as e:
            skipped.append(f"{entry.name} (stat failed: {type(e).__name__}: {e})")
            continue
        if age_s < STALE_PYTEST_TREE_AGE_S:
            skipped.append(f"{entry.name} (age {int(age_s)}s < floor {STALE_PYTEST_TREE_AGE_S}s)")
            continue
        size = _dir_size_bytes(Path(entry.path))
        try:
            shutil.rmtree(entry.path)
        except OSError as e:
            skipped.append(f"{entry.name} (rmtree failed: {type(e).__name__}: {e})")
            continue
        removed.append(entry.name)
        freed_bytes += size

    report.pytest_tree_reap = {
        "tmp_root": str(tmp_root),
        "removed": removed,
        "skipped": skipped,
        "bytes_freed": freed_bytes,
    }
    _safe_print(
        f"[cloud_setup] pytest tree reaper: removed {len(removed)}, "
        f"freed {freed_bytes} byte(s), skipped {len(skipped)}"
    )


def _record_session_surfaces_best_effort(report: Report) -> None:
    """Resolve the env-box PATH and land the verdict surface, swallowing failures.

    Both are post-pipeline and therefore outside `run_step`'s net, and neither
    may break the exit-zero contract the cloud setup script is held to. They are
    attempted independently: a failure to resolve PATH must not also cost the
    session its verdict.
    """
    try:
        resolve_session_path(report)
    except Exception as e:  # noqa: BLE001 - a determination must not break exit-zero
        _safe_print(f"[cloud_setup] could not resolve the session PATH value: {e}")
    try:
        write_session_verdict(report)
    except Exception as e:  # noqa: BLE001 - nor may the surface that reports it
        _safe_print(f"[cloud_setup] could not write the session verdict surface: {e}")
    try:
        write_session_orientation(report)
    except Exception as e:  # noqa: BLE001 - nor the one that states the normal shape
        _safe_print(f"[cloud_setup] could not write the session orientation surface: {e}")


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
        # The refusal is the single most
        # severe pre-boot outcome, and by write_session_verdict's own rationale
        # is exactly when a session most needs cloud-preboot-verdict.md: the
        # JSON report requires a reader, the rules surface does not.
        _record_session_surfaces_best_effort(report)
        _write_report_best_effort(report)
        return 0
    print(f"[cloud_setup] {reason}")

    report = Report()

    # FIRST, cheap disk hygiene ahead of every clone/install step below: a
    # volume already filled by a prior wave's leaked pytest trees fails those
    # steps with ENOSPC before this reaper ever gets a turn.
    run_step("reap stale pytest trees", lambda: reap_stale_pytest_trees(report), report)
    run_step("clone coordinator-claude", lambda: clone_repo("coordinator-claude"), report)
    run_step("clone klabauter", lambda: clone_repo("klabauter"), report)
    run_step("set engine env", lambda: set_engine_env(report), report)
    run_step("install engine CLI shims", lambda: install_engine_cli_shims(report), report)
    # BEFORE the orchestrator, not after: the orchestrator's install-health
    # phase trust-checks the plugin root this script hands it against these
    # exact registry keys, and refuses fail-loud when they resolve empty. See
    # seed_trust_anchor_keys' docstring for the cascade that ordering caused.
    run_step("seed trust anchor keys", lambda: seed_trust_anchor_keys(report), report)
    run_step(
        "coordinator-claude install orchestrator",
        run_coordinator_install_trampoline,
        report,
    )
    run_step("run scripts/setup.py", lambda: run_claude_klabauter_setup(report), report)
    run_step("register plugin settings", register_plugin_settings, report)
    run_step("apply settings-manifest env", lambda: apply_settings_manifest_env(report), report)
    run_step("verify plugin settings", lambda: verify_plugin_settings(report), report)
    run_step("pin session PATH", lambda: pin_session_path(report), report)
    run_step("register live plugin record", register_live_plugin_record, report)
    run_step("install global doctrine", lambda: install_global_doctrine(report), report)
    run_step("verify global doctrine", lambda: verify_global_doctrine(report), report)

    # The example-retrieval-repo half, AFTER the coordinator trampoline and scripts/setup.py
    # above. That ordering is a hard constraint, not a preference: example-retrieval-repo's
    # installer seeds a concern into the machine-local registry those two steps
    # create, so running it earlier reproduces the documented refusal — and the
    # refusal reads as a example-retrieval-repo defect rather than an ordering one.
    # The registration goes first and unconditionally: it is the half a session
    # cannot repair, and it needs nothing from the checkout.
    run_step("register retrieval MCP entry", lambda: register_retrieval_mcp_entry(report), report)
    run_step(
        f"locate or clone {RETRIEVAL_REPO_SLUG}",
        lambda: locate_or_clone_repo(RETRIEVAL_REPO_SLUG, report),
        report,
    )
    run_step(
        f"locate or clone {RETRIEVAL_UE_ADDON_SLUG}",
        lambda: locate_or_clone_repo(RETRIEVAL_UE_ADDON_SLUG, report),
        report,
    )
    run_step("register machine-local repo keys", lambda: register_machine_local_repo_keys(report), report)
    # AFTER repo-key registration, not before: the fleet installer enumerates
    # registered `repos.*` keys, so running it earlier heals nothing.
    run_step("install git hooks fleet", lambda: install_hooks_fleet(report), report)
    run_step(
        "register publish mirror keys",
        lambda: register_publish_mirror_keys(report),
        report,
    )
    run_step(f"{RETRIEVAL_REPO_SLUG} cloud install", lambda: run_example_retrieval_repo_cloud_install(report), report)
    run_step("verify MCP registration", lambda: verify_mcp_registration(report), report)
    # LAST of the pipeline, deliberately: the retrieval installer and the
    # coordinator trampoline both touch plugin registration, so a check placed
    # beside the write above would attest to a record a later step could still
    # have replaced with an unresolvable one.
    run_step("verify plugin install paths", lambda: verify_plugin_install_path(report), report)
    # AFTER every writer, because it attests to the composed result rather than
    # to any one step's return. It is the only step that can tell a container
    # that wired nothing from one that wired everything.
    run_step(
        "drop double-fired settings hooks",
        lambda: drop_double_fired_settings_hooks(report),
        report,
    )
    run_step("assert hook plane armed", lambda: assert_hook_plane_armed(report), report)

    _record_session_surfaces_best_effort(report)

    _print_summary(report)

    _write_report_best_effort(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
