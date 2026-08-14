"""coordinator_core.trusted_root_guard — shared trusted-prefix guard for every
site that resolves a coordinator content root via CLAUDE_PLUGIN_ROOT.

Purpose: promotes the byte-identical trust-core (previously duplicated
inline at ~68 bash call sites) into ONE function, mirrored here so a
Python caller gets the identical trust decision without shelling out to
the bash sourced-lib.

Trust boundary: a resolved root is trusted iff it sits under one of four
anchors:
  1. the marketplace-cache install (``${CLAUDE_HOME:-$HOME}/.claude/``),
  2. the DoE clone at the ``.doe-root`` sentinel's content, read registry-first
     per DR-071 (2026-07-22 — the settings-home machine-local registry key
     ``repos.doe_claude`` is the canonical, authoritative coordinator-root
     anchor; the ``.doe-root`` file is a demoted, non-authoritative mirror),
     with the durable/legacy file rungs retained as fallbacks:
     ``repos.doe_claude`` registry key first, then
     ``<settings-home>/machine-local/.doe-root``, then
     ``${CLAUDE_HOME:-$HOME}/.claude/.doe-root``,
  3. the registry-resolved claude-klabauter root (2026-07-22 — the settings-home
     machine-local registry key ``repos.claude_klabauter``, the same anchor
     ``coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root()`` resolves for
     in-process callers), with the durable ``<settings-home>/machine-local/
     .claude-klabauter-root`` pointer file retained as a fallback rung. Absence of the
     ``repos.claude_klabauter`` key degrades cleanly to "this anchor
     contributes nothing" — it never raises and never widens trust on its
     own; claude-klabauter's own bin scripts (and every consolidated caller in this
     module's docstring) live under this root, so without this anchor every
     one of them false-rejects its own repo as untrusted, or
  4. an arbitrary ``--plugin-dir`` checkout with the explicit
     ``COORDINATOR_PLUGIN_ROOT_TRUSTED=1`` developer opt-out —
AND does not contain a ``/..`` traversal segment (closes the
``$HOME/.claude/../../tmp/evil`` bypass that plain prefix-matching would
miss; realpath is banned per DR-148, so this is a textual traversal check,
not a filesystem resolution).

--mode is REQUIRED and has NO default, mirroring the bash sourced-lib's
own hard rule: the two modes are NOT interchangeable safety levels — they
are byte-identical trust-core checks wired to two functionally opposite
tails (fail-loud raises; fail-open degrades and warns, never raising). A
defaulted mode is exactly the mechanism by which a fail-open-shaped
advisory call site would get silently promoted to fail-loud during
mechanical migration, terminating a hook chain it must never terminate.

Negative-spec:
  - Does NOT call ``os.path.realpath`` / ``Path.resolve()`` — the
    traversal check is textual (mirrors the bash ``case "$_root" in
    *"/.."*`` check), per the realpath ban (DR-148).
  - Does NOT mutate any caller-side variable by reference — the bash
    sourced-lib's "no bash-4 nameref" constraint doesn't apply to Python,
    but the function still returns trust status rather than reaching into
    caller state, keeping call-site shape symmetric with the bash version
    for anyone porting a caller later.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.machine_resolver import _flatten, _load_toml


class UntrustedRootError(RuntimeError):
    """Raised by ``fail-loud`` mode when the root is untrusted.

    Mirrors the bash sourced-lib's ``exit 1`` tail for
    ``--mode=fail-loud`` — the caller's process must not proceed with an
    untrusted root. Callers that want the bash script's literal
    process-exit behavior (rather than an exception) can call
    ``coordinator_trusted_root_guard_or_exit`` instead.
    """


def _settings_home_dir_from_env(env: dict) -> str:
    """Resolve the settings-home directory from an INJECTED ``env`` mapping —
    the single shared implementation of the ``COORDINATOR_SETTINGS_HOME``
    override -> ``${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings``
    precedence used by ``_doe_root``, ``_claude_klabauter_root``, and
    ``coordinator_core.resolution.facade.resolve_operator_config``.

    Review: code-reviewer -- Finding 1 (P2, AC-3). Consolidates what used to
    be a 3x/4x-duplicated inline branch (``_doe_root``, ``_claude_klabauter_root``, and
    ``facade._settings_home_dir`` each carried an independent copy) into one
    env-parametrized helper. Reads from the injected ``env`` dict rather than
    calling ``coordinator_core._settings_home.settings_home()`` (which reads
    ``os.environ`` directly) so every call site stays unit-testable without
    mutating process-global env.
    """
    override = env.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return override
    home = _home_from_env(env)
    if home:
        return os.path.join(home, ".coordinator-claude-settings")
    return ""


def _home_from_env(env: dict) -> str:
    """Resolve the user's home directory from an injected ``env`` mapping.

    ``CLAUDE_HOME`` -> ``HOME`` -> ``USERPROFILE``.

    The ``USERPROFILE`` rung is why this is a named helper rather than an inline
    ``env.get("CLAUDE_HOME") or env.get("HOME")``. ``HOME`` is a POSIX convention:
    Git-Bash/MSYS set it, but **native Windows shells (PowerShell, cmd.exe) do not** —
    they set ``USERPROFILE``. Without this rung a native-Windows invocation resolves
    home to ``""``, which makes ``_settings_home_dir_from_env`` return ``""``, which
    skips *every* rung of ``_doe_root``/``_claude_klabauter_root`` — including the canonical
    registry rung whose value is present and correct. One absent env var silently
    disabled the whole resolution chain, so the trust anchor rejected the operator's
    real clone and aborted the installer, with an error naming neither the pointer
    nor the anchors.

    This restores parity with the canonical ``coordinator_core._settings_home.settings_home()``,
    which reaches the same outcome via ``Path.home()`` (``USERPROFILE``-aware on
    Windows). The docstrings here previously claimed to mirror that helper's
    ``${CLAUDE_HOME:-$HOME}`` precedence — they mirrored the *bash* spelling, and
    bash-form ``$HOME`` is exactly the token with no native-Windows analogue.

    Kept env-injected (not ``Path.home()``) so the guard's env-injection tests stay
    able to drive resolution without mutating process-global env.
    """
    return env.get("CLAUDE_HOME") or env.get("HOME") or env.get("USERPROFILE") or ""


def _registry_doe_claude(settings_home_dir: str) -> Optional[str]:
    """Direct-tomllib read of the ``repos.doe_claude`` registry key under
    ``<settings_home_dir>/machine-local/`` — the DR-071 canonical anchor,
    reset-safe because it never shells out to the ``machine-local`` CLI
    (whose reader/exec bits live under the canonical ``<settings-home>/bin/``,
    with a resettable ``~/.claude/bin/`` mirror during the settings-home
    migration window).

    Takes the settings-home directory as a plain string (already resolved
    from the guard's injected ``env`` dict by ``_doe_root``) rather than
    calling ``coordinator_core.machine_resolver.registry_get`` directly —
    that helper reads ``os.environ`` internally via
    ``_settings_home.machine_local_dir()``, which would ignore this guard's
    env-injection contract and break its test isolation. Reuses
    ``machine_resolver``'s pure TOML-parsing helpers (``_load_toml``,
    ``_flatten``) instead of hand-rolling a second parser.
    """
    reg_dir = Path(settings_home_dir) / "machine-local"
    for fname in ("registry.local.toml", "registry.toml"):
        flat = _flatten(_load_toml(reg_dir / fname))
        if "repos.doe_claude" in flat:
            val = flat["repos.doe_claude"]
            if isinstance(val, list):
                val = "\n".join(str(i) for i in val)
            s = str(val)
            if s:
                return s
    return None


def _registry_claude_klabauter(settings_home_dir: str) -> Optional[str]:
    """Direct-tomllib read of the ``repos.claude_klabauter`` registry key,
    mirroring ``_registry_doe_claude`` exactly (same file rungs, same
    env-injection-friendly signature, same reuse of ``machine_resolver``'s
    pure TOML helpers instead of shelling out to the ``machine-local`` CLI).
    """
    reg_dir = Path(settings_home_dir) / "machine-local"
    for fname in ("registry.local.toml", "registry.toml"):
        flat = _flatten(_load_toml(reg_dir / fname))
        if "repos.claude_klabauter" in flat:
            val = flat["repos.claude_klabauter"]
            if isinstance(val, list):
                val = "\n".join(str(i) for i in val)
            s = str(val)
            if s:
                return s
    return None


def _claude_klabauter_root(env: dict) -> str:
    """Read the registry-resolved claude-klabauter root, registry-first with a durable
    pointer-file fallback — same shape as ``_doe_root`` above, minus the
    legacy ``${CLAUDE_HOME:-$HOME}/.claude/`` rung (claude-klabauter has no such
    legacy sentinel; ``coordinator_core.claude_klabauter_root.coordinator_claude_klabauter_root()``
    is the in-process analog for callers that also want the ``CLAUDE_KLABAUTER_ROOT``
    env-var rung and the machine-local CLI subprocess rung — this function
    stays subprocess-free like ``_doe_root``, so a missing/absent registry
    key degrades to "" rather than raising or shelling out):
        1. registry ``repos.claude_klabauter``               (canonical anchor)
        2. <settings-home>/machine-local/.claude-klabauter-root       (durable file mirror)
    Returns "" if neither rung resolves — the caller (``is_trusted``) treats
    an empty claude-klabauter root as "this anchor contributes nothing," never as an
    error.
    """
    content = ""
    settings_home_dir = _settings_home_dir_from_env(env)

    if settings_home_dir:
        registry_value = _registry_claude_klabauter(settings_home_dir)
        if registry_value:
            content = registry_value

    if not content and settings_home_dir:
        durable = os.path.join(settings_home_dir, "machine-local", ".claude-klabauter-root")
        try:
            with open(durable, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""

    content = content.rstrip("\n")
    if content.endswith("/"):
        content = content[:-1]
    return content


def _doe_root(env: dict) -> str:
    """Read the ``.doe-root`` sentinel content, trailing-slash normalized.

    Registry-first per DR-071 (2026-07-22 — the settings-home machine-local
    registry key ``repos.doe_claude`` is the canonical, authoritative
    coordinator-root anchor; ``.doe-root`` is a demoted, non-authoritative
    mirror), durable-file-then-legacy-file fallback (Port of:
    coordinator-trusted-root-guard.sh (DoE bd8cc0e9, 2026-07-22),
    updated for DR-071):
        1. registry ``repos.doe_claude``                    (canonical anchor)
        2. <settings-home>/machine-local/.doe-root          (durable file mirror)
        3. ${CLAUDE_HOME:-$HOME}/.claude/.doe-root          (legacy fallback)
    Mirrors the bash ``cat ... || true`` (missing sentinel -> empty string)
    plus the single trailing-slash strip (``${_cc_doe%/}``) for the two file
    rungs; the registry rung short-circuits before that normalization matters
    (a registry value is never expected to carry a trailing slash, but the
    normalization below still applies uniformly to whichever rung supplied
    ``content``).

    Computes the durable path FROM the injected ``env`` dict (mirroring the
    ``COORDINATOR_SETTINGS_HOME`` -> ``${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings``
    precedence in ``coordinator_core._settings_home.settings_home()``) rather
    than calling ``settings_home()`` directly — that helper reads
    ``os.environ``, which would ignore this function's injected ``env`` and
    break the guard's env-injection tests.
    """
    content = ""
    home = _home_from_env(env)
    settings_home_dir = _settings_home_dir_from_env(env)

    if settings_home_dir:
        registry_value = _registry_doe_claude(settings_home_dir)
        if registry_value:
            content = registry_value

    if not content and settings_home_dir:
        durable = os.path.join(settings_home_dir, "machine-local", ".doe-root")
        try:
            with open(durable, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
    if not content and home:
        sentinel = os.path.join(home, ".claude", ".doe-root")
        try:
            with open(sentinel, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
    # Review: code-reviewer -- byte-exact parity with the bash oracle's
    # `_cc_doe="$(cat ... || true)"` (command substitution strips only
    # trailing newlines, never leading whitespace) + `${_cc_doe%/}` (strips
    # exactly one trailing slash, not all of them). rstrip("/") + .strip()
    # were both undocumented broadenings past the faithful-repro contract.
    content = content.rstrip("\n")
    if content.endswith("/"):
        content = content[:-1]
    return content


def _norm(p: str) -> str:
    """Normalize a path for TEXTUAL prefix comparison on Windows only.

    On Windows the same location is spelled inconsistently across the anchors
    this guard compares: ``.doe-root`` is written with forward slashes
    (``X:/DoE-claude``) while ``CLAUDE_PLUGIN_ROOT`` arrives with backslashes
    (``X:\\DoE-claude\\coordinator``), and the filesystem is case-insensitive.
    Without normalization the DoE-clone anchor can never match and the guard
    false-rejects a legitimately-trusted dev clone — and, worse, the ``/..``
    traversal check silently misses ``\\..``.

    POSIX is returned unchanged, so the bash-oracle parity contract documented
    in this module's header is preserved byte-for-byte on that platform. This
    is still a purely textual transform — no realpath, per DR-148.
    """
    if os.name != "nt":
        return p
    # .lower(), NOT .casefold(): casefold is Unicode-aggressive folding ('ß'->'ss',
    # Kelvin sign->'k', ligatures), a BROADER equivalence than Windows' own
    # case-insensitive path comparison. Two byte-distinct components that Windows
    # treats as different directories could casefold-collide and match the trusted
    # prefix — widening trust, the exact direction this guard exists to prevent.
    # It is also a larger drift from the bash oracle's ASCII `case` match than the
    # byte-exact-parity goal stated for _doe_root tolerates.
    # Review: code-reviewer 2026-07-20 Finding 1 (P2).
    return p.replace("\\", "/").lower()


def _doe_root_rungs(env: dict) -> list[tuple[str, str]]:
    """Diagnostics-only: return each ``_doe_root`` resolution rung's raw
    outcome, in resolution order, as (label, value) pairs. ``"<skipped ...>"``
    marks a rung that never ran because its own precondition (settings-home /
    home resolved) was empty; ``"<absent>"`` marks a rung that ran but found
    no file. Never consulted by ``is_trusted`` or ``_doe_root`` themselves —
    exists purely so a rejection message can show which rung produced (or
    failed to produce) the value, instead of forcing a reader to reconstruct
    it by hand as happened during the 2026-07-28 Windows install dogfood
    (DoE-claude state/2026-07-28-machine-a-install-dogfood-friction-log.md F6).
    """
    home = _home_from_env(env)
    settings_home_dir = _settings_home_dir_from_env(env)
    rungs: list[tuple[str, str]] = []

    if settings_home_dir:
        rungs.append(("registry repos.doe_claude", _registry_doe_claude(settings_home_dir) or "<absent>"))
    else:
        rungs.append(("registry repos.doe_claude", "<skipped: settings-home dir resolved empty>"))

    if settings_home_dir:
        durable = os.path.join(settings_home_dir, "machine-local", ".doe-root")
        try:
            with open(durable, "r", encoding="utf-8") as f:
                rungs.append((f"file {durable}", f.read().rstrip("\n") or "<absent>"))
        except OSError:
            rungs.append((f"file {durable}", "<absent>"))
    else:
        rungs.append(("<settings-home>/machine-local/.doe-root", "<skipped: settings-home dir resolved empty>"))

    if home:
        sentinel = os.path.join(home, ".claude", ".doe-root")
        try:
            with open(sentinel, "r", encoding="utf-8") as f:
                rungs.append((f"legacy file {sentinel}", f.read().rstrip("\n") or "<absent>"))
        except OSError:
            rungs.append((f"legacy file {sentinel}", "<absent>"))
    else:
        rungs.append(("legacy ${CLAUDE_HOME:-$HOME}/.claude/.doe-root", "<skipped: home resolved empty>"))

    return rungs


def _claude_klabauter_root_rungs(env: dict) -> list[tuple[str, str]]:
    """Diagnostics-only sibling of ``_doe_root_rungs`` for ``_claude_klabauter_root``
    (registry rung + durable-file rung only — see ``_claude_klabauter_root`` docstring
    for why it has no legacy-file rung)."""
    settings_home_dir = _settings_home_dir_from_env(env)
    rungs: list[tuple[str, str]] = []

    if settings_home_dir:
        rungs.append(("registry repos.claude_klabauter", _registry_claude_klabauter(settings_home_dir) or "<absent>"))
    else:
        rungs.append(("registry repos.claude_klabauter", "<skipped: settings-home dir resolved empty>"))

    if settings_home_dir:
        durable = os.path.join(settings_home_dir, "machine-local", ".claude-klabauter-root")
        try:
            with open(durable, "r", encoding="utf-8") as f:
                rungs.append((f"file {durable}", f.read().rstrip("\n") or "<absent>"))
        except OSError:
            rungs.append((f"file {durable}", "<absent>"))
    else:
        rungs.append(("<settings-home>/machine-local/.claude-klabauter-root", "<skipped: settings-home dir resolved empty>"))

    return rungs


def _diagnose_untrusted(root: str, env: dict) -> str:
    """Build a human-readable diagnostic block for a rejection message:
    the resolved anchors this root was compared against, and which rung (if
    any) produced each one.

    Diagnostics-only — recomputes the same anchors ``is_trusted`` already
    computed, purely for display; it has no bearing on the trust decision
    and calling it can never change what gets trusted. Exists so an empty
    anchor is VISIBLE (the actual finding, per F6 in the friction log cited
    above) instead of requiring a maintainer to read this module's source
    and hand-run ``is_trusted`` under two shells to discover it.
    """
    home = _home_from_env(env)
    settings_home_dir = _settings_home_dir_from_env(env)
    trusted_prefix = _norm(os.path.join(home, ".claude") + os.sep)
    doe_root = _doe_root(env)
    claude_klabauter_root = _claude_klabauter_root(env)

    def _flag(val: str, note: str) -> str:
        return f"  <-- EMPTY: {note}" if not val else ""

    lines = [
        f"  resolved root:           {root!r}  (normalized: {_norm(root)!r})",
        f"  home:                    {home!r}"
        + _flag(home, "no CLAUDE_HOME/HOME/USERPROFILE in env"),
        f"  settings-home dir:       {settings_home_dir!r}"
        + _flag(settings_home_dir, "skips every registry/durable-file rung below"),
        f"  marketplace anchor:      {trusted_prefix!r}",
        f"  doe_root resolved to:    {doe_root!r}"
        + _flag(doe_root, "every rung below returned nothing"),
    ]
    for label, val in _doe_root_rungs(env):
        lines.append(f"      - {label}: {val!r}")
    lines.append(
        f"  claude_klabauter_root resolved to: {claude_klabauter_root!r}"
        + _flag(claude_klabauter_root, "every rung below returned nothing")
    )
    for label, val in _claude_klabauter_root_rungs(env):
        lines.append(f"      - {label}: {val!r}")

    if not home or not settings_home_dir or not doe_root or not claude_klabauter_root:
        lines.append(
            "  NOTE: at least one anchor above resolved EMPTY. That is very likely the "
            "actual defect (upstream misresolution), not a genuinely untrusted root. Fix "
            "the empty anchor first -- COORDINATOR_PLUGIN_ROOT_TRUSTED=1 would mask it and "
            "leave this machine on a permanent env-var workaround."
        )

    return "\n".join(lines)


def is_trusted(root: str, *, env: dict | None = None) -> bool:
    """Pure trust-core predicate — byte-identical decision to the bash
    sourced-lib's inline check (§ "shared trust-core" comment block).

    No side effects (no stderr, no exit) — the mode-specific tail lives
    in ``coordinator_trusted_root_guard`` / ``..._or_exit``.
    """
    env = os.environ if env is None else env
    claude_home = _home_from_env(env)
    trusted_prefix = _norm(os.path.join(claude_home, ".claude") + os.sep)
    root_cmp = _norm(root)

    trusted = False
    if root_cmp.startswith(trusted_prefix):
        trusted = True

    doe_root = _norm(_doe_root(env))
    # _doe_root strips exactly one trailing "/" from the raw sentinel. A Windows
    # sentinel ending in a BACKSLASH only becomes a trailing slash after _norm,
    # so it survives that strip and would cause a "//" false-reject. Re-strip on
    # Windows only: on POSIX the single-strip behavior is a deliberate
    # bash-oracle parity quirk (see test_doe_root_only_single_trailing_slash_
    # stripped) and must not be broadened here.
    if os.name == "nt" and doe_root.endswith("/"):
        doe_root = doe_root[:-1]
    if doe_root and root_cmp.startswith(doe_root + "/"):
        trusted = True

    claude_klabauter_root = _norm(_claude_klabauter_root(env))
    # Same single-trailing-slash re-strip quirk as doe_root above — see that
    # branch's comment; kept symmetric rather than "fixed" for either anchor.
    if os.name == "nt" and claude_klabauter_root.endswith("/"):
        claude_klabauter_root = claude_klabauter_root[:-1]
    if claude_klabauter_root and root_cmp.startswith(claude_klabauter_root + "/"):
        trusted = True

    # Checked against the normalized form so Windows "\.." is caught too.
    if "/.." in root_cmp:
        trusted = False

    if env.get("COORDINATOR_PLUGIN_ROOT_TRUSTED", "") == "1":
        trusted = True

    return trusted


def coordinator_trusted_root_guard(
    *, mode: str, root: str, site: str = "coordinator root", env: dict | None = None
) -> bool:
    """Trust-check ``root`` against the shared trust-core, then apply the
    named mode's tail behavior.

    Returns:
        True  — root is trusted. Caller proceeds unchanged in both modes.
        False — root is untrusted AND mode == "fail-open" (a WARNING was
                printed to stderr iff the anomaly is security-relevant:
                root non-empty and existing but untrusted; silent on
                routine absence). The caller is responsible for blanking
                its own root variable on a False return (mirrors the bash
                sourced-lib's no-nameref constraint — this Python port
                keeps the same call-site shape for symmetry, even though
                Python could mutate by reference here).

    Raises:
        ValueError — mode is missing or not one of fail-loud/fail-open.
        UntrustedRootError — root is untrusted AND mode == "fail-loud"
                (an ERROR was printed to stderr first). Mirrors the bash
                sourced-lib's ``exit 1`` — callers that need the literal
                process-exit semantics should use
                ``coordinator_trusted_root_guard_or_exit`` instead.
    """
    env = os.environ if env is None else env

    if mode not in ("fail-loud", "fail-open"):
        if mode == "":
            raise ValueError(
                "coordinator_trusted_root_guard: mode is REQUIRED "
                "(fail-loud|fail-open) — no default is provided on purpose; "
                "see coordinator/snippets/cc-root-source-guard.md"
            )
        raise ValueError(
            f"coordinator_trusted_root_guard: unrecognized mode={mode!r} "
            "(expected fail-loud|fail-open)"
        )

    if is_trusted(root, env=env):
        return True

    if mode == "fail-loud":
        diagnostics = _diagnose_untrusted(root, env)
        print(
            f"ERROR: {site} '{root}' outside trusted prefix — refusing to source; "
            "re-run coordinator:install (or, ONLY after confirming every anchor below "
            "resolved correctly, set COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a sanctioned "
            "--plugin-dir spike -- if an anchor resolved EMPTY, fixing that is the real "
            "fix; the override would just mask it)\n"
            f"{diagnostics}",
            file=sys.stderr,
        )
        raise UntrustedRootError(f"{site} '{root}' outside trusted prefix")

    # fail-open
    if root and os.path.isdir(root):
        print(
            f"[coordinator] WARNING: '{root}' outside trusted prefix — "
            f"hook degraded\n{_diagnose_untrusted(root, env)}",
            file=sys.stderr,
        )
    return False


def coordinator_trusted_root_guard_or_exit(
    *, mode: str, root: str, site: str = "coordinator root", env: dict | None = None
) -> bool:
    """Same contract as ``coordinator_trusted_root_guard``, except
    ``fail-loud`` calls ``sys.exit(1)`` (matching the bash sourced-lib's
    literal ``exit 1`` behavior) instead of raising
    ``UntrustedRootError``. Use this variant when porting a bash call
    site whose surrounding script relies on process-exit semantics
    rather than exception propagation.
    """
    try:
        return coordinator_trusted_root_guard(mode=mode, root=root, site=site, env=env)
    except UntrustedRootError:
        sys.exit(1)
