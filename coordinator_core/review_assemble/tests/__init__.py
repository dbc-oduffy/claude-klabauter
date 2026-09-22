"""Package marker: qualifies this test module's import name.

Without it pytest names the module by BASENAME alone, and this directory holds
a `test_scaffold_directive_parity.py` that eight sibling packages also have --
so two of them collided and errored collection for the whole tree, which the
publish assembled-mirror gate (collect-only, fatal, no override) reads as a
broken mirror. 58 of this repo's 80 `tests/` directories already carry one.
"""
