"""coordinator_core.bash_guards.tests.test_no_false_clean_on_unparsed_dialect
-- C7 of `docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-
silent.md`: the STRUCTURAL regression test that stops a guard added six
months from now from silently reintroducing the false clean this plan's
C1-C6 chunks closed.

What this discharges (AC1, verbatim): "Static per-guard non-coverage is
carried by ``MATCHERS``... SILENT covers only the dynamic parse-failure
residue on guards that declare PowerShell. ``None`` from a guard that
never declares PowerShell, or from dispatch.py's master gate/malformed-
payload legs, is unaffected and stays out of AC1's scope." Concretely: for
every guard whose own ``MATCHERS`` declares ``"PowerShell"``, a
PowerShell-idiom payload that lands in that guard's detection domain must
produce either a correct verdict (a non-``None`` return) or a recorded
``SILENT`` declaration (`_verdict.record_silent`) -- NEVER a bare ``None``
with nothing recorded. A guard whose ``MATCHERS`` is ``["Bash"]`` (never
declares PowerShell) returning bare ``None`` on a PowerShell payload is
CORRECT behaviour -- its non-coverage is already declared statically by
``MATCHERS``, and is deliberately NOT exercised by the main property test
below (rescoped 2026-08-07 -- the EM's original brief said "every guard
module," which over-read AC1; this file was corrected to AC1's actual
scope after an initial red run against guards that were never in scope).

Guard-module discovery is BY SCAN, not a hardcoded roster
(`pkgutil.iter_modules` over `coordinator_core.bash_guards`) -- a hardcoded
list is exactly what a guard added later would fall outside of, which is
the failure this test exists to prevent. Eligibility for the scan is BY
CONSTRUCTION: a module's public entry point must accept a `payload` dict or
a raw `cmd` string (the two calling conventions live guards use, both
confirmed against every current guard's signature) -- `commit_tripwires.py`
naturally falls outside this filter (its five `check_*` functions take no
command/payload argument at all, per `docs/reference/guard-dialect-coverage
.md` "Modules excluded from the row set" -- though `check_staged_pathspec_
divergence`, line 842, in fact DOES take a `cmd: str` first parameter,
contradicting that doc's blanket claim; kept excluded here per the doc's
authoritative scoping rather than re-litigated, doc inaccuracy noted for
the record rather than silently absorbed). Within that scan, ``MATCHERS``
is read live off each module (never hardcoded) to select the AC1-in-scope
subset.

``TestMatchersConsistency`` is the same property, framed to name a guard
explicitly on mismatch: a guard declaring ``MATCHERS = [..., "PowerShell"]``
that still bare-cleans on a payload landing squarely in its own detection
domain (the identical fixture the guard's own test file uses to prove
DENY/ADVISORY under PowerShell) is a genuine declaration-without-capability
finding, reported by name, never silently patched here.

Spec backlink: pln-guards-reach-a-verdict-on-powe-0e4bc3 § C7
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

import coordinator_core.bash_guards as _bash_guards_pkg
from coordinator_core.bash_guards._dialect import Dialect
from coordinator_core.bash_guards._verdict import collecting, was_silent

#: Modules that are never command guards, excluded structurally rather than
#: content-wise: the dispatcher itself (never a guard) and its checks
#: registry module.  `commit_tripwires` is deliberately NOT listed here --
#: it falls out of the scan by the payload/cmd-signature filter below, per
#: this module's own docstring.
#: `commit_tripwires` is EXPLICITLY excluded here, not left to fall out of
#: the by-construction filter alone -- `docs/reference/guard-dialect-
#: coverage.md` states all five of its `check_*` functions "take no
#: cmd/payload argument at all," but `check_staged_pathspec_divergence`
#: (line 842) in fact takes a `cmd: str` first parameter, so the
#: by-construction filter below would otherwise pick it up. That function
#: is dialect-neutral by construction (a raw regex for a literal `git
#: commit -- <path>` pattern, no `record_silent`/dialect import anywhere
#: in the module) and is invoked from `dispatch_checks.py` as a repo-state
#: check alongside its four siblings, not registered as an independent
#: command guard -- kept excluded per the reference doc's authoritative
#: scoping rather than re-litigated here. Flagged as a doc inaccuracy in
#: this chunk's own report, not corrected in the doc.
_NEVER_A_GUARD = frozenset({"dispatch", "dispatch_checks", "commit_tripwires"})

#: One PowerShell-idiom command per discovered guard, chosen to land in
#: that guard's own detection domain (git-literal, sentinel-basename,
#: destructive-cmdlet, cross-repo-write, etc.) per `docs/reference/
#: guard-dialect-coverage.md`'s row-by-row triage. A lambda so sentinel
#: guards can read their own `_TARGET_BASENAME` off the live module rather
#: than a basename hand-copied here and liable to drift from the guard's
#: own constant.
_PS_COMMAND_FOR: Dict[str, Callable[[Any], str]] = {
    # Positional-target form ("New-Item <target>"), NOT "-Path ...
    # -ItemType File" -- matches each sentinel-creation guard's own
    # PowerShell test class (`TestPowerShellDialect._ps_payload`,
    # `test_new_item_cmdlet_denies`) exactly. The flagged form was
    # confirmed (debug session, this chunk's rescope) to genuinely miss
    # detection for the disarm-marker/worktree-sentinel guards' PS
    # cmdlet matcher -- a fixture artifact in the ORIGINAL test, not a
    # guard defect: the plain form these guards' own authors already
    # wrote reaches detection cleanly.
    "block_approval_sentinel_creation": (
        lambda mod: f"New-Item {mod._TARGET_BASENAME}"
    ),
    "block_disarm_marker_sentinel_creation": (
        lambda mod: f"New-Item {mod._TARGET_BASENAME}"
    ),
    # `Remove-Item <sentinel>`, per this guard's own
    # `TestPowerShellDialect.test_new_item_cmdlet_denies` sibling class
    # (`test_block_dev_repo_sentinel_removal.py`) -- routed to
    # `check_advisory`, NOT `check` (see `_ENTRY_OVERRIDE` below): `check`
    # is a retired dead leg no longer registered in `dispatch.py`
    # (module's own docstring, "not reachable through the live dispatch
    # chain, only directly callable"); `check_advisory` is "the guard's
    # sole registered leg."
    # Unquoted target, matching the guard's own PowerShell test class
    # exactly (`_payload("Remove-Item %s" % SENTINEL, ...)`) -- a quoted
    # target ("...") was confirmed (debug session, this chunk's rescope)
    # to miss this guard's PS matcher; fixture artifact, not a guard
    # defect.
    "block_dev_repo_sentinel_removal": (
        lambda mod: f"Remove-Item {mod._TARGET_BASENAME}"
    ),
    "block_illegal_filename": lambda mod: 'Remove-Item "bad:name.txt"',
    "block_noncanonical_branch_creation": (
        lambda mod: "git checkout -b wip-nonstandard-name"
    ),
    "block_reviewer_bash_outside_allowlist": (
        lambda mod: "Invoke-WebRequest http://example.com"
    ),
    "block_stash_destruction": lambda mod: "git stash clear",
    "block_subagent_commit": lambda mod: 'git commit -am "wip"',
    "block_subagent_destructive_action": (
        # abs-path-ok: synthetic PowerShell fixture text fed to a guard's
        # parser, never resolved/executed against a real filesystem path.
        lambda mod: "Remove-Item -Recurse -Force C:/scratch/target"
    ),
    "block_subagent_plan_body_bash_write": (
        lambda mod: 'Add-Content -Path docs/plans/test.md -Value "x"'
    ),
    "block_subagent_stash_creation": lambda mod: 'git stash push -m "wip"',
    "block_worktree_creation": (
        lambda mod: "git worktree add ../wt-1 feature-branch"
    ),
    "block_worktree_sentinel_creation": (
        lambda mod: f"New-Item {mod._TARGET_BASENAME}"
    ),
    "check_raw_pid_liveness": lambda mod: "Get-Process -Id 12345",
    "check_test_suite_invocation": lambda mod: "pytest tests/",
    # `git checkout -b <canonical-daily-name>` -- content matches this
    # guard's own `TestDeterministicFiring` fixture shape; the hazard-repo
    # gate and the branch-set/ahead-of-main seams that gate detection
    # BEHIND that content match are supplied via `_MONKEYPATCH_FOR` below
    # (this guard's own test file monkeypatches the identical seams).
    "guard_branch_set_precedence": (
        lambda mod: "git checkout -b work/delphipro/2026-08-07"
    ),
    "guard_grep_via_bash": lambda mod: "Select-String -Pattern foo -Path bar.py",
    "guard_inprocess_search": lambda mod: "Select-String -Pattern foo -Path bar.py",
    # `git checkout -b <longlived-shaped-name>` -- content matches this
    # guard's own fixtures; hazard-repo gate supplied via
    # `_MONKEYPATCH_FOR` below (same seam its own test file patches).
    "guard_longlived_branch_naming": lambda mod: "git checkout -b feature/x",
    "guard_multiprobe_banner": (
        # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        lambda mod: "Get-Process; Get-Service; Get-ChildItem C:\\"
    ),
    "guard_plumbing_and_loops": (
        lambda mod: "Get-Content foo.log | Select-Object -First 20"
    ),
    "bump_foreign_repo_write": (
        lambda mod: "cross-repo-memo send --to peer --summary x"
    ),
    "bump_outside_repo_write": (
        # abs-path-ok: synthetic PowerShell fixture text, never resolved.
        lambda mod: "New-Item -Path C:/scratch-outside/file.txt -ItemType File"
    ),
    "guard_head_tail_rewrite": (
        lambda mod: "Get-Content foo.log | Select-Object -First 20"
    ),
    "guard_offer_git_c": lambda mod: "git -C ../other status",
    "guard_offer_invoke_params_stdin": (
        lambda mod: 'python3 -m coordinator_core.invoke ping --params \'{"a":1}\''
    ),
    "guard_no_optional_locks": lambda mod: "git status",
    "guard_reap_stale_git_lock": lambda mod: "git status",
}


def _discover_guard_modules() -> List[Any]:
    """Scan `coordinator_core.bash_guards` for every non-underscore,
    non-package module that is not the dispatcher itself. This is the
    "by scan, not by roster" mechanism this module's docstring requires --
    a guard added six months from now is picked up automatically."""
    modules = []
    for modinfo in pkgutil.iter_modules(_bash_guards_pkg.__path__):
        name = modinfo.name
        if modinfo.ispkg or name.startswith("_") or name in _NEVER_A_GUARD:
            continue
        modules.append(importlib.import_module(f"coordinator_core.bash_guards.{name}"))
    return modules


#: Per-guard override of WHICH function is the live, registered command-
#: shaped entry point, for the one guard where `check` is not it.
#: `block_dev_repo_sentinel_removal.check` is a retired dead leg (its own
#: docstring: "no longer registered in dispatch.py... not reachable
#: through the live dispatch chain, only directly callable");
#: `check_advisory` is "the guard's sole registered leg" -- calling `check`
#: for this guard proves nothing about its live behaviour. Confirmed by
#: grep: this is the only guard module defining `check_advisory` at all.
_ENTRY_OVERRIDE: Dict[str, str] = {
    "block_dev_repo_sentinel_removal": "check_advisory",
}

#: Per-guard extra payload fields merged over the base PowerShell payload.
#: `block_reviewer_bash_outside_allowlist` fail-closes to allow (returns
#: before ever reaching dialect/detection logic) unless `agent_id` matches
#: its bare-hex-or-named-teammate identity shape AND `agent_type` is the
#: sole `_CONFINED_FINDINGS_AGENTS` member -- a generic payload never
#: reaches this guard's detection at all, per its own
#: `test_block_reviewer_bash_outside_allowlist.py::_payload`/`_confine`
#: helpers (default `agent_id="deadbeef0123"`, confined
#: `agent_type="coordinator:code-reviewer"`).
_PAYLOAD_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "block_reviewer_bash_outside_allowlist": {
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:code-reviewer",
    },
}


def _hazard_repo_monkeypatch(mod: Any, mp: pytest.MonkeyPatch) -> None:
    """Shared seam these two branch-naming guards both gate content
    detection behind: `resolve_git_root`/`_is_hazard_repo`, patched exactly
    as each guard's own test file patches it
    (`_hazard_repo_by_default`/`_hazard_repo_and_clock` fixtures) --
    without this, EVERY command allows clean regardless of dialect, which
    is a real applicability gate, not a dialect-parsing question."""
    mp.setattr(mod, "resolve_git_root", lambda cwd=None: "/repo")
    mp.setattr(mod, "_is_hazard_repo", lambda git_root: True)


def _branch_set_precedence_monkeypatch(
    mod: Any, mp: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """`guard_branch_set_precedence` additionally needs a real candidate
    behind its own `branch_set_provider` seam and a positive `_ahead_of_
    main` count to reach its advisory -- both monkeypatched exactly as
    `TestDeterministicFiring.test_advisory_fires_with_real_branch_and_count`
    (this guard's own test file) does it. Returns the `branch_set_provider`
    kwarg `_call_guard` should thread through (that parameter is not one of
    the generic names `_call_guard` already knows how to fill)."""
    _hazard_repo_monkeypatch(mod, mp)
    mp.setattr(mod, "_now", lambda: 1722700000.0)
    mp.setattr(mod, "_today", lambda: "2026-08-03")
    mp.setattr(mod, "_ahead_of_main", lambda branch, cwd=None: 12)
    mp.setattr(mod, "should_prompt_rename", lambda *a, **k: False)
    recent_epoch = 1722700000.0 - 3600
    provider = lambda: [("work/delphipro/2026-07-31", recent_epoch)]
    return {"branch_set_provider": provider}


#: Per-guard monkeypatch preparation, applied to the live module (via the
#: same seam each guard's own test file patches) immediately before the
#: guard is called. Returns extra kwargs `_call_guard` should merge in
#: (empty for guards needing only the patch, not an extra parameter).
#: These seams (repo-hazard scoping, branch-set candidate enumeration) are
#: APPLICABILITY gates independent of PowerShell-vs-bash dialect -- a
#: guard that never gets past them under ANY dialect is not exercising
#: the dialect/SILENT question at all, so this test drives them open the
#: same way each guard's own author already does.
_MONKEYPATCH_FOR: Dict[str, Callable[[Any, pytest.MonkeyPatch], Dict[str, Any]]] = {
    "guard_longlived_branch_naming": lambda mod, mp: (
        _hazard_repo_monkeypatch(mod, mp) or {}
    ),
    "guard_branch_set_precedence": _branch_set_precedence_monkeypatch,
}


def _find_command_shaped_check(mod: Any) -> Optional[Tuple[str, Callable[..., Any]]]:
    """Return (short_name, fn) for the module's public command-shaped
    entry point, or None if the module has no such entry point -- the
    BY-CONSTRUCTION filter that lets `commit_tripwires.py` (whose
    `check_*` functions take no `cmd`/`payload` argument) fall outside the
    scan without being named in a skip list."""
    short_name = mod.__name__.rsplit(".", 1)[-1]
    override_name = _ENTRY_OVERRIDE.get(short_name)
    if override_name is not None:
        return override_name, getattr(mod, override_name)

    fn = getattr(mod, "check", None)
    if fn is not None and inspect.isfunction(fn):
        params = inspect.signature(fn).parameters
        if "payload" in params or "cmd" in params:
            return "check", fn

    candidates = []
    for attr_name, attr in vars(mod).items():
        if (
            attr_name.startswith("check")
            and inspect.isfunction(attr)
            and getattr(attr, "__module__", None) == mod.__name__
        ):
            params = inspect.signature(attr).parameters
            if "payload" in params or "cmd" in params:
                candidates.append((attr_name, attr))
    if len(candidates) == 1:
        return candidates[0]
    return None


def _call_guard(
    fn: Callable[..., Any],
    cmd: str,
    payload: Dict[str, Any],
    extra_kwargs: Optional[Dict[str, Any]] = None,
) -> Any:
    """Normalize the two live calling conventions
    (`check(payload)` vs `check_xxx(cmd, session_id=..., cwd=..., ...)`)
    into one call, driven entirely off the function's own signature -- no
    per-guard special-casing beyond parameter-name matching, plus whatever
    `extra_kwargs` a guard's `_MONKEYPATCH_FOR` entry supplied (e.g.
    `branch_set_provider`, a real collaborator param `_call_guard` cannot
    infer generically)."""
    params = inspect.signature(fn).parameters
    kwargs: Dict[str, Any] = {}
    for name in params:
        if name == "payload":
            kwargs["payload"] = payload
        elif name == "cmd":
            kwargs["cmd"] = cmd
        elif name == "session_id":
            kwargs["session_id"] = payload.get("session_id", "")
        elif name == "cwd":
            kwargs["cwd"] = payload.get("cwd", "")
        elif name == "dialect":
            kwargs["dialect"] = Dialect.POWERSHELL
        elif name == "host_is_windows":
            kwargs["host_is_windows"] = True
        # Any other optional parameter (policy_path, etc.) not covered
        # above and not supplied via extra_kwargs is left to its own
        # default -- this test drives only the command/dialect surface
        # plus explicitly-declared per-guard collaborators.
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return fn(**kwargs)


def _powershell_payload(short_name: str, cmd: str) -> Dict[str, Any]:
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": cmd},
        "session_id": "c7-structural-test",
        # A real, on-disk git root -- several guards early-exit clean when
        # `resolve_git_root(cwd)` fails, which is a genuine "not applicable
        # here" clean, not the false-clean this test targets. Using this
        # repo's own working directory (not a synthetic path) avoids that
        # confound.
        "cwd": os.getcwd(),
        "agent_id": "c7-structural-test-agent",
        "agent_type": "executor",
    }
    payload.update(_PAYLOAD_OVERRIDES.get(short_name, {}))
    return payload


def _matchers_declares_powershell(mod: Any) -> bool:
    """Read `MATCHERS` live off the module -- never a hardcoded roster."""
    return "PowerShell" in (getattr(mod, "MATCHERS", None) or [])


def _powershell_declared_guards() -> List[Tuple[str, Any, str, Callable[..., Any]]]:
    """(short_name, module, entry_name, fn) for every discovered guard
    that (a) has a command-shaped entry point and (b) declares
    ``"PowerShell"`` in its own ``MATCHERS`` -- the AC1-in-scope set for
    the no-bare-clean property. A guard whose ``MATCHERS`` is ``["Bash"]``
    is deliberately excluded here: per AC1, its bare-``None`` on
    PowerShell input is correct, statically-declared non-coverage, not a
    false clean."""
    out = []
    for mod in _discover_guard_modules():
        if not _matchers_declares_powershell(mod):
            continue
        found = _find_command_shaped_check(mod)
        if found is None:
            continue
        short_name = mod.__name__.rsplit(".", 1)[-1]
        entry_name, fn = found
        out.append((short_name, mod, entry_name, fn))
    return out


class TestNoFalseCleanOnUnparsedDialect:
    """AC1: over every command-shaped guard whose own ``MATCHERS``
    declares ``"PowerShell"``, a PowerShell-idiom payload landing in that
    guard's own detection domain reaches a real verdict or records
    SILENT -- never bare clean. Guards that never declare PowerShell are
    out of AC1's scope by design (see module docstring) and are not
    exercised here."""

    def test_every_powershell_declared_guard_has_a_fixture(self) -> None:
        """Guards discovered by scan that declare PowerShell but are
        missing a `_PS_COMMAND_FOR` entry are a gap in THIS test's own
        coverage table, not a silent skip."""
        missing = []
        for mod in _discover_guard_modules():
            if not _matchers_declares_powershell(mod):
                continue
            found = _find_command_shaped_check(mod)
            if found is None:
                continue
            short_name = mod.__name__.rsplit(".", 1)[-1]
            if short_name not in _PS_COMMAND_FOR:
                missing.append(short_name)
        assert not missing, (
            "PowerShell-declaring guard(s) discovered with no fixture "
            f"authored in _PS_COMMAND_FOR: {missing}. Add a fixture command "
            "for each before this test can certify full coverage."
        )

    @pytest.mark.parametrize(
        "short_name",
        sorted(name for name, _mod, _entry, _fn in _powershell_declared_guards()),
    )
    def test_no_bare_clean_on_powershell_input(
        self, short_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = {n: (m, e, f) for n, m, e, f in _powershell_declared_guards()}
        mod, entry_name, fn = fixtures[short_name]
        cmd = _PS_COMMAND_FOR[short_name](mod)
        payload = _powershell_payload(short_name, cmd)
        extra_kwargs = {}
        prep = _MONKEYPATCH_FOR.get(short_name)
        if prep is not None:
            extra_kwargs = prep(mod, monkeypatch) or {}

        with collecting() as silences:
            try:
                result = _call_guard(fn, cmd, payload, extra_kwargs)
            except Exception as exc:  # pragma: no cover -- surfaced, not swallowed
                pytest.fail(
                    f"{short_name}.{entry_name}() raised {exc!r} on a "
                    f"PowerShell-idiom payload ({cmd!r}) rather than "
                    "returning a verdict or recording SILENT."
                )

        bare_clean = result is None and not silences
        assert not bare_clean, (
            f"{short_name}.{entry_name}() returned a bare clean (None, no "
            f"SILENT recorded) on PowerShell input {cmd!r} -- this is the "
            "false-clean failure C1-C6 of the plan close. The guard must "
            "either reach a real verdict or call record_silent()."
        )


class TestMatchersConsistency:
    """Pins the MATCHERS/measured-behaviour consistency this chunk's own
    dispatch brief asks for: a guard declaring PowerShell in MATCHERS must
    prove it, on the same never-bare-clean property, with the guard named
    in any failure, using a fixture confirmed to land in that guard's own
    detection domain (see `_PS_COMMAND_FOR` / `_PAYLOAD_OVERRIDES`
    comments for the per-guard evidence -- each fixture is either copied
    from, or verified equivalent to, that guard's own PowerShell test
    class). This distinguishes "declares PowerShell and behaves" from
    "declares PowerShell but never reaches detection under a generic
    payload" -- an off-domain fixture (e.g. `Get-ChildItem` with no
    sentinel target) legitimately returns clean per the guards' own test
    suites (`test_unrelated_command_allows_with_no_silence`), so this
    class deliberately does NOT use one. This does not fix a mismatched
    guard -- see the plan's Anti-scope; a failure here is reported, not
    patched."""

    @pytest.mark.parametrize(
        "short_name",
        sorted(name for name, _mod, _entry, _fn in _powershell_declared_guards()),
    )
    def test_matchers_declares_powershell_and_reaches_it(
        self, short_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixtures = {n: (m, e, f) for n, m, e, f in _powershell_declared_guards()}
        mod, _entry_name, fn = fixtures[short_name]
        cmd = _PS_COMMAND_FOR[short_name](mod)
        payload = _powershell_payload(short_name, cmd)
        extra_kwargs = {}
        prep = _MONKEYPATCH_FOR.get(short_name)
        if prep is not None:
            extra_kwargs = prep(mod, monkeypatch) or {}

        with collecting() as silences:
            result = _call_guard(fn, cmd, payload, extra_kwargs)

        bare_clean = result is None and not silences
        assert not bare_clean, (
            f"MATCHERS/behaviour mismatch: {short_name} declares "
            f"MATCHERS={mod.MATCHERS!r} (includes 'PowerShell') but "
            f"returned a bare clean on {cmd!r} -- the declaration is not "
            "backed by measured behaviour. Reported per this chunk's brief, "
            "not fixed here."
        )


#: Substring a module's source must carry, near its own MATCHERS/hold
#: reasoning, to count as a DOCUMENTED hold rather than silent drift --
#: the discharging artifact for "why does a PowerShell-capable classifier
#: stay excluded from MATCHERS" is the ruling of record in
#: `docs/reference/guard-tool-name-membership.md`, so a real hold cites it.
_HOLD_CITATION_MARKER = "guard-tool-name-membership.md"


#: Deliberately narrower than "references `Dialect.POWERSHELL` anywhere" --
#: that would also catch every "declined-conversion" guard that only
#: reaches PowerShell code to call `record_silent()` and return `None`
#: (e.g. `guard_head_tail_rewrite`, `block_subagent_plan_body_bash_write`,
#: `bump_outside_repo_write`), which is CORRECT, declared-SILENT behaviour,
#: not the capability/declaration asymmetry this class targets. What
#: actually distinguishes `block_subagent_destructive_action.py`'s prior
#: state is a function that FORMATS AND RETURNS a PowerShell-specific deny
#: reason string -- i.e. genuinely classifies and denies, not merely
#: declines to rule. Matched via the same string shape every such deny
#: return in this codebase uses: `return f"PowerShell ...` / `return
#: "PowerShell ...`.
_POWERSHELL_DENY_RETURN_RE = re.compile(r'return\s+f?["\']PowerShell\b')


def _module_has_powershell_classifier(mod: Any) -> bool:
    """True if `mod`'s own source contains a function that formats and
    returns a PowerShell-specific deny reason string -- i.e. the module
    contains PowerShell-dialect DENY classification logic, regardless of
    whether its `MATCHERS` currently admits PowerShell payloads at all.
    This is the exact asymmetry (capability outpacing declaration) that
    let `block_subagent_destructive_action.py` slip past
    `_powershell_declared_guards()`'s `MATCHERS`-gated scan above."""
    try:
        src = inspect.getsource(mod)
    except (OSError, TypeError):  # pragma: no cover -- no source available
        return False
    return bool(_POWERSHELL_DENY_RETURN_RE.search(src))


def _undeclared_powershell_capable_guards() -> List[Tuple[str, Any]]:
    """(short_name, module) for every discovered guard module that carries
    PowerShell classification logic but does NOT declare `"PowerShell"` in
    its own `MATCHERS` -- the class this test closes a sweep-scope gap
    for (see module docstring update, review-integration pass)."""
    out = []
    for mod in _discover_guard_modules():
        if _matchers_declares_powershell(mod):
            continue
        if not _module_has_powershell_classifier(mod):
            continue
        short_name = mod.__name__.rsplit(".", 1)[-1]
        out.append((short_name, mod))
    return out


class TestUndeclaredPowerShellCapabilityIsHeld:
    """Closes the C7 sweep-scope gap `_powershell_declared_guards()` (and
    both classes above) structurally cannot see: both existing classes
    scope themselves to guards whose own `MATCHERS` ALREADY declares
    `"PowerShell"`, so a guard whose classifier capability outpaced its
    own `MATCHERS` declaration -- exactly `block_subagent_destructive_
    action.py`'s prior state, a full `_evaluate_powershell_destructive`
    classifier gated `MATCHERS = ("Bash",)` -- is silently excluded from
    the sweep whose entire job is to catch this class of gap.

    This class does not require every such guard to widen MATCHERS (that
    is a deliberate, documented hold per `docs/reference/guard-tool-name-
    membership.md` SS3 for at least one guard today) -- it requires the
    hold to be DOCUMENTED in the module itself, citing that doc, rather
    than silently unreachable with no explanation a reader of the code
    would ever find.
    """

    @pytest.mark.parametrize(
        "short_name",
        sorted(name for name, _mod in _undeclared_powershell_capable_guards()),
    )
    def test_undeclared_capability_carries_a_documented_hold(
        self, short_name: str
    ) -> None:
        fixtures = {n: m for n, m in _undeclared_powershell_capable_guards()}
        mod = fixtures[short_name]
        src = inspect.getsource(mod)
        assert _HOLD_CITATION_MARKER in src, (
            f"{short_name} carries a PowerShell classifier (`Dialect."
            f"POWERSHELL` referenced in-module) but its own MATCHERS does "
            f"not declare 'PowerShell', and the module cites no hold "
            f"rationale (expected a citation of "
            f"'{_HOLD_CITATION_MARKER}' near the MATCHERS declaration). "
            "Either widen MATCHERS or document the hold -- an undocumented "
            "capability/declaration gap is exactly what let this guard's "
            "classifier go unreachable and unswept."
        )
