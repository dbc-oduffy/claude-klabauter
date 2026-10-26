"""Hoisting reaches double-quoted literals and recurring constant `+` chains, and still resolves back exactly."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import prompt_fragments as pf


@pytest.fixture(autouse=True)
def _hoisting_on(monkeypatch):
    monkeypatch.setattr(pf, "HOIST_ENABLED", True)


_S1 = "This first sentence is long enough to be hoisted as a shared fragment. "


def _hoist(src: str) -> str:
    return pf.hoist_repeated_fragments(src, anchor="// anchor")


def test_double_quoted_repeated_sentence_is_hoisted_and_resolves_back():
    src = f'// anchor\nconst a = "{_S1}" + x;\nconst b = "{_S1}" + y;\n'
    out = _hoist(src)
    assert "const _PF0 = '" + _S1 + "';" in out and '"' not in out.replace("// anchor", "")
    assert pf.resolve_fragment_refs(out) == pf.fold_literals(src)


def test_escaped_quotes_survive_the_round_trip():
    s = 'It\'s a sentence with a "quoted" word and enough length to hoist. '
    dq = s.replace("\\", "\\\\").replace('"', '\\"')
    sq = s.replace("'", "\\'")
    src = f'// anchor\nconst a = "{dq}" + x;\nconst b = \'{sq}\' + y;\n'
    out = _hoist(src)
    assert f"const _PF0 = '{sq}';" in out
    assert pf.resolve_fragment_refs(out) == pf.fold_literals(src)
    assert pf.fold_literals(src).count(f"'{sq}'") == 2


def test_unhoisted_double_quoted_literal_is_untouched_by_hoist():
    src = '// anchor\nconst a = "short";\nconst b = "short";\n'
    assert _hoist(src) == src


_SENTENCES = [f"Sentence number {k} is long enough to be hoisted on its own merits here. " for k in range(8)]
_RUN = " + ".join(f"'{t}'" for t in _SENTENCES)


def _run_script(prefix: str = "", suffix: str = "", uses: int = 4) -> str:
    lines = "".join(f"const v{k} = {prefix}{_RUN}{suffix};\n" for k in range(uses))
    return "// anchor\n" + lines


def test_constant_run_is_hoisted_to_a_composite_that_precedes_its_use():
    src = _run_script(suffix=" + tail")
    out = _hoist(src)
    assert "const _PFC0 = _PF" in out
    assert out.index("const _PFC0 = ") < out.index("v0 = _PFC0")
    assert "v0 = _PFC0 + tail;" in out and "v3 = _PFC0 + tail;" in out
    assert pf.resolve_fragment_refs(out) == pf.fold_literals(src)


def test_non_constant_operand_limits_the_composite_to_the_constant_subrun():
    src = _run_script(prefix="x + ", suffix=" + f()")
    out = _hoist(src)
    assert "v0 = x + _PFC0 + f();" in out
    assert pf.resolve_fragment_refs(out) == pf.fold_literals(src)
    split = "// anchor\n" + "".join(
        f"const v{k} = " + " + g() + ".join(f"'{t}'" for t in _SENTENCES[:4]) + ";\n" for k in range(4)
    )
    assert "_PFC" not in _hoist(split)


def test_run_followed_by_a_member_access_does_not_absorb_the_tight_operand():
    out = _hoist(_run_script(suffix=".trim()"))
    assert "const _PFC0 = " in out
    assert "_PFC0.trim()" not in out and "_PFC0 + (_PF7).trim()" in out


def test_two_operand_run_is_not_a_composite():
    src = "// anchor\n" + "".join(f"const v{k} = '{_SENTENCES[0]}' + '{_SENTENCES[1]}';\n" for k in range(6))
    assert "_PFC" not in _hoist(src)


def test_hoist_disabled_leaves_script_byte_identical(monkeypatch):
    monkeypatch.setattr(pf, "HOIST_ENABLED", False)
    src = _run_script()
    assert _hoist(src) == src
