"""
coordinator_core.install.sandbox_check — sandbox clean-install shape validator.

Port of: ``coordinator/bin/install-sandbox-check.sh`` (DoE b5a4192c,
2026-07-20) [coordinator-content-repo repo] (BIG_PORT Wave C, item ``install-sandbox-check``,
971 LOC oracle).
Purpose (unchanged from bash): exercises the W4.1 install steps (DoE clone,
``claude-author`` wrapper, ``gen-settings-hooks`` seeding, resolver cold-tier,
publish-repo parameterization) against an isolated sandbox ``CLAUDE_HOME`` and
asserts the resulting thin-``~/.claude`` + cloned-DoE shape. Validates Tier 1 (filesystem) of the
install-surface-completeness contract; Tier 2 (running-in-Claude-Code) is
printed as a DEFERRED manual-gate banner, unchanged in spirit from the oracle.

FAMILY-I (fresh-install surface): on a cold machine ``REPO_CONTENT_ROOT`` /
``repos.content_root`` may be unresolvable — every unresolved-clone branch below
degrades to a SKIP with actionable remediation text, never a hard crash, and
:func:`main` prints a dedicated engine-root-resolution remediation block if
the claude-klabauter link itself cannot be established (the trampoline's own concern,
not this module's — this module assumes it is already running IN claude-klabauter).

Of the dependency scripts this validator drives, only ``claude-author`` remains
a genuine subprocess-exec of a DoE-owned artifact: it has no native claude-klabauter
peer, and its OWN dry-run/exec-line behavior is what checks 7/7b/F5 assert
on — there is no "port" of a wrapper whose entire job is to be invoked as a
standalone binary. The invocation (check 7) uses ``sys.executable`` directly
rather than an explicit ``bash`` spawn, and the F5 standalone-copy call
(check 7b) invokes the copied wrapper the same way; no bash spawn remains in
this module.

``gen-settings-hooks.sh`` (DoE a2078a9b, 2026-07-22) and
``resolve-coordinator-clone.sh`` (DoE 290997c7, 2026-07-22)
[coordinator-content-repo repo] are the two bridges this module used to subprocess-exec
(``bash <script> ...``) and now calls **in-process** instead, against claude-klabauter's
own native peer modules — :mod:`coordinator_core.install.gen_settings_hooks`
and :mod:`coordinator_core.resolve_coordinator_clone` respectively (see
:func:`_call_gen_settings_hooks`, :func:`_call_resolve_coordinator_clone`,
and the paired ``_assert_*_interface`` functions). Subprocess-exec'ing them
was a circular, Windows-costly (~326ms shim tax measured for
gen-settings-hooks.sh alone) round-trip through ``bash``/``sh`` PATH-probing
back into this repo's own Python. Repointed per
``cross-repo/inbox/2026-07-20-claude-central-em-sandbox-check-execs-doe-shell-blocks-deletion.md``
and its correction
``cross-repo/inbox/2026-07-20-claude-central-em-sandbox-check-doe-fix-blocks-deletion-correction.md``
(the correction is authoritative for gen-settings-hooks specifically: Step
3.5c/F8 were independently already red on a registry-less sandbox
``CLAUDE_HOME`` before that repoint, for a reason the repoint does NOT fix —
see the correction memo's rc=3 ``CLAUDE_KLABAUTER_ROOT``-resolution finding); the same
in-process pattern is the general "reimplement native, do not subprocess a
DoE oracle" doctrine for this plan
(`docs/plans/2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md`
§ Decisions).

Unit decomposition (per porter brief):
    unit1 — :func:`resolve_doe_clone`, :class:`Reporter`, :func:`_run`, arg parse
    unit2 — :func:`_tier1_filesystem_shape` (checks 1-7b)
    unit4b — :func:`_tier1b_mirror_and_cold_tier` (checks 10-11)
    unit5 — :func:`_tier1c_publish_repo_parity` (F8, checks 15-17)
    unit6 — :func:`run_all` (summary), :func:`_tier2_deferred_banner`, :func:`main`

Exit-code contract (addendum rule 3b — fail-loud VALIDATOR class):
    0  all assertions passed (or gracefully skipped — SKIP is not a failure)
    1  one or more assertions FAILed (business outcome — matches bash oracle)
    2  no FAIL, but one or more assertions were UNEVALUABLE on this host — the
       validator could not form a verdict for them (their SUBJECT is absent,
       e.g. the resolved clone is the published FLAT mirror while every
       `<clone>/coordinator/...` assertion here is about the maximalist
       authoring layout). Flagged widening, not silent: these rows used to be
       pass-equivalent SKIPs, so a run that evaluated almost nothing exited 0
       and read as clean. 1 still outranks 2 — a real FAIL is never masked by
       an unevaluable row.
    3  TRANSPORT/ORCHESTRATION failure — the harness itself could not run
       (sandbox tempdir creation failed, an unhandled exception escaped a
       tier function) OR a CLI usage/argument-parsing error (unknown flag,
       argparse's own SystemExit). Dedicated code, collides with neither 0
       (all-pass) nor 1 (business-fail), per addendum rule 3b. The bash
       oracle had no equivalent code (an unhandled bash error under
       ``set -euo pipefail`` just aborted with whatever exit status the
       failing command produced, usually NOT 3) — this is a flagged
       behavioral improvement, not a silent one: a caller parsing rc can now
       tell "checks ran and some failed" (1) apart from "checks could not
       run at all, including a plain CLI typo" (3).

Negative-spec (faithful bash-oracle reproduction, NOT a fix):
    - The bash oracle's ``bash >= 4`` self-guard has
      NO Python analogue — it guarded the oracle's OWN interpreter version
      (associative-array / ``mapfile`` features used later in the script).
      This module is Python, not bash, so that guard is dropped entirely
      (not silently narrowed — there is no bash-version constraint on THIS
      process; the dependency scripts it shells out to still run under
      whatever ``bash`` is first on PATH, exactly as the oracle's own
      sub-invocations did, and inherit their OWN guards unchanged).
    - The oracle's ``jq`` dependency check is REPLACED by the
      stdlib ``json`` module for every settings.json / bare-JSON inspection
      in this module, per the same substrate-change precedent as
      ``coordinator_core.install.gen_settings_hooks`` — behavior-equivalent,
      not a jq reimplementation.
    - Every ``subprocess.run`` in this module carries ``timeout=`` and
      ``stdin=subprocess.DEVNULL`` (addendum rule 2) — the bash oracle had
      NO timeout on any of its ``bash <script>`` invocations (a hung child
      would hang the whole checker forever). A timed-out sub-invocation is
      reported as a FAIL of that specific assertion with a "TIMEOUT" message,
      not a silent skip and not a harness crash — flagged broadening per
      addendum rule 7, not a silent behavior change (every prior-passing
      input still passes; only a previously-infinite-hang input now
      terminates as a clean FAIL).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core import machine_resolver, resolve_coordinator_clone
from coordinator_core._settings_home import native_path_form
from coordinator_core.content_root import CONTENT_ROOT_KEY, POINTER_NAME, migrate_legacy_config
from coordinator_core.install import gen_settings_hooks
from coordinator_core.win_portability import is_executable, no_console_creationflags

#: Named so `_sep_norm` reads without an escape-in-an-escape.
BACKSLASH = chr(92)

# Generator-provenance declaration (coordinator_core/ops/generator_census). Every write in
# this module targets an isolated sandbox CLAUDE_HOME created for the test
# run (docstring: "exercises...against an isolated sandbox CLAUDE_HOME") --
# tmp/test fixtures, never tracked claude-klabauter paths.
GENERATES = []

_DEFAULT_TIMEOUT = 60


# ---------------------------------------------------------------------------
# unit1 — Reporter, subprocess helper, DoE-clone resolution, arg parse
# ---------------------------------------------------------------------------


class Reporter:
    """Mirrors the bash oracle's ``_pass``/``_fail``/``_skip``/``_info``
    helpers and the module-global ``PASS``/``FAIL`` counters."""

    def __init__(self) -> None:
        self.pass_count = 0
        self.fail_count = 0
        self.unevaluable_count = 0
        self.lines: List[str] = []

    def _emit(self, line: str) -> None:
        self.lines.append(line)
        print(line)

    def ok(self, msg: str) -> None:
        self.pass_count += 1
        self._emit(f"PASS: {msg}")

    def bad(self, msg: str) -> None:
        self.fail_count += 1
        print(f"FAIL: {msg}", file=sys.stderr)
        self.lines.append(f"FAIL: {msg}")

    def skip(self, msg: str) -> None:
        self._emit(f"SKIP: {msg}")

    def unevaluable(self, msg: str) -> None:
        """An assertion whose SUBJECT is absent on this host, so neither PASS
        nor FAIL is true of it.

        Distinct from :meth:`skip` on purpose. A SKIP is pass-equivalent and
        exits 0; an UNEVALUABLE row is the validator saying it could not form
        a verdict, and it carries its own exit code (2) so no caller can read
        a not-evaluated run as a clean one. An oracle that reports a pass for
        an assertion it never evaluated is the one failure mode this module
        must not have."""
        self.unevaluable_count += 1
        self._emit(f"UNEVALUABLE: {msg}")

    def info(self, msg: str) -> None:
        self._emit(f"INFO: {msg}")

    def section(self, title: str) -> None:
        self._emit(f"\n{title}")


def _run(
    cmd: List[str],
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Centralized subprocess wrapper — every call site in this module routes
    through here so the addendum's timeout / stdin-guard / no-console rules
    (A2, A4) are applied exactly once, not re-derived per call site. A
    timeout is converted to a synthetic ``CompletedProcess`` with rc=124
    (the POSIX shell convention for a timed-out command) rather than letting
    ``subprocess.TimeoutExpired`` propagate — callers treat rc!=0 uniformly."""
    stdin_kw = {} if input_text is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(
            cmd,
            env=env,
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=input_text,
            **stdin_kw,
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, 124, stdout="", stderr=f"TIMEOUT after {exc.timeout}s: {' '.join(cmd)}"
        )
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=f"exec failed: {exc}")


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _sep_norm(p: str) -> str:
    """Forward-slash form of a path, no trailing separator.

    Every path COMPARISON in this module goes through here. The checks build
    their expectations by f-string concatenation with a literal "/", while the
    resolvers under test return native paths — on Windows that made
    `X:{bs}coordinator-content-repo{bs}coordinator` != `X:{bs}coordinator-content-repo/coordinator` and
    reported a correct resolver as a FAIL. Separator form is not what any of
    these assertions is about."""
    p = p.replace(BACKSLASH, "/")
    while p.endswith("/") and p != "/":
        p = p[:-1]
    return p


def _paths_equal(a: str, b: str) -> bool:
    return _sep_norm(a) == _sep_norm(b)


def _path_mentioned(needle: str, haystack: str) -> bool:
    """True if *haystack* text references the *needle* path in either form."""
    return _sep_norm(needle) in _sep_norm(haystack)


#: The clone layouts ``repos.content_root`` can legitimately resolve to. This
#: validator's subject is the MAXIMALIST one (`<clone>/coordinator/...`); the
#: FLAT one is the published mirror, whose surfaces sit at the clone root and
#: which a marketplace-served install (every cloud container) registers
#: directly. The distinction is not cosmetic: on a flat clone every
#: `<clone>/coordinator/...` path this module asserts is absent by CONSTRUCTION,
#: so a bare FAIL there reports an install defect where the truth is "this
#: validator's subject is not what the pointer resolves to on this host."
CLONE_LAYOUT_MAXIMALIST = "maximalist"
CLONE_LAYOUT_FLAT = "flat"
CLONE_LAYOUT_UNKNOWN = "unknown"


def clone_layout(clone: str) -> str:
    """Classify a resolved clone root as maximalist, flat, or unknown.

    Positive markers only, never a negation: `coordinator/` present means
    maximalist; `hooks/` AND `skills/` at the root with no `coordinator/`
    means the published flat mirror. Anything else is UNKNOWN and is reported
    as such rather than defaulted to either shape — guessing here would put a
    wrong verdict into the oracle, which is worse than declining one."""
    if not clone or not os.path.isdir(clone):
        return CLONE_LAYOUT_UNKNOWN
    if os.path.isdir(os.path.join(clone, "coordinator")):
        return CLONE_LAYOUT_MAXIMALIST
    if os.path.isdir(os.path.join(clone, "hooks")) and os.path.isdir(os.path.join(clone, "skills")):
        return CLONE_LAYOUT_FLAT
    return CLONE_LAYOUT_UNKNOWN


def _layout_note(clone: str) -> str:
    """Suffix naming a non-maximalist clone layout as the cause, for FAIL rows
    whose expected path form is `<clone>/coordinator/...`.

    Empty for a maximalist clone, so a genuine defect's message is unchanged."""
    layout = clone_layout(clone)
    if layout == CLONE_LAYOUT_FLAT:
        return (
            " [cause: the resolved clone is a FLAT published-mirror layout — its surfaces sit at "
            "the clone root, so the maximalist <clone>/coordinator/... form this assertion expects "
            "cannot exist here. Point REPO_CONTENT_ROOT at the maximalist authoring clone, or pass "
            "--coordinator-root <clone-root>.]"
        )
    if layout == CLONE_LAYOUT_UNKNOWN:
        return (
            " [cause: the resolved clone matches NEITHER the maximalist nor the flat published-mirror "
            "layout — this assertion's expected path form may not apply to it at all.]"
        )
    return ""


def _cold_bare_path() -> str:
    """A deliberately minimal PATH for the cold-tier probes: the host's system
    binary directories and nothing else.

    Host-shaped, not POSIX-shaped. The former hardcoded ``/usr/bin:/bin``
    resolves NOTHING on Windows, which made the ``claude-home``/``machine-local``
    absence probe below structurally incapable of finding a binary there — a
    PASS that asserted nothing on every Windows run, the same shape as an
    ``.exe``-gated census that cannot report on POSIX."""
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or "C:" + BACKSLASH + "Windows"
        parts = [os.path.join(system_root, "System32"), system_root]
        return os.pathsep.join(parts)
    parts = ["/usr/bin", "/bin"]
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if os.path.isdir(extra):
            parts.append(extra)
    return os.pathsep.join(parts)


def _claude_author_wrapper_src() -> str:
    """Absolute path to the `claude-author` wrapper this validator installs and
    dry-runs.

    It lives in THIS repo (`<claude-klabauter>/coordinator/bin/claude-author.py`), not under
    the `--coordinator-root` DoE tree — `<doe>/coordinator/bin/` has no
    `claude-author*` file at all. Probing the DoE tree made the wrapper checks FAIL
    on a correctly-installed machine and then crashed the whole run: the F5 leg
    `shutil.copy2`d the same missing path with no existence guard, so an
    unhandled FileNotFoundError replaced every check result with a transport
    failure. Self-location is the RIGHT resolver here precisely because the
    wrapper is this repo's own artifact — the opposite of `templates/`, which
    is the DoE clone's and must come from `--coordinator-root`.
    """
    return str(Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "claude-author.py")


def _python_launch(script: str, *args: str) -> List[str]:
    """argv for running a `.py`/extensionless Python entrypoint. Never exec the
    file directly: the sandbox copies land as extensionless `claude-author`, which
    Windows cannot execute (no shebang honoring, no PATHEXT match) — that path
    reported rc=127 as a wrapper defect on every Windows run."""
    return [sys.executable, script, *args]


def resolve_doe_clone() -> Tuple[str, bool]:
    """Order: ``REPO_CONTENT_ROOT`` env, then ``migrate_legacy_config()`` (an
    upgrade box carrying only a legacy-named pointer or key gains
    ``repos.content_root``; a no-op otherwise, spawns nothing), then an
    in-process ``machine_resolver.registry_get("repos.content_root")`` read,
    then ``machine-local get repos.content_root`` (sibling-of-self or PATH) as
    the CLI-spawn fallback rung. Returns ``("", False)`` on total failure —
    NEVER raises (fresh-install machines routinely fail to resolve this; every
    downstream check degrades to SKIP, per the FAMILY-I contract in the
    module docstring).

    The registry rung buys nothing on a genuinely fresh box — on first
    install ``registry.local.toml`` has no ``repos.content_root`` either, this
    rung misses, and the CLI spawn fires exactly as before. Its value is the
    *repeat* run: once the key has been seeded, every subsequent
    ``sandbox_check`` pass reads it for free instead of paying the spawn
    again. ``registry_get`` does not normalize its return value the way the
    CLI does (``_to_native_drive_path``) — a wrong (unnormalized) value here
    would silently poison every downstream check that validates against it,
    so its result is passed through ``native_path_form`` (the drive/MSYS
    repair) before being returned."""
    doe_clone = os.environ.get("REPO_CONTENT_ROOT", "")
    if doe_clone:
        return doe_clone, True

    try:
        migrate_legacy_config()
    except OSError:
        pass  # an unwritable registry must not stop the read below

    registry_value = machine_resolver.registry_get(CONTENT_ROOT_KEY)
    if registry_value:
        return native_path_form(registry_value), True

    ml = _which("machine-local")
    if ml:
        cp = _run([ml, "get", CONTENT_ROOT_KEY], timeout=15)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip(), True

    return "", False


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install-sandbox-check",
        description=(
            "Validates the W4.1 thin-~/.claude + cloned-upstream + wired-wrapper "
            "install shape in an isolated sandbox."
        ),
        add_help=False,
    )
    parser.add_argument("--keep-sandbox", action="store_true", dest="keep_sandbox")
    parser.add_argument("--verbose", "-v", action="store_true", dest="verbose")
    parser.add_argument(
        "--coordinator-root",
        dest="coordinator_root_override",
        default=None,
        help="Override <doe_clone>/coordinator resolution (the claude-author trampoline "
        "resolves the default from the upstream clone, NOT its own script-dir "
        "location, since this executable now lives in claude-klabauter while "
        "coordinator/templates/ stayed in the upstream clone).",
    )
    parser.add_argument("-h", "--help", action="store_true", dest="help")
    return parser


def _usage_text() -> str:
    return (
        "Usage: install-sandbox-check [OPTIONS]\n"
        "\n"
        "Validates the W4.1 thin-~/.claude + cloned-upstream + wired-wrapper install shape\n"
        "in an isolated sandbox. Tier 1 (filesystem) only — Tier 2 (running-in-Claude-Code)\n"
        "is printed as a DEFERRED manual gate at the end.\n"
        "\n"
        "Options:\n"
        "  --keep-sandbox           Do not delete the sandbox directory after the run.\n"
        "  --verbose, -v            Print each assertion with context even when passing.\n"
        "  --coordinator-root PATH  Override <doe_clone>/coordinator (normally resolved\n"
        "                           by the claude-author trampoline from the upstream clone, not\n"
        "                           from this script's own location).\n"
        "  -h, --help               Show this usage.\n"
        "\n"
        "Environment:\n"
        "  REPO_CONTENT_ROOT  Path to the resolved clone (primary resolution override).\n"
        "                   Falls back to: an in-process registry read (repos.content_root),\n"
        "                   then machine-local get repos.content_root.\n"
        "                   At least one must be set for the clone checks to run.\n"
        "\n"
        "Exit codes:\n"
        "  0  all assertions passed (or gracefully skipped)\n"
        "  1  one or more assertions FAILed\n"
        "  2  no FAIL, but one or more assertions were UNEVALUABLE on this host\n"
        "     (their subject is absent here — the run formed no verdict for them)\n"
        "  3  transport/orchestration failure (harness itself could not run)\n"
    )


# ---------------------------------------------------------------------------
# unit2 — Tier 1: filesystem shape (checks 1-7b)
# ---------------------------------------------------------------------------


def _tier1_filesystem_shape(
    r: Reporter,
    sandbox: str,
    doe_clone: str,
    doe_clone_resolved: bool,
    coordinator_root: str,
) -> bool:
    """Returns ``doe_coordinator_present``."""
    r.section("=== Tier 1: Filesystem shape ===")

    # 1. sandbox CLAUDE_HOME created
    if os.path.isdir(sandbox):
        r.ok(f"sandbox CLAUDE_HOME created: {sandbox}")
    else:
        r.bad(f"sandbox CLAUDE_HOME missing: {sandbox}")

    doe_coordinator_present = False
    if doe_clone_resolved:
        if os.path.isdir(os.path.join(doe_clone, ".git")):
            r.ok(f"clone present: {doe_clone}")
        else:
            r.bad(f"clone missing .git: {doe_clone}")

        if os.path.isdir(os.path.join(doe_clone, "coordinator")):
            r.ok(f"clone's coordinator/ dir present: {os.path.join(doe_clone, 'coordinator')}")
            doe_coordinator_present = True
        else:
            # Was a flat SKIP reading "W4.2 cutover not yet completed", which is
            # both stale and wrong on every marketplace-served install: there the
            # pointer resolves to the PUBLISHED FLAT MIRROR, whose surfaces are at
            # the clone root by design and never under coordinator/. A SKIP is
            # pass-equivalent, so that reading let the whole maximalist tier go
            # un-evaluated behind a clean-looking row.
            layout = clone_layout(doe_clone)
            if layout == CLONE_LAYOUT_MAXIMALIST:  # pragma: no cover - unreachable by construction
                r.bad("clone's coordinator/ dir absent though the clone classifies as maximalist")
            else:
                r.unevaluable(
                    f"clone's coordinator/ dir absent — resolved clone layout is {layout!r}, not the "
                    f"maximalist authoring layout this validator asserts. Every <clone>/coordinator/... "
                    f"assertion below is UNEVALUABLE against this clone, not passing."
                )
                r.info(
                    "  Remediation: point REPO_CONTENT_ROOT (or repos.content_root) at the maximalist "
                    "authoring clone, or pass --coordinator-root <clone-root> for a flat mirror."
                )
    else:
        r.skip("clone checks (clone path not resolved)")

    # 4. no plugin byte-copy
    sandbox_plugin_dir = os.path.join(sandbox, "plugins", "coordinator-claude")
    if os.path.isdir(sandbox_plugin_dir) and os.listdir(sandbox_plugin_dir):
        r.bad(f"byte-copy anti-pattern: {sandbox_plugin_dir}/ is non-empty (should not exist)")
    else:
        r.ok("no plugin byte-copy in sandbox ~/.claude/plugins/coordinator-claude/ (thin shape)")

    # 5. wrapper install
    r.section("--- Step 3.5b: wrapper install ---")
    wrapper_src = _claude_author_wrapper_src()
    wrapper_src_present = os.path.isfile(wrapper_src)
    if not wrapper_src_present:
        r.bad(f"claude-author wrapper not found at: {wrapper_src}")
    else:
        r.ok(f"claude-author wrapper source present: {wrapper_src}")
        sandbox_local_bin = os.path.join(sandbox, ".local", "bin")
        sandbox_wrapper = os.path.join(sandbox_local_bin, "claude-author")
        os.makedirs(sandbox_local_bin, exist_ok=True)
        shutil.copy2(wrapper_src, sandbox_wrapper)
        if os.name != "nt":
            os.chmod(sandbox_wrapper, 0o755)
        # On Windows the exec bit does not exist; `is_executable` is a POSIX
        # mode test, so asserting it there fails a correct install.
        installed_ok = os.path.isfile(sandbox_wrapper) and (
            os.name == "nt" or is_executable(sandbox_wrapper)
        )
        if installed_ok:
            r.ok(f"claude-author wrapper installed: {sandbox_wrapper}")
        else:
            r.bad(f"claude-author wrapper install failed (missing or exec-bit not set): {sandbox_wrapper}")

    # 6. gen-settings-hooks seeding
    r.section("--- Step 3.5c: settings.json hook block seed ---")
    if not doe_coordinator_present:
        r.ok("gen_settings_hooks module available (in-process call, no bash subprocess)")
        r.unevaluable(
            f"gen-settings-hooks live seed: no {doe_clone}/coordinator/ to seed against "
            f"(clone layout {clone_layout(doe_clone)!r}, not maximalist)"
        )
    else:
        _assert_gen_settings_hooks_interface(r)

        sandbox_settings = os.path.join(sandbox, "settings.json")
        Path(sandbox_settings).write_text("{}", encoding="utf-8", newline="\n")

        # COORDINATOR_SETTINGS_HOME must be pinned into the sandbox, not merely
        # inherited: settings-home-rooted writes would otherwise land on the
        # LIVE settings home. CLAUDE_HOME alone does not confine them.
        env = {
            **os.environ,
            "CLAUDE_HOME": sandbox,
            "COORDINATOR_SETTINGS_HOME": os.path.join(sandbox, ".coordinator-claude-settings"),
            "REPO_CONTENT_ROOT": doe_clone,
        }
        rc, err, status = _call_gen_settings_hooks(sandbox_settings, env)
        if rc == 0:
            r.ok(f"gen_settings_hooks.generate() succeeded against sandbox settings.json ({status})")
        else:
            err_path = os.path.join(sandbox, "gen-hooks-err.txt")
            Path(err_path).write_text(err or "", encoding="utf-8", newline="\n")
            r.bad(f"gen_settings_hooks.generate() failed (see {err_path})")

        # A `skipped (...)` status is the generator DECLINING to write, by
        # design: no positive marker on this machine, an operator kill-switch,
        # or plugin-side hook delivery already live and fully resolvable (in
        # which case generating on top would double-fire every hook). Asserting
        # a non-empty hooks array against that outcome made this checker
        # contradict the generator — two components disagreeing about what
        # "installed" means, with the generator being the correct one. The
        # seeded-vs-declined distinction is the generator's to make; this
        # validator only asserts that whichever it chose, it did coherently.
        generation_declined = rc == 0 and status.startswith("skipped")

        hook_count = 0
        mcp_leak = 0
        if os.path.isfile(sandbox_settings):
            try:
                data = json.loads(Path(sandbox_settings).read_text(encoding="utf-8"))
                hooks = data.get("hooks") or {}
                hook_count = len(hooks)
                for groups in hooks.values():
                    for group in groups if isinstance(groups, list) else []:
                        for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                            cmd = (hook or {}).get("command", "") or ""
                            if "mcp_tool" in cmd or "coordinator_core" in cmd:
                                mcp_leak += 1
            except (json.JSONDecodeError, OSError):
                hook_count = 0
            if generation_declined:
                if hook_count == 0:
                    r.skip(f"settings.json hook seed declined by the generator: {status}")
                else:
                    r.bad(
                        f"gen_settings_hooks reported {status!r} but settings.json has "
                        f"{hook_count} hook bucket(s) — a declined run must write nothing"
                    )
            elif hook_count > 0:
                r.ok(f"settings.json hooks array seeded: {hook_count} event bucket(s)")
            else:
                r.bad("settings.json hooks array empty after gen-settings-hooks run")
        else:
            r.bad("settings.json not created in sandbox")

        if mcp_leak == 0:
            r.ok("no mcp_tool entries leaked into settings.json hooks (type filter correct)")
        else:
            r.bad("mcp_tool-shaped command found in settings.json hooks (type filter broken)")

        # idempotency
        sandbox_settings_v2 = os.path.join(sandbox, "settings.v2.json")
        if os.path.isfile(sandbox_settings):
            shutil.copy2(sandbox_settings, sandbox_settings_v2)
            rc2, err2, _status2 = _call_gen_settings_hooks(sandbox_settings, env)
            if rc2 == 0:
                if Path(sandbox_settings_v2).read_bytes() == Path(sandbox_settings).read_bytes():
                    r.ok("gen_settings_hooks.generate() is idempotent (second run = no-op diff)")
                else:
                    r.bad("gen_settings_hooks.generate() is NOT idempotent (second run changed settings.json)")
            else:
                r.bad(f"gen_settings_hooks.generate() failed on second (idempotency) run: {err2}")

    # 7. Claude-author --dry-run
    r.section("--- Step 3.5b dry-run: claude-author --dry-run ---")
    if doe_clone_resolved and doe_coordinator_present:
        # CLAUDE_AUTHOR_GEN_HOOKS_SCRIPT is NOT set here: it was claude-author's
        # per-launch self-heal hook (regenerate settings.json's hook block
        # on every launch via gen-settings-hooks.sh) — see that wrapper's own
        # 2026-07-20 docstring note. That self-heal call site was removed
        # from claude-author entirely (hook seeding is install-time-only now,
        # not a per-launch concern), so claude-author no longer reads this env
        # var at all; setting it to a path that no longer exists on disk
        # (gen-settings-hooks.sh is retired repo-wide) asserted nothing.
        env = {
            **os.environ,
            "CLAUDE_HOME": sandbox,
            "REPO_CONTENT_ROOT": doe_clone,
        }
        cp = _run(_python_launch(wrapper_src, "--dry-run"), env=env)
        dryrun_err = os.path.join(sandbox, "dryrun-err.txt")
        Path(dryrun_err).write_text(cp.stderr or "", encoding="utf-8", newline="\n")
        if cp.returncode == 0:
            dry_out = cp.stdout
            if "exec claude --plugin-dir" in dry_out:
                r.ok("claude-author --dry-run emitted exec line with --plugin-dir")
            else:
                r.bad(f"claude-author --dry-run output missing 'exec claude --plugin-dir' (got: {dry_out})")
            if _path_mentioned(os.path.join(doe_clone, "coordinator"), dry_out):
                r.ok("claude-author --dry-run exec line references clone's coordinator dir")
            else:
                r.bad("claude-author --dry-run exec line does not reference clone's coordinator dir")
        else:
            r.bad(f"claude-author --dry-run exited non-zero (see {dryrun_err})")
    elif not doe_clone_resolved:
        r.skip("claude-author --dry-run (clone path not resolved)")
    else:
        r.unevaluable(
            f"claude-author --dry-run: the exec line it asserts names {doe_clone}/coordinator, which does "
            f"not exist (clone layout {clone_layout(doe_clone)!r}, not maximalist)"
        )

    # 7b. F5 regression: standalone-copy
    r.section("--- F5 regression: claude-author standalone-copy --dry-run (siblings absent) ---")
    if doe_clone_resolved and doe_coordinator_present and wrapper_src_present:
        f5_dir = os.path.join(sandbox, "f5-standalone-bin")
        os.makedirs(f5_dir, exist_ok=True)
        f5_wrapper = os.path.join(f5_dir, "claude-author")
        shutil.copy2(wrapper_src, f5_wrapper)
        if os.name != "nt":
            os.chmod(f5_wrapper, 0o755)

        # gen-settings-hooks.sh dropped from this sibling-leak check: it is
        # retired repo-wide (see check 7's comment above), so its absence
        # here is now always true and asserts nothing — machine-local is the
        # one sibling still meaningful to check for.
        if os.path.isfile(os.path.join(f5_dir, "machine-local")):
            r.bad(f"F5 regression setup broken: siblings unexpectedly present in {f5_dir}")
        else:
            r.ok(f"F5 regression setup: standalone dir has claude-author ALONE (no siblings): {f5_dir}")
            env = {**os.environ, "REPO_CONTENT_ROOT": doe_clone}
            env.pop("CLAUDE_HOME", None)
            cp = _run(_python_launch(f5_wrapper, "--dry-run"), env=env)
            f5_err = os.path.join(sandbox, "f5-dryrun-err.txt")
            Path(f5_err).write_text(cp.stderr or "", encoding="utf-8", newline="\n")
            if cp.returncode == 0:
                dry_out = cp.stdout
                if "exec claude --plugin-dir" in dry_out:
                    r.ok("F5: standalone-copy claude-author --dry-run emitted exec line with --plugin-dir")
                else:
                    r.bad(f"F5: standalone-copy claude-author --dry-run output missing 'exec claude --plugin-dir' (got: {dry_out})")
                if _path_mentioned(os.path.join(doe_clone, "coordinator"), dry_out):
                    r.ok("F5: standalone-copy claude-author --dry-run exec line references clone's coordinator dir")
                else:
                    r.bad("F5: standalone-copy claude-author --dry-run exec line does not reference clone's coordinator dir")
            else:
                r.bad(f"F5: standalone-copy claude-author --dry-run exited non-zero (see {f5_err})")
    elif not doe_clone_resolved:
        r.skip("F5 regression: standalone-copy claude-author --dry-run (clone path not resolved)")
    elif not wrapper_src_present:
        r.skip(f"F5 regression: standalone-copy claude-author --dry-run (wrapper source absent: {wrapper_src})")
    else:
        r.unevaluable(
            f"F5 regression: standalone-copy claude-author --dry-run — the exec line it asserts names "
            f"{doe_clone}/coordinator, which does not exist (clone layout {clone_layout(doe_clone)!r}, "
            f"not maximalist)"
        )

    return doe_coordinator_present


def _assert_gen_settings_hooks_interface(r: Reporter) -> None:
    """Real interface assertion against the in-process
    ``coordinator_core.install.gen_settings_hooks`` module — replaces the
    former source-text grep for a literal ``"--out"`` substring in
    ``gen-settings-hooks.sh``, which becomes meaningless once the call site
    is in-process (there is no CLI arg string left to grep for). Asserts the
    module exposes a callable ``generate()`` accepting an ``out_path=``
    keyword, mirroring the ``--out <path>`` contract the call sites below
    depend on (see :func:`_call_gen_settings_hooks`)."""
    import inspect

    generate_fn = getattr(gen_settings_hooks, "generate", None)
    if not callable(generate_fn):
        r.bad("coordinator_core.install.gen_settings_hooks has no callable generate() (interface contract broken)")
        return
    params = inspect.signature(generate_fn).parameters
    if "out_path" in params:
        r.ok("gen_settings_hooks.generate() accepts out_path= (interface contract verified)")
    else:
        r.bad("gen_settings_hooks.generate() does not accept out_path= (interface contract broken — sandbox isolation will not work)")


def _call_gen_settings_hooks(out_path: str, env: Dict[str, str]) -> Tuple[int, str, str]:
    """In-process call to :func:`gen_settings_hooks.generate`, replacing the
    former ``bash gen-settings-hooks.sh --out <path>`` subprocess spawn
    (measured ~326ms Windows shim tax per invocation, plus a silent
    empty-hooks-array failure under a registry-less sandbox ``CLAUDE_HOME`` —
    see module docstring negative-spec and
    ``cross-repo/inbox/2026-07-20-claude-central-em-sandbox-check-doe-fix-blocks-deletion-correction.md``).
    ``generate()`` resolves ``coordinator_root`` via
    :func:`coordinator_core.install._shared.resolve_coordinator_root`, which
    reads ``CLAUDE_HOME`` / ``REPO_CONTENT_ROOT`` / ``COORDINATOR_ROOT`` from
    process environment — there is no subprocess env dict to hand across
    anymore, so this overlays ``env`` onto the current process's
    ``os.environ`` for the duration of the call and restores the prior
    environment afterward (save/restore, not merge — mirrors the subprocess
    call's isolated child-env semantics as closely as an in-process call
    can). Returns ``(rc, stderr_text)`` mirroring the
    ``subprocess.CompletedProcess`` shape the call sites already branch on:
    0 success (including kill-switch no-op), 1 generator business error
    (message captured in ``stderr_text``, same as bash's undifferentiated
    exit-1 contract).

    Returns ``(rc, stderr_text, status)``. ``status`` is ``generate()``'s own
    return value — ``"skipped (...)"`` when it deliberately declined to write.
    Discarding it (as this wrapper did until 2026-08-14) leaves the caller
    unable to tell "wrote no hooks because it correctly refused" from "wrote no
    hooks because it broke", and the caller then reported the former as a
    FAIL."""
    saved_env = dict(os.environ)
    stderr_buf = io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(env)
        with contextlib.redirect_stderr(stderr_buf):
            status = gen_settings_hooks.generate(out_path=out_path)
        return 0, stderr_buf.getvalue(), str(status or "")
    except gen_settings_hooks.GenSettingsHooksError as exc:
        return 1, str(exc), ""
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def _assert_resolve_coordinator_clone_interface(r: Reporter) -> None:
    """Real interface assertion against the in-process
    ``coordinator_core.resolve_coordinator_clone`` module — replaces the
    former file-presence check of the DoE ``resolve-coordinator-clone.sh``
    (804-line pure-bash oracle, never a polyglot — no ``py_compile`` check
    ever applied to it). Asserts the module exposes both public resolver
    entrypoints (see :func:`_call_resolve_coordinator_clone`)."""
    ok = True
    for name in ("resolve_clone_root", "resolve_content_root"):
        if not callable(getattr(resolve_coordinator_clone, name, None)):
            r.bad(f"coordinator_core.resolve_coordinator_clone has no callable {name}() (interface contract broken)")
            ok = False
    if ok:
        r.ok("resolve_coordinator_clone.resolve_clone_root()/resolve_content_root() callable (interface contract verified)")


def _call_resolve_coordinator_clone(mode: str, env: Dict[str, str]) -> Tuple[int, str]:
    """In-process call to :mod:`coordinator_core.resolve_coordinator_clone`,
    replacing the former ``bash resolve-coordinator-clone.sh
    --for-content|--for-git-ops`` subprocess spawn. ``mode`` is
    ``"content"`` or ``"clone"``. Same save/restore ``os.environ`` overlay
    as :func:`_call_gen_settings_hooks` — the resolver reads
    ``CLAUDE_PLUGIN_ROOT``/``COORDINATOR_ROOT``/``COORDINATOR_CLONE``/
    ``CLAUDE_HOME`` etc. straight from process environment, so there is no
    subprocess env dict to hand across. Returns ``(rc, path_or_error)``:
    ``0`` + the resolved path on success, ``1`` + the error text on
    :class:`resolve_coordinator_clone.ResolveCoordinatorCloneError` (mirrors
    the bash oracle's rc-1 resolution-failure contract; its rc-2 CLI-usage
    path has no analogue here since ``mode`` is always a valid literal, never
    user-supplied argv)."""
    saved_env = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        if mode == "content":
            return 0, resolve_coordinator_clone.resolve_content_root()
        return 0, resolve_coordinator_clone.resolve_clone_root()
    except resolve_coordinator_clone.ResolveCoordinatorCloneError as exc:
        return 1, str(exc)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


#: Opens Tier 1b; the checks that follow (10, 11) are native in-process.
_TIER1B_BANNER = "=== Tier 1b: Maximalist install shape (mirror/resolver — native in-process) ==="


# ---------------------------------------------------------------------------
# unit4b — Tier 1b: mirror verification, resolver cold tier
# ---------------------------------------------------------------------------


def _tier1b_mirror_and_cold_tier(
    r: Reporter,
    sandbox: str,
    doe_clone: str,
    doe_clone_resolved: bool,
) -> None:
    r.section(_TIER1B_BANNER)

    # ---- 10. Mirror verification ----
    r.section("--- Mirror verification: live_path == <doe>/coordinator (AC5) ---")
    _assert_resolve_coordinator_clone_interface(r)
    if doe_clone_resolved:
        expected_mirror = f"{doe_clone}/coordinator"
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": expected_mirror}
        rc, mirror_out_or_err = _call_resolve_coordinator_clone("content", env)
        mirror_out = mirror_out_or_err if rc == 0 else ""
        if _paths_equal(mirror_out, expected_mirror):
            r.ok(f"AC5: resolve_coordinator_clone.resolve_content_root() (CLAUDE_PLUGIN_ROOT): returned expected {doe_clone}/coordinator")
        elif mirror_out and os.path.isdir(mirror_out):
            r.bad(f"AC5: resolve_coordinator_clone.resolve_content_root(): returned '{mirror_out}', expected '{expected_mirror}'")
        else:
            r.bad(f"AC5: resolve_coordinator_clone.resolve_content_root(): returned empty or non-existent path (error: {mirror_out_or_err}){_layout_note(doe_clone)}")
    else:
        r.skip("mirror verification (clone path not resolved)")

    # ---- 11. Resolver cold tier ----
    r.section("--- Resolver cold tier (AC6) ---")
    if doe_clone_resolved:
        cold_env = os.path.join(sandbox, "cold-env")
        os.makedirs(os.path.join(cold_env, ".claude"), exist_ok=True)
        cold_pointer = os.path.join(cold_env, ".claude", POINTER_NAME)
        Path(cold_pointer).write_text(doe_clone, encoding="utf-8", newline="\n")

        cold_ptr_read = Path(cold_pointer).read_text(encoding="utf-8", errors="replace")
        if cold_ptr_read == doe_clone:
            r.ok(f"cold-env content-root pointer seeded: {doe_clone}")
        else:
            r.bad(f"cold-env content-root pointer setup failed (expected '{doe_clone}', got '{cold_ptr_read}')")

        cold_flat = os.path.join(cold_env, ".claude", "plugins", "coordinator-claude", "coordinator")
        if not os.path.isdir(cold_flat):
            r.ok("cold-env: no flat-layout tree (correct cold environment)")
        else:
            r.bad(f"cold-env setup: flat tree unexpectedly present at {cold_flat}")

        if os.path.isdir(os.path.join(doe_clone, ".git")):
            r.ok("clone has .git at root (git-ops pointer-tier pre-condition met)")
        else:
            r.bad("clone missing .git at root — --for-git-ops pointer tier cannot satisfy .git gate")
        if os.path.isdir(os.path.join(doe_clone, "coordinator")):
            r.ok("clone has coordinator/ subdir (content pointer-tier pre-condition met)")
        else:
            r.bad(f"clone missing coordinator/ subdir — --for-content pointer tier cannot satisfy -d gate{_layout_note(doe_clone)}")

        cold_bare_path = _cold_bare_path()

        # Mirrors bash `PATH="$_cold_bare_path" command -v claude-home` — MUST
        # search within the restricted cold_bare_path, not the process's own
        # (unrestricted) PATH, or this check is vacuously true on any machine
        # (found the real bug during byte-parity verification against the
        # bash oracle: an earlier draft called shutil.which() unfiltered).
        #
        # `machine-local` is probed alongside `claude-home` because it, not
        # `claude-home`, is the binary resolve_coordinator_clone actually spawns
        # for its registry rung (`_machine_local_get`). Probing only claude-home
        # asserted the absence of a binary this leg never consults, so the row
        # could pass while the registry tier was wide open.
        cold_registry_bins = {
            name: shutil.which(name, path=cold_bare_path)
            for name in ("machine-local", "claude-home")
        }
        reachable = {n: h for n, h in cold_registry_bins.items() if h}
        if not reachable:
            r.ok(f"cold PATH: machine-local/claude-home both absent from {cold_bare_path!r} (registry read correctly blocked for cold-tier test)")
        else:
            r.info(f"cold PATH: registry binaries still reachable ({reachable}) — cold-tier test may hit the registry tier instead of the pointer tier (expected where these sit in a system PATH dir)")

        cold_env_vars = {
            k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "COORDINATOR_ROOT", "COORDINATOR_CLONE")
        }
        cold_env_vars["PATH"] = cold_bare_path
        cold_env_vars["CLAUDE_HOME"] = cold_env

        rc_content, cold_content_out_or_err = _call_resolve_coordinator_clone("content", cold_env_vars)
        cold_content_out = cold_content_out_or_err if rc_content == 0 else ""
        expected_cold_content = f"{doe_clone}/coordinator"
        if _paths_equal(cold_content_out, expected_cold_content):
            r.ok(f"AC6(a): resolve_content_root() cold (pointer-only): returned {doe_clone}/coordinator")
        elif not cold_content_out:
            r.bad(f"AC6(a): resolve_content_root() cold: returned empty (error: {cold_content_out_or_err})")
        else:
            r.bad(f"AC6(a): resolve_content_root() cold: got '{cold_content_out}', expected '{expected_cold_content}'{_layout_note(doe_clone)}")

        rc_gitops, cold_gitops_out_or_err = _call_resolve_coordinator_clone("clone", cold_env_vars)
        cold_gitops_out = cold_gitops_out_or_err if rc_gitops == 0 else ""
        expected_cold_gitops = doe_clone
        if _paths_equal(cold_gitops_out, expected_cold_gitops):
            r.ok(f"AC6(b): resolve_clone_root() cold (pointer-only): returned {doe_clone} (.git confirmed present)")
        elif not cold_gitops_out:
            r.bad(f"AC6(b): resolve_clone_root() cold: returned empty (error: {cold_gitops_out_or_err})")
        elif _paths_equal(cold_gitops_out, os.path.join(doe_clone, "coordinator")):
            r.bad("AC6(b): resolve_clone_root() cold: returned coordinator/ subdir — coordinator/.git absent under maximalist; mode-split violated")
        else:
            r.bad(f"AC6(b): resolve_clone_root() cold: got '{cold_gitops_out}', expected '{expected_cold_gitops}'{_layout_note(doe_clone)}")
    else:
        r.skip("resolver cold-tier tests (clone path not resolved)")


# ---------------------------------------------------------------------------
# unit5 — Tier 1c: publish-repo clean-install parity (F8)
# ---------------------------------------------------------------------------


def _tier1c_publish_repo_parity(
    r: Reporter,
    sandbox: str,
    doe_clone: str,
    doe_clone_resolved: bool,
) -> None:
    r.section("=== Tier 1c: Publish-repo clean-install parity (F8 — parameterization contract) ===")

    if not doe_clone_resolved:
        r.skip("F8 publish-repo clean-install parity (clone path not resolved)")
        return

    pub_root = os.path.join(sandbox, "publish-repo-check")
    pub_clone = os.path.join(pub_root, "coordinator-claude")
    os.makedirs(os.path.join(pub_clone, "coordinator", "hooks"), exist_ok=True)
    os.makedirs(os.path.join(pub_clone, ".git"), exist_ok=True)

    oracle_hooks_json = os.path.join(doe_clone, "coordinator", "hooks", "hooks.json")
    pub_hooks_json = os.path.join(pub_clone, "coordinator", "hooks", "hooks.json")
    if os.path.isfile(oracle_hooks_json):
        shutil.copy2(oracle_hooks_json, pub_hooks_json)

    # Two distinct conditions used to share one FAIL message: the sandbox build
    # itself failing (a real defect here) and the ORACLE INPUT this fixture
    # copies from — <clone>/coordinator/hooks/hooks.json — not existing on this
    # host at all, which is what happens on every flat published-mirror clone
    # (its hooks.json is at <clone>/hooks/hooks.json). Reporting the second as
    # "clone build failed" sent the reader looking for a bug in the sandbox
    # builder for a condition the builder never touched.
    if not os.path.isdir(os.path.join(pub_clone, ".git")):
        r.bad(f"F8 setup: publish-repo-shaped sandbox clone build failed — .git not created at {pub_clone}")
    elif not os.path.isfile(pub_hooks_json):
        r.unevaluable(
            f"F8 setup: no hooks.json to seed the publish-repo fixture with — the oracle input "
            f"{oracle_hooks_json} does not exist on this host.{_layout_note(doe_clone)} The F8 "
            f"hook-command rooting assertions below have no content to inspect."
        )
    else:
        r.ok(f"F8 setup: publish-repo-shaped sandbox clone built at {pub_clone} (distinct from $RESOLVED_CLONE={doe_clone})")

    # ---- 15. Settings-hooks generator rooted at publish clone ----
    r.section("--- F8: settings.json hook commands rooted at publish clone ---")
    pub_home = os.path.join(sandbox, "publish-repo-check-home")
    os.makedirs(pub_home, exist_ok=True)
    pub_settings = os.path.join(pub_home, "settings.json")
    Path(pub_settings).write_text("{}", encoding="utf-8", newline="\n")
    env2 = {**os.environ, "CLAUDE_HOME": pub_home, "REPO_CONTENT_ROOT": pub_clone}
    rc2, err2, status_pub = _call_gen_settings_hooks(pub_settings, env2)
    if rc2 == 0:
        r.ok(f"F8: gen_settings_hooks.generate() succeeded against publish clone ({status_pub})")
        pub_hook_cmds: List[str] = []
        try:
            data = json.loads(Path(pub_settings).read_text(encoding="utf-8"))
            for groups in (data.get("hooks") or {}).values():
                for group in groups if isinstance(groups, list) else []:
                    for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                        cmd = (hook or {}).get("command")
                        if cmd:
                            pub_hook_cmds.append(cmd)
        except (json.JSONDecodeError, OSError):
            pub_hook_cmds = []

        if status_pub.startswith("skipped"):
            # Same contract as Step 3.5c: a declined generation writes nothing
            # by design, so there is no command list to root-check. Reporting
            # that as a FAIL asserts the opposite of what the generator decided.
            r.skip(f"F8: hook-command root check — generation declined: {status_pub}")
            if pub_hook_cmds:
                r.bad(
                    f"F8: gen_settings_hooks reported {status_pub!r} but wrote "
                    f"{len(pub_hook_cmds)} hook command(s) — a declined run must write nothing"
                )
        elif not pub_hook_cmds:
            r.bad("F8: settings.json produced no hook commands to inspect against publish clone")
        else:
            if any(_path_mentioned(os.path.join(pub_clone, "coordinator", "hooks") + "/", c) for c in pub_hook_cmds):
                r.ok("F8: settings.json hook commands rooted at publish clone ($_pub_clone/coordinator/hooks/...)")
            else:
                r.bad("F8: settings.json hook commands do NOT reference the publish clone's coordinator/hooks/ path")
            if any(_path_mentioned(os.path.join(doe_clone, "coordinator", "hooks") + "/", c) for c in pub_hook_cmds):
                r.bad("F8: settings.json hook commands leak the real $RESOLVED_CLONE path — clone-hardcoding bug in gen_settings_hooks (ignores REPO_CONTENT_ROOT)")
            else:
                r.ok("F8: settings.json hook commands do NOT leak the real $RESOLVED_CLONE path (no clone-hardcoding)")
    else:
        f8_hooks_err = os.path.join(sandbox, "f8-gen-hooks-err.txt")
        Path(f8_hooks_err).write_text(err2 or "", encoding="utf-8", newline="\n")
        r.bad(f"F8: gen_settings_hooks.generate() failed against publish clone (see {f8_hooks_err})")

    # ---- 16. resolve_coordinator_clone rooted at publish clone ----
    r.section("--- F8: resolve_content_root() rooted at publish clone (mirror live_path contract) ---")
    env3 = {**os.environ, "CLAUDE_PLUGIN_ROOT": f"{pub_clone}/coordinator"}
    rc3, pub_mirror_out_or_err = _call_resolve_coordinator_clone("content", env3)
    pub_mirror_out = pub_mirror_out_or_err if rc3 == 0 else ""
    if _paths_equal(pub_mirror_out, os.path.join(pub_clone, "coordinator")):
        r.ok(f"F8: resolve_content_root() rooted at publish clone: {pub_clone}/coordinator")
    else:
        r.bad(f"F8: resolve_content_root() did not root at publish clone — got '{pub_mirror_out}', expected '{pub_clone}/coordinator' (error: {pub_mirror_out_or_err})")

    # ---- 17. Negative control ----
    r.section("--- F8: negative control — assertion discriminates a simulated hardcoded-clone bug ---")
    simulated_hardcoded_output = doe_clone
    if simulated_hardcoded_output != pub_clone:
        r.ok("F8 negative-control: simulated hardcoded-path output ('$RESOLVED_CLONE') correctly fails the '== $_pub_clone' comparison — assertion is selective, not vacuously true")
    else:
        r.bad("F8 negative-control: simulated hardcoded-path output matched the expected publish-clone path — F8 assertions above are NOT selective (would not have caught the bug)")


# ---------------------------------------------------------------------------
# unit6 — summary, Tier 2 deferred banner, orchestration entrypoint
# ---------------------------------------------------------------------------

_TIER2_BANNER = """
=== Tier 2: Running-in-Claude-Code (DEFERRED — manual gate) ===

Per docs/wiki/install-surface-completeness.md § Running-in-Claude-Code:

This tier cannot run inside a subagent. It requires a real Claude Code boot. The EM or PM
must perform these steps manually before declaring the install surface complete (AC-W4.1):

  1. Launch: claude-author --dry-run
     Verify output contains: exec claude --plugin-dir <doe_clone>/coordinator

  2. Launch interactively: claude-author
     Verify in the Claude Code session:
     a. Skill resolution: invoke a coordinator skill (e.g. /repo-setup) and confirm the
        skill base-dir matches <doe_clone>/coordinator (not a cache copy or ~/.claude path).
     b. Hook firing at boot: a fresh boot should fire SessionStart hooks — check the
        mtime of ${CLAUDE_CONFIG_DIR:-~/.claude}/.coordinator-content-root-last-seen
        is at or after this session's launch. Written by run_self_probe
        (coordinator_core/ops/session/guard_hook_generation_self_probe.py), dispatched
        as a StartGuard from the SessionStart hook; a stale mtime means the hook did
        not fire. The file also carries the probe's own verdict= line.
     c. CLAUDE_PLUGIN_ROOT is UNSET in hook env (self-resolution via BASH_SOURCE is active).
     d. No plugin byte-copy at ~/.claude/plugins/coordinator-claude/ (ls should show absent
        or pointer/config only).

  3. Confirm: skills/agents resolve from <doe_clone>/coordinator/skills/ and
     hooks fire from <doe_clone>/coordinator/hooks/ (absolute paths in settings.json).

Source of deference: install-surface-completeness.md § Running-in-Claude-Code:
  "A chain leg is only 'complete' when its Claude Code surface is validated live."
  "Plugins and skills it registers are validated as working — discovery preconditions met
   AND the surface is live, not merely present in settings.json or enabledPlugins."

Note: SessionStart hooks take effect only at the NEXT boot. settings.json hook definitions
hot-reload mid-session, but SessionStart is a boot event — it does NOT fire on settings.json
edits in a running session.
"""


def _tier1d_registry_manifest_integrity(r: Reporter, coordinator_root: str) -> None:
    """Registry manifest present / parsable / required-keys-complete.

    Loads ``coordinator_registry`` by file path and lets its own
    ``_load_manifest()`` (or, on a module that still loads eagerly, its import)
    raise the diagnosis; those three raises are reported verbatim as FAIL. An
    unreachable module is UNEVALUABLE, never a pass-equivalent SKIP."""
    import importlib.util

    r.section("=== Tier 1d: Registry manifest install integrity ===")
    lib_dir = os.path.join(coordinator_root, "bin", "lib") if coordinator_root else ""
    mod_path = os.path.join(lib_dir, "coordinator_registry.py") if lib_dir else ""
    if not mod_path or not os.path.isfile(mod_path):
        r.unevaluable(
            f"registry manifest integrity: coordinator_registry.py not reachable "
            f"(looked at {mod_path or '<no coordinator root resolved>'})"
        )
        return

    saved_path = list(sys.path)
    name = "_sandbox_check_coordinator_registry"
    try:
        sys.path.insert(0, lib_dir)
        spec = importlib.util.spec_from_file_location(name, mod_path)
        if spec is None or spec.loader is None:
            r.unevaluable(f"registry manifest integrity: no import spec for {mod_path}")
            return
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            loader = getattr(module, "_load_manifest", None)
            if callable(loader):
                loader()
        except (FileNotFoundError, ValueError) as exc:
            r.bad(f"registry manifest integrity: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — not one of the three manifest diagnoses
            r.unevaluable(
                f"registry manifest integrity: could not evaluate — "
                f"{type(exc).__name__} loading {mod_path}: {exc}"
            )
            return
        r.ok(f"registry manifest present, parsable, required keys complete ({mod_path})")
    finally:
        sys.path[:] = saved_path
        sys.modules.pop(name, None)


class SandboxCheckTransportError(RuntimeError):
    """Raised when the harness itself could not run (sandbox creation
    failure, unhandled exception mid-tier) — distinct from a business
    check FAIL. Maps to the dedicated exit code 3 (module docstring § Exit-
    code contract, addendum rule 3b)."""


def run_all(
    coordinator_root_override: Optional[str] = None,
    keep_sandbox: bool = False,
) -> Tuple[Reporter, str]:
    """Orchestrates all tiers. Returns ``(reporter, sandbox_path)``. Raises
    :class:`SandboxCheckTransportError` if the sandbox itself cannot be
    created — everything else degrades to a reported FAIL/SKIP, never an
    uncaught exception, per the module's fail-loud-only-at-the-harness-
    boundary contract."""
    r = Reporter()

    doe_clone, doe_clone_resolved = resolve_doe_clone()
    if not doe_clone_resolved:
        r.bad("repos.content_root not resolved (set REPO_CONTENT_ROOT or seed via machine-local set repos.content_root)")
        r.info("Skipping clone-dependent checks.")
    else:
        r.info(f"clone resolved: {doe_clone}")

    coordinator_root = (coordinator_root_override or "").rstrip("/") or (
        f"{doe_clone}/coordinator" if doe_clone else ""
    )

    try:
        tmp_base = tempfile.gettempdir()  # addendum A4 — never os.environ["TMPDIR"] directly
        sandbox = tempfile.mkdtemp(prefix="install-sandbox-check.", dir=tmp_base)
    except OSError as exc:
        raise SandboxCheckTransportError(f"could not create sandbox tempdir under {tempfile.gettempdir()}: {exc}") from exc

    r.info(f"Sandbox CLAUDE_HOME: {sandbox}")

    try:
        doe_coordinator_present = _tier1_filesystem_shape(r, sandbox, doe_clone, doe_clone_resolved, coordinator_root)

        r.section(f"\n=== Tier 1 summary (pre-1b): {r.pass_count} passed, {r.fail_count} failed ===")

        _tier1b_mirror_and_cold_tier(r, sandbox, doe_clone, doe_clone_resolved)
        _tier1c_publish_repo_parity(r, sandbox, doe_clone, doe_clone_resolved)
        _tier1d_registry_manifest_integrity(r, coordinator_root)
    finally:
        if not keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            r.info(f"Sandbox preserved at: {sandbox}")

    r.section(
        f"\n=== Tier 1 summary: {r.pass_count} passed, {r.fail_count} failed, "
        f"{r.unevaluable_count} unevaluable on this host ==="
    )
    print(_TIER2_BANNER)

    return r, sandbox


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    try:
        args, unknown = parser.parse_known_args(argv)
    except SystemExit:
        return 3
    if args.help:
        print(_usage_text(), file=sys.stdout)
        return 0
    if unknown:
        print(f"ERROR: Unknown argument: {unknown[0]}", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        return 3

    try:
        r, _sandbox = run_all(
            coordinator_root_override=args.coordinator_root_override,
            keep_sandbox=args.keep_sandbox,
        )
    except SandboxCheckTransportError as exc:
        print(f"install-sandbox-check: TRANSPORT FAILURE: {exc}", file=sys.stderr)
        print("  Remediation: verify the temp filesystem is writable and has free space.", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 — harness boundary, convert to dedicated code
        # A verification gate that crashes is indistinguishable from one that
        # fails, and worse: every PASS/FAIL it had already established was
        # discarded with the exception. Surface the traceback so the crash site
        # is actionable from the run itself, then still exit 3 (checks did not
        # complete) rather than 1 (checks ran, some failed).
        print(f"install-sandbox-check: TRANSPORT FAILURE: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print(
            "  The run aborted mid-checks; results printed above this line are partial.",
            file=sys.stderr,
        )
        return 3

    if r.fail_count > 0:
        return 1
    if r.unevaluable_count > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
