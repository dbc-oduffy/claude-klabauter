"""Regression guard for the C2 consolidation (docs/plans/2026-09-02-state-
keeps-the-work-not-the-machinery.md): no module outside `machinery_paths.py`
itself may hand-build a machinery-bucket path join by re-spelling one of its
bucket segments (``"subagent-share"``, the twenty-one-plus-seven sites this
chunk repointed) as a string literal used in a `Path(...) / ...` or
`os.path.join(...)` expression.

SCOPE. Running this grep over the whole `coordinator_core/` tree is not this
chunk's job: dozens of files this plan has not yet touched -- C4's
`extract_cited_sidecars.py`, `write_guards/*.py`, and a long tail of
unrelated test fixtures that build their OWN synthetic `state/subagent-
share/...` layout to test unrelated modules -- pre-date this consolidation
and are each some OTHER chunk's (or no chunk's) responsibility to repoint.
Scanning them here would fail this guard on work this chunk was explicitly
told not to do (see its own `writes:` scope), and `bash_guards/` in
particular is a DIFFERENT trust boundary carved out to this plan's C3 on
purpose ("Move the trust boundary deliberately, with the guards' own
tests").

So this guard polices exactly the site set this chunk is accountable for --
the 21+7 sites C2 repointed (this file's own `_GUARDED_FILES`) -- for a hand-
built two-segment `"state"` + `"subagent-share"` join reappearing at any of
them. A corpus-wide sweep is a DIFFERENT, larger guard some future chunk may
add; this one is scoped to not regress the specific repoint this chunk did.

Negative-spec:
    - Does NOT assert every prose mention of "subagent-share" is gone from
      `coordinator_core/` -- see SCOPE above.
    - Does NOT police `bash_guards/`, `write_guards/`, or any file this
      chunk's own `writes:` list does not name.
"""

from __future__ import annotations

import os
import re

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCAN_ROOT = os.path.join(_REPO_ROOT, "coordinator_core")

#: Exactly the sites C2 repointed (its own `writes:` list, minus the deleted
#: `session/subagent_share.py` and this guard's own new test file) plus
#: `session/machinery_paths.py` itself -- the one file allowed to spell the
#: two segments together.
_GUARDED_RELATIVE_FILES = [
    "group_em/baseline.py",
    "guard_advisory_counter.py",
    "registry_fallback_counter.py",
    "hooks/cater_subagent_start.py",
    "hooks/stop_dispatch.py",
    "ops/audience_mismatch_scan.py",
    "ops/completion_ops.py",
    "ops/fold_execution_record.py",
    "ops/verify_coverage.py",
    "review_trail/receipt_credit.py",
    "subagent_sandbox/detect_unfilled_sidecar.py",
    "subagent_sandbox/harvest_exit_interviews.py",
    "subagent_sandbox/provision_report.py",
    "workstream_complete/directives_commit_tail.py",
    "session/machinery_paths.py",
    "group_em/obligations.py",
    "group_em/send_pass.py",
    "group_em/teammates.py",
    "group_em/tests/test_send_pass.py",
    "hooks/watchdog_undischarged_next_move.py",
    "ops/tests/test_stopped_peer_reaches_gem.py",
]

_EXCLUDED_FILES = {
    os.path.join(_SCAN_ROOT, "session", "machinery_paths.py"),
}

#: A hand-built join: the OLD two-segment spelling -- literal `"state"`
#: immediately followed by literal `"subagent-share"` as adjacent path-join
#: arguments, whether chained with `/` (`Path(...) / "state" / "subagent-
#: share"`) or passed positionally (`os.path.join(..., "state",
#: "subagent-share", ...)`). A caller routed correctly through
#: `machinery_root()`/`share_dir()` never spells `"state"` next to
#: `"subagent-share"` -- only a hand-built join does, which is exactly what
#: distinguishes a regression from `machinery_paths.py`'s own sanctioned
#: `os.path.join(machinery_root(repo_root), "subagent-share")` shape (no
#: `"state"` literal in that expression at all).
_JOIN_PATTERN = re.compile(
    r'["\']state["\']\s*[,/]\s*["\']subagent-share["\']'
)


def _iter_py_files():
    for rel in _GUARDED_RELATIVE_FILES:
        fpath = os.path.join(_SCAN_ROOT, rel.replace("/", os.sep))
        if fpath in _EXCLUDED_FILES:
            continue
        if os.path.isfile(fpath):
            yield fpath


def test_no_hand_built_subagent_share_joins_outside_machinery_paths():
    hits = []
    for fpath in _iter_py_files():
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        if _JOIN_PATTERN.search(text):
            hits.append(os.path.relpath(fpath, _REPO_ROOT).replace(os.sep, "/"))
    assert not hits, (
        "hand-built 'subagent-share' path join(s) found outside "
        f"session/machinery_paths.py: {sorted(hits)}"
    )
