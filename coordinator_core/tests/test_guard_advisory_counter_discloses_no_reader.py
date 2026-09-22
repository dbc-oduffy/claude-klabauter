"""Pin: `guard_advisory_counter`'s own module docstring must not let a
future caller assume "counted" means "observed" (state/bug-backlog/
2026-08-29-loud-and-counted-is-neither-nobody-reads-the-counter-and-the-
agent-never-sees-the-stderr.yaml).

`resolve_plugin_root_loud` and `resolve_governed_authoring_surfaces`
(`bash_guards/dispatch.py`) already carry this caveat in their own
docstrings, but a THIRD caller of `record_advisory_fire`/`record_deny_fire`
would read `guard_advisory_counter.py` itself, not those two functions, to
learn what "counted" buys it. Before this module's own docstring said so
too, that reader had no way to learn -- from the mechanism's own source --
that neither `advisory-fire-counts.jsonl` nor `deny-fire-counts.jsonl` has
a consumer anywhere in this tree.

This does not assert a reader now EXISTS (it does not -- that is a
proportionality call the row leaves open) -- only that the module's own
docstring says so, so a future caller cannot mistake counting for
observability.
"""
from __future__ import annotations

import coordinator_core.guard_advisory_counter as guard_advisory_counter


def test_module_docstring_discloses_no_reader_exists() -> None:
    doc = guard_advisory_counter.__doc__ or ""
    assert "no reader" in doc.lower(), (
        "guard_advisory_counter's module docstring must plainly state that "
        "neither counter file has a reader today, so a future caller of "
        "record_advisory_fire/record_deny_fire does not mistake 'counted' "
        "for 'observed'"
    )
