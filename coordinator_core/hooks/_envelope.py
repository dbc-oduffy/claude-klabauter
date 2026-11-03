
from __future__ import annotations

import os

# Hook entry caps BLAS pools before any numpy-importing module loads (process-local setdefault;
# keys mirror spawn_depth.BLAS_THREAD_KEYS, pinned by test_engine_blas_thread_cap).
for _key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_key, "1")

from coordinator_core._hook_envelope import (  # noqa: E402
    allow_advisory,
    context_only,
    deny,
    no_advisory,
    payload_of,
    post_advisory,
    rewrite_input,
)

__all__ = [
    "allow_advisory",
    "context_only",
    "deny",
    "no_advisory",
    "payload_of",
    "post_advisory",
    "rewrite_input",
]
