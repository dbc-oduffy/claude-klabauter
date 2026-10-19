"""Contract tests for ``_literal_bindings.bind_literal_operands`` -- pure, no repo."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_simple,
    tokenize_full_command,
)
from coordinator_core.bash_guards._literal_bindings import (
    bind_literal_iterations,
    bind_literal_operands,
)


def _segs(cmd):
    tokens = tokenize_full_command(cmd)
    assert tokens is not None
    return [list(s) for s in segments_from_tokens_simple(tokens) if s]


def _bind(cmd):
    return bind_literal_operands(_segs(cmd))


def test_for_header_binds_loop_variable():
    out = _bind('for f in a/t1.py a/t2.py; do pytest "$f"; done')
    assert out[1] == ["do", "pytest", "a/t1.py", "a/t2.py"]


def test_standalone_assignment_splits_into_words():
    out = _bind('FILES="a/t1.py a/t2.py"; pytest $FILES')
    assert out[1] == ["pytest", "a/t1.py", "a/t2.py"]


def test_export_assignment_binds():
    assert _bind("export F=a/t1.py; pytest $F")[1] == ["pytest", "a/t1.py"]


def test_braced_form_binds():
    assert _bind("F=a/t1.py; pytest ${F}")[1] == ["pytest", "a/t1.py"]


def test_export_of_bound_name_keeps_binding():
    assert _bind("F=a/t1.py; export F; pytest $F")[2] == ["pytest", "a/t1.py"]


def test_for_list_word_resolves_once_through_variable():
    out = _bind('FILES="a/t1.py"; for f in $FILES; do pytest "$f"; done')
    assert out[2] == ["do", "pytest", "a/t1.py"]


def test_for_list_does_not_chain_two_levels_of_non_literal():
    out = _bind('A=$(ls); B=$A; for f in $B; do pytest $f; done')
    assert out[3] == ["do", "pytest", "$f"]


def test_no_variables_returns_equal_argvs():
    segs = _segs("cd x && python -m pytest tests/a.py -q")
    assert bind_literal_operands(segs) == segs


def test_input_not_mutated():
    segs = _segs("F=a/t1.py; pytest $F")
    before = [list(s) for s in segs]
    bind_literal_operands(segs)
    assert segs == before


@pytest.mark.parametrize(
    "cmd, unresolved",
    [
        ("F=a/t1.py pytest $F", "$F"),
        ("F=$(git ls-files); pytest $F", "$F"),
        ("F=`ls`; pytest $F", "$F"),
        ("F=tests/*.py; pytest $F", "$F"),
        ("F=tests/t{1,2}.py; pytest $F", "$F"),
        ("F=~/t.py; pytest $F", "$F"),
        ("F=(a b); pytest $F", "$F"),
        ("F=a/t1.py; F+=x; pytest $F", "$F"),
        ("F=a/t1.py; read F; pytest $F", "$F"),
        ("F=a/t1.py; unset F; pytest $F", "$F"),
        ("F=a/t1.py; declare F=tests; pytest $F", "$F"),
        ("F=a/t1.py; F=tests; pytest $F", "$F"),
        ("pytest $F; F=a/t1.py", "$F"),
        ("F=a/t1.py; eval 'F=tests'; pytest $F", "$F"),
        ("F=a/t1.py; { F=tests; }; pytest $F", "$F"),
        ("if true; then F=a/t1.py; fi; pytest $F", "$F"),
        ("for f in a/t1.py; do for f in tests; do pytest $f; done; done", "$f"),
        ("for f in $(git ls-files); do pytest $f; done", "$f"),
        ("for f in tests/*.py; do pytest $f; done", "$f"),
        ("for f in `ls tests`; do pytest $f; done", "$f"),
        ("for f; do pytest $f; done", "$f"),
        ("F=a/t1.py; pytest tests/$F/", "tests/$F/"),
        ("F=a/t1.py; pytest $env:F", "$env:F"),
    ],
)
def test_unresolvable_reference_is_left_untouched(cmd, unresolved):
    segs = _segs(cmd)
    out = bind_literal_operands(segs)
    assert len(out) == len(segs)
    flat = [t for s in out for t in s]
    assert unresolved in flat


def test_use_before_binding_is_left_untouched():
    out = _bind("pytest $F; F=a/t1.py")
    assert out[0] == ["pytest", "$F"]


def test_prefix_assignment_does_not_disturb_prior_binding():
    out = _bind("F=a/t1.py; F=tests pytest $F")
    assert out[1][-1] == "$F"


def test_empty_assignment_binds_to_no_words():
    assert _bind("F=; pytest $F")[1] == ["pytest"]


def test_eval_only_poisons_later_segments():
    out = _bind("F=a/t1.py; pytest $F; eval x; pytest $F")
    assert out[1] == ["pytest", "a/t1.py"]
    assert out[3] == ["pytest", "$F"]


def test_empty_input():
    assert bind_literal_operands([]) == []
    assert bind_literal_operands([[]]) == [[]]


def _iter(cmd):
    segs = _segs(cmd)
    out = bind_literal_iterations(segs)
    assert len(out) == len(segs)
    assert all(variants for variants in out)
    return out


def test_iterations_no_variable_is_single_equal_variant():
    segs = [["pytest", "a/t1.py"]]
    assert bind_literal_iterations(segs) == [[["pytest", "a/t1.py"]]]


def test_iterations_plain_variable_is_one_all_words_variant():
    out = _iter('FILES="a/t1.py a/t2.py"; pytest $FILES')
    assert out[1] == [["pytest", "a/t1.py", "a/t2.py"]]


def test_iterations_for_body_yields_one_variant_per_word():
    out = _iter('for f in a/t1.py a/t2.py; do x "$f"; done')
    assert out[1] == [["do", "x", "a/t1.py"], ["do", "x", "a/t2.py"]]
    assert len(out[0]) == 1


def test_iterations_for_list_through_variable_iterates_its_words():
    out = _iter('FILES="a b"; for f in $FILES; do x "$f"; done')
    assert out[2] == [["do", "x", "a"], ["do", "x", "b"]]


def test_iterations_other_names_bind_all_words_beside_a_for_name():
    out = _iter('FILES="a b"; for f in c d; do x $FILES "$f"; done')
    assert out[2] == [["do", "x", "a", "b", "c"], ["do", "x", "a", "b", "d"]]


def test_iterations_two_for_names_give_the_product():
    segs = [["for", "f", "in", "a", "b"], ["for", "g", "in", "c", "d"], ["x", "$f", "$g"]]
    body = bind_literal_iterations(segs)[2]
    assert sorted(v[-2:] for v in body) == [["a", "c"], ["a", "d"], ["b", "c"], ["b", "d"]]


def test_iterations_product_over_cap_stays_unresolved():
    words = " ".join(f"w{i}" for i in range(9))
    segs = [
        ["for", "f", "in", *words.split()],
        ["for", "g", "in", *words.split()],
        ["x", "$f", "$g"],
    ]
    assert bind_literal_iterations(segs)[2] == [["x", "$f", "$g"]]


@pytest.mark.parametrize("cmd", [
    "for f in $(git ls-files); do x $f; done",
    "for f in tests/*.py; do x $f; done",
])
def test_iterations_unresolvable_for_list_is_single_unresolved_variant(cmd):
    out = _iter(cmd)
    assert out[1] == [["do", "x", "$f"]]


def test_iterations_empty_input():
    assert bind_literal_iterations([]) == []
    assert bind_literal_iterations([[]]) == [[[]]]
