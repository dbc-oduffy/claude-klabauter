"""Ratchet: fails when a `.py` file outside C2's recorded exemption set gains the
``claude-klabauter`` noun, and skips loudly (never vacuously) when the manifest is unusable.

Plan: docs/plans/2026-09-10-claude-klabauter-noun-sweep-across-the-tree.md, row C7.
Spec backlink: that row's body and AC8.

Universe: every ``*.py`` file under the four noun-bearing code trees
(``coordinator/``, ``coordinator_core/``, ``bin/``, ``scripts/``) MINUS the exemption
set. The exemption set is read from C2's MACHINE-READABLE artifact
``coordinator/bin/legacy-engine-noun-reference-classes.json`` at test time -- never
hardcoded here -- and is the union of:
  - ``exempt_files``: files carrying a CONTRACT-BOUND, EXTERNALLY-NAMED-THING or
    PROSE-ONLY row;
  - the files named in ``renameable_local_slice_list``, AS RECORDED at C2's own SHA.
    That list can only shrink at regeneration (the generator's own refusal, escalation
    E2); a new RENAMEABLE-LOCAL file cannot silently join the exemption set through a
    routine regeneration.

This makes the test a RATCHET against a file newly gaining the noun, not an assertion
that the tree-wide count is zero -- a vacuous "count == 0" assertion would turn a
correct sweep red and is deliberately never written here.

SKIP RULE: skip, with an explicit reason, ONLY when the classes JSON is absent or
unparseable. A STALE exemption set (present, parseable, but a listed file no longer
exists or the shape doesn't match) is read defensively; an offending file outside the
exemption set is always a FAILURE, never a skip, and the failure message names the
offending file(s) plus the regeneration command.

PERF BUDGET: one in-process read pass, no subprocess. The file walk is in-process too
(no `git ls-files`); only files outside the exemption set are read, because they are
the ratchet's whole universe. Held to 500ms process time (DR-344, claude-klabauter's brightline).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TREES = ("coordinator", "coordinator_core", "bin", "scripts")
NOUN = re.compile(r"claude-klabauter", re.IGNORECASE)
#: ASCII-lowercase needle used for the production offender scan (§ PERF BUDGET): a raw
#: bytes `in` check after a single `.lower()` measures ~4x faster than a per-file
#: decode-then-regex pass over this tree (census spike, in-session), and is equivalent
#: for the ASCII-only spelling this noun uses.
NOUN_NEEDLE = b"claude-klabauter"
CLASSES_JSON_PATH = REPO_ROOT / "coordinator/bin/legacy-engine-noun-reference-classes.json"
GENERATOR_PATH = REPO_ROOT / "coordinator/bin/classify-legacy-engine-noun-references.py"
REGENERATE_COMMAND = "python coordinator/bin/classify-legacy-engine-noun-references.py"


def _load_exemption_set(classes_json_path: Path) -> set[str] | None:
    """Return the union of ``exempt_files`` and the slice list's file names, or None
    when the artifact is absent or unparseable (the only legitimate skip condition)."""
    try:
        raw = classes_json_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    exempt_files = data.get("exempt_files")
    slice_list = data.get("renameable_local_slice_list")
    if not isinstance(exempt_files, list) or not isinstance(slice_list, list):
        return None
    slice_files = {
        row["file"]
        for row in slice_list
        if isinstance(row, dict) and isinstance(row.get("file"), str)
    }
    return {f for f in exempt_files if isinstance(f, str)} | slice_files


def _find_offenders(
    repo_root: Path, trees: tuple[str, ...], exemption_set: set[str]
) -> list[str]:
    """In-process file walk (no `git ls-files`, no subprocess) over the four trees;
    reads only files outside the exemption set, since they are the ratchet's whole
    universe."""
    offenders: list[str] = []
    repo_root_str = str(repo_root)
    for tree in trees:
        root = os.path.join(repo_root_str, tree)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, repo_root_str).replace(os.sep, "/")
                if rel in exemption_set:
                    continue
                try:
                    with open(full, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                if NOUN_NEEDLE in raw.lower():
                    offenders.append(rel)
    return sorted(offenders)


def test_no_file_outside_the_exemption_set_gains_the_claude_klabauter_noun():
    exemption_set = _load_exemption_set(CLASSES_JSON_PATH)
    if exemption_set is None:
        pytest.skip(
            f"{CLASSES_JSON_PATH} is absent or unparseable -- cannot derive the "
            "exemption set, so the ratchet has nothing to check against. Regenerate "
            f"with: {REGENERATE_COMMAND}"
        )

    t0 = time.process_time()
    offenders = _find_offenders(REPO_ROOT, TREES, exemption_set)
    cpu_ms = (time.process_time() - t0) * 1000
    assert cpu_ms < 500, (
        f"ratchet file walk took {cpu_ms:.1f}ms of process time, over the 500ms DR-344 "
        "brightline budget -- this is a defect to fix, not a rationale to accept"
    )

    assert not offenders, (
        "the following file(s) carry the `claude-klabauter` noun but are NOT in C2's recorded "
        "exemption set (a new codename debt line, or the exemption set is stale): "
        + ", ".join(offenders)
        + f". Regenerate the manifest with: {REGENERATE_COMMAND}"
    )


def test_find_offenders_flags_a_noun_bearing_file_outside_the_exemption_set(tmp_path):
    """AC8 fixture leg (i): a noun-bearing file outside the exemption set fails."""
    tree_dir = tmp_path / "coordinator"
    tree_dir.mkdir()
    offending = tree_dir / "new_claude_klabauter_thing.py"
    offending.write_text("claude_klabauter_helper = 1\n", encoding="utf-8")

    offenders = _find_offenders(tmp_path, ("coordinator",), exemption_set=set())
    assert offenders == ["coordinator/new_claude_klabauter_thing.py"]

    # Exempting the exact file clears the ratchet -- proves the exemption path works,
    # not just the failure path.
    offenders_exempt = _find_offenders(
        tmp_path, ("coordinator",), exemption_set={"coordinator/new_claude_klabauter_thing.py"}
    )
    assert offenders_exempt == []


def _load_generator_module():
    spec = importlib.util.spec_from_file_location(
        "legacy_engine_noun_generator_fixture_c7", GENERATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _no_console_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _init_bare_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
        creationflags=_no_console_creationflags(),
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True,
        capture_output=True,
        creationflags=_no_console_creationflags(),
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_generator_refuses_a_new_renameable_local_row_outside_the_slice_list(
    tmp_path, monkeypatch
):
    """AC8 fixture leg (ii): the generator's classification, driven in-process through
    `importlib` by path, refuses a fixture RENAMEABLE-LOCAL line absent from the
    recorded slice list, and admits it only with the explicit per-file flag."""
    repo_root = tmp_path / "repo"
    doe_root = tmp_path / "doe"
    klabauter_root = tmp_path / "klabauter"

    coordinator_dir = repo_root / "coordinator"
    coordinator_dir.mkdir(parents=True)
    new_file = coordinator_dir / "new_local_thing.py"
    new_file.write_text("claude_klabauter_local_helper = 1\n", encoding="utf-8")

    _init_bare_git_repo(repo_root)
    _init_bare_git_repo(doe_root)
    _init_bare_git_repo(klabauter_root)

    module = _load_generator_module()

    empty_slice_list = tmp_path / "empty-slice-list.json"
    empty_slice_list.write_text(
        json.dumps({"renameable_local_slice_list": []}), encoding="utf-8"
    )
    monkeypatch.setattr(module, "SLICE_LIST_PATH", empty_slice_list)

    out_manifest = tmp_path / "out-manifest.md"
    out_json = tmp_path / "out-classes.json"

    argv = [
        "--repo",
        str(repo_root),
        "--doe-root",
        str(doe_root),
        "--klabauter-root",
        str(klabauter_root),
        "--repo-sha",
        "0" * 40,
        "--doe-sha",
        "0" * 40,
        "--klabauter-sha",
        "0" * 40,
        "--out-manifest",
        str(out_manifest),
        "--out-json",
        str(out_json),
    ]

    rc = module.main(list(argv))
    assert rc == 1, "generator must refuse a new RENAMEABLE-LOCAL row outside the slice list"
    assert not out_json.exists(), "a refused run must not write the classes JSON"

    admitted_rc = module.main(
        list(argv) + ["--admit-renameable", "coordinator/new_local_thing.py:1"]
    )
    assert admitted_rc == 0, "the same row must be admitted with the explicit flag"
    assert out_json.exists()
    written = json.loads(out_json.read_text(encoding="utf-8"))
    assert {"file": "coordinator/new_local_thing.py", "line": 1} in written[
        "renameable_local_slice_list"
    ]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
