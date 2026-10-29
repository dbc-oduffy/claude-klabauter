"""Source-side ratchets for two gates that otherwise fail only inside a publish round.

Codename leg: a tracked filename under the published engine surface that carries a non-kept registry
codename (the pattern source of `ops/check_registry_codename_leak`: `_resolve_registry_keys`, `_is_kept`,
slug and hyphen forms, case-insensitive substring). NEW hits fail; the baseline is the shrink-only set that
publish already scrubs through the store's basename rewrites. Machine-local: a box whose registry has no
non-kept key skips the live leg.

Arity leg: `percolate.payload_parity` binds every call site against the callee's signature; this runs it over
the working-tree Python files that differ from HEAD, so a call omitting a required argument is red at commit.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.ops import check_registry_codename_leak as leak
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SURFACE = ("coordinator_core", "coordinator")

#: Tracked names that carry a registry codename and publish clean via the store's rewrites. Shrink only.
_FILENAME_BASELINE = frozenset(
    {
        "coordinator/bin/tests/test_claude_author_content_root_parity.py",
        "coordinator/bin/tests/test_content_root_routing.py",
        "coordinator/bin/tests/test_harvest_content_root_machine_local_leg.py",
        "coordinator/bin/tests/test_machine_local_content_root_key_pair.py",
        "coordinator/tests/test_content_root_durable_resolution.py",
        "coordinator/tests/test_regen_cockpit_schema_content_root.py",
        "coordinator_core/_content_root_primitive.py",
        "coordinator_core/content_root.py",
        "coordinator_core/ops/content_root_resolve.py",
        "coordinator_core/ops/dispatch_emit/tests/fixtures/example_game_repo_route_table.json",
        "coordinator_core/ops/dispatch_emit/tests/test_agent_route_example_game_repo_table.py",
        "coordinator_core/ops/fleet/tests/fixtures/doubled_frontmatter/"
        "2026-07-31-example-market-data-repo-em-x-seven-day-horizon-delta.md.txt",
        "coordinator_core/ops/fleet/tests/fixtures/doubled_frontmatter/"
        "2026-08-01-example-market-data-repo-em-enabler-d-corpus-read-contract.md.txt",
        "coordinator_core/ops/tests/test_content_root_resolve.py",
        "coordinator_core/test_content_root.py",
        "coordinator_core/testing/content_root.py",
        "coordinator_core/testing/test_content_root_fixture.py",
        "coordinator_core/tests/test_content_root_for_both_layouts.py",
    }
)


def _git_lines(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    return out.stdout.splitlines()


def _leak_patterns(env: dict) -> list[str]:
    patterns: list[str] = []
    for key in leak._resolve_registry_keys(env):
        slug = key[len("repos."):] if key.startswith("repos.") else key
        if leak._is_kept(slug):
            continue
        for form in (slug, slug.replace("_", "-")):
            if form.lower() not in patterns:
                patterns.append(form.lower())
    return patterns


def codename_filename_hits(paths: list[str], env: dict) -> list[str]:
    """Paths whose lowercased text contains a non-kept registry codename, in either slug form."""
    patterns = _leak_patterns(env)
    return sorted(p for p in paths if any(pat in p.lower() for pat in patterns))


def test_fixture_filename_with_codename_is_flagged():
    env = {"COORDINATOR_CODENAME_REGISTRY_KEYS": "repos.zorbglatt repos.project_rag"}
    paths = ["coordinator_core/bash_guards/tests/test_x_zorbglatt_cases.py", "coordinator_core/ok.py"]
    assert codename_filename_hits(paths, env) == [paths[0]]


def test_fixture_hyphen_form_and_kept_slug_sides():
    env = {"COORDINATOR_CODENAME_REGISTRY_KEYS": "repos.zorb_glatt repos.project_rag"}
    assert codename_filename_hits(["a/zorb-glatt.md"], env) == ["a/zorb-glatt.md"]
    assert codename_filename_hits(["a/example_retrieval_repo_notes.py"], env) == []


def test_tracked_surface_filenames_add_no_new_codename_hit():
    env = dict(os.environ)
    if not _leak_patterns(env):
        pytest.skip("registry has no non-kept codename key on this machine")
    tracked = _git_lines("ls-files", *_SURFACE)
    new = [p for p in codename_filename_hits(tracked, env) if p not in _FILENAME_BASELINE]
    assert not new, (
        f"tracked filename(s) carry a private codename the publish no-residual-pattern guard refuses: {new}. "
        "Rename off the codename, or add a basename_rename and a baseline entry."
    )


_CALLEE_REL = "coordinator_core/ops/review_leg.py"
_CALLER_REL = "coordinator_core/ops/review_caller.py"
_CALLEE = "def reverify(rows, review_writes, *, group_slice_prefix=None):\n    return rows\n"
_CALLER_OMITS = (
    "from coordinator_core.ops.review_leg import reverify\n\n"
    "def run(rows):\n    return reverify(rows)\n"
)
_CALLER_PASSES = (
    "from coordinator_core.ops.review_leg import reverify\n\n"
    "def run(rows, writes):\n    return reverify(rows, writes)\n"
)


def _staged(root: Path, caller: str) -> None:
    for rel, text in ((_CALLEE_REL, _CALLEE), (_CALLER_REL, caller)):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / "coordinator_core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "coordinator_core" / "ops" / "__init__.py").write_text("", encoding="utf-8")


def _parity_violations(staging: Path, changed: list[str]) -> list[str]:
    # Dynamic load: the restricted publish tree carries no `percolate`, and a static import fails its closure gate.
    try:
        pp = importlib.import_module("coordinator_core.percolate.payload_parity")
    except ModuleNotFoundError:
        pytest.skip("coordinator_core.percolate is absent from this tree")
    with tempfile.TemporaryDirectory() as empty:
        report = pp.payload_parity_report(Path(empty), staging, rel_root="", changed_files=changed)
    return [f"{f.call_file}:{f.lineno} {f.message}" for f in report.refusing_violations]


def test_fixture_call_omitting_required_argument_is_flagged(tmp_path):
    _staged(tmp_path, _CALLER_OMITS)
    found = _parity_violations(tmp_path, [_CALLER_REL])
    assert len(found) == 1 and "review_writes" in found[0], found


def test_fixture_call_supplying_required_argument_passes(tmp_path):
    _staged(tmp_path, _CALLER_PASSES)
    assert _parity_violations(tmp_path, [_CALLER_REL]) == []


def test_changed_python_files_bind_against_their_callees():
    changed = [
        p
        for line in _git_lines("status", "--porcelain", "--", *_SURFACE)
        if (p := line[3:].strip().strip('"')).endswith(".py") and (_REPO_ROOT / p).is_file()
    ]
    if not changed:
        pytest.skip("no changed Python file under the published surface")
    found = _parity_violations(_REPO_ROOT, changed)
    assert not found, f"call site(s) do not bind against the callee signature: {found}"
