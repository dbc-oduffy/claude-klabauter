"""
coordinator_core.plugin_health.tests.test_fleet_reachability

Coverage for the fleet-reachability delete-safety gate (see
fleet_reachability.py's own module docstring for the `c79e66cd` regression
this closes: claude-klabauter deleted `lint-frontmatter.js` while a DoE-claude skill
still cited `bin/lint-frontmatter`).

Every scenario uses tmp_path fixtures standing in for claude-klabauter's
coordinator/bin/ and DoE-claude's coordinator/{skills,commands,hooks,
pipelines} — never the operator's actual claude-klabauter/DoE-claude checkouts (see
`check_fleet_reachability`'s explicit-override params). The skip-masking
guard tests use monkeypatch instead of the real machine-local registry, so
this suite's outcome does not depend on whether `repos.doe_claude` happens
to be registered on the machine running it.

Spec backlink: pln-python-ize-claude-klabauter-bin-oracles--218413 D3
(original gate). 2026-07-27: `test_extra_oracle_dir_oracle_is_not_reported_missing` and
`test_extra_oracle_dirs_not_auto_populated_when_agent_bin_overridden` cover the
`<repo-root>/bin/` + `coordinator/lib/` scan-side widening (see commit 411f80ac
and fleet_reachability.py's own module docstring for the defect this closes).

2026-07-27 (commit b1bc5789's own follow-up, same day): every fixture below that
exercises a genuine positive match was updated from a bare `bin/<name>` citation to a
namespace-qualified one (`coordinator/bin/<name>` or `templates/bin/<name>`) —
see `fleet_reachability._is_namespace_qualified_citation`'s own docstring for why
bare citations no longer count as demand. `test_bare_citation_*_is_not_demand` (new)
and `test_excluded_file_class_citations_are_not_demand` (new) cover the two
structural filters that fix closes the remaining `check-fixture-sync` /
`coordinator-handoff-archive` / `ensure-coordinator-venv` false-positive class.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.plugin_health import fleet_reachability as fr


def _write_claude_klabauter_oracle(agent_bin: Path, filename: str) -> None:
    """A single file landing in claude-klabauter's coordinator/bin/ — the source
    `_derive_agent_helper_target_map` scans. `filename` carries whatever
    extension (or none) the real on-disk CLI would."""
    agent_bin.mkdir(parents=True, exist_ok=True)
    (agent_bin / filename).write_text("#!/usr/bin/env python3\nprint('hi')\n")


def _write_doe_fence(doe_root: Path, subdir: str, filename: str, body: str) -> None:
    """`filename` may itself be a nested relative path (e.g.
    `tests/some-doc.md`) to stand in for a real DoE sub-subdirectory -- the
    parent of the FINAL target path is created, not just `d`, so a nested
    `filename` lands correctly instead of raising on a missing intermediate
    directory."""
    d = doe_root / "coordinator" / subdir
    target = d / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_clean_no_missing_qualified_and_extensioned(tmp_path: Path):
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "foo.py")
    _write_claude_klabauter_oracle(agent_bin, "bar.js")
    _write_doe_fence(doe_root, "skills", "SKILL.md", "Run `coordinator/bin/foo` then `coordinator/bin/bar.js`.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.skipped is False
    assert result.missing == []


def test_regression_fixture_c79e66cd_shape(tmp_path: Path):
    """The exact break shape: claude-klabauter's coordinator/bin/ has no
    lint-frontmatter oracle in any form, but a DoE-claude skill still cites
    `coordinator/bin/lint-frontmatter` — the gate MUST fail loud, not warn."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "query-records.js")
    _write_doe_fence(
        doe_root,
        "skills",
        "handoff-SKILL.md",
        "Validate with `coordinator/bin/lint-frontmatter.js --file \"$HANDOFF_FILE\"`.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is False
    assert result.skipped is False
    assert result.missing == ["lint-frontmatter"]


def test_extension_normalization_qualified_citation_matches_js_oracle(tmp_path: Path):
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "query-records.js")
    _write_doe_fence(doe_root, "commands", "workday-start.md", "Run two `coordinator/bin/query-records` calls.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_non_oracle_subdir_and_placeholder_tokens_filtered(tmp_path: Path):
    """Review: code-reviewer (Finding 2) — the original fixture cited
    `bin/lib/schema.js` bare, so `_is_namespace_qualified_citation` dropped
    the match before execution ever reached `_NON_ORACLE_SUBDIR_NAMES` (this
    test's actual subject); it would have passed identically with that
    filter deleted. Namespace-qualified to `coordinator/bin/lib/schema.js`
    so the "lib" subdir-name filter is the thing actually proven here."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    _write_doe_fence(
        doe_root,
        "skills",
        "percolate-SKILL.md",
        "Every `coordinator/bin/...` CLI (`percolate-gate`, `publish`) is reached via "
        "`coordinator/bin/lib/schema.js` internals — no separate resolution needed.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    # "..." and "lib" (a subdir, not a CLI) must not surface as missing
    # oracles — only genuine bare-name citations count as demand, and this
    # fixture cites none.
    assert result.ok is True
    assert result.missing == []


def test_hooks_and_pipelines_dirs_are_swept(tmp_path: Path):
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "present.py")
    _write_doe_fence(doe_root, "hooks", "pre-commit.md", "Invokes `coordinator/bin/hook-only-tool`.")
    _write_doe_fence(doe_root, "pipelines", "deep-research.md", "Invokes `coordinator/bin/pipeline-only-tool`.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is False
    assert sorted(result.missing) == ["hook-only-tool", "pipeline-only-tool"]


def test_qualified_citation_trailing_period_does_not_swallow_punctuation(tmp_path: Path):
    """Review: code-reviewer (Finding 2) — a bare, unfenced citation at the
    end of a prose sentence (no space before the period) must not capture
    the sentence-terminating "." into the token; a prior version of
    `_DOE_BIN_TOKEN_RE` did, producing a spurious "missing" false-positive
    for a real, reachable oracle. Namespace-qualified (2026-07-27) so this
    stays a genuine demand under `_is_namespace_qualified_citation`."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "query-records.js")
    _write_doe_fence(doe_root, "skills", "SKILL.md", "See coordinator/bin/query-records.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_cmd_extension_normalizes_to_match_claude_klabauter_oracle(tmp_path: Path):
    """Review: code-reviewer (Finding 3) — a Windows-launcher `.cmd`
    citation must normalize to the same stem as the `.py` claude-klabauter oracle,
    not false-positive as missing. Namespace-qualified (2026-07-27)."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "query-records.py")
    _write_doe_fence(doe_root, "skills", "SKILL.md", "Windows: `coordinator/bin/query-records.cmd`.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_missing_doe_subdir_is_not_an_error(tmp_path: Path):
    """A leaner DoE checkout missing e.g. pipelines/ entirely must not raise
    — the sweep skips absent subdirs rather than failing."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"
    _write_claude_klabauter_oracle(agent_bin, "present.py")
    (doe_root / "coordinator" / "skills").mkdir(parents=True)

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True


def test_reserved_name_on_disk_is_not_reported_missing(tmp_path: Path):
    """2026-07-27 scan-side defect: `_derive_agent_helper_target_map` pops
    every `_AGENT_HELPER_RESERVED_NAMES` entry from its own returned mapping
    (they install via a different family, not the forwarder-generation
    path) — but a reserved name that genuinely EXISTS on disk in
    `coordinator/bin/` is still a real, reachable oracle for THIS gate's
    purposes. `machine-local` was the concrete instance: it lives at
    `coordinator/bin/machine-local` yet was reported missing before this
    fix restored disk-existence visibility independent of the
    forwarder-installability question."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "machine-local")
    _write_doe_fence(doe_root, "skills", "SKILL.md", "Run `coordinator/bin/machine-local get repos.foo`.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_extra_oracle_dir_oracle_is_not_reported_missing(tmp_path: Path):
    """2026-07-27 scan-side widening: the live set was `coordinator/bin/`-only,
    so a fleet-cited oracle living in claude-klabauter's `<repo-root>/bin/` or
    `coordinator/lib/` (e.g. `bin/claude-klabauter-doctor-probe.py`, `coordinator/lib/
    resolve-coordinator-clone.py`) was reported missing despite genuinely
    existing on disk. `extra_oracle_dirs` closes that gap without touching
    `agent_bin`'s own resolution — see `check_fleet_reachability`'s own
    docstring for why the two overrides are independent.

    Spec backlink: commit 411f80ac (scan-side miss this widening follows up
    on) + this dispatch's own fix.
    """
    agent_bin = tmp_path / "claude-klabauter-bin" / "coordinator" / "bin"
    repo_root_bin = tmp_path / "claude-klabauter-bin" / "bin"
    coordinator_lib = tmp_path / "claude-klabauter-bin" / "coordinator" / "lib"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(repo_root_bin, "claude-klabauter-doctor-probe.py")
    _write_claude_klabauter_oracle(coordinator_lib, "resolve-coordinator-clone.py")
    _write_doe_fence(
        doe_root,
        "commands",
        "workday-start.md",
        "Written by claude-klabauter's `coordinator/bin/claude-klabauter-doctor-probe.py`; reached via "
        "`templates/bin/resolve-coordinator-clone`.",
    )

    result = fr.check_fleet_reachability(
        agent_bin=agent_bin,
        extra_oracle_dirs=[repo_root_bin, coordinator_lib],
        doe_root=doe_root,
    )

    assert result.ok is True
    assert result.missing == []


def test_extra_oracle_dirs_not_auto_populated_when_agent_bin_overridden(tmp_path: Path):
    """Negative-spec companion to the test above: an explicit `agent_bin`
    override with NO `extra_oracle_dirs` override must NOT silently widen to
    this machine's real `<repo-root>/bin/`/`coordinator/lib/` contents —
    every pre-2026-07-27 fixture in this file relies on that isolation. A
    name only present in a sibling dir the caller did not pass stays
    genuinely missing."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_doe_fence(doe_root, "skills", "SKILL.md", "Run `coordinator/bin/claude-klabauter-doctor-probe.py` first.")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is False
    assert result.missing == ["claude-klabauter-doctor-probe"]


def test_bare_citation_with_no_qualifier_is_not_demand(tmp_path: Path):
    """2026-07-27 fix (commit b1bc5789's own follow-up): a BARE `bin/<name>`
    citation -- no `coordinator/`, `templates/`, or settings-home-forwarder
    -seam prefix -- is no longer treated as a demand on claude-klabauter's oracle
    surface. This is the real DoE-tree shape of `check-fixture-sync`
    (`workday-start.md`'s "Repos ... ship a `bin/check-fixture-sync.sh`" --
    a per-consumer-repo convention, never a claude-klabauter oracle): the oracle has
    NO on-disk entry anywhere, yet the gate must NOT fail, because the
    citation was never namespace-qualified."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_doe_fence(
        doe_root,
        "commands",
        "workday-start.md",
        "Repos with paired cross-repo writers ship a `bin/check-fixture-sync.sh` "
        "that byte-compares declared fixtures against sibling-repo copies.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_qualified_citation_of_a_truly_absent_oracle_still_fails(tmp_path: Path):
    """Negative-spec companion to the test above -- proves the
    namespace-qualification narrowing did not turn the gate into a no-op.
    A genuinely NAMESPACE-QUALIFIED citation of an oracle with no live
    claude-klabauter entry anywhere and no ledger explanation still fails loud."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_doe_fence(
        doe_root,
        "commands",
        "install.md",
        "Per DR-047, claude-klabauter owns `coordinator/bin/shell-init-guard.py`, already ported.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is False
    assert result.missing == ["shell-init-guard"]


def test_retired_artifact_backlink_bare_citation_is_not_demand(tmp_path: Path):
    """2026-07-27 finding: `ensure-coordinator-venv` and
    `coordinator-handoff-archive` are both DoE citing THEIR OWN retired
    artifact in a bare, backtick-fenced "no longer exists" / "formerly"
    backlink (`install.md`'s "`bin/ensure-coordinator-venv.sh` no longer
    exists"; `handoff-archival.md`'s "formerly `bin/coordinator-handoff-
    archive.sh <predecessor> --exclude <successor>`"). Both are bare (no
    namespace qualifier), so the namespace-qualification filter alone --
    without any prose interpretation of "no longer exists" / "formerly" --
    already excludes them from demand."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_doe_fence(
        doe_root,
        "commands",
        "install.md",
        "`bin/ensure-coordinator-venv.sh` no longer exists — it was deleted once "
        "venv provisioning was ported natively.",
    )
    _write_doe_fence(
        doe_root,
        "pipelines",
        "handoff-archival.md",
        "the guarded native call, formerly `bin/coordinator-handoff-archive.sh "
        "<predecessor> --exclude <successor>`, runs only on an install whose "
        "claude-klabauter seam is absent.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_excluded_file_class_citations_are_not_demand(tmp_path: Path):
    """2026-07-27 fix (file-class filter, commit b1bc5789's own follow-up):
    a NAMESPACE-QUALIFIED `bin/<name>` citation living inside a test
    /fixture directory, a CHANGELOG, or a `docs/plans/` working doc is
    still not live invocation surface -- see
    `_is_excluded_from_invocation_surface`'s own docstring for the real
    DoE-tree instance this closes
    (`coordinator/hooks/tests/block-destructive-rm.security-review.md`, a
    code-review artifact, not a hook DoE ever tells an agent to invoke)."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_doe_fence(
        doe_root,
        "hooks",
        "tests/block-destructive-rm.security-review.md",
        "Reviewer note: consider adding `coordinator/bin/hypothetical-reviewer-only-tool`.",
    )
    _write_doe_fence(
        doe_root,
        "commands",
        "CHANGELOG.md",
        "- Added `coordinator/bin/hypothetical-changelog-only-tool` in this release.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_review_named_skill_file_is_not_excluded(tmp_path: Path):
    """Negative-spec companion: the file-class filter must NOT match on a
    `review` substring in the filename/path -- DoE's own tree has genuine,
    live skill/command entrypoints named exactly that shape
    (`commands/parallel-code-review.md`, `commands/enrich-and-review.md`).
    A citation inside one of those must still register as demand."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_doe_fence(
        doe_root,
        "commands",
        "parallel-code-review.md",
        "Dispatches reviewers via `coordinator/bin/parallel-review-gate-decision`.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is False
    assert result.missing == ["parallel-review-gate-decision"]


def test_shebang_and_system_path_citations_are_not_demand(tmp_path: Path):
    """2026-07-27 regex-precision defect: `#!/bin/bash`, `#!/usr/bin/env
    python3`, and a bare `/bin/sh` prose mention all match `\\bbin/<name>`
    structurally identically to a genuine `coordinator/bin/<name>` fence
    citation -- but none of them cite a claude-klabauter oracle. Real DoE hits:
    install.md's bash-version-probe prose ("`#!/usr/bin/env bash`",
    "`/bin/bash`") and hook test fixtures' "#!/bin/sh" shebang literals."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    _write_doe_fence(
        doe_root,
        "commands",
        "install.md",
        "Resolves via `#!/usr/bin/env bash` — check the PATH-resolved bash, not `/bin/bash`. "
        "A stray `#!/bin/sh` shebang mention too.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_home_dir_bin_citations_are_not_demand(tmp_path: Path):
    """2026-07-27 finding: `$HOME/bin/scc` and `~/bin/scc` cite a
    THIRD-PARTY tool installed into the user's generic PATH bin directory
    (real DoE hit: install.md/workday-start.md's `scc`/`shellcheck` PATH
    probe), not a claude-klabauter `coordinator/bin/` oracle -- structurally
    indistinguishable from a real citation without recognizing the
    canonical `$HOME`/`~` home-directory markers."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    _write_doe_fence(
        doe_root,
        "commands",
        "install.md",
        'Probe for it on PATH, falling back to `$HOME/bin/scc` (also `~/bin/scc`).',
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_glob_wildcard_family_reference_is_not_demand(tmp_path: Path):
    """2026-07-27 finding: `bin/verify-*-sync.sh` / `bin/wsc-*.sh` /
    `bin/distill-*.py` are glob-family convention mentions ("everything
    matching this pattern"), not a citation of one specific oracle named
    literally "verify"/"wsc"/"distill" -- the token regex's char class
    (correctly) does not consume `*`, so it truncates the match down to the
    stem before the wildcard; without this filter that truncated stem
    reports as a phantom missing oracle.

    Review: code-reviewer (Finding 2) — the original fixture cited both
    globs bare, so `_is_namespace_qualified_citation` dropped each match
    before execution ever reached `_GLOB_TRUNCATION_RE` (this test's actual
    subject); it would have passed identically with that filter deleted.
    Namespace-qualified so the glob-truncation filter is the thing actually
    proven here."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    _write_doe_fence(
        doe_root,
        "commands",
        "update-docs.md",
        "Run every snippet-sync verifier (`coordinator/bin/verify-*-sync.sh` convention) and "
        "`coordinator/bin/wsc-*.sh`.",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_non_markdown_file_is_not_swept(tmp_path: Path):
    """2026-07-27 finding: a `.py` file's docstring/path-literal embedding
    `coordinator/bin/foo.sh` (real DoE hit:
    test_nudge_em_code_dispatch.py's own regression-guard docstring) is
    DoE-internal test scaffolding, never a fenced Markdown demand -- the
    sweep must not treat arbitrary Python source as a citation surface."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    doe_hooks = doe_root / "coordinator" / "hooks"
    doe_hooks.mkdir(parents=True)
    (doe_hooks / "test_something.py").write_text(
        '"""(coordinator/bin/foo.sh) must NOT be swallowed."""\n', encoding="utf-8"
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root)

    assert result.ok is True
    assert result.missing == []


def test_ledger_retired_entry_explains_a_missing_name(tmp_path: Path):
    """2026-07-27: a name with no live claude-klabauter oracle but a `"retired"`
    relocation-ledger entry is deliberately gone, not a fleet-reachability
    failure -- the natural join this gate's dispatch brief named as the
    likely right fix for a DoE-side stale citation of a retired artifact."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"
    ledger_path = tmp_path / "relocation-ledger.json"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    _write_doe_fence(doe_root, "commands", "install.md", "Reached via `coordinator/bin/resolve-coordinator-clone`.")
    ledger_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "disposition": "retired",
                        "old_repo": "claude_klabauter",
                        "old_path": "bin/resolve-coordinator-clone.sh",
                        "reason": "dissolved into the resolve-claude-klabauter-bin contract",
                        "retired_at": "2026-07-22",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root, ledger_path=ledger_path)

    assert result.ok is True
    assert result.missing == []


def test_ledger_moved_entry_explains_a_missing_name(tmp_path: Path):
    """A `"moved"` ledger entry (resolves under a different name/repo) is
    likewise not a fleet-reachability failure -- see module docstring's
    residual-blind-spots note on what this gate does and does not verify
    about a `"moved"` entry's destination."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"
    ledger_path = tmp_path / "relocation-ledger.json"

    _write_claude_klabauter_oracle(agent_bin, "unrelated.py")
    _write_doe_fence(doe_root, "commands", "install.md", "Reached via `coordinator/bin/old-name.sh`.")
    ledger_path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "disposition": "moved",
                        "old_repo": "coordinator-claude (DoE-claude)",
                        "old_path": "bin/old-name.sh",
                        "new_repo": "claude_klabauter",
                        "new_path": "coordinator/bin/new-name.py",
                        "new_runtime": "python3.11+",
                        "forwarder": "none",
                        "moved_at": "2026-07-22",
                        "moved_by_commit": "deadbeef",
                        "reason": "renamed during the port",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root, ledger_path=ledger_path)

    assert result.ok is True
    assert result.missing == []


def test_ledger_silent_on_name_reports_genuine_missing(tmp_path: Path):
    """A name with no live oracle AND no ledger entry at all is a genuine
    unmet fleet demand -- the ledger must not manufacture coverage it
    was never given; this stays FAIL, exactly the pre-ledger contract."""
    agent_bin = tmp_path / "claude-klabauter-bin"
    doe_root = tmp_path / "doe"
    ledger_path = tmp_path / "relocation-ledger.json"

    _write_doe_fence(doe_root, "commands", "install.md", "Reached via `coordinator/bin/genuinely-gone-tool`.")
    ledger_path.write_text(json.dumps({"entries": []}), encoding="utf-8")

    result = fr.check_fleet_reachability(agent_bin=agent_bin, doe_root=doe_root, ledger_path=ledger_path)

    assert result.ok is False
    assert result.missing == ["genuinely-gone-tool"]


def test_skip_when_claude_klabauter_root_unresolvable(tmp_path: Path, monkeypatch):
    """Claude-Klabauter-root resolution now lives in `plugin_health.oracle_surface`
    (see that module's own docstring — this is the single definition
    `bin_inventory_gate.py` also consumes), so the unresolvable-root case is
    patched at ITS `coordinator_claude_klabauter_root` import, not on `fr` directly."""
    doe_root = tmp_path / "doe"
    doe_root.mkdir()

    def _raise():
        raise RuntimeError("no claude-klabauter root")

    import coordinator_core.plugin_health.oracle_surface as oracle_surface

    monkeypatch.setattr(oracle_surface, "coordinator_claude_klabauter_root", _raise)

    result = fr.check_fleet_reachability(doe_root=doe_root)

    assert result.ok is True
    assert result.skipped is True


def test_skip_when_doe_root_unresolvable(tmp_path: Path, monkeypatch):
    agent_bin = tmp_path / "claude-klabauter-bin"
    agent_bin.mkdir()

    monkeypatch.setattr(fr, "read_doe_root_pointer", lambda: "")

    result = fr.check_fleet_reachability(agent_bin=agent_bin)

    assert result.ok is True
    assert result.skipped is True


def test_skip_masking_guard_noop_when_doe_claude_unregistered(monkeypatch):
    monkeypatch.setattr(fr, "registry_get", lambda key: None)
    # Must not raise, regardless of what check_fleet_reachability would do —
    # this persona (repos.doe_claude not registered) legitimately has
    # nothing to compare.
    fr.assert_registered_implies_no_skip()


def test_skip_masking_guard_raises_when_registered_but_gate_skipped(monkeypatch):
    monkeypatch.setattr(fr, "registry_get", lambda key: "/some/doe/root")
    monkeypatch.setattr(
        fr,
        "check_fleet_reachability",
        lambda **kwargs: fr.FleetReachabilityResult(ok=True, skipped=True, lines=["[skip] stub"]),
    )

    with pytest.raises(AssertionError):
        fr.assert_registered_implies_no_skip()


def test_skip_masking_guard_passes_when_registered_and_gate_ran(monkeypatch):
    monkeypatch.setattr(fr, "registry_get", lambda key: "/some/doe/root")
    monkeypatch.setattr(
        fr,
        "check_fleet_reachability",
        lambda **kwargs: fr.FleetReachabilityResult(ok=True, skipped=False, lines=["[ok] stub"]),
    )

    # Must not raise — the gate ran (skipped=False), so there is no
    # skip-masking to catch.
    fr.assert_registered_implies_no_skip()


@pytest.mark.real_home  # live-tree oracle: resolves repos.doe_claude via the machine-local
# registry, which the suite-root `_quarantine_real_home` autouse fixture would otherwise hide,
# turning this into an unconditional skip. Read-only (registry lookup + delete-time sweep, no
# writes), which is the marker's own sanctioned use per conftest.py's docstring.
def test_ci_no_silent_skip_on_this_machine_when_registered():
    """Live-tree smoke, gated on this machine's own registration state
    (never asserted content, only the skip flag, so it cannot flake against
    a concurrent chunk mutating coordinator/bin/ elsewhere in this repo)."""
    from coordinator_core.machine_resolver import registry_get as real_registry_get

    if not real_registry_get("repos.doe_claude"):
        pytest.skip("repos.doe_claude not registered on this machine")

    fr.assert_registered_implies_no_skip()


# Marked @pytest.mark.real_home as of 2026-07-27 (commit b1bc5789's own follow-up): the
# remaining three false positives from the scan-side-widening pass -- check-fixture-sync,
# coordinator-handoff-archive, ensure-coordinator-venv -- are now resolved and verified,
# not fabricated. Each was confirmed against DoE-claude's actual citing lines (not
# guessed) to be a citation this gate SHOULD NOT treat as demand: check-fixture-sync is
# a per-consumer-repo convention (`workday-start.md`'s "Repos ... ship a
# `bin/check-fixture-sync.sh`"), and ensure-coordinator-venv / coordinator-handoff-archive
# are both bare, backtick-fenced "no longer exists" / "formerly" backlinks to already-
# retired claude-klabauter artifacts. All three share one structural property that closes them
# without interpreting any of that prose: none is ever cited in a NAMESPACE-QUALIFIED
# form (`coordinator/bin/<name>`, `templates/bin/<name>`, or the settings-home-forwarder
# -seam expansion) anywhere in DoE's swept surface -- see
# `_is_namespace_qualified_citation`'s own docstring in fleet_reachability.py for the fix
# and its accepted coverage trade (a real oracle cited ONLY in bare form is now invisible
# to this gate; `claude-klabauter-doctor-probe` is the one live instance of that residual today).
# Real-tree run at this pass: `python3 -m coordinator_core.plugin_health.fleet_reachability`
# reports `[ok] fleet-reachability: 140 fleet-cited oracle(s) all have a surviving claude-klabauter
# coordinator/bin/ entry`. See the run-report sidecar for the full per-name citing-line
# breakdown this diagnosis is based on.
@pytest.mark.real_home
def test_live_tree_reachability_ok_on_this_machine_when_registered():
    """Review: code-reviewer (Finding 1) — the ONLY test in this file that
    asserts `result.ok` against REAL (unmocked) claude-klabauter + DoE-claude disk
    state, closing the gap where every content-asserting test above uses
    synthetic tmp_path fixtures and the pre-existing
    test_ci_no_silent_skip_on_this_machine_when_registered deliberately
    checks only the skip flag, never `result.ok`. Without this, a real
    c79e66cd-shaped regression (an oracle deleted from coordinator/bin/
    while a real DoE fence still cites it) would pass this entire suite.

    Gated on repos.doe_claude being registered (same skip-cleanly-otherwise
    pattern as test_ci_no_silent_skip_on_this_machine_when_registered) so
    this cannot fail in an OSS-consumer checkout with no DoE-claude root."""
    from coordinator_core.machine_resolver import registry_get as real_registry_get

    if not real_registry_get("repos.doe_claude"):
        pytest.skip("repos.doe_claude not registered on this machine")

    result = fr.check_fleet_reachability()

    assert result.skipped is False
    assert result.ok is True, (
        f"live fleet-reachability gate failed against real disk state — "
        f"missing oracle(s): {result.missing}"
    )
    # Review: code-reviewer (Finding 1) — `ok is True` alone is satisfied
    # identically by today's real ~140-citation sweep AND by a demand-filter
    # regression that zeroed `doe_demand` out entirely (missing_normalized
    # would be [] either way). `demand_count` makes the sweep's actual
    # coverage a first-class, asserted fact instead of something visible
    # only in stdout. Floor of 50 (not >0): the real count was 140 at the
    # time this assertion was written (see the module-level comment above
    # this test, and the 2026-07-27 fleet-reachability report), so 50 leaves
    # headroom for ordinary fleet churn (new/retired skills, doc reshuffles)
    # while still catching a >60% over-filter regression — a bug that
    # accidentally excludes, say, an entire sweep subdirectory or inverts a
    # qualification check would collapse the count far below this floor long
    # before it could ever reach zero outright.
    assert result.demand_count > 50, (
        f"fleet-reachability demand sweep found only {result.demand_count} citation(s) against "
        "real DoE-claude disk state — expected >50; this is the vacuous-pass shape Finding 1 "
        "closes (a demand-filter regression could zero doe_demand and still report ok=True)"
    )
