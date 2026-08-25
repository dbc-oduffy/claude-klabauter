"""coordinator_core.install.manifest_reader — Python-native reimplementation
of the interpreter-resolution and NDJSON-emission logic in
coordinator/scripts/lib/manifest_reader.sh's ``_co_find_python``,
``_co_resolve_manifest_path``, and ``_co_manifest_read_ndjson``.

Port source: coordinator/scripts/lib/manifest_reader.sh [DoE-claude repo]
(bash lib is SOURCED by coordinator/scripts/lib/dep_check.sh — it stays in
place; this module is a parallel Python-native implementation for
Python-side consumers, NOT a trampoline over the bash file.
project-rag-ue-addon vendors THIS module (not the bash) since its
2026-07-28 de-bash re-vendor — see that repo's
project_rag_ue_addon_scripts/lib/coordinator_prereq/VENDOR.md, which pins
a SHA of this file; changing this module's Step Zero contract obligates a
cross-repo memo so they can re-pin.)

Spec backlink: docs/plans/2026-06-15-coordinator-install-chain-application-phase-b.md §7 C3
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Contract mirrors the bash original:
  - find_python(): resolve a functional python3.11+ interpreter the same way
    _co_find_python does (python3 -> python -> [Windows] py launcher
    variants), functionally probed (actually executed, not existence-only)
    to dodge the WindowsApps Store-stub trap (a `python3.exe` App Execution
    alias that passes a PATH-existence check but exits 49 "Python was not
    found" when run). Returns the resolved executable as an absolute path
    (py-launcher branch) or the bare command name (python3/python branch),
    matching the bash contract of "always a single invokable token".
  - resolve_manifest_path()/manifest_read_ndjson(): layout-aware resolution
    of agent-install-manifest.json (nested working-tree vs flat
    publish-repo-root) + stdlib-only JSON parse/validate/NDJSON-emit,
    preserving the hard contract: missing/corrupt manifest is a loud
    failure (a raised exception here; exit 1 in the bash), never a silent
    "all deps OK".

Negative-spec — two deliberate scope narrowings vs. the bash original, both
because this module runs INSIDE an already-running >=3.11 Python process
rather than being sourced into a bash caller that must shell out to invoke
Python at all:
  1. manifest_read_ndjson() parses the manifest with the running
     interpreter's stdlib ``json`` module directly — it does NOT shell out
     to a resolved find_python() candidate the way
     ``_co_manifest_read_ndjson`` does (`"$_python" -c "..."`). The
     resulting NDJSON lines are contract-identical field-for-field; only the
     execution vehicle differs (in-process parse vs. subprocess bash->python
     round-trip), so this is not a behavioral drop.
  2. resolve_manifest_path()'s bash original falls back to a location
     derived from the SCRIPT'S OWN FILE PATH (``scripts/lib`` -> two levels
     up) when no repo_root is supplied — a fallback that only makes sense
     because manifest_reader.sh lives inside the DoE-claude coordinator/
     tree it is resolving paths within. This makima module has no analogous
     co-located tree to derive from, so repo_root is REQUIRED here (via
     argument or $REPO_ROOT) — callers get a loud ValueError instead of a
     silently-wrong guess, which is the stricter (not weaker) failure mode.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Union

from coordinator_core.win_portability import no_console_creationflags

_VERSION_CHECK = "import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)"
_PROBE_TIMEOUT_SECS = 10.0

_ACCEPTED_MANIFEST_VERSIONS = (1, 2, 3)
_REQUIRED_TOP_FIELDS = ("agent_install_contract_version", "repo_id", "direct_deps")
_PROBE_ARG_KEYS = ("path", "paths", "expr", "cmd")


class NoPythonInterpreterError(RuntimeError):
    """Raised when no functional Python 3.11+ interpreter can be resolved —
    mirrors the bash original's remediation-message-then-exit-1 path
    (_co_find_python's trailing ``return 1`` after printing to stderr)."""


class ManifestCorruptError(RuntimeError):
    """Raised for every "manifest corrupt or missing" condition the bash
    original maps to exit 1: missing file, JSON parse error, unreadable
    file, missing required top-level field, unrecognised contract version,
    or a non-array ``direct_deps``. Never silently treated as "all deps
    OK" — callers MUST NOT catch-and-ignore this."""


def _probe_candidate(executable: str) -> bool:
    """Return True iff `executable -c _VERSION_CHECK` succeeds. Functional
    probe (actually executed), not an existence-only check — mirrors
    _co_fp_probe. timeout + stdin=DEVNULL per the unbounded-hang /
    blocking-stdin defect classes (a misbehaving candidate binary must not
    wedge the caller).

    Deliberate isolation boundary, not a candidate for an in-process
    import — ``executable`` is an unverified candidate interpreter
    resolved from PATH/py-launcher, not the interpreter running this
    module, so it must be probed as its own process before it is trusted.
    See ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    try:
        result = subprocess.run(
            [executable, "-c", _VERSION_CHECK],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _resolve_py_launcher_candidate(version_flag: str) -> Optional[str]:
    """Resolve one `py [version_flag] -c "import sys;print(sys.executable)"`
    candidate to a concrete absolute executable path, mirroring the bash
    original's per-version-flag loop body. Returns None on any failure
    (missing launcher, bad version, non-functional resolved interpreter) —
    never a multi-word / malformed token.

    Deliberate isolation boundary, not a candidate for an in-process
    import — the whole point of this call is to ask the Windows `py`
    launcher which concrete interpreter a version flag resolves to; that
    interpreter is unknown and unverified until this probe runs, so it is
    by construction not importable in-process. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    argv = ["py"]
    if version_flag:
        argv.append(version_flag)
    argv += ["-c", "import sys;print(sys.executable)"]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECS,
            encoding="utf-8",
            errors="replace",
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    resolved = (result.stdout or "").strip()
    if not resolved:
        return None
    if not _probe_candidate(resolved):
        return None
    return resolved


def find_python() -> str:
    """Resolve a functional Python 3.11+ interpreter token, mirroring
    _co_find_python's candidate order: python3 -> python -> (Windows only)
    `py` launcher variants (-3.12, -3.11, -3, bare).

    Returns a single invokable token (bare command name for the
    python3/python branches, an absolute path for the py-launcher branch).
    Raises NoPythonInterpreterError with the same OS-aware remediation text
    the bash prints to stderr when no candidate is functional.
    """
    if _probe_candidate("python3"):
        return "python3"
    if _probe_candidate("python"):
        return "python"

    if os.name == "nt":
        for version_flag in ("-3.12", "-3.11", "-3", ""):
            resolved = _resolve_py_launcher_candidate(version_flag)
            if resolved:
                return resolved
        message = (
            "ERROR: no functional Python 3.11+ interpreter found on PATH "
            "(tried python3, python, py (launcher)).\n"
            "  Python 3.11+ is required to read the install manifest.\n"
            "  On Windows: if python3/python exist but are non-functional, disable the\n"
            "    WindowsApps python/python3 App Execution aliases in:\n"
            "    Settings > Apps > App execution aliases\n"
            "    then install real Python 3.11+ from https://www.python.org/downloads/\n"
            "  See: https://www.python.org/downloads/"
        )
    else:
        message = (
            "ERROR: no functional Python 3.11+ interpreter found on PATH "
            "(tried python3, python).\n"
            "  Python 3.11+ is required to read the install manifest.\n"
            "  See: https://www.python.org/downloads/"
        )
    raise NoPythonInterpreterError(message)


def resolve_manifest_path(repo_root: Optional[Union[str, Path]] = None) -> Path:
    """Layout-aware resolution of agent-install-manifest.json, mirroring
    _co_resolve_manifest_path. Probes both known layouts and returns the
    first that exists, absolute + normalized:

      Nested working-tree / mirror layout (repo_root == coordinator/, the
      parent of scripts/):
          <repo_root>/docs/install/agent-install-manifest.json
      Flat publish-repo-root layout (manifest one level ABOVE coordinator/):
          <repo_root>/../docs/install/agent-install-manifest.json

    repo_root resolution order: explicit argument -> $REPO_ROOT env var.
    Unlike the bash original there is no file-location-derived fallback —
    see module docstring negative-spec (2). Raises ValueError if repo_root
    cannot be resolved by either means, and ManifestCorruptError if neither
    layout location exists on disk (fail loud, never an unbound/empty path).
    """
    if repo_root is None:
        repo_root = os.environ.get("REPO_ROOT")
    if not repo_root:
        raise ValueError(
            "resolve_manifest_path: repo_root not supplied and $REPO_ROOT is unset — "
            "this port requires an explicit repo_root (see module docstring negative-spec)"
        )
    repo_root_path = Path(repo_root)

    rel = Path("docs") / "install" / "agent-install-manifest.json"
    nested = repo_root_path / rel
    flat = repo_root_path / ".." / rel

    hit: Optional[Path] = None
    if nested.is_file():
        hit = nested
    elif flat.is_file():
        hit = flat
    else:
        raise ManifestCorruptError(
            "ERROR: install manifest not found in either layout location:\n"
            f"  nested (working-tree/mirror): {nested}\n"
            f"  flat   (publish-repo-root):   {flat}\n"
            "  Remediation: re-publish BOTH install-surface targets from the meta-repo\n"
            "  (POSIX: python3 / Windows: python) —\n"
            "    python3 coordinator/bin/publish.py coordinator-claude-toplevel-install   # repo-root docs/install/\n"
            "    python3 coordinator/bin/publish.py coordinator-claude                    # coordinator/ mirror\n"
            "  (a manifest-only or mirror-only re-publish leaves the layouts inconsistent)."
        )
    return hit.resolve()


def _load_manifest(manifest_path: Path) -> dict:
    """Load + validate the manifest per the bash original's inline
    `python -c` block — same field/version checks, same failure taxonomy,
    all folded into ManifestCorruptError (mirrors the bash's uniform exit 1
    for every corrupt-manifest branch)."""
    if not manifest_path.is_file():
        raise ManifestCorruptError(
            f"ERROR: manifest not found: {manifest_path}\n"
            "  manifest corrupt or missing — cannot proceed"
        )
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ManifestCorruptError(
            f"ERROR: manifest corrupt (JSON parse error): {exc}\n  file: {manifest_path}"
        ) from exc
    except OSError as exc:
        raise ManifestCorruptError(f"ERROR: manifest unreadable: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ManifestCorruptError(
            f"ERROR: manifest corrupt — top-level JSON value must be an object, got {type(manifest).__name__}"
        )

    for field in _REQUIRED_TOP_FIELDS:
        if field not in manifest:
            raise ManifestCorruptError(
                f"ERROR: manifest corrupt — missing required field: {field}"
            )

    version = manifest.get("agent_install_contract_version")
    if version not in _ACCEPTED_MANIFEST_VERSIONS:
        raise ManifestCorruptError(
            f"ERROR: manifest corrupt — unrecognised contract version: {version!r}"
        )

    direct_deps = manifest.get("direct_deps", [])
    if not isinstance(direct_deps, list):
        raise ManifestCorruptError(
            "ERROR: manifest corrupt — direct_deps must be an array"
        )
    return manifest


def _dep_to_record(dep: dict) -> dict:
    """Build one output record for a single direct_dep entry, matching the
    bash inline script's per-dep dict shape field-for-field (id, severity,
    sibling_dir_name, upstream_url, functional_probe_kind,
    functional_probe_args)."""
    probe = dep.get("functional_probe", {}) or {}
    probe_kind = probe.get("kind", "")
    probe_args = {key: probe[key] for key in _PROBE_ARG_KEYS if key in probe}
    return {
        "id": dep.get("id", ""),
        "severity": dep.get("severity", ""),
        "sibling_dir_name": dep.get("sibling_dir_name", ""),
        "upstream_url": dep.get("upstream_url", ""),
        "functional_probe_kind": probe_kind,
        "functional_probe_args": probe_args,
    }


def manifest_read_ndjson(
    manifest_path: Optional[Union[str, Path]] = None,
    repo_root: Optional[Union[str, Path]] = None,
) -> List[str]:
    """Read + validate agent-install-manifest.json and return one NDJSON
    line (WITHOUT trailing newline; caller joins/prints as needed) per
    direct_dep entry, mirroring _co_manifest_read_ndjson's per-dep record
    shape and json.dumps(..., ensure_ascii=True) encoding byte-for-byte.

    manifest_path resolution: explicit argument, else
    resolve_manifest_path(repo_root) (layout-aware default). Raises
    ManifestCorruptError on any of the corrupt-manifest conditions
    documented on that exception — hard contract, never a silent
    "all deps OK" default.
    """
    if manifest_path is None:
        path = resolve_manifest_path(repo_root)
    else:
        path = Path(manifest_path)

    manifest = _load_manifest(path)
    direct_deps = manifest.get("direct_deps", [])

    lines = []
    for dep in direct_deps:
        record = _dep_to_record(dep)
        lines.append(json.dumps(record, ensure_ascii=True))
    return lines


if __name__ == "__main__":  # pragma: no cover — parity smoke aid, not a CLI contract
    try:
        for line in manifest_read_ndjson():
            print(line)
    except (ManifestCorruptError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
