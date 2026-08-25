"""coordinator_core.ops.eol -- the `eol` op family (census, repair,
audit_producers): declared-vs-actual line-ending drift detection and repair
for any caller-supplied `target_root`.

Package marker only. Each op module self-registers via `register_op` on
import (see `coordinator_core/ops/eol/census.py`, `audit_producers.py`);
`coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES` table is what makes
that import happen eagerly (C5's registration quad), not this file.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md
"""

from __future__ import annotations
