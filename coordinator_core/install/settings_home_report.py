"""Enumerate and verify the settings-home post-condition.

docs/plans/2026-08-17-machine-first-install-surface.md § C5: today the
settings home (``~/.coordinator-claude-settings``) is populated *emergently*
-- ``bin/`` forwarders, ``.percolate-identity``, the machine-local registry,
and ``machine-local/.claude-klabauter-live-root`` each land via their own install step, with
no single verified statement that the settings home is complete. This module
is that statement: it enumerates what a correct settings home contains and
checks presence, not intent, so ``scripts/setup.py``'s report line and the
``claude-klabauter.settings_home.complete`` doctor probe (bin/claude-klabauter-doctor-probe.py)
share one oracle instead of two that can drift apart.

A forwarder COUNTS only when its body is the one this claude-klabauter root's
installer writes (`forwarder_body_is_ours`) OR, for exactly the
`coordinator-invoke` slot, when the native warm-engine door has legitimately
claimed that bare name instead (`_is_door_owned_forwarder_slot`) OR, for a
member the installer delivers by BYTE COPY rather than as a generated
trampoline, when the installed bytes are this root's source file's bytes
(`_byte_copied_body_matches_source`) -- see each function's own docstring for
why these are distinct, verified states and not a suppression of the check. Settings-home `bin/` is one directory every
engine root on the box installs into: a run rooted at the published mirror
lands its own forwarder set there, prunes the names its tree does not carry,
and leaves bodies importing `_resolve_claude_klabauter`. An existence-only
count reports that state green -- it was green on machine-b on 2026-08-22
while four names were absent and the other 390 resolved through a different
root's shim.

Input, not invention -- state precedence explicitly. DoE-claude
(coordinator-claude) already declares the settings-home post-condition:

- ``coordinator-claude coordinator/docs/wiki/machine-local-registry.md``
  §4e / its settings-home inventory row (line ~837) names the top-level
  namespace: ``machine-local/`` (TOML registry), ``bin/`` (resolver family),
  ``coordinator-whoami/``, ``.coordinator-venv/``, ``settings-manifest.md``.
  Presence is checked for all six; ``.coordinator-venv/`` is DEMANDED only
  while a machine-local interpreter pin still names it -- claude-klabauter installs it
  only under ``--allow-venv-fallback``. See ``_FIXED_MEMBERS``.
- ``coordinator-claude coordinator/docs/install/AGENT.md`` § Fail-loud
  claude-klabauter resolution names ``<settings-home>/machine-local/.claude-klabauter-live-root`` as
  the rung-2 pointer file a downstream repo's chain-walker reads.

DoE's declaration is authoritative for those six members' PURPOSE; this
module only checks their PRESENCE. It is additive-only -- it appends the one
member claude-klabauter itself is responsible for installing
(``.percolate-identity``, `scripts/setup.py :: install_percolate_identity`)
and never redefines what DoE already named. If DoE's declaration and this
list ever disagree about a shared path, DoE's wins -- this module asserts no
competing definition of its own.

Generator, not a hand-list, for the member that actually churns: the ``bin/``
forwarder set is not copied here by name. ``expected_forwarders()`` calls
``coordinator_core.install.substrate._derive_agent_helper_target_map`` --
the exact function ``_install_bin_resolvers`` uses to decide what to
install -- against the live ``coordinator/bin/`` directory listing, so a CLI
added there reaches this check with no edit here. The fixed six-entry
top-level shape above is NOT generator-derived (DoE ships it as wiki prose,
not a machine-readable manifest) and is a known, named limitation: its
oracle is human transcription of the two DoE citations above, not code.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.install.door_install import BARE_FORWARDER_NAME, is_door_installed
from coordinator_core.install.substrate import (
    _AGENT_FORWARDER_MARKER,
    _RM_FAMILY_FILES,
    BYTE_COPIED_BIN_SOURCES,
    _derive_agent_helper_target_map,
)
from coordinator_core.machine_resolver import registry_get

# (label, relative_path, kind, source, required) -- the DoE-declared top-level
# shape plus claude-klabauter's one additive member. kind is "dir" or "file".
#
# The `required` column is the DECLARED baseline. One member overrides it at
# check time: `.coordinator-venv`, whose requirement is CONDITIONAL on whether
# the venv is actually this machine's resolved coordinator interpreter --
# `_venv_is_the_resolved_interpreter` is that predicate.
#
# Why conditional and not simply optional: the venv is reachable only via the
# break-glass `--allow-venv-fallback`, required on every run with no automatic
# or prior-consent path (INSTALL.md § Dependency provisioning item 5; DR-307,
# DR-317), so demanding it unconditionally made a correct machine-first install
# permanently incomplete. But a box that DID exercise the fallback has registry
# pins aimed into that venv, and there its absence is a broken interpreter pin,
# not a clean state -- flat-optional would go quiet on exactly the box where the
# member is load-bearing. Evidence this is a live shape and not a hypothetical:
# the coordinator-doctor sentinel run of 2026-08-12 (verdict GREEN) recorded
# `invoking_path` as `<settings-home>/.coordinator-venv/bin/python`; ten days
# later the machine-first path installs under the machine-level general pin and
# no producer creates the venv at all.
#
# DoE's inventory row still names the member unconditionally; that is DoE's
# surface to reconcile, and this file asserts no competing definition of its
# PURPOSE -- only when claude-klabauter's own completeness check has grounds to demand it.
#: Settings-home-relative path of the break-glass venv. Named once: the
#: conditional-requirement predicate and the inventory row must not drift.
_VENV_REL = ".coordinator-venv"

#: The registry keys that can aim an interpreter into that venv.
#: `coordinator.whoami_python` is the pointer the venv builder itself owns;
#: `coordinator.python` is the general pin an operator may have aimed there.
#: Both, because either one naming the venv makes its absence a broken pin.
_VENV_PIN_KEYS = ("coordinator.whoami_python", "coordinator.python")

_FIXED_MEMBERS: tuple[tuple[str, str, str, str, bool], ...] = (
    (
        "machine-local/ (TOML registry dir)",
        "machine-local",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory",
        True,
    ),
    (
        "bin/ (resolver family + agent-helper forwarders)",
        "bin",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory",
        True,
    ),
    (
        # NOT REQUIRED: the package is RETIRED and nothing in the install chain
        # creates it. `scripts/setup.py`'s own module docstring states there is
        # no `coordinator_whoami` provisioning step and that its absence is
        # "deliberately absent, not lost". Requiring it made every clean install
        # end on a FAIL for a directory we removed on purpose, printed directly
        # above "setup: complete" — a mixed signal that left a first-time
        # installer unable to tell whether the install had worked
        # (klabauter#1, macOS 15.5).
        #
        # Demoted rather than deleted: the citation below is DoE's inventory
        # spec, and if §4e still names this member then their spec and our
        # retirement disagree. Deleting the row here would hide that
        # disagreement instead of surfacing it. Memo'd to DoE; delete the row
        # once §4e drops it.
        "coordinator-whoami/ (RETIRED — no provisioning step creates it)",
        "coordinator-whoami",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory "
        "(retired claude-klabauter-side; see scripts/setup.py module docstring)",
        False,
    ),
    (
        ".coordinator-venv/ (required only while an interpreter pin names it)",
        _VENV_REL,
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory; "
        "claude-klabauter INSTALL.md § Dependency provisioning item 5 (--allow-venv-fallback)",
        False,
    ),
    (
        "settings-manifest.md",
        "settings-manifest.md",
        "file",
        "DoE machine-local-registry.md §4e settings-home inventory",
        True,
    ),
    (
        "machine-local/.claude-klabauter-live-root (fail-loud rung-2 pointer)",
        "machine-local/.claude-klabauter-live-root",
        "file",
        "DoE docs/install/AGENT.md § Fail-loud claude-klabauter resolution, rung 2",
        True,
    ),
    (
        f"bin/{_RM_FAMILY_FILES[0]} (resolution shim every forwarder imports)",
        f"bin/{_RM_FAMILY_FILES[0]}",
        "file",
        "claude-klabauter coordinator_core/install/substrate.py :: _install_bin_resolvers, rm_family",
        True,
    ),
    (
        ".percolate-identity (publish audit config)",
        ".percolate-identity",
        "file",
        "claude-klabauter scripts/setup.py :: install_percolate_identity (additive, not a DoE member)",
        True,
    ),
)


@dataclass
class SettingsHomeMember:
    label: str
    path: Path
    kind: str
    present: bool
    source: str
    required: bool = True


@dataclass
class SettingsHomeReport:
    settings_home_path: Path
    members: list[SettingsHomeMember] = field(default_factory=list)
    forwarder_expected: int = 0
    forwarder_present: int = 0
    forwarder_missing: list[str] = field(default_factory=list)
    forwarder_unverified: list[str] = field(default_factory=list)
    forwarder_door_owned: list[str] = field(default_factory=list)
    forwarder_byte_copied: list[str] = field(default_factory=list)
    forwarder_derivation_error: str | None = None

    @property
    def fixed_missing(self) -> list[SettingsHomeMember]:
        """Members that are REQUIRED and absent -- the ones that make the
        settings home incomplete. An optional member's absence is enumerated
        in `members` and reported, never counted here (see `_FIXED_MEMBERS`)."""
        return [m for m in self.members if m.required and not m.present]

    @property
    def complete(self) -> bool:
        if self.forwarder_derivation_error is not None:
            return False
        return (
            not self.fixed_missing
            and not self.forwarder_missing
            and not self.forwarder_unverified
        )


def expected_forwarders(claude_klabauter_root: Path) -> dict[str, str]:
    """The live-derived installed-name -> on-disk-target map, straight from
    the same function `_install_bin_resolvers` uses. Not a hand-list --
    see module docstring.

    `_derive_agent_helper_target_map` `print()`s a WARNING line straight to
    stdout on a legacy extensionless/`.py`-twin collision (a legal, already-
    tolerated on-disk shape -- see that function's own docstring). Its other
    caller (`install_bin_forwarders`) runs it inside a subprocess whose
    stdout is printed conditionally, so that's harmless there; this module's
    own caller, `_run_probe_settings_home_complete`
    (bin/claude-klabauter-doctor-probe.py), emits a pure-JSON envelope on stdout by
    contract -- an unswallowed print here would corrupt that JSON. Swallow
    it at the call site rather than changing the shared derivation
    function's behavior for its other, JSON-agnostic caller.
    """
    agent_bin = claude_klabauter_root / "coordinator" / "bin"
    with contextlib.redirect_stdout(io.StringIO()):
        return _derive_agent_helper_target_map(agent_bin)


def forwarder_body_is_ours(path: Path, target: str) -> bool:
    """True iff `path` holds the forwarder THIS claude-klabauter root's installer
    writes for `target` -- the marker line every generated body carries
    (`substrate._AGENT_FORWARDER_MARKER`, interpolated from the same
    constant `_write_agent_forwarder` emits, so the two cannot drift) plus
    the `exec_cli` call naming this root's on-disk target filename.

    Existence alone is not the post-condition. A settings-home `bin/` is a
    single directory that every engine root on the box installs into, and a
    forwarder written from a DIFFERENT root imports that root's resolution
    shim (`_resolve_claude_klabauter` rather than `_resolve_claude_klabauter`) or
    execs a renamed target -- a file that exists, satisfies an
    existence-only count, and rc=1s on invocation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _AGENT_FORWARDER_MARKER in text and f'exec_cli("{target}")' in text


def _byte_copied_body_matches_source(
    installed_name: str, path: Path, claude_klabauter_root: Path
) -> bool:
    """True iff `installed_name` is a BYTE-COPIED bin member
    (`substrate.BYTE_COPIED_BIN_SOURCES`) and the installed file's bytes are
    exactly THIS claude-klabauter root's source file's bytes.

    Why a second verification arm and not a suppression: `forwarder_body_is_ours`
    verifies the shape of a GENERATED trampoline -- the marker line plus the
    `exec_cli("<target>")` call. A byte copy carries neither and can never carry
    them: the delivery mechanism is `shutil.copyfile` of the source file, so the
    installed body IS the source. `claude-doe` is that shape on POSIX -- the
    forwarder loop writes a trampoline at `<settings-home>/bin/claude-doe`, then
    `install-claude-doe-wrapper.py` copies the wrapper source onto
    `~/.local/bin/claude-doe`, which `maximalist`'s Step 3.5b has made a symlink
    ONTO that same settings-home file, so the source bytes are the final body on
    every completed install. The marker check therefore reported one permanent
    `body not this root's` on every run -- a FAIL whose remediation ("re-run
    scripts/setup.py") reproduced it exactly.

    Same discipline as `_is_door_owned_forwarder_slot`: a NAMED member in a
    KNOWN-different delivery shape gets its own positive verification, never a
    pass-by-default. The comparison is against this root's source file, so the
    real signal the check exists for survives -- a byte copy landed by a
    DIFFERENT engine root diverges from these bytes and still reports
    unverified, exactly as a foreign-root trampoline does.

    Derived from the installer's own data, not a hand-list here: the mapping is
    `maximalist`'s own `wrapper_src` source of truth.
    """
    rel = BYTE_COPIED_BIN_SOURCES.get(installed_name)
    if rel is None:
        return False
    src = claude_klabauter_root.joinpath(*rel)
    try:
        return src.read_bytes() == path.read_bytes()
    except OSError:
        return False


def _is_door_owned_forwarder_slot(installed_name: str, path: Path, bin_dir: Path) -> bool:
    """True iff `installed_name`/`path` is the `coordinator-invoke` forwarder
    slot AND the native warm-engine door has legitimately claimed it --
    `coordinator_core.install.door_install.install_door()`'s documented
    behaviour on a successful install (its module docstring's Windows
    paragraph; `door_uninstall.uninstall_door()` is the reverse leg that
    puts a plain forwarder back when the door is removed).

    Narrower than "does `is_door_installed` say yes anywhere in `bin_dir`":
    gated first on `installed_name == BARE_FORWARDER_NAME`, so a door
    correctly installed at `coordinator-invoke`/`coordinator-invoke.exe`
    can never be read as covering a DIFFERENT forwarder slot whose body
    happens to also fail `forwarder_body_is_ours` for an unrelated reason
    (a real corruption). On POSIX `BARE_FORWARDER_NAME` and
    `DOOR_INSTALLED_NAME` are the identical bare string, so this is exactly
    the slot the door overwrites; on Windows the door installs at the
    distinct `coordinator-invoke.exe` path and never touches this slot's
    body at all, so `forwarder_body_is_ours` already passes there and this
    function is not reached for a genuinely-installed Windows door.

    `is_door_installed` itself only checks presence of the platform-resolved
    door binary plus its engine-root sidecar at `bin_dir` -- cheap and
    already the load-bearing presence oracle `install_door`/`door_uninstall`
    themselves use, not re-derived here.
    """
    if installed_name != BARE_FORWARDER_NAME:
        return False
    return is_door_installed(bin_dir)


def _venv_is_the_resolved_interpreter(settings_home_path: Path) -> bool:
    """Does a machine-local interpreter pin resolve INTO the settings-home venv?

    True means this box exercised the break-glass fallback and something now
    depends on the venv, so its absence is a broken pin worth reporting. False
    means nothing points there -- the machine-first state, where the directory
    is correctly absent.

    Reads the registry directly (`machine_resolver.registry_get`, tomllib, no
    subprocess) rather than through the `machine-local` CLI: the CLI lives
    under the resettable `~/.claude/bin/`, and this check runs on install paths
    where that may not exist yet.

    Fail-open on an unreadable registry: a registry we cannot read is not
    evidence that the venv is load-bearing, and guessing "required" there would
    reintroduce the permanent red this predicate exists to remove.
    """
    venv = settings_home_path / _VENV_REL
    for key in _VENV_PIN_KEYS:
        try:
            pin = registry_get(key)
        except Exception:
            return False
        if not pin:
            continue
        try:
            if Path(pin).is_relative_to(venv):
                return True
        except (OSError, ValueError):
            continue
    return False


def check_settings_home(settings_home_path: Path, claude_klabauter_root: Path) -> SettingsHomeReport:
    """Stat the settings home and report what is actually there.

    Mutation-test contract: this must fail when the settings home stops
    being populated, not when the enumeration text drifts -- every check
    below is a live `Path.exists()`/dir-listing read against
    `settings_home_path`, never a read of a self-reported manifest
    (e.g. `bin/.coordinator-bin-manifest.json`, which the installer itself
    writes and would report green even if the installer silently failed to
    land a forwarder). Forwarder bodies are read, not merely stat'd -- see
    `forwarder_body_is_ours`.
    """
    report = SettingsHomeReport(settings_home_path=settings_home_path)

    for label, rel, kind, source, required in _FIXED_MEMBERS:
        p = settings_home_path / rel
        present = p.is_dir() if kind == "dir" else p.is_file()
        if rel == _VENV_REL and not required:
            required = _venv_is_the_resolved_interpreter(settings_home_path)
        report.members.append(
            SettingsHomeMember(
                label=label,
                path=p,
                kind=kind,
                present=present,
                source=source,
                required=required,
            )
        )

    try:
        expected = expected_forwarders(claude_klabauter_root)
    except OSError as exc:
        report.forwarder_derivation_error = str(exc)
        return report

    report.forwarder_expected = len(expected)
    bin_dir = settings_home_path / "bin"
    missing: list[str] = []
    unverified: list[str] = []
    door_owned: list[str] = []
    byte_copied: list[str] = []
    present_count = 0
    for installed_name, target in sorted(expected.items()):
        path = bin_dir / installed_name
        if not path.is_file():
            missing.append(installed_name)
            continue
        if forwarder_body_is_ours(path, target):
            present_count += 1
        elif _byte_copied_body_matches_source(installed_name, path, claude_klabauter_root):
            byte_copied.append(installed_name)
            present_count += 1
        elif _is_door_owned_forwarder_slot(installed_name, path, bin_dir):
            door_owned.append(installed_name)
            present_count += 1
        else:
            unverified.append(installed_name)
    report.forwarder_present = present_count
    report.forwarder_missing = missing
    report.forwarder_unverified = unverified
    report.forwarder_door_owned = door_owned
    report.forwarder_byte_copied = byte_copied
    return report


def format_report_lines(report: SettingsHomeReport) -> list[str]:
    """Human-readable PASS/FAIL lines for the `scripts/setup.py` report step.

    An absent OPTIONAL member renders SKIP, not FAIL -- it is enumerated
    because DoE named the namespace, not demanded because claude-klabauter installs it.
    """
    lines: list[str] = []
    for m in report.members:
        if m.present:
            status = "PASS"
        else:
            status = "FAIL" if m.required else "SKIP"
        lines.append(f"  {status} [settings-home] {m.label} -> {m.path} ({m.source})")

    if report.forwarder_derivation_error is not None:
        lines.append(
            "  FAIL [settings-home] bin/ forwarder set could not be derived from "
            f"coordinator/bin/: {report.forwarder_derivation_error}"
        )
    else:
        forwarders_ok = not report.forwarder_missing and not report.forwarder_unverified
        lines.append(
            f"  {'PASS' if forwarders_ok else 'FAIL'} [settings-home] "
            f"bin/ forwarders: {report.forwarder_present}/{report.forwarder_expected} verified"
        )
        for label, names in (
            ("missing", report.forwarder_missing),
            ("body not this root's", report.forwarder_unverified),
            ("door-owned", report.forwarder_door_owned),
            ("byte-copied (bytes match this root's source)", report.forwarder_byte_copied),
        ):
            if not names:
                continue
            preview = ", ".join(names[:10])
            more = "" if len(names) <= 10 else f" (+{len(names) - 10} more)"
            lines.append(f"    {label}: {preview}{more}")
    return lines
