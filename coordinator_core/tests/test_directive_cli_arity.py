"""Unit coverage for C11 of
`docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md`: the publish
gate's `argv_parity_report` must keep a directive site inside its domain
when the site is keyed `"op"` instead of `"cli"` -- the migrated form -- not
silently drop it as an unrecognized shape.

Does NOT assert against the live repo. Every fixture is written to a
`tmp_path` tree shaped like `<root>/coordinator_core/<module>.py`, the one
directory `argv_parity_report`'s module sweep reads for this module's
concern (no `coordinator/bin/` script needed: an `"op"`-keyed site is
in-process by construction and is never resolved against a bin script).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.directive_cli_arity import (
    argv_parity_report,
    emitted_option_tokens,
    looks_like_in_process_op,
)


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator" / "bin").mkdir(parents=True)
    return root


def test_op_keyed_dict_literal_site_is_discovered_and_treated_in_process(tmp_path):
    root = _make_repo(tmp_path)
    module = _write(
        root,
        "coordinator_core/op_assembler.py",
        '''
def build_directive(sid):
    args = ["--sid", sid]
    return {"id": "d1", "op": "noop-cli", "args": args, "depends_on": None}
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens == {"noop-cli": {"--sid"}}

    report = argv_parity_report(root)
    assert not any(p.cli == "noop-cli" for p in report.pairings), (
        "an op-keyed site is in-process by construction and must never "
        "surface a bin-script parity pairing"
    )


def test_op_keyed_builder_call_site_is_discovered_and_treated_in_process(tmp_path):
    root = _make_repo(tmp_path)
    module = _write(
        root,
        "coordinator_core/op_builder_assembler.py",
        '''
def _directive(id_, op, args, depends_on=None):
    return {"id": id_, "op": op, "args": args, "depends_on": depends_on}


def build_directives(sid):
    return [_directive("d1", "noop-cli", ["--sid", sid])]
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens == {"noop-cli": {"--sid"}}

    report = argv_parity_report(root)
    assert not any(p.cli == "noop-cli" for p in report.pairings)


def test_op_keyed_site_with_undeclared_dynamic_value_still_surfaces_unresolved(tmp_path):
    """An `"op"`-keyed site whose value is not a literal (dynamically
    computed) must still surface as an `unresolved` pairing -- the same
    fail-safe discipline a `"cli"`-keyed site gets -- never silently
    dropped from the report entirely."""
    root = _make_repo(tmp_path)
    module = _write(
        root,
        "coordinator_core/op_dynamic_assembler.py",
        '''
def build_directive(op_name, sid):
    args = ["--sid", sid]
    return {"id": "d1", "op": op_name, "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    assert any(p.unresolved for p in report.pairings), (
        "a dynamically-valued op-keyed site must surface unresolved, not "
        "vanish from the report"
    )


def test_looks_like_in_process_op_unaffected_by_op_key_discovery():
    """`looks_like_in_process_op` itself still discriminates purely on `.`
    containment for a bareword `cli` value -- the `"op"`-key discriminator
    lives at the site-discovery layer, not inside this function."""
    assert looks_like_in_process_op("handoff.author_fork") is True
    assert looks_like_in_process_op("noop-cli") is False
