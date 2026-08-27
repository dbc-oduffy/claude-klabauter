"""Oracle for the amplification exemption at
`coordinator_core/ops/setup_chain_walker.py::dep_probe_all::dep_probe`.

The claim (state/audits/2026-08-19-amplification-register-remaining-fourteen-dispositions.md,
"Oracle-decidable" section): this is a VARYING-PROGRAM site. For a `python_import` dep the `-c`
source executed is the manifest-declared `functional_probe_args["expr"]`, which differs per dep
by construction -- not a fixed program invoked once per row. No bounded static taint pass can
carry "this string came from a disk-read manifest field" through `_dep_by_id` ->
`functional_probe_args` -> the subprocess call without becoming the "resolves to any
transitive spawner" rule this module already measured at 189 near-all-false hits (see the
audit). Batching the probes into one interpreter would also violate `dep_probe`'s own
documented import-state-isolation / crash-containment invariant (a malicious or crashing
manifest-supplied import must not land in this process's `sys.modules` or crash it) --
see state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.

This oracle pins both halves of that claim as observed facts rather than inferred ones: drive
`dep_probe_all` over a synthetic manifest carrying M `python_import` deps and assert (a) exactly
M spawns, and (b) each spawn's argv differs from every other. `subprocess.run` is monkeypatched
at the module's own reference -- no real interpreters are launched, and every fixture lives
under pytest's own `tmp_path`, so this never touches the shared working tree and stays
fast-tier-safe under a ~50-concurrent-session load.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import coordinator_core.ops.setup_chain_walker as setup_chain_walker

_M = 4


def _write_manifest(tmp_path: Path, exprs: list[str]) -> tuple[Path, Path]:
    """Synthetic manifest with one `python_import` dep per entry in ``exprs``, each pointing
    at its own pre-created sibling directory (so dep resolution succeeds without exercising the
    fallback ladder, which is orthogonal to this oracle's claim)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # `_sibling_search_root` walks up from `repo_root` for the nearest `.git`
    # entry (`resolve_git_root_cheap`) before falling back to a manifest-
    # nesting-depth guess; without a real marker here it takes that fallback
    # and resolves siblings against the wrong ancestor, and `sibling_path`
    # never matches the `sib-N` dirs this fixture creates beside `repo_root`.
    (repo_root / ".git").mkdir()
    deps = []
    for i, expr in enumerate(exprs):
        sibling_dir = f"sib-{i}"
        (tmp_path / sibling_dir).mkdir()
        deps.append(
            {
                "id": f"dep-{i}",
                "severity": "hard",
                "sibling_dir_name": sibling_dir,
                "upstream_url": f"https://example.invalid/dep-{i}",
                "functional_probe_kind": "python_import",
                "functional_probe_args": {"expr": expr},
            }
        )
    manifest_path = tmp_path / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps({"direct_deps": deps}), encoding="utf-8")
    return manifest_path, repo_root


def _patched_run(monkeypatch, calls: list):
    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(setup_chain_walker.subprocess, "run", fake_run)


def test_dep_probe_all_issues_one_spawn_per_python_import_dep(tmp_path, monkeypatch):
    """M `python_import` deps must cost exactly M spawns -- the register's claim that this
    site cannot be batched depends on it actually running one subprocess per dep, not on a
    cached or short-circuited path silently doing fewer."""
    calls: list = []
    _patched_run(monkeypatch, calls)

    exprs = [f"import sys; sys.exit(0)  # dep-{i}" for i in range(_M)]
    manifest_path, repo_root = _write_manifest(tmp_path, exprs)

    rows = setup_chain_walker.dep_probe_all(manifest_path, repo_root)

    assert len(rows) == _M
    assert len(calls) == _M, (
        f"dep_probe_all issued {len(calls)} spawns for {_M} python_import deps -- the "
        "VARYING-PROGRAM exemption claims one spawn per dep; a count that no longer matches M "
        "means either a dep silently short-circuited or the loop started batching, and either "
        "way the exemption needs to be re-verified against the code, not re-asserted here."
    )


def test_dep_probe_all_python_import_spawns_have_distinct_argv(tmp_path, monkeypatch):
    """The varying-program half of the claim: each spawn's full argv (not merely its dep id)
    must differ from every other, because the executed `-c` source is the per-dep manifest
    `expr` -- proof this is not a fixed program invoked M times, which is the shape a static
    per-item-spawn predicate could otherwise have flagged and batched."""
    calls: list = []
    _patched_run(monkeypatch, calls)

    exprs = [f"import sys; sys.exit(0)  # dep-{i}" for i in range(_M)]
    manifest_path, repo_root = _write_manifest(tmp_path, exprs)

    setup_chain_walker.dep_probe_all(manifest_path, repo_root)

    assert len(calls) == _M
    assert len(set(calls)) == _M, (
        "two or more python_import spawns issued the IDENTICAL argv -- the exemption's "
        "premise is that each dep's `expr` differs, so identical argv means either the "
        "synthetic manifest stopped varying `expr` per dep, or dep_probe stopped threading "
        "the per-dep expr through to the child. Either way the varying-program claim is not "
        "currently demonstrated."
    )


def test_oracle_fails_when_all_deps_share_one_expr(tmp_path, monkeypatch):
    """Proves the argv-distinctness oracle is not vacuous: a manifest where every dep shares
    the SAME `expr` must fail the distinctness assertion. Without this, a bug that dropped the
    per-dep expr substitution (e.g. dep_probe always running the first dep's expr) would pass
    silently."""
    calls: list = []
    _patched_run(monkeypatch, calls)

    exprs = ["import sys; sys.exit(0)"] * _M
    manifest_path, repo_root = _write_manifest(tmp_path, exprs)

    setup_chain_walker.dep_probe_all(manifest_path, repo_root)

    assert len(calls) == _M
    assert len(set(calls)) == 1, (
        "sanity check itself is broken: a manifest with an identical `expr` on every dep was "
        "supposed to produce identical argv, so it could demonstrate the real oracle catches "
        "non-distinctness."
    )
