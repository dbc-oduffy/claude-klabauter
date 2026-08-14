"""
test_verify_superseded_retirement — regression guard for the 2026-06-26
`superseded` handoff-status retirement.

Port of: verify-superseded-retirement.sh (DoE 7cca4d4c, 2026-07-16)
(T6 disposition (a): rewrite-as-pytest — the invariant it guards, no live
`HandoffSummary` record emits `status: superseded`, is a still-live contract
rule, not dead bash machinery).

AC4/AC5 (grep-only ACs the original DoE plan table covers via inline greps,
not this dedicated script) are out of scope here — only ac1/ac2/ac3/ac6 (the
four the original `.sh` covers) are ported.

Spec backlink: docs/plans/2026-06-26-retire-superseded-handoff-status.md

Retiring-ruling backlink: 2026-08-02 fast-tier stale-test triage
(tasks/mise-verify/triage-C-cockpit.md § test_verify_superseded_retirement.py,
AC2). AC2 used to also assert `coordinator/CLAUDE.md` (DoE-side) exists and
doesn't list the retired status vocabulary. That file was deliberately
retired: DoE commit `e8f9051db` ("C10/C11: finish the coordinator/CLAUDE.md
retirement and re-point every citation") deleted it, ratified in an earlier
`0c90dc50d` ("C2+C3: rewrite global doctrine in system terms; retire
coordinator/CLAUDE.md as a doctrine surface"). Its content was NOT relocated
to one successor file — `e8f9051db`'s own diff dispositions each of its
sections independently (mechanize / re-route / state-in-full / delete /
accept-unenforced), and a live check of DoE's current root `CLAUDE.md` and
`global-doctrine/CLAUDE.md` confirms neither carries any handoff-status
content today (grep -in for "handoff" or "status:" against both: zero
doctrine hits). AC2's actual guarantee — no handoff WRITER surface prescribes
`status: superseded` — is still fully covered by the three writer surfaces
below that were never retired: `docs/wiki/spinoff-handoffs.md`,
`skills/handoff/SKILL.md`, and `schemas/handoff.schema.json`. The retired
`coordinator/CLAUDE.md` check is dropped rather than repointed at a guessed
successor, since no successor exists to point at.
"""
from __future__ import annotations

import json
import re
import subprocess

from coordinator_core.contract.cockpit_schema.tests.conftest import (
    COCKPIT_CONTRACT_DIR,
    DOE_CLONE,
    skip_no_doe,
)

# The original `verify-superseded-retirement.sh` derives `CC` (the
# coordinator dir) from ITS OWN location: `test/../..` relative to
# `coordinator/cockpit-contract/test/` resolves to `coordinator/`, NOT the
# repo root — every path below is `coordinator/`-relative, not
# `DOE_CLONE`-relative directly. Guarded (not evaluated at collection time
# when DoE is absent) since @skip_no_doe only fires inside the test body.
def _coordinator_dir():
    return COCKPIT_CONTRACT_DIR.parent


@skip_no_doe
def test_ac1_no_handoff_record_carries_status_superseded():
    """No handoff record (live or archived) carries status: superseded."""
    result = subprocess.run(
        ["grep", "-rl", "^status: superseded", "state/handoffs/", "archive/handoffs/"],
        cwd=DOE_CLONE,
        capture_output=True,
        text=True,
    )
    # grep exit 1 == no matches (pass); exit 0 with output == a violator found.
    assert result.returncode != 0, (
        f"a handoff record still carries 'status: superseded':\n{result.stdout}"
    )


@skip_no_doe
def test_ac2_no_handoff_writer_surface_prescribes_status_superseded():
    """No handoff WRITER surface prescribes status: superseded.

    `coordinator/CLAUDE.md` (DoE-side) was one of the four surfaces this AC
    originally checked; it was deliberately retired (DoE `e8f9051db`, ratified
    in `0c90dc50d`) with no single successor file inheriting its content — see
    module docstring. Dropped from this checklist rather than repointed at a
    guessed replacement.
    """
    cc = _coordinator_dir()
    spinoff_handoffs = cc / "docs" / "wiki" / "spinoff-handoffs.md"
    handoff_skill = cc / "skills" / "handoff" / "SKILL.md"
    handoff_schema = cc / "schemas" / "handoff.schema.json"

    for p in (spinoff_handoffs, handoff_skill, handoff_schema):
        assert p.is_file(), f"missing file: {p}"

    assert "active | consumed | superseded" not in spinoff_handoffs.read_text(), (
        "spinoff-handoffs.md still lists the superseded enum value"
    )
    assert "status: superseded" not in handoff_skill.read_text(), (
        "skills/handoff/SKILL.md still prescribes status: superseded"
    )

    # AC2's subject is the RETIREMENT of `superseded`, not the enum's exact membership.
    # Exact-equality against [active, consumed] made this assertion break on any
    # legitimate widen — it fired on DR-084's dual-vocabulary P0 widen (DoE e75c6c7a:
    # status += open/claimed), which the vocabulary-overhaul plan mandates. Assert the
    # retirement itself so the guarantee survives the widened window and the P4 narrow.
    handoff_schema_doc = json.loads(handoff_schema.read_text())
    status_enum = handoff_schema_doc["properties"]["status"]["enum"]
    assert "superseded" not in status_enum, (
        f"handoff.schema.json status enum still carries the retired value: {status_enum}"
    )


@skip_no_doe
def test_ac3_shared_token_safety_protected_rows_preserved():
    """Shared-token safety: 3 protected rows (memo/plan/decision) preserved,
    handoff row removed."""
    shapes_doc = _coordinator_dir() / "docs" / "wiki" / "canonical-artifact-shapes.md"
    assert shapes_doc.is_file(), f"missing file: {shapes_doc}"

    text = shapes_doc.read_text()
    n = len(re.findall(r"^\| (memo|plan|decision) \| status \| superseded", text, re.MULTILINE))
    assert n == 3, f"expected 3 memo/plan/decision superseded rows, found {n}"

    assert not re.search(r"^\| handoff \| status \| superseded", text, re.MULTILINE), (
        "handoff DONE row still present"
    )


@skip_no_doe
def test_ac6_ts_zod_mirror_retirement_complete_7cca4d4c():
    """AC6 FLIPPED (2026-07-21): assert the Zod/TS mirror retirement is COMPLETE, not its
    (now-impossible) content.

    AC6 originally asserted: enum narrowed in the Zod SOURCE (`src/entities/summaries.ts`),
    writer schema narrowed, reader schema deliberately tolerant, and schema-bundle version ==
    the Zod source's `CONTRACT_VERSION`. Every one of those assertions READ the TS/Zod mirror
    (`src/`) — DoE commit `7cca4d4c` (2026-07-16) deleted that mirror wholesale in favor of
    claude-klabauter's pydantic emitter as sanctioned canonical (see
    `cross-repo/archive/2026-07-16-claude-central-em-cockpit-{emitter-ownership,crossrepo-write-and-emitter-dependency}.md`).
    A test that re-parses a deleted tree cannot run — the ORIGINAL `@skip_no_ts_mirror` guard
    correctly kept it from erroring, but a permanently-skipped test is a permanently-blind
    test: this AC's purpose (retirement-tracking) is better served by asserting the retirement
    ITSELF held, not by skipping forever.

    This function therefore no longer reads `src/entities/summaries.ts` / `src/index.ts` at
    all — it asserts `cockpit-contract/src/` stays ABSENT. If a TS/Zod mirror is ever
    reintroduced, this test fails loud (not silently skips), which is the correct signal to
    restore the original content assertions rather than to widen this absence check.
    """
    cc = _coordinator_dir()
    ts_mirror_dir = cc / "cockpit-contract" / "src"
    assert not ts_mirror_dir.exists(), (
        f"TS/Zod mirror {ts_mirror_dir} is present — expected retired since DoE 7cca4d4c "
        "(2026-07-16). If a mirror was deliberately reintroduced, this AC's original "
        "content assertions (enum narrowing, schema/version parity read from the Zod "
        "source) need to be restored, not this absence check."
    )
