"""Interface of the rebuilt run landing behind `dispatch.terminal_commit`.

`land_run` is the product-landing branch only; the halted, fanout, grind and no-script modes stay on
their own modules. Invariants it must discharge: `docs/reference/run-landing-invariants.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

#: Settings-home key `dispatch.terminal_commit` reads to choose the landing path. Defined once: the
#: cutover switch and the benchmark driver both import it.
LANDING_IMPL_KEY = "terminal_commit_landing"
IMPL_OLD = "old"
IMPL_NEW = "new"
LANDING_IMPLS = (IMPL_OLD, IMPL_NEW)
#: A missing or unrecognised key value selects this path until cutover.
LANDING_IMPL_DEFAULT = IMPL_OLD


class LandRunNotBuilt(NotImplementedError):
    """`land_run` has no body yet."""


def land_run(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land one run's product commit; returns the reply dict `dispatch.terminal_commit` returns.

    `params` are the op's params; `repo_root` is the git common dir, as the op handler receives it.
    """
    raise LandRunNotBuilt("run_landing.land_run is the C1 interface stub; C3 builds the body")
