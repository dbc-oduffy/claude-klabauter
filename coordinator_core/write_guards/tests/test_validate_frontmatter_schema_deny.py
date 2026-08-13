"""Behavioral tests for
coordinator_core.write_guards.validate_frontmatter_schema_deny — the
CLASS=hard-deny leg of the fan-in split of example-doctrine-repo's
validate-frontmatter-schema.py PreToolUse hook.

Requires the sibling example-doctrine-repo checkout (for coordinator/schemas/ and the
registry manifest) — skipped entirely when absent, mirroring the advisory
sibling's suite and the coordinator_core.testing.doe_root convention.

`coordinator_doe_root` is monkeypatched to the resolved sibling root for
every test (rather than relying on REPO_EXAMPLE_DOCTRINE_REPO / machine-local at test
time) so the suite is deterministic regardless of this machine's registry
state.

Covers: non-write-tool/non-dict-tool_input passthrough, example-doctrine-repo-root-unresolvable
fail-open, the four UNCONDITIONAL denies (own-inbox misplacement, lineage-
reachability hard-reject, grouping-approval scope-cut, D3 out-of-enum
handoff `kind`) firing regardless of COORDINATOR_SCHEMA_STRICT, every
warn-by-default path (schema validation, mislocated-memo offer,
routing-mismatch offer, scaffold offer) staying silent by default (the
advisory sibling renders it) and — per the 2026-08-06 warn-not-block ruling
(docs/plans/2026-08-06-apply-guard-class-census.md C15) — rendering an
`additionalContext` WARNING of its own (never a deny) under
COORDINATOR_SCHEMA_STRICT=1, exactly when the advisory sibling stands down
for that same finding, a mutual-exclusivity smoke test against the advisory
sibling across a representative payload set, and the module contract
(CLASS/MATCHERS/PRIORITY).

Spec backlink: docs/plans/2026-07-29-hook-fan-in-write-path.md § C10
Source: example-doctrine-repo coordinator/hooks/scripts/validate-frontmatter-schema.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import yaml

from coordinator_core.frontmatter.schema_validate import compute_grouping_digest
from coordinator_core.testing.doe_root import doe_root_and_present
from coordinator_core.write_guards import validate_frontmatter_schema_advisory as advisory_guard
from coordinator_core.write_guards import validate_frontmatter_schema_deny as guard

_doe_root, _doe_present = doe_root_and_present()

# Real-git spawn is load-bearing: `_init_repo` builds a real git repo whose
# lineage-reachability the deny guard's unconditional hard-reject reads
# directly -- a mocked git would not prove the reachability check fires
# against real commit ancestry. Per-test isolation via tmp_path fixtures,
# not hoisted. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _pin_doe_root(monkeypatch):
    if not _doe_present:
        pytest.skip("sibling example-doctrine-repo checkout not found")
    monkeypatch.setattr(guard, "coordinator_doe_root", lambda: _doe_root)
    monkeypatch.setattr(advisory_guard, "coordinator_doe_root", lambda: _doe_root)


def _payload(tool_name, file_path, cwd, **tool_input_extra):
    tool_input = {"file_path": file_path}
    tool_input.update(tool_input_extra)
    return {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd}


def _assert_deny_shape(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "additionalContext" not in hso
    assert set(hso.keys()) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    return hso["permissionDecisionReason"]


def _assert_advisory_shape(result: dict) -> str:
    """2026-08-06 ruling (docs/plans/2026-08-06-apply-guard-class-census.md
    C15): every formerly COORDINATOR_SCHEMA_STRICT=1-upgraded deny leg now
    renders this shape instead — an `additionalContext` warning, never a
    `permissionDecision`, so a strict-mode schema-shaped finding never
    throws away the write attempt that triggered it.
    """
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert set(hso.keys()) == {"hookEventName", "additionalContext"}
    return hso["additionalContext"]


class TestModuleContract:
    def test_class_matchers_priority(self):
        assert guard.CLASS == "hard-deny"
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit"]
        assert guard.PRIORITY == 5


class TestGateOnToolAndPayloadShape:
    def test_non_guarded_tool_passes_through(self, tmp_path):
        assert guard.check(_payload("Read", str(tmp_path / "x.md"), str(tmp_path))) is None

    def test_tool_input_not_dict_passes_through(self):
        assert guard.check({"tool_name": "Write", "tool_input": "nope"}) is None

    def test_missing_file_path_passes_through(self, tmp_path):
        assert guard.check({"tool_name": "Write", "tool_input": {}, "cwd": str(tmp_path)}) is None

    def test_doe_root_unresolvable_fails_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: None)
        result = guard.check(
            _payload("Write", str(tmp_path / "state" / "handoffs" / "x.md"), str(tmp_path),
                     content="---\ntitle: t\n---\nbody")
        )
        assert result is None


class TestSchemaCorpusResolutionWithDoeSiblingAbsent:
    """AC2 (docs/plans/2026-08-06-repoint-write-enforcement-at-vendored-corpus.md):
    the deny guard still validates against the schema corpus with the example-doctrine-repo
    sibling completely unreachable — closing the fail-open-on-missing-
    sibling hole the plan's Problem section names, because the schema
    corpus now ships in-repo (vendored) rather than being read from example-doctrine-repo-
    claude's live working tree. Only manifest-DERIVED behaviour (memo
    routing, scaffold offers) is expected to degrade when the sibling is
    absent — schema-shape validation, lineage-reachability, and the other
    unconditional denies must not.
    """

    def _absent_doe_root(self, monkeypatch):
        # Simulate absence via the resolver, never by touching/moving the
        # real sibling checkout — see plan C2's "never by moving the real
        # clone" instruction.
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: None)

    def test_schema_shape_violation_still_warns_under_strict_with_sibling_absent(
        self, tmp_path, monkeypatch
    ):
        """2026-08-06 ruling: a schema-shape violation warns, never blocks —
        this still exercises the vendored-corpus-with-sibling-absent property
        (AC2), just against the new advisory shape."""
        self._absent_doe_root(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "handoff:" in reason

    def test_fully_conformant_handoff_still_passes_with_sibling_absent(self, tmp_path, monkeypatch):
        self._absent_doe_root(monkeypatch)
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fm = (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n---\nold"
        )
        fp.write_text(fm, encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old", new_string="new")
        )
        assert result is None

    def test_context_still_resolves_vendored_schemas_dir_with_sibling_absent(self, monkeypatch):
        self._absent_doe_root(monkeypatch)
        ctx = guard._load_context()
        assert ctx is not None
        assert ctx.schemas_dir == guard._VENDORED_SCHEMAS_DIR
        # Manifest-derived fields degrade to empty defaults, never a crash.
        assert ctx.manifest == {}
        assert ctx.central_em_ids == set()
        assert ctx.central_canonical_id is None


class TestSchemaCorpusSourcePinned:
    """AC3: pins the resolution SOURCE, not just behaviour — a behavioural
    test alone would silently pass again the moment example-doctrine-repo's live corpus and
    claude-klabauter's vendored copy happen to agree, exactly the trap the plan names.
    Asserts the guard's resolved schemas directory is the in-repo vendored
    path and is never derived from `coordinator_doe_root()`.
    """

    def test_resolved_schemas_dir_is_the_vendored_in_repo_path(self):
        ctx = guard._load_context()
        assert ctx is not None
        expected = (
            Path(guard.__file__).resolve().parents[1] / "frontmatter" / "schemas"
        )
        assert ctx.schemas_dir == expected
        assert ctx.schemas_dir.is_dir()

    def test_resolved_schemas_dir_does_not_move_when_doe_root_changes(self, tmp_path, monkeypatch):
        # A re-point at ANY doe_root value (real, fake, or absent) must never
        # change the resolved schema corpus -- pinning the SOURCE, not merely
        # today's behavioural agreement between the two corpora.
        fake_doe_root = str(tmp_path / "not-a-real-example-doctrine-repo-checkout")
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: fake_doe_root)
        ctx = guard._load_context()
        assert ctx is not None
        assert ctx.schemas_dir == guard._VENDORED_SCHEMAS_DIR
        assert "coordinator" not in ctx.schemas_dir.parts

    def test_resolved_schemas_dir_is_never_derived_from_coordinator_doe_root(self):
        ctx = guard._load_context()
        assert ctx is not None
        # The vendored corpus lives under claude-klabauter's own frontmatter/ tree,
        # never under the example-doctrine-repo sibling's coordinator/schemas/.
        assert str(ctx.schemas_dir) != str(Path(_doe_root) / "coordinator" / "schemas")
        assert ctx.schemas_dir.resolve().is_relative_to(
            Path(guard.__file__).resolve().parents[1]
        )


class TestTornWriteRetry:
    """example-doctrine-repo's schema corpus and registry manifest are read from the sibling
    checkout's LIVE working tree on every check() — a concurrent session can
    leave a torn/partial JSON file mid-write. `_retry_on_transient_read_failure`
    narrows that race without changing the guard's fail-open contract once
    every attempt is exhausted.
    """

    def _present_path(self, tmp_path):
        """A path that exists, so the retry loop is in play (presence is
        the discriminator the helper checks — the retry tests below are
        exercising the "present but unparseable" branch, not the absent
        one)."""
        p = tmp_path / "present.json"
        p.write_text("{}", encoding="utf-8")
        return p

    def test_recovers_from_a_transient_failure_within_budget(self, tmp_path):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < guard._TORN_WRITE_RETRY_ATTEMPTS:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return "ok"

        assert guard._retry_on_transient_read_failure(flaky, exists_path=self._present_path(tmp_path)) == "ok"
        assert calls["n"] == guard._TORN_WRITE_RETRY_ATTEMPTS

    def test_reraises_the_last_exception_once_attempts_are_exhausted(self, tmp_path):
        calls = {"n": 0}

        def always_broken():
            calls["n"] += 1
            raise ValueError(f"torn write #{calls['n']}")

        with pytest.raises(ValueError, match="torn write #3"):
            guard._retry_on_transient_read_failure(always_broken, exists_path=self._present_path(tmp_path))
        assert calls["n"] == guard._TORN_WRITE_RETRY_ATTEMPTS

    def test_succeeds_immediately_without_extra_calls(self, tmp_path):
        calls = {"n": 0}

        def works_first_try():
            calls["n"] += 1
            return "ok"

        assert guard._retry_on_transient_read_failure(works_first_try, exists_path=self._present_path(tmp_path)) == "ok"
        assert calls["n"] == 1

    def test_absent_path_calls_once_and_never_sleeps_or_retries(self, tmp_path, monkeypatch):
        """A missing path (partial install, no schemas tree, permissions
        problem) is a STEADY STATE, not a race — no number of retries makes
        it appear. Assert the attempt count and that time.sleep is never
        invoked — never assert on wall-time, that's flaky by construction.
        """
        calls = {"n": 0}
        sleep_calls = {"n": 0}
        monkeypatch.setattr(guard.time, "sleep", lambda secs: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))

        def always_broken():
            calls["n"] += 1
            raise FileNotFoundError("no such file")

        missing = tmp_path / "does-not-exist.json"
        assert not missing.exists()

        with pytest.raises(FileNotFoundError):
            guard._retry_on_transient_read_failure(always_broken, exists_path=missing)
        assert calls["n"] == 1
        assert sleep_calls["n"] == 0

    def test_transient_schema_load_failure_still_denies_like_today(self, tmp_path, monkeypatch):
        """A schema corpus read that fails once (torn write) then succeeds
        must produce the SAME deny it would have with no torn-write at all —
        the retry must never surface a weaker verdict than a clean read.
        """
        real_load_schemas = guard._load_schemas
        calls = {"n": 0}

        def flaky_load_schemas(schemas_dir):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("torn write on first read")
            return real_load_schemas(schemas_dir)

        monkeypatch.setattr(guard, "_load_schemas", flaky_load_schemas)

        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text(
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: 2099-01-01-this-handoff-never-existed-anywhere.md\n"
            "category: infra\nsummary: a one-line summary\n---\nold body",
            encoding="utf-8",
        )
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        reason = _assert_deny_shape(result)
        assert "Lineage-reachability check failed" in reason
        assert calls["n"] >= 1

    def test_permanently_malformed_schema_corpus_still_fails_open(self, tmp_path, monkeypatch):
        """Once every retry attempt is exhausted, behavior must be identical
        to today: `check()` returns None (fail-open on infra), never a deny
        manufactured from partial state.
        """

        def always_broken(schemas_dir):
            raise ValueError("schemas dir is permanently malformed")

        monkeypatch.setattr(guard, "_load_schemas", always_broken)

        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text(
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\ncategory: infra\n"
            "summary: a one-line summary\n---\nold body",
            encoding="utf-8",
        )
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is None

    def test_missing_manifest_still_fails_open_with_no_sleep(self, tmp_path, monkeypatch):
        """An unresolvable/missing manifest (the corpus never loads at all,
        distinct from a torn-write mid-load) is untouched by the retry and
        must keep returning None — AND must not pay any retry-sleep cost,
        since a PreToolUse hook firing on every guarded Write/Edit/MultiEdit
        cannot afford dead sleep on a steady-state absent path."""
        sleep_calls = {"n": 0}
        monkeypatch.setattr(guard.time, "sleep", lambda secs: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))
        fake_root = str(tmp_path / "nonexistent-example-doctrine-repo-root")
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: fake_root)
        result = guard.check(
            _payload("Write", "/tmp/state/handoffs/x.md", "/tmp", content="---\ntitle: t\n---\nbody")
        )
        assert result is None
        assert sleep_calls["n"] == 0


class TestOwnInboxUnconditionalDeny:
    """The own-inbox misplacement guard fires regardless of
    COORDINATOR_SCHEMA_STRICT — this is the deny module's exclusive
    territory (the advisory sibling always returns None for it).
    """

    def _memo_content(self):
        return (
            "---\nfrom: example-doctrine-repo-em\nto: example-game-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )

    def test_own_inbox_misplacement_denies_in_default_mode(self):
        fp = f"{_doe_root}/cross-repo/inbox/2099-01-01-fake-own-inbox-test.md"
        result = guard.check(_payload("Write", fp, _doe_root, content=self._memo_content()))
        assert result is not None
        reason = _assert_deny_shape(result)
        assert "cross-repo/inbox/" in reason
        # The override note routes to the doc and does not echo the key —
        # register rule B6 (BYPASS-KEY IN THE DENIAL).
        assert "guard-override-keys.md" in reason

    def test_own_inbox_misplacement_denies_under_strict_too(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = f"{_doe_root}/cross-repo/inbox/2099-01-01-fake-own-inbox-strict-test.md"
        result = guard.check(_payload("Write", fp, _doe_root, content=self._memo_content()))
        assert result is not None
        _assert_deny_shape(result)

    def test_override_env_var_suppresses_the_deny(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_OWN_INBOX", "1")
        fp = f"{_doe_root}/cross-repo/inbox/2099-01-01-fake-own-inbox-override-test.md"
        result = guard.check(_payload("Write", fp, _doe_root, content=self._memo_content()))
        assert result is None

    def test_correct_inbound_memo_passes_through_silent(self):
        fp = f"{_doe_root}/cross-repo/inbox/2099-01-01-fake-correct-inbound-test.md"
        memo_content = (
            "---\nfrom: example-game-repo-em\nto: example-doctrine-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        result = guard.check(_payload("Write", fp, _doe_root, content=memo_content))
        assert result is None


class TestOwnInboxCasefoldBypass:
    """Casefold bypass-proof for the `this_repo_is_central`/
    `landing_repo_is_central` identity check (2026-08-05 fast-follow to
    `test_casefold_bypass_lint.py`, commit `223e04b7bf2e`).

    Reproduces the bug WITHOUT depending on the host filesystem's own
    case sensitivity: both `repo_root_realpath` and `doe_root_realpath`
    are `Path(...).resolve()` outputs, and `.resolve()` is lexical (no
    on-disk lookup, hence no case correction) for any path COMPONENT that
    does not exist on disk -- true on every platform, not only a
    case-insensitive one. Using a real (existing) `tmp_path` parent with a
    NONEXISTENT, differently-cased leaf reproduces the exact shape a real
    case-insensitive-but-case-preserving filesystem (macOS APFS, Windows)
    would alias, deterministically, in CI.
    """

    def _memo_content(self, from_id: str, to_id: str) -> str:
        return (
            f"---\nfrom: {from_id}\nto: {to_id}\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )

    def test_differently_cased_central_repo_own_inbox_misplacement_now_denied(self, tmp_path, monkeypatch):
        ctx = guard._load_context()
        central_id = ctx.central_canonical_id

        canonical_doe_root = str(tmp_path / "doe-root-casefold-proof")
        aliased_repo_root = str(tmp_path / "DOE-ROOT-CASEFOLD-PROOF")

        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: canonical_doe_root)

        repo_rel = "cross-repo/inbox/2099-01-01-fake-casefold-bypass-test.md"
        abs_file_path = f"{aliased_repo_root}/{repo_rel}"
        tool_input = {
            "file_path": abs_file_path,
            "content": self._memo_content(central_id, "some-other-repo-em"),
        }

        result = guard._memo_guard_step(
            ctx, "Write", tool_input, aliased_repo_root, abs_file_path, repo_rel
        )
        assert result is not None, (
            "casefold bypass reopened: a differently-cased alias of the "
            "central repo was not recognized as central, so the own-inbox "
            "misplacement (from central, to elsewhere) went undetected"
        )
        assert result[0] == "deny"

    def test_matching_case_control_still_denies(self, tmp_path, monkeypatch):
        """Control: the same scenario with matching case was already denied
        before this fix -- proves the test setup itself is sound."""
        ctx = guard._load_context()
        central_id = ctx.central_canonical_id

        canonical_doe_root = str(tmp_path / "doe-root-casefold-proof-control")
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: canonical_doe_root)

        repo_rel = "cross-repo/inbox/2099-01-01-fake-casefold-control-test.md"
        abs_file_path = f"{canonical_doe_root}/{repo_rel}"
        tool_input = {
            "file_path": abs_file_path,
            "content": self._memo_content(central_id, "some-other-repo-em"),
        }

        result = guard._memo_guard_step(
            ctx, "Write", tool_input, canonical_doe_root, abs_file_path, repo_rel
        )
        assert result is not None and result[0] == "deny"


class TestLineageReachabilityUnconditionalDeny:
    def _handoff_path(self, tmp_path):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        return d / "existing.md"

    def _unresolvable_frontmatter(self):
        return (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: 2099-01-01-this-handoff-never-existed-anywhere.md\n"
            "category: infra\nsummary: a one-line summary\n---\nold body"
        )

    def test_unresolvable_predecessor_denies_in_default_mode(self, tmp_path):
        fp = self._handoff_path(tmp_path)
        fp.write_text(self._unresolvable_frontmatter(), encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        reason = _assert_deny_shape(result)
        assert "Lineage-reachability check failed" in reason
        assert "predecessor" in reason

    def test_unresolvable_predecessor_denies_under_strict_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._handoff_path(tmp_path)
        fp.write_text(self._unresolvable_frontmatter(), encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        _assert_deny_shape(result)


class TestDenyForensicsCapture:
    """`_capture_guard_forensics` — the forensic snapshot fired on the DENY
    branch, and on the genuine-torn-read fail-open branch, of `check()` (see
    its docstring and `_is_torn_read_signature`). Reuses
    `TestLineageReachabilityUnconditionalDeny`'s unresolvable-predecessor
    fixture as a cheap, unconditional (no COORDINATOR_SCHEMA_STRICT needed)
    deny trigger; `tmp_path` doubles as this guard's own repo root
    (`_resolve_repo_root` falls back to `cwd` when `git rev-parse` finds no
    repo there), so the forensics dump lands under
    `tmp_path/state/scratch/write-guard-forensics/` fully isolated per test.
    """

    def _handoff_path(self, tmp_path):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        return d / "existing.md"

    def _unresolvable_frontmatter(self):
        return (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: 2099-01-01-this-handoff-never-existed-anywhere.md\n"
            "category: infra\nsummary: a one-line summary\n---\nold body"
        )

    def _forensics_dir(self, tmp_path):
        return tmp_path / "state" / "scratch" / "write-guard-forensics"

    def _deny_payload(self, tmp_path):
        fp = self._handoff_path(tmp_path)
        fp.write_text(self._unresolvable_frontmatter(), encoding="utf-8")
        return _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")

    def test_capture_fires_on_deny_with_expected_fields(self, tmp_path):
        payload = self._deny_payload(tmp_path)
        result = guard.check(payload)
        reason = _assert_deny_shape(result)

        forensics_dir = self._forensics_dir(tmp_path)
        assert forensics_dir.is_dir(), "a deny must write a forensics capture under state/scratch/"
        dumps = list(forensics_dir.glob("validate_frontmatter_schema_deny-*.json"))
        assert len(dumps) == 1, f"expected exactly one capture file, found {dumps}"

        record = json.loads(dumps[0].read_text(encoding="utf-8"))
        assert record["guard"] == "validate_frontmatter_schema_deny"
        assert record["capture_reason"] == "deny"
        assert record["deny_reason"] == reason
        assert record["matched_schema_name"] == "handoff"
        assert record["doe_root"] == _doe_root
        # Repointed at the vendored corpus (AC1/AC3) -- no longer example-doctrine-repo's live
        # working tree -- so the forensics capture must reflect that too.
        assert record["schema_corpus_path"] == str(guard._VENDORED_SCHEMAS_DIR)
        # plan-tasks.schema.json exists in the real example-doctrine-repo checkout this suite
        # requires (see module docstring's skip-if-absent condition), and
        # this deny's own walk reached the schema-corpus load (it fires
        # AFTER that load, from `_reachability_and_schema_step`) -- so the
        # status must be "hashed", the real thing, not a re-read guess.
        assert record["plan_tasks_schema_status"] == "hashed"
        assert isinstance(record["plan_tasks_schema_sha256"], str)
        assert len(record["plan_tasks_schema_sha256"]) == 64
        assert record["schema_corpus_retry"]["path_existed"] is True
        assert record["schema_corpus_retry"]["succeeded"] is True
        assert record["doe_tree_dirty_at_capture"] in (True, False)

    def test_capture_hashes_the_object_that_produced_the_verdict_not_a_fresh_reread(
        self, tmp_path, monkeypatch
    ):
        """Regression for the fidelity defect this replaces: a re-read taken
        AFTER the verdict can land cleanly even when the read that actually
        produced a deny was torn, making a hash of "whatever's on disk now"
        actively misleading. Proven by swapping in a synthetic `plan-tasks`
        schema at load time and confirming the capture reflects THAT
        content's hash — not whatever plan-tasks.schema.json holds on disk
        right now, which this test never touches.
        """
        payload = self._deny_payload(tmp_path)

        synthetic_plan_tasks = {"type": "object", "marker": "synthetic-test-schema"}
        real_load_schemas = guard._load_schemas

        def _fake_load_schemas(schemas_dir):
            loaded = real_load_schemas(schemas_dir)
            loaded["plan-tasks"] = synthetic_plan_tasks
            return loaded

        monkeypatch.setattr(guard, "_load_schemas", _fake_load_schemas)

        result = guard.check(payload)
        _assert_deny_shape(result)

        dumps = list(self._forensics_dir(tmp_path).glob("validate_frontmatter_schema_deny-*.json"))
        assert len(dumps) == 1
        record = json.loads(dumps[0].read_text(encoding="utf-8"))

        expected_hash = hashlib.sha256(
            json.dumps(synthetic_plan_tasks, sort_keys=True).encode("utf-8")
        ).hexdigest()
        assert record["plan_tasks_schema_status"] == "hashed"
        assert record["plan_tasks_schema_sha256"] == expected_hash

    def test_capture_records_not_loaded_when_an_earlier_guard_step_denies_first(self, tmp_path):
        """The own-inbox misplacement deny fires inside `_memo_guard_step`,
        BEFORE the schema corpus is ever read for this call.
        `plan_tasks_schema_status` must say so explicitly (`not_loaded`) —
        a bare `None` here would be indistinguishable from "we looked and
        found nothing", which is a different (and wrong) forensic claim.
        """
        inbox_dir = tmp_path / "cross-repo" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        fp = inbox_dir / "2099-01-01-fake-own-inbox-forensics-test.md"

        # tmp_path is not the central example-doctrine-repo checkout, so its EM id is derived
        # from its own basename -- compute it the same way the guard does.
        ctx = guard._load_context()
        this_em_id = guard._em_id_for_basename(ctx, tmp_path.name)

        memo_content = (
            f"---\nfrom: {this_em_id}\nto: example-game-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        payload = _payload("Write", str(fp), str(tmp_path), content=memo_content)
        result = guard.check(payload)
        _assert_deny_shape(result)

        dumps = list(self._forensics_dir(tmp_path).glob("validate_frontmatter_schema_deny-*.json"))
        assert len(dumps) == 1
        record = json.loads(dumps[0].read_text(encoding="utf-8"))
        assert record["plan_tasks_schema_status"] == "not_loaded"
        assert record["plan_tasks_schema_sha256"] is None
        assert record["schema_corpus_retry"] is None

    def test_capture_fires_on_fail_open_torn_read_not_just_deny(self, tmp_path, monkeypatch):
        """A schema-corpus read that exists on disk but never parses
        cleanly (every retry attempt fails) is exactly the torn-read shape
        this task exists to make legible — and it resolves as an ALLOW
        (fail-open), not a deny. `check()` must still capture it, labeled
        distinctly, so a future investigator doesn't read a missing capture
        as "nothing happened" when a real corpus-load failure occurred.
        """
        def _always_torn(schemas_dir):
            raise ValueError("simulated torn schema-corpus read")

        monkeypatch.setattr(guard, "_load_schemas", _always_torn)

        fp = tmp_path / "docs" / "note.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        payload = _payload("Write", str(fp), str(tmp_path), content="hello")

        result = guard.check(payload)
        assert result is None, "a corpus load failure must fail OPEN, never deny"

        dumps = list(self._forensics_dir(tmp_path).glob("validate_frontmatter_schema_deny-*.json"))
        assert len(dumps) == 1, "a genuine torn read must still leave a forensics capture"
        record = json.loads(dumps[0].read_text(encoding="utf-8"))
        assert record["capture_reason"] == "load_failure_fail_open"
        assert record["deny_reason"] is None
        assert record["plan_tasks_schema_status"] == "load_failed"
        assert record["schema_corpus_retry"]["exhausted"] is True
        assert record["schema_corpus_retry"]["path_existed"] is True

    def test_capture_does_not_fire_on_allow(self, tmp_path):
        result = guard.check(_payload("Read", str(tmp_path / "x.md"), str(tmp_path)))
        assert result is None
        assert not self._forensics_dir(tmp_path).exists()

    def test_capture_does_not_fire_on_absent_doe_root_steady_state(self, tmp_path, monkeypatch):
        """An unresolvable example-doctrine-repo root is the ABSENT steady state this module's
        docstring calls out (partial install, no sibling checkout) — NOT a
        failure, and potentially true on EVERY guarded write on such a
        machine. It must never trigger a forensics write, or this capture
        would reintroduce a per-write tax on exactly the machines least able
        to afford one.
        """
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: None)
        fp = tmp_path / "state" / "handoffs" / "x.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        result = guard.check(_payload("Write", str(fp), str(tmp_path), content="---\ntitle: t\n---\nbody"))
        assert result is None
        assert not self._forensics_dir(tmp_path).exists()

    def test_broken_capture_target_leaves_verdict_byte_identical_and_does_not_raise(
        self, tmp_path, monkeypatch
    ):
        payload = self._deny_payload(tmp_path)
        baseline = guard.check(payload)
        assert baseline is not None
        # A fresh capture-dir mkdir already happened for `baseline` above;
        # remove it so this second call exercises capture from a clean slate
        # and isn't just re-finding the first dump.
        import shutil

        forensics_dir = self._forensics_dir(tmp_path)
        if forensics_dir.exists():
            shutil.rmtree(forensics_dir)

        def _boom(self, *args, **kwargs):
            raise OSError("simulated unwritable forensics target")

        monkeypatch.setattr(guard.Path, "write_text", _boom)

        result = guard.check(payload)

        assert result == baseline, "a broken forensics capture must not alter the deny verdict"
        assert not forensics_dir.exists() or not list(forensics_dir.glob("*.json"))


class TestSchemaValidationDeny:
    def _handoff_path(self, tmp_path):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        return d / "existing.md"

    def test_missing_required_fields_stays_silent_by_default(self, tmp_path):
        fp = self._handoff_path(tmp_path)
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is None

    def test_missing_required_fields_warns_under_strict(self, tmp_path, monkeypatch):
        """2026-08-06 ruling: strict mode no longer escalates a schema-shape
        violation to a deny -- this leg now renders the advisory itself
        (the sibling stands down under strict)."""
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._handoff_path(tmp_path)
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "handoff:" in reason

    def test_fully_conformant_handoff_passes_through_silent_even_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._handoff_path(tmp_path)
        fm = (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n---\nold"
        )
        fp.write_text(fm, encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old", new_string="new")
        )
        assert result is None


class TestMemoOffersDeny:
    def test_mislocated_memo_path_silent_by_default(self, tmp_path):
        d = tmp_path / "state" / "memos"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "hand-rolled.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="To: someone\nFrom: me\n\nhi")
        )
        assert result is None

    def test_mislocated_memo_path_warns_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "memos"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "hand-rolled.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="To: someone\nFrom: me\n\nhi")
        )
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "cross-repo-memo --to" in reason


class TestRoutingMismatchDeny:
    def _memo_content(self):
        return (
            "---\nfrom: example-game-repo-em\nto: example-retrieval-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )

    def test_routing_mismatch_silent_by_default(self):
        fp = f"{_doe_root}/cross-repo/2099-01-01-fake-routing-mismatch-test.md"
        result = guard.check(_payload("Write", fp, _doe_root, content=self._memo_content()))
        assert result is None

    def test_routing_mismatch_warns_under_strict(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = f"{_doe_root}/cross-repo/2099-01-01-fake-routing-mismatch-strict-test.md"
        result = guard.check(_payload("Write", fp, _doe_root, content=self._memo_content()))
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "example-retrieval-repo-em" in reason


class TestScaffoldOfferDeny:
    def test_new_schema_matching_file_silent_by_default(self, tmp_path):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "brand-new.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="---\ntitle: t\n---\nbody")
        )
        assert result is None

    def test_new_schema_matching_file_warns_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "brand-new.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="---\ntitle: t\n---\nbody")
        )
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "coordinator-doc-new --type handoff" in reason

    def test_existing_file_does_not_trigger_scaffold_offer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text("---\ntitle: t\n---\nold", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old", new_string="new")
        )
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "coordinator-doc-new" not in reason
        assert "handoff:" in reason


class TestNonMemoPathUntouched:
    def test_unrelated_path_with_no_schema_match_passes_through(self, tmp_path):
        fp = tmp_path / "README.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="# hello\n\nnothing schema-shaped here")
        )
        assert result is None


class TestPlanTasksSpineDeny:
    """STRICT-mode shape of the plan `## Tasks` task-spine row validation —
    mirrors the advisory sibling's TestPlanTasksSpineWarn exactly (same
    fixtures, same findings), since both legs share _plan_tasks_spine_errors
    and must report identically, only differing in warn-vs-deny shape.
    """

    _FRONTMATTER = (
        "---\ntitle: Test plan\ncreated: 2026-07-29\nauthor: test\nstatus: draft\n---\n\n"
        "# Plan\n\n## Tasks\n"
    )

    def _plan_path(self, tmp_path):
        d = tmp_path / "docs" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return d / "2026-07-29-test-plan.md"

    def _write_and_check(self, tmp_path, tasks_block):
        """Drives the DENY leg (guard.check) — the module under test in this file."""
        fp = self._plan_path(tmp_path)
        old_content = self._FRONTMATTER
        fp.write_text(old_content, encoding="utf-8")
        new_content = self._FRONTMATTER + tasks_block
        payload = _payload(
            "Edit", str(fp), str(tmp_path), old_string=old_content, new_string=new_content
        )
        return guard.check(payload)

    def test_bad_change_kind_silent_by_default(self, tmp_path):
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-port\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        assert self._write_and_check(tmp_path, tasks_block) is None

    def test_bad_change_kind_warns_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-port\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        fp = self._plan_path(tmp_path)
        old_content = self._FRONTMATTER
        fp.write_text(old_content, encoding="utf-8")
        new_content = old_content + tasks_block
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string=old_content, new_string=new_content)
        )
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "tasks[C1].change_kind" in reason
        assert 'invalid enum value "script-port"' in reason

    def test_valid_spine_passes_silent_even_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        fp = self._plan_path(tmp_path)
        old_content = self._FRONTMATTER
        fp.write_text(old_content, encoding="utf-8")
        new_content = old_content + tasks_block
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string=old_content, new_string=new_content)
        )
        assert result is None

    def test_governed_closed_row_without_pm_approved_passes_schema_layer(self, tmp_path, monkeypatch):
        """Gap 1 regression (2026-07-29 grouping-approval contract,
        write-guard-bypass fix): on a GOVERNED plan a closed row carries NO
        `pm_approved` key by design — authorization lives in the plan's
        `grouping_approvals` block, not the row. Before the fix, this write
        guard validated every row against the raw, UNFILTERED vendored
        schema (whose `deferred=>pm_approved` and `CLOSED-dispositions=>
        pm_approved` allOf/if-then branches ARE evaluated by
        `_validate_json_schema_node` — contrary to a since-corrected belief
        in `plan_tasks_mutate.py`'s own docstring that those branches were
        "silently ignored by design") regardless of governed status, so a
        fully-approved governed plan's closed row was rejected at the
        schema layer even though `check_plan_tasks_grouping_approval` had
        already cleared it. This is the test that would have caught that:
        it drives `guard.check` directly (the write-guard path), not the
        mutate op (whose own path already filtered correctly before this
        fix landed).

        STRICT IS LOAD-BEARING HERE, and this test did not set it when first
        written (2026-07-29) — which made it a tautology for one commit.
        Schema-shaped messages resolve `shape = "deny" if _is_strict() else
        "advisory"`, and `check` returns None for anything that is not
        `"deny"`. So without `COORDINATOR_SCHEMA_STRICT=1` this assertion
        read None whether or not the pm_approved bug was present: verified
        by forcing the pre-fix condition (`_is_governed_plan -> False`) and
        watching the test still pass in default mode, then fail once strict
        was forced. That is the same defect class as the bug under test — an
        assertion satisfied for a reason unrelated to what it claims — so
        the env var is the whole point of the test, not boilerplate copied
        from its neighbours.
        """
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        # `change_kind` and `surface` are schema-required on every row and were
        # absent when this test was first written. They went unnoticed because
        # non-strict mode swallowed the resulting messages along with the one
        # the test exists to catch; under strict they surface and would mask
        # `pm_approved` as the thing under test. Present here so the ONLY
        # variable this test isolates is the pm_approved-required branch.
        rows_yaml = (
            "- id: C1\n"
            "  title: live\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/frontmatter/schema_validate.py\n"
            "- id: C2\n"
            "  title: declined\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/frontmatter/schema_validate.py\n"
            "  disposition: wont_do\n"
            "  disposition_detail: PM said no, out of scope\n"
            # Required by plan-tasks 1.6.0's allOf conditional on the two scope-cut
            # dispositions. Present so the ONLY variable this test isolates stays the
            # pm_approved-required branch. NOTE: this fixture tracks example-doctrine-repo's LIVE tree,
            # not claude-klabauter's vendored copy -- these guards resolve schemas_dir from
            # coordinator_doe_root(), so a example-doctrine-repo-side bump reaches them with no re-vendor.
            "  case_against: Superseded by the C4 rewrite; carrying it forward would\n"
            "    duplicate that surface.\n"
        )
        rows = yaml.safe_load(rows_yaml)
        digest = compute_grouping_digest(rows, "ruled_out")
        frontmatter = (
            "---\ntitle: Test plan\ncreated: 2026-07-29\nauthor: test\nstatus: draft\n"
            "grouping_approvals:\n"
            "  ruled_out:\n"
            "    status: approved\n"
            "    approver: pm\n"
            "    approved_at: 2026-07-29\n"
            "    pm_utterance: 'yes, drop C2'\n"
            f"    digest: '{digest}'\n"
            "---\n\n"
            "# Plan\n\n## Tasks\n"
        )
        fp = self._plan_path(tmp_path)
        fp.write_text(frontmatter, encoding="utf-8")
        tasks_block = f"```yaml plan-tasks\n{rows_yaml}```\n"
        new_content = frontmatter + tasks_block
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string=frontmatter, new_string=new_content)
        )
        if result is not None:
            reason = result.get("hookSpecificOutput", {}).get(
                "permissionDecisionReason", "<no permissionDecisionReason in result>"
            )
            # `reason` is the byte-for-byte deny message (see INTERFACE.md rule 2):
            # for a schema-shape deny it already leads with the matched schema
            # name and the per-row field/error that fired (see
            # `_violation_message`), which is the "which schema, why" a reader
            # needs on this test's ONE failure — a raw `result` dict makes them
            # decode envelope shape first. If this ever fires, also check
            # state/scratch/write-guard-forensics/ for a
            # validate_frontmatter_schema_deny-*.json capture (example-doctrine-repo root, schema
            # corpus path, plan-tasks schema content hash, sibling-tree dirty
            # flag at capture time) written by `_capture_deny_forensics`.
            pytest.fail(
                "a governed, approved plan's closed row without pm_approved must "
                f"not be denied at the schema layer.\ndeny reason: {reason}"
            )

    def test_zero_fences_is_silent_even_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._plan_path(tmp_path)
        old_content = self._FRONTMATTER
        fp.write_text(old_content, encoding="utf-8")
        new_content = old_content + "\nNo spine yet.\n"
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string=old_content, new_string=new_content)
        )
        assert result is None


class TestEveryFiringResultIsDenyOrAdvisoryShaped:
    """This module is CLASS=hard-deny; the engine's hard-deny phase returns
    on the first non-None DENY result regardless of shape, so a *deny*
    envelope here that is really an advisory-shaped finding would corrupt
    the engine's deny-vs-advisory split. Historically this module could
    ONLY ever emit `permissionDecision` (never `additionalContext`) — the
    2026-08-06 warn-not-block ruling (docs/plans/2026-08-06-apply-guard-
    class-census.md C15) changed that deliberately: the four genuinely
    UNCONDITIONAL findings (own-inbox, lineage-reachability, and — not
    included in this payload set, see their own dedicated test classes —
    grouping-approval and D3 kind-enum) still deny in every mode; every
    other finding now renders `additionalContext` under
    COORDINATOR_SCHEMA_STRICT=1 and stays silent (None) otherwise, never a
    `permissionDecision`. This asserts BOTH halves of that split, not just
    "never advisory" as the pre-ruling version of this test did.
    """

    def _unconditional_deny_payloads(self, tmp_path):
        """Fire regardless of COORDINATOR_SCHEMA_STRICT — must ALWAYS deny."""
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        unresolvable = handoff_dir / "unresolvable.md"
        unresolvable.write_text(
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: 2099-01-01-never-existed.md\ncategory: infra\n"
            "summary: a one-line summary\n---\nold",
            encoding="utf-8",
        )
        return [
            _payload("Edit", str(unresolvable), str(tmp_path),
                     old_string="old", new_string="new"),
            _payload("Write", f"{_doe_root}/cross-repo/inbox/2099-01-01-shape-test.md", _doe_root,
                      content=(
                          "---\nfrom: example-doctrine-repo-em\nto: example-game-repo-em\ntopic: test\ntitle: t\n"
                          "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
                      )),
        ]

    def _formerly_strict_gated_payloads(self, tmp_path):
        """Silent by default, and — since the ruling — advisory-shaped
        (never a deny) under COORDINATOR_SCHEMA_STRICT=1."""
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        existing = handoff_dir / "existing.md"
        existing.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        brand_new = handoff_dir / "brand-new.md"
        memo_dir = tmp_path / "state" / "memos"
        memo_dir.mkdir(parents=True, exist_ok=True)
        hand_rolled_memo = memo_dir / "hand-rolled.md"

        return [
            _payload("Edit", str(existing), str(tmp_path),
                     old_string="old body", new_string="new body"),
            _payload("Write", str(brand_new), str(tmp_path), content="---\ntitle: t\n---\nbody"),
            _payload("Write", str(hand_rolled_memo), str(tmp_path),
                     content="To: someone\nFrom: me\n\nhi"),
        ]

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_unconditional_findings_always_deny(self, tmp_path, monkeypatch, strict):
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        for payload in self._unconditional_deny_payloads(tmp_path):
            result = guard.check(payload)
            assert result is not None
            _assert_deny_shape(result)

    def test_formerly_strict_gated_findings_silent_by_default(self, tmp_path):
        for payload in self._formerly_strict_gated_payloads(tmp_path):
            assert guard.check(payload) is None

    def test_formerly_strict_gated_findings_warn_never_deny_under_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        for payload in self._formerly_strict_gated_payloads(tmp_path):
            result = guard.check(payload)
            assert result is not None
            _assert_advisory_shape(result)


class TestMutualExclusivityWithAdvisorySibling:
    """The property the two-module split exists to guarantee: for any given
    payload, at most one of {deny, advisory} returns non-None.
    """

    def _representative_payloads(self, tmp_path):
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        existing = handoff_dir / "existing.md"
        existing.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        conformant = handoff_dir / "conformant.md"
        conformant.write_text(
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n---\nold",
            encoding="utf-8",
        )
        unresolvable = handoff_dir / "unresolvable.md"
        unresolvable.write_text(
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: 2099-01-01-never-existed.md\ncategory: infra\n"
            "summary: a one-line summary\n---\nold",
            encoding="utf-8",
        )
        brand_new = handoff_dir / "brand-new.md"
        memo_dir = tmp_path / "state" / "memos"
        memo_dir.mkdir(parents=True, exist_ok=True)
        hand_rolled_memo = memo_dir / "hand-rolled.md"
        readme = tmp_path / "README.md"

        # Grouping-approval payload (2026-07-29 contract) — the third
        # unconditional deny needs its own entry in this corpus, or the
        # differential property it shares with the other two goes untested.
        # A GOVERNED plan closing a row whose `defer` grouping is still
        # pending: deny must fire, advisory must stand down.
        #
        # Uses `backlogged` rather than `spun_off` (2026-08-05): example-doctrine-repo's ruling
        # gave `spun_off` its own ungated grouping
        # (`_PLAN_TASKS_GROUPING_BY_DISPOSITION` maps it to `'spun_off'`, not
        # `'defer'`), so a `spun_off` row here would never touch the
        # `defer` grouping's pending-approval gate at all and this payload
        # would stop exercising the deny it exists to test. `backlogged`
        # still maps to `defer` and remains PM-gated.
        plans_dir = tmp_path / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        ungoverned_cut = plans_dir / "2026-07-29-ungoverned-cut.md"
        ungoverned_cut_content = (
            "---\n"
            "title: A plan\n"
            "schema_version: '1.2.0'\n"
            "grouping_approvals:\n"
            "  defer:\n"
            "    status: pending\n"
            "    approver: null\n"
            "    approved_at: null\n"
            "    pm_utterance: null\n"
            "    digest: null\n"
            "---\n\n"
            "# A plan\n\n"
            "## Tasks\n\n"
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: cut without assent\n"
            "  disposition: backlogged\n"
            "  disposition_detail: because I felt like it\n"
            "  disposition_ref: docs/plans/2026-07-29-elsewhere.md\n"
            "```\n"
        )
        ungoverned_cut.write_text("---\ntitle: A plan\n---\nplaceholder\n", encoding="utf-8")

        # Out-of-enum handoff `kind` (2026-07-29 D3 contract) — the fourth
        # unconditional deny needs its own corpus entry too, same reasoning
        # as the grouping-approval entry above.
        off_enum_kind = handoff_dir / "off-enum-kind.md"
        off_enum_kind.write_text(
            "---\nkind: not-a-real-kind\ntitle: t\ncreated: 2026-07-29\nbranch: main\n"
            "status: open\npredecessor: none\ncategory: infra\n"
            "summary: a one-line summary\n---\nold",
            encoding="utf-8",
        )

        # Keyed by name, never positional: a test that reaches for one specific
        # payload must name it. Appending an entry here previously shifted a
        # negative index in `test_grouping_approval_denies_unconditionally` and
        # silently retargeted that assertion at a different payload — an
        # unconditional-deny invariant is exactly the wrong thing to leave
        # resting on list order.
        return {
            "existing": _payload("Edit", str(existing), str(tmp_path),
                                 old_string="old body", new_string="new body"),
            "conformant": _payload("Edit", str(conformant), str(tmp_path),
                                   old_string="old", new_string="new"),
            "unresolvable": _payload("Edit", str(unresolvable), str(tmp_path),
                                     old_string="old", new_string="new"),
            "brand_new": _payload("Write", str(brand_new), str(tmp_path),
                                  content="---\ntitle: t\n---\nbody"),
            "hand_rolled_memo": _payload("Write", str(hand_rolled_memo), str(tmp_path),
                                         content="To: someone\nFrom: me\n\nhi"),
            "readme": _payload("Write", str(readme), str(tmp_path),
                               content="# hello\n\nnot schema-shaped"),
            "own_inbox_misplacement": _payload(
                "Write", f"{_doe_root}/cross-repo/inbox/2099-01-01-mutex-test.md", _doe_root,
                content=(
                    "---\nfrom: example-doctrine-repo-em\nto: example-game-repo-em\ntopic: test\ntitle: t\n"
                    "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
                )),
            "cross_repo_routing": _payload(
                "Write", f"{_doe_root}/cross-repo/2099-01-01-mutex-routing-test.md", _doe_root,
                content=(
                    "---\nfrom: example-game-repo-em\nto: example-retrieval-repo-em\ntopic: test\ntitle: t\n"
                    "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
                )),
            "ungoverned_cut": _payload("Write", str(ungoverned_cut), str(tmp_path),
                                       content=ungoverned_cut_content),
            "off_enum_kind": _payload("Edit", str(off_enum_kind), str(tmp_path),
                                      old_string="old", new_string="new"),
        }

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_at_most_one_sibling_fires_per_payload(self, tmp_path, monkeypatch, strict):
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        for payload in self._representative_payloads(tmp_path).values():
            deny_result = guard.check(payload)
            advisory_result = advisory_guard.check(payload)
            fired = [r for r in (deny_result, advisory_result) if r is not None]
            assert len(fired) <= 1, (
                f"both deny and advisory fired for payload {payload!r}: "
                f"deny={deny_result!r} advisory={advisory_result!r}"
            )

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_grouping_approval_denies_unconditionally(self, tmp_path, monkeypatch, strict):
        """The mutual-exclusivity test above passes when NEITHER sibling
        fires, so it cannot on its own prove the new branch works. This
        asserts the positive: a governed plan closing a row under a pending
        grouping is DENIED, in both strict and non-strict trees, because
        this deny is unconditional rather than _is_strict()-gated.
        """
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        payload = self._representative_payloads(tmp_path)["ungoverned_cut"]

        deny_result = guard.check(payload)
        assert deny_result is not None, "governed plan with a pending grouping must deny"
        assert advisory_guard.check(payload) is None, "advisory must stand down in lockstep"

        rendered = str(deny_result)
        assert "PM" in rendered
        for forbidden in ("--verb stamp", "--updates"):
            assert forbidden not in rendered, (
                "guard refusal must not print a command that satisfies the gate"
            )


class TestHandoffKindOffEnumUnconditionalDeny:
    """D3 (2026-07-29): an out-of-enum `kind` on a state/handoffs/** write is
    an unconditional deny — not COORDINATOR_SCHEMA_STRICT-gated — scoped
    only to schema_name == "handoff" (state/handoffs/**, never
    handoff-archived or any other record family).
    """

    _VALID_FM = (
        "---\nkind: {kind}\ntitle: t\ncreated: 2026-07-29\nbranch: main\n"
        "status: open\npredecessor: none\ncategory: infra\n"
        "summary: a one-line summary\n---\nbody"
    )

    def _write_handoff(self, tmp_path, name, kind):
        handoff_dir = tmp_path / "state" / "handoffs"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        fp = handoff_dir / name
        content = self._VALID_FM.format(kind=kind) if kind is not None else (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n---\nbody"
        )
        fp.write_text(content, encoding="utf-8")
        return fp

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_off_enum_kind_denies_unconditionally(self, tmp_path, monkeypatch, strict):
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._write_handoff(tmp_path, "off-enum.md", "not-a-real-kind")
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")

        result = guard.check(payload)
        assert result is not None, "off-enum kind must deny regardless of strict mode"
        reason = _assert_deny_shape(result)
        assert "not-a-real-kind" in reason

        assert advisory_guard.check(payload) is None, "advisory must stand down in lockstep"

    def test_absent_kind_stays_valid(self, tmp_path):
        """An ABSENT `kind` is valid (the emitter injects the
        `session-handoff` default) — this must never deny a missing field."""
        fp = self._write_handoff(tmp_path, "absent-kind.md", None)
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")
        assert guard.check(payload) is None

    def test_valid_kind_stays_valid(self, tmp_path):
        fp = self._write_handoff(tmp_path, "valid-kind.md", "roadmap-baton")
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")
        assert guard.check(payload) is None

    @pytest.mark.parametrize("strict", ["0", "1"])
    def test_legacy_alias_kind_stays_valid(self, tmp_path, monkeypatch, strict):
        """Retired pre-rename `kind` values (spinoff-roadmap -> roadmap-baton,
        etc.) never trigger the D3 kind-enum deny itself — that much holds in
        both modes. But this is NOT the same claim as "a legacy-alias write
        is accepted end-to-end": alias tolerance is a READER contract, not a
        writer one (the on-disk `kind` enum was deliberately narrowed to
        canonical values only), so under strict mode the base schema-shape
        check (unrelated to this deny, a few lines below it) still flags a
        legacy spelling as an invalid enum value — as of the 2026-08-06
        warn-not-block ruling that is now a WARNING this module itself
        renders under strict, never a deny.

        STRICT IS LOAD-BEARING HERE, same class of tautology risk as
        `test_governed_closed_row_without_pm_approved_passes_schema_layer`
        (review: code-reviewer -- Finding 1, ae407001): without parametrizing
        on strict, this test only ever exercised non-strict mode, where
        `guard.check()` returns `None` for any advisory-shaped first hit
        regardless of whether `canonical_kind()` de-aliasing works at all —
        it would have passed even if the D3 deny's alias handling were
        broken. Parametrizing on `strict` (mirroring
        `test_off_enum_kind_denies_unconditionally`) makes the honest
        behavior explicit: this deny stands down in both modes, but the
        schema-shape path still fires (as a warning) under strict.
        """
        if strict == "1":
            monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._write_handoff(tmp_path, "legacy-alias-kind.md", "spinoff-roadmap")
        payload = _payload("Edit", str(fp), str(tmp_path), old_string="body", new_string="body2")
        result = guard.check(payload)
        if strict == "1":
            assert result is not None, (
                "a legacy-alias kind must still surface end-to-end under strict "
                "mode, via the schema-shape path (not the D3 kind-enum deny)"
            )
            reason = _assert_advisory_shape(result)
            assert "spinoff-roadmap" in reason
        else:
            assert result is None


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


class TestCrossRepoTargetReachesSchemaStepAdvisoryOnly:
    """2a: cwd-vs-target defect fix (`_first_result` now resolves `repo_root`
    from the TARGET FILE's own repo, not the session cwd) plus the
    advisory-only cross-repo rule (DR-277) it enables — see `check()`'s
    docstring.

    Uses TWO real git repos (mirrors `test_bump_out_of_repo_tool_write.py`'s
    pattern) so `repo_root` genuinely differs between the session's cwd and
    the write target, reproducing the real defect shape (a session in one
    repo writing into a sibling repo's cross-repo/inbox/) rather than a
    same-repo stand-in.
    """

    def test_cross_repo_own_inbox_write_reaches_schema_step_and_warns(self, tmp_path):
        session_repo = _init_repo(tmp_path, "session-repo")
        target_repo = _init_repo(tmp_path, "target-repo")

        ctx = guard._load_context()
        # Resolve the em-id `_memo_guard_step` would derive for target_repo's
        # own basename, so the own-inbox misplacement predicate (from ==
        # landing repo, to != landing repo) genuinely fires for this write.
        target_em_id = guard._em_id_for_basename(ctx, target_repo.name)

        fp = target_repo / "cross-repo" / "inbox" / "2099-01-01-cross-repo-test.md"
        fp.parent.mkdir(parents=True)
        content = (
            f"---\nfrom: {target_em_id}\nto: some-other-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        payload = _payload("Write", str(fp), str(session_repo), content=content)

        # Same finding, evaluated IN-REPO (cwd == target repo), still denies
        # exactly as before this fix — in-repo behavior is byte-identical.
        in_repo_payload = _payload("Write", str(fp), str(target_repo), content=content)
        in_repo_result = guard.check(in_repo_payload)
        assert in_repo_result is not None
        _assert_deny_shape(in_repo_result)

        # Cross-repo (cwd == session_repo, target under target_repo): the
        # SAME own-inbox misplacement finding now reaches the schema step
        # (previously `_to_repo_relative` returned None here and the guard
        # produced nothing at all) but is advisory-only, never a deny.
        result = guard.check(payload)
        assert result is not None, (
            "cross-repo write must reach the schema/deny walk, not silently "
            "short-circuit before any validation runs"
        )
        reason = _assert_advisory_shape(result)
        assert "cross-repo/inbox/" in reason
        assert "cross-repo target" in reason

    def test_cross_repo_off_enum_handoff_kind_warns_not_denies(self, tmp_path):
        session_repo = _init_repo(tmp_path, "session-repo-2")
        target_repo = _init_repo(tmp_path, "target-repo-2")

        handoff_dir = target_repo / "state" / "handoffs"
        handoff_dir.mkdir(parents=True)
        fp = handoff_dir / "off-enum.md"
        content = (
            "---\nkind: not-a-real-kind\ntitle: t\ncreated: 2026-07-29\nbranch: main\n"
            "status: open\npredecessor: none\ncategory: infra\n"
            "summary: a one-line summary\n---\nold"
        )
        fp.write_text(content, encoding="utf-8")

        in_repo_payload = _payload("Edit", str(fp), str(target_repo),
                                    old_string="old", new_string="new")
        in_repo_result = guard.check(in_repo_payload)
        assert in_repo_result is not None
        reason = _assert_deny_shape(in_repo_result)
        assert "not-a-real-kind" in reason

        cross_repo_payload = _payload("Edit", str(fp), str(session_repo),
                                       old_string="old", new_string="new")
        result = guard.check(cross_repo_payload)
        assert result is not None
        reason = _assert_advisory_shape(result)
        assert "not-a-real-kind" in reason
        assert "cross-repo target" in reason
