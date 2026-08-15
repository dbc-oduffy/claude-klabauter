"""Unit coverage for `coordinator_core.directive_cli_arity`'s flag-parity
oracle -- `declared_option_strings`, `emitted_option_tokens`,
`argv_parity_report` -- against synthetic fixture sources.

Spec backlink: C1 of
`docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md`.

Does NOT assert against the live repo -- that is C2
(`test_engine_cli_argv_parity.py`). Every fixture here is written to a
`tmp_path` tree shaped like `<root>/coordinator_core/<module>.py` and
`<root>/coordinator/bin/<cli>.py`, exactly the two directories
`argv_parity_report` reads.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.directive_cli_arity import (
    argv_parity_report,
    declared_option_strings,
    declared_required_option_strings,
    emitted_option_tokens,
)

_SCRIPT_SOURCE = '''
import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="known-cli.py")
    parser.add_argument("--sid", default=None)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--prose", default=None)
    return parser


if __name__ == "__main__":
    build_parser().parse_args()
'''


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


def test_declared_option_strings_reads_literal_flags(tmp_path):
    root = _make_repo(tmp_path)
    script = _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    declared, saw_unresolved = declared_option_strings(script)
    assert declared == {"--sid", "--subject", "--prose"}
    assert saw_unresolved is False


def test_declared_required_option_strings_reads_required_flags(tmp_path):
    root = _make_repo(tmp_path)
    script = _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    required, saw_unresolved = declared_required_option_strings(script)
    assert required == {"--subject"}
    assert saw_unresolved is False


def test_emitted_option_tokens_subset_of_declared_is_clean(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/subset_assembler.py",
        '''
def build_directive(sid):
    args = ["--sid", sid]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens == {"known-cli": {"--sid"}}

    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.cli == "known-cli")
    assert pairing.unresolved is False
    assert pairing.unaccepted == frozenset()
    assert pairing.undeclared_required == frozenset({"--subject"})


def test_emitted_option_tokens_superset_reports_unaccepted(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/superset_assembler.py",
        '''
def build_directive(sid, subject):
    args = ["--sid", sid, "--subject", subject, "--not-declared-flag"]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens["known-cli"] == {"--sid", "--subject", "--not-declared-flag"}

    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.cli == "known-cli")
    assert pairing.unresolved is False
    assert pairing.unaccepted == frozenset({"--not-declared-flag"})
    assert pairing.undeclared_required == frozenset()


def test_two_consecutive_literal_flags_both_land_in_emitted_set(tmp_path):
    """Regression: `expect_value` must not swallow a literal dash-prefixed
    Constant as though it were the preceding flag's value -- two
    consecutive `store_true`-shaped flags in one list literal
    (`args += ["--foo", "--bar"]`) must both be collected, or the oracle
    reads a pairing clean when the emitter actually emits an unaccepted
    second flag."""
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/two_flags_assembler.py",
        '''
def build_directive():
    args = ["--sid", "--prose"]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens["known-cli"] == {"--sid", "--prose"}


def test_flag_after_dynamic_value_still_collected(tmp_path):
    """Mixed case: a literal flag, its dynamic (Name) value, then a second
    literal flag in the same list -- the dynamic value must still be
    treated as consumed (not unresolved), and the trailing flag must still
    land in the emitted set."""
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/mixed_flag_value_flag_assembler.py",
        '''
def build_directive(sid):
    args = ["--sid", sid, "--prose"]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens["known-cli"] == {"--sid", "--prose"}

    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is False
    assert pairing.unaccepted == frozenset()


def test_non_literal_flag_position_forces_unresolved(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/dynamic_flag_assembler.py",
        '''
def build_directive(dynamic_flag, sid):
    args = [dynamic_flag, "--sid", sid]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.cli == "known-cli")
    assert pairing.unresolved is True
    assert pairing.unaccepted == frozenset()
    assert pairing.undeclared_required == frozenset()


def test_module_level_name_binding_resolves(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/const_flag_assembler.py",
        '''
_SUBJECT_FLAG = "--subject"


def build_directive(sid, subject):
    args = ["--sid", sid, _SUBJECT_FLAG, subject]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    tokens = emitted_option_tokens(module)
    assert tokens["known-cli"] == {"--sid", "--subject"}

    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.cli == "known-cli")
    assert pairing.unresolved is False
    assert pairing.undeclared_required == frozenset()


def test_required_flag_left_unemitted_lands_in_undeclared_required(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/known-cli.py", _SCRIPT_SOURCE)
    module = _write(
        root,
        "coordinator_core/no_subject_assembler.py",
        '''
def build_directive(sid):
    args = ["--sid", sid]
    return {"id": "d1", "cli": "known-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.cli == "known-cli")
    assert pairing.unresolved is False
    assert pairing.undeclared_required == frozenset({"--subject"})


_WSC_TAIL_SCRIPT = '''
import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="wsc-tail.py")
    parser.add_argument("--sid", default=None)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--prose", default=None)
    parser.add_argument("--stage-paths", dest="stage_paths", nargs="+", default=None)
    parser.add_argument("--governing-plan-slug", dest="governing_plan_slug", default=None)
    parser.add_argument("--deleted-paths", dest="deleted_paths", nargs="+", default=None)
    parser.add_argument("--kept-entries", dest="kept_entries", nargs="+", default=None)
    parser.add_argument("--adjudication-present", dest="b_adjudication_present", action="store_true", default=False)
    parser.add_argument("--partition-mandatory", dest="partition_mandatory", action="store_true", default=False)
    return parser


if __name__ == "__main__":
    build_parser().parse_args()
'''

_WSC_CLOSE_SCRIPT = '''
import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="wsc-close.py")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    tail_args = sub.add_parser("tail-args")
    tail_args.add_argument("--deleted-paths", dest="deleted_paths", nargs="+", default=None)
    tail_args.add_argument("--kept-entries", dest="kept_entries", nargs="+", default=None)
    return parser


if __name__ == "__main__":
    build_parser().parse_args()
'''

_WSC_TAIL_ASSEMBLER = '''
def _directive(id_, cli, args, depends_on=None):
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on}


def build_close_tail_args_directive(decisions):
    args = ["tail-args"]
    if decisions.get("deleted_paths"):
        args += ["--deleted-paths", decisions["deleted_paths"]]
    if decisions.get("kept_entries"):
        args += ["--kept-entries", decisions["kept_entries"]]
    return _directive("d-close-tail-args", "wsc-close", args)


def build_wsc_tail_directive(sid, decisions, partition_mandatory=False):
    args = ["--sid", sid]
    if decisions.get("subject"):
        args += ["--subject", decisions["subject"]]
    if decisions.get("prose"):
        args += ["--prose", decisions["prose"]]
    stage_paths = decisions.get("stage_paths")
    if stage_paths:
        args += ["--stage-paths", stage_paths]
    if decisions.get("governing_plan_slug"):
        args += ["--governing-plan-slug", decisions["governing_plan_slug"]]
    if decisions.get("review_qualifies"):
        args.append("--adjudication-present")
    if partition_mandatory:
        args.append("--partition-mandatory")
    args.append("{d-close-tail-args.argv}")
    return _directive("d-run-wsc-tail", "wsc-tail", args, depends_on="d-close-tail-args")
'''


def test_wsc_tail_producer_token_pairing_resolves_via_sibling_directive_id(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/wsc-tail.py", _WSC_TAIL_SCRIPT)
    _write(root, "coordinator/bin/wsc-close.py", _WSC_CLOSE_SCRIPT)
    module = _write(
        root,
        "coordinator_core/workstream_complete_directives_commit_tail.py",
        _WSC_TAIL_ASSEMBLER,
    )

    tokens = emitted_option_tokens(module)
    wsc_tail_tokens = tokens["wsc-tail"]
    assert "--deleted-paths" in wsc_tail_tokens, "producer-token union did not pull in the d-close-tail-args flags"
    assert "--kept-entries" in wsc_tail_tokens
    assert {"--sid", "--subject", "--prose", "--stage-paths", "--governing-plan-slug", "--adjudication-present", "--partition-mandatory"} <= wsc_tail_tokens

    report = argv_parity_report(root)
    wsc_tail_pairing = next(p for p in report.pairings if p.cli == "wsc-tail")
    assert wsc_tail_pairing.unresolved is False, "the plan's motivating AC1 pairing must resolve non-unresolved"
    assert wsc_tail_pairing.unaccepted == frozenset()
    assert wsc_tail_pairing.undeclared_required == frozenset()


def test_unresolvable_producer_ref_forces_consumer_unresolved(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/wsc-tail.py", _WSC_TAIL_SCRIPT)
    module = _write(
        root,
        "coordinator_core/dangling_producer_ref_assembler.py",
        '''
def _directive(id_, cli, args, depends_on=None):
    return {"id": id_, "cli": cli, "args": args, "depends_on": depends_on}


def build_directive(sid):
    args = ["--sid", sid, "{d-nonexistent-producer.argv}"]
    return _directive("d-run-wsc-tail", "wsc-tail", args)
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.cli == "wsc-tail")
    assert pairing.unresolved is True


def test_unresolvable_cli_reports_unresolved_not_clean(tmp_path):
    root = _make_repo(tmp_path)
    module = _write(
        root,
        "coordinator_core/missing_script_assembler.py",
        '''
def build_directive():
    args = ["--flag"]
    return {"id": "d1", "cli": "no-such-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is True
    assert pairing.unaccepted == frozenset()
    assert pairing.undeclared_required == frozenset()


_TRAMPOLINE_SOURCE = '''
import sys


def _import_main():
    from coordinator_core.real_parser_module import main as _op_main
    return _op_main


def main() -> None:
    sys.exit(_import_main()(sys.argv[1:]))


if __name__ == "__main__":
    main()
'''

_REAL_PARSER_SOURCE = '''
import argparse


def main(argv):
    parser = argparse.ArgumentParser(prog="trampoline-cli.py")
    parser.add_argument("--sid", default=None)
    parser.add_argument("--subject", required=True)
    parser.parse_args(argv)
    return 0
'''


def test_trampoline_flags_resolve_via_one_hop_import(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/trampoline-cli.py", _TRAMPOLINE_SOURCE)
    real_parser = _write(root, "coordinator_core/real_parser_module.py", _REAL_PARSER_SOURCE)

    declared, saw_unresolved = declared_option_strings(
        root / "coordinator" / "bin" / "trampoline-cli.py"
    )
    assert declared == {"--sid", "--subject"}
    assert saw_unresolved is False

    required, required_unresolved = declared_required_option_strings(
        root / "coordinator" / "bin" / "trampoline-cli.py"
    )
    assert required == {"--subject"}
    assert required_unresolved is False

    module = _write(
        root,
        "coordinator_core/trampoline_assembler.py",
        '''
def build_directive(sid, subject):
    args = ["--sid", sid, "--subject", subject]
    return {"id": "d1", "cli": "trampoline-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is False
    assert pairing.unaccepted == frozenset()
    assert pairing.undeclared_required == frozenset()


def test_trampoline_with_unresolvable_import_reports_unresolved_not_unaccepted(tmp_path):
    root = _make_repo(tmp_path)
    unresolvable_trampoline = '''
import sys


def _import_main():
    from coordinator_core.no_such_parser_module import main as _op_main
    return _op_main


def main() -> None:
    sys.exit(_import_main()(sys.argv[1:]))


if __name__ == "__main__":
    main()
'''
    _write(root, "coordinator/bin/orphan-trampoline.py", unresolvable_trampoline)

    declared, saw_unresolved = declared_option_strings(
        root / "coordinator" / "bin" / "orphan-trampoline.py"
    )
    assert declared == set()
    assert saw_unresolved is True

    module = _write(
        root,
        "coordinator_core/orphan_trampoline_assembler.py",
        '''
def build_directive(sid):
    args = ["--sid", sid]
    return {"id": "d1", "cli": "orphan-trampoline", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is True
    assert pairing.unaccepted == frozenset()
    assert pairing.undeclared_required == frozenset()


_TWO_SUBCOMMAND_SCRIPT = '''
import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="two-subcommand-cli.py")
    parser.add_argument("--global-flag", required=True)
    sub = parser.add_subparsers(dest="subcommand", required=True)
    p_a = sub.add_parser("subcommand-a")
    p_a.add_argument("--a-only", required=True)
    p_b = sub.add_parser("subcommand-b")
    p_b.add_argument("--b-only", required=True)
    return parser


if __name__ == "__main__":
    build_parser().parse_args()
'''


def test_subcommand_scoped_required_flag_not_flagged_for_sibling_subcommand(tmp_path):
    """Regression for the false-positive class this fix removes: a
    directive invoking subcommand A must not be flagged for subcommand B's
    required flag, purely because both live in the same script's argparse
    tree."""
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/two-subcommand-cli.py", _TWO_SUBCOMMAND_SCRIPT)
    module = _write(
        root,
        "coordinator_core/subcommand_a_assembler.py",
        '''
def build_directive():
    args = ["subcommand-a", "--global-flag", "g", "--a-only", "x"]
    return {"id": "d1", "cli": "two-subcommand-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is False
    assert pairing.undeclared_required == frozenset(), (
        "must not be flagged for subcommand B's --b-only, and must not be "
        "flagged for its own --a-only or the top-level --global-flag since "
        "both are emitted"
    )


def test_subcommand_scoped_required_flag_still_enforced_for_own_subcommand(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/two-subcommand-cli.py", _TWO_SUBCOMMAND_SCRIPT)
    module = _write(
        root,
        "coordinator_core/subcommand_a_missing_own_flag_assembler.py",
        '''
def build_directive():
    args = ["subcommand-a", "--global-flag", "g"]
    return {"id": "d1", "cli": "two-subcommand-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is False
    assert pairing.undeclared_required == frozenset({"--a-only"})


def test_top_level_required_flag_still_enforced_regardless_of_subcommand(tmp_path):
    root = _make_repo(tmp_path)
    _write(root, "coordinator/bin/two-subcommand-cli.py", _TWO_SUBCOMMAND_SCRIPT)
    module = _write(
        root,
        "coordinator_core/subcommand_a_missing_global_flag_assembler.py",
        '''
def build_directive():
    args = ["subcommand-a", "--a-only", "x"]
    return {"id": "d1", "cli": "two-subcommand-cli", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    pairing = next(p for p in report.pairings if p.directive_id == "d1")
    assert pairing.unresolved is False
    assert pairing.undeclared_required == frozenset({"--global-flag"})


def test_in_process_op_cli_excluded_from_pairings(tmp_path):
    root = _make_repo(tmp_path)
    module = _write(
        root,
        "coordinator_core/in_process_op_assembler.py",
        '''
def build_directive():
    args = []
    return {"id": "d1", "cli": "handoff.author_fork", "args": args, "depends_on": None}
''',
    )
    report = argv_parity_report(root)
    assert all(p.cli != "handoff.author_fork" for p in report.pairings)
