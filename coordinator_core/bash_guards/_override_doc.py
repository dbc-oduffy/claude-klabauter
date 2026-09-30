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

#: Reference doc carrying the full bypass-options content this function used
#: to inline on every firing (the two relayable routes, the CONFINEMENT_DENY
#: caveat, the pre-launch-only env-var constraint, and the generated
#: enumeration of every COORDINATOR_OVERRIDE_*/COORDINATOR_ALLOW_* key). This
#: module never reads the file, it only names it.
#:
#: Repo-root-relative. This is the RESOLUTION form -- what a caller joins to a
#: repo root to find the file on disk (the retention suite does exactly that).
OVERRIDE_KEYS_DOC = "docs/reference/guard-override-keys.md"

#: The DISPLAY form -- DR-290 form 2, the literal, never-expanded
#: settings-root pointer. THIS constant is the canonical carrier of that
#: shape (2026-09-03): the comment here used to defer to
#: ``session/guard_unlock_sentinel.py :: _SETTINGS_ROOT_WIKI_POINTER`` as
#: the shape that "already ships", but that constant went unread when its
#: module stopped rendering doc pointers and has been removed -- see that
#: module's note at ``_SENTINEL_PREFIX``. Not interchangeable with
#: ``OVERRIDE_KEYS_DOC`` above: that
#: constant is the file-resolution form a caller joins to a repo root, this
#: is what a reader of a guard MESSAGE sees.
#:
#: These guards are not claude-klabauter-local: DoE's PreToolUse shim resolves this
#: engine and runs the guard logic in-process for EVERY repo on the machine,
#: so the reader of this pointer is usually sitting in some other repo's
#: tree, where a bare `docs/reference/...` resolves to nothing, and where a
#: repo-qualified relative form (DR-290 form 1, this constant's prior value)
#: names a repo the reader may have neither checked out nor heard of --
#: exactly the foreign-repo-identity leak
#: `docs/plans/2026-08-30-the-engine-stops-naming-its-own-repo.md` exists to
#: close. The settings-root form resolves for every reader, including one
#: with none of our repos checked out, because it points at the one
#: location every coordinator install populates regardless of which repo a
#: session happens to be working in: `coordinator_core/install/substrate.py
#: :: _install_seed_wikis`'s claude-klabauter-sourced sibling leg copies this file's
#: named page (`docs/reference/guard-override-keys.md`) to
#: `<settings-home>/coordinator-claude/docs/wiki/guard-override-keys.md` at
#: install time, so the pointer below resolves post-install without naming
#: this repo. NEGATIVE SPEC: do not collapse these two constants back into
#: one -- the file-resolution caller and the message reader need different
#: strings. Left as ``~/...`` literally -- never expanded via
#: ``Path.home()``/``os.path.expanduser``: expansion would reintroduce the
#: machine-specific leak this form exists to avoid.
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
#: hit that.
#:
#: 2026-08-30 (DR-290 form 1 -> form 2, `the engine stops naming its own
#: repo` plan, C2): the repo-qualified relative form above was itself still
#: a foreign-repo-identity leak for a session working in neither
#: claude-klabauter nor its publish mirror -- it named `claude-klabauter`
#: unconditionally. Moved to DR-290 form 2 (the literal settings-root
#: pointer) instead, with the install leg (`_install_seed_wikis`'s
#: claude-klabauter-sourced sibling) landing in the same commit so the pointer
#: resolves rather than trading one broken pointer for another.
OVERRIDE_KEYS_DOC_DISPLAY = (
    "~/.coordinator-claude-settings/coordinator-claude/docs/wiki/guard-override-keys.md"
)
