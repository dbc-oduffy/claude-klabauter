"""
coordinator_core.plugin_health.drift — read-only drift probe for registered plugin
live installs (git-state, venv-state, SHA-sentinel).

Purpose: detect when a live install has fallen behind its source — git-state drift
(commits-behind), venv-state drift (stale editable pin / MAPPING / entry-point shim),
and SHA-sentinel drift (copy_install mode). Surfaces daily via /workday-start Step
1.10 Addon Health.

Port of example-doctrine-repo coordinator/bin/check-plugin-drift.sh (recovered pre-stub body,
commit dd820339^ — the script had been silently replaced with an always-fail test
stub on disk; this port restores + ports the real 977-line body, not the stub).
Also folds in the plugin.mirrors registry reader formerly at coordinator/bin/lib/
read-mirrors.sh (an orphaned spawn-hidden.sh caller with no pre-assigned owning
tranche — this port is its landing site) and the settings-home resolution formerly
at coordinator/lib/settings-home.sh (mirrored here via coordinator_core._settings_home,
already a Python-importable seam post its own migration).

Five embedded bash heredocs collapse to four consolidated pure functions (each takes
an optional expected_pin_root/expected_root param covering both the default-mode and
editable_sibling_venv-mode call sites that used to be near-duplicate heredocs) plus
one batch TOML reader — there is no subprocess boundary inside a single Python
process, so the heredoc-vs-non-heredoc distinction this script used to carry
disappears entirely.

Two intentional behavior deltas vs the bash oracle (both plan-directed, not
regressions):
  1. eval() -> ast.literal_eval in the venv-MAPPING leg (both call sites). Behaviorally
     identical for the only content that ever appears there (a literal str:str dict);
     additionally now REJECTS anything eval() would silently execute if a malicious/
     corrupted finder file ever appeared (pure hardening).
  2. `git fetch ... || true` no longer silently swallows a fetch failure into an
     undifferentiated stale-ref success path. A failed fetch now emits a `[warn]`
     line to stderr naming the possibly-stale comparison, then CONTINUES with the
     stale-ref comparison (unchanged downstream behavior) — visible-but-non-blocking,
     not fail-open-silent.

Self-registration: importing this module calls register_op("plugin_health.drift", ...)
as a side-effect (same pattern as ops/engine_drift.py / ops/session_hierarchy_derive.py).

Spec backlink: docs/plans/2026-05-21-plugin-source-live-mirror-doctrine.md § Chunk 1
Extended by:    docs/plans/2026-05-23-copy-install-drift-coverage.md § Chunk 2
                docs/plans/2026-05-28-reverse-drift-gate-meta-repo-coverage.md § Chunk 3a
                docs/plans/2026-05-28-forward-drift-probe-content-equivalence.md § Chunk 1
                docs/plans/2026-05-29-windows-console-flash-elimination.md § Chunk 2
                docs/plans/2026-05-30-editable-sibling-venv-propagation-mode.md § Chunk 2
                docs/plans/2026-07-06-durable-substrate-to-settings-home.md § C2b
Port of: check-plugin-drift.sh (example-doctrine-repo b5a4192c, 2026-07-20; dd820339^ recovered
         pre-stub body), read-mirrors.sh (example-doctrine-repo 721a71f4, 2026-07-21)
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from coordinator_core._settings_home import settings_home
from coordinator_core.ipc import register_op

_PROG = "check-plugin-drift.sh"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class DriftResult:
    """ok=True -> clean (no drift); ok=False -> drift detected (or a probe error
    that must fail-loud as drift, mirroring the bash `return 1` fail-closed
    convention). `lines` are the stdout [drift]/[ok]/[info]/[warn] messages callers
    interleave in original emission order; `stderr_lines` are the small set of
    diagnostic lines the bash oracle sends to stderr (Leg-3 skip diagnostics,
    --check-clean-only skip diagnostics, git-status-failed warnings, the new
    fetch-failure warning)."""

    ok: bool
    lines: List[str] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)


def _run_git(args: List[str], cwd: Optional[Path] = None, timeout: int = 60):
    import subprocess

    try:
        # Review: code-reviewer — had a timeout but no stdin guard; an inherited
        # stdin can still hand an interactive prompt a controlling terminal before
        # the timeout kills the process. Windows console-popup suppression added too.
        return subprocess.run(
            ["git", *(["-C", str(cwd)] if cwd is not None else []), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 — mirrors bash's `|| true` / `|| echo unknown`
        class _Fail:
            returncode = 1
            stdout = ""
            stderr = str(exc)

        return _Fail()


# ---------------------------------------------------------------------------
# Leg-3 working-tree-root guard (pinned incidents d82ba601 / 9acb8ab9)
# ---------------------------------------------------------------------------


def git_worktree_root_if_self(path: Path) -> Optional[Path]:
    """Return the physical git work-tree root of `path` IFF `path` is itself that
    root (not a data-only dir nested in a parent repo, not a submodule). None
    otherwise. Canon via realpath (mirrors bash `cd && pwd -P`)."""
    path = Path(path)
    if not path.is_dir():
        return None
    canon = Path(os.path.realpath(str(path)))
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if result.returncode != 0:
        return None
    top_str = (result.stdout or "").strip().replace("\r", "")
    if not top_str:
        return None
    top_path = Path(top_str)
    if not top_path.is_dir():
        return None
    top_canon = Path(os.path.realpath(str(top_path)))
    return canon if top_canon == canon else None


# ---------------------------------------------------------------------------
# Consolidated venv-state leg functions (2a-2d) — one body, optional
# expected_pin_root/expected_root param covers both default-mode and
# editable_sibling_venv-mode call sites.
# ---------------------------------------------------------------------------


def _venv_pin_check(
    live_path: Path,
    dist_name: str,
    site_packages: Path,
    expected_pin_root: Optional[Path] = None,
) -> str:
    """Leg 2a. Returns one of: OK, NO_VENV, NO_DIST_INFO, NO_DIRECT_URL,
    DANGLING:<path>, WRONG_PATH:<path>, PARSE_ERROR:<msg>, ERROR:<msg>."""
    expected_root = expected_pin_root if expected_pin_root is not None else live_path
    site_pkg_dir = Path(site_packages)
    if not site_pkg_dir.exists():
        return "NO_VENV"
    # Review: code-reviewer — sort by mtime (newest first), not lexicographically:
    # lexicographic sort mis-orders semantic versions (e.g. "9.0" > "10.0") and could
    # pick a stale dist-info dir when multiple are present.
    dist_info_dirs = sorted(
        site_pkg_dir.glob(f"{dist_name}-*.dist-info"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not dist_info_dirs:
        return "NO_DIST_INFO"
    direct_url_path = dist_info_dirs[0] / "direct_url.json"
    if not direct_url_path.exists():
        return "NO_DIRECT_URL"
    try:
        direct_url = json.loads(direct_url_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — mirrors bash `except Exception as e`
        return f"PARSE_ERROR: {exc}"
    url = direct_url.get("url", "")
    if url.startswith("file:///"):
        pinned_str = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        if os.name == "nt" and len(pinned_str) > 2 and pinned_str[0] == "/" and pinned_str[2] == ":":
            pinned_str = pinned_str[1:]
        pinned_path = Path(pinned_str)
    else:
        pinned_path = Path(url)
    try:
        if not pinned_path.exists():
            return f"DANGLING:{pinned_path}"
        if pinned_path.resolve() != Path(expected_root).resolve():
            return f"WRONG_PATH:{pinned_path}"
        return "OK"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR:{exc}"


def _pyproject_hash(path: Path) -> str:
    """Leg 2b helper — SHA256 of a pyproject.toml (or any path)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _venv_mapping_check(site_packages: Path, expected_root: Path) -> str:
    """Leg 2c. ``eval()`` -> ``ast.literal_eval`` (plan-directed hardening, both
    call sites): behaviorally identical for the only content that ever appears
    here (a literal str:str dict), but rejects anything eval() would silently
    execute on a malicious/corrupted finder file. Returns one of: OK,
    NO_SITE_PKGS, NO_FINDER, STALE:<p1>|<p2>|<p3>."""
    site_pkg_dir = Path(site_packages)
    if not site_pkg_dir.exists():
        return "NO_SITE_PKGS"
    finder_files = list(site_pkg_dir.glob("__editable__*_finder.py"))
    if not finder_files:
        return "NO_FINDER"
    expected_root = Path(expected_root).resolve()
    stale_paths: List[str] = []
    for finder in finder_files:
        try:
            src = finder.read_text(encoding="utf-8")
            m = re.search(r"MAPPING\s*=\s*(\{[^}]*\})", src, re.DOTALL)
            if not m:
                return "NO_FINDER"
            try:
                mapping_dict = ast.literal_eval(m.group(1))
            except (ValueError, SyntaxError) as exc:
                print(
                    f"[warn] {_PROG}: venv-mapping: unparsable MAPPING literal in "
                    f"{finder} — treating as NO_FINDER: {exc}",
                    file=sys.stderr,
                )
                return "NO_FINDER"
            if not isinstance(mapping_dict, dict):
                continue
            for path_str in mapping_dict.values():
                c = Path(str(path_str))
                if not c.exists():
                    stale_paths.append(str(c))
                    continue
                try:
                    c.resolve().relative_to(expected_root)
                except ValueError:
                    stale_paths.append(str(c))
        except Exception:  # noqa: BLE001 — mirrors bash's blanket per-finder except
            pass
    if stale_paths:
        return "STALE:" + "|".join(stale_paths[:3])
    return "OK"


def _venv_shim_check(pyproject_source_path: Path, live_path: Path, is_windows: bool) -> List[str]:
    """Leg 2d. Returns the list of missing entry-point shim names (empty if
    none, or if no [project.scripts] table, or no TOML parser available)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            print(
                f"[warn] {_PROG}: venv-shim: no TOML parser available (tomllib/tomli) "
                "— skipping entry-point shim check",
                file=sys.stderr,
            )
            return []
    pyproject = Path(pyproject_source_path)
    if not pyproject.exists():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(
            f"[warn] {_PROG}: venv-shim: could not parse {pyproject}: {exc} — "
            "skipping entry-point shim check",
            file=sys.stderr,
        )
        return []
    scripts = data.get("project", {}).get("scripts", {})
    if not scripts:
        return []
    venv = Path(live_path) / ".venv"
    missing: List[str] = []
    for ep_name in scripts:
        if is_windows:
            shim = venv / "Scripts" / f"{ep_name}.exe"
            shim_ne = venv / "Scripts" / ep_name
        else:
            shim = venv / "bin" / ep_name
            shim_ne = shim
        if not shim.exists() and not shim_ne.exists():
            missing.append(ep_name)
    return missing


def _refresh_log_baseline_hash(refresh_log: Path, plugin_name: str) -> str:
    """Mirrors the bash `grep ... | grep pyproject_hash= | tail -1 | sed ...`
    baseline-hash lookup against the refresh audit log."""
    if not refresh_log.exists():
        return ""
    try:
        text = refresh_log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[warn] {_PROG}: could not read {refresh_log}: {exc}", file=sys.stderr)
        return ""
    baseline = ""
    for line in text.splitlines():
        if f" {plugin_name} " not in line or "pyproject_hash=" not in line:
            continue
        m = re.search(r"pyproject_hash=([a-f0-9]*)", line)
        if m:
            baseline = m.group(1)
    return baseline.replace("\r", "")


def _site_packages_dir(venv_dir: Path, is_windows: bool) -> Optional[Path]:
    if is_windows:
        candidate = venv_dir / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    candidates = sorted((venv_dir / "lib").glob("python*/site-packages")) if (venv_dir / "lib").is_dir() else []
    for c in candidates:
        if c.is_dir():
            return c
    return None


# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------


def _check_source_is_live(plugin_name: str) -> DriftResult:
    return DriftResult(
        ok=True,
        lines=[f"[ok] {plugin_name}: propagation_mode=source_is_live -- n/a by design"],
    )


def _check_copy_install(
    plugin_name: str,
    source_path: str,
    live_path: str,
    source_subpath: str,
    check_clean_only: bool,
    claude_home: Path,
) -> DriftResult:
    lines: List[str] = []
    ci_live_path = Path(live_path.replace("\\", "/")) if live_path else None
    ci_source_path = Path(source_path.replace("\\", "/")) if source_path else None

    # --check-clean-only: copy_install has no live git working tree; re-running
    # the idempotent copy installer is always safe — nothing to block on.
    if check_clean_only:
        return DriftResult(ok=True)

    if ci_live_path is None or not ci_live_path.is_dir():
        lines.append(f"[drift] {plugin_name}: copy_install — live_path missing or not a directory: '{live_path}'")
        return DriftResult(ok=False, lines=lines)

    sentinel_file = ci_live_path / "version.txt"
    if not sentinel_file.is_file():
        lines.append(f"[info] {plugin_name}: copy_install — no version.txt sentinel (installer did not write one yet; see example-game-repo memo)")
        return DriftResult(ok=True, lines=lines)

    sentinel_sha = sentinel_file.read_text(encoding="utf-8", errors="replace").strip("\r\n")
    if len(sentinel_sha) != 40 or not re.match(r"^[0-9a-f]+$", sentinel_sha):
        lines.append(f"[warn] {plugin_name}: version.txt malformed (len={len(sentinel_sha)}) — refresh to rewrite sentinel")
        return DriftResult(ok=True, lines=lines)

    head_result = _run_git(["rev-parse", "HEAD"], cwd=ci_source_path)
    if head_result.returncode != 0:
        lines.append(f"[warn] {plugin_name}: copy_install — could not read source HEAD from '{source_path}'")
        return DriftResult(ok=True, lines=lines)
    source_head = (head_result.stdout or "").strip().replace("\r", "")

    if sentinel_sha == source_head:
        lines.append(f"[ok] {plugin_name}: copy_install — sentinel matches source HEAD ({sentinel_sha[:12]})")
        return DriftResult(ok=True, lines=lines)

    resolved_subpath = source_subpath or f"plugin/{plugin_name}"
    full_source_subpath = ci_source_path / resolved_subpath if ci_source_path else None

    if full_source_subpath is None or not full_source_subpath.is_dir():
        lines.append(
            f"[warn] {plugin_name}: copy_install — sentinel mismatch and source_subpath "
            f"'{resolved_subpath}' not found; cannot run content-equivalence check"
        )
        return DriftResult(ok=True, lines=lines)

    ls_result = _run_git(["ls-tree", "-r", "HEAD", "--", resolved_subpath], cwd=ci_source_path)
    if ls_result.returncode != 0:
        lines.append(
            f"[warn] {plugin_name}: copy_install — could not enumerate source tracked files "
            f"(git ls-tree failed): {(ls_result.stdout or '') + (ls_result.stderr or '')}"
        )
        return DriftResult(ok=True, lines=lines)

    ls_tree_out = ls_result.stdout or ""
    if not ls_tree_out.strip():
        lines.append(
            f"[warn] {plugin_name}: copy_install — git ls-tree returned empty for source_subpath "
            f"'{resolved_subpath}'; cannot run content-equivalence check"
        )
        return DriftResult(ok=True, lines=lines)

    mismatched_paths: List[str] = []
    for ls_line in ls_tree_out.splitlines():
        if not ls_line:
            continue
        # "<mode> <type> <sha>\t<path>"
        try:
            meta, tracked_path = ls_line.split("\t", 1)
        except ValueError:
            print(
                f"[warn] {_PROG}: copy_install: unparsable git ls-tree line, skipping: {ls_line!r}",
                file=sys.stderr,
            )
            continue
        meta_parts = meta.split()
        if len(meta_parts) < 3:
            continue
        entry_type, blob_sha = meta_parts[1], meta_parts[2]
        if entry_type != "blob" or not tracked_path:
            continue

        rel_path = tracked_path
        prefix = f"{resolved_subpath}/"
        if rel_path.startswith(prefix):
            rel_path = rel_path[len(prefix):]
        live_file = ci_live_path / rel_path

        if not live_file.is_file():
            mismatched_paths.append(f"{tracked_path} (missing in live)")
            continue

        # -C claude_home (NOT ci_live_path): applies the LIVE REPO's clean filter
        # (incl. autocrlf) — apples-to-apples with the source blob SHA. Pinned by
        # the AC5/crlf regression test (crlf-divergent-but-content-equal).
        hash_result = _run_git(["hash-object", str(live_file)], cwd=claude_home)
        if hash_result.returncode != 0:
            mismatched_paths.append(f"{tracked_path} (hash-object failed)")
            continue
        live_sha = (hash_result.stdout or "").strip().replace("\r", "")
        if blob_sha != live_sha:
            mismatched_paths.append(tracked_path)

    if not mismatched_paths:
        lines.append(
            f"[ok-via-git-propagation] {plugin_name}: copy_install — live content matches source HEAD "
            f"({source_head[:12]}); sentinel at {sentinel_sha[:12]} (lagging — will refresh on next install)"
        )
        return DriftResult(ok=True, lines=lines)

    count = len(mismatched_paths)
    lines.append(
        f"[drift] copy_install: {plugin_name} — sentinel {sentinel_sha[:12]} ≠ source HEAD "
        f"{source_head[:12]} AND content differs:"
    )
    for i, mp in enumerate(mismatched_paths):
        if i >= 10:
            lines.append(f"  … + {count - i} more")
            break
        lines.append(f"  {mp}")
    return DriftResult(ok=False, lines=lines)


def _check_editable_sibling_venv(
    plugin_name: str,
    source_path: str,
    live_path: str,
    track_ref: str,
    dist_name: str,
    is_windows: bool,
    check_clean_only: bool,
    refresh_log: Path,
    current_pyproject_hash_override: Optional[str],
) -> DriftResult:
    lines: List[str] = []
    stderr_lines: List[str] = []
    drift = False

    if check_clean_only:
        return DriftResult(ok=True)

    esv_live_path = Path(live_path.replace("\\", "/")) if live_path else None
    esv_source_path = Path(source_path.replace("\\", "/")) if source_path else None

    if esv_live_path is None or not esv_live_path.is_dir():
        lines.append(
            f"[drift] {plugin_name} (editable_sibling_venv): live_path (host venv dir) missing or not "
            f"a directory: '{live_path}'"
        )
        return DriftResult(ok=False, lines=lines)

    # git-state leg: runs on source_path, not live_path.
    if track_ref == "live":
        lines.append(f"[info] {plugin_name} (editable_sibling_venv): git-state: live (track_ref=live sentinel) — skipped")
    else:
        if esv_source_path is None or not esv_source_path.is_dir():
            lines.append(
                f"[drift] {plugin_name} (editable_sibling_venv): source_path (addon git repo) missing or "
                f"not a directory: '{source_path}'"
            )
            drift = True
        else:
            fetch_result = _run_git(["fetch", "-q", "origin"], cwd=esv_source_path)
            if fetch_result.returncode != 0:
                stderr_lines.append(
                    f"[warn] {plugin_name}: git fetch failed (offline?) — git-state leg comparing "
                    "against a possibly-stale local ref"
                )
            behind_result = _run_git(["rev-list", "--count", f"HEAD..{track_ref}"], cwd=esv_source_path)
            behind_str = (behind_result.stdout or "").strip().replace("\r", "")
            behind = int(behind_str) if behind_str.isdigit() else None
            if behind is not None and behind > 0:
                sha_result = _run_git(["rev-parse", track_ref], cwd=esv_source_path)
                esv_src_sha = (sha_result.stdout or "unknown").strip()[:12] if sha_result.returncode == 0 else "unknown"
                lines.append(
                    f"[drift] git-state: {plugin_name} (editable_sibling_venv) is {behind} commits "
                    f"behind {track_ref} (source HEAD: {esv_src_sha})"
                )
                drift = True

    esv_venv_dir = esv_live_path / ".venv"
    esv_site_packages = _site_packages_dir(esv_venv_dir, is_windows)

    if esv_site_packages is None and esv_venv_dir.is_dir():
        lines.append(f"[drift] {plugin_name} (editable_sibling_venv): venv-state: site-packages dir not found under {esv_venv_dir}")
        drift = True

    # Leg 2a: expected_pin_root = source_path.
    if esv_venv_dir.is_dir():
        result = _venv_pin_check(esv_live_path, dist_name, esv_site_packages or esv_venv_dir, esv_source_path)
        if result in ("OK", "NO_VENV"):
            pass
        elif result == "NO_DIST_INFO":
            lines.append(f"[drift] venv-pin: {plugin_name} (editable_sibling_venv) not installed in host venv — run refresh-plugin-live-install.sh")
            drift = True
        elif result == "NO_DIRECT_URL":
            lines.append(f"[drift] venv-pin: {plugin_name} (editable_sibling_venv) direct_url.json not found -- re-run refresh-plugin-live-install.sh")
            drift = True
        elif result.startswith("DANGLING:"):
            lines.append(f"[drift] venv-pin: {plugin_name} (editable_sibling_venv) direct_url.json dangling: {result[len('DANGLING:'):]}")
            drift = True
        elif result.startswith("WRONG_PATH:"):
            lines.append(f"[drift] venv-pin: {plugin_name} (editable_sibling_venv) editable pin points to wrong checkout: {result[len('WRONG_PATH:'):]}")
            drift = True
        else:
            lines.append(f"[drift] venv-pin: {plugin_name} (editable_sibling_venv) check error: {result}")
            drift = True

    # Leg 2b: reads source_path/pyproject.toml, not live_path's.
    esv_pyproject_path = esv_source_path / "pyproject.toml" if esv_source_path else None
    if esv_pyproject_path is not None and esv_pyproject_path.is_file():
        try:
            esv_current_hash = _pyproject_hash(esv_pyproject_path)
        except OSError as exc:
            print(
                f"[warn] {_PROG}: venv-pyproject: could not hash {esv_pyproject_path}: {exc} "
                "— skipping pyproject-drift check",
                file=sys.stderr,
            )
            esv_current_hash = ""
        esv_baseline_hash = current_pyproject_hash_override or ""
        if not esv_baseline_hash and refresh_log.is_file():
            esv_baseline_hash = _refresh_log_baseline_hash(refresh_log, plugin_name)
        if esv_current_hash and esv_baseline_hash:
            if esv_current_hash != esv_baseline_hash:
                lines.append(f"[drift] venv-pyproject: {plugin_name} (editable_sibling_venv) pyproject.toml changed since last refresh")
                drift = True
        elif esv_current_hash:
            if refresh_log.is_file():
                lines.append(f"[info] venv-pyproject: {plugin_name} (editable_sibling_venv) no refresh baseline found")
            else:
                lines.append(f"[info] venv-pyproject: {plugin_name} (editable_sibling_venv) no refresh log found")

    # Leg 2c: MAPPING paths resolve relative to source_path, not live_path.
    if esv_site_packages is not None and esv_site_packages.is_dir():
        mresult = _venv_mapping_check(esv_site_packages, esv_source_path or esv_live_path)
        if mresult.startswith("STALE:"):
            lines.append(f"[drift] venv-mapping: {plugin_name} (editable_sibling_venv) editable MAPPING has stale paths")
            drift = True

    # Leg 2d: reads source_path/pyproject.toml; shims live in live_path/.venv.
    if esv_pyproject_path is not None and esv_pyproject_path.is_file():
        missing = _venv_shim_check(esv_pyproject_path, esv_live_path, is_windows)
        for shim_name in missing:
            lines.append(f"[drift] venv-shim: {plugin_name} (editable_sibling_venv) entry-point shim missing for '{shim_name}'")
            drift = True

    if not drift:
        lines.append(f"[ok] {plugin_name} (editable_sibling_venv): all legs clean")

    return DriftResult(ok=not drift, lines=lines, stderr_lines=stderr_lines)


def _check_default(
    plugin_name: str,
    live_path: str,
    track_ref: str,
    dist_name: str,
    is_windows: bool,
    check_clean_only: bool,
    refresh_log: Path,
    current_pyproject_hash_override: Optional[str],
) -> DriftResult:
    lines: List[str] = []
    stderr_lines: List[str] = []
    drift = False

    norm_live_path = Path(live_path.replace("\\", "/")) if live_path else None

    if norm_live_path is None or not norm_live_path.is_dir():
        lines.append(f"[drift] {plugin_name}: live_path missing or not a directory: '{live_path}'")
        return DriftResult(ok=False, lines=lines)

    if check_clean_only:
        wtr = git_worktree_root_if_self(norm_live_path)
        if wtr is None:
            if _run_git(["rev-parse", "--git-dir"], cwd=norm_live_path).returncode == 0:
                stderr_lines.append(
                    f"[info] {plugin_name}: --check-clean-only skipped — live_path is not its own "
                    "work-tree root (nested dir or submodule)"
                )
            return DriftResult(ok=True, stderr_lines=stderr_lines)
        status_result = _run_git(["status", "--porcelain"], cwd=norm_live_path)
        if status_result.returncode != 0:
            return DriftResult(ok=False, stderr_lines=stderr_lines)
        porcelain = (status_result.stdout or "").replace("\r", "")
        if porcelain.strip():
            lines.append(f"[drift] {plugin_name}: working-tree: live checkout has uncommitted edits")
            return DriftResult(ok=False, lines=lines, stderr_lines=stderr_lines)
        return DriftResult(ok=True, stderr_lines=stderr_lines)

    # Leg 1: git-state.
    fetch_result = _run_git(["fetch", "-q", "origin"], cwd=norm_live_path)
    if fetch_result.returncode != 0:
        stderr_lines.append(
            f"[warn] {plugin_name}: git fetch failed (offline?) — git-state leg comparing "
            "against a possibly-stale local ref"
        )
    behind_result = _run_git(["rev-list", "--count", f"HEAD..{track_ref}"], cwd=norm_live_path)
    behind_str = (behind_result.stdout or "").strip().replace("\r", "")
    behind = int(behind_str) if behind_str.isdigit() else None
    if behind is not None and behind > 0:
        sha_result = _run_git(["rev-parse", track_ref], cwd=norm_live_path)
        src_sha = (sha_result.stdout or "unknown").strip()[:12] if sha_result.returncode == 0 else "unknown"
        lines.append(f"[drift] git-state: {plugin_name} is {behind} commits behind {track_ref} (source HEAD: {src_sha})")
        drift = True

    venv_dir = norm_live_path / ".venv"
    site_packages = _site_packages_dir(venv_dir, is_windows)

    # Leg 2a: expected_pin_root defaults to live_path. NO_DIST_INFO is deliberately
    # NOT drift here (contra editable_sibling_venv's leg, where NO_DIST_INFO means
    # the addon isn't installed in the host venv at all -- default-mode plugins
    # are not required to have an editable install present, so its absence is
    # not itself a signal of staleness). Verified against the bash oracle
    # (dd820339^): the asymmetry is present there too, with an explicit review
    # comment on the editable_sibling_venv leg confirming the same intent.
    if venv_dir.is_dir():
        result = _venv_pin_check(norm_live_path, dist_name, site_packages or venv_dir)
        if result in ("OK", "NO_VENV", "NO_DIST_INFO"):
            pass
        elif result == "NO_DIRECT_URL":
            lines.append(f"[drift] venv-pin: {plugin_name} direct_url.json not found -- re-run refresh-plugin-live-install.sh")
            drift = True
        elif result.startswith("DANGLING:"):
            lines.append(f"[drift] venv-pin: {plugin_name} direct_url.json dangling: {result[len('DANGLING:'):]}")
            drift = True
        elif result.startswith("WRONG_PATH:"):
            lines.append(f"[drift] venv-pin: {plugin_name} editable pin points to wrong checkout: {result[len('WRONG_PATH:'):]}")
            drift = True
        else:
            lines.append(f"[drift] venv-pin: {plugin_name} check error: {result}")
            drift = True

    # Leg 2b.
    pyproject_path = norm_live_path / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            current_hash = _pyproject_hash(pyproject_path)
        except OSError as exc:
            print(
                f"[warn] {_PROG}: venv-pyproject: could not hash {pyproject_path}: {exc} "
                "— skipping pyproject-drift check",
                file=sys.stderr,
            )
            current_hash = ""
        baseline_hash = current_pyproject_hash_override or ""
        if not baseline_hash and refresh_log.is_file():
            baseline_hash = _refresh_log_baseline_hash(refresh_log, plugin_name)
        if current_hash and baseline_hash:
            if current_hash != baseline_hash:
                lines.append(f"[drift] venv-pyproject: {plugin_name} pyproject.toml changed since last refresh")
                drift = True
        elif current_hash:
            if refresh_log.is_file():
                lines.append(f"[info] venv-pyproject: {plugin_name} no refresh baseline found")
            else:
                lines.append(f"[info] venv-pyproject: {plugin_name} no refresh log found")

    # Leg 2c.
    if site_packages is not None and site_packages.is_dir():
        mresult = _venv_mapping_check(site_packages, norm_live_path)
        if mresult.startswith("STALE:"):
            lines.append(f"[drift] venv-mapping: {plugin_name} editable MAPPING has stale paths")
            drift = True

    # Leg 2d.
    if pyproject_path.is_file():
        missing = _venv_shim_check(pyproject_path, norm_live_path, is_windows)
        for shim_name in missing:
            lines.append(f"[drift] venv-shim: {plugin_name} entry-point shim missing for '{shim_name}'")
            drift = True

    # Leg 3: working-tree cleanliness — only when live_path is its own work-tree root.
    leg3_wtr = git_worktree_root_if_self(norm_live_path)
    if leg3_wtr is not None:
        status_result = _run_git(["status", "--porcelain"], cwd=norm_live_path)
        if status_result.returncode != 0:
            stderr_lines.append(f"[warn] {plugin_name}: working-tree cleanliness unknown — git status failed")
            drift = True
        else:
            porcelain = (status_result.stdout or "").replace("\r", "")
            if porcelain.strip():
                lines.append(f"[drift] working-tree: {plugin_name} live checkout has uncommitted edits -- refresh blocked")
                drift = True
    else:
        if _run_git(["rev-parse", "--git-dir"], cwd=norm_live_path).returncode == 0:
            stderr_lines.append(
                f"[info] {plugin_name}: Leg 3 skipped — live_path is not its own work-tree root "
                "(nested dir or submodule); working-tree cleanliness not checked"
            )

    return DriftResult(ok=not drift, lines=lines, stderr_lines=stderr_lines)


def check_plugin(
    plugin_name: str,
    source_path: str,
    live_path: str,
    track_ref: str,
    dist_name: str,
    prop_mode: str,
    source_subpath: str,
    *,
    check_clean_only: bool = False,
    is_windows: bool = False,
    refresh_log: Optional[Path] = None,
    current_pyproject_hash_override: Optional[str] = None,
    claude_home: Optional[Path] = None,
) -> DriftResult:
    """Dispatch to the four propagation-mode legs. Mirrors bash `_check_plugin`."""
    refresh_log = refresh_log if refresh_log is not None else Path.home() / ".claude" / "plugins" / ".refresh-log"
    claude_home = claude_home if claude_home is not None else Path(os.environ.get("HOME") or str(Path.home())) / ".claude"

    if prop_mode == "source_is_live":
        return _check_source_is_live(plugin_name)
    if prop_mode == "copy_install":
        return _check_copy_install(plugin_name, source_path, live_path, source_subpath, check_clean_only, claude_home)
    if prop_mode == "editable_sibling_venv":
        return _check_editable_sibling_venv(
            plugin_name,
            source_path,
            live_path,
            track_ref,
            dist_name,
            is_windows,
            check_clean_only,
            refresh_log,
            current_pyproject_hash_override,
        )
    # Review: code-reviewer — an unrecognized (e.g. typo'd) propagation_mode must
    # fail loud, not silently fall through to _check_default as if it were the
    # legitimate empty-string default. Only "" reaches this point intentionally.
    if prop_mode:
        return DriftResult(
            ok=False,
            lines=[
                f"[drift] {plugin_name}: unrecognized propagation_mode '{prop_mode}' -- registry entry "
                "misconfigured (expected one of: source_is_live, copy_install, editable_sibling_venv, "
                "or empty for default)"
            ],
        )
    return _check_default(
        plugin_name,
        live_path,
        track_ref,
        dist_name,
        is_windows,
        check_clean_only,
        refresh_log,
        current_pyproject_hash_override,
    )


# ---------------------------------------------------------------------------
# Registry reading (folds in coordinator/bin/lib/read-mirrors.sh)
# ---------------------------------------------------------------------------


def _load_tomllib():
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

        return tomllib


def read_all_mirrors(registry_path: Path) -> Dict[str, dict]:
    """Parse plugin.mirrors entries out of a machine-local registry TOML.

    Merges two valid registry-write forms (nested table wins on conflict):
      1. Nested table: [plugin.mirrors.<name>] entries.
      2. Flat dotted-key: "plugin.mirrors.<name>.<field>" = "..." top-level keys
         (written by `machine-local set`).

    Returns {} when the registry has no plugin.mirrors (mirrors bash's NO_MIRRORS
    sentinel, translated to an empty dict for a pure-Python caller). Every returned
    entry carries resolved defaults (track_ref="origin/main",
    dist_name=plugin_name.replace('-', '_')) — the defaulting the bash pipe-format
    applied before printing.
    """
    return _apply_mirror_defaults(_read_raw_mirrors(registry_path))


def _read_raw_mirrors(registry_path: Path) -> Dict[str, dict]:
    """Single-file plugin.mirrors parse WITHOUT defaults applied — the raw
    declared fields only, so ``read_merged_mirrors`` can merge per-field across
    registry files before defaulting (defaulting first would let a
    higher-precedence file's synthesized default clobber a lower-precedence
    file's explicit declaration)."""
    registry_path = Path(registry_path)
    if not registry_path.exists():
        return {}
    tomllib = _load_tomllib()
    data = tomllib.loads(registry_path.read_text(encoding="utf-8"))

    mirrors: Dict[str, dict] = {}
    nested = data.get("plugin", {}).get("mirrors", {})
    if isinstance(nested, dict):
        for plugin_name, entry in nested.items():
            if isinstance(entry, dict):
                mirrors[plugin_name] = dict(entry)

    prefix = "plugin.mirrors."
    for raw_key, raw_val in data.items():
        if not isinstance(raw_key, str) or not raw_key.startswith(prefix):
            continue
        rest = raw_key[len(prefix):]
        parts = rest.split(".", 1)
        if len(parts) != 2:
            continue
        plugin_name, field_name = parts
        mirrors.setdefault(plugin_name, {})
        if field_name not in mirrors[plugin_name]:
            mirrors[plugin_name][field_name] = raw_val

    return mirrors


def _apply_mirror_defaults(mirrors: Dict[str, dict]) -> Dict[str, dict]:
    """The defaulting the bash pipe-format applied before printing — split out
    of ``read_all_mirrors`` so merged multi-file reads default AFTER merging."""
    for plugin_name, entry in mirrors.items():
        entry.setdefault("propagation_mode", entry.get("propagation_mode", ""))
        entry.setdefault("source_path", entry.get("source_path", ""))
        entry.setdefault("live_path", entry.get("live_path", ""))
        entry.setdefault("track_ref", "origin/main")
        entry.setdefault("dist_name", plugin_name.replace("-", "_"))
        entry.setdefault("reverse_drift_cmd", entry.get("reverse_drift_cmd", ""))
    return mirrors


def read_merged_mirrors(registry_paths: Sequence[Path]) -> Dict[str, dict]:
    """Per-key precedence merge of plugin.mirrors across the precedence-ordered
    registry files (``registry.local.toml`` first, tracked ``registry.toml``
    second — ``machine_resolver.registry_get`` semantics): an earlier file's
    non-empty field wins; later files fill fields (and whole plugin entries)
    the earlier ones left absent or empty. Replaces the first-FILE-wins read
    under which a plugin registered only in the tracked ``registry.toml`` was
    invisible whenever a ``registry.local.toml`` existed. Defaults are applied
    once, after the merge (see ``_apply_mirror_defaults``)."""
    merged: Dict[str, dict] = {}
    for path in registry_paths:
        for plugin_name, entry in _read_raw_mirrors(path).items():
            target = merged.setdefault(plugin_name, {})
            for field_name, val in entry.items():
                if not target.get(field_name, ""):
                    target[field_name] = val
    return _apply_mirror_defaults(merged)


def lookup_all_source_subpaths(plugin_names: List[str], registry_path: Path) -> Dict[str, str]:
    """Batch source_subpath lookup for copy_install plugins (nested-table form
    first, then the flat dotted-key form `machine-local set` writes). Standalone
    reader (no default-mode twin) — pure port of the bash `_lookup_all_source_
    subpaths` heredoc (L886-919 of the recovered original)."""
    registry_path = Path(registry_path)
    if not registry_path.exists() or not plugin_names:
        return {}
    tomllib = _load_tomllib()
    try:
        data = tomllib.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — mirrors bash's silent-degrade-to-empty
        return {}

    result: Dict[str, str] = {}
    for plugin_name in plugin_names:
        val = data.get("plugin", {}).get("mirrors", {}).get(plugin_name, {}).get("source_subpath")
        if val is None:
            flat_key = f"plugin.mirrors.{plugin_name}.source_subpath"
            val = data.get(flat_key)
        if val is not None:
            result[plugin_name] = val
    return result


def _resolve_registry_dir() -> Path:
    override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    if override:
        return Path(override)
    return settings_home() / "machine-local"


def _resolve_claude_home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home())) / ".claude"


def _resolve_refresh_log() -> Path:
    return _resolve_claude_home() / "plugins" / ".refresh-log"


# ---------------------------------------------------------------------------
# CLI / op entry
# ---------------------------------------------------------------------------

_USAGE = """Usage: check-plugin-drift.sh [<plugin>] [--check-clean-only]
Two-leg drift probe. Exit 0=clean, 1=drift detected.
Environment: MACHINE_LOCAL_REGISTRY_DIR, HOME
"""


def _detect_is_windows() -> bool:
    return os.name == "nt" or bool(os.environ.get("WINDIR"))


def _run(filter_plugin: str, check_clean_only: bool):
    """Core scan — used by both `main()` (CLI) and the `plugin_health.drift` op.
    Returns (stdout_lines, stderr_lines, exit_code)."""
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    registry_dir = _resolve_registry_dir()
    # Per-key precedence (local wins per key, tracked fills gaps) — NOT
    # first-file-wins; see read_merged_mirrors.
    registry_files = [
        p
        for p in (registry_dir / "registry.local.toml", registry_dir / "registry.toml")
        if p.is_file()
    ]

    if not registry_files:
        stdout_lines.append(f"{_PROG}: no plugin.mirrors registry found -- skip (no plugin.mirrors registered)")
        return stdout_lines, stderr_lines, 0

    try:
        mirrors = read_merged_mirrors(registry_files)
    except Exception as exc:  # noqa: BLE001
        stderr_lines.append(f"{_PROG}: failed to read registry: {exc}")
        return stdout_lines, stderr_lines, 2

    if not mirrors:
        stdout_lines.append(f"{_PROG}: no plugin.mirrors registered -- nothing to check")
        return stdout_lines, stderr_lines, 0

    # Review: code-reviewer — a filter_plugin that matches no registered entry must
    # fail loud (typo'd name is otherwise indistinguishable from "plugin is healthy").
    if filter_plugin and filter_plugin not in mirrors:
        stderr_lines.append(
            f"{_PROG}: unknown plugin '{filter_plugin}' -- not found in plugin.mirrors registry"
        )
        return stdout_lines, stderr_lines, 2

    is_windows = _detect_is_windows()
    claude_home = _resolve_claude_home()
    refresh_log = claude_home / "plugins" / ".refresh-log"
    current_pyproject_hash_override = os.environ.get("CURRENT_PYPROJECT_HASH_OVERRIDE") or None

    copy_install_names = [
        name
        for name, entry in mirrors.items()
        if (not filter_plugin or name == filter_plugin) and entry.get("propagation_mode") == "copy_install"
    ]
    # Same per-key precedence for source_subpath: later (tracked) file first,
    # then overlay the earlier (local) file so its declarations win.
    subpath_map: Dict[str, str] = {}
    if copy_install_names:
        for reg in reversed(registry_files):
            for name, subpath in lookup_all_source_subpaths(copy_install_names, reg).items():
                if subpath:  # empty-string-is-miss: never clobber with a blank declaration
                    subpath_map[name] = subpath

    total_drift = 0
    for plugin_name, entry in mirrors.items():
        if filter_plugin and plugin_name != filter_plugin:
            continue
        prop_mode = entry.get("propagation_mode", "")
        source_subpath = subpath_map.get(plugin_name, "") if prop_mode == "copy_install" else ""
        result = check_plugin(
            plugin_name,
            entry.get("source_path", ""),
            entry.get("live_path", ""),
            entry.get("track_ref", "origin/main"),
            entry.get("dist_name", plugin_name.replace("-", "_")),
            prop_mode,
            source_subpath,
            check_clean_only=check_clean_only,
            is_windows=is_windows,
            refresh_log=refresh_log,
            current_pyproject_hash_override=current_pyproject_hash_override,
            claude_home=claude_home,
        )
        stdout_lines.extend(result.lines)
        stderr_lines.extend(result.stderr_lines)
        if not result.ok:
            total_drift = 1

    return stdout_lines, stderr_lines, total_drift


def main(argv: List[str]) -> int:
    filter_plugin = ""
    check_clean_only = False

    for arg in argv:
        if arg in ("--help", "-h"):
            print(_USAGE)
            return 0
        if arg == "--check-clean-only":
            check_clean_only = True
        elif arg.startswith("-"):
            print(f"{_PROG}: unknown flag: {arg}", file=sys.stderr)
            return 2
        else:
            if not filter_plugin:
                filter_plugin = arg
            else:
                print(f"{_PROG}: unexpected argument: {arg}", file=sys.stderr)
                return 2

    stdout_lines, stderr_lines, exit_code = _run(filter_plugin, check_clean_only)
    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    return exit_code


@register_op("plugin_health.drift")
async def _plugin_health_drift(params: dict, repo_root=None) -> dict:
    """JSON-RPC "plugin_health.drift" handler.

    Params: filter_plugin (optional str, single plugin name), check_clean_only
    (optional bool). repo_root is accepted for handler-signature parity but
    IGNORED — this op inspects the operator's OWN machine-local plugin registry
    (settings-home resolved), not the caller's repo (same "none"-scope class as
    engine.drift / percolate.validate_store).

    Returns {"exit_code": int, "clean": bool, "lines": [...], "stderr_lines": [...]}.
    """
    params = params or {}
    filter_plugin = str(params.get("filter_plugin") or "")
    check_clean_only = bool(params.get("check_clean_only", False))
    stdout_lines, stderr_lines, exit_code = _run(filter_plugin, check_clean_only)
    return {
        "exit_code": exit_code,
        "clean": exit_code == 0,
        "lines": stdout_lines,
        "stderr_lines": stderr_lines,
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
