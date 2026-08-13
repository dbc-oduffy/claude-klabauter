"""
coordinator_core.plugin_health.probe_select — manifest-driven probe selector for
coordinator-doctor.

Purpose: read bin/doctor-probes.toml (path supplied by the caller, or a sibling-file
default under DOCTOR_PROBES_MANIFEST) and emit probe ids matching the given selection
grammar. sentinel.py imports this module IN-PROCESS to resolve ACTIVE_PROBES for each
run mode (closes a subprocess spawn site that fired on every doctor-sentinel invocation
under the bash oracle — that script shelled out to this file, itself once-Python, as a
separate `python doctor-probe-select.py` process).

`git mv` port of coordinator-claude coordinator/bin/doctor-probe-select.py: logic is otherwise
unchanged (it was already pure, portable Python with no shell dependency) — only the
manifest-path default changed from "sibling to this file" (coordinator/bin/) to an
explicit caller-supplied `manifest_path` argument, since this module's own directory is
no longer the directory the manifest actually lives in (the manifest is coordinator-claude-side data,
this module is claude-klabauter-side code — see plugin_health/__init__.py negative-spec).

CARGO-CULT GUARD: this selector operates on the fired-probe manifest only. P-7a is
EM-native and NOT in the manifest; --probe P-7a / select_probe(probes, "P-7a") exits 2.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g2
Port of:       coordinator/bin/doctor-probe-select.py (coordinator-claude repo)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def _default_manifest_path() -> Path:
    """Resolve the manifest path when the caller supplies none.

    Env override first (DOCTOR_PROBES_MANIFEST — mirrors the bash oracle's
    DOCTOR_PROBES_MANIFEST override), else the historical coordinator-claude bin/ location.
    CLI-mode callers that need a real doctor-probes.toml should always pass
    manifest_path explicitly (the coordinator-claude trampoline knows its own sibling-file
    location); this fallback exists for parity/test convenience only.
    """
    override = os.environ.get("DOCTOR_PROBES_MANIFEST")
    if override:
        return Path(override)
    return Path(__file__).parent / "doctor-probes.toml"


def load_probes(manifest_path: Optional[Path] = None) -> list:
    """Load and return the [[probe]] array from the manifest."""
    try:
        import tomllib  # type: ignore[import]
    except ImportError:
        print(
            "inconclusive: tomllib unavailable -- requires Python >= 3.11 "
            "(or install tomli backport). Cannot select probes.",
            file=sys.stderr,
        )
        sys.exit(3)

    manifest = manifest_path if manifest_path is not None else _default_manifest_path()
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"inconclusive: manifest not found at {manifest}", file=sys.stderr)
        sys.exit(3)
    except Exception as exc:
        print(f"inconclusive: manifest parse error -- {exc}", file=sys.stderr)
        sys.exit(3)

    probes = data.get("probe", [])
    if not probes:
        print("inconclusive: manifest contains no [[probe]] entries", file=sys.stderr)
        sys.exit(3)
    return probes


# ---------------------------------------------------------------------------
# Selection modes
# ---------------------------------------------------------------------------


def select_triage(probes: list) -> List[str]:
    """Return ids where triage == true, in manifest order."""
    return [p["id"] for p in probes if p.get("triage") is True]


def select_full(probes: list) -> List[str]:
    """Return all ids in manifest order."""
    return [p["id"] for p in probes]


def select_cluster(probes: list, name: str) -> List[str]:
    """Return ids in the named cluster. Unknown cluster exits 2 (fail-loud)."""
    known = sorted({p["cluster"] for p in probes})
    matched = [p["id"] for p in probes if p["cluster"] == name]
    if not matched:
        print(
            f"unknown cluster: {name} (known: {', '.join(known)})",
            file=sys.stderr,
        )
        sys.exit(2)
    return matched


def select_probe(probes: list, probe_id: str) -> List[str]:
    """Return [id] if present in manifest; exits 2 if not (non-fired guard)."""
    matched = [p["id"] for p in probes if p["id"] == probe_id]
    if not matched:
        print(f"unknown probe: {probe_id}", file=sys.stderr)
        sys.exit(2)
    return matched


def select_symptom(probes: list, text: str) -> List[str]:
    """Case-insensitive substring match against symptom_keywords.

    Returns ALL probe ids in any cluster that has a keyword match (cluster-expansion):
    matched clusters are collected first, then all probes in those clusters are returned.
    No match -> exit 2 (vacuous-GREEN guard -- never print empty set and exit 0).
    """
    needle = text.lower()
    matched_clusters = set()
    for p in probes:
        for kw in p.get("symptom_keywords", []):
            if needle in kw.lower():
                matched_clusters.add(p["cluster"])
                break

    if not matched_clusters:
        print(f"no symptom match for: {text}", file=sys.stderr)
        sys.exit(2)

    return [p["id"] for p in probes if p["cluster"] in matched_clusters]


def id_to_cluster(probes: list, probe_id: str) -> str:
    """Return the cluster name for the given probe id; exits 2 if unknown."""
    for p in probes:
        if p["id"] == probe_id:
            return p["cluster"]
    print(f"unknown probe id: {probe_id}", file=sys.stderr)
    sys.exit(2)


def list_clusters(probes: list) -> List[str]:
    """Return sorted unique cluster names."""
    return sorted({p["cluster"] for p in probes})


# ---------------------------------------------------------------------------
# In-process API for sentinel.py — avoids the CLI argv dance entirely.
# ---------------------------------------------------------------------------


def resolve_active_probes(
    mode: str,
    arg: Optional[str],
    manifest_path: Optional[Path] = None,
) -> List[str]:
    """Return the active probe-id list for (mode, arg) — sentinel.py's entry point.

    mode in {"triage", "full", "cluster", "probe", "symptom"}; arg is required for
    cluster/probe/symptom, ignored for triage/full. Exits 2/3 on selector error
    (same fail-loud contract as the CLI — sentinel.py propagates these as its own
    exit code, matching the bash oracle's `"$PY" "$_SELECTOR" ... || exit $?`).
    """
    probes = load_probes(manifest_path)
    if mode == "triage":
        return select_triage(probes)
    if mode == "full":
        return select_full(probes)
    if mode == "cluster":
        assert arg is not None
        return select_cluster(probes, arg)
    if mode == "probe":
        assert arg is not None
        return select_probe(probes, arg)
    if mode == "symptom":
        assert arg is not None
        return select_symptom(probes, arg)
    print(f"unknown selector mode: {mode}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# CLI (retained — some test suites may still invoke this module as a standalone
# script; see recipe § Delete set for the caller-repoint disposition).
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    else:
        argv = list(argv)

    if len(argv) == 0:
        mode = "triage"
        mode_arg = None
    elif argv[0] == "--triage":
        if len(argv) != 1:
            print("--triage takes no arguments", file=sys.stderr)
            return 2
        mode = "triage"
        mode_arg = None
    elif argv[0] == "--full":
        if len(argv) != 1:
            print("--full takes no arguments", file=sys.stderr)
            return 2
        mode = "full"
        mode_arg = None
    elif argv[0] == "--cluster":
        if len(argv) != 2:
            print("--cluster requires exactly one NAME argument", file=sys.stderr)
            return 2
        mode = "cluster"
        mode_arg = argv[1]
    elif argv[0] == "--probe":
        if len(argv) != 2:
            print("--probe requires exactly one ID argument", file=sys.stderr)
            return 2
        mode = "probe"
        mode_arg = argv[1]
    elif argv[0] == "--symptom":
        if len(argv) < 2:
            print("--symptom requires a TEXT argument", file=sys.stderr)
            return 2
        mode = "symptom"
        mode_arg = " ".join(argv[1:])
    elif argv[0] == "--list-clusters":
        if len(argv) != 1:
            print("--list-clusters takes no arguments", file=sys.stderr)
            return 2
        mode = "list-clusters"
        mode_arg = None
    elif argv[0] == "--id-to-cluster":
        if len(argv) != 2:
            print("--id-to-cluster requires exactly one ID argument", file=sys.stderr)
            return 2
        mode = "id-to-cluster"
        mode_arg = argv[1]
    else:
        print(
            f"unknown flag: {argv[0]} "
            "(valid: --triage, --full, --cluster NAME, --probe ID, "
            "--symptom TEXT, --list-clusters, --id-to-cluster ID)",
            file=sys.stderr,
        )
        return 2

    probes = load_probes()

    if mode == "triage":
        ids = select_triage(probes)
    elif mode == "full":
        ids = select_full(probes)
    elif mode == "cluster":
        ids = select_cluster(probes, mode_arg)  # type: ignore[arg-type]
    elif mode == "probe":
        ids = select_probe(probes, mode_arg)  # type: ignore[arg-type]
    elif mode == "symptom":
        ids = select_symptom(probes, mode_arg)  # type: ignore[arg-type]
    elif mode == "list-clusters":
        for c in list_clusters(probes):
            print(c)
        return 0
    elif mode == "id-to-cluster":
        cluster = id_to_cluster(probes, mode_arg)  # type: ignore[arg-type]
        print(cluster)
        return 0
    else:
        return 2

    for probe_id in ids:
        print(probe_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
