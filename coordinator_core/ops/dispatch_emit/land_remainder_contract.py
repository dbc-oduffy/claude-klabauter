"""Contract of ``dispatch.land_verified_remainder``: op name, request and reply keys, refusal codes, and the
signatures the lane's legs implement.

Imports nothing beyond the stdlib so a caller can read the contract without loading a leg module.
Implemented by ``land_remainder_writeset`` (refuse_out_of_write_set, dirty_declared),
``land_remainder_verify`` (verify_remainder) and ``land_remainder`` (the op body).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Protocol

#: The literal ``land_remainder`` registers under; ``@register_op`` spells it out and a test pins the two equal.
OP_NAME: Final = "dispatch.land_verified_remainder"

#: Module that registers ``OP_NAME`` (the op body).
OP_MODULE: Final = "coordinator_core.ops.dispatch_emit.land_remainder"

#: Keys the caller supplies. The op body also reads optional ``paths``, ``run_token`` and ``script_path``;
#: ``worktree_root`` is resolved by the dispatcher, not read by the op body.
REQUEST_KEYS: Final = frozenset({"run_id", "worktree_root"})

#: Keys a caller reads from a reply. Refusal replies add detail keys that are not listed.
REPLY_KEYS: Final = frozenset({"landed", "sha", "refused", "omitted", "closure_open", "retest"})

REFUSED_OUT_OF_WRITE_SET: Final = "out-of-write-set"
REFUSED_RETEST_FAILED: Final = "retest-failed"
REFUSED_IMPORT_CLOSURE_OPEN: Final = "import-closure-open"
REFUSED_NOT_HALTED: Final = "not-halted"
REFUSED_NO_BASE_EXPORT: Final = "no-base-export"
#: No child verdict exists for the current on-disk bytes; the op spawned the child.
REFUSED_RETEST_PENDING: Final = "retest-pending"

#: Codes the verify and write-set legs produce. The op body adds ``run-id-unsafe``, ``run-token``,
#: ``no-write-set``, ``dirty-check-failed``, ``commit-refused`` and ``no-repo-root``, which are not listed here.
REFUSAL_CODES: Final = frozenset({
    REFUSED_OUT_OF_WRITE_SET,
    REFUSED_RETEST_FAILED,
    REFUSED_IMPORT_CLOSURE_OPEN,
    REFUSED_NOT_HALTED,
    REFUSED_NO_BASE_EXPORT,
    REFUSED_RETEST_PENDING,
})


class RefuseOutOfWriteSet(Protocol):
    """Return the requested ``paths`` that fall outside the run's declared-write union (empty when all are in)."""

    def __call__(self, root: Path, run_id: str, paths: list[str]) -> list[str]: ...


class DirtyDeclared(Protocol):
    """Return ``(to_stage, omitted)``: declared dirty paths to stage, and ``(path, reason)`` pairs left out.

    Stages nothing itself; the commit leg stages ``to_stage`` and returns ``omitted`` as ``reply["omitted"]``.
    """

    def __call__(self, root: Path, run_id: str) -> tuple[list[str], list[tuple[str, str]]]: ...


class VerifyRemainder(Protocol):
    """Return the retest verdict dict for ``paths`` (keys among ``REPLY_KEYS``: ``refused``, ``closure_open``,
    ``retest``); runs the export retest in a detached child, never inline.
    """

    def __call__(self, root: Path, run_id: str, paths: list[str]) -> dict: ...
