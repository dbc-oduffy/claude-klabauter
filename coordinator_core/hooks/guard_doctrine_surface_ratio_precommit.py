"""Native git pre-commit hook: Layer 1 leg 1b of the doctrinal surface
weight ratchet -- the ONLY enforcing leg (leg 1a,
`coordinator_core.hooks.guard_doctrine_surface_ratio`, is advisory-only).

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/guard-doctrine-surface-ratio-
precommit.py`. `command/native-door` (per the W4-C1 verdict) does not apply
to this row's entry: a git pre-commit hook is invoked directly by `git`,
never dialled through `hook-run`, so it carries no `register_op` and no
`hooks.<name>` op contract -- see the module-level
`# guard-not-a-hook-entrypoint` marker above.

Shape changes, all forced by the port, none behavioural:

  - `REPO_ROOT`: the source anchored on `Path(__file__).resolve().parents[3]`
    -- correct there, since that module lived inside the doctrine-plane repo
    whose own pre-commit hook invoked it with that repo as cwd. This module
    now lives inside the ENGINE (a different checkout). Matches its W4-C7
    sibling's own convention instead: `REPO_ROOT = Path.cwd()` -- git invokes
    a pre-commit hook with the TARGET repo (the one being committed) already
    as cwd, and every `git` subprocess call below inherits it, unchanged
    from the source's own `cwd=str(REPO_ROOT)` pass-through.
  - Sibling-script imports (`_claude_md_ledger`, `_doctrine_surface_netting`,
    `doctrine_surface_tiers`) become ordinary package imports of this
    package's own already-landed modules
    (`coordinator_core.hooks.claude_md_ledger`,
    `coordinator_core.hooks.doctrine_surface_netting`,
    `coordinator_core.doctrine_surface_tiers`) -- same pattern the W4-C5
    sibling `guard_doctrine_surface_ratio` already used. `surface_of` comes
    from `coordinator_core.hooks.doctrine_changelog_prose` (this same
    chunk), same as that sibling.
  - The sub-floor accumulator's on-disk home moves from a doctrine-plane-
    tracked-adjacent path (`coordinator/hooks/state/doctrine-surface-ratio-
    accumulator.json`, `.gitignore`d per-machine runtime state living
    ALONGSIDE tracked doctrine content) to
    `coordinator_core._settings_home.machine_local_dir()` -- this engine's
    own fleet-wide per-machine state root, the natural home for "per-machine
    runtime state, never doctrine content" now that the accumulator's owner
    lives in a different repo than the doctrine content it prices. Same
    write-temp-then-rename atomicity, same shape.
  - `_load_split_recognizer`: the source's `importlib.util.spec_from_file_
    location` load of a hyphenated-basename sibling
    (`coordinator/lib/generate-doctrine-surface-split.py`) is NOT part of
    this row's footprint (not landed by this chunk) and has no claude-klabauter-side
    home yet. Resolution now probes the doctrine-plane repo root (reusing
    `coordinator_core.hooks.derive_global_doctrine_live_copy.
    _resolve_doctrine_repo_root`'s own multi-rung probe) for that same
    relative path, `coordinator/lib/generate-doctrine-surface-split.py`, and
    loads it the same `spec_from_file_location` way if found. A miss at
    every rung (module not present, or the doctrine repo root itself
    unresolvable) raises -- already caught by `main()`'s own pre-existing
    fail-open wrapper around the sanctioned-split leg (A5/A15's own posture:
    a defect in split recognition must not become a universal commit
    block), so an environment without that sibling module degrades to
    "sanctioned splits are not recognized this commit," never a broken
    guard.

Everything else -- the byte-unit (never line-oriented) netting, the
admission leg's fail-CLOSED posture (deliberately NOT sharing the ratio
predicate's fail-open posture, see the source's own "WHY THIS LEG DOES NOT
SHARE..." section), the sub-floor accumulator's crossing-then-reset
mechanics, the reasoned-growth commit-trailer escape hatch, and the
denied-commit-never-persists-its-own-debt-payoff invariant -- is unchanged.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from coordinator_core._settings_home import machine_local_dir
from coordinator_core.doctrine_surface_tiers import admission_cap_for, tier_boundaries_for
from coordinator_core.hooks.claude_md_ledger import (
    GOVERNED_AUTHORING_SURFACES,
    admission_check_for_surface,
)
from coordinator_core.hooks.derive_global_doctrine_live_copy import (
    _resolve_doctrine_repo_root,
)
from coordinator_core.hooks.doctrine_changelog_prose import surface_of
from coordinator_core.hooks.doctrine_surface_netting import (
    CREDIT_SCOPE_FILE,
    CREDIT_SCOPE_SURFACE,
    is_sanctioned_reason,
    net_bytes_by_file,
    net_bytes_by_surface,
)

REPO_ROOT = Path.cwd()

_NULL_OID = "0" * 40

_SUB_FLOOR_BYTES = 512

_RATIO_PRICED_SURFACES = frozenset({"wiki", "commands", "snippets"})
_ADMISSION_PRICED_SURFACES = frozenset({"wiki", "commands", "snippets", "agents", "skills"})

_ACCUMULATOR_STATE_PATH = machine_local_dir() / "doctrine-surface-ratio-accumulator.json"

_ACCUMULATOR_KEY_BY_SCOPE = {
    CREDIT_SCOPE_SURFACE: "sub_floor_accumulator_surface",
    CREDIT_SCOPE_FILE: "sub_floor_accumulator_file",
}

_NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_split_recognizer():
    repo_root = _resolve_doctrine_repo_root()
    if repo_root is None:
        raise ModuleNotFoundError("doctrine-plane repo root not resolvable")
    module_path = repo_root / "coordinator" / "lib" / "generate-doctrine-surface-split.py"
    if not module_path.is_file():
        raise ModuleNotFoundError(f"{module_path} not found")
    spec = importlib.util.spec_from_file_location("generate_doctrine_surface_split", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.is_sanctioned_split


def _run_git(args: "list[str]") -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=True,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _blob_sizes_batch(oids: "set[str]") -> "dict[str, int]":
    sizes: "dict[str, int]" = {}
    real_oids = []
    for oid in oids:
        if oid in (_NULL_OID, "") or set(oid) == {"0"}:
            sizes[oid] = 0
        else:
            real_oids.append(oid)
    if not real_oids:
        return sizes
    result = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=str(REPO_ROOT),
        input="\n".join(real_oids) + "\n",
        capture_output=True,
        text=True,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    for line in result.stdout.splitlines():
        parts = line.split(" ")
        if len(parts) >= 2 and parts[-1] == "missing":
            sizes[parts[0]] = 0
        elif len(parts) >= 3:
            try:
                sizes[parts[0]] = int(parts[2])
            except ValueError:
                sizes[parts[0]] = 0
    return sizes


def _parse_raw_diff_records(raw_output: str) -> "list[tuple[str, str, str, str]]":
    fields = [f for f in raw_output.split("\x00") if f != ""]
    records: "list[tuple[str, str, str, str]]" = []
    i = 0
    while i < len(fields):
        header = fields[i]
        if not header.startswith(":"):
            i += 1
            continue
        parts = header[1:].split(" ")
        if len(parts) < 5:
            i += 1
            continue
        old_oid, new_oid, status = parts[2], parts[3], parts[4]
        i += 1
        if i >= len(fields):
            break
        path = fields[i]
        i += 1
        if status[0] in ("R", "C"):
            if i < len(fields):
                path = fields[i]
                i += 1
        records.append((old_oid, new_oid, status, path))
    return records


def _parse_raw_diff(raw_output: str) -> "list[tuple[int, int, str]]":
    records = _parse_raw_diff_records(raw_output)
    all_oids: "set[str]" = set()
    for old_oid, new_oid, _status, _path in records:
        all_oids.add(old_oid)
        all_oids.add(new_oid)
    sizes = _blob_sizes_batch(all_oids)
    rows: "list[tuple[int, int, str]]" = []
    for old_oid, new_oid, _status, path in records:
        rows.append((sizes.get(old_oid, 0), sizes.get(new_oid, 0), path))
    return rows


def _cat_file_contents_batch(oids: "set[str]") -> "dict[str, bytes]":
    contents: "dict[str, bytes]" = {}
    real_oids = []
    for oid in oids:
        if oid in (_NULL_OID, "") or set(oid) == {"0"}:
            contents[oid] = b""
        else:
            real_oids.append(oid)
    if not real_oids:
        return contents
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=str(REPO_ROOT),
        input=("\n".join(real_oids) + "\n").encode("utf-8"),
        capture_output=True,
        creationflags=_NO_CONSOLE_WINDOW,
    )
    data = result.stdout
    pos = 0
    for oid in real_oids:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].decode("utf-8", errors="replace")
        parts = header.split(" ")
        if len(parts) < 2 or parts[-1] == "missing" or len(parts) < 3:
            contents[oid] = b""
            pos = nl + 1
            continue
        try:
            size = int(parts[2])
        except ValueError:
            contents[oid] = b""
            pos = nl + 1
            continue
        start = nl + 1
        contents[oid] = data[start : start + size]
        pos = start + size + 1
    return contents


def _admission_denials_for_governed_surfaces(raw_output: str) -> "list[str]":
    denials: "list[str]" = []
    governed = set(GOVERNED_AUTHORING_SURFACES)
    records = _parse_raw_diff_records(raw_output)
    touched = [
        (old_oid, new_oid, path) for old_oid, new_oid, _status, path in records if path in governed
    ]
    if not touched:
        return denials

    needed_oids = {oid for old_oid, new_oid, _path in touched for oid in (old_oid, new_oid)}
    contents_by_oid = _cat_file_contents_batch(needed_oids)

    for old_oid, new_oid, path in touched:
        try:
            old_content = contents_by_oid.get(old_oid, b"").decode("utf-8", errors="replace")
            new_content = contents_by_oid.get(new_oid, b"").decode("utf-8", errors="replace")
            allowed, message = admission_check_for_surface(path, old_content, new_content, REPO_ROOT)
        except Exception as exc:  # noqa: BLE001 -- deliberate fail-CLOSED, see module docstring.
            denials.append(
                f"{path}: the doctrine-surface admission check could not complete "
                f"({exc.__class__.__name__}: {exc}) -- blocking this commit rather "
                "than silently approving an unclassified change to a governed "
                "authoring surface."
            )
            continue
        if not allowed:
            denials.append(f"{path}: {message}")

    return denials


def _sanctioned_split_paths(raw_output: str) -> "set[str]":
    is_sanctioned_split = _load_split_recognizer()
    records = _parse_raw_diff_records(raw_output)

    old_oid_by_path = {}
    new_oid_by_path = {}
    for old_oid, new_oid, _status, path in records:
        old_oid_by_path[path] = old_oid
        new_oid_by_path[path] = new_oid

    deleted_md_stems = [
        path[: -len(".md")]
        for old_oid, new_oid, status, path in records
        if path.endswith(".md") and (set(new_oid) == {"0"} or new_oid == _NULL_OID)
    ]

    stem_new_paths: "dict[str, list[str]]" = {}
    needed_oids: "set[str]" = set()
    for stem in deleted_md_stems:
        prefix = f"{stem}/"
        new_under_stem = [p for p in new_oid_by_path if p.startswith(prefix)]
        if not new_under_stem:
            continue
        stem_new_paths[stem] = new_under_stem
        md_path = f"{stem}.md"
        needed_oids.add(old_oid_by_path.get(md_path, _NULL_OID))
        for p in new_under_stem:
            needed_oids.add(new_oid_by_path[p])
    contents_by_oid = _cat_file_contents_batch(needed_oids)

    sanctioned: "set[str]" = set()
    for stem, new_under_stem in stem_new_paths.items():
        md_path = f"{stem}.md"
        prior_snapshot = {
            md_path: contents_by_oid.get(old_oid_by_path.get(md_path, _NULL_OID), b"")
        }
        current_snapshot = {
            p: contents_by_oid.get(new_oid_by_path[p], b"") for p in new_under_stem
        }
        if is_sanctioned_split(prior_snapshot, current_snapshot, stem):
            sanctioned.add(md_path)
            sanctioned.update(new_under_stem)
    return sanctioned


def _load_baseline() -> dict:
    try:
        return json.loads(_ACCUMULATOR_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"surfaces": {}}


def _save_baseline(baseline: dict) -> None:
    _ACCUMULATOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(baseline, indent=2, sort_keys=False) + "\n"
    fd, tmp_path = tempfile.mkstemp(
        dir=str(_ACCUMULATOR_STATE_PATH.parent), prefix=".doctrine-surface-ratio-accumulator-"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp_path, str(_ACCUMULATOR_STATE_PATH))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _persist_baseline_fail_open(baseline: dict) -> None:
    try:
        _save_baseline(baseline)
    except Exception:
        pass


def _build_tier_of(rows: "list[tuple[int, int, str]]"):
    pre_edit_size_by_path = {path: old_bytes for old_bytes, _new, path in rows}

    def tier_of(path: str) -> str:
        surface = surface_of(Path(path))
        if surface is None or surface not in _RATIO_PRICED_SURFACES:
            return "unpriced"
        pre_edit_size = pre_edit_size_by_path.get(path, 0)
        boundaries = tier_boundaries_for(surface)
        for band in boundaries["tiers"]:
            if band["max"] is None or pre_edit_size < band["max"]:
                return band["credit_scope"]
        return boundaries["tiers"][-1]["credit_scope"]

    return tier_of


def _new_file_admission_denials(rows: "list[tuple[int, int, str]]") -> "list[str]":
    denials = []
    for old_bytes, new_bytes, path in rows:
        if old_bytes != 0:
            continue
        surface = surface_of(Path(path))
        if surface is None or surface not in _ADMISSION_PRICED_SURFACES:
            continue
        threshold = admission_cap_for(surface)
        if new_bytes > threshold:
            denials.append(
                f"{path} arrives at {new_bytes} B, over {surface}'s "
                f"{threshold} B new-file admission cap."
            )
    return denials


def _apply_sub_floor_and_bill(
    baseline: dict, surface_net: "dict[str, int]", file_net: "dict[str, int]", tier_of
) -> "list[str]":
    denials: "list[str]" = []
    surfaces = baseline["surfaces"]
    surface_key = _ACCUMULATOR_KEY_BY_SCOPE[CREDIT_SCOPE_SURFACE]
    file_key = _ACCUMULATOR_KEY_BY_SCOPE[CREDIT_SCOPE_FILE]

    for surface, net in surface_net.items():
        entry = surfaces.setdefault(surface, {})
        prior = int(entry.get(surface_key, 0))
        addition = max(net, 0)
        accumulated = prior + addition
        if accumulated >= _SUB_FLOOR_BYTES:
            owed = accumulated * 2
            denials.append(
                f"`{surface}` crosses the 512 B sub-floor accumulator "
                f"(accumulated {accumulated} B) -- {owed} B of cuts owed "
                "on the surface this commit."
            )
            entry[surface_key] = 0
        else:
            entry[surface_key] = accumulated

    for path, net in file_net.items():
        surface = surface_of(Path(path))
        if surface is None:
            continue
        entry = surfaces.setdefault(surface, {})
        prior = int(entry.get(file_key, 0))
        addition = max(net, 0)
        accumulated = prior + addition
        if accumulated >= _SUB_FLOOR_BYTES:
            boundaries = tier_boundaries_for(surface)
            ratio = 5
            for band in boundaries["tiers"]:
                if band["credit_scope"] == CREDIT_SCOPE_FILE:
                    ratio = band["ratio"]
                    break
            owed = accumulated * ratio
            denials.append(
                f"{path} crosses the 512 B sub-floor accumulator "
                f"(accumulated {accumulated} B) -- {owed} B of cuts owed "
                "on THIS FILE this commit."
            )
            entry[file_key] = 0
        else:
            entry[file_key] = accumulated

    return denials


_REASON_TRAILER_KEY = "Doctrine-Growth-Reason:"


def _read_reasoned_growth_marker() -> "str | None":
    env_value = os.environ.get("COORDINATOR_DOCTRINE_GROWTH_REASON")
    if env_value:
        return env_value
    try:
        msg_path = REPO_ROOT / ".git" / "COMMIT_EDITMSG"
        if not msg_path.is_file():
            return None
        lines = msg_path.read_text(encoding="utf-8", errors="replace").splitlines()
        last_blank = -1
        for i, line in enumerate(lines):
            if line.strip() == "":
                last_blank = i
        trailer_block = lines[last_blank + 1 :]
        if last_blank == -1 and len(lines) > 1:
            return None
        for line in trailer_block:
            stripped = line.strip()
            if stripped.startswith(_REASON_TRAILER_KEY):
                return stripped[len(_REASON_TRAILER_KEY) :].strip()
    except Exception:
        return None
    return None


def _emit_governed_admission_denials(governed_admission_denials: "list[str]") -> None:
    for reason in governed_admission_denials:
        print(f"[guard-doctrine-surface-admission] {reason}", file=sys.stderr)


def main() -> int:
    try:
        raw = _run_git(["diff", "--cached", "--raw", "-z", "-M"])
    except Exception:
        return 0

    governed_admission_denials = _admission_denials_for_governed_surfaces(raw)

    try:
        rows = _parse_raw_diff(raw)
    except Exception:
        if governed_admission_denials:
            _emit_governed_admission_denials(governed_admission_denials)
            return 1
        return 0
    if not rows:
        if governed_admission_denials:
            _emit_governed_admission_denials(governed_admission_denials)
            return 1
        return 0

    try:
        sanctioned_paths = _sanctioned_split_paths(raw)
        if sanctioned_paths:
            rows = [row for row in rows if row[2] not in sanctioned_paths]
        if not rows:
            if governed_admission_denials:
                _emit_governed_admission_denials(governed_admission_denials)
                return 1
            return 0
    except Exception:
        pass

    try:
        admission_denials = _new_file_admission_denials(rows)
        tier_of = _build_tier_of(rows)
        surface_net = net_bytes_by_surface(rows, tier_of)
        file_net = net_bytes_by_file(rows, tier_of)

        baseline = _load_baseline()
        floor_denials = _apply_sub_floor_and_bill(baseline, surface_net, file_net, tier_of)
    except Exception:
        if governed_admission_denials:
            _emit_governed_admission_denials(governed_admission_denials)
            return 1
        return 0

    denials = admission_denials + floor_denials + governed_admission_denials
    if not denials:
        _persist_baseline_fail_open(baseline)
        return 0

    marker = _read_reasoned_growth_marker()
    if is_sanctioned_reason(marker) and not governed_admission_denials:
        _persist_baseline_fail_open(baseline)
        return 0

    for reason in admission_denials + floor_denials:
        print(f"[guard-doctrine-surface-ratio-precommit] {reason}", file=sys.stderr)
    _emit_governed_admission_denials(governed_admission_denials)
    if admission_denials or floor_denials:
        print(
            "Reasoned-growth escape: add a commit trailer "
            f"`{_REASON_TRAILER_KEY} <reason>` naming a sanctioned reason "
            "(see coordinator_core.hooks.doctrine_surface_netting."
            "REASONED_GROWTH_MARKERS) to exempt this growth -- silence is "
            "not an exception, and an out-of-enum value fails the same way.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
