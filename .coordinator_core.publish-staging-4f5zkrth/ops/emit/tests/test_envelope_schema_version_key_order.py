"""
Pins `envelope.py`'s dict-literal key order: `schema_version` first, `emitted_at` second.

`publish_envelope.py`'s bounded head-scan depends on this ordering holding -- it is NOT
enforced by anything else in `envelope.py` itself (a plain dict literal + CPython insertion
order, no `sort_keys` on the writer). This is a one-line negative test, not a guard: it fails
if a future edit to `envelope.py`'s dict literal reorders these two keys, which is exactly
the drift `publish_envelope.py`'s scan is defenseless against without this pin.
"""

from __future__ import annotations

import inspect
import re

from coordinator_core.ops.emit import envelope


def test_schema_version_is_the_first_key_and_emitted_at_the_second():
    source = inspect.getsource(envelope)
    match = re.search(r'return\s*\{\s*"schema_version":.*?"emitted_at":', source, re.DOTALL)
    assert match is not None, (
        "envelope.py's envelope dict literal no longer opens with "
        '"schema_version" immediately followed by "emitted_at" -- '
        "publish_envelope.py's bounded head-scan relies on this ordering."
    )
