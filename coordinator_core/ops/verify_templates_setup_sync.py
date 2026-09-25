"""
coordinator_core.ops.verify_templates_setup_sync — drift oracle over the
setup/ tracked set: template<->live byte parity, template<->repo-root byte
parity, and (for publish_sync.py only) the claude-klabauter dispatch contract.

Purpose: inspect-only drift detector, run at preflight, install and P-11.
Reports OK / MISMATCH / LIVE_MISSING / TMPL_MISSING / NOT_PRESENT per
tracked relpath on the template<->live leg; SOURCE_OK / SOURCE_MISMATCH on
the template<->repo-root byte leg (every tracked file except
publish_sync.py); and CONTRACT_OK / CONTRACT_REFUSE on the publish_sync.py
would-refuse leg, once per copy reachable (template, repo-root). Exits
non-zero if any leg records a failure. There is no --fix — recovery is
manual and template-as-authoritative (`cp coordinator/templates/setup/<file>
~/.claude/setup/<file>`); a prior live->template --fix path was removed (it
directly contradicted the outward-only doctrine — see
docs/plans/2026-05-21-generic-percolation-via-coordinator-install.md § Step 3
and the DoE lesson 2026-07-06-verify-templates-setup-sync-sh-fix-is-ba.yaml).

Port of: verify-templates-setup-sync.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-21-generic-percolation-via-coordinator-install.md § Step 3 [DEAD-CITATION: plan file never committed to this repo]
Legs added (source leg, contract leg, manifest-tracked set): P077-C2,
docs/plans/2026-09-11-pairs-oracle-reads-the-doe-source.md.

Negative-spec:
    - Takes no flags. A stray positional arg (e.g. a stale caller still
      passing `--fix`) is NOT actioned as a copy trigger — a warning is
      printed to stderr and the verify proceeds anyway, faithfully
      reproducing the bash oracle's behavior (which likewise ignored `$1`
      after the warning).
    - When BOTH sides of the template<->live leg are absent for a relpath,
      that relpath is a graceful skip (NOT_PRESENT), not a failure — this
      matches the "verifier authored before the files it verifies"
      bootstrap pattern used by sibling verify-*-sync scripts.
    - The tracked set is the manifest (`setup_template_manifest.py`'s three
      `_MANIFEST_ATTRS` lists), never a hand-written list in this module.
      There is no `PAIRS` constant here; the tracked set is read fresh each
      run through the one loader `install/substrate.py` itself calls,
      so the oracle and the installer can never read different manifests.
    - The source leg (template vs `<repo_root>/setup/`) is a graceful,
      whole-run skip — one informational line, no per-row noise, no exit
      effect — when `<repo_root>/setup/` does not exist (OSS/marketplace
      install, no authoring clone). A tracked relpath present in the
      template but absent from an otherwise-present repo-root `setup/` is
      template-canonical and is not a failure either — only a relpath that
      exists on BOTH sides with differing bytes fails this leg.
    - `publish_sync.py` is judged on the source and template legs by
      claude-klabauter's own dispatch contract (`percolate.publish_sync_contract
      :: would_refuse`, AST-only, never imported), not by a byte compare —
      every copy legitimately carries additions the others lack. The live
      copy needs no contract check of its own: the template<->live byte leg
      already ties it to the template, so a live copy that matches the
      template inherits the template's contract result, and one that does
      not match is already a MISMATCH on that leg.
    - Exit code convention (parity-critical): 0 only if every leg reports a
      passing/skippable state. Any MISMATCH, LIVE_MISSING, TMPL_MISSING,
      SOURCE_MISMATCH or CONTRACT_REFUSE makes the whole run exit 1.
    - An unresolvable plugin root (CLAUDE_PLUGIN_ROOT unset) is a hard
      failure (exit 1, PluginRootUnresolved), not a Path.cwd() fallback —
      cwd is whatever directory the caller happened to invoke from, and a
      silent wrong-root would misreport every pair as TMPL_MISSING/OK
      against the wrong tree rather than surfacing the actual problem
      (caller failed to resolve/set CLAUDE_PLUGIN_ROOT). See
      _resolve_plugin_root()'s docstring for the caller-resolves-topology
      split this enforces.
    - An unreadable manifest (`SubstrateFatalError`) is the same fail-loud
      posture: exit 1 with a named line, never a silent fallback to a
      built-in list.
"""

from __future__ import annotations

import filecmp
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core._settings_home import claude_config_dir
from coordinator_core.engine_root import coordinator_engine_root_with_class
from coordinator_core.install.setup_template_manifest import (
    SubstrateFatalError,
    _load_setup_template_manifest,
)
from coordinator_core.ops.coordinator_doe_root import (
    repo_root_from_plugin_root_candidate,
)

_PUBLISH_SYNC_RELPATH = "publish_sync.py"


class PluginRootUnresolved(RuntimeError):
    """Raised by _resolve_plugin_root() when CLAUDE_PLUGIN_ROOT is unset.

    There is no fallback root for this module to guess at — see
    _resolve_plugin_root()'s docstring for why a cwd() fallback is a
    silent-wrong-root hazard, not a graceful default.
    """


def _resolve_plugin_root() -> Path:
    """Resolves the plugin root (coordinator/templates/setup/'s parent).

    This module does NOT derive the root from its own __file__ location:
    it is imported from the claude-klabauter side, not from a copy of the DoE
    coordinator/bin/ tree, so `__file__`-relative resolution would point at
    the wrong repo entirely (claude-klabauter has no templates/ tree at all).
    It also does NOT fall back to Path.cwd() when CLAUDE_PLUGIN_ROOT is
    unset — cwd is whatever directory the caller happened to invoke from,
    a silent-wrong-root twin of the __file__ hazard this module already
    guards against. Resolving the DoE-claude repo root is the CALLER's
    job (topology knowledge the caller has and this module does not): the
    coordinator/bin/verify-templates-setup-sync.py trampoline sets
    CLAUDE_PLUGIN_ROOT via the doe_root() registry helper before invoking
    main(); coordinator_core.plugin_health.sentinel's in-process probe P-11
    call sets it directly from its own resolved plugins_root. Both callers
    are responsible for setting the env var — this function's only job is
    to read it, or fail loud when it is missing.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root)
    raise PluginRootUnresolved(
        "CLAUDE_PLUGIN_ROOT is unset — cannot resolve the plugin root that "
        "owns templates/setup/. The caller must resolve the DoE-claude repo "
        "root (e.g. via the coordinator_registry.doe_root() ladder) and set "
        "CLAUDE_PLUGIN_ROOT before calling main()."
    )


def _tracked_relpaths(claude_klabauter_root: Path) -> List[str]:
    """The tracked set: the union of every list `_MANIFEST_ATTRS` names,
    read fresh through the one loader `install/substrate.py` itself calls
    at its setup-install step — never a hand list in this module. Iterates
    all three returned lists (files, exec_files, hook_files), not just the
    two that happen to be non-empty today."""
    files, exec_files, hook_files = _load_setup_template_manifest(claude_klabauter_root)
    return list(files) + list(exec_files) + list(hook_files)


def _ensure_percolate_on_path() -> None:
    """Add `coordinator/lib` to `sys.path` so `percolate.*` is importable —
    the same rung `coordinator_core/percolate/round.py` and
    `coordinator_core/ops/emit_withheld_knobs.py :: ensure_percolate_on_path`
    use. Not imported from `emit_withheld_knobs` directly: that module pulls
    `data_root`, `locked_write` and `declared_writes`, none of which this
    oracle needs. Resolved from THIS module's own `__file__`, like both of
    those rungs — not from `coordinator_engine_root_with_class()` — because
    the running tree (a live claude-klabauter checkout or the published klabauter
    mirror) always carries its own `coordinator/lib` alongside this file;
    a second resolution could name a tree that has not yet received this
    same change."""
    coordinator_lib = Path(__file__).resolve().parents[2] / "coordinator" / "lib"
    if str(coordinator_lib) not in sys.path:
        sys.path.insert(0, str(coordinator_lib))


def _contract_lines_for_publish_sync(
    templates_setup: Path, source_setup: Optional[Path]
) -> Tuple[List[str], int]:
    """CONTRACT_OK / CONTRACT_REFUSE lines for every reachable copy of
    publish_sync.py (template, repo-root) — never the live copy, which is
    already covered by the template<->live byte leg (§ Shape 4)."""
    from percolate.publish_sync_contract import would_refuse  # noqa: PLC0415

    lines: List[str] = []
    exit_code = 0

    template_path = templates_setup / _PUBLISH_SYNC_RELPATH
    if template_path.is_file():
        reasons = would_refuse(template_path)
        if reasons:
            lines.append(
                f"CONTRACT_REFUSE {_PUBLISH_SYNC_RELPATH} (template): "
                + "; ".join(reasons)
            )
            exit_code = 1
        else:
            lines.append(f"CONTRACT_OK     {_PUBLISH_SYNC_RELPATH} (template)")

    if source_setup is not None and source_setup.is_dir():
        source_path = source_setup / _PUBLISH_SYNC_RELPATH
        if source_path.is_file():
            reasons = would_refuse(source_path)
            if reasons:
                lines.append(
                    f"CONTRACT_REFUSE {_PUBLISH_SYNC_RELPATH} (repo-root): "
                    + "; ".join(reasons)
                )
                exit_code = 1
            else:
                lines.append(f"CONTRACT_OK     {_PUBLISH_SYNC_RELPATH} (repo-root)")

    return lines, exit_code


def check_pairs(
    templates_setup: Path,
    live_setup: Path,
    tracked_relpaths: List[str],
    source_setup: Optional[Path] = None,
) -> Tuple[List[str], int]:
    """Runs every leg over `tracked_relpaths`. Returns (report_lines, exit_code).

    `source_setup` is the DoE repo-root's `setup/` dir, or `None`/absent
    when the box has no authoring clone. Explicit arguments throughout —
    no environment variable is read below `main()` — so a test can inject
    every tree directly."""
    lines: List[str] = []
    exit_code = 0
    any_checked = False

    for relpath in tracked_relpaths:
        live_path = live_setup / relpath
        tmpl_path = templates_setup / relpath

        live_exists = live_path.is_file()
        tmpl_exists = tmpl_path.is_file()

        if not live_exists and not tmpl_exists:
            lines.append(
                f"NOT_PRESENT  {relpath} (neither live nor template exists "
                "yet — Step 1 will create them)"
            )
            continue

        if not live_exists:
            lines.append(
                f"LIVE_MISSING {relpath} (template exists but live copy "
                f"absent at {live_path})"
            )
            exit_code = 1
            continue

        if not tmpl_exists:
            lines.append(
                f"TMPL_MISSING {relpath} (live exists but template absent "
                f"at {tmpl_path})"
            )
            exit_code = 1
            continue

        any_checked = True

        if filecmp.cmp(str(live_path), str(tmpl_path), shallow=False):
            lines.append(f"OK           {relpath}")
        else:
            lines.append(f"MISMATCH     {relpath}")
            exit_code = 1

    source_dir_present = source_setup is not None and source_setup.is_dir()
    if not source_dir_present:
        lines.append(
            "SOURCE_SKIPPED repo-root setup/ absent — source legs skipped "
            "for this run (OSS/marketplace install, no authoring clone)"
        )
    else:
        for relpath in tracked_relpaths:
            if relpath == _PUBLISH_SYNC_RELPATH:
                continue
            source_path = source_setup / relpath
            tmpl_path = templates_setup / relpath
            if not source_path.is_file() or not tmpl_path.is_file():
                # Template-canonical (no repo-root copy) is not a failure.
                continue
            if filecmp.cmp(str(source_path), str(tmpl_path), shallow=False):
                lines.append(f"SOURCE_OK       {relpath}")
            else:
                lines.append(f"SOURCE_MISMATCH {relpath}")
                exit_code = 1

    contract_lines, contract_exit = _contract_lines_for_publish_sync(
        templates_setup, source_setup if source_dir_present else None
    )
    lines.extend(contract_lines)
    if contract_exit:
        exit_code = 1

    if not any_checked and exit_code == 0:
        lines.append("no files present — nothing to verify (Step 1 will create the live and template copies)")

    return lines, exit_code


def main(argv: List[str]) -> int:
    """CLI entry: resolve roots, warn on stray args, run check, print report."""
    if argv and argv[0] != "":
        print(
            "WARNING: verify-templates-setup-sync takes no flags (--fix was "
            "removed; inspect-only now).",
            file=sys.stderr,
        )
        print(
            "         Manual recovery is template-as-authoritative: "
            "cp coordinator/templates/setup/<file> ~/.claude/setup/<file>",
            file=sys.stderr,
        )

    try:
        plugin_root = _resolve_plugin_root()
    except PluginRootUnresolved as exc:
        print(f"verify-templates-setup-sync: {exc}", file=sys.stderr)
        return 1

    claude_klabauter_root, _resolution_class = coordinator_engine_root_with_class()
    claude_klabauter_root_path = Path(claude_klabauter_root)

    try:
        tracked_relpaths = _tracked_relpaths(claude_klabauter_root_path)
    except SubstrateFatalError as exc:
        print(f"verify-templates-setup-sync: {exc}", file=sys.stderr)
        return 1

    _ensure_percolate_on_path()

    templates_setup = plugin_root / "templates" / "setup"
    live_setup = claude_config_dir() / "setup"

    repo_root = repo_root_from_plugin_root_candidate(str(plugin_root))
    source_setup = Path(repo_root) / "setup"

    lines, exit_code = check_pairs(templates_setup, live_setup, tracked_relpaths, source_setup)
    for line in lines:
        print(line)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
