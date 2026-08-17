"""Enumerate and verify the settings-home post-condition.

docs/plans/2026-08-17-machine-first-install-surface.md § C5: today the
settings home (``~/.coordinator-claude-settings``) is populated *emergently*
-- ``bin/`` forwarders, ``.percolate-identity``, the machine-local registry,
and ``machine-local/.claude-klabauter-root`` each land via their own install step, with
no single verified statement that the settings home is complete. This module
is that statement: it enumerates what a correct settings home contains and
checks presence, not intent, so ``scripts/setup.py``'s report line and the
``claude-klabauter.settings_home.complete`` doctor probe (bin/claude-klabauter-doctor-probe.py)
share one oracle instead of two that can drift apart.

Input, not invention -- state precedence explicitly. DoE-claude
(coordinator-claude) already declares the settings-home post-condition:

- ``coordinator-claude coordinator/docs/wiki/machine-local-registry.md``
  §4e / its settings-home inventory row (line ~837) names the top-level
  namespace: ``machine-local/`` (TOML registry), ``bin/`` (resolver family),
  ``coordinator-whoami/``, ``.coordinator-venv/``, ``settings-manifest.md``.
- ``coordinator-claude coordinator/docs/install/AGENT.md`` § Fail-loud
  claude-klabauter resolution names ``<settings-home>/machine-local/.claude-klabauter-root`` as
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

from coordinator_core.install.substrate import _derive_agent_helper_target_map

# (label, relative_path, kind, source) -- the DoE-declared top-level shape
# plus claude-klabauter's one additive member. kind is "dir" or "file".
_FIXED_MEMBERS: tuple[tuple[str, str, str, str], ...] = (
    (
        "machine-local/ (TOML registry dir)",
        "machine-local",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory",
    ),
    (
        "bin/ (resolver family + agent-helper forwarders)",
        "bin",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory",
    ),
    (
        "coordinator-whoami/",
        "coordinator-whoami",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory",
    ),
    (
        ".coordinator-venv/ (settings-home coordinator venv)",
        ".coordinator-venv",
        "dir",
        "DoE machine-local-registry.md §4e settings-home inventory",
    ),
    (
        "settings-manifest.md",
        "settings-manifest.md",
        "file",
        "DoE machine-local-registry.md §4e settings-home inventory",
    ),
    (
        "machine-local/.claude-klabauter-root (fail-loud rung-2 pointer)",
        "machine-local/.claude-klabauter-root",
        "file",
        "DoE docs/install/AGENT.md § Fail-loud claude-klabauter resolution, rung 2",
    ),
    (
        ".percolate-identity (publish audit config)",
        ".percolate-identity",
        "file",
        "claude-klabauter scripts/setup.py :: install_percolate_identity (additive, not a DoE member)",
    ),
)


@dataclass
class SettingsHomeMember:
    label: str
    path: Path
    kind: str
    present: bool
    source: str


@dataclass
class SettingsHomeReport:
    settings_home_path: Path
    members: list[SettingsHomeMember] = field(default_factory=list)
    forwarder_expected: int = 0
    forwarder_present: int = 0
    forwarder_missing: list[str] = field(default_factory=list)
    forwarder_derivation_error: str | None = None

    @property
    def fixed_missing(self) -> list[SettingsHomeMember]:
        return [m for m in self.members if not m.present]

    @property
    def complete(self) -> bool:
        if self.forwarder_derivation_error is not None:
            return False
        return not self.fixed_missing and not self.forwarder_missing


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


def check_settings_home(settings_home_path: Path, claude_klabauter_root: Path) -> SettingsHomeReport:
    """Stat the settings home and report what is actually there.

    Mutation-test contract: this must fail when the settings home stops
    being populated, not when the enumeration text drifts -- every check
    below is a live `Path.exists()`/dir-listing read against
    `settings_home_path`, never a read of a self-reported manifest
    (e.g. `bin/.coordinator-bin-manifest.json`, which the installer itself
    writes and would report green even if the installer silently failed to
    land a forwarder).
    """
    report = SettingsHomeReport(settings_home_path=settings_home_path)

    for label, rel, kind, source in _FIXED_MEMBERS:
        p = settings_home_path / rel
        present = p.is_dir() if kind == "dir" else p.is_file()
        report.members.append(
            SettingsHomeMember(label=label, path=p, kind=kind, present=present, source=source)
        )

    try:
        expected = expected_forwarders(claude_klabauter_root)
    except OSError as exc:
        report.forwarder_derivation_error = str(exc)
        return report

    report.forwarder_expected = len(expected)
    bin_dir = settings_home_path / "bin"
    missing: list[str] = []
    present_count = 0
    for installed_name in sorted(expected):
        if (bin_dir / installed_name).is_file():
            present_count += 1
        else:
            missing.append(installed_name)
    report.forwarder_present = present_count
    report.forwarder_missing = missing
    return report


def format_report_lines(report: SettingsHomeReport) -> list[str]:
    """Human-readable PASS/FAIL lines for the `scripts/setup.py` report step."""
    lines: list[str] = []
    for m in report.members:
        status = "PASS" if m.present else "FAIL"
        lines.append(f"  {status} [settings-home] {m.label} -> {m.path} ({m.source})")

    if report.forwarder_derivation_error is not None:
        lines.append(
            "  FAIL [settings-home] bin/ forwarder set could not be derived from "
            f"coordinator/bin/: {report.forwarder_derivation_error}"
        )
    else:
        lines.append(
            f"  {'PASS' if not report.forwarder_missing else 'FAIL'} [settings-home] "
            f"bin/ forwarders: {report.forwarder_present}/{report.forwarder_expected} present"
        )
        if report.forwarder_missing:
            preview = ", ".join(report.forwarder_missing[:10])
            more = "" if len(report.forwarder_missing) <= 10 else f" (+{len(report.forwarder_missing) - 10} more)"
            lines.append(f"    missing: {preview}{more}")
    return lines
