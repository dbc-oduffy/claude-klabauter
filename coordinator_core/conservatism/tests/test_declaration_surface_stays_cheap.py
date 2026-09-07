"""
The declaration surface must stay cheap enough for a guard hot path.

The first draft of `coordinator_core.conservatism` used `@dataclass(frozen=True)`
and `typing`, measured 13.8ms to import against 1.1ms of its own body:
`dataclasses` drags `inspect`. Both were removed. Nothing stops a later author
reaching for `@dataclass` again except this test.

`inspect` is named separately from `dataclasses` because it is the actual cost
and several other conveniences reach it by a different road.

The measurement is a DELTA over `coordinator_core`'s own package import, which
already drags `typing` for reasons that predate this module and are not this
baton's to fix. Pinning the absolute set would make this test a report on the
parent package instead of on the surface it is guarding.

Negative-spec: this pins the DECLARATION surface only. `conservatism.verify`
is imported by tests exclusively and may cost whatever it likes.
"""

from __future__ import annotations

import subprocess
import sys

_FORBIDDEN = ("dataclasses", "inspect", "typing")


def test_declaration_surface_drags_no_expensive_stdlib_import():
    probe = (
        "import sys;"
        "import coordinator_core;"
        "baseline = set(sys.modules);"
        "import coordinator_core.conservatism;"
        f"print(','.join(sorted(m for m in {_FORBIDDEN!r} "
        "if m in sys.modules and m not in baseline)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    dragged = [m for m in result.stdout.strip().split(",") if m]
    assert not dragged, (
        f"coordinator_core.conservatism now drags {dragged} in on top of coordinator_core. "
        "Measured cost when this pin was written: dataclasses 9.3ms (it pulls inspect), "
        "typing 2.4ms, against ~1ms for the module's own body. Use __slots__ and string "
        "annotations instead, or move the convenience into conservatism.verify."
    )
