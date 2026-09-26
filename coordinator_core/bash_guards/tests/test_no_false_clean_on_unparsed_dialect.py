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

#: `commit_tripwires` is EXPLICITLY excluded here, not left to fall out of
_NEVER_A_GUARD = frozenset({"dispatch", "dispatch_checks", "commit_tripwires"})

#: guards can read their own `_TARGET_BASENAME` off the live module rather
_PS_COMMAND_FOR: Dict[str, Callable[[Any], str]] = {
    # cmdlet matcher -- a fixture artifact in the ORIGINAL test, not a
    "block_approval_sentinel_creation": (
        lambda mod: f"New-Item {mod._TARGET_BASENAME}"
    ),
    "block_disarm_marker_sentinel_creation": (
        lambda mod: f"New-Item {mod._TARGET_BASENAME}"
    ),
    # `check_advisory`, NOT `check` (see `_ENTRY_OVERRIDE` below): `check`
    # exactly (`_payload("Remove-Item %s" % SENTINEL, ...)`) -- a quoted
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
        lambda mod: "Remove-Item -Recurse -Force C:/scratch/target"
    ),
    # the same subagent-boundary MATCHERS widening. Each fixture is the
    # `_MODULE_M_GRANT` constant from that guard's OWN test file, which its
    "block_subagent_grant_acquisition": (
        lambda mod: (
            'python3 -m coordinator_core.session.claude_md_grant grant pm "note"'
        )
    ),
    "block_subagent_guard_grant": (
        lambda mod: (
            "python3 -m coordinator_core.session.em_guard_grant grant "
            'bump-foreign-repo-write "reason"'
        )
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
    "guard_grep_via_bash": lambda mod: "Select-String -Pattern foo -Path bar.py",
    "guard_inprocess_search": lambda mod: "Select-String -Pattern foo -Path bar.py",
    "guard_multiprobe_banner": (
        lambda mod: "Get-Process; Get-Service; Get-ChildItem C:\\"
    ),
    "guard_plumbing_and_loops": (
        lambda mod: "Get-Content foo.log | Select-Object -First 20"
    ),
    "bump_foreign_repo_write": (
        lambda mod: "cross-repo-memo send --to peer --summary x"
    ),
    "bump_outside_repo_write": (
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
    # each declares `MATCHERS = COMMAND_TOOL_NAMES` (PowerShell included) and
    "block_fleet_delegation_creation": (
        lambda mod: f"New-Item {mod._TARGET_BASENAME}"
    ),
    "guard_repo_setup_claude_home_refusal": (
        lambda mod: (
            "python3 -m coordinator_core.install.scaffold_structure --root "
            + os.path.join(os.path.expanduser("~"), ".claude").replace("\\", "/")
        )
    ),
    "guard_host_subagent_bash_spawn_shapes": lambda mod: "rg TODO",
    # capability landed with `_PS_WRITE_CMDLET_RE` in the same session that
    "guard_doctrine_surface_bash_write": (
        lambda mod: "Set-Content CLAUDE.md 'corrupted'"
    ),
    # (`_is_p4_gated`, D1's marker check) is supplied via `_MONKEYPATCH_FOR`
    "p4_verb_fence": (
        lambda mod: "p4.exe -p ssl:host:1666 -c client submit"
    ),
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


_ENTRY_OVERRIDE: Dict[str, str] = {
    "block_dev_repo_sentinel_removal": "check_advisory",
}

#: sole `_CONFINED_FINDINGS_AGENTS` member -- a generic payload never
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


#: APPLICABILITY gates independent of PowerShell-vs-bash dialect -- a
_MONKEYPATCH_FOR: Dict[str, Callable[[Any, pytest.MonkeyPatch], Dict[str, Any]]] = {
    # test's population on 2026-08-19 when the subagent-boundary MATCHERS
    # parity widened its `MATCHERS` from `("Bash",)` to `COMMAND_TOOL_
    "block_noncanonical_branch_creation": lambda mod, mp: (
        _hazard_repo_monkeypatch(mod, mp) or {}
    ),
    "guard_host_subagent_bash_spawn_shapes": lambda mod, mp: (
        mp.setattr(mod, "_repo_config", lambda cwd=None: "structural-test-config"),
        mp.setattr(mod, "_policy_is_deny", lambda config: True),
        {},
    )[-1],
    # `governed_surfaces` is a REQUIRED positional this guard's caller
    "guard_doctrine_surface_bash_write": lambda mod, mp: {
        "governed_surfaces": [
            "CLAUDE.md",
            "MEMORY.md",
            "coordinator.local.md",
            "AGENTS.md",
        ]
    },
    "p4_verb_fence": lambda mod, mp: (
        mp.setattr(mod, "_is_p4_gated", lambda cwd: True),
        {},
    )[-1],
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
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return fn(**kwargs)


def _powershell_payload(short_name: str, cmd: str) -> Dict[str, Any]:
    payload = {
        "tool_name": "PowerShell",
        "tool_input": {"command": cmd},
        "session_id": "c7-structural-test",
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
#: stay excluded from MATCHERS" is the ruling of record in
_HOLD_CITATION_MARKER = "guard-tool-name-membership.md"


#: Deliberately narrower than "references `Dialect.POWERSHELL` anywhere" --
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
