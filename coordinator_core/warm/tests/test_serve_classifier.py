"""Fixture-shape coverage for `coordinator_core.warm.serve_classifier`,
lifted alongside the predicate it tests (see that module's docstring). DoE's
14 predicate tests are the reason their checker can be trusted; a lifted
predicate without them is an unverified copy -- this file carries the same
14 structural-purity fixtures PLUS the fixtures for C1's own three deltas
(arity, script existence, module-scope import purity) and a live-corpus
smoke test over the real `coordinator/bin` population.

Spec backlink: docs/plans/2026-08-27-every-bin-name-warm-serves-and-a-classifier-says-so.md, chunk C1
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.warm import serve_classifier as sc


# --- Lifted structural fixtures (parity with DoE's 14) ----------------------


def test_predicate_fails_on_missing_main():
    source = '''
"""A CLI with no main(argv) to call."""
import sys

print("hello")
'''
    findings = sc.find_module_body_violations(source, "fixture/no_main.py")
    reasons = {f.reason for f in findings}
    assert "missing module-level main(argv)" in reasons


def test_predicate_fails_on_module_scope_sys_path_insert():
    source = '''
"""A CLI whose module scope mutates sys.path before main runs."""
import sys

sys.path.insert(0, "/some/dir")


def main(argv):
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''
    findings = sc.find_module_body_violations(source, "fixture/path_insert.py")
    reasons = {f.reason for f in findings}
    assert "module-scope process mutation" in reasons
    assert not any(f.reason == "missing module-level main(argv)" for f in findings)


def test_predicate_passes_clean_entrypoint():
    source = '''
"""A correctly-shaped entrypoint: only imports, defs, and the guard at
module scope."""
import sys


def main(argv):
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''
    assert sc.find_module_body_violations(source, "fixture/clean.py") == []


def test_predicate_fails_on_impure_assign_rhs():
    source = '''
"""A CLI whose module scope mutates sys.path via a discarded Assign."""
import sys

_ = sys.path.insert(0, "/x")


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_assign.py")
    reasons = {f.reason for f in findings}
    assert "module-scope process mutation" in reasons


def test_predicate_fails_on_impure_assign_to_name():
    source = '''
"""A CLI whose module scope chdirs via a named Assign target."""
import os

X = os.chdir("/tmp")


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_assign_name.py")
    reasons = {f.reason for f in findings}
    assert "module-scope process mutation" in reasons


def test_predicate_fails_on_impure_annassign():
    source = '''
"""A CLI whose module scope runs a shell command via an annotated Assign."""
import os

X: int = os.system("echo hi")


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_annassign.py")
    reasons = {f.reason for f in findings}
    assert "module-scope process mutation" in reasons


def test_predicate_fails_on_classdef_body_mutation():
    source = '''
"""A CLI hiding module-scope-executed code inside a ClassDef body."""
import sys


class C:
    sys.path.insert(0, "/x")


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/classdef_body.py")
    reasons = {f.reason for f in findings}
    assert "module-scope process mutation" in reasons


def test_predicate_fails_on_impure_decorator():
    source = '''
"""A CLI whose entrypoint decorator executes at import time."""
import sys


def deco(fn):
    sys.path.insert(0, "/x")
    return fn


@deco
def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_decorator.py")
    reasons = {f.reason for f in findings}
    assert "impure decorator" in reasons


def test_predicate_fails_on_call_target_that_is_itself_a_call():
    source = '''
"""A CLI that obtains os.chdir via getattr, then calls the result."""
import os

X = getattr(os, "chdir")("/tmp")


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/call_of_call.py")
    reasons = {f.reason for f in findings}
    assert "module-scope process mutation" in reasons


def test_predicate_fails_on_whitelisted_name_rebinding():
    source = '''
"""A CLI that shadows the trusted name Path with os.chdir."""
import os

Path = os.chdir
X = Path("/tmp")


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/rebind_whitelisted_name.py")
    reasons = {f.reason for f in findings}
    assert "whitelisted-name rebinding" in reasons


def test_predicate_fails_on_impure_function_default():
    source = '''
"""A CLI whose helper default argument mutates sys.path at import."""
import sys


def helper(x=sys.path.insert(0, "/x")):
    return x


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_default.py")
    reasons = {f.reason for f in findings}
    assert "impure default-argument expression" in reasons


def test_predicate_fails_on_impure_classdef_base():
    source = '''
"""A CLI whose class base expression executes an impure call at import."""
import sys


def mk():
    sys.path.insert(0, "/x")
    return object


class C(mk()):
    pass


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_classdef_base.py")
    reasons = {f.reason for f in findings}
    assert "impure class base expression" in reasons


def test_predicate_fails_on_impure_classdef_keyword():
    source = '''
"""A CLI whose class metaclass keyword executes an impure call at import."""
import sys


def mk():
    sys.path.insert(0, "/x")
    return type


class C(object, metaclass=mk()):
    pass


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/impure_classdef_keyword.py")
    reasons = {f.reason for f in findings}
    assert "impure class keyword expression" in reasons


# --- C1 delta 1: arity -------------------------------------------------------


def test_zero_arity_main_is_not_servable():
    source = '''
"""A CLI whose main() takes no argv -- the ~160-name defect."""


def main():
    return 0
'''
    tree_findings = sc.find_module_body_violations(source, "fixture/zero_arity.py")
    assert tree_findings == []  # module body IS inert -- arity is a separate axis
    verdict = sc.ServeVerdict(
        name="zero_arity",
        script_relpath="fixture/zero_arity.py",
        script_exists=True,
        has_main=True,
        main_arity_ok=False,
        findings=(),
    )
    assert verdict.servable is False


def test_main_argv_with_default_is_servable_arity():
    import ast

    source = "def main(argv=None):\n    return 0\n"
    tree = ast.parse(source)
    fn = tree.body[0]
    assert sc._main_arity_ok(fn) is True


def test_main_zero_arity_arity_check():
    import ast

    source = "def main():\n    return 0\n"
    tree = ast.parse(source)
    fn = tree.body[0]
    assert sc._main_arity_ok(fn) is False


def test_main_star_args_counts_as_servable_arity():
    import ast

    source = "def main(*args):\n    return 0\n"
    tree = ast.parse(source)
    fn = tree.body[0]
    assert sc._main_arity_ok(fn) is True


# --- C1 delta 2: script existence -------------------------------------------


def test_classify_entrypoint_missing_script(tmp_path):
    verdict = sc.classify_entrypoint("does-not-exist", bin_dir=tmp_path)
    assert verdict.script_exists is False
    assert verdict.has_main is False
    assert verdict.servable is False
    assert verdict.findings == ()


def test_classify_entrypoint_resolves_by_name(tmp_path):
    (tmp_path / "real-name.py").write_text(
        '"""doc."""\n\n\ndef main(argv):\n    return 0\n', encoding="utf-8"
    )
    verdict = sc.classify_entrypoint("real-name", bin_dir=tmp_path)
    assert verdict.script_exists is True
    assert verdict.has_main is True
    assert verdict.main_arity_ok is True
    assert verdict.servable is True
    assert verdict.inert is True


# --- C1 delta 3: module-scope import purity (the load-bearing delta) --------


def test_module_scope_lib_import_is_flagged():
    """Verbatim the shape that killed coordinator-auto-push.py on the
    forwarder route: a module-scope `from lib.X import Y` PASSES the lifted
    predicate's structural check (Import/ImportFrom is inert-by-construction
    there) but must fail C1's import-purity conjunct."""
    source = '''
"""A CLI importing a non-stdlib helper at module scope."""
from lib.cc_invoke import require_dispatch_engine_on_path


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/lib_import.py")
    reasons = {f.reason for f in findings}
    assert "module-scope non-stdlib import" in reasons


def test_module_scope_coordinator_core_import_is_flagged():
    source = '''
"""A CLI importing coordinator_core at module scope."""
import coordinator_core.ipc


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/cc_import.py")
    reasons = {f.reason for f in findings}
    assert "module-scope non-stdlib import" in reasons


def test_module_scope_stdlib_import_is_not_flagged():
    source = '''
"""A CLI importing only stdlib at module scope."""
import os
import sys
from pathlib import Path
from __future__ import annotations


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/stdlib_import.py")
    assert findings == []


def test_guarded_optional_import_of_non_stdlib_is_still_flagged():
    """The DoE-permitted `try: import yaml / except ImportError: yaml = None`
    shape is structurally inert (no process mutation), but `yaml` is
    non-stdlib -- C1's conjunct must see through the guard, unlike the
    lifted predicate's own AC6 exemption (which only concerns structural
    purity, not import identity)."""
    source = '''
"""A CLI with a guarded optional third-party dependency."""
try:
    import yaml
except ImportError:
    yaml = None


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/guarded_yaml.py")
    reasons = {f.reason for f in findings}
    assert "module-scope non-stdlib import" in reasons


def test_guarded_optional_import_of_stdlib_is_not_flagged():
    source = '''
"""A CLI with a guarded optional stdlib-only import."""
try:
    import tomllib
except ImportError:
    tomllib = None


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/guarded_stdlib.py")
    assert findings == []


def test_relative_import_is_flagged():
    source = '''
"""A CLI with a relative import -- never valid for a standalone bin CLI."""
from . import helper


def main(argv):
    return 0
'''
    findings = sc.find_module_body_violations(source, "fixture/relative_import.py")
    reasons = {f.reason for f in findings}
    assert "module-scope non-stdlib import" in reasons


# --- Partition report --------------------------------------------------------


def test_partition_report_buckets_are_mutually_exclusive_and_sum_to_total():
    verdicts = [
        sc.ServeVerdict("a", "a.py", False, False, False, ()),  # no_script
        sc.ServeVerdict("b", "b.py", True, False, False, ()),  # no_main
        sc.ServeVerdict("c", "c.py", True, True, False, ()),  # zero_arity_main
        sc.ServeVerdict("d", "d.py", True, True, True, ()),  # main_argv, inert
        sc.ServeVerdict(
            "e",
            "e.py",
            True,
            True,
            True,
            (sc.Finding("e.py", 3, "sys.path.insert(0, 'x')", "module-scope process mutation"),),
        ),  # main_argv, sys_path_mutation
    ]
    report = sc.partition_report(verdicts)
    assert report["total"] == 5
    assert report["no_script"] == 1
    assert report["no_main"] == 1
    assert report["zero_arity_main"] == 1
    assert report["main_argv"] == 2
    assert report["cannot_serve"] == 3
    assert report["servable"] == 2
    assert report["sys_path_mutation"] == 1
    assert report["servable_and_inert"] == 1  # only "d" is servable AND has zero findings


def test_load_allowlist_names_is_a_named_population():
    names = sc.load_allowlist_names()
    assert isinstance(names, list)
    assert len(names) > 0
    assert all(isinstance(n, str) for n in names)


def test_pure_call_target_whitelist_is_reviewed_not_empty():
    """C1's own re-seed record: the whitelist is non-empty (DoE's shape
    ported) and does NOT contain any of the repo-local engine-bootstrap
    helper names the classifier exists to keep flagging."""
    assert len(sc._PURE_CALL_TARGETS) > 0
    for bootstrap_name in (
        "require_dispatch_engine_on_path",
        "require_engine_on_path",
        "require_colocated_engine_on_path",
    ):
        assert bootstrap_name not in sc._PURE_CALL_TARGETS


# --- Live-corpus smoke test --------------------------------------------------


def test_live_allowlist_population_classifies_without_error():
    """Runs the classifier over the real, named allowlist population --
    never a directory glob (Anti-scope: "Do not invoke the 382 to measure
    them" concerns RUNTIME import; static AST parsing every allowlisted
    script is exactly this chunk's job). Asserts only that every name
    produces a verdict and the partition buckets stay internally consistent
    -- the actual counts are expected to change as C2-C6 land, so this test
    does not pin any of this session's specific numbers."""
    names = sc.load_allowlist_names()
    verdicts = sc.classify_population(names)
    assert len(verdicts) == len(names)
    report = sc.partition_report(verdicts)
    assert report["total"] == len(names)
    assert report["no_script"] + report["no_main"] + report["zero_arity_main"] + report["main_argv"] == report["total"]
    assert report["cannot_serve"] + report["main_argv"] == report["total"]


def test_classify_population_is_over_a_named_list_not_a_glob():
    """`classify_population` never globs `coordinator/bin` itself -- its
    only source of names is the list a caller passes in."""
    verdicts = sc.classify_population(["definitely-not-a-real-entrypoint-name"])
    assert len(verdicts) == 1
    assert verdicts[0].script_exists is False
