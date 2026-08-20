"""
coordinator_core.install.sandbox_check — sandbox clean-install shape validator.

Port of: ``coordinator/bin/install-sandbox-check.sh`` (DoE b5a4192c,
2026-07-20) [DoE-claude repo] (BIG_PORT Wave C, item ``install-sandbox-check``,
971 LOC oracle).
Purpose (unchanged from bash): exercises the W4.1 install steps (DoE clone,
``claude-doe`` wrapper, ``gen-settings-hooks`` seeding, ``.doe-root`` pointer,
``claude-doe-shim.sh``, resolver cold-tier, publish-repo parameterization)
against an isolated sandbox ``CLAUDE_HOME`` and asserts the resulting thin-
``~/.claude`` + cloned-DoE shape. Validates Tier 1 (filesystem) of the
install-surface-completeness contract; Tier 2 (running-in-Claude-Code) is
printed as a DEFERRED manual-gate banner, unchanged in spirit from the oracle.

FAMILY-I (fresh-install surface): on a cold machine ``REPO_DOE_CLAUDE`` /
``machine-local`` may be unresolvable — every unresolved-clone branch below
degrades to a SKIP with actionable remediation text, never a hard crash, and
:func:`main` prints a dedicated engine-root-resolution remediation block if
the claude-klabauter link itself cannot be established (the trampoline's own concern,
not this module's — this module assumes it is already running IN claude-klabauter).

Of the dependency scripts this validator drives, only ``claude-doe`` remains
a genuine subprocess-exec of a DoE-owned artifact: it has no native claude-klabauter
peer, and its OWN dry-run/exec-line behavior is what checks 7/7b/F5 assert
on — there is no "port" of a wrapper whose entire job is to be invoked as a
standalone binary. ``claude-doe`` was PURE BASH on disk at the time this
module was first ported (confirmed via ``file(1)``); DoE-claude has since
ported it to python3 (shebang ``#!/usr/bin/env python3``), so the invocation
below (check 7) uses ``sys.executable`` directly rather than an explicit
``bash`` spawn — feeding Python source to a bash interpreter fails outright
(``from: command not found``). No dependency-script bash-spawn remains at
check 7/7b: the F5 standalone-copy call (check 7b) already invoked the
copied wrapper directly via shebang-exec (``[f5_wrapper, "--dry-run"]``,
never explicit ``bash``) and needed no change. The one bash spawn that DOES
remain in this module (:func:`_tier1b_mirror_and_cold_tier`'s cold-tier
shim probe, ``["bash", "-c", "source '<shim>.sh' ..."]``) is a SANCTIONED
carve-out (e) live-shell-environment artifact under test, by PM ruling
2026-07-22 (commit ``09d6c382``) — see CLAUDE.md § Runtime conventions
carve-out (e), which names :func:`_tier1b_mirror_and_cold_tier` as that
class's sole site. Do NOT re-assert carve-out (d) interpreter/shell
self-probe for this site: a prior classification under (d) was WRONG on two
independent grounds (see
``state/review-trail/findings/2026-07-22-adjudication-bash-carveout-7-13.md``
§1, §3 for the full argument — that refutation stands; it is not what the
(e) ruling revisits) — (1) carve-out (d) membership is by enumeration and
its ``Sites:`` list names only ``first_run.py`` ``_bash_version_ok`` and
``normalize_env.py`` ``_ne_verify_bash_profile_repair``; this site was never
named there; (2) on the merits, ``source '<shim>'`` hands bash an external
``.sh`` file's *logic* to execute — the shim is the subject under test, not
bash itself — which is exactly what (d)'s anti-loophole clause excludes.
Carve-out (e) instead names the *live shell environment* applying the shim
as the subject under test, not the shim's logic in isolation — a distinct
rationale from (d), not a re-run of it. The shim this probe sources
(``claude-doe-shim.sh``) is itself emitted by claude-klabauter's own
:mod:`coordinator_core.ops.gen_claude_doe_shim` op, which copies a
DoE-authored *template*'s bytes verbatim rather than reimplementing the
template body (see that module's own docstring) — so the shim is more
precisely a claude-klabauter-emitted artifact carrying a DoE-template body, not
purely "a DoE artifact," and this provenance is load-bearing for why the
site qualifies under (e): asserting the live-shell-environment behavior of
a byte-verbatim-copied template is the thing under test, not a foreign
script's independent logic. Tracked as the reclassified (was: row #7) entry
in ``state/audits/2026-07-21-pure-python-bash-spawn-audit.md``.

``gen-settings-hooks.sh`` (DoE a2078a9b, 2026-07-22),
``gen-doe-root-pointer.sh`` (DoE b5a4192c, 2026-07-20),
``gen-claude-doe-shim.sh`` (DoE b5a4192c, 2026-07-20), and
``resolve-coordinator-clone.sh`` (DoE 290997c7, 2026-07-22)
[DoE-claude repo] are the FOUR bridges this module used to subprocess-exec
(``bash <script> ...``, relying on the first three being sh/python polyglot
trampolines) and now calls **in-process** instead, against claude-klabauter's own
native peer modules — :mod:`coordinator_core.install.gen_settings_hooks`,
:mod:`coordinator_core.ops.gen_doe_root_pointer`,
:mod:`coordinator_core.ops.gen_claude_doe_shim`, and
:mod:`coordinator_core.resolve_coordinator_clone` respectively (see
:func:`_call_gen_settings_hooks`, :func:`_call_gen_doe_root_pointer`,
:func:`_call_gen_claude_doe_shim`, :func:`_call_resolve_coordinator_clone`,
and the paired ``_assert_*_interface`` functions). Each DoE trampoline's only
job was to import and call the same claude-klabauter-owned module this validator now
calls directly — subprocess-exec'ing it was a circular, Windows-costly
(~326ms shim tax measured for gen-settings-hooks.sh alone) round-trip through
``bash``/``sh`` PATH-probing back into this repo's own Python, and (for the
other three) a dependency on DoE `.sh` files being present/landed at all
before this validator's OWN in-tree logic could be exercised. Repointed per
``cross-repo/inbox/2026-07-20-claude-central-em-sandbox-check-execs-doe-shell-blocks-deletion.md``
and its correction
``cross-repo/inbox/2026-07-20-claude-central-em-sandbox-check-doe-fix-blocks-deletion-correction.md``
(the correction is authoritative for gen-settings-hooks specifically: Step
3.5c/F8 were independently already red on a registry-less sandbox
``CLAUDE_HOME`` before that repoint, for a reason the repoint does NOT fix —
see the correction memo's rc=3 ``CLAUDE_KLABAUTER_ROOT``-resolution finding); the same
in-process pattern is extended here to the other three bridges as the
general "reimplement native, do not subprocess a DoE oracle" doctrine for
this plan (`docs/plans/2026-07-21-claude-klabauter-pure-python-shop-retire-all-bash.md`
§ Decisions).

Flagged behavioral broadening (not silent): because the pointer/shim/resolver
calls no longer require the corresponding DoE ``.sh`` file to exist on disk,
checks that used to report FAIL "not found (Cn not yet landed — expected
RED)" now exercise the real native logic directly and can PASS even against
a DoE clone that has not yet shipped those trampolines. This is the intended
outcome of the port (the native module IS the authoritative implementation
now, matching :mod:`coordinator_core.resolve_coordinator_clone`'s own
"reimplement-native" docstring), not a masked regression — the shim/pointer
TEMPLATE file (``coordinator/templates/shell/claude-doe-shim.sh.tmpl``) still
must exist, since ``gen_claude_doe_shim`` has no default template of its own
and fails loud without one.

Unit decomposition (per porter brief):
    unit1 — :func:`_resolve_doe_clone`, :class:`Reporter`, :func:`_run`, arg parse
    unit2 — :func:`_tier1_filesystem_shape` (checks 1-7b)
    unit3 — Tier1b banner (folded into :func:`_tier1b_maximalist_shape` header)
    unit4a — :func:`_tier1b_pointer_and_shim` (checks 8-9, live-artifact baseline)
    unit4b — :func:`_tier1b_mirror_and_cold_tier` (checks 10-13)
    unit5 — :func:`_tier1c_publish_repo_parity` (F8, checks 14-17)
    unit6 — :func:`run_all` (summary), :func:`_tier2_deferred_banner`, :func:`main`

Exit-code contract (addendum rule 3b — fail-loud VALIDATOR class):
    0  all assertions passed (or gracefully skipped — SKIP is not a failure)
    1  one or more assertions FAILed (business outcome — matches bash oracle)
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
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core import resolve_coordinator_clone
from coordinator_core.install import gen_settings_hooks
from coordinator_core.ops import gen_claude_doe_shim, gen_doe_root_pointer
from coordinator_core.win_portability import is_executable, no_console_creationflags

#: Named so `_sep_norm` reads without an escape-in-an-escape.
BACKSLASH = chr(92)

# Generator-provenance declaration (generator_provenance.py). Every write in
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
    `X:{bs}DoE-claude{bs}coordinator` != `X:{bs}DoE-claude/coordinator` and
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


def _claude_doe_wrapper_src() -> str:
    """Absolute path to the `claude-doe` wrapper this validator installs and
    dry-runs.

    It lives in THIS repo (`<claude-klabauter>/coordinator/bin/claude-doe.py`), not under
    the `--coordinator-root` DoE tree — `<doe>/coordinator/bin/` has no
    `claude-doe*` file at all. Probing the DoE tree made the wrapper checks FAIL
    on a correctly-installed machine and then crashed the whole run: the F5 leg
    `shutil.copy2`d the same missing path with no existence guard, so an
    unhandled FileNotFoundError replaced every check result with a transport
    failure. Self-location is the RIGHT resolver here precisely because the
    wrapper is this repo's own artifact — the opposite of `templates/`, which
    is the DoE clone's and must come from `--coordinator-root`.
    """
    return str(Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "claude-doe.py")


def _python_launch(script: str, *args: str) -> List[str]:
    """argv for running a `.py`/extensionless Python entrypoint. Never exec the
    file directly: the sandbox copies land as extensionless `claude-doe`, which
    Windows cannot execute (no shebang honoring, no PATHEXT match) — that path
    reported rc=127 as a wrapper defect on every Windows run."""
    return [sys.executable, script, *args]


def resolve_doe_clone() -> Tuple[str, bool]:
    """Mirrors bash lines 79-98. Order: ``REPO_DOE_CLAUDE`` env, then
    ``machine-local get repos.doe_claude`` (sibling-of-self or PATH). Returns
    ``("", False)`` on total failure — NEVER raises (fresh-install machines
    routinely fail to resolve this; every downstream check degrades to SKIP,
    per the FAMILY-I contract in the module docstring)."""
    doe_clone = os.environ.get("REPO_DOE_CLAUDE", "")
    if doe_clone:
        return doe_clone, True

    ml = _which("machine-local")
    if ml:
        cp = _run([ml, "get", "repos.doe_claude"], timeout=15)
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
        help="Override <doe_clone>/coordinator resolution (the claude-doe trampoline "
        "resolves the default via doe_root(), NOT its own script-dir "
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
        "                           by the claude-doe trampoline via doe_root(), not its own\n"
        "                           script-dir location).\n"
        "  -h, --help               Show this usage.\n"
        "\n"
        "Environment:\n"
        "  REPO_DOE_CLAUDE  Path to the resolved clone (primary resolution override).\n"
        "                   Falls back to: machine-local get repos.doe_claude.\n"
        "                   At least one must be set for the clone checks to run.\n"
        "\n"
        "Exit codes:\n"
        "  0  all assertions passed (or gracefully skipped)\n"
        "  1  one or more assertions FAILed\n"
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
            r.skip("clone's coordinator/ dir absent — W4.2 cutover not yet completed (expected pre-cutover)")
            r.info("  coordinator/ will be populated after the W4.2 source-relocation cutover.")
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
    wrapper_src = _claude_doe_wrapper_src()
    wrapper_src_present = os.path.isfile(wrapper_src)
    if not wrapper_src_present:
        r.bad(f"claude-doe wrapper not found at: {wrapper_src}")
    else:
        r.ok(f"claude-doe wrapper source present: {wrapper_src}")
        sandbox_local_bin = os.path.join(sandbox, ".local", "bin")
        sandbox_wrapper = os.path.join(sandbox_local_bin, "claude-doe")
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
            r.ok(f"claude-doe wrapper installed: {sandbox_wrapper}")
        else:
            r.bad(f"claude-doe wrapper install failed (missing or exec-bit not set): {sandbox_wrapper}")

    # 6. gen-settings-hooks seeding
    r.section("--- Step 3.5c: settings.json hook block seed ---")
    if not doe_coordinator_present:
        r.ok("gen_settings_hooks module available (in-process call, no bash subprocess)")
        r.skip("gen-settings-hooks live seed (clone's coordinator/ absent pre-W4.2 — will run post-cutover)")
        r.info(f"  Seeding will succeed after W4.2 populates {doe_clone}/coordinator/")
    else:
        _assert_gen_settings_hooks_interface(r)

        sandbox_settings = os.path.join(sandbox, "settings.json")
        Path(sandbox_settings).write_text("{}", encoding="utf-8", newline="\n")

        # COORDINATOR_SETTINGS_HOME must be pinned into the sandbox, not merely
        # inherited: since 2026-07-28 gen_doe_root_pointer writes
        # <settings-home>/machine-local/.doe-root, so an inherited real
        # COORDINATOR_SETTINGS_HOME would redirect this sandbox write onto the
        # LIVE pointer. CLAUDE_HOME alone no longer confines it.
        env = {
            **os.environ,
            "CLAUDE_HOME": sandbox,
            "COORDINATOR_SETTINGS_HOME": os.path.join(sandbox, ".coordinator-claude-settings"),
            "REPO_DOE_CLAUDE": doe_clone,
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

    # 7. claude-doe --dry-run
    r.section("--- Step 3.5b dry-run: claude-doe --dry-run ---")
    if doe_clone_resolved and doe_coordinator_present:
        # CLAUDE_DOE_GEN_HOOKS_SCRIPT is NOT set here: it was claude-doe's
        # per-launch self-heal hook (regenerate settings.json's hook block
        # on every launch via gen-settings-hooks.sh) — see that wrapper's own
        # 2026-07-20 docstring note. That self-heal call site was removed
        # from claude-doe entirely (hook seeding is install-time-only now,
        # not a per-launch concern), so claude-doe no longer reads this env
        # var at all; setting it to a path that no longer exists on disk
        # (gen-settings-hooks.sh is retired repo-wide) asserted nothing.
        env = {
            **os.environ,
            "CLAUDE_HOME": sandbox,
            "REPO_DOE_CLAUDE": doe_clone,
        }
        cp = _run(_python_launch(wrapper_src, "--dry-run"), env=env)
        dryrun_err = os.path.join(sandbox, "dryrun-err.txt")
        Path(dryrun_err).write_text(cp.stderr or "", encoding="utf-8", newline="\n")
        if cp.returncode == 0:
            dry_out = cp.stdout
            if "exec claude --plugin-dir" in dry_out:
                r.ok("claude-doe --dry-run emitted exec line with --plugin-dir")
            else:
                r.bad(f"claude-doe --dry-run output missing 'exec claude --plugin-dir' (got: {dry_out})")
            if _path_mentioned(os.path.join(doe_clone, "coordinator"), dry_out):
                r.ok("claude-doe --dry-run exec line references clone's coordinator dir")
            else:
                r.bad("claude-doe --dry-run exec line does not reference clone's coordinator dir")
        else:
            r.bad(f"claude-doe --dry-run exited non-zero (see {dryrun_err})")
    elif not doe_clone_resolved:
        r.skip("claude-doe --dry-run (clone path not resolved)")
    else:
        r.skip("claude-doe --dry-run (clone's coordinator/ dir absent pre-W4.2 — runs post-cutover)")

    # 7b. F5 regression: standalone-copy
    r.section("--- F5 regression: claude-doe standalone-copy --dry-run (siblings absent) ---")
    if doe_clone_resolved and doe_coordinator_present and wrapper_src_present:
        f5_dir = os.path.join(sandbox, "f5-standalone-bin")
        os.makedirs(f5_dir, exist_ok=True)
        f5_wrapper = os.path.join(f5_dir, "claude-doe")
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
            r.ok(f"F5 regression setup: standalone dir has claude-doe ALONE (no siblings): {f5_dir}")
            env = {**os.environ, "REPO_DOE_CLAUDE": doe_clone}
            env.pop("CLAUDE_HOME", None)
            cp = _run(_python_launch(f5_wrapper, "--dry-run"), env=env)
            f5_err = os.path.join(sandbox, "f5-dryrun-err.txt")
            Path(f5_err).write_text(cp.stderr or "", encoding="utf-8", newline="\n")
            if cp.returncode == 0:
                dry_out = cp.stdout
                if "exec claude --plugin-dir" in dry_out:
                    r.ok("F5: standalone-copy claude-doe --dry-run emitted exec line with --plugin-dir")
                else:
                    r.bad(f"F5: standalone-copy claude-doe --dry-run output missing 'exec claude --plugin-dir' (got: {dry_out})")
                if _path_mentioned(os.path.join(doe_clone, "coordinator"), dry_out):
                    r.ok("F5: standalone-copy claude-doe --dry-run exec line references clone's coordinator dir")
                else:
                    r.bad("F5: standalone-copy claude-doe --dry-run exec line does not reference clone's coordinator dir")
            else:
                r.bad(f"F5: standalone-copy claude-doe --dry-run exited non-zero (see {f5_err})")
    elif not doe_clone_resolved:
        r.skip("F5 regression: standalone-copy claude-doe --dry-run (clone path not resolved)")
    elif not wrapper_src_present:
        r.skip(f"F5 regression: standalone-copy claude-doe --dry-run (wrapper source absent: {wrapper_src})")
    else:
        r.skip("F5 regression: standalone-copy claude-doe --dry-run (clone's coordinator/ dir absent pre-W4.2 — runs post-cutover)")

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
    reads ``CLAUDE_HOME`` / ``REPO_DOE_CLAUDE`` / ``COORDINATOR_ROOT`` from
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


def _assert_gen_doe_root_pointer_interface(r: Reporter) -> None:
    """Real interface assertion against the in-process
    ``coordinator_core.ops.gen_doe_root_pointer`` module — replaces the
    former ``bash -n``/``py_compile`` syntax checks of the DoE
    ``gen-doe-root-pointer.sh`` polyglot, which become meaningless once the
    call site no longer reads that file at all. Asserts the module exposes
    a callable ``main(argv)`` (the ``--check-only`` contract the call sites
    below depend on — see :func:`_call_gen_doe_root_pointer`)."""
    main_fn = getattr(gen_doe_root_pointer, "main", None)
    if not callable(main_fn):
        r.bad("coordinator_core.ops.gen_doe_root_pointer has no callable main() (interface contract broken)")
    else:
        r.ok("gen_doe_root_pointer.main() callable (interface contract verified)")


def _call_gen_doe_root_pointer(argv: List[str], env: Dict[str, str]) -> Tuple[int, str]:
    """In-process call to :func:`gen_doe_root_pointer.main`, replacing the
    former ``bash gen-doe-root-pointer.sh [--check-only]`` subprocess spawn.
    Overlays ``env`` onto the current process's ``os.environ`` for the
    duration of the call and restores the prior environment afterward
    (save/restore, mirrors :func:`_call_gen_settings_hooks`). Returns
    ``(rc, stderr_text)`` — same shape the ``subprocess.CompletedProcess``
    call sites already branch on."""
    saved_env = dict(os.environ)
    stderr_buf = io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(env)
        with contextlib.redirect_stderr(stderr_buf):
            rc = gen_doe_root_pointer.main(argv)
        return rc, stderr_buf.getvalue()
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def _assert_gen_claude_doe_shim_interface(r: Reporter) -> None:
    """Real interface assertion against the in-process
    ``coordinator_core.ops.gen_claude_doe_shim`` module — replaces the
    former ``bash -n``/``py_compile`` syntax checks of the DoE
    ``gen-claude-doe-shim.sh`` polyglot. Asserts the module exposes a
    callable ``main(argv)`` (see :func:`_call_gen_claude_doe_shim`)."""
    main_fn = getattr(gen_claude_doe_shim, "main", None)
    if not callable(main_fn):
        r.bad("coordinator_core.ops.gen_claude_doe_shim has no callable main() (interface contract broken)")
    else:
        r.ok("gen_claude_doe_shim.main() callable (interface contract verified)")


def _call_gen_claude_doe_shim(argv: List[str], env: Dict[str, str]) -> Tuple[int, str]:
    """In-process call to :func:`gen_claude_doe_shim.main`, replacing the
    former ``bash gen-claude-doe-shim.sh [...]`` subprocess spawn. Same
    save/restore ``os.environ`` overlay as :func:`_call_gen_doe_root_pointer`.
    Returns ``(rc, stderr_text)``."""
    saved_env = dict(os.environ)
    stderr_buf = io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(env)
        with contextlib.redirect_stderr(stderr_buf):
            rc = gen_claude_doe_shim.main(argv)
        return rc, stderr_buf.getvalue()
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


# ---------------------------------------------------------------------------
# unit4a — Tier 1b: pointer + shim artifacts, live-artifact baseline
# ---------------------------------------------------------------------------


def _tier1b_pointer_and_shim(
    r: Reporter,
    sandbox: str,
    doe_clone: str,
    doe_clone_resolved: bool,
    coordinator_root: str,
) -> Tuple[bool, bool, str, str]:
    """Returns ``(pointer_section_ran, shim_section_ran, live_doe_root_bak, live_shim_bak)``."""
    r.section(
        "=== Tier 1b: Maximalist install shape (pointer/shim/resolver — native in-process) ==="
    )

    home = os.environ.get("HOME", str(Path.home()))
    live_doe_root = os.path.join(
        os.environ.get("COORDINATOR_SETTINGS_HOME")
        or os.path.join(home, ".coordinator-claude-settings"),
        "machine-local",
        ".doe-root",
    )
    live_shim_file = os.path.join(home, ".claude", "shell", "claude-doe-shim.sh")
    live_doe_root_bak = os.path.join(sandbox, ".live-doe-root.bak")
    live_shim_bak = os.path.join(sandbox, ".live-shim.bak")

    if os.path.isfile(live_doe_root):
        shutil.copy2(live_doe_root, live_doe_root_bak)
    if os.path.isfile(live_shim_file):
        shutil.copy2(live_shim_file, live_shim_bak)

    # ---- 8. Pointer artifact ----
    r.section("--- Pointer artifact (.doe-root) ---")
    pointer_section_ran = False

    _assert_gen_doe_root_pointer_interface(r)

    if doe_clone_resolved:
        env = {**os.environ, "CLAUDE_HOME": sandbox, "REPO_DOE_CLAUDE": doe_clone}
        rc, err = _call_gen_doe_root_pointer([], env)
        gp_err = os.path.join(sandbox, "gen-pointer-err.txt")
        Path(gp_err).write_text(err or "", encoding="utf-8", newline="\n")
        if rc == 0:
            r.ok("gen_doe_root_pointer.main() exited 0 against sandbox")
            pointer_section_ran = True
        else:
            r.bad(f"gen_doe_root_pointer.main() failed (see {gp_err})")

        sandbox_doe_root = os.path.join(
            sandbox, ".coordinator-claude-settings", "machine-local", ".doe-root"
        )
        if os.path.isfile(sandbox_doe_root):
            r.ok(f".doe-root pointer present in sandbox: {sandbox_doe_root}")
            pointer_content = Path(sandbox_doe_root).read_text(encoding="utf-8", errors="replace").rstrip("\n")
            if pointer_content == doe_clone:
                r.ok(f".doe-root content matches registry repos.doe_claude: {doe_clone}")
            else:
                r.bad(f".doe-root content mismatch: got '{pointer_content}', expected '{doe_clone}'")
        else:
            r.bad(f".doe-root not created in sandbox (expected at: {sandbox_doe_root})")

        if os.path.isfile(sandbox_doe_root):
            ptr_bak = os.path.join(sandbox, ".doe-root.bak-checkonly")
            shutil.copy2(sandbox_doe_root, ptr_bak)
            rc_check, _err_check = _call_gen_doe_root_pointer(["--check-only"], env)
            if rc_check == 0:
                if Path(ptr_bak).read_bytes() == Path(sandbox_doe_root).read_bytes():
                    r.ok("gen_doe_root_pointer.main() --check-only: sandbox .doe-root byte-unchanged (dry-run-safe)")
                else:
                    r.bad("gen_doe_root_pointer.main() --check-only: sandbox .doe-root was mutated (dry-run violation)")
            else:
                r.bad("gen_doe_root_pointer.main() --check-only exited non-zero")

        if os.path.isfile(sandbox_doe_root):
            ptr_v1_bak = os.path.join(sandbox, ".doe-root.bak-idem")
            shutil.copy2(sandbox_doe_root, ptr_v1_bak)
            rc_idem, _err_idem = _call_gen_doe_root_pointer([], env)
            if rc_idem == 0:
                if Path(ptr_v1_bak).read_bytes() == Path(sandbox_doe_root).read_bytes():
                    r.ok("gen_doe_root_pointer.main() idempotent: second run — no pointer churn")
                else:
                    r.bad("gen_doe_root_pointer.main() NOT idempotent: second run changed .doe-root content")
            else:
                r.bad("gen_doe_root_pointer.main() failed on second (idempotency) run")
    else:
        r.skip("pointer sandbox run (clone path not resolved)")

    # ---- 9. Shim artifact ----
    r.section("--- Shim artifact (claude-doe-shim.sh) ---")
    shim_tmpl = os.path.join(coordinator_root, "templates", "shell", "claude-doe-shim.sh.tmpl")
    shim_section_ran = False

    _assert_gen_claude_doe_shim_interface(r)

    if not os.path.isfile(shim_tmpl):
        r.bad(f"claude-doe-shim.sh.tmpl not found at: {shim_tmpl} (template missing — expected RED pre-cutover)")
    else:
        r.ok(f"claude-doe-shim.sh.tmpl present: {shim_tmpl}")

        sandbox_rc = os.path.join(sandbox, "sandbox-rc.sh")
        Path(sandbox_rc).write_text("# sandbox-rc\n", encoding="utf-8", newline="\n")

        env = {**os.environ, "CLAUDE_HOME": sandbox, "REPO_DOE_CLAUDE": doe_clone or "", "COORDINATOR_SHIM_RC": sandbox_rc}
        shim_argv = ["--template", shim_tmpl]
        rc, err = _call_gen_claude_doe_shim(shim_argv, env)
        gs_err = os.path.join(sandbox, "gen-shim-err.txt")
        Path(gs_err).write_text(err or "", encoding="utf-8", newline="\n")
        if rc == 0:
            r.ok("gen_claude_doe_shim.main() exited 0 against sandbox")
            shim_section_ran = True
        else:
            r.bad(f"gen_claude_doe_shim.main() failed (see {gs_err})")

        sandbox_shim = os.path.join(sandbox, ".claude", "shell", "claude-doe-shim.sh")
        if os.path.isfile(sandbox_shim):
            r.ok(f"claude-doe-shim.sh present in sandbox: {sandbox_shim}")
            shim_body = Path(sandbox_shim).read_text(encoding="utf-8", errors="replace")

            if "claude()" in shim_body:
                r.ok("claude-doe-shim.sh defines a claude() function")
            else:
                r.bad("claude-doe-shim.sh does not define a claude() function")

            if ".doe-root" in shim_body:
                r.ok("claude-doe-shim.sh references .doe-root (pointer-reading verified)")
            else:
                r.bad("claude-doe-shim.sh does not reference .doe-root (pointer-read missing)")

            home_literal = os.environ.get("HOME", "")
            if home_literal and home_literal in shim_body:
                r.bad(f"claude-doe-shim.sh contains hardcoded machine path (literal $HOME='{home_literal}' found in body)")
            else:
                r.ok("claude-doe-shim.sh: no hardcoded machine path ($HOME literal absent from body)")

            import re

            hardcoded_pattern = re.compile(r"^[^#].*(/Users/[a-zA-Z_]|/home/[a-zA-Z_])", re.MULTILINE)
            if hardcoded_pattern.search(shim_body):
                r.bad("claude-doe-shim.sh contains hardcoded /Users/ or /home/ path on non-comment line")
            else:
                r.ok("claude-doe-shim.sh: no hardcoded /Users/ or /home/ paths on non-comment lines")
        else:
            r.bad(f"claude-doe-shim.sh not created in sandbox (expected at: {sandbox_shim})")

        # Count matching LINES, not raw substring occurrences: EXPECTED_SOURCE_LINE
        # itself contains the "claude-doe-shim" token twice (the -f test and the
        # source path both reference claude-doe-shim.sh) -- a substring
        # re.findall() over the whole body over-counts a single, correctly
        # idempotent source line as 2 and would always FALSE-POSITIVE "duplicated
        # source line" (a genuine bug caught while wiring this call in-process;
        # this assertion never ran against real content before, since the DoE
        # .sh bridge was always "not found" in every prior fixture run).
        _source_line_re = re.compile(r"claude-doe-shim|claude_doe_shim")

        def _count_source_lines(text: str) -> int:
            return sum(1 for line in text.split("\n") if _source_line_re.search(line))

        if os.path.isfile(sandbox_rc):
            rc_body = Path(sandbox_rc).read_text(encoding="utf-8", errors="replace")
            source_line_count = _count_source_lines(rc_body)
            if source_line_count == 1:
                r.ok(f"exactly one marked source line in sandbox rc (got: {source_line_count})")
            elif source_line_count == 0:
                r.bad("no source line for claude-doe-shim found in sandbox rc (expected 1)")
            else:
                r.bad(f"multiple source lines for claude-doe-shim in sandbox rc (expected 1, got: {source_line_count})")

        if os.path.isfile(sandbox_rc):
            rc2, _err2 = _call_gen_claude_doe_shim(shim_argv, env)
            if rc2 == 0:
                rc_body2 = Path(sandbox_rc).read_text(encoding="utf-8", errors="replace")
                count2 = _count_source_lines(rc_body2)
                if count2 == 1:
                    r.ok("idempotency: second gen_claude_doe_shim.main() run — still exactly one source line (no duplication)")
                else:
                    r.bad(f"idempotency: second run produced {count2} source lines (expected 1 — duplicated source line)")
            else:
                r.bad("gen_claude_doe_shim.main() failed on second (idempotency) run")

        sandbox_rc_check = os.path.join(sandbox, "sandbox-rc-checkonly.sh")
        Path(sandbox_rc_check).write_text("# sandbox-rc-checkonly\n", encoding="utf-8", newline="\n")
        rc_bak = os.path.join(sandbox, "sandbox-rc-checkonly.bak")
        shutil.copy2(sandbox_rc_check, rc_bak)
        env_check = {**os.environ, "CLAUDE_HOME": sandbox, "REPO_DOE_CLAUDE": doe_clone or "", "COORDINATOR_SHIM_RC": sandbox_rc_check}
        rc_check, _err_check = _call_gen_claude_doe_shim(shim_argv + ["--check-only"], env_check)
        if rc_check == 0:
            if Path(rc_bak).read_bytes() == Path(sandbox_rc_check).read_bytes():
                r.ok("gen_claude_doe_shim.main() --check-only: sandbox rc byte-unchanged (dry-run-safe)")
            else:
                r.bad("gen_claude_doe_shim.main() --check-only: sandbox rc was mutated (dry-run violation)")
        else:
            r.bad("gen_claude_doe_shim.main() --check-only exited non-zero")

    return pointer_section_ran, shim_section_ran, live_doe_root_bak, live_shim_bak


# ---------------------------------------------------------------------------
# unit4b — Tier 1b: mirror verification, cold tier, cold-shell, non-mutation
# ---------------------------------------------------------------------------


def _tier1b_mirror_and_cold_tier(
    r: Reporter,
    sandbox: str,
    doe_clone: str,
    doe_clone_resolved: bool,
    shim_section_ran: bool,
    live_doe_root_bak: str,
    live_shim_bak: str,
) -> None:
    home = os.environ.get("HOME", str(Path.home()))
    live_doe_root = os.path.join(
        os.environ.get("COORDINATOR_SETTINGS_HOME")
        or os.path.join(home, ".coordinator-claude-settings"),
        "machine-local",
        ".doe-root",
    )
    live_shim_file = os.path.join(home, ".claude", "shell", "claude-doe-shim.sh")

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
            r.bad(f"AC5: resolve_coordinator_clone.resolve_content_root(): returned empty or non-existent path (error: {mirror_out_or_err})")
    else:
        r.skip("mirror verification (clone path not resolved)")

    # ---- 11. Resolver cold tier ----
    r.section("--- Resolver cold tier (AC6) ---")
    if doe_clone_resolved:
        cold_env = os.path.join(sandbox, "cold-env")
        os.makedirs(os.path.join(cold_env, ".claude"), exist_ok=True)
        Path(os.path.join(cold_env, ".claude", ".doe-root")).write_text(doe_clone, encoding="utf-8", newline="\n")

        cold_ptr_read = Path(os.path.join(cold_env, ".claude", ".doe-root")).read_text(encoding="utf-8", errors="replace")
        if cold_ptr_read == doe_clone:
            r.ok(f"cold-env .doe-root seeded: {doe_clone}")
        else:
            r.bad(f"cold-env .doe-root setup failed (expected '{doe_clone}', got '{cold_ptr_read}')")

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
            r.bad("clone missing coordinator/ subdir — --for-content pointer tier cannot satisfy -d gate")

        cold_bare_path = "/usr/bin:/bin"
        if os.path.isdir("/opt/homebrew/bin"):
            cold_bare_path += ":/opt/homebrew/bin"
        if os.path.isdir("/usr/local/bin"):
            cold_bare_path += ":/usr/local/bin"

        # Mirrors bash `PATH="$_cold_bare_path" command -v claude-home` — MUST
        # search within the restricted cold_bare_path, not the process's own
        # (unrestricted) PATH, or this check is vacuously true on any machine
        # (found the real bug during byte-parity verification against the
        # bash oracle: an earlier draft called shutil.which() unfiltered).
        cold_has_claude_home = shutil.which("claude-home", path=cold_bare_path)
        if not cold_has_claude_home:
            r.ok("cold PATH: claude-home absent (registry read correctly blocked for cold-tier test)")
        else:
            r.info(f"cold PATH: claude-home still reachable at '{cold_has_claude_home}' — cold-tier test may hit registry tier instead of pointer tier (expected on machines where claude-home is in brew PATH)")

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
            r.bad(f"AC6(a): resolve_content_root() cold: got '{cold_content_out}', expected '{expected_cold_content}'")

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
            r.bad(f"AC6(b): resolve_clone_root() cold: got '{cold_gitops_out}', expected '{expected_cold_gitops}'")
    else:
        r.skip("resolver cold-tier tests (clone path not resolved)")

    # ---- 12. Cold-shell launch ----
    r.section("--- Cold-shell launch: REPO_DOE_CLAUDE from pointer alone (AC2) ---")
    sandbox_shim_path = os.path.join(sandbox, ".claude", "shell", "claude-doe-shim.sh")
    if os.name == "nt":
        # The probe sources a POSIX `.sh` shim under a hand-built cold PATH of
        # /usr/bin:/bin. On Windows that PATH resolves nothing, the `.sh` shim
        # is not the launch surface (the `.cmd`/`.ps1` twins are), and the
        # resulting empty REPO_DOE_CLAUDE was reported as an install defect on
        # every Windows run. Not applicable is not a failure.
        r.skip("AC2 cold-shell (POSIX login-shell seam; not applicable on Windows)")
    elif shim_section_ran and os.path.isfile(sandbox_shim_path):
        cold_path = "/usr/bin:/bin"
        for extra in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/local/opt/bash/bin"):
            if os.path.isdir(extra):
                cold_path += f":{extra}"

        cold_env_vars = {"PATH": cold_path, "CLAUDE_HOME": sandbox, "HOME": home}
        cp = _run(
            ["bash", "-c", f"source '{sandbox_shim_path}' 2>/dev/null; printf '%s' \"${{REPO_DOE_CLAUDE:-}}\""],
            env=cold_env_vars,
        )
        cold_launch_out = cp.stdout.strip()
        if cold_launch_out:
            if _paths_equal(cold_launch_out, doe_clone):
                r.ok(f"AC2 cold-shell: REPO_DOE_CLAUDE resolved from pointer alone: {doe_clone}")
            else:
                r.bad(f"AC2 cold-shell: REPO_DOE_CLAUDE='{cold_launch_out}', expected '{doe_clone}'")
        else:
            r.bad("AC2 cold-shell: REPO_DOE_CLAUDE empty after sourcing shim with cold PATH (pointer-read failed or shim does not export REPO_DOE_CLAUDE)")
    elif not shim_section_ran:
        r.bad("AC2 cold-shell: skipped — gen_claude_doe_shim.main() did not produce a sandbox shim")
    else:
        r.skip("AC2 cold-shell (sandbox shim not generated)")

    # ---- 13. Live-artifact non-mutation ----
    r.section("--- Live-artifact non-mutation (AC4) ---")
    if os.path.isfile(live_doe_root_bak):
        if os.path.isfile(live_doe_root) and Path(live_doe_root_bak).read_bytes() == Path(live_doe_root).read_bytes():
            r.ok("AC4: live ~/.claude/.doe-root byte-unchanged after all sandbox runs")
        else:
            r.bad("AC4: live ~/.claude/.doe-root was MUTATED by a sandbox/check-only run (dry-run violation!)")
    else:
        r.skip("AC4 .doe-root live-non-mutation (live .doe-root absent — C1 not yet installed on this machine)")

    if os.path.isfile(live_shim_bak):
        if os.path.isfile(live_shim_file) and Path(live_shim_bak).read_bytes() == Path(live_shim_file).read_bytes():
            r.ok("AC4: live ~/.claude/shell/claude-doe-shim.sh byte-unchanged after all sandbox runs")
        else:
            r.bad("AC4: live ~/.claude/shell/claude-doe-shim.sh was MUTATED by a sandbox/check-only run (dry-run violation!)")
    else:
        r.skip("AC4 shim live-non-mutation (live shim absent — C2 not yet installed on this machine)")


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

    if os.path.isdir(os.path.join(pub_clone, ".git")) and os.path.isfile(pub_hooks_json):
        r.ok(f"F8 setup: publish-repo-shaped sandbox clone built at {pub_clone} (distinct from $RESOLVED_CLONE={doe_clone})")
    else:
        r.bad(f"F8 setup: publish-repo-shaped sandbox clone build failed at {pub_clone}")

    # ---- 14. Pointer generator rooted at publish clone ----
    r.section("--- F8: .doe-root pointer rooted at publish clone ---")
    pub_home = os.path.join(sandbox, "publish-repo-check-home")
    os.makedirs(pub_home, exist_ok=True)
    # COORDINATOR_SETTINGS_HOME pinned into the sandbox for the same reason as
    # the Tier-1b pointer section: the generator's target is settings-home-rooted
    # since 2026-07-28, so an inherited real value would send this probe's write
    # onto the LIVE pointer instead of the sandbox.
    env = {
        **os.environ,
        "CLAUDE_HOME": pub_home,
        "COORDINATOR_SETTINGS_HOME": os.path.join(pub_home, ".coordinator-claude-settings"),
        "REPO_DOE_CLAUDE": pub_clone,
    }
    rc, err = _call_gen_doe_root_pointer([], env)
    if rc == 0:
        pub_doe_root = os.path.join(
            pub_home, ".coordinator-claude-settings", "machine-local", ".doe-root"
        )
        if os.path.isfile(pub_doe_root):
            pub_pointer_content = Path(pub_doe_root).read_text(encoding="utf-8", errors="replace").rstrip("\n")
            if pub_pointer_content == pub_clone:
                r.ok(f"F8: .doe-root pointer rooted at publish clone (got: {pub_pointer_content})")
            else:
                r.bad(f"F8: .doe-root pointer NOT rooted at publish clone — got '{pub_pointer_content}', expected '{pub_clone}'")
            if pub_pointer_content != doe_clone:
                r.ok("F8: .doe-root pointer does NOT leak the real $RESOLVED_CLONE path (no clone-hardcoding)")
            else:
                r.bad("F8: .doe-root pointer content equals the real $RESOLVED_CLONE path — clone-hardcoding bug in gen_doe_root_pointer (ignores REPO_DOE_CLAUDE)")
        else:
            r.bad(f"F8: .doe-root pointer not created for publish clone (expected at: {pub_doe_root})")
    else:
        f8_err = os.path.join(sandbox, "f8-gen-pointer-err.txt")
        Path(f8_err).write_text(err or "", encoding="utf-8", newline="\n")
        r.bad(f"F8: gen_doe_root_pointer.main() failed against publish clone (see {f8_err})")

    # ---- 15. Settings-hooks generator rooted at publish clone ----
    r.section("--- F8: settings.json hook commands rooted at publish clone ---")
    pub_settings = os.path.join(pub_home, "settings.json")
    Path(pub_settings).write_text("{}", encoding="utf-8", newline="\n")
    env2 = {**os.environ, "CLAUDE_HOME": pub_home, "REPO_DOE_CLAUDE": pub_clone}
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
                r.bad("F8: settings.json hook commands leak the real $RESOLVED_CLONE path — clone-hardcoding bug in gen_settings_hooks (ignores REPO_DOE_CLAUDE)")
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

  1. Launch: claude-doe --dry-run
     Verify output contains: exec claude --plugin-dir <doe_clone>/coordinator

  2. Launch interactively: claude-doe
     Verify in the Claude Code session:
     a. Skill resolution: invoke a coordinator skill (e.g. /repo-setup) and confirm the
        skill base-dir matches <doe_clone>/coordinator (not a cache copy or ~/.claude path).
     b. Hook firing at boot: a fresh boot should fire SessionStart hooks — check the
        session-sentinel file (~/.claude/.session-sentinel) was updated on launch.
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
        r.bad("repos.doe_claude not resolved (set REPO_DOE_CLAUDE or seed via machine-local set repos.doe_claude)")
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

        pointer_ran, shim_ran, live_doe_root_bak, live_shim_bak = _tier1b_pointer_and_shim(
            r, sandbox, doe_clone, doe_clone_resolved, coordinator_root
        )
        _tier1b_mirror_and_cold_tier(
            r, sandbox, doe_clone, doe_clone_resolved, shim_ran, live_doe_root_bak, live_shim_bak
        )
        _tier1c_publish_repo_parity(r, sandbox, doe_clone, doe_clone_resolved)
    finally:
        if not keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            r.info(f"Sandbox preserved at: {sandbox}")

    r.section(f"\n=== Tier 1 summary: {r.pass_count} passed, {r.fail_count} failed ===")
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

    return 1 if r.fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
