"""
coordinator_core.workweek_complete — the `workweek-complete` computed-skill
engine (`brief.py` read-only half, `apply.py` mutating half).

Purpose: this file's sole job is to make `workweek_complete` a proper
package rather than an implicit namespace package — its submodules are
imported directly (`coordinator_core.workweek_complete.brief`/`.apply`) and
carry their own module docstrings; this `__init__` exports nothing.
Package-qualifying this directory keeps `test_apply.py`'s module identity
distinct from `workday_complete.test_apply`/`workstream_complete.test_apply`
under pytest's rootless basename collection (see those siblings' own
`__init__.py`).
"""

from __future__ import annotations
