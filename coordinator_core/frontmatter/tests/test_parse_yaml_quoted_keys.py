"""parse_yaml reads a quoted mapping key as the bare string.

sizing.accept_exit_criterion writes `'on':` (quoted to dodge YAML 1.1's
boolean), and a literal-quote key made every later edit to that sizing
fail the schema guard with "Add on:".
"""

from coordinator_core.frontmatter.schema_validate import parse_yaml


def test_single_quoted_key_is_unquoted():
    assert parse_yaml("accepted:\n  'on': '2026-10-01'\n") == {"accepted": {"on": "2026-10-01"}}


def test_double_quoted_key_is_unquoted():
    assert parse_yaml('accepted:\n  "on": x\n') == {"accepted": {"on": "x"}}


def test_unquoted_key_is_unchanged():
    assert parse_yaml("on: x\n") == {"on": "x"}
