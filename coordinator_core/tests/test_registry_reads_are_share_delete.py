"""
Ceiling guard: a non-test module that names ``registry.local.toml`` or
``registry.toml`` and opens a file directly reads the registry through a
handle that blocks a concurrent replace on Windows (CPython ``open()`` omits
FILE_SHARE_DELETE). Registry reads go through
``coordinator_core.shared_read.read_bytes_shared``.

``_CEILING`` is the literal register of modules still reading with a plain
open; it can only shrink. A module that no longer hits is a stale entry and
fails. ``machine_resolver.py`` and ``_machine_local.py`` own the registry
write path and must never be in it.
"""

from __future__ import annotations

import os
import pathlib
import re
import time
from typing import Dict, List

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_ROOTS = ("coordinator_core", "coordinator", "scripts")
_EXCLUDED_DIRS = frozenset(
    {"tests", "testing", "benchmarks", "__pycache__", ".git", "node_modules", "scratch"}
)
_SCAN_MS_BUDGET = 200.0

_NAMES_REGISTRY = (b"registry.local.toml", b"registry.toml")
_OPENS_DIRECTLY_RE = re.compile(
    rb"\bopen\(|\.read_text\(|\.read_bytes\(|\btomllib\.load\(|\btomli\.load\("
)

_MUST_NOT_BE_REGISTERED = (
    "coordinator_core/machine_resolver.py",
    "coordinator/templates/bin/_machine_local.py",
)

# Substring match by design: _machine_local.py carries its own local
# _read_bytes_shared. The scan cannot verify that body, only that a reader exists.
_SHARED_READER_TOKEN = b"read_bytes_shared"

_PLAIN_OPEN = "names a registry file and opens with a plain open()/read_text(); migrate to read_bytes_shared"

_CEILING: Dict[str, str] = {
    "coordinator/bin/break_glass.py": _PLAIN_OPEN,
    "coordinator/bin/check-projectragignore-fleet-drift.py": _PLAIN_OPEN,
    "coordinator/bin/claude-author.py": _PLAIN_OPEN,
    "coordinator/bin/cruft-sweep.py": _PLAIN_OPEN,
    "coordinator/bin/lib/coordinator_registry.py": _PLAIN_OPEN,
    "coordinator/bin/lib/git_hook_install.py": _PLAIN_OPEN,
    "coordinator/bin/lib/machine_local_impl_resolve.py": _PLAIN_OPEN,
    "coordinator/bin/publish.py": _PLAIN_OPEN,
    "coordinator/bin/refresh-plugin-live-install.py": _PLAIN_OPEN,
    "coordinator/bin/seed-marketplace-enabledplugins.py": _PLAIN_OPEN,
    "coordinator/lib/percolate/targets.py": _PLAIN_OPEN,
    "coordinator/lib/resolve-coordinator-clone.py": _PLAIN_OPEN,
    "coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py": _PLAIN_OPEN,
    "coordinator_core/_fleet_names.py": _PLAIN_OPEN,
    "coordinator_core/bash_guards/_write_bump_applicability.py": _PLAIN_OPEN,
    "coordinator_core/bash_guards/block_push_pr_non_owned_remote.py": _PLAIN_OPEN,
    "coordinator_core/bash_guards/block_topic_branch.py": _PLAIN_OPEN,
    "coordinator_core/cadence/period_mode.py": _PLAIN_OPEN,
    "coordinator_core/execute_plan_assemble/goal_gate.py": _PLAIN_OPEN,
    "coordinator_core/hooks/platform_localize.py": _PLAIN_OPEN,
    "coordinator_core/install/check_install_singularity.py": _PLAIN_OPEN,
    "coordinator_core/install/hook_plane_verdict.py": _PLAIN_OPEN,
    "coordinator_core/install/maximalist.py": _PLAIN_OPEN,
    "coordinator_core/install/sandbox_check.py": _PLAIN_OPEN,
    "coordinator_core/install/substrate.py": _PLAIN_OPEN,
    "coordinator_core/ops/check_machine_local_regeneratability.py": _PLAIN_OPEN,
    "coordinator_core/ops/check_registry_codename_leak.py": _PLAIN_OPEN,
    "coordinator_core/ops/completion_ops.py": _PLAIN_OPEN,
    "coordinator_core/ops/detect_hardware.py": _PLAIN_OPEN,
    "coordinator_core/ops/discover_working_repos.py": _PLAIN_OPEN,
    "coordinator_core/ops/emit/doe_drift.py": _PLAIN_OPEN,
    "coordinator_core/ops/emit/resolvers.py": _PLAIN_OPEN,
    "coordinator_core/ops/emit/sections/_shared.py": _PLAIN_OPEN,
    "coordinator_core/ops/fleet/_memo_resolver.py": _PLAIN_OPEN,
    "coordinator_core/ops/fleet/capability_index.py": _PLAIN_OPEN,
    "coordinator_core/ops/fleet/consumer_corpus_preflight.py": _PLAIN_OPEN,
    "coordinator_core/ops/probe_cwd_example_retrieval_repo_relevance.py": _PLAIN_OPEN,
    "coordinator_core/ops/register_coordinator_mirror.py": _PLAIN_OPEN,
    "coordinator_core/ops/session/fix_concrete_path_citations.py": _PLAIN_OPEN,
    "coordinator_core/ops/verify_ue_overrides.py": _PLAIN_OPEN,
    "coordinator_core/pickup_gate_brief.py": _PLAIN_OPEN,
    "coordinator_core/plugin_health/drift.py": _PLAIN_OPEN,
    "coordinator_core/plugin_health/release_currency.py": _PLAIN_OPEN,
    "coordinator_core/plugin_health/sentinel.py": _PLAIN_OPEN,
    "coordinator_core/plugin_health/stale_registered_paths.py": _PLAIN_OPEN,
    "coordinator_core/snippet_sync/registry.py": _PLAIN_OPEN,
    "coordinator_core/snippet_sync/verify.py": _PLAIN_OPEN,
    "coordinator_core/snippet_sync/verify_registry_consistency.py": _PLAIN_OPEN,
    "coordinator_core/subagent_sandbox/provision_report.py": _PLAIN_OPEN,
    "coordinator_core/trusted_root_guard.py": _PLAIN_OPEN,
    "coordinator_core/write_guards/guard_doctrine_surface_edits.py": _PLAIN_OPEN,
}


def scan_direct_registry_readers(root: pathlib.Path) -> List[str]:
    found: List[str] = []
    root_str = str(root)
    prefix_len = len(root_str) + 1 if not root_str.endswith(os.sep) else len(root_str)
    for scan_root in _SCAN_ROOTS:
        top = os.path.join(root_str, scan_root)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
            for filename in filenames:
                if (
                    not filename.endswith(".py")
                    or filename.startswith("test_")
                    or filename == "conftest.py"
                ):
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    fd = os.open(full, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                except OSError:
                    continue
                try:
                    content = os.read(fd, os.fstat(fd).st_size + 1)
                except OSError:
                    continue
                finally:
                    os.close(fd)
                if (
                    any(n in content for n in _NAMES_REGISTRY)
                    and _SHARED_READER_TOKEN not in content
                    and _OPENS_DIRECTLY_RE.search(content) is not None
                ):
                    found.append(full[prefix_len:].replace(os.sep, "/"))
    return sorted(found)


def _evaluate(found: List[str], ceiling: Dict[str, str]) -> List[str]:
    problems = [f"unregistered direct registry reader: {p}" for p in found if p not in ceiling]
    problems += [f"stale ceiling entry (no longer hits): {p}" for p in ceiling if p not in found]
    problems += [f"blank reason: {p}" for p, why in ceiling.items() if not why.strip()]
    return problems


def test_the_live_ceiling_is_exact():
    assert _evaluate(scan_direct_registry_readers(_REPO_ROOT), _CEILING) == []


def test_the_registry_owners_are_never_on_the_ceiling():
    for path in _MUST_NOT_BE_REGISTERED:
        assert path not in _CEILING
        assert path not in scan_direct_registry_readers(_REPO_ROOT)


def test_scan_cost_is_at_or_below_the_budget():
    start = time.process_time()
    scan_direct_registry_readers(_REPO_ROOT)
    elapsed_ms = (time.process_time() - start) * 1000.0
    assert elapsed_ms <= _SCAN_MS_BUDGET, f"scan took {elapsed_ms:.1f}ms"


def test_an_unregistered_direct_reader_goes_red(tmp_path):
    mod = tmp_path / "coordinator_core" / "leaky.py"
    mod.parent.mkdir()
    mod.write_text('p = "registry.local.toml"\nopen(p, "rb")\n', encoding="utf-8")
    found = scan_direct_registry_readers(tmp_path)
    assert found == ["coordinator_core/leaky.py"]
    assert _evaluate(found, {}) == ["unregistered direct registry reader: coordinator_core/leaky.py"]


def test_a_registered_reader_that_stops_hitting_goes_red(tmp_path):
    mod = tmp_path / "coordinator_core" / "clean.py"
    mod.parent.mkdir()
    mod.write_text('p = "registry.toml"\nread_bytes_shared(p)\n', encoding="utf-8")
    found = scan_direct_registry_readers(tmp_path)
    assert found == []
    assert _evaluate(found, {"coordinator_core/clean.py": "why"}) == [
        "stale ceiling entry (no longer hits): coordinator_core/clean.py"
    ]


def test_a_module_that_reads_through_the_shared_reader_is_not_a_hit(tmp_path):
    mod = tmp_path / "coordinator_core" / "migrated.py"
    mod.parent.mkdir()
    mod.write_text(
        'p = "registry.toml"\ndef _read_bytes_shared(x):\n    return open(x, "rb").read()\n',
        encoding="utf-8",
    )
    assert scan_direct_registry_readers(tmp_path) == []


def test_a_module_naming_the_registry_without_opening_it_is_not_a_hit(tmp_path):
    mod = tmp_path / "coordinator_core" / "namer.py"
    mod.parent.mkdir()
    mod.write_text('NAME = "registry.local.toml"\n', encoding="utf-8")
    assert scan_direct_registry_readers(tmp_path) == []
