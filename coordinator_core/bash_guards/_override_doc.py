"""coordinator_core.bash_guards._override_doc -- the override-doc pointer
constants, extracted to a leaf module with no imports of its own.

Split out of ``_helpers.py`` (2026-08-25, "the commit gate stops importing a
subsystem") so a caller needing only these two constants -- e.g.
``coordinator_core.ops.detect_staged_rollback``, which sits on the commit
pre-commit hook path -- does not have to import ``_helpers.py`` and, through
it, the wider ``bash_guards``/``subagent_sandbox`` import graph. This module
holds ONLY the two constants below, verbatim from ``_helpers.py``, and
imports nothing itself so importing it can never pull in anything beyond the
Python standard library.

``_helpers.py`` imports both names FROM this leaf and re-exports them --
every existing importer of ``_helpers.OVERRIDE_KEYS_DOC``/
``_helpers.OVERRIDE_KEYS_DOC_DISPLAY`` keeps working unmodified.
"""

from __future__ import annotations

#: Reference doc carrying the full bypass-options content this function used
#: to inline on every firing (the two relayable routes, the CONFINEMENT_DENY
#: caveat, the pre-launch-only env-var constraint, and the generated
#: enumeration of every COORDINATOR_OVERRIDE_*/COORDINATOR_ALLOW_* key). This
#: module never reads the file, it only names it.
#:
#: Repo-root-relative. This is the RESOLUTION form -- what a caller joins to a
#: repo root to find the file on disk (the retention suite does exactly that).
OVERRIDE_KEYS_DOC = "docs/reference/guard-override-keys.md"

#: The DISPLAY form -- a repo-qualified hint, e.g.
#: ``"claude-klabauter docs/reference/guard-override-keys.md"``. Not
#: interchangeable with ``OVERRIDE_KEYS_DOC`` above: that constant is the
#: file-resolution form a caller joins to a repo root, this is what a reader
#: of a guard MESSAGE sees.
#:
#: These guards are not claude-klabauter-local: DoE's PreToolUse shim resolves this
#: engine and runs the guard logic in-process for EVERY repo on the machine,
#: so the reader of this pointer is usually sitting in some other repo's
#: tree, where a bare `docs/reference/...` resolves to nothing. Naming the
#: repo matches the convention CLAUDE.md already uses for cross-repo
#: citations ("DoE-claude coordinator/docs/wiki/..."). NEGATIVE SPEC: do not
#: collapse these two constants back into one -- the file-resolution caller
#: and the message reader need different strings.
#:
#: 2026-08-05 (PM-raised, break-class): this used to be only the FALLBACK,
#: with a resolver (``_resolve_override_keys_doc_display``, since reduced to
#: a trivial wrapper below) preferring an absolute, in-process-resolved path
#: instead. That absolute form was wrong on two independent axes at once:
#: it interpolated this operator's home directory and repo name into every
#: guard message the suite emits (a machine-path leak matching the class
#: ``check-machine-path-leak.py`` exists for, though that checker's scope is
#: `settings.json`/`working-repos.yaml` only -- it never scanned
#: runtime-rendered guard text, which is why it missed this), AND it only
#: ever resolved correctly in the SAME process that rendered it -- wrong
#: shape on Windows (a POSIX-joined path there is meaningless) and wrong for
#: any other username or checkout location, i.e. every machine but the one
#: that produced it. A pointer that does not resolve for a stranger on a
#: different OS is not "correct but sensitive", it is simply broken, and
#: this repo ships as an OSS mirror -- every downstream reader would have
#: hit that. The repo-qualified relative form is the only one of the two
#: that is portable AND leaks nothing, so it is now unconditional.
OVERRIDE_KEYS_DOC_DISPLAY = "claude-klabauter " + OVERRIDE_KEYS_DOC
