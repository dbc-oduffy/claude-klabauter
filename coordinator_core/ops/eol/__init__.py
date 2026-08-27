"""coordinator_core.ops.eol -- declared-vs-actual line-ending drift detection
and repair for any caller-supplied `target_root`.

ONE op, not a family. `eol.repair` in `repair.py` is the single registered id;
it runs the census itself (`census.census`, imported and called on every run,
`mutate: false` being what used to be reached as `eol.census`). The `eol.census`
and `eol.audit_producers` op ids are K-062 gravestones -- `audit_producers.py`
is deleted, and `census.py` is now `repair.py`'s library, not a registered op.
Do not re-decorate it: three ids over one mechanism is what the collapse
removed. → `state/kill-ledger.md` § K-062.

Package marker only. `repair.py` self-registers via `register_op` on import;
`coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES` table is what makes
that import happen eagerly (C5's registration quad), not this file.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md
"""

from __future__ import annotations
