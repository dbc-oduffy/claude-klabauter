"""The launcher <-> run-script seam for a multi-ask fan-out.

A launcher script starts each emitted run as a nested workflow child. Nesting is one level deep,
so a child never calls ``workflow()``; the launcher, being top-level, performs the stage read for
it and hands the verified text back through these three keys. Both continuation routes (re-entry
and split script) use the same keys. Pure constants: no I/O, no imports.
"""

# Fire-args key the launcher sets true on every child it starts.
LAUNCHER_CHILD_ARG = "launcherChild"

# A launcher child's return when it reaches a stage fold: {handle, ...}.
STAGE_PENDING_KEY = "stage_pending"

# Args key the launcher hands back on re-start: {handle, text}; the run verifies text against handle.
STAGED_MANIFEST_ARG = "stagedManifest"
