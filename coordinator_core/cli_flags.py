"""Shared CLI flag spellings.

Rule: a flag spelled at both an emitter (a directive's `args`) and a parser
(`add_argument`) is named here once, as a module-level str constant, and both
sides import it. `directive_cli_arity` resolves these constants statically.

Constants only: no imports, no logic.
"""
