"""coordinator_core.bash_guards._platform_verdict — one detection path, two
verdicts (problem 6 of `docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md`,
C6).

Purpose: every shape-specific guard in this package (grep-via-Bash,
multi-probe banner, head/tail plumbing, for-loop, find-exec/xargs — C3/C4/
C5/C9) DETECTS the same way regardless of host, but must VERDICT
differently: DENY on Windows (the cold-start-tax host this whole plan
exists to protect — see the plan's problem 0), ADVISE on macOS (a seam
advisory, not a doctrine rule — see "Seam-advisory-vs-doctrine-rule"
below). This module is that shared verdict layer: shape detection stays in
each calling guard; this module only turns a detected shape into the
correct platform-conditioned envelope.

Public surface
--------------
``platform_verdict(deny_reason, advisory_context, host_is_windows=None)``
    Low-level: caller supplies both fully-rendered message strings, gets
    back the correctly-shaped envelope for the resolved platform.

``platform_verdict_for_shape(shape_name, matched_cmd, outlet_summary,
outlet_example, host_is_windows=None)``
    Convenience: renders an advisory message from a shared template so
    every shape-guard's advisory text reads as one family (AC-7 — a
    misdescribing message is a release blocker, and a shared template is
    cheaper to keep correct than N independently hand-written ones).
    ADVISORY-ONLY (DR-280, 2026-08-07): both live callers
    (``guard_multiprobe_banner``, ``guard_plumbing_and_loops``) fix
    ``host_is_windows=False`` at the call site because the Windows deny leg
    is structurally unreachable through the real dispatch chain — an
    earlier-registered ``ADVISORY_REWRITE`` rewrite sibling for the same
    shape always wins first (see
    ``docs/decisions/DR-280-unreachable-deny-legs-retire-rather-than.md``).
    This function no longer renders a deny message at all; the rendered
    advisory carries the working alternative in the SAME model-visible
    field the harness surfaces to the agent (``additionalContext``) — see
    "Seam-advisory-vs-doctrine-rule" below for why that placement is the
    mechanism, not decoration. The low-level ``platform_verdict`` below is
    unaffected and still renders either envelope shape from caller-supplied
    text.

Platform-override contract (AC-8 / AC-11)
------------------------------------------
``host_is_windows: Optional[bool] = None`` on both public functions, when
``None``, resolved by ``_resolve_host_is_windows`` — this is prior art, not
a new pattern: ``coordinator_core/ops/session/guard_foreign_platform_paths.py``
(``detect_foreign_platform_paths``, line ~288) declares the identical
parameter and its test module
(``coordinator_core/ops/session/tests/test_guard_foreign_platform_paths.py``)
drives both platform branches from macOS by passing the kwarg. A Python
keyword parameter with no environment-variable mirror is not agent-
reachable — `settings.json`'s ``env`` block sets OS environment variables,
and nothing in that block can set a Python function's default argument —
so this discharges AC-8 (no ``COORDINATOR_OVERRIDE_*`` key exists anywhere
in this module) while still discharging AC-11 (the DENY path is exercisable
from macOS in a real test process, not merely asserted-by-reading-code).

Declared-platform escape hatch (2026-08-05 widening, PM ruling)
-----------------------------------------------------------------
When ``host_is_windows`` is ``None``, resolution no longer reads
``os.name`` directly — it now consults, in order: (1) a declared value in
the machine-local registry (``_REGISTRY_KEY`` = ``coordinator.
host_is_windows``, under the settings home — always untracked, always
local to the one machine it describes, so it can never travel to and
misclassify a different machine); (2) minimal runtime sniffing
(``os.name``, widened by ``sys.platform`` for a Cygwin/MSYS2-built
interpreter). This closes the Git-for-Windows-bundled-Python gap (such an
interpreter can report ``os.name == "posix"`` despite running on a Windows
host) AND gives an operator a way to fix a misdetecting box by declaring
the truth (``machine-local set coordinator.host_is_windows true``) with no
code change. AC-8's guarantee is unaffected: the registry read is not an
environment variable, and nothing in ``settings.json``'s ``env`` block can
write to the machine-local registry TOML.

THE THREADING CONTRACT THIS CHUNK PINS FOR C7/C8 (dispatch.py, NOT edited
by this chunk — this docstring is the contract the dispatch-chain-owning
chunk authors against):

  Placing ``host_is_windows`` only in this module satisfies AC-11 in
  isolation but breaks AC-9, which requires each guard observed firing
  THROUGH THE REAL DISPATCHER, not in isolation. AC-9/AC-11 are discharged
  TOGETHER only if the override also threads through
  ``coordinator_core.bash_guards.dispatch.evaluate_payload_json`` — the
  same function that already threads an analogous per-call override
  (``policy_file``) to exactly one guard in its chain
  (``block_reviewer_bash_outside_allowlist.check``, ``policy_path=``
  keyword). C7/C8 MUST extend that same shape:

    def evaluate_payload_json(
        raw: str,
        policy_file: Optional[str] = None,
        host_is_windows: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        ...

  and every guard_chain entry whose ``check()`` calls into
  ``platform_verdict``/``platform_verdict_for_shape`` (C3/C4/C5/C9) must
  accept and forward the SAME keyword, e.g.:

    (
        "grep-via-bash",
        lambda: _check_grep_via_bash(payload, host_is_windows=host_is_windows),
        True,
    ),

  ``host_is_windows`` defaults to ``None`` at every hop (never a truthy
  fallback) so an ordinary production call — the harness's own
  invocation of ``evaluate_payload_json``, with no test ever supplying the
  kwarg — falls through unchanged to each guard's own ``os.name == "nt"``
  read, exactly as it does today. Only a test that explicitly passes
  ``host_is_windows=True`` from a macOS process can observe the Windows
  branch; nothing an agent controls (env, settings.json, tool input) can
  set it.

Seam-advisory-vs-doctrine-rule distinction (load-bearing for AC-14's
revisit trigger)
--------------------------------------------------------------------------
macOS gets ADVISE, not DENY, by design (problem 6) — but this is not a
weaker rehash of the CLAUDE.md anti-grep-via-Bash rule that was already
tried fleet-wide and measured LOSING: 24,542 violations, 39.3% of the
corpus, against that rule's own prose. If a seam advisory did no better,
AC-14's measurement would show macOS unchanged and there would be no way
to tell whether the MECHANISM failed or was simply never distinguishable
from the rule it replaced — which is why the distinction has to be stated
here, in words, rather than left to be inferred from the diff.

The reason a seam advisory is expected to outperform the CLAUDE.md rule is
CATEGORICAL, not a re-run of the same instrument with a different label:

  - The CLAUDE.md rule is read ONCE, at session start, in prose competing
    with every other paragraph of standing doctrine, and is never
    resurfaced at the moment a violating command is about to be typed.
  - A seam advisory fires AT THE MOMENT OF THE REACH — the PreToolUse
    hook observes the actual command about to execute — and its message
    lands in a MODEL-VISIBLE field (``additionalContext``) carrying the
    WORKING ALTERNATIVE inline, not a cross-reference to doctrine the
    model has to recall unprompted.

These are different mechanisms (recall-then-comply vs. surfaced-at-the-
point-of-the-mistake), not the same mechanism measured twice. AC-1/AC-14
require the per-machine probe to report macOS's advisory-to-redirect
conversion rate SEPARATELY from Windows deny counts specifically so this
categorical claim is falsifiable rather than asserted.

Negative-spec
-------------
  - Does NOT detect any shape itself — shape classification is
    ``_shape_classifier``'s job (C2); this module is invoked only AFTER a
    caller has already decided a shape matched.
  - Does NOT read any ``COORDINATOR_OVERRIDE_*`` OR ``MACHINE_LOCAL_*``
    environment variable — the two override surfaces are the
    ``host_is_windows`` keyword argument (read at call time, never hoisted
    or env-mirrored, AC-8) and the declared machine-local registry value
    (see "Declared-platform escape hatch" above) — a local TOML file under
    the settings home, never an environment variable and never
    agent-settable via ``settings.json``. Deliberately reads that TOML file
    directly (``_read_declared_registry_value``) rather than through
    ``machine_resolver.registry_get``, because ``registry_get`` has its own
    ``MACHINE_LOCAL_<KEY>`` env-override rung that would otherwise be a live
    env-var override for this key (Review: code-reviewer F1, P1).
  - Does NOT edit ``dispatch.py`` — that threading is C7/C8's surface; the
    section above is the contract, not the implementation.
  - ``platform_verdict_for_shape`` does NOT render a deny envelope for any
    input, at any ``host_is_windows`` value — that leg was retired as dead
    code (DR-280, 2026-08-07; see ``platform_verdict_for_shape``'s own
    docstring). Only the low-level ``platform_verdict`` still renders a
    deny envelope, and only when a caller explicitly asks for one.

Spec backlink: DoE-claude:pln-fleet-wide-bash-spawn-fan-out--2f6552 § C6
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import allow_advisory, deny
from coordinator_core.machine_resolver import load_flat_registry_file, registry_dir

_EVENT_NAME = "PreToolUse"

#: Operator-declarable escape hatch (PM ruling, 2026-08-05) for a host that
#: misdetects under runtime sniffing -- e.g. Python running under Git-for-
#: Windows' bundled MSYS2/Git-Bash environment, which can report
#: ``os.name == "posix"`` despite the underlying host being Windows. Lives
#: in the machine-local registry under
#: ``${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}`` --
#: always untracked, always local to the one machine it describes, so a
#: declared value can never travel to (and misclassify) a machine it does
#: not describe.
#:
#: Accepted values (case-insensitive): "true"/"1"/"yes" -> Windows;
#: "false"/"0"/"no" -> not-Windows. Any other value, or an ABSENT key,
#: falls through to runtime sniffing below -- absence never means
#: "not Windows".
#:
#: Operator command to declare it (fixes a misdetecting box with no code
#: change and no round trip through the EM):
#:   machine-local set coordinator.host_is_windows true
_REGISTRY_KEY = "coordinator.host_is_windows"
_TRUE_VALUES = {"true", "1", "yes"}
_FALSE_VALUES = {"false", "0", "no"}


def _read_declared_registry_value() -> Optional[str]:
    """Direct TOML read for ``_REGISTRY_KEY`` -- deliberately does NOT go
    through ``machine_resolver.registry_get``, because that function has its
    own ``MACHINE_LOCAL_<KEY>`` env-var override rung checked BEFORE the
    TOML file (see ``registry_get``'s docstring / ``_env_override_key``).
    That env leg is a live third override surface for this specific
    security-sensitive key (Review: code-reviewer F1, P1) -- an agent-
    settable ``settings.json`` ``env`` block could otherwise downgrade a
    genuine Windows host's DENY guards to ADVISE, contradicting this
    module's own AC-8 claim that the declared registry value is a local
    TOML file and nothing else. Mirrors ``registry_get``'s file precedence
    (``registry.local.toml`` > ``registry.toml``) and list/str coercion via
    the same public ``registry_dir``/``load_flat_registry_file`` seams, but
    never touches ``os.environ``."""
    reg_dir = registry_dir()
    for fname in ("registry.local.toml", "registry.toml"):
        flat = load_flat_registry_file(reg_dir / fname)
        if _REGISTRY_KEY in flat:
            val = flat[_REGISTRY_KEY]
            if isinstance(val, list):
                val = "\n".join(str(i) for i in val)
            s = str(val)
            if s:
                return s
    return None


def _declared_host_is_windows() -> Optional[bool]:
    """Read the declared registry value, fresh, every call -- no caching,
    so there is no stale-cache-across-interpreter-change hazard to guard
    against (the concern PM raised for a cached variant). ``_read_declared_
    registry_value`` is a direct TOML file read -- no subprocess, ever, on
    this path, and (per its own docstring) no env-var leg either.

    Broad ``except Exception`` on purpose: this resolver must never be the
    reason the platform-verdict decision raises. The direct TOML read
    already degrades most failure modes (missing/malformed registry file)
    to ``None`` internally, but its own dependency chain
    (``_settings_home.machine_local_dir`` -> ``Path.home()``) resolves a
    concrete ``pathlib`` class off the CURRENT ``os.name`` -- a real hazard
    only in a test process that monkeypatches ``os.name`` without also
    being the OS pathlib is instantiating for (``WindowsPath`` refuses to
    instantiate on an actual POSIX interpreter), never on a genuine host.
    Falling through to sniffing on any such failure keeps that an
    unaffected test artifact rather than a crashed guard chain."""
    try:
        raw = _read_declared_registry_value()
    except Exception:
        return None
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _sniff_host_is_windows() -> bool:
    """Minimal runtime fallback -- consulted only when neither the
    ``host_is_windows`` override nor the declared registry key resolves the
    question. ``os.name == "nt"`` is the primary signal (native Windows
    Python); ``sys.platform`` is widened to also catch a Cygwin/MSYS2-built
    Python interpreter (``cygwin``/``msys``), which reports
    ``os.name == "posix"`` despite running on a Windows host. Deliberately
    NOT a full env-var detection matrix (``MSYSTEM``/``SystemRoot``/WSL
    markers) -- PM ruling 2026-08-05: that configuration is fringe enough
    that the declared-registry-value escape hatch above is the real answer,
    not a bigger sniffing matrix."""
    if os.name == "nt":
        return True
    return sys.platform in ("win32", "cygwin", "msys")


def _resolve_host_is_windows(host_is_windows: Optional[bool]) -> bool:
    """Resolve the effective platform for this call, in order: (1) the
    ``host_is_windows`` kwarg when not ``None`` -- the pre-existing override
    contract, unchanged; (2) a declared value in the machine-local registry
    (operator escape hatch, see ``_REGISTRY_KEY``); (3) minimal runtime
    sniffing (``_sniff_host_is_windows``). Read at CALL time, never hoisted
    to module scope, so a test-supplied override never leaks into a later
    production call in the same process."""
    if host_is_windows is not None:
        return host_is_windows
    declared = _declared_host_is_windows()
    if declared is not None:
        return declared
    return _sniff_host_is_windows()


def resolve_host_is_windows(host_is_windows: Optional[bool]) -> bool:
    """Public alias of `_resolve_host_is_windows`, promoted 2026-07-30 (H4,
    docs/plans/2026-07-30-os-aware-guard-advisory-defaults.md) so
    ``dispatch.evaluate_payload_json`` can resolve the SAME real-host-vs-
    override answer the three PLATFORM_CONDITIONED_DENY guards already
    compute internally, rather than re-deriving `os.name == "nt"` a second
    time. `host_is_windows=None` means "read the real host", never "not
    Windows" -- every production call passes `None`, so a caller that
    treats `None` as falsy inverts this on the one host the suite exists
    for. Do not read `os.name` directly anywhere else in this package --
    this is the one seam.
    """
    return _resolve_host_is_windows(host_is_windows)


def platform_verdict(
    deny_reason: str,
    advisory_context: str,
    host_is_windows: Optional[bool] = None,
) -> Dict[str, Any]:
    """Return the platform-conditioned envelope for an already-detected
    shape: DENY (with ``deny_reason``) on Windows, ADVISE (with
    ``advisory_context``) everywhere else.

    Both message strings are the caller's responsibility — this function
    makes no claim about their content, only about which one ships and
    under which envelope shape. See ``platform_verdict_for_shape`` for a
    template that keeps deny/advisory text consistent across guards.
    """
    if _resolve_host_is_windows(host_is_windows):
        return deny(_EVENT_NAME, deny_reason)
    return allow_advisory(_EVENT_NAME, advisory_context)


def platform_verdict_for_shape(
    shape_name: str,
    matched_cmd: str,
    outlet_summary: str,
    outlet_example: str,
    host_is_windows: Optional[bool] = None,
) -> Dict[str, Any]:
    """Render the standard advisory message for a detected bash-spawn shape
    and return the advisory envelope.

    ADVISORY-ONLY (DR-280, 2026-08-07): this function never returns a deny
    envelope. It used to platform-condition its verdict via the low-level
    ``platform_verdict`` (DENY on Windows, ADVISE elsewhere); that deny leg
    was retired as dead code because every live caller
    (``guard_multiprobe_banner``, ``guard_plumbing_and_loops``) fixes
    ``host_is_windows=False`` at the call site -- the deny branch could
    never open through the real dispatch chain (see the module docstring's
    "Negative-spec" section and DR-280 for the reachability evidence).

    Args:
        shape_name: the classifier's shape label (e.g. "grep-via-bash"),
            surfaced verbatim in the advisory so a reader can match the
            finding back to `_shape_classifier`'s taxonomy.
        matched_cmd: accepted for signature/template-family compatibility
            with the caller sites; not currently rendered into the
            advisory text (the advisory does not echo the raw command).
        outlet_summary: one-line description of the working alternative
            (design-as-offers — the alternative leads, not the violation).
        outlet_example: a concrete equivalent invocation using the
            alternative, shown in the advisory message.
        host_is_windows: VESTIGIAL as of this function's DR-280 fix --
            accepted and still threaded because every guard chain entry
            and ``dispatch.py``'s registration lambdas already pass it
            (see module docstring's threading contract), but the value has
            no effect on this function's output: the message rendered is
            identical for every value. Retired rather than dropped from
            the signature to avoid an unrelated signature-break across
            every caller; see this chunk's run report for the tradeoff.
    """
    del matched_cmd, host_is_windows
    advisory_context = (
        "BASH-SPAWN ADVISORY (non-blocking): this command matches the "
        "`%s` shape, one of this fleet's per-process cold-start-cost "
        "drivers on Windows; consider %s here too so behavior stays "
        "consistent across the fleet.\n\n"
        "  Example:  %s\n"
    ) % (shape_name, outlet_summary, outlet_example)
    return allow_advisory(_EVENT_NAME, advisory_context)
