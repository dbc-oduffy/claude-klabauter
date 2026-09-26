"""coordinator_core.bash_guards._override_doc -- the override-doc pointer
constants, extracted to a leaf module with no imports of its own.

Split out of ``_helpers.py`` (2026-08-25, "the commit gate stops importing a
subsystem") so a caller needing only these two constants -- originally
``coordinator_core.ops.detect_staged_rollback``, which sat on the commit
pre-commit hook path -- would not have to import ``_helpers.py`` and, through
it, the wider ``bash_guards``/``subagent_sandbox`` import graph. That caller
is gone (deleted 2026-08-25, "the staged rollback gate dies without blocking
a commit"; claude-klabauter ends with no pre-commit hook), but the leaf split still
stands: this module holds ONLY the two constants below, verbatim from
``_helpers.py``, and imports nothing itself so importing it can never pull in
anything beyond the Python standard library.

``_helpers.py`` imports both names FROM this leaf and re-exports them --
every existing importer of ``_helpers.OVERRIDE_KEYS_DOC``/
``_helpers.OVERRIDE_KEYS_DOC_DISPLAY`` keeps working unmodified.
"""

from __future__ import annotations

#: to inline on every firing (the two relayable routes, the CONFINEMENT_DENY
#: enumeration of every COORDINATOR_OVERRIDE_*/COORDINATOR_ALLOW_* key). This
#: Repo-root-relative. This is the RESOLUTION form -- what a caller joins to a
OVERRIDE_KEYS_DOC = "docs/reference/guard-override-keys.md"

#: ``session/guard_unlock_sentinel.py :: _SETTINGS_ROOT_WIKI_POINTER`` as
#: module's note at ``_SENTINEL_PREFIX``. Not interchangeable with
#: ``OVERRIDE_KEYS_DOC`` above: that
#: this repo. NEGATIVE SPEC: do not collapse these two constants back into
#: 2026-08-05 (PM-raised, break-class): this used to be only the FALLBACK,
OVERRIDE_KEYS_DOC_DISPLAY = (
    "~/.coordinator-claude-settings/coordinator-claude/docs/wiki/guard-override-keys.md"
)
